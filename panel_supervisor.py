"""Keep this panel and HTTPS gateway paired with the configured OAS process.

Only process existence is used for shutdown decisions. A slow OAS HTTP request
must never stop its game tasks or its web panel. This helper never controls OAS.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BackendIdentity:
    pid: int
    created: int


def is_configured_backend(executable: str, arguments: list[str], backend_root: Path,
                          runtime: Path | None = None) -> bool:
    """Require the configured runtime and an actual server.py entry argument."""
    def normalized(value: str) -> str:
        return value.replace("/", "\\").rstrip("\\").casefold()

    allowed = {normalized(str(runtime))} if runtime else {
        normalized(str(backend_root / "toolkit" / name))
        for name in ("python.exe", "pythonw.exe")
    }
    if normalized(executable) not in allowed or len(arguments) < 2:
        return False
    # Find Python's actual script entry, excluding arguments passed to another
    # script. Account for -X/-W values and attached variants such as -Xutf8.
    index = 1
    while index < len(arguments):
        option = arguments[index]
        if option == "--":
            index += 1
            break
        if not option.startswith("-") or option == "-":
            break
        if option.startswith("-c") or option.startswith("-m"):
            return False
        if option in ("-X", "-W"):
            if index + 1 >= len(arguments):
                return False
            index += 2
        elif option.startswith(("-X", "-W")):
            index += 1
        elif option.startswith("-") and set(option[1:]) <= set("BEIOPqRsuSvx"):
            index += 1
        else:
            return False
    entry = normalized(str(backend_root / "server.py"))
    return index < len(arguments) and normalized(arguments[index]) in (entry, "server.py", ".\\server.py")


class LifecycleCoordinator:
    """Small injectable state machine; offline tests never launch real services."""
    def __init__(self, controller, record=lambda state: None,
                 stop_grace=2.0, retry_delay=10.0, health_interval=5.0):
        self.controller = controller
        self.record = record
        self.stop_grace = stop_grace
        self.retry_delay = retry_delay
        self.health_interval = health_interval
        self.active = None
        self.backend = None
        self.missing_since = None
        self.retry_at = 0.0
        self.health_at = 0.0

    def step(self, backend: BackendIdentity | None, now: float):
        self.backend = backend
        if backend is not None:
            self.missing_since = None
            if now < self.retry_at:
                return
            healthy = self.active is True
            if healthy and now >= self.health_at:
                # Listener/process inspection only, deliberately no HTTP timeout.
                healthy = self.controller.running()
                self.health_at = now + self.health_interval
            if not healthy:
                self._operate("start", now)
            else:
                self._record("running")
            return
        if self.active is False:
            self._record("waiting_for_oas")
            return
        if self.missing_since is None:
            self.missing_since = now
            self._record("confirming_oas_exit")
        if now - self.missing_since >= self.stop_grace and now >= self.retry_at:
            self._operate("stop", now)

    def _operate(self, action: str, now: float):
        self._record("starting" if action == "start" else "stopping")
        try:
            getattr(self.controller, action)()
        except Exception:
            self.retry_at = now + self.retry_delay
            # Deliberately omit exception text: subprocesses may include settings.
            self._record(action + "_retry")
            return
        self.active = action == "start"
        self.retry_at = 0.0
        self.health_at = now + self.health_interval
        self._record("running" if self.active else "waiting_for_oas")

    def _record(self, phase: str):
        self.record({"phase": phase, "backend_pid": self.backend.pid if self.backend else None,
                     "backend_created": self.backend.created if self.backend else None})


class WindowsProcesses:
    """Inspect TCP owners and process identities with inexpensive Windows APIs."""
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.iphelper = ctypes.WinDLL("iphlpapi", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        self.shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.iphelper.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                                                     wintypes.BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
        self.iphelper.GetExtendedTcpTable.restype = wintypes.DWORD

    def single_instance(self, name: str):
        ctypes.set_last_error(0)
        handle = self.kernel.CreateMutexW(None, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = self.kernel.WaitForSingleObject(handle, 0)
        if acquired not in (0, 128):  # WAIT_OBJECT_0 / WAIT_ABANDONED
            self.kernel.CloseHandle(handle)
            return None
        return handle

    def listening(self) -> dict[int, set[int]]:
        size = wintypes.DWORD(0)
        # AF_INET, TCP_TABLE_OWNER_PID_LISTENER. Both calls are local, no sockets.
        result = self.iphelper.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 3, 0)
        if result not in (0, 122):
            raise OSError(result, "TCP owner inspection failed")
        for _ in range(3):
            buffer = ctypes.create_string_buffer(size.value)
            result = self.iphelper.GetExtendedTcpTable(buffer, ctypes.byref(size), False, 2, 3, 0)
            if result == 122:
                continue
            if result:
                raise OSError(result, "TCP owner inspection failed")
            class Row(ctypes.Structure):
                _fields_ = [(key, wintypes.DWORD) for key in
                            ("state", "local_address", "local_port", "remote_address", "remote_port", "pid")]
            count = ctypes.c_uint32.from_buffer(buffer).value
            required = 4 + count * ctypes.sizeof(Row)
            if required > len(buffer):
                raise OSError("Incomplete TCP owner table")
            owners: dict[int, set[int]] = {}
            for index in range(count):
                row = Row.from_buffer_copy(buffer, 4 + index * ctypes.sizeof(Row))
                owners.setdefault(socket.ntohs(row.local_port & 0xFFFF), set()).add(row.pid)
            return owners
        raise OSError("TCP owner table kept changing")

    def identity(self, pid: int):
        handle = self.kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error in (87, 1168):
                return None
            raise ctypes.WinError(error)
        try:
            if self.kernel.WaitForSingleObject(handle, 0) == 0:
                return None
            created, exited, kernel, user = [wintypes.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in (created, exited, kernel, user))):
                raise ctypes.WinError(ctypes.get_last_error())
            length = wintypes.DWORD(32768)
            image = ctypes.create_unicode_buffer(length.value)
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
                raise ctypes.WinError(ctypes.get_last_error())
            birth = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return BackendIdentity(pid, birth), image.value
        finally:
            self.kernel.CloseHandle(handle)

    def arguments(self, pid: int) -> list[str]:
        # CIM is only used for a previously unseen candidate, never each tick.
        query = (f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}';"
                 "if($p){$p.CommandLine|ConvertTo-Json -Compress}")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=8, creationflags=0x08000000)
        if result.returncode or not result.stdout.strip():
            raise OSError("Could not verify backend entry")
        command = json.loads(result.stdout)
        count = ctypes.c_int()
        pointer = self.shell.CommandLineToArgvW(command, ctypes.byref(count))
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return [pointer[index] for index in range(count.value)]
        finally:
            self.kernel.LocalFree(pointer)


class BackendProbe:
    def __init__(self, processes, backend_root: Path, port=22289, runtime=None):
        self.processes = processes
        self.backend_root = backend_root
        self.port = port
        self.runtime = runtime
        self.bound = None
        self.checked = {}

    def current(self):
        if self.bound is not None:
            found = self.processes.identity(self.bound.pid)
            if found and found[0] == self.bound:
                # Listener/HTTP hiccups during heavy game work do not imply exit.
                return self.bound
            self.bound = None
        owners = self.processes.listening().get(self.port, set())
        for pid in sorted(owners):
            found = self.processes.identity(pid)
            if not found:
                continue
            identity, executable = found
            if identity not in self.checked:
                self.checked[identity] = is_configured_backend(
                    executable, self.processes.arguments(pid), self.backend_root, self.runtime)
            if not self.checked[identity]:
                continue
            # Recheck identity after CIM, closing the exit/PID reuse race.
            verified = self.processes.identity(pid)
            if verified and verified[0] == identity:
                self.bound = identity
                self.checked = {identity: True}
                return identity
        # Bound memory if an unrelated program repeatedly occupies this port.
        if len(self.checked) > 32:
            self.checked.clear()
        return None


class PanelController:
    def __init__(self, panel_root: Path, processes, settings: dict, settings_file: Path):
        self.root = panel_root
        self.processes = processes
        self.settings = settings
        self.settings_file = settings_file

    def _script(self, name: str, *arguments):
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                                 "-ExecutionPolicy", "RemoteSigned", "-WindowStyle", "Hidden",
                                 "-File", str(self.root / name), *arguments],
                                cwd=self.root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=45, creationflags=0x08000000)
        if result.returncode:
            raise OSError("Panel operation failed")

    def start(self):
        arguments = ["-NoBrowser", "-PythonPath", sys.executable,
                     "-SettingsPath", str(self.settings_file)]
        if urlsplit(self.settings["public_origin"]).scheme == "https":
            arguments.append("-Public")
        self._script("Start-Panel.ps1", *arguments)

    def stop(self):
        self._script("Stop-Panel.ps1", "-OnlyIfOasStopped",
                     "-SettingsPath", str(self.settings_file))

    def running(self):
        listeners = self.processes.listening()
        state_root = Path(self.settings["state_dir"])
        public = urlsplit(self.settings["public_origin"])
        expected = [("panel", self.settings["port"], self.root / "app.py")]
        if public.scheme == "https":
            expected.append(("caddy", public.port or 443, self.root / "Caddyfile"))
        for name, port, entry in expected:
            try:
                pid = int((state_root / (name + ".pid")).read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                return False
            identity = self.processes.identity(pid)
            if pid not in listeners.get(port, set()) or not identity:
                return False
            arguments = self.processes.arguments(pid)
            if str(entry).casefold() not in {argument.casefold() for argument in arguments}:
                return False
        return True


def run(panel_root: Path, settings: dict, settings_file: Path):
    processes = WindowsProcesses()
    root_key = hashlib.sha256(str(panel_root).casefold().encode("utf-8")).hexdigest()[:16]
    mutex = processes.single_instance("Local\\OASWebPanelSupervisor-" + root_key)
    if mutex is None:
        return
    state_root = Path(settings["state_dir"])
    state_root.mkdir(parents=True, exist_ok=True)
    pid_file = state_root / "supervisor.pid"
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    state_file = state_root / "supervisor.json"
    previous = None

    def record(state):
        nonlocal previous
        if state == previous:
            return
        previous = dict(state)
        body = {**state, "supervisor_pid": os.getpid(), "updated_at": time.time()}
        temporary = state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(body), encoding="utf-8")
        temporary.replace(state_file)

    coordinator = LifecycleCoordinator(PanelController(panel_root, processes, settings, settings_file), record)
    runtime = Path(settings["backend_python"]) if settings.get("backend_python") else None
    probe = BackendProbe(processes, Path(settings["backend_root"]),
                         urlsplit(settings["backend_url"]).port, runtime)
    try:
        while True:
            try:
                backend = probe.current()
                # Recheck immediately before an exit-triggered stop after grace.
                if backend is None and coordinator.missing_since is not None:
                    backend = probe.current()
                coordinator.step(backend, time.monotonic())
            except Exception:
                # A permission/transient inspection failure is not process exit.
                record({"phase": "inspection_retry", "backend_pid": probe.bound.pid if probe.bound else None})
            time.sleep(0.5)
    finally:
        if pid_file.exists() and pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
            pid_file.unlink()
        processes.kernel.ReleaseMutex(mutex)
        processes.kernel.CloseHandle(mutex)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--check", action="store_true", help="Read identities only; never start or stop services")
    arguments = parser.parse_args()
    root = arguments.panel_root.resolve()
    from app import load_settings
    settings_file = (arguments.settings or root / "settings.json").resolve()
    settings = load_settings(settings_file)
    backend_root = Path(settings["backend_root"])
    runtime = Path(settings["backend_python"]) if settings.get("backend_python") else None
    if arguments.check:
        processes = WindowsProcesses()
        backend = BackendProbe(processes, backend_root, urlsplit(settings["backend_url"]).port, runtime).current()
        print(json.dumps({"backend_pid": backend.pid if backend else None,
                          "backend_created": backend.created if backend else None,
                          "panel_and_https_running": PanelController(root, processes, settings, settings_file).running()}))
    else:
        run(root, settings, settings_file)

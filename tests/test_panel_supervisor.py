"""Offline lifecycle checks. No OAS, panel, Caddy or emulator is launched."""
import os
import tempfile
import time
from pathlib import Path
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Thread

from panel_supervisor import BackendIdentity, BackendProbe, LifecycleCoordinator, PanelController, WindowsProcesses, is_configured_backend


BACKEND_ROOT = Path(r"C:\Example\OAS-Backend")
ONE = BackendIdentity(101, 10001)
TWO = BackendIdentity(102, 10002)


class FakeController:
    def __init__(self):
        self.actions = []
        self.healthy = True
        self.fail_start = False

    def start(self):
        self.actions.append("start")
        if self.fail_start:
            raise OSError("simulated failure")
        self.healthy = True

    def stop(self):
        self.actions.append("stop")
        self.healthy = False

    def running(self):
        return self.healthy


class FakeProcesses:
    def __init__(self):
        self.owners = {22289: {ONE.pid}}
        self.identities = {ONE.pid: (ONE, str(BACKEND_ROOT / "toolkit" / "pythonw.exe"))}
        self.entries = {ONE.pid: ["pythonw.exe", "server.py"]}
        self.argument_calls = 0

    def listening(self):
        return self.owners

    def identity(self, pid):
        return self.identities.get(pid)

    def arguments(self, pid):
        self.argument_calls += 1
        return self.entries[pid]


class PanelLifecycleTests(unittest.TestCase):
    def test_configured_backend_started_and_real_exit_stops_after_grace(self):
        controller = FakeController()
        logic = LifecycleCoordinator(controller)
        logic.step(ONE, 0)
        logic.step(None, 1)
        logic.step(None, 2.9)
        self.assertEqual(controller.actions, ["start"])
        logic.step(None, 3)
        logic.step(None, 4)
        self.assertEqual(controller.actions, ["start", "stop"])

    def test_quick_restart_rebinds_without_stopping_panel(self):
        controller = FakeController()
        logic = LifecycleCoordinator(controller)
        logic.step(ONE, 0)
        logic.step(None, 10)
        logic.step(TWO, 11)
        logic.step(TWO, 20)
        self.assertEqual(controller.actions, ["start"])
        self.assertEqual(logic.backend, TWO)

    def test_later_backend_restart_restores_panel_and_https(self):
        controller = FakeController()
        logic = LifecycleCoordinator(controller)
        for backend, now in ((ONE, 0), (None, 1), (None, 3), (TWO, 20)):
            logic.step(backend, now)
        self.assertEqual(controller.actions, ["start", "stop", "start"])

    def test_http_timeout_has_no_shutdown_path(self):
        processes = FakeProcesses()
        probe = BackendProbe(processes, BACKEND_ROOT)
        self.assertEqual(probe.current(), ONE)
        # HTTP and even the listening socket may be temporarily unavailable.
        processes.owners = {}
        controller = FakeController()
        logic = LifecycleCoordinator(controller)
        for now in range(60):
            logic.step(probe.current(), now)
        self.assertEqual(controller.actions, ["start"])
        self.assertEqual(processes.argument_calls, 1)

    def test_xy_runtime_and_non_server_python_do_not_match(self):
        allowed = str(BACKEND_ROOT / "toolkit" / "pythonw.exe")
        self.assertTrue(is_configured_backend(allowed, [allowed, "server.py"], BACKEND_ROOT))
        self.assertTrue(is_configured_backend(allowed, [allowed, "-X", "utf8", str(BACKEND_ROOT / "server.py")], BACKEND_ROOT))
        self.assertFalse(is_configured_backend(r"C:\Example\Other-Backend\toolkit\pythonw.exe", ["pythonw.exe", "server.py"], BACKEND_ROOT))
        self.assertFalse(is_configured_backend(allowed, [allowed, "script.py"], BACKEND_ROOT))
        self.assertFalse(is_configured_backend(allowed, [allowed, "-c", "server.py"], BACKEND_ROOT))
        self.assertFalse(is_configured_backend(allowed, [allowed, "-m", "server.py"], BACKEND_ROOT))
        self.assertFalse(is_configured_backend(allowed, [allowed, "other.py", "server.py"], BACKEND_ROOT))
        self.assertFalse(is_configured_backend(allowed, [allowed, "-X", "server.py", "other.py"], BACKEND_ROOT))
        self.assertTrue(is_configured_backend(allowed, [allowed, "-B", "-Xutf8", "-W", "ignore", "server.py"], BACKEND_ROOT))

    def test_wrong_port_owner_never_starts_panel(self):
        processes = FakeProcesses()
        processes.identities[ONE.pid] = (ONE, r"C:\unrelated\pythonw.exe")
        probe = BackendProbe(processes, BACKEND_ROOT)
        self.assertIsNone(probe.current())

    def test_configured_port_and_external_runtime_identify_only_the_backend(self):
        processes = FakeProcesses()
        runtime = BACKEND_ROOT / '.venv' / 'Scripts' / 'python.exe'
        processes.owners = {23456: {ONE.pid}}
        processes.identities[ONE.pid] = (ONE, str(runtime))
        self.assertIsNone(BackendProbe(processes, BACKEND_ROOT).current())
        self.assertIsNone(BackendProbe(processes, BACKEND_ROOT, port=23456).current())
        self.assertEqual(BackendProbe(processes, BACKEND_ROOT, port=23456, runtime=runtime).current(), ONE)

    def test_controller_uses_settings_file_and_optional_https(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = {'port': 23457, 'public_origin': 'http://127.0.0.1:23457', 'state_dir': str(root)}
            controller = PanelController(root, FakeProcesses(), settings, root / 'custom-settings.json')
            calls = []
            controller._script = lambda *arguments: calls.append(arguments)
            controller.start()
            self.assertNotIn('-Public', calls[-1])
            self.assertIn(str(root / 'custom-settings.json'), calls[-1])
            settings['public_origin'] = 'https://panel.example.com:4443'
            controller.start()
            self.assertIn('-Public', calls[-1])
            controller.stop()
            self.assertIn('-OnlyIfOasStopped', calls[-1])

    def test_controller_health_requires_configured_listener_and_exact_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'panel.pid').write_text(str(ONE.pid), encoding='ascii')
            settings = {'port': 23457, 'public_origin': 'http://127.0.0.1:23457', 'state_dir': str(root)}
            processes = FakeProcesses()
            processes.owners = {23457: {ONE.pid}}
            processes.entries[ONE.pid] = ['python.exe', str(root / 'app.py')]
            controller = PanelController(root, processes, settings, root / 'settings.json')
            self.assertTrue(controller.running())
            processes.entries[ONE.pid] = ['python.exe', str(root / 'other.py')]
            self.assertFalse(controller.running())
            processes.entries[ONE.pid] = ['python.exe', str(root / 'app.py')]
            processes.owners = {22300: {ONE.pid}}
            self.assertFalse(controller.running())

    def test_pid_reuse_rechecks_creation_identity(self):
        processes = FakeProcesses()
        probe = BackendProbe(processes, BACKEND_ROOT)
        self.assertEqual(probe.current(), ONE)
        replaced = BackendIdentity(ONE.pid, ONE.created + 1)
        processes.identities[ONE.pid] = (replaced, r"C:\unrelated\pythonw.exe")
        self.assertIsNone(probe.current())
        self.assertEqual(processes.argument_calls, 2)

    def test_start_failure_retries_with_backoff(self):
        controller = FakeController()
        controller.fail_start = True
        logic = LifecycleCoordinator(controller)
        logic.step(ONE, 0)
        logic.step(ONE, 1)
        logic.step(ONE, 9)
        self.assertEqual(controller.actions, ["start"])
        controller.fail_start = False
        logic.step(ONE, 10)
        self.assertEqual(controller.actions, ["start", "start"])

    def test_crashed_panel_is_restored_without_touching_backend(self):
        controller = FakeController()
        logic = LifecycleCoordinator(controller)
        logic.step(ONE, 0)
        controller.healthy = False
        logic.step(ONE, 5)
        self.assertEqual(controller.actions, ["start", "start"])
        self.assertEqual(logic.backend, ONE)

    def test_running_state_callback_contains_no_credentials(self):
        records = []
        logic = LifecycleCoordinator(FakeController(), records.append)
        logic.step(ONE, 0)
        for record in records:
            self.assertEqual(set(record), {"phase", "backend_pid", "backend_created"})

    @unittest.skipUnless(os.name == "nt", "Windows named mutex")
    def test_named_mutex_allows_one_supervisor_only_and_releases_on_exit(self):
        processes = WindowsProcesses()
        name = "Local\\OASPanelTest_" + uuid.uuid4().hex
        first = processes.single_instance(name)
        self.assertIsNotNone(first)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                self.assertIsNone(executor.submit(processes.single_instance, name).result())
        finally:
            processes.kernel.ReleaseMutex(first)
            processes.kernel.CloseHandle(first)
        replacement = processes.single_instance(name)
        self.assertIsNotNone(replacement)
        processes.kernel.ReleaseMutex(replacement)
        processes.kernel.CloseHandle(replacement)

    @unittest.skipUnless(os.name == "nt", "Windows named mutex")
    def test_abandoned_mutex_can_be_taken_over(self):
        processes = WindowsProcesses()
        name = "Local\\OASPanelTest_" + uuid.uuid4().hex
        abandoned = []
        thread = Thread(target=lambda: abandoned.append(processes.single_instance(name)))
        thread.start()
        thread.join()
        # Python join may return before Windows finishes native thread teardown.
        # Abandonment becomes visible once that OS thread has actually exited.
        replacement = None
        deadline = time.monotonic() + 1
        while replacement is None and time.monotonic() < deadline:
            replacement = processes.single_instance(name)
            if replacement is None:
                time.sleep(0.01)
        self.assertIsNotNone(replacement)
        processes.kernel.ReleaseMutex(replacement)
        processes.kernel.CloseHandle(replacement)
        processes.kernel.CloseHandle(abandoned[0])


if __name__ == "__main__":
    unittest.main()

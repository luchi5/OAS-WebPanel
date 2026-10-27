"""Bounded HTTPS gateway startup; only a verified gateway identity may be reclaimed."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import time

from panel_supervisor import WindowsProcesses


def gateway_arguments(arguments, executable, config):
    normalize = lambda value: str(value).replace('/', '\\').rstrip('\\').casefold()
    return (len(arguments) == 6 and normalize(arguments[0]) == normalize(executable)
            and arguments[1:3] == ['run', '--config']
            and normalize(arguments[3]) == normalize(config)
            and arguments[4:] == ['--adapter', 'caddyfile'])


class GatewayGuard:
    def __init__(self, runtime, *, now=time.monotonic, sleep=time.sleep,
                 timeout=10.0, record=lambda phase, identity=None: None):
        self.runtime, self.now, self.sleep = runtime, now, sleep
        self.timeout, self.record = timeout, record

    def wait_ready(self, identity):
        until = self.now() + self.timeout
        while True:
            owners = self.runtime.owners()
            if owners - {identity.pid}:
                raise RuntimeError('Public port belongs to another process; no process was stopped')
            if not self.runtime.alive(identity):
                return False
            if identity.pid in owners:
                return True
            if self.now() >= until:
                return False
            self.sleep(min(0.25, until - self.now()))

    def ensure(self):
        existing = self.runtime.existing()
        if len(existing) > 1:
            raise RuntimeError('Multiple matching gateway instances; no process was stopped')
        if existing:
            identity = existing[0]
            self.record('checking', identity)
            if self.wait_ready(identity):
                self.record('ready', identity)
                return identity
            self.record('stalled', identity)
            # Revalidation belongs inside terminate, including creation time.
            self.runtime.terminate(identity)
        if self.runtime.owners():
            raise RuntimeError('Public port is occupied; no process was stopped')
        self.runtime.validate()
        identity = self.runtime.start()
        self.record('starting', identity)
        if self.wait_ready(identity):
            self.record('ready', identity)
            return identity
        self.record('failed', identity)
        self.runtime.terminate(identity)
        raise RuntimeError('Gateway did not begin listening before the startup deadline')


class GatewayRuntime:
    def __init__(self, root, port, processes):
        self.root, self.port, self.processes = root, port, processes
        self.executable = root / 'tools' / 'caddy.exe'
        self.config = root / 'Caddyfile'
        self.arguments = [str(self.executable), 'run', '--config', str(self.config), '--adapter', 'caddyfile']
        self.env = {**os.environ, 'XDG_DATA_HOME': str(root / 'state' / 'caddy-data'),
                    'XDG_CONFIG_HOME': str(root / 'state' / 'caddy-config')}

    def existing(self):
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            "@(Get-CimInstance Win32_Process -Filter \"Name='caddy.exe'\" | Select-Object -ExpandProperty ProcessId) | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=8, creationflags=0x08000000)
        if result.returncode:
            raise OSError('Gateway inspection failed')
        pids = (json.loads(result.stdout) if result.stdout.strip() else []) or []
        if isinstance(pids, int):
            pids = [pids]
        matched = []
        for pid in pids:
            found = self.processes.identity(pid)
            if not found or Path(found[1]) != self.executable:
                continue
            identity, _ = found
            if not gateway_arguments(self.processes.arguments(pid), self.executable, self.config):
                raise RuntimeError('Gateway executable is used by a different command; no process was stopped')
            if self.alive(identity):
                matched.append(identity)
        return matched

    def owners(self):
        return self.processes.listening().get(self.port, set())

    def alive(self, identity):
        found = self.processes.identity(identity.pid)
        return bool(found and found[0] == identity and Path(found[1]) == self.executable)

    def terminate(self, identity):
        if not self.alive(identity):
            return
        if not gateway_arguments(self.processes.arguments(identity.pid), self.executable, self.config):
            raise RuntimeError('Gateway identity changed; no process was stopped')
        self.processes.terminate_verified(identity, str(self.executable))

    def validate(self):
        result = subprocess.run([str(self.executable), 'validate', '--config', str(self.config), '--adapter', 'caddyfile'],
                                cwd=self.root, env=self.env, timeout=10, creationflags=0x08000000,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise RuntimeError('Gateway configuration validation failed')

    def start(self):
        with (self.root / 'logs' / 'caddy.stdout.log').open('ab') as output, (self.root / 'logs' / 'caddy.stderr.log').open('ab') as error:
            process = subprocess.Popen(self.arguments, cwd=self.root, env=self.env,
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                                       creationflags=0x08000000)
        found = self.processes.identity(process.pid)
        if not found or Path(found[1]) != self.executable or process.poll() is not None:
            raise RuntimeError('Gateway exited during startup')
        if not gateway_arguments(self.processes.arguments(process.pid), self.executable, self.config) or not self.alive(found[0]):
            raise RuntimeError('Gateway identity could not be confirmed')
        return found[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--panel-root', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    root = args.panel_root.resolve()
    processes = WindowsProcesses()
    mutex = processes.single_instance('Local\\OnmyojiPrivateGatewayOperation')
    if mutex is None:
        raise SystemExit('Another gateway operation is still in progress.')
    state = root / 'state'
    previous = None
    def record(phase, identity=None):
        nonlocal previous
        value = {'phase': phase, 'updated_at': time.time(), 'process': asdict(identity) if identity else None}
        if phase == 'retry_required' and previous:
            value.update(previous_phase=previous['phase'], process=previous['process'])
        temporary = state / 'gateway.json.tmp'
        temporary.write_text(json.dumps(value), encoding='utf-8')
        temporary.replace(state / 'gateway.json')
        previous = value
    try:
        identity = GatewayGuard(GatewayRuntime(root, args.port, processes), record=record).ensure()
        (state / 'caddy.pid').write_text(str(identity.pid), encoding='ascii')
        print('HTTPS gateway is listening.')
    except Exception:
        record('retry_required')
        raise SystemExit('HTTPS gateway could not be confirmed ready. See state/gateway.json and logs/caddy.stderr.log.')
    finally:
        processes.kernel.ReleaseMutex(mutex)
        processes.kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()

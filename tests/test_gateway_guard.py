"""Fake process/clock tests; never inspect or control a real gateway."""
from pathlib import Path
import unittest

from gateway_guard import GatewayGuard, GatewayRuntime, gateway_arguments
from panel_supervisor import BackendIdentity, LifecycleCoordinator, PanelController, WindowsProcesses
from tests.test_panel_supervisor import FakeController, ONE


OLD = BackendIdentity(5, 100)
NEW = BackendIdentity(6, 200)


class FakeRuntime:
    def __init__(self, existing=(), *, ready_at=None):
        self.identities = list(existing)
        self.alive_ids = set(existing)
        self.clock = 0
        self.ready_at = ready_at
        self.listener = set()
        self.actions = []
        self.spawn_ready = True

    def existing(self): return self.identities
    def alive(self, identity): return identity in self.alive_ids
    def owners(self):
        if self.ready_at is not None and self.clock >= self.ready_at and self.identities:
            return {self.identities[0].pid}
        return self.listener
    def sleep(self, seconds): self.clock += seconds
    def terminate(self, identity):
        self.actions.append(('terminate', identity))
        self.alive_ids.discard(identity)
        self.identities = []
        self.listener.discard(identity.pid)
    def validate(self): self.actions.append(('validate',))
    def start(self):
        self.actions.append(('start',))
        self.alive_ids.add(NEW)
        if self.spawn_ready: self.listener = {NEW.pid}
        return NEW


class GatewayTests(unittest.TestCase):
    def guard(self, runtime):
        return GatewayGuard(runtime, now=lambda: runtime.clock, sleep=runtime.sleep, timeout=2)

    def test_running_instance_is_retained(self):
        runtime = FakeRuntime([OLD], ready_at=0)
        self.assertEqual(self.guard(runtime).ensure(), OLD)
        self.assertEqual(runtime.actions, [])

    def test_starting_instance_has_a_bounded_grace_period(self):
        runtime = FakeRuntime([OLD], ready_at=1)
        self.assertEqual(self.guard(runtime).ensure(), OLD)
        self.assertEqual(runtime.clock, 1)
        self.assertEqual(runtime.actions, [])

    def test_stalled_verified_instance_is_replaced_once(self):
        runtime = FakeRuntime([OLD])
        self.assertEqual(self.guard(runtime).ensure(), NEW)
        self.assertEqual(runtime.clock, 2)
        self.assertEqual(runtime.actions, [('terminate', OLD), ('validate',), ('start',)])

    def test_occupied_port_never_stops_any_process(self):
        for identities in ([], [OLD]):
            runtime = FakeRuntime(identities)
            runtime.listener = {999}
            with self.assertRaises(RuntimeError): self.guard(runtime).ensure()
            self.assertEqual(runtime.actions, [])

    def test_ambiguous_multiple_instances_are_left_alone(self):
        runtime = FakeRuntime([OLD, NEW])
        with self.assertRaises(RuntimeError): self.guard(runtime).ensure()
        self.assertEqual(runtime.actions, [])

    def test_new_stalled_instance_is_cleaned_without_restart_loop(self):
        runtime = FakeRuntime()
        runtime.spawn_ready = False
        with self.assertRaises(RuntimeError): self.guard(runtime).ensure()
        self.assertEqual(runtime.actions, [('validate',), ('start',), ('terminate', NEW)])
        self.assertEqual(runtime.clock, 2)

    def test_exited_instance_has_no_process_to_wait_for(self):
        runtime = FakeRuntime([OLD])
        runtime.alive_ids.clear()
        self.assertEqual(self.guard(runtime).ensure(), NEW)
        self.assertEqual(runtime.clock, 0)

    def test_pid_reuse_does_not_match_identity(self):
        class Processes:
            def identity(self, pid): return BackendIdentity(OLD.pid, OLD.created + 1), str(root / 'tools' / 'caddy.exe')
            def arguments(self, pid): raise AssertionError('Must not inspect or kill replacement')
            def terminate_verified(self, *args): raise AssertionError('Must not kill replacement')
        root = Path('C:/temporary-panel')
        runtime = GatewayRuntime(root, 12345, Processes())
        runtime.terminate(OLD)

    def test_command_must_match_exact_config_and_run_entry(self):
        exe, config = 'C:/panel/tools/caddy.exe', 'C:/panel/Caddyfile'
        args = [exe, 'run', '--config', config, '--adapter', 'caddyfile']
        self.assertTrue(gateway_arguments(args, exe, config))
        self.assertFalse(gateway_arguments(args, exe, 'C:/other/Caddyfile'))
        self.assertFalse(gateway_arguments([exe, 'validate', *args[2:]], exe, config))
        self.assertFalse(gateway_arguments([*args, '--watch'], exe, config))

    def test_supervisor_does_not_report_ready_just_because_script_returned(self):
        controller = FakeController()
        controller.healthy = False
        controller.start = lambda: controller.actions.append('start')
        records = []
        logic = LifecycleCoordinator(controller, records.append)
        logic.step(ONE, 0)
        self.assertEqual(records[-1]['phase'], 'start_retry')
        self.assertIsNone(logic.active)
        logic.step(ONE, 1)
        self.assertEqual(controller.actions, ['start'])

    def test_termination_checks_creation_time_on_the_same_native_handle(self):
        class Kernel:
            terminated = False
            created = OLD.created
            def OpenProcess(self, *args): return 42
            def CloseHandle(self, handle): self.closed = handle
            def WaitForSingleObject(self, handle, wait): return 0 if self.terminated else 258
            def GetProcessTimes(self, handle, created, *args):
                created._obj.dwLowDateTime = self.created
                return True
            def QueryFullProcessImageNameW(self, handle, flags, image, length):
                image.value = str(executable)
                return True
            def TerminateProcess(self, handle, status):
                self.terminated = True
                return True
        executable = Path('C:/temporary-panel/tools/caddy.exe')
        processes = WindowsProcesses.__new__(WindowsProcesses)
        processes.kernel = Kernel()
        processes.kernel.created += 1
        with self.assertRaises(RuntimeError): processes.terminate_verified(OLD, str(executable))
        self.assertFalse(processes.kernel.terminated)
        processes.kernel.created = OLD.created
        processes.terminate_verified(OLD, str(executable))
        self.assertTrue(processes.kernel.terminated)
        self.assertEqual(processes.kernel.closed, 42)

    def test_ready_probe_rejects_same_executable_with_wrong_config(self):
        root = Path('C:/temporary-panel')
        class Processes:
            def listening(self): return {22300: {OLD.pid}, 22290: {NEW.pid}}
            def identity(self, pid):
                return ((OLD, str(root / '.venv' / 'Scripts' / 'python.exe')) if pid == OLD.pid
                        else (NEW, str(root / 'tools' / 'caddy.exe')))
            def arguments(self, pid):
                return ([str(root / '.venv' / 'Scripts' / 'python.exe'), '-B', str(root / 'app.py')] if pid == OLD.pid
                        else [str(root / 'tools' / 'caddy.exe'), 'run', '--config', 'C:/other/Caddyfile', '--adapter', 'caddyfile'])
        from unittest.mock import patch
        def pid_file(path, *args, **kwargs):
            return str(OLD.pid if path.name == 'panel.pid' else NEW.pid)
        with patch.object(Path, 'read_text', pid_file):
            self.assertFalse(PanelController(root, Processes(), {'state_dir': str(root / 'state'), 'port': 22300, 'public_origin': 'https://panel.example.com:22290'}, root / 'settings.json').running())


if __name__ == '__main__':
    unittest.main()

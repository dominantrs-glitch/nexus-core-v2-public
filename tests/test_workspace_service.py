import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from nexus import workspace_service as service


class ServiceTests(unittest.TestCase):
    def test_lock_released_and_stale_connected_not_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service.write_json(root/'status.json', dict(state='connected', updated=time.time()))
            self.assertFalse(service.status(root)['running'])
            with service.instance_lock(root) as first:
                self.assertTrue(first)
                with service.instance_lock(root) as second:
                    self.assertFalse(second)
                service.write_json(root/'status.json', dict(state='connected', updated=0))
                self.assertEqual(service.status(root)['state'], 'unresponsive')
            with service.instance_lock(root) as again:
                self.assertTrue(again)

    def test_old_stop_does_not_stop_new_instance(self):
        async def scenario(root):
            entered = asyncio.Event()
            async def fake_activate(workspace, callback, **kwargs):
                callback('connected'); entered.set()
                await asyncio.Future()
            service.write_json(root/'stop.json', dict(instance='old-instance'))
            with patch('nexus.intake_connector.activate', fake_activate):
                host = asyncio.create_task(service.supervise(root, root))
                await entered.wait()
                await asyncio.sleep(.05)
                self.assertFalse(host.done())
                data = service.read_json(root/'status.json')
                self.assertEqual(data['state'], 'connected')
                service.write_json(root/'stop.json', dict(instance=data['instance']))
                await asyncio.wait_for(host, 3)
                self.assertEqual(service.read_json(root/'status.json')['state'], 'stopped')
        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_failure_has_no_exception_payload(self):
        async def fail(*args, **kwargs):
            raise ValueError('PRIVATE-TOKEN-AND-CONTENT')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch('nexus.intake_connector.activate', fail):
                asyncio.run(service.supervise(root, root))
            saved = (root/'status.json').read_text()
            self.assertNotIn('PRIVATE', saved)
            self.assertEqual(json.loads(saved)['state'], 'error')
            self.assertNotIn('PRIVATE', (root/'events.json').read_text())

    def test_unexpected_activation_crash_restarts_at_most_three_times(self):
        calls=[]
        async def fail(*args, **kwargs):
            calls.append(1)
            raise RuntimeError('PRIVATE exception payload')
        real_sleep=asyncio.sleep
        async def fast_sleep(delay):
            await real_sleep(0)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with patch('nexus.intake_connector.activate',fail), patch('nexus.workspace_service.asyncio.sleep',fast_sleep):
                asyncio.run(service.supervise(root,root))
            self.assertEqual(len(calls),3)
            self.assertEqual(service.read_json(root/'status.json')['state'],'error')
            events=service.read_json(root/'events.json')['events']
            self.assertEqual(events[-1]['state'],'error')
            self.assertNotIn('PRIVATE',json.dumps(events))

    def test_transition_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for index in range(125):
                service.record_state(root,'reconnecting',str(index))
            events=service.read_json(root/'events.json')['events']
            self.assertEqual(len(events),100)
            self.assertEqual(events[0]['instance'],'25')

    def test_real_windows_read_lock_does_not_end_connection_and_recovers(self):
        async def scenario(root):
            entered = asyncio.Event()
            cancelled = []
            async def activate(workspace, callback, **kwargs):
                callback('connected'); entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.append(True)
            with patch('nexus.intake_connector.activate', activate):
                host = asyncio.create_task(service.supervise(root, root))
                try:
                    await entered.wait()
                    instance = service.read_json(root/'status.json')['instance']
                    with (root/'status.json').open('rb'):
                        await asyncio.sleep(1.1)
                        self.assertFalse(host.done())
                        self.assertFalse(cancelled)
                        failures = [e for e in service.read_json(root/'events.json')['events']
                                    if e.get('reason') == 'status_write_failed']
                        self.assertEqual(len(failures), 1)
                        self.assertEqual(failures[0]['error_kind'], 'permission')
                    await asyncio.sleep(1.1)
                    self.assertFalse(host.done())
                    self.assertEqual(service.read_json(root/'status.json')['state'], 'connected')
                    self.assertIn('status_write_recovered',
                                  [e.get('reason') for e in service.read_json(root/'events.json')['events']])
                    service.write_json(root/'stop.json', dict(instance=instance))
                    await asyncio.wait_for(host, 2)
                    self.assertEqual(service.read_json(root/'status.json')['reason'], 'owner_stop')
                finally:
                    host.cancel()
                    await asyncio.gather(host, return_exceptions=True)
        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_history_lock_retains_events_and_recovers_without_interrupting_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service.record_state(root, 'stopped', 'old')
            reporter = service.ConnectionReporter(root, 'abc123')
            reporter.update('connected')
            with (root/'events.json').open('rb'):
                reporter.update('reconnecting')
                reporter.diagnostic('network_unavailable')
                for _ in range(3):
                    reporter.update()
                self.assertEqual(service.read_json(root/'status.json')['state'], 'reconnecting')
                self.assertEqual(len([e for e in reporter.events if e.get('reason') == 'history_write_failed']), 1)
            reporter.update('connected')
            reporter.flush()
            events = service.read_json(root/'events.json')['events']
            self.assertEqual(events[0]['instance'], 'old')
            self.assertIn('network_unavailable', [e.get('reason') for e in events])
            self.assertIn('history_write_recovered', [e.get('reason') for e in events])
            self.assertEqual(service.read_json(root/'status.json')['reporting_issues'], [])

    def test_all_storage_failure_does_not_cancel_running_service(self):
        async def scenario(root):
            entered = asyncio.Event()
            cancelled = []
            async def activate(workspace, callback, **kwargs):
                callback('connected'); entered.set()
                kwargs['on_diagnostic']('network_unavailable')
                try:
                    await asyncio.Future()
                finally:
                    cancelled.append(True)
            with patch('nexus.intake_connector.activate', activate):
                with patch.object(service, 'write_json', side_effect=OSError(28, 'PRIVATE payload')):
                    host = asyncio.create_task(service.supervise(root, root))
                    await entered.wait()
                    await asyncio.sleep(1.1)
                    self.assertFalse(host.done())
                    self.assertFalse(cancelled)
                await asyncio.sleep(1.1)
                value = service.read_json(root/'status.json')
                self.assertEqual(value['state'], 'connected')
                service.write_json(root/'stop.json', dict(instance=value['instance']))
                await asyncio.wait_for(host, 2)
                self.assertNotIn('PRIVATE', (root/'events.json').read_text())
        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_terminal_reason_survives_when_only_status_file_is_unwritable(self):
        async def fail(*args, **kwargs):
            raise PermissionError(5, 'PRIVATE secret and filename')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = service.write_json
            def deny_status(path, value):
                if path.name == 'status.json':
                    raise PermissionError(5, 'PRIVATE')
                original(path, value)
            with patch('nexus.intake_connector.activate', fail), patch.object(service, 'write_json', deny_status):
                asyncio.run(service.supervise(root, root))
            value = service.status(root)
            self.assertFalse(value['running'])
            self.assertEqual(value['state'], 'error')
            self.assertEqual(value['reason'], 'access_denied')
            self.assertNotIn('PRIVATE', json.dumps(service.diagnostics(root)))

    def test_fatal_causes_are_classified_without_exception_text(self):
        for error, expected in [(ValueError('PRIVATE'), 'configuration_invalid'),
                                (FileNotFoundError('PRIVATE'), 'required_file_missing')]:
            async def fail(*args, **kwargs):
                raise error
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch('nexus.intake_connector.activate', fail):
                    asyncio.run(service.supervise(root, root))
                diagnostic = service.diagnostics(root)
                self.assertEqual(diagnostic['status']['reason'], expected)
                self.assertNotIn('PRIVATE', json.dumps(diagnostic))

    def test_unsaved_events_are_bounded_and_corrupt_history_does_not_inject_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service.write_json(root/'events.json', {'events': [None, {'state': {}},
                {'time': time.time(), 'state': 'error', 'instance': 'abc', 'reason': {}, 'secret': 'PRIVATE'}]})
            reporter = service.ConnectionReporter(root, 'abc123')
            with patch.object(service, 'write_json', side_effect=PermissionError('PRIVATE')):
                for _ in range(150):
                    reporter.diagnostic('network_unavailable')
            self.assertEqual(len(reporter.events), 100)
            reporter.update('connected')
            reporter.flush()
            self.assertNotIn('PRIVATE', (root/'events.json').read_text())

    def test_unavailable_status_does_not_start_a_duplicate_process(self):
        with patch.object(service, 'status', return_value={'running': None, 'state': 'unresponsive'}), \
                patch.object(service.subprocess, 'Popen') as spawn:
            self.assertEqual(service.start()['state'], 'unresponsive')
            spawn.assert_not_called()


if __name__ == '__main__': unittest.main()

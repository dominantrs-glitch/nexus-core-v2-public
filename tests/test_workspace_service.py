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
            async def fake_activate(workspace, callback):
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
        async def fail(*args):
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
        async def fail(*args):
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


if __name__ == '__main__': unittest.main()

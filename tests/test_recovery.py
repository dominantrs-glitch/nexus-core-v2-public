import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nexus.recovery import ReconnectBackoff


class RecoveryTests(unittest.TestCase):
    def test_circuit_bounds_failures_until_real_health(self):
        retry = ReconnectBackoff()
        self.assertEqual([retry.failed()[1] for _ in range(8)], [1, 2, 4, 8, 16, 300, 300, 300])
        self.assertEqual(retry.failures, 6)
        retry.healthy()
        self.assertEqual(retry.failed(), ('reconnecting', 1))

    def test_repeated_socket_open_close_does_not_reset_backoff(self):
        from nexus.connector import relay_app
        @asynccontextmanager
        async def lifespan(app):
            yield
        class Socket:
            async def recv(self):
                raise OSError('private data must not enter diagnostics')
            async def send(self, data):
                pass
        @asynccontextmanager
        async def connect(*args, **kwargs):
            yield Socket()
        pauses, states, reasons = [], [], []
        async def sleep(delay):
            pauses.append(delay)
            if delay == 300:
                raise asyncio.CancelledError()
        app = SimpleNamespace(router=SimpleNamespace(lifespan_context=lifespan))
        async def run():
            with patch('nexus.connector.connect', connect), patch('nexus.connector.asyncio.sleep', sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await relay_app('https://synthetic.invalid', 'a'*43, 'synthetic-probe', app,
                                    on_state=states.append, on_diagnostic=reasons.append)
        asyncio.run(run())
        self.assertEqual(pauses, [1, 2, 4, 8, 16, 300])
        self.assertEqual(states[-1], 'circuit_open')
        self.assertEqual(reasons, ['network_unavailable'] * 6)

    def test_transport_causes_are_fixed_categories_without_server_details(self):
        import httpx2
        from websockets.exceptions import InvalidStatus, ConnectionClosed
        from websockets.frames import Close
        from nexus.connector import RelayReplyRejected, transport_reason
        cases = [
            (InvalidStatus(SimpleNamespace(status_code=401, reason_phrase='PRIVATE')), 'relay_access_denied'),
            (InvalidStatus(SimpleNamespace(status_code=409, reason_phrase='PRIVATE')), 'connector_already_online'),
            (RelayReplyRejected(429), 'relay_rate_limited'),
            (RelayReplyRejected(503), 'relay_reply_rejected'),
            (httpx2.ReadTimeout('PRIVATE url/token'), 'transport_timeout'),
            (OSError('PRIVATE path'), 'network_unavailable'),
            (ValueError('PRIVATE request'), 'invalid_relay_data'),
            (ConnectionClosed(Close(1001, 'PRIVATE'), None), 'connection_closed'),
        ]
        for error, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(transport_reason(error), reason)


if __name__ == '__main__':
    unittest.main()

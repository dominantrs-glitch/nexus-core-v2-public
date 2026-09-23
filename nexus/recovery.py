"""Small bounded reconnect policy; no request payloads or hidden store fallback."""
from dataclasses import dataclass


@dataclass
class ReconnectBackoff:
    failures: int = 0

    def healthy(self):
        self.failures = 0

    def failed(self):
        self.failures = min(self.failures + 1, 6)
        if self.failures >= 6:
            # A bounded circuit with one recovery probe every five minutes.
            # Opening a socket alone does not reset repeated failures.
            return 'circuit_open', 300
        return 'reconnecting', min(2 ** (self.failures - 1), 60)

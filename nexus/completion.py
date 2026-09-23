"""Trusted local wiring for operation-specific, live completion checks."""
from dataclasses import dataclass
from typing import Callable

from nexus.artifacts import Access, ArtifactStore, InsufficientContext
from nexus.context import ContextRecord, resolve_context


@dataclass(frozen=True)
class CompletionGate:
    project: str
    contract_revision: int
    principal: str
    operation: str
    required_context: frozenset[str]
    required_authority: dict[str, str]
    required_artifacts: tuple[tuple[str, int], ...]
    records: Callable[[], tuple[ContextRecord, ...]]
    artifacts: ArtifactStore

    def __call__(self, project, contract_revision, output):
        if project != self.project or contract_revision != self.contract_revision:
            raise InsufficientContext('completion policy unavailable')
        access = Access(self.principal, project)
        resolve_context(self.records(), access, self.operation,
                        required=self.required_context, required_authority=self.required_authority)
        # open_verified checks complete bytes before returning the stream; not metadata alone.
        for artifact_id, revision in self.required_artifacts:
            with self.artifacts.open_verified(artifact_id, revision, access):
                pass
        with self.artifacts.open_verified(output['artifact_id'], output['revision'], access) as opened:
            manifest, stream = opened
            if manifest['sha256'] != output['sha256']:
                raise InsufficientContext('output identity mismatch')

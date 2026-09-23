"""Provider-neutral, deterministic context selection; no model or host state.

This is a resolver increment, not a Project store or a confirmation service.
Records must come from a trusted, schema/confirmation-validating repository.
Do not expose record construction or authority changes as model-writable input.
"""
from dataclasses import dataclass
from typing import Literal

from nexus.artifacts import Access, InsufficientContext


@dataclass(frozen=True)
class ContextRecord:
    id: str
    revision: int
    kind: Literal["decision", "personal", "reference", "candidate"]
    authority: Literal["confirmed", "verified", "derived", "candidate", "unknown"]
    status: Literal["current", "historical", "superseded", "suppressed"]
    projects: frozenset[str]
    operations: frozenset[str]
    readers: frozenset[str]
    source_id: str
    body: str
    source_revision: int = 1
    approval_receipt: str | None = None

    def __post_init__(self):
        if (not self.id or type(self.revision) is not int or self.revision < 1 or
            self.kind not in {"decision", "personal", "reference", "candidate"} or
            self.authority not in {"confirmed", "verified", "derived", "candidate", "unknown"} or
            self.status not in {"current", "historical", "superseded", "suppressed"} or
            not self.source_id or not isinstance(self.body, str) or
            type(self.source_revision) is not int or self.source_revision < 1):
            raise ValueError("invalid context record")
        if self.approval_receipt is not None and (not isinstance(self.approval_receipt, str) or not self.approval_receipt):
            raise ValueError("invalid approval receipt")
        for values in (self.projects, self.operations, self.readers):
            if not isinstance(values, frozenset) or not values or not all(isinstance(v, str) and v for v in values):
                raise ValueError("explicit scope and readers required")
        if self.kind == "candidate" and self.authority != "candidate":
            raise ValueError("a learning candidate cannot be binding")


@dataclass(frozen=True)
class SelectedContext:
    record: ContextRecord
    binding: bool


@dataclass(frozen=True)
class Resolution:
    operation: str
    items: tuple[SelectedContext, ...]
    # Aggregate observation only: never reveal hidden identities or bodies.
    excluded: int


def resolve_context(records: tuple[ContextRecord, ...], access: Access, operation: str,
                    *, required: frozenset[str], required_authority: dict[str, str] | None = None) -> Resolution:
    """Select current authorized records; never substitute stale/derived content.

    Both the operation and required IDs are a server-side operation policy, not
    optional suggestions supplied by the model. Revision ordering does not
    resolve two current records: that is an unresolved governance conflict.
    """
    if not operation or not isinstance(required, frozenset):
        raise ValueError("operation policy required")
    policy = dict(required_authority or {})
    if not set(policy).issubset(required) or any(a not in {"confirmed", "verified", "derived", "candidate"} for a in policy.values()):
        raise ValueError("invalid required authority policy")
    seen = set()
    allowed: dict[str, list[ContextRecord]] = {}
    excluded = 0
    for r in records:
        key = (r.id, r.revision)
        if key in seen:
            raise ValueError("duplicate context revision")
        seen.add(key)
        if (access.principal not in r.readers or access.project not in r.projects or
            operation not in r.operations or r.status != "current"):
            excluded += 1
            continue
        allowed.setdefault(r.id, []).append(r)
    selected = []
    for identity, versions in allowed.items():
        if len(versions) != 1 or versions[0].authority == "unknown":
            # An optional ambiguous record can be omitted without guessing.
            excluded += len(versions)
            continue
        r = versions[0]
        selected.append(SelectedContext(r, r.kind == "decision" and r.authority == "confirmed"))
    selected.sort(key=lambda item: (item.record.id, item.record.revision))
    if not required.issubset({item.record.id for item in selected}):
        # Uniform error avoids an existence oracle for non-permitted records.
        raise InsufficientContext("required context unavailable or unresolved")
    if any(item.record.authority != policy[item.record.id] for item in selected if item.record.id in policy):
        raise InsufficientContext("required context unavailable or unresolved")
    return Resolution(operation, tuple(selected), excluded)

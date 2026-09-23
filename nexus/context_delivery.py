"""Reviewable, revision-pinned text delivery. No deployment or credentials.

The grant is trusted launcher configuration, never a model tool argument.
Creating this adapter is NOT authorization to activate a remote connection.
"""
from dataclasses import dataclass
import json

from mcp import types
from mcp.server import MCPServer
from nexus.artifacts import AccessDenied, InsufficientContext


@dataclass(frozen=True)
class DeliveryGrant:
    project: str
    contract_revision: int
    # Each tuple is ("context" or "personal", record identity, exact revision).
    records: tuple[tuple[str, str, int], ...] = ()

    def __post_init__(self):
        if not self.project or type(self.contract_revision) is not int or self.contract_revision < 1:
            raise ValueError('exact project and contract revision required')
        if not isinstance(self.records, tuple) or len(self.records) != len(set(self.records)):
            raise ValueError('unique record revisions required')
        for origin, identity, revision in self.records:
            if origin not in {'context', 'personal'} or not identity or type(revision) is not int or revision < 1:
                raise ValueError('invalid record grant')


def project_delivery(task, grant):
    if task.project != grant.project:
        raise InsufficientContext('approved context unavailable')
    view = task.read('implement')
    return select_delivery(view, grant)


def select_delivery(view, grant):
    """Project an already-resolved snapshot without a second database read."""
    contract = view['contract']
    if contract['revision'] != grant.contract_revision:
        raise InsufficientContext('approved context unavailable')
    records = []
    for origin, identity, revision in grant.records:
        available = view['context'] if origin == 'context' else view['personal']['items']
        matches = [i for i in available if i['id'] == identity and i['revision'] == revision]
        if len(matches) != 1:
            raise InsufficientContext('approved context unavailable')
        item = matches[0]
        records.append(dict(origin=origin, id=identity, revision=revision,
            text=item['body'] if origin == 'context' else item['text'],
            authority=item['authority'], binding=item['binding']))
    # Deliberately excludes owners, file paths, receipts, unselected context,
    # historical events, corrections, artifacts and full source documents.
    return dict(schema='nexus.selected-context.v1', project=grant.project,
        state=view['project']['state'], contract={k: contract[k] for k in
            ('revision', 'goal', 'acceptance', 'constraints', 'authority')}, records=records)


def build_context_server(task, grant):
    server = MCPServer('nexus-selected-project-context', version='0.1.0')

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    def read_project_context() -> types.CallToolResult:
        """Read the approved current project contract and selected scoped context.

        Use this when continuing this project's work. Only confirmed contract and
        binding decisions constrain work; personal context and candidates do not
        authorize actions. An error requires clarification, never stale substitutes.
        This tool cannot register decisions, confirm UAT, or fetch binary originals.
        """
        try:
            value = project_delivery(task, grant)
        except (AccessDenied, InsufficientContext):
            return types.CallToolResult(isError=True, content=[types.TextContent(text='{"status":"insufficient_context"}')])
        return types.CallToolResult(content=[types.TextContent(text=json.dumps(value, ensure_ascii=False))], structuredContent=value)

    return server

"""Scoped draft writes, no owner authority, arbitrary file or shell tools."""
import json
from nexus.intake import OVERVIEW_GUIDANCE
from mcp import types
from mcp.server import MCPServer


def build_intake_server(workspace):
    server=MCPServer('nexus-project-workspace',version='0.2.0')
    read=types.ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False)
    write=types.ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False)

    def result(action,unavailable='not_saved_or_unavailable'):
        try:
            data=action()
            if isinstance(data,dict) and 'project' in data and 'notes' not in data:
                view=workspace.read(data['project'],remote=True)
                data=dict(data,overview=view['overview'])
                if not view['overview'] or not view['overview']['current']:
                    data['display_overview_needed']=True
                    data['display_overview_guidance']=OVERVIEW_GUIDANCE
        except ValueError as e:
            return types.CallToolResult(isError=True,content=[types.TextContent(text=json.dumps({'status':unavailable,'reason':str(e)}))])
        return types.CallToolResult(content=[types.TextContent(text=json.dumps(data,ensure_ascii=False))],structuredContent=data)

    @server.tool(annotations=read)
    def find_projects(query: str='',offset: int=0,snapshot: str | None=None) -> types.CallToolResult:
        """Search permitted project titles. search.status distinguishes not_searched, matches and no_match; no_match is limited to the permitted title catalog, not local-only projects or project content. Continue with next_offset and snapshot. Errors mean search_unavailable, never no_match."""
        return result(lambda:workspace.list(query,remote=True,offset=offset,snapshot=snapshot),'search_unavailable')

    @server.tool(annotations=read)
    def read_project(project: str,offset: int=0,snapshot: str | None=None,operation: str='resume',mode: str='delegate') -> types.CallToolResult:
        """Read current goals, choices and notes. Continue all pages with returned snapshot and same operation/mode. Git reads include scoped context; required context.complete=false blocks dependent work. Independent/red-team excludes routed personal/learning influence, not explicit project requirements. Drafts are not owner approval. Report unavailable sources without stale fallback."""
        return result(lambda:workspace.read(project,remote=True,offset=offset,snapshot=snapshot,operation=operation,mode=mode),'read_unavailable')

    @server.tool(annotations=write)
    def create_project(title: str,source: str,request_id: str) -> types.CallToolResult:
        """Use when the user says to start/save a project. Creates local project folders and a durable draft visible through this connection. Search existing projects first. source identifies this conversation; request_id is a unique retry key reused for an uncertain attempt. Does not approve a Contract or start implementation."""
        return result(lambda:workspace.create(title,source,request_id,remote=True))

    @server.tool(annotations=write)
    def save_project_note(project: str,kind: str,body: str,source: str,evidence: str,quote: str,expected_revision: int,request_id: str,supersedes: str | None=None) -> types.CallToolResult:
        """Save useful project context when the user asks to save, starts a project, or has authorized ongoing capture. kinds: goal,acceptance,constraint,explicit_choice,preference,research,proposal,question,correction,source. evidence: user_statement (exact short quote required), model_inference, external_source. Inferred requirements must be proposal; never call an AI suggestion a user choice. source is conversation or source URL plus date, not invented. expected_revision comes from read/save; stale edits fail. Reuse request_id for retry. supersedes replaces an existing same-project note while retaining history. No full conversations, secrets or unrelated private information. Never claims owner-confirmed authority, Contract change, UAT or permission. Split long notes into useful short records."""
        return result(lambda:workspace.save(project,kind,body,source,evidence,quote,expected_revision,request_id,supersedes=supersedes,remote=True))

    return server

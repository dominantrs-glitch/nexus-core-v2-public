"""Foreground draft workspace. Activation requires the reviewed write/data scope."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from nexus.connector import relay_app
from nexus.intake import Intake, default_root
from nexus.git_intake import open_intake
from nexus.intake_server import build_intake_server
from nexus.launch_context import read_windows_token


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=default_root())
    p.add_argument('--activate',action='store_true')
    args=p.parse_args()
    workspace=open_intake(args.root)
    if not args.activate:
        print(json.dumps(dict(network_started=False,visible_projects=workspace.list(remote=True))))
        return
    from nexus.workspace_service import run_host
    run_host(args.root)


async def activate(workspace_root, on_state=None, *, on_diagnostic=None):
    workspace=open_intake(workspace_root)
    root=Path(__file__).resolve().parents[1]
    cfg=json.loads((root/'cloudflare/wrangler.relay.remote.jsonc').read_text(encoding='utf-8-sig'))['vars']
    if cfg['RELAY_ENABLED']!='true': raise ValueError('relay disabled')
    token=read_windows_token(root/'runtime/connector-token.dpapi')
    app=build_intake_server(workspace).streamable_http_app(json_response=True,stateless_http=True,max_request_body_size=16384)
    await relay_app(cfg['PUBLIC_ORIGIN'],token,cfg['PROJECT_ID'],app,on_state=on_state,
                    on_diagnostic=on_diagnostic)


if __name__=='__main__': main()

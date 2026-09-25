# Core and optional adapters

The default starting point is local storage and attributed project/context
management. No Desktop UI, AI provider, cloud account or background connector is
required for the local example. Learning, verification and automation policy
belong to Core; an external trigger, notification destination or model does not.

| Layer | Responsibility | Optional dependency |
| --- | --- | --- |
| Core | Records, exact originals, context selection, project state, corrections, learning evidence and verification gates | Python standard library; explicit local storage |
| Owner adapter | Authenticate the operator and obtain explicit confirmation for native Contract/Decision/UAT | Current reference implementation uses Windows DPAPI and a confirmation dialog |
| MCP adapter | Present authorized Core operations to an AI client | `requirements-mcp.txt`; any compatible client must respect the same scope |
| PC connector / surface | Relay requests to an explicitly running PC and display connection status | `requirements-pc-connector.txt`; optional Windows panel |
| Shared Git adapter | One private canonical store, optimistic concurrency, scoped context, originals, withdrawal and restoration | Optional `cloudflare/` implementation using GitHub and Cloudflare |
| Recovery adapter | Encrypted portable package, isolated restoration and activation review | `requirements-recovery.txt`; separate key custody |
| Daily surface | Selected work and calendar projections | Optional configured calendar, mail or desktop integrations; no operator-specific applications are bundled |

These are module boundaries within the source tree, not a claim that every
backend can be selected through a universal configuration switch. In particular,
the owner adapter currently has a Windows implementation, and the shared adapter
currently uses GitHub. Replacing either requires implementing and testing the
same identity, currentness, retry and confirmation contracts. A draft or a passing
machine check must never stand in for owner confirmation.

## Try Core without adapters

Use a new directory you choose, from the repository root:

```powershell
python -S -m examples.core_only --root ./my-core-example
```

`-S` disables site packages. The example stores only synthetic data, checks replay
without duplication and reopens the local store. It refuses an existing directory.
It starts no background process and makes no network request. It demonstrates
local intake persistence, not native acceptance or complete disaster recovery.

## Add only what is needed

Install one of the optional requirement profiles when adding that adapter. The
full development/reference environment remains in `requirements-lock.txt` and
`cloudflare/package-lock.json`. Production connection templates are disabled and
unconfigured; each operator supplies a private destination and credentials.

Current-use note removal is reversible and preserves original attribution and
Git history. It is not an erasure facility. Large explicitly shared PNG, JPEG,
WebP and PDF files are bounded at 64 MiB; configured transfer budgets still apply.
External backup/key custody and real restored-machine activation remain separate
operational tasks after the code and isolated restore checks pass.

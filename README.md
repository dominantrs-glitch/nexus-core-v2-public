# Nexus Core V2

Nexus Core V2 is a context and project foundation owned by its operator. It keeps
project records, exact originals, decisions, corrections and scoped learning
candidates separate, so an AI can resume work without treating its own suggestions
as the owner's approval.

This source distribution has no private project data, credentials, production
connections or previous private Git history. The source is available under the
[MIT License](LICENSE). Every operator supplies their own private data and reviewed
connections. This is a developer foundation, not a hosted service.

Start with [Core and optional adapters](docs/architecture.md). A local synthetic
example runs with the Python standard library alone:

```powershell
python -S -m examples.core_only --root ./my-core-example
```

The explicit directory must be new. This opens no UI or network connection and
does not touch the operator's existing data. Desktop, a particular AI client and
cloud infrastructure are optional reference adapters.

## Included

- Python project, contract, source/context and artifact storage; immutable versions,
  verification gates, explicit Windows owner confirmation, export and restore.
- Attributed project notes, intentions/actions, short daily briefs, reported work
  hours, scoped learning and local decision comparison.
- Git-backed shared notes, scoped rule delivery, reversible note withdrawal,
  project organization, source-checked learning, search and optimistic concurrency.
- Explicitly shared PNG/JPEG/WebP/PDF originals up to 64 MiB; encrypted portable
  backup, isolated restore and legacy-original catalog/extraction.
- Cloudflare authentication/control adapter and optional Windows PC connector.
- Synthetic tests and disabled connection templates. The older probe adapter is
  retained for tests; it is not the recommended production deployment.

## Run local checks

The current validation platform is Windows, Python 3.12 and Node.js 22 or later.
Windows DPAPI and native owner confirmation are required by the current local owner
adapter. Other platforms have not passed the complete suite.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests
cd cloudflare
npm ci
npm run typecheck
npm run test:git
npm run test:relay
npm run build:git-local
```

Release checks are recorded in [release validation](docs/release-validation.md).
Tests use synthetic data and local/mock backends.
Passing these checks is separate
from a real owner's acceptance, long-term operational evidence, or a successful
deployment in a different account.

## Start a local project

From the repository root, use `python -m nexus.intake --help` to create/read/save
attributed project notes. `python -m nexus.task --help` exposes the separate native
contract, output, verification and owner-acceptance workflow. A saved conversation
or a successful test cannot grant owner approval. Do not start a background
connector until its destination and data scope have been explicitly configured.

By default the local data root is `%LOCALAPPDATA%/NexusCoreV2`. A new installation
must use a fresh root; do not point an unreviewed candidate at an existing owner's
data. `nexus.work`, `nexus.personal`, `nexus.decision_review` and `nexus.efficiency`
provide their own `--help` entry points.

## Configure your own connections

All checked-in Worker switches are disabled, remote configuration is absent, and
resource IDs are placeholders. Supply your own reviewed GitHub/Cloudflare setup,
private data repository, credentials, access rules and required-context profiles.
Source code publication never means project data should be public. Secrets belong
in local ignored configuration or the provider's secret store.

`YOUR_GITHUB_ACCOUNT` is an explicit placeholder in the Git pilot template and the
optional legacy locator adapters (`nexus/source_documents.py`,
`nexus/information_access.py`, `cloudflare/src/source-documents.ts`). Configure it
before using legacy migration/reference lookup. Such links are historical
references, never proof of a fetched or current original. No former owner's
repository is configured. Missing required context must block dependent work.

Shared PNG/JPEG/WebP/PDF originals are limited to 64 MiB each and require explicit
project/operation scope. Listing metadata does not deliver original bytes. The distribution does not contain a universal uploader,
automatic memory collection, model routing, or permission inheritance.

## Provenance and review

`release-manifest.json` lists the exact release file sizes and SHA-256 hashes.
Only explicitly reviewed source files were copied. Account-specific locator text
was replaced with the configuration placeholder; private history and execution
reports were excluded. Dependencies are referenced by lockfiles, not vendored;
their upstream licenses still apply. Review your changes and exact file list before
publishing a derived version or connecting it to real project data.

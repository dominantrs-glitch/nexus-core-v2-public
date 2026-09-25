# Release validation — 2026-09-25

This revision adds current-use note withdrawal/restoration, bounded rule-update
validation, current context/rule/learning behavior, 64 MiB shared originals,
portable recovery, and PC connection diagnostics. The local Core example and
optional adapter profiles make the deployment choices explicit.

The exported candidate was tested in a new dependency environment: Python 239,
Git 238, authenticated Worker 25 and interface 25 tests passed, together with
TypeScript checking and the local Git-client build. A final pagination repair
retains withdrawal/restore metadata in exports; its affected Python client tests
were run again against the final source. Core's synthetic example also passed
with `python -S`, without site packages, a UI or provider configuration.

Source selection uses an explicit hashed allowlist and exact replacement counts.
Private account identifiers, actual project IDs, credentials, runtime data,
production configuration and prior private Git history are excluded. The release
manifest records the final exported bytes, including this documentation update.

Windows remains the complete reference-test platform. A successful synthetic
check does not prove another operator's credentials, cloud setup, native
acceptance, off-PC backup custody or long-term reliability.

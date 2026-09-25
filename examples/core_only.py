"""Run a synthetic local example without a UI, network, provider or pip packages.

From the repository root:
    python -S -m examples.core_only --root <new-empty-directory>
The explicit directory must not exist. This never opens the operator's data root.
"""
import argparse
import json
from pathlib import Path

from nexus.intake import Intake


def run(root):
    root = Path(root).absolute()
    # Exclusive creation prevents reuse of an existing operator store.
    root.mkdir(parents=False, exist_ok=False)
    store = Intake(root)
    project = store.create('Synthetic core-only example', 'synthetic example', 'example-create')['project']
    args = dict(project=project, kind='proposal', body='Synthetic idea; not an owner requirement.',
                source='examples/core_only.py', evidence='model_inference', quote='',
                expected_revision=0, request_id='example-save')
    first = store.save(**args)
    if store.save(**args) != first:
        raise RuntimeError('idempotent save failed')
    # Reopen from disk instead of proving only that in-memory state works.
    view = Intake(root).read(project)
    if view['revision'] != 1 or view['binding'] or len(view['notes']) != 1:
        raise RuntimeError('local persistence or attribution check failed')
    return dict(status='passed',project=project,revision=1,network_used=False,
                ui_used=False,third_party_packages_required=False,native_approval=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    print(json.dumps(run(parser.parse_args().root)))

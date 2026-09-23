"""Owner-local, precommitted predictions and later attributed choice reports.

Uses immutable Source revisions in the existing personal ContextStore, so its
normal local archive retains the complete history. Nothing is automatically
injected as a preference, Decision, permission, or generally accurate model.
"""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import sys

from nexus.context_store import ContextStore
from nexus.owner import OwnerCapability, local_owner_data_root
from nexus.personal import HOME


def _text(value, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('bounded nonempty text required')
    return value.strip()


class DecisionReview:
    def __init__(self, store, capability, owner):
        self.store, self.capability, self.owner = store, capability, owner

    def _events(self, identity):
        self.capability.authenticate(self.owner)
        if not isinstance(identity, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,60}', identity):
            raise ValueError('invalid prediction identity')
        source_id = 'decision-review-' + identity
        rows = [s for s in self.store.export(HOME, self.owner)['sources'] if s['id'] == source_id]
        return source_id, rows

    def _append(self, source_id, rows, payload, expected_revision):
        # Exact replay compares content before generating a new capture time.
        if rows and json.loads(rows[-1]['body'])['payload'] == payload:
            return {'id': source_id, 'revision': rows[-1]['revision'], 'binding': False}
        if type(expected_revision) is not int or len(rows) != expected_revision:
            raise ValueError('revision conflict')
        event = dict(schema='nexus.decision-review.v1', payload=payload,
                     captured_at=datetime.now(timezone.utc).isoformat())
        ref = self.store.register_source(source_id, HOME, self.owner,
            json.dumps(event, ensure_ascii=False, sort_keys=True), expected_revision=expected_revision)
        return {'id': source_id, 'revision': ref.revision, 'binding': False}

    def freeze(self, identity, *, project, question, options, predicted_choice,
               reason, priorities, reversal_conditions, provenance):
        source_id, rows = self._events(identity)
        project = _text(project, 100)
        if project == '*' or not isinstance(options, dict) or not 2 <= len(options) <= 8:
            raise ValueError('explicit project and two to eight options required')
        options = {_text(k, 80): _text(v, 500) for k, v in options.items()}
        if 'other' in options:
            raise ValueError('other is reserved for an unlisted actual choice')
        if predicted_choice not in options:
            raise ValueError('predicted choice must name an option')
        payload = dict(phase='prediction', project=project, question=_text(question), options=options,
            predicted_choice=predicted_choice, reason=_text(reason), priorities=_text(priorities),
            reversal_conditions=_text(reversal_conditions), provenance=_text(provenance),
            authority='model_inference', binding=False)
        # A prediction is never overwritten or revised after its first capture.
        if rows and (len(rows) != 1 or json.loads(rows[0]['body'])['payload'] != payload):
            raise ValueError('prediction is frozen; use a new identity for another prediction')
        return self._append(source_id, rows, payload, 0)

    def evaluate(self, identity, *, chosen_option, quote, provenance, comparison,
                 expected_revision=1):
        source_id, rows = self._events(identity)
        if not rows or json.loads(rows[0]['body'])['payload']['phase'] != 'prediction':
            raise ValueError('freeze a prediction before recording a choice')
        prediction = json.loads(rows[0]['body'])['payload']
        if json.loads(rows[-1]['body'])['payload']['phase'] == 'withdrawn':
            raise ValueError('prediction withdrawn')
        if chosen_option not in prediction['options'] and chosen_option != 'other':
            raise ValueError('choose a recorded option or other')
        payload = dict(phase='evaluation', project=prediction['project'], prediction_sha256=rows[0]['sha256'],
            chosen_option=chosen_option, quote=_text(quote), provenance=_text(provenance),
            comparison=_text(comparison), comparison_authority='model_inference',
            choice_match=chosen_option == prediction['predicted_choice'], binding=False,
            ordering='prediction_saved_before_answer_capture', actual_answer_timing='not_verified')
        return self._append(source_id, rows, payload, expected_revision)

    def withdraw(self, identity, *, reason, provenance, expected_revision):
        source_id, rows = self._events(identity)
        if not rows:
            raise ValueError('prediction unavailable')
        prediction = json.loads(rows[0]['body'])['payload']
        return self._append(source_id, rows, dict(phase='withdrawn', project=prediction['project'],
            reason=_text(reason), provenance=_text(provenance), binding=False), expected_revision)

    def read(self, project):
        self.capability.authenticate(self.owner)
        _text(project, 100)
        histories = {}
        for row in self.store.export(HOME, self.owner)['sources']:
            if row['id'].startswith('decision-review-'):
                histories.setdefault(row['id'], []).append(row)
        items = []
        for identity, rows in histories.items():
            first, last = json.loads(rows[0]['body']), json.loads(rows[-1]['body'])
            if first['payload']['project'] != project:
                continue
            items.append(dict(id=identity.removeprefix('decision-review-'), revision=rows[-1]['revision'],
                prediction=first['payload'], prediction_captured_at=first['captured_at'],
                prediction_sha256=rows[0]['sha256'], latest=last['payload'], captured_at=last['captured_at']))
        evaluated = [i for i in items if i['latest']['phase'] == 'evaluation']
        return dict(project=project, items=items, binding=False, evaluated=len(evaluated),
            matched=sum(i['latest']['choice_match'] for i in evaluated),
            instruction='Local reported choices only. Capture order does not prove when the owner actually answered. '
                'Matched choices do not establish correct reasons or general predictive quality. '
                'Review reasons, priorities and reversal conditions separately; no automatic preference updates.')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Local decision prediction/review; no external delivery')
    parser.add_argument('--database', type=Path, default=Path(os.environ.get('LOCALAPPDATA', '.')) /
                        'NexusCoreV2' / 'personal' / 'context.sqlite3')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('freeze', 'evaluate', 'withdraw'):
        command = sub.add_parser(name)
        command.add_argument('identity')
        command.add_argument('--file', type=Path, required=True, help='UTF-8 JSON arguments')
    sub.add_parser('read').add_argument('--project', required=True)
    args = parser.parse_args()
    owner = getpass.getuser()
    capability = OwnerCapability.open(local_owner_data_root() / 'owner.capability.json', owner)
    args.database.parent.mkdir(parents=True, exist_ok=True)
    review = DecisionReview(ContextStore(args.database), capability, owner)
    if args.command == 'read':
        result = review.read(args.project)
    else:
        result = getattr(review, args.command)(args.identity, **json.loads(args.file.read_text(encoding='utf-8-sig')))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

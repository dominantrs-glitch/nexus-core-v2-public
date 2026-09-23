"""Shared synthetic native fixture for Python-to-TypeScript transport tests."""
from pathlib import Path
import tempfile
import json
import sys

from nexus.core_migration import prepare_core, freeze_prepared_core
from nexus.git_core import GitCore, pack_bundle,digest
from nexus.artifacts import Access
from nexus.task import Task


class SyntheticSurface:
    def confirm(self, request): return True


def fixture(project='synthetic-native'):
    with tempfile.TemporaryDirectory(prefix='nexus-native-fixture-') as temporary:
        root = Path(temporary)
        task = Task(root / 'source', project, 'native-owner', owner_root=root / 'owner', surface=SyntheticSurface())
        task.start(dict(goal='架空データの正式な手順確認', acceptance={'file': ['machine', 'contract', 'uat']},
                        constraints=['架空データだけ'], source='synthetic fixture, never real UAT'))
        task.adapter.confirm_decision_from_body('synthetic-rule', task.project, task.owner,
            source_id='rule-source', source_body='fixture instruction', body='Keep the original bytes',
            projects=frozenset({task.project}), operations=frozenset({'implement', 'review'}), readers=frozenset({task.owner}))
        for operation in ('implement', 'review'):
            policy = task.contexts.policy(task.project, task.owner, 1, operation)
            task.contexts.set_operation_policy(task.project, task.owner, contract_revision=1, operation=operation,
                principal=task.owner, required_context=policy.required_context | {'synthetic-rule'},
                required_authority={**policy.required_authority, 'synthetic-rule': 'confirmed'}, expected_revision=policy.revision)
        output = root / 'result.txt'
        output.write_text('incorrect synthetic original',encoding='utf-8')
        task.publish_output(output)
        for kind in ('machine','contract'):task.verify('file',kind,'fail','synthetic fixture failure')
        task.correct('file','Keep the exact fictional original.','synthetic fixture correction')
        output.write_text('架空の原本\n', encoding='utf-8')
        task.publish_output(output)
        for kind in ('machine', 'contract', 'uat'):
            task.verify('file', kind, 'pass', 'synthetic fixture', review_file=output)
        task.finish()
        if project=='synthetic-target':task.classify_work(['data-export'],'synthetic classification')
        prepare_core(task, root / 'package')
        frozen = freeze_prepared_core(task, root / 'package')
        return dict(project=task.project, expected_revision=0, expected_document_sha256=None, expected_generation='test-1', request_id='native-import-'+project,
                    files=pack_bundle(root / 'package' / 'bundle', task.owner, task.project),
                    origin={key: frozen[key] for key in ('generation', 'source_digest', 'package_sha256')})


def advance(view, action='uat'):
    with tempfile.TemporaryDirectory(prefix='nexus-native-next-') as temporary:
        root = Path(temporary)
        requests = []
        def call(operation, args):
            if operation == 'core_read': return view
            requests.append(args)
            return dict(project=args['project'], revision=args['expected_revision'] + 1)
        store = GitCore(root / 'route', 'native-owner', 'test-1', owner_root=root / 'owner', surface=SyntheticSurface(), call=call,route_name='fixture-native')
        with store.open(view['document']['project']) as session:
            snapshot = session.task.snapshot()
            output = next(e for e in reversed(snapshot['events']) if e['kind'] == 'output')['payload']
            file = root / 'review.txt'
            file.write_bytes(session.task.artifacts.read(output['artifact_id'], output['revision'], Access('native-owner', session.task.project)).data)
            if action.startswith('share'):
                from nexus.git_learning import stage_shared_candidate
                scope={'share':'project','share-type':'task_type','share-general':'general'}[action]
                stage_shared_candidate(session,'synthetic-principle',principle='Preserve exact values in a roundtrip.',
                    rationale='Synthetic repaired-case comparison.',destinations=frozenset({'synthetic-target'}),
                    required_constraints=['架空データだけ'],reuse_scope=scope,
                    forbidden_constraints=['No synthetic reuse'],
                    applicable_work_types=['data-export'] if scope=='task_type' else [],
                    reuse_basis='Synthetic scoped reuse basis.' if scope!='project' else '')
            elif action=='withdraw':
                from nexus.git_learning import withdraw_shared_candidate
                withdraw_shared_candidate(session,'shared-lesson:synthetic-principle',1)
            elif action=='regress':session.task.verify('file','machine','fail','Synthetic later regression')
            elif action=='recheck':session.task.verify('file','machine','pass','Synthetic newer machine result')
            elif action in ('target-constraints','target-forbidden'):
                old=session.task.snapshot()['contracts'][-1]
                draft={k:old[k] for k in ('goal','acceptance','constraints','source')}
                draft['constraints']=['Different target requirement'] if action=='target-constraints' else old['constraints']+['No synthetic reuse']
                session.task.adapter.confirm_contract(session.task.project,session.task.owner,**draft,expected_revision=old['revision'])
                session.task.start(draft)
            elif action=='target-type':
                prior=next(e for e in reversed(snapshot['events']) if e['kind']=='work_classification')
                session.task.classify_work(['unrelated-work'],'synthetic changed classification',expected_event=prior['seq'])
            else:session.task.verify('file', 'uat', 'pass', 'second synthetic native confirmation', review_file=file)
            session.commit('native-fixture-'+action)
        return requests[0]


if __name__ == '__main__':
    if '--learning-bundle' in sys.argv:
        source=fixture()
        document=dict(schema=1,project=source['project'],owner='native-owner',revision=1,previous_sha256=None,origin=source['origin'],files=source['files'])
        view=dict(snapshot='synthetic-fixture',generation='test-1',write_state='active',document=document,document_sha256=digest(document))
        result=dict(source=source,target=fixture('synthetic-target'),publication=advance(view,'share'))
    elif '--advance' in sys.argv:
        result=advance(json.load(sys.stdin),sys.argv[sys.argv.index('--advance')+1] if len(sys.argv)>sys.argv.index('--advance')+1 else 'uat')
    else:result=fixture('synthetic-target' if '--target' in sys.argv else 'synthetic-native')
    print(json.dumps(result, ensure_ascii=True))

import {expect,it} from 'vitest';
import {FakeGitHub} from './git-fixture';
import {GitHubBackend} from '../src/github-store';
import {GitIntake} from '../src/git-store';
import {planDeletion,applyDeletion} from '../src/git-deletion';
import {auditFiles} from '../src/git-audit';

async function setup(){
  const git=await new FakeGitHub().init(),backend=new GitHubBackend(git.config,git.fetch),store=new GitIntake(backend,'owner','test-1');
  const create={title:'Delete synthetic project',source:'synthetic',request_id:'create'},p=await store.create(create);
  const note=await store.save({project:p.project,kind:'proposal',body:'synthetic payload',source:'synthetic',quote:'',
    evidence:'model_inference',expected_revision:0,request_id:'note'});
  const input={action:'archive',project:p.project,source:'synthetic owner',quote:'Archive this synthetic project',reason:'test'};
  const preview=await store.previewLifecycle(input);
  await store.applyLifecycle({...input,plan_digest:preview.plan_digest,request_id:'archive'});
  const args={project:p.project,scope:'current_shared_data',retained_history_acknowledged:true,quote:'Remove this synthetic project',source:'synthetic owner'};
  return {git,backend,store,p,note,create,args};
}
it('removes only the reviewed current project data and leaves an anti-replay marker and truthful history status',async()=>{
  const {git,backend,store,p,note,create,args}=await setup(),before=git.branch;
  const plan=await planDeletion(git.objects.get(before)!,before,args);
  expect(plan.blockers).toEqual([]);
  expect(plan.impact.removed_files).toBe(3);
  git.losePatchReply=true;
  const deleted=await applyDeletion(backend,plan);
  expect(deleted).toMatchObject({status:'removed_current_shared_data',history_erased:false});
  expect(await applyDeletion(backend,plan)).toEqual(deleted);
  expect(git.objects.get(git.branch)![`projects/${p.project}/records/${note.note}.json`]).toBeUndefined();
  expect(git.objects.get(before)![`projects/${p.project}/records/${note.note}.json`]).toBeDefined();
  await expect(store.read({project:p.project,snapshot:before})).rejects.toThrow('project_unavailable');
  await expect(store.create(create)).rejects.toThrow('project_unavailable');
  expect((await auditFiles(git.objects.get(git.branch)!,git.branch)).status).toBe('passed');
});
it('rejects changed snapshots and linked dependencies before deletion',async()=>{
  const {git,backend,store,p,args}=await setup(),before=git.branch;
  const plan=await planDeletion(git.objects.get(before)!,before,args);
  await store.create({title:'Concurrent unrelated work',source:'synthetic',request_id:'other'});
  await expect(applyDeletion(backend,plan)).rejects.toThrow('deletion_snapshot_changed');
  const files=git.objects.get(git.branch)!;
  (files['lifecycle.json'] as any).relations=[{project:p.project,target:(files['nexus.json'] as any).projects[1].id,
    relation:'related',source:'synthetic',quote:'link',reason:'test'}];
  const linked=await planDeletion(files,git.branch,args);
  expect(linked.blockers).toContain('organization_relations');
  files[`projects/${p.project}/binary/manifest.json`]={current:[{remote:true,projects:[p.project,'other-project']}]};
  expect((await planDeletion(files,git.branch,args)).blockers).toContain('original_used_by_other_projects');
  await expect(applyDeletion(backend,linked)).rejects.toThrow('deletion_dependencies_unresolved');
});

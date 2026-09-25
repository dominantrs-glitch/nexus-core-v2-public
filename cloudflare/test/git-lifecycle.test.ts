import {it,expect} from 'vitest';
import {GitIntake} from '../src/git-store';
import {GitHubBackend} from '../src/github-store';
import {FakeGitHub} from './git-fixture';
const day={date:'2026-09-24',now:'2026-09-24T12:00:00+09:00',utc_offset:'+09:00'};
async function setup(){
  const git=await new FakeGitHub().init(),store=new GitIntake(new GitHubBackend(git.config,git.fetch),'owner','test-1');
  const a=await store.create({title:'Alpha',source:'test',request_id:'alpha'}),b=await store.create({title:'Beta',source:'test',request_id:'beta'});
  const plan=(action:string,extra:object={})=>({action,project:a.project,quote:'Organize these synthetic projects.',source:'synthetic owner',reason:'Same objective and retained source history',...extra});
  const apply=async(input:any,key=input.action)=>store.applyLifecycle({...input,plan_digest:(await store.previewLifecycle(input)).plan_digest,request_id:key});
  return {git,store,a,b,plan,apply};
}
it('explicit archive hides current lists and work, retains search/history, and restore returns it',async()=>{
  const {store,a,plan,apply}=await setup();
  await store.saveWork({project:a.project,item:{id:'work',type:'action',title:'Keep history',status:'open'},
    expected_revision:0,evidence:'user_statement',quote:'Do it',source:'test',request_id:'work'});
  expect((await store.list()).projects).toHaveLength(2);
  await apply(plan('archive'));
  expect((await store.list()).projects).toHaveLength(1);
  expect((await store.list({include_archived:true})).projects).toHaveLength(2);
  expect((await store.work(day)).items).toHaveLength(0);
  expect((await store.search({query:'Keep'})).matches).toHaveLength(1);
  expect(await store.read({project:a.project,detail:'overview'})).toMatchObject({lifecycle:{status:'archived'}});
  await expect(store.save({project:a.project,kind:'proposal',body:'test',source:'test',evidence:'model_inference',quote:'',expected_revision:1,request_id:'bad'})).rejects.toThrow('archived');
  await apply(plan('restore'));
  expect((await store.work(day)).items).toHaveLength(1);
});
it('merge keeps exact source IDs, linked work and history under a single active destination',async()=>{
  const {store,git,a,b,plan,apply}=await setup();
  const work={project:a.project,item:{id:'work',type:'action',title:'Move next',status:'open'},expected_revision:0,
    evidence:'user_statement',quote:'Do it',source:'test',request_id:'work'};
  const saved=await store.saveWork(work);
  const request=plan('merge',{target:b.project}),preview=await store.previewLifecycle(request);
  git.losePatchReply=true;
  const result=await store.applyLifecycle({...request,plan_digest:preview.plan_digest,request_id:'merge'});
  expect(await store.applyLifecycle({...request,plan_digest:preview.plan_digest,request_id:'merge'})).toEqual(result);
  expect((await store.list()).projects.map(p=>p.id)).toEqual([b.project]);
  expect(await store.list({query:'Alpha'})).toMatchObject({projects:[{id:b.project,matched_aliases:[{project:a.project,title:'Alpha'}]}]});
  expect(await store.search({query:'Move',project:b.project})).toMatchObject({matches:[{project:a.project}]});
  expect(await store.read({project:a.project})).toMatchObject({lifecycle:{canonical_project:b.project},notes:[{id:saved.note}]});
  expect(await store.read({project:b.project})).toMatchObject({lifecycle:{related_history:[{project:a.project}]}});
  expect(await store.work({...day,project:b.project})).toMatchObject({items:[{project:a.project,canonical_project:b.project}]});
  await store.saveWork({...work,expected_revision:1,supersedes:saved.note,request_id:'done',item:{...work.item,status:'done'}});
  expect(await store.work({...day,project:b.project,include_closed:true})).toMatchObject({items:[{
    learning_evaluation:{status:'review_required',binding:false}}]});
  await expect(store.save({project:a.project,kind:'proposal',body:'new',source:'test',evidence:'model_inference',quote:'',expected_revision:2,request_id:'new'})).rejects.toThrow('use_canonical');
  await apply(plan('rollback',{event_revision:1}));
  expect((await store.list()).projects).toHaveLength(2);
  expect((await store.read({project:a.project})).revision).toBe(2);
});
it('preview detects stale impacts and relation edits prevent parent cycles',async()=>{
  const {store,a,b,plan,apply}=await setup();
  const p=plan('archive'),preview=await store.previewLifecycle(p);
  await store.save({project:a.project,kind:'proposal',body:'new',source:'test',evidence:'model_inference',quote:'',expected_revision:0,request_id:'new'});
  await expect(store.applyLifecycle({...p,plan_digest:preview.plan_digest,request_id:'stale'})).rejects.toThrow('preview_again');
  await apply(plan('relate',{target:b.project,relation:'parent'}));
  await expect(store.previewLifecycle(plan('relate',{project:b.project,target:a.project,relation:'parent'}))).rejects.toThrow('integrity');
  expect(await store.lifecycle({detail:'integrity'})).toMatchObject({issues:[]});
  await apply(plan('unrelate',{target:b.project,relation:'parent'}));
  await expect(store.previewLifecycle(plan('rollback',{event_revision:1}))).rejects.toThrow('latest_matching');
});
it('replays do not disclose a target revoked after merge and deletion review never removes data',async()=>{
  const {store,git,a,b,plan}=await setup();
  const input=plan('merge',{target:b.project}),request={...input,plan_digest:(await store.previewLifecycle(input)).plan_digest,request_id:'merge'};
  await store.applyLifecycle(request);
  const head=git.objects.get(git.branch)! as any;head['nexus.json'].projects.find((p:any)=>p.id===b.project).remote=false;
  const replay=await store.applyLifecycle(request);
  expect(JSON.stringify(replay)).not.toContain(b.project);
  expect(await store.lifecycle({project:a.project,detail:'deletion_review'})).toMatchObject({full_deletion_available:false,confirmation_required:true});
  await expect(store.create({title:'Ａｌｐｈａ',source:'test',request_id:'duplicate'})).rejects.toThrow('duplicate_project');
});
it('project creation review retains exact evidence and refuses reused objectives or stale sources',async()=>{
  const {store,a}=await setup();
  const note=await store.save({project:a.project,kind:'goal',body:'Read shared records',source:'synthetic',evidence:'user_statement',
    quote:'Read shared records',expected_revision:0,request_id:'goal'});
  const input={title:'New surface',purpose:'Read shared records on another surface',responsibility:'Display only',canonical:'Existing Alpha records',
    assets:[],dependencies:['Alpha'],search_terms:['shared records'],candidates:[{project:a.project,
      basis:[{project:a.project,note:note.note,revision:1}],suggestion:'surface_of',reason:'Same records, different display responsibility'}]};
  const preview=await store.reviewStart(input);
  expect(preview.candidates[0]).toMatchObject({binding:false,basis:[{quote:'Read shared records'}]});
  const reuse={...input,candidates:[{...input.candidates[0],suggestion:'reuse'}]};
  await expect(store.create({title:input.title,source:'test',request_id:'reuse',review:{input:reuse,digest:(await store.reviewStart(reuse)).digest}})).rejects.toThrow('reuse_existing');
  const created=await store.create({title:input.title,source:'test',request_id:'new-surface',review:{input,digest:preview.digest}});
  expect(created.status).toBe('saved-draft');
  const old=await store.reviewStart({...input,title:'Another'});
  await store.save({project:a.project,kind:'goal',body:'Changed',source:'synthetic',evidence:'user_statement',quote:'Changed',
    expected_revision:1,supersedes:note.note,request_id:'correction'});
  await expect(store.create({title:'Another',source:'test',request_id:'stale',review:{input:old.input,digest:old.digest}})).rejects.toThrow('basis_unavailable');
});
it('merges expose retained relationships at their canonical endpoints and reject collapsed graphs',async()=>{
  const {store,a,b,plan,apply}=await setup();
  const c=await store.create({title:'Gamma',source:'test',request_id:'gamma'});
  await apply(plan('relate',{target:c.project,relation:'parent'}));
  await apply(plan('merge',{target:b.project}));
  expect(await store.lifecycle({project:b.project})).toMatchObject({projects:[{relations:[{
    project:a.project,target:c.project,canonical_project:b.project,canonical_target:c.project}]}]});
  await expect(store.previewLifecycle(plan('merge',{project:b.project,target:c.project}))).rejects.toThrow('integrity');
  await expect(store.previewLifecycle(plan('relate',{project:b.project,target:c.project,relation:'parent'}))).rejects.toThrow('integrity');
  await expect(store.previewLifecycle(plan('relate',{project:c.project,target:b.project,relation:'parent'}))).rejects.toThrow('integrity');
  await apply(plan('unrelate',{target:c.project,relation:'parent'}),'unlink-old-source');
  expect(await store.previewLifecycle(plan('merge',{project:b.project,target:c.project}))).toHaveProperty('plan_digest');
});
it('capability status distinguishes implemented project review from enabled creation',async()=>{
  const {store,git}=await setup();
  expect(await store.capabilities()).toMatchObject({features:{projects:{new_project_review:true,creation_enabled:true}}});
  const backend=new GitHubBackend(git.config,git.fetch);
  const catalog=await backend.read(git.branch,'nexus.json') as any;
  catalog.allow_create=false;
  await backend.commit(git.branch,{'nexus.json':catalog});
  expect(await store.capabilities()).toMatchObject({features:{projects:{new_project_review:true,creation_enabled:false,full_delete:false}}});
});

import {it,expect} from 'vitest';
import {GitIntake} from '../src/git-store';
import {GitHubBackend} from '../src/github-store';
import {gitMcp} from '../src/git-mcp';
import {FakeGitHub} from './git-fixture';
import {removalHeader} from '../src/git-note-removal';
import {workHeader} from '../src/git-work';

async function fixture(evidence:'user_statement'|'model_inference'|'external_source'='model_inference'){
  const fake=await new FakeGitHub().init(),store=new GitIntake(new GitHubBackend(fake.config,fake.fetch),'owner','test-1');
  const p=await store.create({title:'Synthetic removal',source:'fixture',request_id:'create'});
  const original={project:p.project,kind:evidence==='external_source'?'source':'proposal',body:'unique mistaken content',
    source:'synthetic original',evidence,quote:evidence==='user_statement'?'synthetic owner words':'',expected_revision:0,request_id:'original'};
  const saved=await store.save(original);
  const input={action:'remove' as const,project:p.project,note:saved.note!,expected_revision:1,
    quote:'Remove this mistaken record',source:'synthetic owner removal request',reason:'Wrong case'};
  return {fake,store,original,saved,input,files:()=>fake.objects.get(fake.branch)! as Record<string,any>};
}
it.each(['user_statement','model_inference','external_source'] as const)('withdraws and restores exact %s attribution, retaining history and lost-response replay',async evidence=>{
  const f=await fixture(evidence),before=await f.store.read({project:f.input.project});
  const plan=await f.store.previewNoteRemoval(f.input);
  expect(plan.can_apply).toBe(true);expect(plan.impact.original.body).toBe(f.original.body);
  const args={...f.input,plan_digest:plan.plan_digest,request_id:'remove'};
  f.fake.losePatchReply=true;
  const removed=await f.store.applyNoteRemoval(args);
  expect(await f.store.applyNoteRemoval(args)).toEqual(removed);
  const after=await f.store.read({project:f.input.project});
  expect(after.notes).toEqual([]);
  expect('withdrawn_notes' in after&&after.withdrawn_notes).toMatchObject([{note:removed.note,original_note:f.saved.note,status:'withdrawn'}]);
  expect((await f.store.search({project:f.input.project,query:'unique mistaken'})).matches).toEqual([]);
  const changes=await f.store.read({project:f.input.project,detail:'changes',known_snapshot:before.snapshot,since_revision:1});
  expect(changes.removed_note_ids).toEqual([f.saved.note]);expect(changes.notes).toEqual([]);
  expect(f.files()[`projects/${f.input.project}/records/${f.saved.note}.json`].body).toBe(f.original.body);
  const restore={...f.input,action:'restore' as const,note:removed.note!,expected_revision:2,quote:'Restore the mistaken withdrawal'};
  const preview=await f.store.previewNoteRemoval(restore);
  const restoreArgs={...restore,plan_digest:preview.plan_digest,request_id:'restore'};
  const restored=await f.store.applyNoteRemoval(restoreArgs);
  expect(await f.store.applyNoteRemoval(restoreArgs)).toEqual(restored);
  const read=await f.store.read({project:f.input.project});
  expect(read.notes).toHaveLength(1);expect('withdrawn_notes' in read&&read.withdrawn_notes).toEqual([]);
  expect(read.notes[0]).toMatchObject({body:f.original.body,evidence:f.original.evidence,source:f.original.source,quote:f.original.quote});
  expect(f.files()[`projects/${f.input.project}/removals/3.json`]).toMatchObject({action:'restore',quote:restore.quote,original_note:f.saved.note});
  expect((await f.store.search({project:f.input.project,query:'unique mistaken'})).matches).toHaveLength(1);
});
it('rejects changes to input, stale project, reused request, and revoked access on replay',async()=>{
  const f=await fixture(),plan=await f.store.previewNoteRemoval(f.input),args={...f.input,plan_digest:plan.plan_digest,request_id:'remove'};
  await expect(f.store.applyNoteRemoval({...args,reason:'different'})).rejects.toThrow('preview_changed');
  await expect(f.store.applyNoteRemoval({...args,expected_revision:0})).rejects.toThrow('revision_conflict');
  await f.store.applyNoteRemoval(args);
  await expect(f.store.applyNoteRemoval({...args,quote:'different'})).rejects.toThrow('request_key_reused');
  f.files()['nexus.json'].projects[0].remote=false;
  await expect(f.store.applyNoteRemoval(args)).rejects.toThrow('project_unavailable');
});
it('blocks a current mandatory context source and reports the affected rule',async()=>{
  const f=await fixture(),files=f.files();
  files['nexus.json'].context_revision=1;
  files['context.json']={schema:1,mode:'synthetic',owner:'owner',generation:'test-1',revision:1,
    profiles:[{project:'*',operations:['resume'],required:{global_rules:['required'],personal:[],learning:[],relations:[]}}],
    entries:[{id:'required',category:'global_rules',projects:['*'],operations:['resume'],status:'active',source:{project:f.input.project,note:f.saved.note,revision:1}}]};
  const plan=await f.store.previewNoteRemoval(f.input);
  expect(plan).toMatchObject({can_apply:false,impact:{blockers:[{type:'active_context_reference',rule:'required'}]}});
  await expect(f.store.applyNoteRemoval({...f.input,plan_digest:plan.plan_digest,request_id:'remove'})).rejects.toThrow('has_dependents');
  expect(files[`projects/${f.input.project}/manifest.json`].revision).toBe(1);
});
it('detects a new learning reference after preview without damaging either record',async()=>{
  const f=await fixture('external_source'),plan=await f.store.previewNoteRemoval(f.input);
  const other=await f.store.create({title:'Other',source:'fixture',request_id:'other'});
  // Cross-project imported references must also be caught, not only own indexes.
  const files=f.files(),p=files[`projects/${other.project}/manifest.json`];
  p.revision=1;p.current=['n-work'];p.work_index=[{id:'job',note:'n-work',key:'job',kind:'work',status:'done',date:null,created:new Date().toISOString()}];
  files['nexus.json'].projects.find((p:any)=>p.id===other.project).revision=1;
  files[`projects/${other.project}/records/n-work.json`]={id:'n-work',project:other.project,revision:1,kind:'proposal',captured_kind:'proposal',
    evidence:'user_statement',quote:'do the job',source:'fixture',supersedes:null,created:new Date().toISOString(),body:workHeader+JSON.stringify({schema:1,id:'job',type:'action',title:'job',status:'done',learning:{corrections:[],checks:[{project:f.input.project,note:f.saved.note,revision:1}],conclusion:'checked'}})};
  await expect(f.store.applyNoteRemoval({...f.input,plan_digest:plan.plan_digest,request_id:'remove'})).rejects.toThrow('preview_changed');
  expect((await f.store.previewNoteRemoval(f.input)).impact.blockers).toMatchObject([{type:'learning_source',project:other.project}]);
});
it.each(['frozen','archived','merged'] as const)('refuses a %s target',async state=>{
  const f=await fixture(),files=f.files();
  if(state==='frozen')files[`projects/${f.input.project}/manifest.json`].write_state='frozen';
  else files['lifecycle.json']={schema:1,owner:'owner',generation:'test-1',revision:1,relations:[],records:{[f.input.project]:
    {status:state,canonical:state==='merged'?'other':null,quote:'owner',source:'test',reason:'test'}}};
  await expect(f.store.previewNoteRemoval(f.input)).rejects.toThrow();
});
it('does not let ordinary save forge a tombstone or replace it without restoration review',async()=>{
  const f=await fixture();
  await expect(f.store.save({...f.original,body:removalHeader+'{}',request_id:'forge',expected_revision:1})).rejects.toThrow('requires_preview');
  const plan=await f.store.previewNoteRemoval(f.input),removed=await f.store.applyNoteRemoval({...f.input,plan_digest:plan.plan_digest,request_id:'remove'});
  await expect(f.store.save({...f.original,evidence:'user_statement',quote:'replace',kind:'correction',request_id:'bypass',expected_revision:2,supersedes:removed.note})).rejects.toThrow('requires_preview');
  await expect(f.store.previewNoteRemoval({...f.input,note:removed.note!,expected_revision:2})).rejects.toThrow('state_mismatch');
});
it('reserves structured task cancellation for its existing path',async()=>{
  const f=await fixture();
  const n=f.files()[`projects/${f.input.project}/records/${f.saved.note}.json`];
  n.body=workHeader+JSON.stringify({schema:1,id:'work',type:'action',title:'job',status:'open'});
  await expect(f.store.previewNoteRemoval(f.input)).rejects.toThrow('structured_record_use_existing');
});
it('makes the review/read scope visible through the actual MCP router',async()=>{
  const f=await fixture();
  const result=await (await gitMcp({jsonrpc:'2.0',id:1,method:'tools/list'},f.store)).json() as any;
  expect(result.result.tools.find((x:any)=>x.name==='preview_note_removal').annotations.readOnlyHint).toBe(true);
  const response=await (await gitMcp({jsonrpc:'2.0',id:2,method:'tools/call',params:{name:'preview_note_removal',arguments:f.input}},f.store)).json() as any;
  expect(response.result.structuredContent).toMatchObject({can_apply:true,impact:{fully_erased:false}});
});

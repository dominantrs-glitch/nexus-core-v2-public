import {expect,it} from 'vitest';
import {GitIntake} from '../src/git-store';
import {GitHubBackend} from '../src/github-store';
import {FakeGitHub} from './git-fixture';

async function fixture() {
  const git=await new FakeGitHub().init(),store=new GitIntake(new GitHubBackend(git.config,git.fetch),'owner','test-1');
  const source=await store.create({title:'Desktop',source:'synthetic',request_id:'source'});
  const target=await store.create({title:'Brand',source:'synthetic',request_id:'target'});
  const note=async(project:string,request_id:string)=>store.save({project,kind:'goal',body:'shared visual responsibilities',
    source:'synthetic',evidence:'model_inference',quote:'',expected_revision:0,request_id});
  const a=await note(source.project,'a'),b=await note(target.project,'b');
  const args={project:source.project,target_project:target.project,target_revision:1,expected_revision:1,
    relation:'shares_context_with',status:'candidate',reason:'Visual design needs the shared brand rules',
    basis:[{project:source.project,note:a.note!,revision:1},{project:target.project,note:b.note!,revision:1}],
    request_id:'relation',source:'synthetic judgment based on both current goals'};
  return {git,store,args,a,b};
}

it('records relations as reversible proposals, with replay and without context inheritance',async()=>{
  const {store,args}=await fixture();
  const saved=await store.saveRelation(args);
  expect(await store.saveRelation(args)).toEqual(saved);
  const view=await store.read({project:args.project,detail:'relations'});
  expect(view.context.complete).toBe(false);
  expect(view).toMatchObject({read_scope:'relation_candidates',notes_omitted:true,candidates:[{
    note:saved.note,status:'candidate',basis_current:true,binding:false,source_changed:false,target_changed:false}]});
  const full=await store.read({project:args.project});
  expect(full.notes.find(n=>n.id===saved.note)).toMatchObject({kind:'proposal',evidence:'model_inference',quote:''});
  expect(full.context.complete).toBe(false);
});

it('invalidates replaced grounds and supports re-evaluation while preserving the previous note',async()=>{
  const {store,args,b,git}=await fixture();
  const saved=await store.saveRelation(args);
  const newer=await store.save({project:args.target_project,kind:'correction',body:'new purpose',source:'synthetic',
    evidence:'model_inference',quote:'',expected_revision:1,request_id:'change',supersedes:b.note});
  expect(await store.read({project:args.project,detail:'relations'})).toMatchObject({candidates:[{
    status:'needs_review',basis_current:false,target_changed:true}]});
  expect(await store.saveRelation(args)).toEqual(saved);
  const revised=await store.saveRelation({...args,expected_revision:2,target_revision:2,request_id:'reevaluate',
    supersedes:saved.note,basis:[args.basis[0],{project:args.target_project,note:newer.note,revision:2}]});
  expect(await store.read({project:args.project,detail:'relations'})).toMatchObject({candidates:[{note:revised.note,status:'candidate'}]});
  expect(git.objects.get(git.branch)![`projects/${args.project}/records/${saved.note}.json`]).toBeDefined();
});

it('withholds denied target details but still allows withdrawing a previous suggestion',async()=>{
  const {store,args,git}=await fixture();
  const saved=await store.saveRelation(args);
  const files=git.objects.get(git.branch)!;
  (files['nexus.json'] as any).projects.find((p:any)=>p.id===args.target_project).remote=false;
  const denied=await store.read({project:args.project,detail:'relations'});
  expect(denied).toMatchObject({candidates:[{status:'unavailable'}]});
  expect(JSON.stringify(denied)).not.toContain(args.target_project);
  expect(JSON.stringify(denied)).not.toContain(args.reason);
  const withdrawn=await store.saveRelation({...args,expected_revision:2,status:'withdrawn',supersedes:saved.note,request_id:'withdraw'});
  expect(await store.read({project:args.project,detail:'relations'})).toMatchObject({candidates:[{note:withdrawn.note,status:'withdrawn'}]});
});

it('rejects ungrounded relationships and high-impact structure changes',async()=>{
  const {store,args}=await fixture();
  await expect(store.saveRelation({...args,relation:'primary_parent'})).rejects.toThrow();
  await expect(store.saveRelation({...args,authority:'confirmed'})).rejects.toThrow();
  await expect(store.saveRelation({...args,target_project:args.project})).rejects.toThrow('nonbinding');
  await expect(store.saveRelation({...args,basis:[args.basis[0],args.basis[0]]})).rejects.toThrow('both_project_sources');
  await expect(store.saveRelation({...args,target_revision:0})).rejects.toThrow('target_changed');
  await expect(store.saveRelation({...args,basis:[args.basis[0],{...args.basis[1],revision:2}]})).rejects.toThrow('basis_unavailable');
  await expect(store.saveRelation({...args,status:'withdrawn'})).rejects.toThrow('withdrawal_needs_current');
});

it('keeps relation metadata inside atomic saves and ordinary correction/delta history',async()=>{
  const {store,args}=await fixture();
  const baseline=await store.read({project:args.project});
  const saved=await store.saveRelation(args);
  const fixed=await store.save({project:args.project,kind:'correction',body:'This relation was an unsupported idea',source:'synthetic',
    evidence:'model_inference',quote:'',expected_revision:2,request_id:'ordinary-correction',supersedes:saved.note});
  expect(await store.read({project:args.project,detail:'relations'})).toMatchObject({candidates:[]});
  const delta=await store.read({project:args.project,detail:'changes',since_revision:1,known_snapshot:baseline.snapshot});
  expect(delta).toMatchObject({status:'changes',notes:[{id:fixed.note}],removed_note_ids:[]});
});

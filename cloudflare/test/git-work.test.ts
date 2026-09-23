import {expect,it} from 'vitest';
import {GitIntake} from '../src/git-store';
import {GitHubBackend} from '../src/github-store';
import {FakeGitHub} from './git-fixture';
import {gitMcp} from '../src/git-mcp';

const day={date:'2026-09-23',now:'2026-09-23T12:00:00+09:00',utc_offset:'+09:00'};
async function setup(){
  const git=await new FakeGitHub().init(),store=new GitIntake(new GitHubBackend(git.config,git.fetch),'owner','test-1');
  const p=await store.create({title:'Synthetic work',source:'synthetic',request_id:'create'});
  const args={project:p.project,expected_revision:0,request_id:'work-1',source:'synthetic:owner',
    evidence:'user_statement',quote:'Please do this first.',item:{id:'one',type:'action',title:'First action',
      status:'open',user_priority:'high',priority_quote:'first'}};
  return {git,store,args};
}

it('saves one attributed item atomically, handles a lost reply and preserves correction history',async()=>{
  const {git,store,args}=await setup();git.losePatchReply=true;
  const saved=await store.saveWork(args);
  expect(await store.saveWork(args)).toEqual(saved);
  const first=await store.work({...day,project:args.project});
  expect(first).toMatchObject({complete:true,items:[{id:'one',evidence:'user_statement',user_priority:'high',binding:false}]});
  const changed=await store.saveWork({...args,expected_revision:1,request_id:'complete',supersedes:saved.note,
    item:{...args.item,status:'done'}});
  expect(await store.work({...day,project:args.project})).toMatchObject({complete:true,items:[]});
  expect(await store.work({...day,project:args.project,include_closed:true})).toMatchObject({items:[{
    note:changed.note,status:'done',created_at:first.items[0].created_at,completed_at:expect.any(String)}]});
  const completed=(await store.work({...day,project:args.project,include_closed:true})).items[0].completed_at;
  await store.saveWork({...args,expected_revision:2,request_id:'description-only',supersedes:changed.note,
    item:{...args.item,status:'done',next_step:'No next step; description corrected'}});
  expect((await store.work({...day,project:args.project,include_closed:true})).items[0].completed_at).toBe(completed);
  expect(git.objects.get(git.branch)![`projects/${args.project}/records/${saved.note}.json`]).toBeDefined();
  await expect(store.saveWork({...args,request_id:'stale'})).rejects.toThrow('revision_conflict');
});

it('rejects duplicates, wrong item replacement and guessed priorities or commitments',async()=>{
  const {store,args}=await setup(),saved=await store.saveWork(args);
  await expect(store.saveWork({...args,request_id:'duplicate',expected_revision:1,
    item:{...args.item,id:'two',title:'Ｆｉｒｓｔ   ACTION'}})).rejects.toThrow('duplicate_work_item');
  await expect(store.saveWork({...args,request_id:'wrong-item',expected_revision:1,supersedes:saved.note,
    item:{...args.item,id:'two',title:'Second'}})).rejects.toThrow('work_update_requires_current_item');
  await expect(store.saveWork({...args,request_id:'priority',expected_revision:1,supersedes:saved.note,
    item:{...args.item,priority_quote:'urgent'}})).rejects.toThrow('owner_priority_requires');
  await expect(store.saveWork({...args,request_id:'inferred',expected_revision:1,evidence:'model_inference',quote:'',
    item:{id:'two',type:'intent',title:'An interesting idea',status:'open'}})).rejects.toThrow('inferred_intent');
  await store.saveWork({...args,request_id:'candidate',expected_revision:1,evidence:'model_inference',quote:'',
    item:{id:'two',type:'intent',title:'An interesting idea',status:'candidate',ai_suggestion:'Consider later'}});
  expect(await store.work({...day})).toMatchObject({items:[{status:'open'},{status:'candidate',user_priority:null}]});
});

it('exposes every overdue, waiting, blocker and approval alert, including beyond three actions',async()=>{
  const {store,args}=await setup();
  for(let n=0;n<5;n++)await store.saveWork({...args,expected_revision:n,request_id:'action-'+n,
    item:{...args.item,id:'action-'+n,title:'Action '+n,due_at:'2026-09-22',
      blocker:n===3?'Access needed':'',approval_wait:n===4,status:n===2?'waiting':'open',waiting_for:n===2?'Owner':''}});
  const view=await store.work({...day});
  expect(view.alerts).toHaveLength(5);
  expect(view.alerts.every(a=>a.overdue)).toBe(true);
  await store.saveWork({...args,expected_revision:5,request_id:'today',item:{...args.item,id:'today',title:'Today',due_at:day.date}});
  expect((await store.work({...day})).items.at(-1).overdue).toBe(false);
});

it('requires reported dated hours, keeps conflicting reports visible and supports withdrawal',async()=>{
  const {store,args}=await setup();
  const hours={schema:1,id:'shift',date:day.date,start:'2026-09-23T22:00:00+09:00',end:'2026-09-24T06:00:00+09:00',status:'current'};
  const {item,...base}=args;
  await expect(store.saveHours({...base,evidence:'model_inference',quote:'',hours})).rejects.toThrow('work_hours_require');
  const saved=await store.saveHours({...base,hours});
  await store.saveHours({...base,request_id:'conflict',expected_revision:1,hours:{...hours,id:'shift-other',end:'2026-09-24T05:00:00+09:00'}});
  expect((await store.work({...day})).hours).toHaveLength(2);
  await store.saveHours({...base,request_id:'withdraw',expected_revision:2,supersedes:saved.note,hours:{...hours,status:'withdrawn'}});
  expect((await store.work({...day})).hours).toHaveLength(1);
  expect((await store.work({...day,date:'2026-09-24',now:'2026-09-24T12:00:00+09:00'})).hours).toEqual([]);
});

it('binds paged views to one snapshot and filter and excludes inaccessible projects',async()=>{
  const {git,store,args}=await setup();
  for(let n=0;n<21;n++)await store.saveWork({...args,expected_revision:n,request_id:'row-'+n,
    item:{...args.item,id:'row-'+n,title:'Row '+n}});
  const first=await store.work({...day});
  expect(first).toMatchObject({complete:false,exhausted:false,items:expect.any(Array)});
  expect(first.items).toHaveLength(20);
  const second=await store.work({...day,cursor:first.next_cursor,snapshot:first.snapshot});
  expect(second).toMatchObject({complete:false,exhausted:true,items:[{id:'row-20'}]});
  await expect(store.work({...day,include_closed:true,cursor:first.next_cursor,snapshot:first.snapshot})).rejects.toThrow('cursor_mismatch');
  (git.objects.get(git.branch)!['nexus.json'] as any).projects[0].remote=false;
  expect((await store.work({...day})).items).toEqual([]);
  await expect(store.work({...day,project:args.project})).rejects.toThrow('project_unavailable');
});

it('exposes MCP read/write schemas and fails closed on corrupt indexed content',async()=>{
  const {git,store,args}=await setup(),saved=await store.saveWork(args);
  const tools=(await(await gitMcp({id:1,method:'tools/list'},store)).json() as any).result.tools;
  expect(tools.find((t:any)=>t.name==='read_work_items').annotations.readOnlyHint).toBe(true);
  expect(tools.find((t:any)=>t.name==='save_work_item').annotations.readOnlyHint).toBe(false);
  delete git.objects.get(git.branch)![`projects/${args.project}/records/${saved.note}.json`];
  const result=(await(await gitMcp({id:2,method:'tools/call',params:{name:'read_work_items',arguments:day}},store)).json() as any).result;
  expect(result.isError).toBe(true);
  expect(result.content[0].text).toContain('read_unavailable');
});

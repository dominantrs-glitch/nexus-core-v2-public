import {expect,it} from 'vitest';
import {GitIntake} from '../src/git-store';
import {GitHubBackend} from '../src/github-store';
import {FakeGitHub} from './git-fixture';
import {dailyInput,readDaily} from '../src/git-daily';
const now='2026-09-24T22:30:00+09:00';
const input={surface:'desktop',now,utc_offset:'+09:00'};
async function setup(){
 const git=await new FakeGitHub().init(),store=new GitIntake(new GitHubBackend(git.config,git.fetch),'owner','test-1');
 const p=await store.create({title:'Synthetic daily',source:'synthetic',request_id:'create'});
 const base={project:p.project,expected_revision:0,source:'synthetic owner statement',evidence:'user_statement',quote:'Please do this.'};
 return {git,store,base};
}
it('uses tomorrow hours without moving the deadline clock, excludes candidates, and performs no writes',async()=>{
 const {git,store,base}=await setup();
 await store.saveHours({...base,request_id:'hours',hours:{schema:1,id:'tomorrow',date:'2026-09-25',
  start:'2026-09-25T09:30:00+09:00',end:'2026-09-25T18:00:00+09:00',status:'current'}});
 await store.saveWork({...base,expected_revision:1,request_id:'today',item:{id:'today',type:'action',title:'Today deadline',status:'open',due_at:'2026-09-24'}});
 await store.saveWork({...base,expected_revision:2,request_id:'candidate',evidence:'model_inference',quote:'',item:{id:'idea',type:'action',title:'Not approved',status:'candidate'}});
 const boundary=git.calls.length;
 const view=await store.daily(input);
 expect(view.hours_text).toBe('明日の勤務：09:30〜18:00');
 expect(view.local_date).toBe('2026-09-24');expect(view.hours_date).toBe('2026-09-25');
 expect(view.alerts).toHaveLength(0);expect(view.actions).toHaveLength(1);
 expect(view.focus).toMatchObject({title:'Today deadline',basis:'suggestion'});
 expect(git.calls.slice(boundary).every(c=>c.method==='GET'||c.method==='POST'&&c.path==='/graphql')).toBe(true);
 expect((await store.daily({...input,surface:'morning'})).hours_status).toBe('missing');
});
it('collects every work page and every alert beyond the three-action cap',async()=>{
 const {store,base}=await setup();
 for(let n=0;n<23;n++)await store.saveWork({...base,expected_revision:n,request_id:'row'+n,
  item:{id:'row'+n,type:'action',title:'Action '+n,status:'open',due_at:'2026-09-23'}});
 const v=await store.daily(input);
 expect(v.complete).toBe(true);expect(v.pages).toBe(2);expect(v.actions).toHaveLength(3);expect(v.alerts).toHaveLength(23);
 expect(v.alerts.some(a=>a.title==='Action 22')).toBe(true);
});
it('shows conflicting hours and never reuses a superseded project summary',async()=>{
 const {store,base}=await setup();
 await store.save({...base,request_id:'summary',kind:'proposal',evidence:'model_inference',quote:'',body:'【画面用の概要】\nOld short summary'});
 await store.saveWork({...base,expected_revision:1,request_id:'action',item:{id:'a',type:'action',title:'Action',status:'open'}});
 for(const [n,start] of ['09:00','10:00'].entries())await store.saveHours({...base,expected_revision:2+n,request_id:'hours'+n,
  hours:{schema:1,id:'hours'+n,date:'2026-09-25',start:`2026-09-25T${start}:00+09:00`,end:'2026-09-25T18:00:00+09:00',status:'current'}});
 const v=await store.daily(input);
 expect(v.hours_status).toBe('conflict');expect(v.hours_records).toHaveLength(2);expect(v.projects[0].status).toBe('stale');
 expect(JSON.stringify(v)).not.toContain('Old short summary');
});
it('separates owner importance from inferred rank and treats open-but-waiting as a real alert',async()=>{
 const {store,base}=await setup();
 await store.saveWork({...base,request_id:'important',quote:'Important: do this.',item:{id:'a',type:'action',title:'Owner choice',status:'open',user_priority:'high',priority_quote:'Important',due_at:'2026-09-23'}});
 await store.saveWork({...base,expected_revision:1,request_id:'waiting',item:{id:'b',type:'action',title:'Pending reply',status:'open',waiting_for:'Reply'}});
 const v=await store.daily(input);
 expect(v.focus).toMatchObject({title:'Owner choice',basis:'owner_priority'});expect(v.focus!.reason).toContain('順番は提案');expect(v.focus!.reason).toContain('本人が重要');
 expect(v.actions).toHaveLength(1);expect(v.alerts.some(a=>a.text.includes('Reply'))).toBe(true);
});
it('rejects drift, malformed offsets and unreadable source data instead of returning an empty success',async()=>{
 const {git,store,base}=await setup();
 await store.saveWork({...base,request_id:'x',item:{id:'x',type:'action',title:'x',status:'open'}});
 await expect(readDaily(dailyInput.parse(input),{snapshot:'a'.repeat(40),generation:'test',projects:[],
  getProject:async()=>{throw Error('unexpected')},getNote:async()=>{throw Error('unexpected')},currentHead:async()=>'b'.repeat(40)})).rejects.toThrow('daily_snapshot_changed');
 await expect(store.daily({...input,now:'2026-09-24T00:00:00Z'})).rejects.toThrow();
 git.failReads=true;await expect(store.daily(input)).rejects.toThrow();
});
it('excludes unshared projects from the daily surface',async()=>{
 const {git,store,base}=await setup();
 await store.saveWork({...base,request_id:'secret',item:{id:'x',type:'action',title:'Excluded',status:'open'}});
 (git.objects.get(git.branch)!['nexus.json'] as any).projects[0].remote=false;
 const v=await store.daily(input);expect(v.actions).toEqual([]);expect(JSON.stringify(v)).not.toContain('Excluded');
});
it('places a timed deadline before the same local whole-day deadline and undated work last',async()=>{
 const {store,base}=await setup();
 for(const [n,id,due] of [[0,'whole-day','2026-09-24'],[1,'timed','2026-09-24T14:00:00Z'],[2,'undated',null]] as const)
  await store.saveWork({...base,expected_revision:n,request_id:id,item:{id,type:'action',title:id,status:'open',due_at:due}});
 const view=await store.daily(input);
 expect(view.actions.map(a=>a.id)).toEqual(['timed','whole-day','undated']);
 expect(view.alerts).toEqual([]);
});

import {expect,it} from 'vitest';
import {FakeGitHub} from './git-fixture';
import {GitHubBackend} from '../src/github-store';
import {GitIntake} from '../src/git-store';
import {calendarPath,japaneseNow} from '../src/git-calendar';
const now='2026-09-24T06:00:00+09:00';
const payload=(request_id='sync')=>({request_id,retrieved_at:now,complete:true,range_start:'2026-09-24',range_end:'2026-09-26',
 calendars:[{id:'primary@example.invalid',complete:true,events:[{id:'meeting',title:'予定',start:'2026-09-24T11:00:00+09:00',end:'2026-09-24T12:00:00+09:00',all_day:false}]},
 {id:'work@example.invalid',complete:true,events:[{id:'shift',title:'勤務',start:'2026-09-25T09:30:00+09:00',end:'2026-09-25T18:00:00+09:00',all_day:false,shift_evidence:'10月度シフト'}]}]});
async function setup(){
 const fake=await new FakeGitHub().init(),backend=new GitHubBackend(fake.config,fake.fetch),store=new GitIntake(backend,'owner','test-1');
 const p=await store.create({title:'Calendar source',source:'synthetic',request_id:'create'});
 return {fake,backend,store,project:p.project,scope:{project:p.project,calendars:['primary@example.invalid','work@example.invalid'],workplace:'work@example.invalid'}};
}
it('stores source atomically, retains overview revision, and replays without another write',async()=>{
 const {fake,store,project,scope}=await setup();
 await store.save({project,expected_revision:0,request_id:'overview',kind:'proposal',body:'【画面用の概要】\n短い概要',source:'synthetic',evidence:'model_inference',quote:''});
 const result=await store.syncCalendar(payload(),scope,now),head=fake.branch;
 expect(result.revision).toBe(1);
 expect(await store.syncCalendar(payload(),scope,now)).toEqual(result);expect(fake.branch).toBe(head);
 const v=await store.daily({surface:'desktop',now,utc_offset:'+09:00'});
 expect(v.hours_text).toBe('明日の勤務：09:30〜18:00');expect(v.calendar_status).toBe('current');expect(v.events[0].title).toBe('予定');
 expect(v.events_date).toBe('2026-09-24');expect((fake.objects.get(head)![`projects/${project}/manifest.json`] as any).revision).toBe(1);
 expect(await store.checkDaily(head,project,now)).toBe(true);
 expect(await store.checkDaily(head,project,'2026-09-24T08:00:00+09:00')).toBe(false);
});
it('uses new Calendar times, suppresses cancelled/stale managed hours, and preserves manual hours',async()=>{
 const {store,project,scope}=await setup();
 const base={project,expected_revision:0,request_id:'old',source:'Google Calendar; calendar_id=work@example.invalid; event_id=shift; retrieved previously',evidence:'external_source',quote:''};
 await store.saveHours({...base,hours:{schema:1,id:'gcal-shift',date:'2026-09-25',start:'2026-09-25T08:00:00+09:00',end:'2026-09-25T17:00:00+09:00',status:'current'}});
 await store.syncCalendar(payload(),scope,now);
 const read=(at=now)=>store.daily({surface:'desktop',now:at,utc_offset:'+09:00'});
 expect((await read()).hours_records).toHaveLength(1);expect((await read()).hours_text).toContain('09:30');
 const cancelled=payload('cancel');cancelled.calendars[1].events=[];cancelled.retrieved_at='2026-09-24T06:01:00+09:00';
 await store.syncCalendar(cancelled,scope,cancelled.retrieved_at);expect((await read(cancelled.retrieved_at)).hours_status).toBe('missing');
 await store.saveHours({...base,expected_revision:1,request_id:'manual',evidence:'user_statement',quote:'Tomorrow 10 to 18',source:'owner',
   hours:{schema:1,id:'manual',date:'2026-09-25',start:'2026-09-25T10:00:00+09:00',end:'2026-09-25T18:00:00+09:00',status:'current'}});
 const stale=await read('2026-09-24T08:00:00+09:00');expect(stale.calendar_status).toBe('stale');expect(stale.events).toEqual([]);
 expect(stale.hours_records).toHaveLength(1);expect(stale.hours_text).toContain('10:00');
});
it('rejects incomplete, foreign, old, out-of-range and ambiguous input before mutation',async()=>{
 const {fake,store,scope}=await setup(),head=fake.branch;
 const ambiguous:any=payload();ambiguous.calendars[1].events[0].shift_evidence='シフトの相談';
 for(const value of [ambiguous,{...payload(),complete:false},{...payload(),range_end:'2026-09-27'},
   {...payload(),retrieved_at:'2026-09-24T05:00:00+09:00'},
   {...payload(),calendars:[payload().calendars[0],payload().calendars[0]]}])
   await expect(store.syncCalendar(value,scope,now)).rejects.toThrow();
 expect(fake.branch).toBe(head);
 await store.syncCalendar(payload(),scope,now);
 const newer={...payload('newer'),retrieved_at:'2026-09-24T06:02:00+09:00'};
 await store.syncCalendar(newer,scope,newer.retrieved_at);
 await expect(store.syncCalendar(payload('late'),scope,newer.retrieved_at)).rejects.toThrow('calendar_older_source_rejected');
});
it('rejects frozen/archived/hidden scope and changed source identities',async()=>{
 const {fake,store,scope,project}=await setup();await store.syncCalendar(payload(),scope,now);
 const files=fake.objects.get(fake.branch)!;
 (files[calendarPath(project)] as any).owner='other';
 await expect(store.daily({surface:'morning',now,utc_offset:'+09:00'})).rejects.toThrow('calendar_identity');
 (files[calendarPath(project)] as any).owner='owner';
 files['lifecycle.json']={schema:1,owner:'owner',generation:'test-1',revision:1,records:{[project]:{status:'archived',canonical:null,quote:'archive',source:'owner',reason:'owner'}},relations:[]};
 await expect(store.syncCalendar(payload('archived'),scope,now)).rejects.toThrow('calendar_project_not_active');
 expect((await store.daily({surface:'morning',now,utc_offset:'+09:00'})).calendar_status).toBe('missing');
});
it('batches over 46 manifests without dropping other project work and never batches a mutation',async()=>{
 const {fake,store,backend}=await setup();
 for(let i=0;i<46;i++)await store.create({title:'Another '+i,source:'synthetic',request_id:'p'+i});
 const before=fake.calls.length;
 const v=await store.daily({surface:'morning',now,utc_offset:'+09:00'});
 const calls=fake.calls.slice(before);expect(v.pages).toBe(10);expect(calls.length).toBeLessThan(15);
 expect(calls.filter(c=>c.path==='/graphql')).toHaveLength(2);
 await expect(backend.readMany(fake.branch,['../../secret'])).rejects.toThrow('invalid_canonical_path');
 expect(japaneseNow(new Date('2026-09-24T15:30:00Z'))).toBe('2026-09-25T00:30:00.000+09:00');
});
it('preserves 500-character event titles and rejects longer titles without a partial update',async()=>{
 const {store,fake,scope}=await setup(),a=payload();
 a.calendars[0].events[0].title='予'.repeat(500);
 await store.syncCalendar(a,scope,now);
 const view=await store.daily({surface:'morning',now,utc_offset:'+09:00'});
 expect(view.events[0].title).toBe(a.calendars[0].events[0].title);
 const head=fake.branch,b=payload('too-long');b.calendars[0].events[0].title='予'.repeat(501);
 await expect(store.syncCalendar(b,scope,now)).rejects.toThrow();expect(fake.branch).toBe(head);
});
it('orders mixed-offset, all-day and overnight events by actual start time without changing original values',async()=>{
 const {store,scope,fake,project}=await setup(),a=payload();
 a.calendars[0].events=[
  {id:'late-utc',title:'Late UTC',start:'2026-09-24T01:00:00Z',end:'2026-09-24T02:00:00Z',all_day:false},
  {id:'early-jst',title:'Early JST',start:'2026-09-24T09:00:00+09:00',end:'2026-09-24T10:00:00+09:00',all_day:false},
  {id:'all-day',title:'All day',start:'2026-09-24',end:'2026-09-25',all_day:true},
  {id:'overnight',title:'Overnight',start:'2026-09-23T23:00:00+09:00',end:'2026-09-24T08:00:00+09:00',all_day:false}
 ];
 await store.syncCalendar(a,scope,now);
 const view=await store.daily({surface:'morning',now,utc_offset:'+09:00'});
 expect(view.events.map(e=>e.id)).toEqual(['overnight','all-day','early-jst','late-utc']);
 expect(view.events.at(-1).start).toBe('2026-09-24T01:00:00Z');
 const saved=fake.objects.get(fake.branch)![calendarPath(project)] as any;
 expect(saved.calendars.find((c:any)=>c.id==='primary@example.invalid').events.map((e:any)=>e.id))
  .toEqual(['overnight','all-day','early-jst','late-utc']);
});

/** Source-backed Calendar snapshot. Operational data, never an owner decision. */
import {z} from 'zod';

const id=z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/);
const calendarId=z.string().min(1).max(200);
const instant=z.iso.datetime({offset:true});
export const calendarEvent=z.object({id:z.string().regex(/^[A-Za-z0-9_-]{1,200}$/),
  title:z.string().max(500),start:z.string(),end:z.string(),all_day:z.boolean(),
  shift_evidence:z.string().max(160).optional()}).strict().superRefine((e,c)=>{
  const valid=e.all_day?z.iso.date():instant;
  if(!valid.safeParse(e.start).success||!valid.safeParse(e.end).success||e.all_day&&e.start>=e.end||
     (!e.all_day&&Date.parse(e.start)>=Date.parse(e.end))||
     (e.shift_evidence!==undefined&&(e.all_day||!/^\s*(?:\d{1,2}月(?:度)?\s*)?シフト\s*$/.test(e.shift_evidence)||
       Date.parse(e.end)-Date.parse(e.start)>86400000)))c.addIssue({code:'custom',message:'invalid event interval or shift evidence'});
});
export const calendarPayload=z.object({retrieved_at:instant,range_start:z.iso.date(),range_end:z.iso.date(),
  complete:z.literal(true),calendars:z.array(z.object({id:calendarId,complete:z.literal(true),
    events:z.array(calendarEvent).max(50)}).strict()).length(2)}).strict();
export const calendarRefresh=calendarPayload.extend({request_id:z.string().min(1).max(150)}).strict();
export const calendarSnapshot=calendarPayload.extend({schema:z.literal(1),owner:z.string(),generation:id,project:id,
  workplace_calendar_id:calendarId,sequence:z.number().int().safe().positive(),received_at:instant,binding:z.literal(false),
  replaced_hours:z.array(z.object({id, date:z.iso.date(),event_id:z.string(),calendar_id:calendarId}).strict()).max(500)}).strict();
export type CalendarSnapshot=z.infer<typeof calendarSnapshot>;
export const calendarPath=(project:string)=>`projects/${project}/daily/calendar.json`;
export function japaneseNow(now=new Date()){return new Date(now.getTime()+9*3600000).toISOString().replace('Z','+09:00');}
export function japaneseDate(now:string,days=0){return new Date(Date.parse(now)+9*3600000+days*86400000).toISOString().slice(0,10);}
export function calendarStart(event:{start:string;all_day:boolean}){
  return Date.parse(event.start+(event.all_day?'T00:00:00+09:00':''));
}
export function validateCalendar(a:z.infer<typeof calendarPayload>,allowed:string[],workplace:string,now:string){
  if(allowed.length!==2||new Set(allowed).size!==2||!allowed.includes(workplace)||
    a.calendars.some(c=>!allowed.includes(c.id))||new Set(a.calendars.map(c=>c.id)).size!==2||
    a.range_start!==japaneseDate(now)||a.range_end!==japaneseDate(now,2)||
    Date.parse(a.retrieved_at)>Date.parse(now)+60000||Date.parse(now)-Date.parse(a.retrieved_at)>5*60000)
    throw Error('calendar_scope_or_freshness_invalid');
  const min=Date.parse(a.range_start+'T00:00:00+09:00'),max=Date.parse(a.range_end+'T00:00:00+09:00');
  for(const c of a.calendars){
    if(new Set(c.events.map(e=>e.id)).size!==c.events.length)throw Error('calendar_duplicate_event');
    for(const e of c.events){
      const start=Date.parse(e.start+(e.all_day?'T00:00:00+09:00':'')),end=Date.parse(e.end+(e.all_day?'T00:00:00+09:00':''));
      if(start>=max||end<=min||e.shift_evidence!==undefined&&c.id!==workplace)throw Error('calendar_event_outside_scope');
    }
    c.events.sort((x,y)=>calendarStart(x)-calendarStart(y)||x.id.localeCompare(y.id));
  }
  a.calendars.sort((x,y)=>x.id.localeCompare(y.id));
}
export function parseCalendar(raw:unknown,identity:{owner:string;generation:string;project:string}){
  const s=calendarSnapshot.parse(raw);
  if(s.owner!==identity.owner||s.generation!==identity.generation||s.project!==identity.project)
    throw Error('calendar_identity_mismatch');
  // Validate the stored source's internal scope using its successful retrieval time.
  validateCalendar(s,s.calendars.map(c=>c.id),s.workplace_calendar_id,s.retrieved_at);
  return s;
}

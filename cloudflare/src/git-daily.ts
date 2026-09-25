/** One read-only projection for local morning mail and Desktop. No delivery or mutation. */
import {z} from 'zod';
import {readWork,workReadInput} from './git-work';
import {calendarStart,japaneseDate,type CalendarSnapshot} from './git-calendar';

export const dailyInput=z.object({surface:z.enum(['morning','desktop']),
  now:z.iso.datetime({offset:true}),utc_offset:z.literal('+09:00')}).strict()
  .refine(a=>a.now.endsWith(a.utc_offset),'explicit Japanese local time required');
type Args=z.infer<typeof dailyInput>;
type Source={snapshot:string;generation:string;projects:{id:string;canonical_project?:string}[];
  getProject:(id:string)=>Promise<any>;getNote:(p:any,id:string)=>Promise<any>;currentHead:()=>Promise<string>;calendars?:CalendarSnapshot[]};

function shiftedDate(instant:string,days=0){return new Date(Date.parse(instant)+9*3600000+days*86400000).toISOString().slice(0,10);}
function timeLabel(instant:string,date:string){
  const local=new Date(Date.parse(instant)+9*3600000).toISOString();
  return (local.slice(0,10)!==date?'翌日 ':'')+local.slice(11,16);
}
function action(item:any){
  return {project:item.project,id:item.id,title:item.title,due_at:item.due_at,
    basis:item.user_priority==='high'?'owner_priority':'suggestion',
    reason:item.overdue?(item.user_priority==='high'?'本人が重要と指定した作業です。期限を過ぎているため最初の着手候補としています。順番は提案です。':'期限を過ぎた作業からの着手案です。'):item.user_priority==='high'?
      '本人が重要と指定した作業です。着手順は提案です。':item.due_at?'期限が近い作業からの着手案です。':'登録された作業からの着手案です。'};
}

export async function readDaily(a:Args,source:Source){
  const localDate=shiftedDate(a.now),hoursDate=shiftedDate(a.now,a.surface==='desktop'?1:0);
  const base=workReadInput.parse({date:localDate,now:a.now,utc_offset:a.utc_offset});
  const items:any[]=[],hours:any[]=[],alerts:any[]=[],seen=new Set<string>();
  let cursor:string|undefined,pages=0;
  do{
    const page=await readWork({...base,...(cursor?{cursor,snapshot:source.snapshot}:{})},source.snapshot,
      source.projects,source.getProject,source.getNote,hoursDate);
    items.push(...page.items);hours.push(...page.hours);alerts.push(...page.alerts);pages++;
    if(page.next_cursor===null){if(!page.exhausted)throw Error('daily_incomplete');break;}
    if(seen.has(page.next_cursor)||pages>=1000)throw Error('daily_incomplete');
    seen.add(page.next_cursor);cursor=page.next_cursor;
  }while(true);
  const calendarSources=source.calendars??[];
  const fresh=calendarSources.filter(s=>Date.parse(a.now)>=Date.parse(s.retrieved_at)-60000&&
    Date.parse(a.now)-Date.parse(s.retrieved_at)<=90*60000&&s.range_start<=localDate&&s.range_end>hoursDate);
  const calendarStatus=!calendarSources.length?'missing':fresh.length===calendarSources.length?'current':'stale';
  const retrieved=calendarSources.length?calendarSources.map(s=>s.retrieved_at).sort((x,y)=>Date.parse(x)-Date.parse(y))[0]:null;
  // A managed Calendar source replaces only the exact external records it owns.
  // Stale snapshots still suppress those old values; they never revive as a fallback.
  const managedHours=hours.filter(h=>!calendarSources.some(s=>s.project===h.project&&s.replaced_hours.some(r=>
    r.id===h.id&&r.date===h.date&&h.evidence==='external_source'&&h.source.includes('calendar_id='+r.calendar_id+';')&&
    h.source.includes('event_id='+r.event_id+';'))));
  const events:any[]=[];
  for(const s of fresh){
    for(const c of s.calendars)for(const e of c.events){
      const startDate=e.all_day?e.start:japaneseDate(e.start),endDate=e.all_day?e.end:japaneseDate(e.end);
      if(startDate<=localDate&&(e.all_day?endDate>localDate:Date.parse(e.end)>Date.parse(localDate+'T00:00:00+09:00')))
        events.push({id:e.id,title:e.title,start:e.start,end:e.end,all_day:e.all_day,calendar_id:c.id});
      if(c.id===s.workplace_calendar_id&&e.shift_evidence&&startDate===hoursDate)managedHours.push({project:s.project,
        note:'calendar-source',revision:s.sequence,start:e.start,end:e.end,source_kind:'calendar'});
    }
  }
  events.sort((x,y)=>calendarStart(x)-calendarStart(y)||x.title.localeCompare(y.title)||x.id.localeCompare(y.id));
  const uniqueHours=new Map(managedHours.map(h=>[`${Date.parse(h.start)}/${Date.parse(h.end)}`,h]));
  const hoursStatus=!uniqueHours.size?'missing':uniqueHours.size>1?'conflict':'reported';
  const prefix=a.surface==='desktop'?'明日の勤務':'今日の勤務';
  const hoursText=hoursStatus==='missing'?prefix+'：勤務時間未取得':hoursStatus==='conflict'?
    prefix+'：記録が一致していません':prefix+'：'+[...uniqueHours.values()].map(h=>timeLabel(h.start,hoursDate)+'〜'+timeLabel(h.end,hoursDate))[0];
  const priorities:Record<string,number>={high:0,normal:1,low:3};
  const deadline=(value:string|null)=>value===null?Infinity:Date.parse(value.length===10?
    value+'T23:59:59.999'+a.utc_offset:value);
  const available=items.filter(i=>i.type==='action'&&i.status==='open'&&!i.blocker&&!i.approval_wait&&!i.waiting_for);
  available.sort((x,y)=>Number(y.overdue)-Number(x.overdue)||
    (priorities[x.user_priority]??2)-(priorities[y.user_priority]??2)||
    deadline(x.due_at)-deadline(y.due_at)||x.created_at.localeCompare(y.created_at)||x.id.localeCompare(y.id));
  const actions=available.slice(0,3).map(action);
  const canonical=(id:string)=>source.projects.find(p=>p.id===id)?.canonical_project??id;
  const projectIds=[...new Set([...actions.map(x=>canonical(x.project)),...alerts.map(x=>canonical(x.project))])].slice(0,3);
  const projects=await Promise.all(projectIds.map(async id=>{
    const p=await source.getProject(id);
    if(!p.overview)return {project:id,title:p.title,summary:'概要がまだありません。',status:'missing'};
    const n=await source.getNote(p,p.overview),header='【画面用の概要】\n';
    if(n.kind!=='proposal'||n.evidence!=='model_inference'||!n.body.startsWith(header))throw Error('daily_invalid_overview');
    if(n.revision!==p.revision)return {project:id,title:p.title,summary:'概要の更新が必要です。',status:'stale'};
    const text=n.body.slice(header.length).trim();
    // Do not clip a condition or negation into a false short statement.
    return {project:id,title:p.title,summary:text.length<=220?text:'詳しい概要は案件から確認できます。',status:'current'};
  }));
  const alertRows=alerts.map(i=>{
    const parts=[];
    if(i.overdue)parts.push('期限を過ぎています');
    if(i.blocker)parts.push('進められない理由：'+i.blocker);
    if(i.approval_wait)parts.push('本人の確認待ち');
    if(i.waiting_for)parts.push('待っているもの：'+i.waiting_for);
    return {project:i.project,id:i.id,title:i.title,text:parts.join('／')||'待機中'};
  });
  // A result is never silently assembled across revisions, even when another project changed.
  if(await source.currentHead()!==source.snapshot)throw Error('daily_snapshot_changed');
  return {schema:1,surface:a.surface,state:hoursStatus==='reported'&&calendarStatus!=='stale'&&!projects.some(p=>p.status!=='current')?'ok':'partial',
    as_of:a.now,local_date:localDate,hours_date:hoursDate,expires_at:new Date(Date.parse(a.now)+15*60000).toISOString(),
    hours_status:hoursStatus,hours_text:hoursText,
    hours_records:[...uniqueHours.values()].map(h=>({project:h.project,note:h.note,revision:h.revision,start:h.start,end:h.end,source_kind:h.source_kind??'record'})),
    events:calendarStatus==='current'?events:[],events_date:localDate,calendar_status:calendarStatus,calendar_retrieved_at:retrieved,
    calendar_message:calendarStatus==='current'?'指定したカレンダーを取得済みです。':calendarStatus==='stale'?
      '予定・勤務の取得が古くなっています。古いカレンダー情報は表示していません。':'予定の連携データをまだ取得できていません。',
    focus:actions[0]??null,actions,alerts:alertRows,projects,
    limitation:'Nexusに登録された共有の作業と、指定カレンダーの予定を表示しています。文章中の未整理作業は含みません。',
    snapshot:source.snapshot,generation:source.generation,pages,complete:true,binding:false};
}

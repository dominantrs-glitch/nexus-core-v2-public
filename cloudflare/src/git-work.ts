/** Attributed intentions/actions and reported work hours, in the same note ledger. */
import {z} from 'zod';
import {hash} from './relay-common';

const id=z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/);
const text=(max:number)=>z.string().trim().min(1).max(max);
const revision=z.number().int().safe().nonnegative();
const date=z.iso.date(),instant=z.iso.datetime({offset:true});
const status=z.enum(['candidate','open','waiting','done','cancelled']);
export const workHeader='【作業・やりたいこと】\n';
export const hoursHeader='【勤務時間の記録】\n';
export const learningRef=z.object({project:id,note:id,revision:revision.positive()}).strict();
export const workLearning=z.object({corrections:z.array(learningRef).max(8),checks:z.array(learningRef).max(8),
  conclusion:z.string().trim().min(1).max(1200)}).strict();
export const workDraft=z.object({schema:z.literal(1),id,type:z.enum(['intent','action']),title:text(200),status,
  purpose:z.string().max(1000).default(''),unknowns:z.string().max(1000).default(''),
  next_step:z.string().max(1000).default(''),due_at:z.union([date,instant]).nullable().default(null),
  user_priority:z.enum(['high','normal','low']).nullable().default(null),priority_quote:z.string().max(1000).default(''),
  ai_suggestion:z.string().max(1000).default(''),waiting_for:z.string().max(500).default(''),
  blocker:z.string().max(500).default(''),approval_wait:z.boolean().default(false),learning:workLearning.optional()}).strict();
export const hoursDraft=z.object({schema:z.literal(1),id,date,start:instant,end:instant,
  status:z.enum(['current','withdrawn'])}).strict().refine(v=>Date.parse(v.start)<Date.parse(v.end) &&
    Date.parse(v.end)-Date.parse(v.start)<=86400000 && v.start.slice(0,10)===v.date,
    'work hours must start on the supplied local date and last at most 24 hours');
const saveBase={project:id,expected_revision:revision,request_id:text(150),source:text(2000),
  evidence:z.enum(['user_statement','model_inference','external_source']),quote:z.string().max(8000),
  supersedes:id.nullable().optional()};
export const workInput=z.object({...saveBase,item:workDraft.omit({schema:true})}).strict();
export const hoursInput=z.object({...saveBase,hours:hoursDraft}).strict();
export const workReadInput=z.object({project:id.optional(),date,now:instant,
  utc_offset:z.string().regex(/^[+-](?:(?:0\d|1[0-3]):[0-5]\d|14:00)$/),
  include_closed:z.boolean().default(false),cursor:z.string().max(1500).optional(),
  snapshot:z.string().regex(/^[a-f0-9]{40}$/).optional()}).strict().refine(a=>
    a.now.endsWith(a.utc_offset) && a.now.slice(0,10)===a.date,'date, now and explicit UTC offset must agree');
export const workIndex=z.object({id,note:id,key:z.string().max(300),kind:z.enum(['work','hours']),
  status:z.enum(['candidate','open','waiting','done','cancelled','current','withdrawn']),
  date:date.nullable(),created:z.string().datetime(),
  completed_at:z.string().datetime().nullable().optional(),cancelled_at:z.string().datetime().nullable().optional()}).strict();
export const learningAssessment=z.object({status:z.enum(['review_required','no_linked_correction','needs_verification','candidate_review_available']),
  correction_notes:z.array(id).max(500),scope:id,binding:z.literal(false),verification:z.literal('draft_sources_only'),
  sources:workLearning.optional()}).strict();
export function assessWorkLearning(project:string,sources?:z.infer<typeof workLearning>) {
  return {status:!sources?'review_required':!sources.corrections.length?'no_linked_correction':
    !sources.checks.length?'needs_verification':'candidate_review_available',
    correction_notes:sources?.corrections.map(r=>r.note)??[],scope:project,binding:false,
    verification:'draft_sources_only',...(sources?{sources}:{})};
}
export function workKey(title:string){return title.normalize('NFKC').trim().toLocaleLowerCase().replace(/\s+/g,' ');}
export function parseWork(body:string) {
  try {
    if(body.startsWith(workHeader))return {kind:'work' as const,value:workDraft.parse(JSON.parse(body.slice(workHeader.length)))};
    if(body.startsWith(hoursHeader))return {kind:'hours' as const,value:hoursDraft.parse(JSON.parse(body.slice(hoursHeader.length)))};
    return null;
  }catch{throw new Error('invalid_work_record');}
}
export type WorkRead=z.infer<typeof workReadInput>;
type WorkProject={id:string;title:string;revision:number;work_index?:z.infer<typeof workIndex>[]};
type Note={id:string;body:string;evidence:string;quote:string;source:string;created:string;revision:number;kind:string};
const cursorSchema=z.object({snapshot:z.string(),filter:z.string(),project:revision,item:revision}).strict();
export async function readWork(a:WorkRead,snapshot:string,projects:{id:string}[],
  getProject:(id:string)=>Promise<WorkProject>,getNote:(p:WorkProject,id:string)=>Promise<Note>,hoursDate=a.date,collectClosedReviews=false) {
  const filter=await hash(JSON.stringify([a.project??null,a.date,a.now,a.utc_offset,a.include_closed,hoursDate]));
  let project=0,item=0;
  if(a.cursor) {
    let cursor:z.infer<typeof cursorSchema>;
    try{cursor=cursorSchema.parse(JSON.parse(atob(a.cursor)));}catch{throw new Error('invalid_work_cursor');}
    if(!a.snapshot || cursor.snapshot!==snapshot || cursor.filter!==filter)throw new Error('work_cursor_mismatch_restart');
    ({project,item}=cursor);
  }
  if(project>projects.length)throw new Error('invalid_work_cursor');
  const items:any[]=[],hours:any[]=[],alerts:any[]=[],closed_review_items:any[]=[];
  let checked=0,visited=0;
  while(project<projects.length && checked<20 && visited<5) {
    const p=await getProject(projects[project].id);visited++;
    const entries=p.work_index??[];
    if(item>entries.length)throw new Error('invalid_work_cursor');
    while(item<entries.length && checked<20) {
      const entry=entries[item++];checked++;
      if(entry.kind==='hours' ? entry.date!==hoursDate || entry.status!=='current' :
        !a.include_closed && (entry.status==='cancelled'||entry.status==='done'&&!collectClosedReviews))continue;
      const note=await getNote(p,entry.note),parsed=parseWork(note.body);
      if(!parsed || parsed.kind!==entry.kind || parsed.value.id!==entry.id || parsed.value.status!==entry.status ||
        (parsed.kind==='work' ? workKey(parsed.value.title)!==entry.key : parsed.value.date!==entry.date))
        throw new Error('work_record_unavailable');
      const base={project:p.id,project_title:p.title,project_revision:p.revision,note:note.id,revision:note.revision,
        created_at:entry.created,updated_at:note.created,source:note.source,evidence:note.evidence,quote:note.quote,binding:false};
      if(parsed.kind==='hours') {
        if(note.kind!=='source' || !['user_statement','external_source'].includes(note.evidence))throw new Error('work_record_unavailable');
        hours.push({...base,...parsed.value});continue;
      }
      const value=parsed.value;
      if(note.kind!=='proposal' || note.evidence==='external_source' ||
        (note.evidence==='model_inference' && !['candidate','cancelled'].includes(value.status)) ||
        (value.user_priority ? note.evidence!=='user_statement' || !value.priority_quote.trim() || !note.quote.includes(value.priority_quote) :
          !!value.priority_quote))throw new Error('work_record_unavailable');
      const due=value.due_at?(value.due_at.length===10?Date.parse(value.due_at+'T23:59:59.999'+a.utc_offset):Date.parse(value.due_at)):null;
      const overdue=!['done','cancelled'].includes(value.status) && due!==null && due<Date.parse(a.now);
      const row={...base,...value,overdue,
        completed_at:value.status==='done'?(entry.completed_at??note.created):null,
        cancelled_at:value.status==='cancelled'?(entry.cancelled_at??note.created):null};
      if(value.status==='done'&&!a.include_closed)closed_review_items.push(row);
      else items.push(row);
      if(!['done','cancelled'].includes(value.status) && (overdue || value.blocker || value.approval_wait || value.waiting_for || value.status==='waiting'))
        alerts.push({project:p.id,id:value.id,title:value.title,overdue,blocker:value.blocker,
          approval_wait:value.approval_wait,waiting_for:value.waiting_for});
    }
    if(item===entries.length){project++;item=0;}
  }
  const exhausted=project===projects.length;
  return {snapshot,items,hours,alerts,closed_review_items,next_cursor:exhausted?null:btoa(JSON.stringify({snapshot,filter,project,item})),
    exhausted,complete:!a.cursor&&exhausted,scope:a.project?'project_work_records':'permitted_shared_work_records',
    counts:{records_checked:checked,projects_checked:visited},binding:false,context_evaluated:false,
    instruction:'Collect every page at this snapshot and unchanged date/now/offset before a daily brief. '
      +'No rows is not proof of no work outside this structured shared scope. Use only reported hours for the date; '
      +'if none say 勤務時間未取得; if multiple disagree show a conflict. Show a short natural brief with one focus '
      +'and up to three next actions, but never hide additional overdue/blocker/approval/waiting alerts. '
      +'Owner priority is separate from AI suggestions. Candidate intentions are uncommitted. Due dates are deadlines, '
      +'not calendar appointments. View changes never delete source records; inspect project context before acting.'};
}

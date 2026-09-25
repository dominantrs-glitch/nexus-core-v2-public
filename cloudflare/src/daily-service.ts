/** Narrow Apps Script integration; separate from OAuth/MCP and native authority. */
import {z} from 'zod';
import {GitHubBackend,type GitConfig} from './github-store';
import {GitIntake,StoreError} from './git-store';
import {calendarRefresh,japaneseNow,japaneseDate} from './git-calendar';
import {failure,hash,smallBody,type RelayEnv} from './relay-common';

const scopeSchema=z.object({project:z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/),
  calendars:z.array(z.string().min(1).max(200)).length(2),workplace:z.string().min(1).max(200)}).strict();
const readInput=z.object({surface:z.enum(['morning','desktop'])}).strict();
const checkInput=z.object({snapshot:z.string().regex(/^[a-f0-9]{40}$/),local_date:z.iso.date(),as_of:z.iso.datetime({offset:true})}).strict();
async function dailyBudget(env:RelayEnv){
  const day='daily:'+new Date().toISOString().slice(0,10);
  const row=await env.DB.prepare(`INSERT INTO relay_budget(day,requests,bytes,auth) VALUES (?,1,0,0)
    ON CONFLICT(day) DO UPDATE SET requests=requests+1 WHERE requests<288 RETURNING day`).bind(day).first();
  return !!row;
}
export async function dailyService(request:Request,env:RelayEnv,make=()=>new GitIntake(new GitHubBackend(env as GitConfig),
  env.OWNER_ID,env.GIT_GENERATION,'draft-intake',env.GIT_NATIVE_OWNER)){
  const url=new URL(request.url);
  if(!['/daily/read','/daily/refresh','/daily/check'].includes(url.pathname)||url.search)return failure(404,'not_found');
  if(env.DAILY_ENABLED!=='true'||env.GIT_WORKSPACE_ENABLED!=='true'||env.GIT_DATA_MODE!=='draft-intake')return failure(503,'daily_disabled');
  const token=request.headers.get('Authorization')?.match(/^Bearer ([A-Za-z0-9_-]{43,128})$/)?.[1];
  if(!token||!env.DAILY_TOKEN_SHA256||await hash(token)!==env.DAILY_TOKEN_SHA256)return failure(401,'daily_unauthorized');
  if(request.method!=='POST')return new Response(null,{status:405,headers:{Allow:'POST','Cache-Control':'no-store'}});
  if(!request.headers.get('Content-Type')?.startsWith('application/json'))return failure(415,'json_required');
  let args:unknown,scope:z.infer<typeof scopeSchema>;
  try{scope=scopeSchema.parse({project:env.DAILY_PROJECT,calendars:JSON.parse(env.DAILY_CALENDAR_IDS??''),workplace:env.DAILY_WORKPLACE_CALENDAR});}
  catch{return failure(503,'daily_configuration_required');}
  try{args=JSON.parse(await smallBody(request,65536));}catch{return failure(400,'daily_invalid_request');}
  if(!await dailyBudget(env))return failure(429,'daily_limit_reached');
  const now=japaneseNow(),store=make();
  try{
    if(url.pathname==='/daily/check'){
      const a=checkInput.parse(args),age=Date.parse(now)-Date.parse(a.as_of);
      if(a.local_date!==japaneseDate(now)||japaneseDate(a.as_of)!==a.local_date||age<0||age>15*60000||!await store.checkDaily(a.snapshot,scope.project,now))
        return failure(409,'daily_changed_refresh_required');
      return Response.json({current:true,snapshot:a.snapshot,local_date:a.local_date,checked_at:now},{headers:{'Cache-Control':'no-store'}});
    }
    let sync:unknown;
    let surface:'morning'|'desktop';
    if(url.pathname==='/daily/refresh'){
      const a=calendarRefresh.parse(args);
      sync=await store.syncCalendar(a,scope,now);surface='morning';
    }else surface=readInput.parse(args).surface;
    const view=await store.daily({surface,now,utc_offset:'+09:00'});
    return Response.json({...(sync?{calendar_sync:{...sync as object,retrieved_at:view.calendar_retrieved_at,status:view.calendar_status}}:{}),view},
      {headers:{'Cache-Control':'no-store'}});
  }catch(error){
    if(error instanceof z.ZodError||error instanceof Error&&/^calendar_(?:scope_or_freshness_invalid|duplicate_event|event_outside_scope)$/.test(error.message))
      return failure(400,'daily_invalid_request');
    if(error instanceof StoreError&&error.code==='canonical_rate_limited')return Response.json({error:'daily_upstream_rate_limited',
      retry_at:error.diagnostic?.retry_at},{status:429,headers:{'Cache-Control':'no-store'}});
    return failure(503,'daily_unavailable');
  }
}

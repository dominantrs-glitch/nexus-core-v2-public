import {env as bindings} from 'cloudflare:workers';
import {applyD1Migrations,createExecutionContext,type D1Migration} from 'cloudflare:test';
import {beforeAll,beforeEach,afterEach,expect,it,vi} from 'vitest';
import worker from '../src/relay';
import {dailyService} from '../src/daily-service';
import {hash,type RelayEnv} from '../src/relay-common';
import {GitIntake} from '../src/git-store';
import {japaneseNow,japaneseDate} from '../src/git-calendar';
const raw=bindings as unknown as RelayEnv&{TEST_MIGRATIONS:D1Migration[]};
let env:RelayEnv;const token='d'.repeat(43),origin='https://nexus.invalid';
beforeAll(async()=>{await applyD1Migrations(raw.DB,raw.TEST_MIGRATIONS);});
beforeEach(async()=>{
 env={...raw,RELAY_ENABLED:'true',GIT_WORKSPACE_ENABLED:'true',GIT_DATA_MODE:'draft-intake',PUBLIC_ORIGIN:origin,
  DAILY_ENABLED:'true',DAILY_TOKEN_SHA256:await hash(token),DAILY_PROJECT:'p-daily',DAILY_CALENDAR_IDS:'["primary","work"]',DAILY_WORKPLACE_CALENDAR:'work'};
 await raw.DB.exec('DELETE FROM relay_budget;');
});
afterEach(()=>vi.restoreAllMocks());
function req(path:string,body:unknown={},extra:RequestInit={}){return new Request(origin+path,{method:'POST',
 headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(body),...extra});}
it('rejects disabled, missing-key, origin, query, method and content-type requests before upstream work',async()=>{
 const external=vi.spyOn(globalThis,'fetch').mockImplementation(async()=>{throw Error('must not call upstream')});
 expect((await worker.fetch(req('/daily/read',{}, {headers:{'Content-Type':'application/json'}}),env,createExecutionContext())).status).toBe(401);
 expect((await worker.fetch(req('/daily/read',{}, {headers:{Authorization:'Bearer '+token,'Content-Type':'application/json',Origin:'https://other.invalid'}}),env,createExecutionContext())).status).toBe(403);
 expect((await worker.fetch(req('/daily/read?x=1'),env,createExecutionContext())).status).toBe(404);
 expect((await worker.fetch(req('/daily/read',{}, {method:'GET',body:undefined}),env,createExecutionContext())).status).toBe(405);
 expect((await worker.fetch(req('/daily/read',{}, {headers:{Authorization:'Bearer '+token}}),env,createExecutionContext())).status).toBe(415);
 env.DAILY_ENABLED='false';expect((await worker.fetch(req('/daily/read'),env,createExecutionContext())).status).toBe(503);
 expect(external).not.toHaveBeenCalled();
});
it('accepts only declared operations, returns fresh views, and rejects drift or old check receipts',async()=>{
 const now=japaneseNow(),date=japaneseDate(now),snapshot='a'.repeat(40);
 const daily=vi.fn(async(args:any)=>({surface:args.surface,snapshot,calendar_status:'current',calendar_retrieved_at:now}));
 const syncCalendar=vi.fn(async()=>({project:'p-daily',revision:4}));
 const checkDaily=vi.fn(async()=>true),make=()=>({daily,syncCalendar,checkDaily} as unknown as GitIntake);
 const good=await dailyService(req('/daily/read',{surface:'desktop'}),env,make);
 expect(good.status).toBe(200);expect((await good.json() as any).view.surface).toBe('desktop');
 expect((await dailyService(req('/daily/read',{surface:'desktop',project:'another'}),env,make)).status).toBe(400);
 expect((await dailyService(req('/daily/check',{snapshot,local_date:date,as_of:now}),env,make)).status).toBe(200);
 checkDaily.mockResolvedValue(false);
 expect((await dailyService(req('/daily/check',{snapshot,local_date:date,as_of:now}),env,make)).status).toBe(409);
 checkDaily.mockResolvedValue(true);
 expect((await dailyService(req('/daily/check',{snapshot,local_date:date,as_of:japaneseNow(new Date(Date.now()-16*60000))}),env,make)).status).toBe(409);
 const payload={request_id:'refresh',retrieved_at:now,range_start:date,range_end:japaneseDate(now,2),complete:true,
   calendars:[{id:'primary',complete:true,events:[]},{id:'work',complete:true,events:[]}]};
 expect((await dailyService(req('/daily/refresh',payload),env,make)).status).toBe(200);
 expect(syncCalendar).toHaveBeenCalledWith(payload,{project:'p-daily',calendars:['primary','work'],workplace:'work'},expect.any(String));
 expect((await dailyService(req('/daily/refresh',{...payload,complete:false}),env,make)).status).toBe(400);
});
it('caps dedicated daily budget and never returns old success or raw upstream errors',async()=>{
 const day='daily:'+new Date().toISOString().slice(0,10);
 await raw.DB.prepare('INSERT INTO relay_budget(day,requests,bytes,auth) VALUES (?,288,0,0)').bind(day).run();
 const make=()=>{throw Error('credential-private-text');};
 expect((await dailyService(req('/daily/read',{surface:'morning'}),env,make)).status).toBe(429);
 await raw.DB.exec('DELETE FROM relay_budget;');
 const daily=async()=>{throw Error('credential-private-text')};
 const failed=await dailyService(req('/daily/read',{surface:'morning'}),env,()=>({daily} as unknown as GitIntake));
 expect(failed.status).toBe(503);expect(await failed.text()).not.toContain('credential-private-text');
 expect((await dailyService(req('/daily/read',{padding:'x'.repeat(65537)}),env,make)).status).toBe(400);
});

import {expect,it} from 'vitest';
import {FakeGitHub} from './git-fixture';
import {GitHubBackend} from '../src/github-store';
import {GitIntake} from '../src/git-store';
const now='2026-09-24T09:00:00+09:00';
async function setup(){
 const fake=await new FakeGitHub().init(),backend=new GitHubBackend(fake.config,fake.fetch),store=new GitIntake(backend,'owner','test-1');
 const p=await store.create({title:'Independent daily review',source:'synthetic',request_id:'create'});
 const base={project:p.project,expected_revision:0,source:'synthetic owner instruction',evidence:'user_statement',quote:'Please do this.'};
 return {fake,backend,store,base};
}
it('JST date-only deadlines remain on time until midnight and become overdue the next day',async()=>{
 const {store,base}=await setup();await store.saveWork({...base,request_id:'work',item:{id:'due',type:'action',title:'Due today',status:'open',due_at:'2026-09-24'}});
 expect((await store.daily({surface:'morning',now:'2026-09-24T23:59:59+09:00',utc_offset:'+09:00'})).alerts).toHaveLength(0);
 expect((await store.daily({surface:'morning',now:'2026-09-25T00:00:00+09:00',utc_offset:'+09:00'})).alerts).toHaveLength(1);
});
it('orders offset timestamps by actual deadline instant, not their lexical spelling',async()=>{
 const {store,base}=await setup();
 await store.saveWork({...base,request_id:'early',item:{id:'early',type:'action',title:'Earlier actual deadline',status:'open',due_at:'2026-09-24T00:30:00+09:00'}});
 await store.saveWork({...base,expected_revision:1,request_id:'later',item:{id:'later',type:'action',title:'Later actual deadline',status:'open',due_at:'2026-09-23T23:00:00Z'}});
 const v=await store.daily({surface:'morning',now,utc_offset:'+09:00'});
 expect(v.alerts).toHaveLength(2);expect(v.focus!.id).toBe('early');
});
it('GraphQL batching refuses omitted, truncated, wrong identity and GraphQL error responses',async()=>{
 const fake=await new FakeGitHub().init();
 for(const repository of [
  {databaseId:42,isPrivate:true},
  {databaseId:42,isPrivate:true,b0:{__typename:'Blob',text:'{}',byteSize:2,isTruncated:true}},
  {databaseId:999,isPrivate:true,b0:null},
  {databaseId:42,isPrivate:false,b0:null},
 ]){
  const backend=new GitHubBackend(fake.config,async(input,init)=>String(input).endsWith('/graphql')?Response.json({data:{repository}}):fake.fetch(input,init));
  await expect(backend.readMany(fake.branch,['nexus.json'])).rejects.toThrow('canonical_batch_unavailable');
 }
 const backend=new GitHubBackend(fake.config,async(input,init)=>String(input).endsWith('/graphql')?Response.json({data:{repository:{databaseId:42,isPrivate:true,b0:null}},errors:[{message:'denied'}]}):fake.fetch(input,init));
 await expect(backend.readMany(fake.branch,['nexus.json'])).rejects.toThrow('canonical_batch_unavailable');
});
it('missing referenced note from a batch is an error, not an empty daily success',async()=>{
 const {fake,store,base}=await setup();const saved=await store.saveWork({...base,request_id:'work',item:{id:'work',type:'action',title:'Must remain visible',status:'open'}});
 delete fake.objects.get(fake.branch)![`projects/${base.project}/records/${saved.note}.json`];
 await expect(store.daily({surface:'morning',now,utc_offset:'+09:00'})).rejects.toThrow('canonical_invalid_or_unavailable');
});
it('HTTP 200 GraphQL rate errors report a bounded wait without caching partial results',async()=>{
 const fake=await new FakeGitHub().init();let batches=0;
 const backend=new GitHubBackend(fake.config,async(input,init)=>{
   if(String(input).endsWith('/graphql')){batches++;return Response.json({data:{repository:{databaseId:42,isPrivate:true,b0:null}},errors:[{type:'RATE_LIMITED',message:'private upstream text'}]});}
   return fake.fetch(input,init);
 });
 await expect(backend.readMany(fake.branch,['nexus.json'])).rejects.toMatchObject({code:'canonical_rate_limited',diagnostic:{http_status:200,retryable:true,retry_after_seconds:60}});
 expect(batches).toBe(1);
 const value=await backend.read(fake.branch,'nexus.json');expect(value).not.toBeNull();
});
it('readMany always requests exact snapshot blobs, not history or arbitrary file paths',async()=>{
 const {fake,backend,store,base}=await setup();await store.saveWork({...base,request_id:'work',item:{id:'a',type:'action',title:'Current only',status:'open'}});
 const snapshot=fake.branch,before=fake.calls.length;await store.daily({surface:'morning',now,utc_offset:'+09:00'});
 const batches=fake.calls.slice(before).filter(c=>c.path==='/graphql');expect(batches.length).toBeGreaterThan(0);
 const expressions=batches.flatMap(c=>Object.entries(c.body.variables).filter(([k])=>/^p\d+$/.test(k)).map(([,v])=>String(v)));
 expect(expressions.every(x=>x.startsWith(snapshot+':projects/'))).toBe(true);
 expect(expressions.some(x=>/\/changes\/|\/originals\/|\/binary\/|\/learning/.test(x))).toBe(false);
 await expect(backend.readMany(snapshot,['projects/elsewhere/../../secret'])).rejects.toThrow('invalid_canonical_path');
});
it('current catalog revocation excludes previously readable work on the next daily read',async()=>{
 const {fake,store,base}=await setup();await store.saveWork({...base,request_id:'work',item:{id:'a',type:'action',title:'Visible before revocation',status:'open'}});
 expect((await store.daily({surface:'morning',now,utc_offset:'+09:00'})).actions).toHaveLength(1);
 const files=structuredClone(fake.objects.get(fake.branch)!);(files['nexus.json'] as any).projects[0].remote=false;
 fake.branch='f'.repeat(40);fake.objects.set(fake.branch,files);
 const v=await store.daily({surface:'morning',now,utc_offset:'+09:00'});expect(v.actions).toEqual([]);expect(v.alerts).toEqual([]);
});

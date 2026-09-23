import {beforeAll,it,expect} from "vitest";
import {execFileSync} from "node:child_process";
import {resolve} from "node:path";
import {createHash} from "node:crypto";
import {GitCoreStore} from "../src/git-core";
import {GitIntake} from "../src/git-store";
import {GitHubBackend} from "../src/github-store";
import {FakeGitHub} from "./git-fixture";

let input:any;
const canonical=(v:any):string=>Array.isArray(v)?'['+v.map(canonical).join(',')+']':
  v!==null&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}':JSON.stringify(v);
const digest=(text:string)=>createHash('sha256').update(text).digest('hex');
beforeAll(()=>{
  const root=resolve('..'),python=resolve(root,process.platform==='win32'?'.venv/Scripts/python.exe':'.venv/bin/python');
  input=JSON.parse(execFileSync(python,['-m','tests.native_git_fixture'],{cwd:root,encoding:'utf8'}));
});
async function fixture() {
  const fake=await new FakeGitHub().init(),git=new GitHubBackend(fake.config,fake.fetch);
  const core=new GitCoreStore(git,'owner','test-1','synthetic','native-owner');await core.write(input);
  const store=new GitIntake(git,'owner','test-1','synthetic','native-owner');
  const target=await store.create({title:'Synthetic native target',source:'synthetic',request_id:'target'});
  const native=await git.read(fake.branch,'native/catalog.json') as any;
  const operations=['resume','plan','implement','review'];
  native.projects[0].shares=operations.map(operation=>({project:target.project,operation,native_operation:'implement'}));
  const catalog=await git.read(fake.branch,'nexus.json') as any;catalog.context_revision=1;
  const context={schema:1,owner:'owner',mode:'synthetic',generation:'test-1',revision:1,
    profiles:[{project:target.project,operations,required:{global_rules:['native-task'],personal:[],learning:[],relations:[]}}],
    entries:[{id:'native-task',category:'global_rules',projects:[target.project],operations,status:'active',
      source:{native_project:input.project,contract_revision:1,native_operation:'implement'},evidence:[]}]};
  await git.commit(fake.branch,{'native/catalog.json':native,'nexus.json':catalog,'context.json':context});
  return {fake,git,core,store,target,native,context};
}
async function editPackage(f:Awaited<ReturnType<typeof fixture>>,change:(p:any)=>void) {
  const path=`native/projects/${input.project}/1.json`,doc=await f.git.read(f.fake.branch,path) as any;
  const decoded=Object.fromEntries(['project.json','context.json','approvals.json','artifacts/manifest.json'].map(p=>
    [p,JSON.parse(Buffer.from(doc.files[p],'base64').toString('utf8'))]));
  change(decoded);
  const manifest=JSON.parse(Buffer.from(doc.files['manifest.json'],'base64').toString('utf8'));
  for(const [path,value] of Object.entries(decoded)) {
    const text=JSON.stringify(value);doc.files[path]=Buffer.from(text).toString('base64');manifest.files[path]=digest(text);
  }
  doc.files['manifest.json']=Buffer.from(JSON.stringify(manifest)).toString('base64');
  f.native.projects[0].document_sha256=digest(canonical(doc));
  await f.git.commit(f.fake.branch,{[path]:doc,'native/catalog.json':f.native});
}
it('ordinary read returns current native Contract and only required confirmed/scoped context',async()=>{
  const f=await fixture(),view=await f.store.read({project:f.target.project});
  expect(view.context.complete).toBe(true);
  const item=view.context.items[0] as any;
  expect(item).toMatchObject({authority:'native-confirmed-source',binding:false,native:{project:input.project,
    contract_revision:1,policy_revision:2,confirmation:{kind:'confirmed_contract'}}});
  expect(item.native.required_context.find((r:any)=>r.id==='synthetic-rule')).toMatchObject({authority:'confirmed',binding:true,
    binding_project:input.project,body:'Keep the original bytes',confirmation:{kind:'confirmed_decision'}});
  expect(item.native.required_context).toHaveLength(2);
  expect(item.native.required_context.find((r:any)=>r.kind==='reference')).toMatchObject({content_ref:'contract'});
  expect(item.native.required_context.find((r:any)=>r.kind==='reference')).not.toHaveProperty('body');
  for(const secret of ['capability_id','native-owner','owner.capability','user_uat','synthetic-only'])
    expect(JSON.stringify(view)).not.toContain(secret);
  expect(item.native).not.toHaveProperty('state');
});
it.each(['unconfigured','unshared','wrong-target','wrong-operation','disabled','frozen','stale-contract'])('%s native routing blocks without leaking the source',async failure=>{
  const f=await fixture();let store=f.store;
  if(failure==='unconfigured')store=new GitIntake(f.git,'owner','test-1');
  if(failure==='unshared')f.native.projects[0].shares=[];
  if(failure==='wrong-target')f.native.projects[0].shares[0].project='different';
  if(failure==='wrong-operation')f.native.projects[0].shares[0].native_operation='review';
  if(failure==='disabled')f.native.projects[0].enabled=false;
  if(failure==='frozen')f.native.projects[0].write_state='frozen';
  if(failure==='stale-contract')f.context.entries[0].source.contract_revision=2;
  await f.git.commit(f.fake.branch,{'native/catalog.json':f.native,'context.json':f.context});
  const context=(await store.read({project:f.target.project})).context;
  expect(context).toMatchObject({complete:false,categories:{global_rules:'required_unavailable'},items:[]});
  expect(JSON.stringify(context)).not.toContain('Keep the original bytes');
  expect(JSON.stringify(context)).not.toContain(input.project);
});
it.each(['missing-contract-receipt','wrong-contract-summary','wrong-decision-summary','suppressed','denied-reader','source-changed','missing-policy','required-unknown','required-artifact-denied'])('%s fails required native resolution',async failure=>{
  const f=await fixture();
  await editPackage(f,p=>{
    const state=p['project.json'],ctx=p['context.json'],audit=p['approvals.json'];
    const decision=ctx.contexts.find((r:any)=>r.id==='synthetic-rule');
    if(failure==='missing-contract-receipt')delete state.events.find((e:any)=>e.kind==='confirmation').payload.approval_receipt;
    if(failure==='wrong-contract-summary')state.contracts[0].goal='forged goal with otherwise valid transport hashes';
    if(failure==='wrong-decision-summary')decision.body='forged decision';
    if(failure==='suppressed')decision.status='suppressed';
    if(failure==='denied-reader')decision.readers=['other'];
    if(failure==='source-changed')ctx.sources.push({...ctx.sources.find((s:any)=>s.id===decision.source_id),revision:2});
    if(failure==='missing-policy')ctx.policies=ctx.policies.filter((p:any)=>p.operation!=='implement');
    if(failure==='required-unknown')decision.authority='unknown';
    if(failure==='required-artifact-denied') {
      const item=p['artifacts/manifest.json'].entries[0];item.principals=['other'];
      ctx.policies.find((p:any)=>p.operation==='implement'&&p.revision===2).payload.required_artifacts=[[item.artifact_id,item.revision]];
    }
  });
  expect((await f.store.read({project:f.target.project})).context).toMatchObject({complete:false,items:[]});
});
it.each(['independent','red-team'])('%s never returns mandatory personal context inside a native rule bundle',async mode=>{
  const f=await fixture();
  await editPackage(f,p=>{
    const ctx=p['context.json'],record=ctx.contexts.find((r:any)=>r.id==='synthetic-rule');
    record.kind='personal';record.body='hidden personal judgment';
  });
  const view=await f.store.read({project:f.target.project,mode});
  expect(view.context.complete).toBe(false);expect(JSON.stringify(view)).not.toContain('hidden personal judgment');
});
it('ordinary native updates preserve explicit shares and newer contract pins must be reviewed',async()=>{
  const f=await fixture(),before=await f.core.read({project:input.project});
  await f.core.write({...input,expected_revision:1,expected_document_sha256:before.document_sha256,request_id:'native-next'});
  expect((await f.store.read({project:f.target.project})).context.complete).toBe(true);
  const catalog=await f.git.read(f.fake.branch,'native/catalog.json') as any;
  expect(catalog.projects[0].shares).toEqual(f.native.projects[0].shares);
});

import {beforeAll,it,expect} from 'vitest';
import {execFileSync} from 'node:child_process';
import {resolve} from 'node:path';
import {GitCoreStore} from '../src/git-core';
import {GitIntake} from '../src/git-store';
import {GitHubBackend} from '../src/github-store';
import {FakeGitHub} from './git-fixture';
const root=resolve('..'),python=resolve(root,process.platform==='win32'?'.venv/Scripts/python.exe':'.venv/bin/python');
const run=(args:string[],input?:unknown)=>JSON.parse(execFileSync(python,['-m','tests.native_git_fixture',...args],
  {cwd:root,encoding:'utf8',...(input===undefined?{}:{input:JSON.stringify(input)})}));
let source:any,target:any,published:any,withdrawn:any,regressed:any,rechecked:any,typePublished:any,generalPublished:any;
const changedTargets:Record<string,any>={};
beforeAll(async()=>{
  source=run([]);target=run(['--target']);
  const fake=await new FakeGitHub().init(),git=new GitHubBackend(fake.config,fake.fetch),core=new GitCoreStore(git,'owner','test-1','synthetic','native-owner');
  await core.write(source);const initial=await core.read({project:source.project});
  published=run(['--advance','share'],initial);typePublished=run(['--advance','share-type'],initial);generalPublished=run(['--advance','share-general'],initial);
  await core.write(published);const current=await core.read({project:source.project});
  withdrawn=run(['--advance','withdraw'],current);regressed=run(['--advance','regress'],current);rechecked=run(['--advance','recheck'],current);
  await core.write(target);const recipient=await core.read({project:target.project});
  for(const action of ['target-constraints','target-forbidden','target-type'])changedTargets[action]=run(['--advance',action],recipient);
},30000);
async function fixture(publication=published) {
  const fake=await new FakeGitHub().init(),git=new GitHubBackend(fake.config,fake.fetch),core=new GitCoreStore(git,'owner','test-1','synthetic','native-owner');
  await core.write(source);await core.write(target);await core.write(publication);
  const store=new GitIntake(git,'owner','test-1','synthetic','native-owner');
  const created=await store.create({title:'Synthetic recipient',source:'synthetic',request_id:'target-intake'});
  const native=await git.read(fake.branch,'native/catalog.json') as any,catalog=await git.read(fake.branch,'nexus.json') as any;
  for(const entry of native.projects)entry.shares=[{project:created.project,operation:'implement',native_operation:entry.project===source.project?'review':'implement'}];
  catalog.context_revision=1;
  const context={schema:1,mode:'synthetic',owner:'owner',generation:'test-1',revision:1,
    profiles:[{project:created.project,operations:['implement'],required:{global_rules:[],personal:[],learning:['lesson'],relations:[]}}],
    entries:[{id:'lesson',category:'learning',projects:[created.project],operations:['implement'],status:'active',evidence:[],
      source:{native_learning:'shared-lesson:synthetic-principle',source_project:source.project,revision:1,
        target:{native_project:target.project,contract_revision:1,native_operation:'implement'}}}]};
  await git.commit(fake.branch,{'native/catalog.json':native,'nexus.json':catalog,'context.json':context});
  const read=(mode='delegate')=>store.read({project:created.project,operation:'implement',mode});
  return {fake,git,core,store,created,native,context,read};
}
it('delivers a native checked repaired-case candidate to the current matching target without private evidence text',async()=>{
  const f=await fixture(),view=await f.read();
  expect(view.context).toMatchObject({complete:true,items:[{authority:'candidate',binding:false,
    learning:{principle:'Preserve exact values in a roundtrip.',verification:'current-native-repaired-case-machine-and-contract',
      target:{project:target.project,contract_revision:1}}}]});
  const text=JSON.stringify(view.context);
  for(const hidden of ['Keep the exact fictional original.','capability_id','fixture-native','database','approved_at'])expect(text).not.toContain(hidden);
});
it.each(['share-type','share-general'])('preserves %s reuse as a nonbinding candidate with explicit destination',async scope=>{
  const f=await fixture(scope==='share-type'?typePublished:generalPublished);
  expect((await f.read()).context).toMatchObject({complete:true,items:[{learning:{binding:false,authority:'candidate',
    reuse_scope:scope==='share-type'?'task_type':'general'}}]});
});
it.each(['source-share','target-share','target-contract','candidate-revision','wrong-category'])('%s cannot silently broaden delivery',async failure=>{
  const f=await fixture();
  if(failure==='source-share')f.native.projects.find((p:any)=>p.project===source.project).shares=[];
  if(failure==='target-share')f.native.projects.find((p:any)=>p.project===target.project).shares=[];
  if(failure==='target-contract')f.context.entries[0].source.target.contract_revision=2;
  if(failure==='candidate-revision')f.context.entries[0].source.revision=2;
  if(failure==='wrong-category'){f.context.entries[0].category='global_rules';f.context.profiles[0].required.global_rules=['lesson'] as never;}
  await f.git.commit(f.fake.branch,{'native/catalog.json':f.native,'context.json':f.context});
  expect((await f.read()).context).toMatchObject({complete:false,items:[]});
});
it.each(['independent','red-team'])('%s excludes learning without loading native evidence',async mode=>{
  const f=await fixture(),before=f.fake.calls.length;
  const view=await f.read(mode);expect(view.context).toMatchObject({complete:true,items:[],categories:{learning:'excluded_by_mode'}});
  expect(f.fake.calls.slice(before).some(c=>c.path.includes('/native/'))).toBe(false);
});
it('withholds a withdrawn canonical candidate without falling back to its earlier revision',async()=>{
  const f=await fixture();await f.core.write(withdrawn);
  expect((await f.read()).context).toMatchObject({complete:false,items:[]});
});
it('withholds after later source regression while preserving the candidate and earlier proof',async()=>{
  const f=await fixture();await f.core.write(regressed);
  expect((await f.read()).context).toMatchObject({complete:false,items:[]});
});
it('does not silently substitute newer PASS evidence for the published proof',async()=>{
  const f=await fixture();await f.core.write(rechecked);
  expect((await f.read()).context).toMatchObject({complete:false,items:[]});
});
it.each(['target-constraints','target-forbidden','target-type'])('%s withholds an otherwise valid source candidate',async action=>{
  const f=await fixture(action==='target-type'?typePublished:published);
  await f.core.write(changedTargets[action]);
  if(action!=='target-type') {
    f.context.entries[0].source.target.contract_revision=2;
    await f.git.commit(f.fake.branch,{'context.json':f.context});
  }
  expect((await f.read()).context).toMatchObject({complete:false,items:[]});
});

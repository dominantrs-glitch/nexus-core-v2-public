import {it,expect} from 'vitest';
import {GitIntake} from '../src/git-store';
import {GitHubBackend} from '../src/github-store';
import {FakeGitHub} from './git-fixture';
import {validationSnapshot} from '../src/git-validation';

async function fixture(){
  const fake=await new FakeGitHub().init(),files=fake.objects.get(fake.branch)! as Record<string,any>;
  const operations=['resume','plan','implement','review'];
  const projects=Array.from({length:60},(_,i)=>({id:'p-'+i,title:'Synthetic '+i,revision:i===0?3:1,remote:true}));
  files['nexus.json']={schema:1,mode:'synthetic',owner:'owner',generation:'test-1',context_revision:1,projects};
  const note=(id:string,project:string,revision:number,body:string,kind='proposal',evidence='model_inference')=>({id,project,revision,body,kind,evidence,
    captured_kind:kind,source:'synthetic fixture',quote:evidence==='user_statement'?'synthetic owner rule':'',supersedes:null,created:new Date().toISOString()});
  for(const p of projects){
    files[`projects/${p.id}/manifest.json`]={...p,source:'fixture',overview:'n-overview',current:p.id==='p-0'?['n-rule','n-basis','n-overview']:['n-overview']};
    files[`projects/${p.id}/records/n-overview.json`]=note('n-overview',p.id,p.revision,'【画面用の概要】\nSynthetic summary');
  }
  files['projects/p-0/records/n-rule.json']=note('n-rule','p-0',1,'Old rule','explicit_choice','user_statement');
  files['projects/p-0/records/n-basis.json']=note('n-basis','p-0',2,'Use owner wide','explicit_choice','user_statement');
  files['context.json']={schema:2,mode:'synthetic',owner:'owner',generation:'test-1',revision:1,
    profiles:[{project:'*',operations,required:{global_rules:['rule'],personal:[],learning:[],relations:[]}}],
    entries:[{id:'rule',category:'global_rules',projects:['*'],operations,status:'active',scope:'owner',record_kind:'ai_rule',rule_key:'output',
      source:{project:'p-0',note:'n-rule',revision:1},reuse_basis:[{project:'p-0',note:'n-basis',revision:2}]}]};
  let requests=0,limited=false;
  const store=new GitIntake(new GitHubBackend(fake.config,async(input,init)=>{
    if(limited&&++requests>50)throw new Error('synthetic request budget exhausted');
    return fake.fetch(input,init);
  }),'owner','test-1');
  const input={rule:'rule',project:'p-0',note:'n-rule',expected_context_revision:1,expected_revision:3,
    body:'New output rule',quote:'Use new output rule',source:'synthetic owner request'};
  return {fake,files,store,input,start:()=>{limited=true;requests=0;},count:()=>requests};
}
it('validates 60 projects and all four operations within a 50-request upstream budget',async()=>{
  const f=await fixture(),plan=await f.store.previewRule(f.input);
  f.start();const result=await f.store.applyRule({...f.input,plan_digest:plan.plan_digest,request_id:'update'});
  expect(result.revision).toBe(4);expect(f.count()).toBeLessThan(30);
  expect(f.fake.calls.filter(c=>c.path==='/graphql').length).toBeGreaterThan(0);
  const current=f.fake.objects.get(f.fake.branch)! as Record<string,any>;
  expect(current['context.json'].profiles).toEqual(f.files['context.json'].profiles);
  expect(current['context.json'].entries[0].source.note).toBe(result.note);
});
it('batching still prevents a replacement from exceeding actual required-context delivery limits',async()=>{
  const f=await fixture();f.files['context.json'].profiles[0].max_bytes=1024;
  const input={...f.input,body:'x'.repeat(3000)},plan=await f.store.previewRule(input),before=f.fake.branch;
  await expect(f.store.applyRule({...input,plan_digest:plan.plan_digest,request_id:'too-long'})).rejects.toThrow('rule_delivery_check_failed');
  expect(f.fake.branch).toBe(before);
});
it('an immutable validation cache never shares mutable objects or authorizes another snapshot',async()=>{
  const f=await fixture(),git=new GitHubBackend(f.fake.config,f.fake.fetch),commit=await git.head();
  const view=validationSnapshot(git,commit);
  await view.readMany!(commit,['nexus.json']);
  const first:any=await view.read(commit,'nexus.json');first.projects.length=0;
  expect((await view.read(commit,'nexus.json') as any).projects).toHaveLength(60);
  await expect(view.read('f'.repeat(40),'nexus.json')).rejects.toThrow('snapshot_changed');
  await expect(view.commit(commit,{})).rejects.toThrow('preview_only');
  f.fake.privateRepo=false;
  await expect(git.head()).rejects.toThrow('private_repository_identity_mismatch');
});

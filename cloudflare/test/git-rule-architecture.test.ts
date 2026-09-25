import {it,expect} from 'vitest';
import {GitIntake,type GitBackend} from '../src/git-store';
import {contextManifest} from '../src/git-context';
const operations=['resume','plan','implement','review'];
function fixture(){
  const files:Record<string,any>={},seen:string[]=[];
  let serial=1;
  const git:GitBackend={head:async()=>serial.toString(16).padStart(40,'0'),read:async(_,p)=>{seen.push(p);return structuredClone(files[p]??null);},
    commit:async(_,changes)=>{Object.assign(files,structuredClone(changes));serial++;}};
  const refs:Record<string,any>={};
  const names=['owner-rule','scope-basis','goal','permission','instruction','foundation','brand','judgment','candidate'];
  names.forEach((name,i)=>{
    refs[name]={project:'sources',note:'n-'+name,revision:i+1};
    files[`projects/sources/records/n-${name}.json`]={id:'n-'+name,project:'sources',revision:i+1,kind:'explicit_choice',
      captured_kind:'explicit_choice',evidence:'user_statement',quote:'synthetic choice '+name,body:'synthetic '+name,
      source:'synthetic test',supersedes:null,created:'2026-09-24'};
  });
  const entries=names.filter(n=>n!=='scope-basis').map(name=>({id:name,category:'global_rules',projects:['core'],operations,
    scope:'project',status:'active',source:refs[name],record_kind:'project_context',task_types:[],triggers:[],evidence:[],reuse_basis:[]}));
  const get=(id:string)=>entries.find(e=>e.id===id)! as any;
  Object.assign(get('owner-rule'),{record_kind:'ai_rule',scope:'owner',projects:['*'],rule_key:'owner-output',reuse_basis:[refs['scope-basis']]});
  Object.assign(get('goal'),{record_kind:'goal'});
  Object.assign(get('permission'),{record_kind:'permission',scope:'task_type',task_types:['external_transfer']});
  Object.assign(get('instruction'),{record_kind:'one_shot_instruction'});
  Object.assign(get('foundation'),{record_kind:'foundation_rule'});
  Object.assign(get('candidate'),{record_kind:'ai_rule',rule_key:'candidate-rule',status:'candidate'});
  Object.assign(get('brand'),{category:'relations',scope:'task_type',task_types:['visual_design'],projects:['desktop']});
  Object.assign(get('judgment'),{record_kind:'decision',category:'personal',scope:'judgment',triggers:['priority','tradeoff','delegated_decision'],projects:['*']});
  const required={global_rules:['owner-rule'],personal:['judgment'],learning:[],relations:[]};
  const config:any={schema:2,owner:'owner',generation:'fixture',mode:'synthetic',revision:1,
    profiles:[{project:'*',operations,required},{project:'desktop',operations,required:{global_rules:[],personal:[],learning:[],relations:['brand']}}],entries};
  const projects=['core','desktop','brand','sources'].map(id=>({id,title:id,revision:id==='sources'?names.length:0,remote:true}));
  files['nexus.json']={schema:1,owner:'owner',generation:'fixture',mode:'synthetic',context_revision:1,projects};
  for(const p of projects)files[`projects/${p.id}/manifest.json`]={...p,source:'fixture',overview:null,current:p.id==='sources'?names.map(n=>'n-'+n):[]};
  files['context.json']=config;
  const read=(project='core',extra={})=>new GitIntake(git,'owner','fixture').read({project,detail:'context',task_types:[],decision_factors:['none'],...extra});
  return {files,git,seen,config,get,refs,read};
}
it('separates reusable rules from goals, one-off instructions, candidate rules and foundation checks',async()=>{
  const f=fixture(),view=await f.read();
  expect(view).toMatchObject({read_scope:'required_context_only',project_history_read:false,notes:[],next_offset:null});
  expect(view.context).toMatchObject({complete:true,metrics:{selected_rule_count:1,selected_context_count:2,missing_required_count:0}});
  expect(view.context).toMatchObject({behavior_verification:{rule_use:'not_observed',learning_effect:'not_evaluated_by_retrieval'}});
  expect(view.context.items.map((x:any)=>x.id)).toEqual(['owner-rule','goal']);
  expect(f.seen).not.toContain('projects/sources/records/n-permission.json');
  expect(f.seen).not.toContain('projects/sources/records/n-instruction.json');
  expect((view.context as any).selection).toContainEqual(expect.objectContaining({id:'candidate',reason:'excluded_candidate'}));
});
it.each(['core','desktop','brand'])('%s factual/priority reads and fresh clients route consistently',async project=>{
  const f=fixture(),first=await f.read(project),fresh=await f.read(project);
  expect(first.context).toEqual(fresh.context);
  expect(first.context.items.map((x:any)=>x.id)).not.toContain('judgment');
  const choice=await f.read(project,{decision_factors:['priority']});
  expect(choice.context.items.map((x:any)=>x.id)).toContain('judgment');
  expect((await f.read(project,{mode:'independent',decision_factors:['priority']})).context.items.map((x:any)=>x.id)).not.toContain('judgment');
});
it('a declared visual task, not merely a project relationship, activates related brand information',async()=>{
  const f=fixture();
  expect((await f.read('desktop')).context.items.map((x:any)=>x.id)).not.toContain('brand');
  expect((await f.read('desktop',{task_types:['visual_design']})).context.items.map((x:any)=>x.id)).toContain('brand');
  expect((await f.read('core',{task_types:['visual_design']})).context.items.map((x:any)=>x.id)).not.toContain('brand');
  expect((await f.read('core',{task_types:['external_transfer']})).context.items.find((x:any)=>x.id==='permission'))
    .toMatchObject({record_kind:'permission',binding:false,confirmation_state:'attributed_not_native_confirmation'});
});
it.each(['missing','revoked','scope_basis','wrong_authority'])('required %s does not become successful context',async failure=>{
  const f=fixture();
  if(failure==='missing')delete f.files['projects/sources/records/n-owner-rule.json'];
  if(failure==='revoked')f.get('owner-rule').status='revoked';
  if(failure==='scope_basis')f.files['projects/sources/manifest.json'].current=f.files['projects/sources/manifest.json'].current.filter((n:string)=>n!=='n-scope-basis');
  if(failure==='wrong_authority')Object.assign(f.files['projects/sources/records/n-owner-rule.json'],{kind:'proposal',captured_kind:'proposal',evidence:'model_inference',quote:''});
  const view=await f.read();
  expect(view.context).toMatchObject({complete:false,missing_required:['owner-rule'],metrics:{missing_required_count:1}});
  expect(view.context.items.map((x:any)=>x.id)).not.toContain('owner-rule');
});
it('scope and classification are mandatory in the new policy; a goal cannot be owner-wide',async()=>{
  const f=fixture();delete f.get('owner-rule').scope;
  expect(contextManifest.safeParse(f.config).success).toBe(false);
  f.get('owner-rule').scope='owner';delete f.get('owner-rule').record_kind;
  expect(contextManifest.safeParse(f.config).success).toBe(false);
  f.get('owner-rule').record_kind='goal';
  expect((await f.read()).context).toMatchObject({complete:false,status:'invalid_context_classification'});
});
it('same-purpose conflicting active rules stop instead of picking the newer write',async()=>{
  const f=fixture();f.config.entries.push({...f.get('owner-rule'),id:'conflict',source:f.refs['scope-basis']});
  expect((await f.read()).context).toMatchObject({complete:false,status:'context_rule_conflict',items:[]});
});
it('the focused route cannot claim an old snapshot or a complete history baseline',async()=>{
  const f=fixture();
  await expect(f.read('core',{snapshot:'1'.repeat(40)})).rejects.toThrow('context_requires_current_snapshot');
  await expect(f.read('core',{since_revision:0,known_snapshot:'1'.repeat(40)})).rejects.toThrow('changes_require_prior');
  const view=await f.read();expect(view.context.items.every((x:any)=>x.binding===false)).toBe(true);
});
it('editing a current AI rule preserves historical references and never expands delivery scope',async()=>{
  const f=fixture();
  f.config.entries.push({...f.get('owner-rule'),id:'old-reference',record_kind:'history',category:'personal'});
  const store=new GitIntake(f.git,'owner','fixture');
  const input={rule:'owner-rule',project:'sources',note:'n-owner-rule',expected_context_revision:1,expected_revision:9,
    body:'Revised output rule',quote:'Please revise my output rule',source:'synthetic owner correction'};
  const plan=await store.previewRule(input);
  const saved=await store.applyRule({...input,plan_digest:plan.plan_digest,request_id:'revise-rule'});
  expect(f.files['context.json'].entries.find((e:any)=>e.id==='old-reference').source.note).toBe('n-owner-rule');
  expect(f.files['context.json'].entries.find((e:any)=>e.id==='owner-rule').source.note).toBe(saved.note);
  expect((await f.read()).context.complete).toBe(true);
  await expect(store.save({project:'sources',kind:'correction',body:'remove basis',source:'synthetic',evidence:'user_statement',quote:'remove basis',
    expected_revision:10,request_id:'basis',supersedes:'n-scope-basis'})).rejects.toThrow('rule_update_requires_delivery_preview');
});
it('an ordinary overview automatically carries owner rules without judgment, brand or old permissions',async()=>{
  const f=fixture();
  for(const project of ['core','desktop','brand']){
    const v=await new GitIntake(f.git,'owner','fixture').read({project,detail:'overview'});
    expect(v.context).toMatchObject({complete:true,readiness:'status_only_not_implementation_context'});
    expect(v.context.items.map((x:any)=>x.id)).toEqual(['owner-rule']);
    expect(v.notes).toEqual([]);expect(v.next_offset).toBeNull();
  }
  expect(f.seen).not.toContain('projects/sources/records/n-brand.json');
  expect(f.seen).not.toContain('projects/sources/records/n-judgment.json');
});
it('overview cannot turn an unavailable mandatory owner rule into a successful bypass',async()=>{
  const f=fixture();delete f.files['projects/sources/records/n-owner-rule.json'];
  const v=await new GitIntake(f.git,'owner','fixture').read({project:'desktop',detail:'overview'});
  expect(v.context).toMatchObject({complete:false,missing_required:['owner-rule']});
});

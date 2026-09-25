import {expect,it} from 'vitest';
import {GitHubBackend} from '../src/github-store';
import {GitIntake,StoreError} from '../src/git-store';
import {gitMcp} from '../src/git-mcp';
import {FakeGitHub} from './git-fixture';

it.each([
  [403,{'x-ratelimit-remaining':'0','x-ratelimit-reset':String(Math.floor(Date.now()/1000)+180)},'API rate limit exceeded','canonical_rate_limited',true],
  [403,{'x-ratelimit-remaining':'4200','retry-after':'90'},'secondary rate limit','canonical_rate_limited',true],
  [403,{},'You have exceeded a secondary rate limit','canonical_rate_limited',true],
  [429,{},'upstream refused a private request','canonical_rate_limited',true],
  [403,{},'Resource not accessible by integration','canonical_access_denied',false],
  [401,{},'Bad credentials','canonical_authentication_denied',false],
] as const)('classifies upstream status %s safely (%s)',async(status,headers,message,code,retryable)=>{
  const fake=await new FakeGitHub().init();
  const backend=new GitHubBackend(fake.config,async()=>Response.json({message,secret:'never expose this'},{status,headers:headers as HeadersInit}));
  const response=await gitMcp({jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'find_projects',arguments:{query:''}}},new GitIntake(backend,'owner'));
  const data=await response.json() as any;
  const failure=JSON.parse(data.result.content[0].text);
  expect(failure).toMatchObject({status:'search_unavailable',reason:code,diagnostic:{http_status:status,retryable}});
  expect(JSON.stringify(data)).not.toContain('never expose this');
  expect(JSON.stringify(data)).not.toContain(message);
  if(retryable){expect(failure.diagnostic.retry_after_seconds).toBeGreaterThanOrEqual(1);expect(Date.parse(failure.diagnostic.retry_at)).toBeGreaterThan(Date.now());}
});

it('coalesces token acquisition and immutable reads while isolating mutable objects',async()=>{
  const fake=await new FakeGitHub().init(),backend=new GitHubBackend(fake.config,fake.fetch);
  const [a,b]=await Promise.all([backend.read(fake.branch,'nexus.json'),backend.read(fake.branch,'nexus.json')]) as any[];
  a.projects.push({id:'changed'});
  expect(b.projects).toEqual([]);
  expect((await backend.read(fake.branch,'nexus.json') as any).projects).toEqual([]);
  expect(fake.calls.filter(c=>c.path.includes('/access_tokens'))).toHaveLength(1);
  expect(fake.calls.filter(c=>c.path.includes('/contents/'))).toHaveLength(1);
  fake.privateRepo=false;
  await expect(backend.head()).rejects.toThrow('private_repository_identity_mismatch');
});

it('does not cache failed reads',async()=>{
  const fake=await new FakeGitHub().init(),backend=new GitHubBackend(fake.config,fake.fetch);
  fake.failReads=true;await expect(backend.read(fake.branch,'nexus.json')).rejects.toThrow('canonical_unavailable');
  fake.failReads=false;expect(await backend.read(fake.branch,'nexus.json')).toHaveProperty('owner','owner');
});

it('does not spend further requests on receipt recovery while rate limited',async()=>{
  const fake=await new FakeGitHub().init();let limited=false,afterLimit=0;
  const backend=new GitHubBackend(fake.config,async(input,init)=>{
    if(limited)afterLimit++;
    if(init?.method==='POST'&&String(input).endsWith('/git/trees')){
      limited=true;return Response.json({message:'secondary rate limit'},{status:403,headers:{'retry-after':'60'}});
    }
    return fake.fetch(input,init);
  });
  await expect(new GitIntake(backend,'owner').create({title:'test',source:'test',request_id:'limited'})).rejects.toThrow('canonical_rate_limited');
  expect(afterLimit).toBe(0);
});

it('reconstructs a rejected global commit against fresh data without losing unrelated changes',async()=>{
  const fake=await new FakeGitHub().init();let race=true;
  const backend=new GitHubBackend(fake.config,async(input,init)=>{
    if(race&&init?.method==='PATCH'){
      race=false;
      await new GitIntake(new GitHubBackend(fake.config,fake.fetch),'owner').create({title:'Other',source:'test',request_id:'other'});
    }
    return fake.fetch(input,init);
  });
  const store=new GitIntake(backend,'owner');
  const args={title:'Mine',source:'test',request_id:'mine'};
  const saved=await store.create(args);
  expect(await store.create(args)).toEqual(saved);
  expect((await store.list()).projects.map(p=>p.title).sort()).toEqual(['Mine','Other']);
});

it('does not reinterpret a same-project conflict as a safe retry',async()=>{
  const fake=await new FakeGitHub().init();
  const setup=new GitIntake(new GitHubBackend(fake.config,fake.fetch),'owner');
  const p=await setup.create({title:'test',source:'test',request_id:'c'});
  const args={project:p.project,kind:'proposal',body:'first',source:'test',evidence:'model_inference',quote:'',expected_revision:0,request_id:'first'};
  let race=true;
  const backend=new GitHubBackend(fake.config,async(input,init)=>{
    if(race&&init?.method==='PATCH'){race=false;await setup.save({...args,body:'Other writer',request_id:'other'});}
    return fake.fetch(input,init);
  });
  await expect(new GitIntake(backend,'owner').save(args)).rejects.toThrow('revision_conflict_reread');
  expect((await setup.read({project:p.project})).notes.map(n=>n.body)).toEqual(['Other writer']);
});

it('finishes immutable pages during writes and reports the new revision for a delta read',async()=>{
  const fake=await new FakeGitHub().init(),backend=new GitHubBackend(fake.config,fake.fetch),store=new GitIntake(backend,'owner');
  const p=await store.create({title:'paging',source:'test',request_id:'paging-create'});
  const args={project:p.project,kind:'proposal',source:'test',evidence:'model_inference',quote:''};
  for(let i=0;i<12;i++)await store.save({...args,body:'old '+i,expected_revision:i,request_id:'page-'+i});
  const first=await store.read({project:p.project});
  await store.save({...args,body:'new condition',expected_revision:12,request_id:'during-pages'});
  const last=await store.read({project:p.project,offset:first.next_offset!,snapshot:first.snapshot});
  expect(last.revision).toBe(12);expect(last.next_offset).toBeNull();
  expect(last.notes.map(n=>n.body)).toEqual(['old 10','old 11']);
  expect(last).toMatchObject({currentness:{changed:true,latest_revision:13}});
  const delta=await store.read({project:p.project,detail:'changes',known_snapshot:first.snapshot,since_revision:12});
  expect(delta.notes.map(n=>n.body)).toEqual(['new condition']);
  const catalog=await backend.read(fake.branch,'nexus.json') as any;
  catalog.projects[0].remote=false;
  await backend.commit(fake.branch,{'nexus.json':catalog});
  await expect(store.read({project:p.project,offset:10,snapshot:first.snapshot})).rejects.toThrow('project_unavailable');
});

it('does not reuse a page snapshot after its required routing policy changes',async()=>{
  const fake=await new FakeGitHub().init(),backend=new GitHubBackend(fake.config,fake.fetch),store=new GitIntake(backend,'owner');
  const p=await store.create({title:'policy',source:'test',request_id:'policy-create'});
  const before=await store.read({project:p.project});
  const catalog=await backend.read(fake.branch,'nexus.json') as any;catalog.context_revision=1;
  await backend.commit(fake.branch,{'nexus.json':catalog,'context.json':{schema:1}});
  await expect(store.read({project:p.project,snapshot:before.snapshot})).rejects.toThrow('required_context_changed_reread');
});

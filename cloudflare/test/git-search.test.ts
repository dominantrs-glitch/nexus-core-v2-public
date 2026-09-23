import {expect,it} from 'vitest';
import {GitIntake} from '../src/git-store';
import {GitHubBackend} from '../src/github-store';
import {gitMcp} from '../src/git-mcp';
import {FakeGitHub} from './git-fixture';

async function fixture(count=1) {
  const fake=await new FakeGitHub().init(),files=fake.objects.get(fake.branch)!;
  const project='p-search',current=Array.from({length:count},(_,i)=>'n-'+i);
  files['nexus.json']={schema:1,mode:'synthetic',owner:'owner',generation:'test-1',
    projects:[{id:project,title:'Name that does not match',revision:count,remote:true}]};
  files[`projects/${project}/manifest.json`]={id:project,title:'Name that does not match',revision:count,
    source:'synthetic',remote:true,current,overview:null};
  for(let i=0;i<count;i++)files[`projects/${project}/records/n-${i}.json`]={id:current[i],project,revision:i+1,
    kind:'goal',captured_kind:'goal',body:'shared responsibility 東京 '+i,source:'synthetic',quote:'',
    evidence:'model_inference',supersedes:null,created:'2026-09-23'};
  return {fake,files,project,store:new GitIntake(new GitHubBackend(fake.config,fake.fetch),'owner','test-1')};
}

it('discovers unlinked projects by current contents, not similar titles; keeps attribution',async()=>{
  const {store}=await fixture();
  const result=await store.search({query:'ＳＨＡＲＥＤ 東京',kinds:['goal']});
  expect(result.search).toMatchObject({status:'matches',complete:true,notes_scanned:1});
  expect(result.matches).toEqual([expect.objectContaining({kind:'goal',evidence:'model_inference',
    binding:false,excerpt_field:'body',read_offset:0})]);
  expect((await store.search({query:'Name that does not match'})).search.status).toBe('no_match');
  expect((await store.search({query:'東京',kinds:['acceptance']})).matches).toEqual([]);
  expect((await store.search({query:'n-0'})).matches).toHaveLength(1);
});

it('bounds scanning, binds continuation to query/filters/snapshot, distinguishes an empty page',async()=>{
  const {store,fake}=await fixture(23);
  const first=await store.search({query:'22'});
  expect(first.search).toMatchObject({status:'page_no_match',complete:false,exhausted:false,notes_scanned:20});
  expect(fake.calls.length).toBeLessThan(35);
  const args={query:'22',cursor:first.next_cursor!,snapshot:first.snapshot};
  const last=await store.search(args);
  expect(last.search).toMatchObject({status:'matches',complete:false,exhausted:true,notes_scanned:3});
  expect(last.matches).toEqual([expect.objectContaining({note:'n-22',read_offset:20})]);
  expect(last.next_cursor).toBeNull();
  await expect(store.search({...args,query:'other'})).rejects.toThrow('search_cursor_mismatch');
  await expect(store.search({...args,kinds:['goal']})).rejects.toThrow('search_cursor_mismatch');
  await expect(store.search({query:'22',cursor:first.next_cursor})).rejects.toThrow('search_cursor_mismatch');
  await store.save({project:'p-search',kind:'proposal',body:'update',source:'test',evidence:'model_inference',quote:'',expected_revision:23,request_id:'update'});
  await expect(store.search(args)).rejects.toThrow('snapshot_changed');
});

it('does not turn a later empty page into global no_match',async()=>{
  const {store}=await fixture(21);
  const first=await store.search({query:'n-1'});
  const last=await store.search({query:'n-1',cursor:first.next_cursor!,snapshot:first.snapshot});
  expect(first.matches.length).toBeGreaterThan(0);
  expect(last.search).toMatchObject({status:'page_no_match',complete:false,exhausted:true});
  const empty=await store.search({query:'absent'});
  const end=await store.search({query:'absent',cursor:empty.next_cursor!,snapshot:empty.snapshot});
  expect(end.search).toMatchObject({status:'page_no_match',complete:false,exhausted:true});
  expect(last.search.exhausted).toBe(true);
});

it('excludes superseded records and fails after visibility is revoked',async()=>{
  const {store,project}=await fixture();
  await store.save({project,kind:'correction',body:'replacement purpose',source:'test',evidence:'model_inference',quote:'',
    supersedes:'n-0',expected_revision:1,request_id:'correction'});
  expect((await store.search({query:'shared'})).search.status).toBe('no_match');
  expect((await store.search({query:'replacement'})).matches).toHaveLength(1);
  const denied=await fixture();(denied.files['nexus.json'] as any).projects[0].remote=false;
  expect((await denied.store.search({query:'shared'})).matches).toEqual([]);
  await expect(denied.store.search({query:'shared',project})).rejects.toThrow('project_unavailable');
});

it('never reports a partial scan as successful when a source is corrupt or unavailable',async()=>{
  const {store,fake,files}=await fixture(2);
  delete files['projects/p-search/records/n-1.json'];
  const call=()=>gitMcp({jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'search_project_notes',arguments:{query:'shared'}}},store);
  const corrupted=await (await call()).json() as any;
  expect(corrupted.result.isError).toBe(true);
  expect(JSON.parse(corrupted.result.content[0].text).status).toBe('search_unavailable');
  fake.failReads=true;
  const unavailable=await (await call()).json() as any;
  expect(JSON.parse(unavailable.result.content[0].text).status).toBe('search_unavailable');
});

it('rejects malformed cursors and empty queries',async()=>{
  const {store}=await fixture();
  await expect(store.search({query:'   '})).rejects.toThrow('invalid_search_query');
  await expect(store.search({query:'a b c d e f g h i'})).rejects.toThrow('invalid_search_query');
  await expect(store.search({query:'a',cursor:'garbage'})).rejects.toThrow('invalid_search_cursor');
});

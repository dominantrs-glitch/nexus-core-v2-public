import {expect,it} from 'vitest';
import {GitIntake} from '../src/git-store';
import {GitHubBackend} from '../src/github-store';
import {FakeGitHub} from './git-fixture';
import {gitMcp} from '../src/git-mcp';

async function fixture(media_type='image/png') {
  const git=await new FakeGitHub().init(),store=new GitIntake(new GitHubBackend(git.config,git.fetch),'owner','test-1');
  const p=await store.create({title:'Synthetic binary',source:'synthetic',request_id:'create'});
  const raw=media_type==='image/png'?'\x89PNG\r\n\x1a\nfixture-bytes':'%PDF-1.7\nsynthetic bytes';
  const bytes=Uint8Array.from(raw,c=>c.charCodeAt(0));
  const sha256=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(n=>n.toString(16).padStart(2,'0')).join('');
  const record={schema:1,project:p.project,id:'sample',revision:1,sha256,media_type,encoding:'base64',
    title:'Synthetic fixture',source:'synthetic:fixture-not-rendering-evidence',captured_at:'2026-09-23T00:00:00Z',
    bytes:bytes.length,content:btoa(raw),authority:'source-document-not-native-confirmation'};
  const path=`projects/${p.project}/binary/sample/1.json`,manifestPath=`projects/${p.project}/binary/manifest.json`;
  const manifest={schema:1,owner:'owner',mode:'synthetic',generation:'test-1',project:p.project,current:[{
    id:'sample',revision:1,sha256,remote:true,projects:[p.project],operations:['resume','implement']}]};
  Object.assign(git.objects.get(git.branch)!,{[path]:record,[manifestPath]:manifest});
  const request={project:p.project,detail:'content',original:'sample',revision:1,sha256};
  return {git,store,record,manifest,path,request};
}

it('lists metadata then emits exact image bytes without dumping base64 into text or structured metadata',async()=>{
  const {store,request,record}=await fixture();
  const list=await store.binary({project:request.project});
  expect(list).toMatchObject({status:'listed',originals:[{sha256:record.sha256,bytes:record.bytes}]});
  expect((list as any).originals[0]).not.toHaveProperty('content');
  const result=(await(await gitMcp({id:1,method:'tools/call',params:{name:'read_shared_artifact',
    arguments:{...request,snapshot:list.snapshot}}},store)).json() as any).result;
  expect(result.content[1]).toEqual({type:'image',mimeType:'image/png',data:record.content});
  expect(result.structuredContent.original).not.toHaveProperty('content');
  expect(result.content[0].text).not.toContain(record.content);
});

it('delivers PDFs as byte-exact resources without claiming client rendering',async()=>{
  const {store,request,record}=await fixture('application/pdf');
  const result=(await(await gitMcp({id:1,method:'tools/call',params:{name:'read_shared_artifact',arguments:request}},store)).json() as any).result;
  expect(result.content[1]).toMatchObject({type:'resource',resource:{mimeType:'application/pdf',blob:record.content}});
  expect(result.structuredContent.context_evaluated).toBe(false);
});

it('rechecks access, current revision and operation on every retrieval',async()=>{
  const {store,request,manifest}=await fixture();
  await expect(store.binary({...request,operation:'review'})).rejects.toThrow('binary_unavailable');
  await expect(store.binary({...request,revision:2})).rejects.toThrow('binary_unavailable');
  manifest.current[0].remote=false;
  await expect(store.binary(request)).rejects.toThrow('binary_unavailable');
  expect(await store.binary({project:request.project})).toMatchObject({status:'listed',originals:[]});
});

it('rejects corruption, media mismatch, oversized or replaced records and arbitrary locations',async()=>{
  const {store,request,record}=await fixture();
  record.media_type='application/pdf';
  await expect(store.binary(request)).rejects.toThrow('binary_unavailable');
  record.media_type='image/png';record.bytes++;
  await expect(store.binary(request)).rejects.toThrow('binary_unavailable');
  record.bytes=262145;
  await expect(store.binary(request)).rejects.toThrow('binary_unavailable');
  await expect(store.binary({...request,url:'https://unexpected.invalid'})).rejects.toThrow();
});

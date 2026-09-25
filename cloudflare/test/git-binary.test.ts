import {expect,it} from 'vitest';
import {GitIntake} from '../src/git-store';
import {GitHubBackend} from '../src/github-store';
import {FakeGitHub} from './git-fixture';
import {gitMcp} from '../src/git-mcp';
import {CHUNK_BYTES,chunkPath,readBinary,binaryInput,MAX_BINARY_BYTES} from '../src/git-binary';
import {auditFiles} from '../src/git-audit';

async function fixture(media_type='image/png') {
  const git=await new FakeGitHub().init(),store=new GitIntake(new GitHubBackend(git.config,git.fetch),'owner','test-1');
  const p=await store.create({title:'Synthetic binary',source:'synthetic',request_id:'create'});
  const raw=media_type==='image/png'?'\x89PNG\r\n\x1a\nfixture-bytes':media_type==='image/webp'?'RIFF0000WEBPfixture':'%PDF-1.7\nsynthetic bytes';
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

it('attaches explicitly shared WebP bytes and rejects a mismatched container signature',async()=>{
  const {store,request,record}=await fixture('image/webp');
  const result=(await(await gitMcp({id:1,method:'tools/call',params:{name:'read_shared_artifact',arguments:request}},store)).json() as any).result;
  expect(result.content[1]).toEqual({type:'image',mimeType:'image/webp',data:record.content});
  record.media_type='application/pdf';
  await expect(store.binary(request)).rejects.toThrow('binary_unavailable');
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

async function chunkedFixture(){
  const f=await fixture(),files=f.git.objects.get(f.git.branch)!;
  // Several nonidentical chunks, beyond GitHub Contents' 1 MiB base64 limit.
  const raw='\x89PNG\r\n\x1a\n'+'a'.repeat(CHUNK_BYTES)+'b'.repeat(CHUNK_BYTES)+'c'.repeat(CHUNK_BYTES*4);
  const sha=async(raw:string)=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',Uint8Array.from(raw,c=>c.charCodeAt(0))))]
    .map(n=>n.toString(16).padStart(2,'0')).join('');
  const chunks=[];
  for(let i=0;i<raw.length;i+=CHUNK_BYTES){
    const part=raw.slice(i,i+CHUNK_BYTES),sha256=await sha(part);
    chunks.push({sha256,bytes:part.length});
    files[chunkPath(f.request.project,sha256)]={schema:1,encoding:'base64',sha256,bytes:part.length,content:btoa(part)};
  }
  const {content,...meta}=f.record,record={...meta,schema:2,encoding:'chunked-base64',bytes:raw.length,sha256:await sha(raw),chunks};
  files[f.path]=record;f.manifest.current[0].sha256=record.sha256;
  return {...f,files,record,raw,request:{...f.request,sha256:record.sha256}};
}

it('lists large metadata without chunk fetches and retrieves exact bytes with bounded batching',async()=>{
  const f=await chunkedFixture(),calls:string[]=[],batches:string[][]=[];
  const read=async(path:string)=>{calls.push(path);return f.files[path]??null;};
  const list=await readBinary(binaryInput.parse({project:f.request.project}),{owner:'owner',generation:'test-1',mode:'synthetic'},read);
  expect(calls).toHaveLength(2);
  expect(list).toMatchObject({originals:[{content_verification:'not_requested',bytes:f.raw.length}]});
  const full:any=await readBinary(binaryInput.parse(f.request),{owner:'owner',generation:'test-1',mode:'synthetic'},read,
    {prefetch:async paths=>{batches.push(paths);}});
  expect(batches.every(b=>b.length<=12)).toBe(true);
  expect(atob(full.original.content)).toBe(f.raw);
  const result=(await(await gitMcp({id:1,method:'tools/call',params:{name:'read_shared_artifact',arguments:f.request}},f.store)).json() as any).result;
  expect(result.content[1].data).toBe(btoa(f.raw));
  expect(result.content[0].text).not.toContain(btoa(f.raw));
  expect(result.structuredContent.original).not.toHaveProperty('binary_payload');
  expect(result.structuredContent.original).not.toHaveProperty('chunks');
  const audit=await auditFiles(f.files,f.git.branch);
  expect(audit.issues).toEqual([]);
  f.files[chunkPath(f.request.project,'0'.repeat(64))]={schema:1};
  expect((await auditFiles(f.files,f.git.branch)).issues).toContainEqual(expect.objectContaining({type:'unindexed_original_review'}));
});

it('a pinned snapshot cannot retrieve an original whose current share was revoked',async()=>{
  const f=await chunkedFixture(),old=f.git.branch;
  const next=structuredClone(f.files);
  (next[`projects/${f.request.project}/binary/manifest.json`] as any).current[0].remote=false;
  const commit='b'.repeat(40);f.git.objects.set(commit,next);f.git.branch=commit;
  await expect(f.store.binary({...f.request,snapshot:old})).rejects.toThrow('snapshot_changed_restart_read');
});

it('fails closed for missing, substituted, reordered, oversized or revoked chunked originals',async()=>{
  for(const problem of ['missing','substituted','reordered','oversized','revoked']){
    const f=await chunkedFixture(),first=chunkPath(f.request.project,f.record.chunks[0].sha256);
    if(problem==='missing')delete f.files[first];
    if(problem==='substituted')(f.files[first] as any).content=btoa('invalid');
    if(problem==='reordered')[f.record.chunks[0],f.record.chunks[1]]=[f.record.chunks[1],f.record.chunks[0]];
    if(problem==='oversized')f.record.bytes=MAX_BINARY_BYTES+1;
    if(problem==='revoked')f.manifest.current[0].remote=false;
    await expect(f.store.binary(f.request)).rejects.toThrow('binary_unavailable');
  }
});

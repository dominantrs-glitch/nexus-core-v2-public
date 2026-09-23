const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const crypto = require('node:crypto');
const source = fs.readFileSync('nexus/ui/handoff.js', 'utf8');
const bytes = Buffer.from('synthetic exact original');
const manifest = {artifact_id:'small-png', revision:1, project:'synthetic-probe',
  mime_type:'image/png', size:bytes.length, sha256:crypto.createHash('sha256').update(bytes).digest('hex')};
const result = {_meta:{original_base64:bytes.toString('base64')}, structuredContent:manifest};
async function until(fn) {
  for(let i=0;i<100;i++){ if(fn())return; await new Promise(r=>setTimeout(r,5)); }
  throw new Error('condition timed out');
}
function host(input, {noHelper=false, saved=null, downloadUrl='https://unapproved.example/file', fetcher=null}={}) {
  const nodes = Object.fromEntries(['status','preview','download','local','upload','inspect','recheck','diagnostics'].map(id=>[id,{disabled:true,hidden:true}]));
  const handlers={}; const uploads=[]; const states=[]; const messages=[];
  const parent={postMessage(m){messages.push(m);if(m.id)queueMicrotask(()=>handlers.message({source:parent,data:{jsonrpc:'2.0',id:m.id,result:{}}}));}};
  const api={toolResponseMetadata:{mcp_tool_result:input},widgetState:saved,
    uploadFile:async file=>{uploads.push(Buffer.from(await file.arrayBuffer()));return {fileId:'sediment://file_test123'};},
    setWidgetState:s=>{api.widgetState=s;states.push(s);handlers['openai:set_globals']();},
    getFileDownloadUrl:async()=>({downloadUrl})};
  if(noHelper)delete api.uploadFile;
  const window={parent,openai:api,addEventListener:(n,f)=>handlers[n]=f};
  let fetches=0;
  vm.runInNewContext(source,{window,document:{getElementById:id=>nodes[id]},URL,Blob,File,Uint8Array,
    crypto:crypto.webcrypto,atob,setTimeout,clearTimeout,AbortSignal,
    fetch:async()=>{fetches++;if(fetcher)return fetcher();throw new Error('must not fetch');}});
  return {nodes,uploads,states,messages,get fetches(){return fetches;}};
}
test('actual received bytes reach upload unchanged; duplicate host globals preserve state',async()=>{
  const h=host(result);await until(()=>!h.nodes.upload.disabled);
  h.nodes.upload.onclick();await until(()=>h.states.length===1&&!h.nodes.inspect.disabled);
  assert.deepEqual(h.uploads,[bytes]);assert.equal(h.states[0].receiver_widget_verified,undefined);
  assert.equal(h.states[0].modelContent.receiver_widget_verified,true);
  assert.equal(h.states[0].modelContent.host_download_verified,false);
  assert.equal(h.states[0].modelContent.sandbox_original_verified,false);
  assert.deepEqual(Array.from(h.states[0].imageIds),['sediment://file_test123']);
  assert.equal(h.fetches,0);
  await h.nodes.inspect.onclick();
  assert.equal(h.messages.filter(x=>x.method==='ui/message').length,1);
  assert.ok(!JSON.stringify(h.states).includes(bytes.toString('base64')));
});
test('unknown URL reports origin only without disclosing signed path/query',async()=>{
  const h=host(result,{downloadUrl:'https://other.example/secret-path?token=secret'});
  await until(()=>!h.nodes.upload.disabled);h.nodes.upload.onclick();await until(()=>h.states.length===1);
  const d=JSON.parse(h.nodes.diagnostics.textContent);
  assert.equal(d.download,'origin_not_allowlisted');assert.equal(d.origin,'https://other.example');
  assert.ok(!h.nodes.diagnostics.textContent.includes('secret'));assert.equal(h.fetches,0);
});
test('host download verifies actual bytes separately from sandbox',async()=>{
  const h=host(result,{downloadUrl:'https://oaisdmntprcentralus.blob.core.windows.net/signed',fetcher:()=>new Response(bytes)});
  await until(()=>!h.nodes.upload.disabled);h.nodes.upload.onclick();await until(()=>h.states.length===1);
  assert.equal(h.states[0].modelContent.host_download_verified,true);
  assert.equal(h.states[0].modelContent.sandbox_original_verified,false);
});
test('lookalike Azure origin is rejected before any request',async()=>{
  const h=host(result,{downloadUrl:'https://oaisdmntprcentralus.blob.core.windows.net.evil.example/signed'});
  await until(()=>!h.nodes.upload.disabled);h.nodes.upload.onclick();await until(()=>h.states.length===1);
  assert.equal(h.fetches,0);assert.equal(JSON.parse(h.nodes.diagnostics.textContent).download,'origin_not_allowlisted');
});
test('mismatched host bytes clear model image references and disable inspection',async()=>{
  const h=host(result,{downloadUrl:'https://files.oaiusercontent.com/signed',fetcher:()=>new Response('wrong')});
  await until(()=>!h.nodes.upload.disabled);h.nodes.upload.onclick();await until(()=>h.nodes.status.textContent?.includes('未達'));
  assert.equal(h.nodes.inspect.disabled,true);assert.equal(h.states.length,1);
  assert.deepEqual(Array.from(h.states[0].imageIds),[]);
  assert.equal(h.states[0].modelContent.status,'integrity_failed');
});
test('reload restores only matching artifact identity and never promotes saved hash gate',async()=>{
  const saved={privateContent:{fileId:'sediment://file_saved',artifact_id:'small-png',revision:1,sha256:manifest.sha256},imageIds:['sediment://file_saved']};
  const h=host(result,{saved});await until(()=>!h.nodes.recheck.disabled);
  assert.equal(JSON.parse(h.nodes.diagnostics.textContent).widget_state,'restored_from_host');
  assert.equal(h.uploads.length,0);
  const wrong=host(result,{saved:{...saved,privateContent:{...saved.privateContent,sha256:'f'.repeat(64)}}});
  await until(()=>!wrong.nodes.upload.disabled);assert.equal(wrong.nodes.recheck.disabled,true);
});
test('tampered transport disables upload and preview',async()=>{
  const h=host({...result,_meta:{original_base64:Buffer.from('tampered').toString('base64')}});
  await until(()=>h.nodes.status.textContent?.includes('失敗'));
  assert.equal(h.nodes.upload.disabled,true);assert.equal(h.nodes.preview.hidden,true);assert.equal(h.uploads.length,0);
});
test('different user-selected file is not sent to host',async()=>{
  const h=host(result);await until(()=>!h.nodes.local.disabled);
  h.nodes.local.onchange({target:{files:[new File(['wrong'],'wrong.png')],value:'wrong'}});
  await until(()=>h.nodes.status.textContent?.includes('未達'));
  assert.equal(h.uploads.length,0);
});
test('missing host capability retains download fallback without claiming file registration',async()=>{
  const h=host(result,{noHelper:true});await until(()=>h.nodes.status.textContent?.includes('検出できません'));
  assert.equal(h.nodes.download.hidden,false);assert.equal(h.nodes.upload.disabled,true);assert.equal(h.states.length,0);
});

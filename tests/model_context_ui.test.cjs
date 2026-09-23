const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),crypto=require('node:crypto');
const source=fs.readFileSync('nexus/ui/model-context.js','utf8'),bytes=Buffer.from('synthetic bytes');
const input={structuredContent:{artifact_id:'small-png',revision:1,project:'synthetic-probe',mime_type:'image/png',size:bytes.length,sha256:crypto.createHash('sha256').update(bytes).digest('hex')},_meta:{original_base64:bytes.toString('base64')}};
async function until(f){for(let i=0;i<100;i++){if(f())return;await new Promise(r=>setTimeout(r,5));}throw Error('timeout');}
function host({caps={text:{},image:{}},reject=false,tamper=false,hostName="test",malformed=false}={}){
 const nodes=Object.fromEntries(['status','preview','text','nonce','answer','compare','image','diagnostics'].map(k=>[k,{disabled:true,value:''}])),handlers={},messages=[];
 const parent={postMessage(m){
  messages.push(m);
  if(m.id)queueMicrotask(()=>{
   const response=reject&&m.method!=='ui/initialize' ? {error:{code:-32601}} : {result:m.method==='ui/initialize'?{hostCapabilities:{updateModelContext:caps},protocolVersion:'2026-01-26',hostInfo:{name:hostName}}:(malformed?null:{})};
   handlers.message({source:parent,data:{jsonrpc:'2.0',id:m.id,...response}});
  });
 }};
 const window={parent,addEventListener:(n,f)=>handlers[n]=f,openai:{toolResponseMetadata:{mcp_tool_result:tamper?{...input,_meta:{original_base64:'YmFk'}}:input}}};
 vm.runInNewContext(source,{window,document:{getElementById:id=>nodes[id]},crypto:crypto.webcrypto,Uint8Array,atob,setTimeout,clearTimeout});
 return {nodes,messages,d:()=>JSON.parse(nodes.diagnostics.textContent)};
}
test('nonce delivery and explicit match gate precede exact image bytes; no automatic success',async()=>{
 const h=host();await until(()=>!h.nodes.text.disabled);assert.equal(h.nodes.image.disabled,true);
 await h.nodes.text.onclick();assert.equal(h.d().text_ack,'accepted');assert.equal(h.d().text_match,false);
 h.nodes.answer.value='wrong';h.nodes.compare.onclick();assert.equal(h.nodes.image.disabled,true);
 h.nodes.answer.value=h.nodes.nonce.textContent;h.nodes.compare.onclick();await h.nodes.image.onclick();
 const calls=h.messages.filter(m=>m.method==='ui/update-model-context');assert.equal(calls.length,2);
 assert.deepEqual(Buffer.from(calls[1].params.content[0].data,'base64'),bytes);
 assert.equal(h.d().sandbox_verified,false);assert.equal(h.nodes.image.disabled,true);
 assert.ok(!h.nodes.diagnostics.textContent.includes(h.nodes.nonce.textContent));
});
test('unadvertised modality remains disabled and sends no context',async()=>{
 const h=host({caps:{}});await until(()=>h.d().text_support==='not_advertised');
 await h.nodes.text.onclick();assert.equal(h.messages.filter(m=>m.method==='ui/update-model-context').length,0);
});
test('rejected context never unlocks image and tampered original cannot send',async()=>{
 const h=host({reject:true});await until(()=>!h.nodes.text.disabled);await h.nodes.text.onclick();
 assert.equal(h.d().text_ack,'method_not_found');assert.equal(h.nodes.image.disabled,true);
 const bad=host({tamper:true});await until(()=>bad.nodes.status.textContent?.includes('不一致'));
 assert.equal(bad.nodes.text.disabled,true);
});

test('unadvertised ChatGPT permits one probe but invalid declarations do not',async()=>{
 const h=host({caps:{},hostName:'chatgpt'});await until(()=>!h.nodes.text.disabled);await h.nodes.text.onclick();await h.nodes.text.onclick();
 assert.equal(h.messages.filter(m=>m.method==='ui/update-model-context').length,1);assert.equal(h.d().text_support,'not_advertised');
 const bad=host({caps:{text:false},hostName:'chatgpt'});await until(()=>bad.d().text_support==='invalid');assert.equal(bad.nodes.text.disabled,true);
});
test('malformed ACK is not success',async()=>{
 const h=host({malformed:true});await until(()=>!h.nodes.text.disabled);await h.nodes.text.onclick();assert.equal(h.d().text_ack,'protocol_error');assert.equal(h.nodes.image.disabled,true);
});

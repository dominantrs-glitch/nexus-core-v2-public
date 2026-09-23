/* Synthetic-only, explicit-action model context probe. No fetch or file registration. */
(() => {
  'use strict';
  const el = id => document.getElementById(id), pending = new Map();
  let seq = 1, ready = false, busy = false, nonce = null, verified = null, signature = null, epoch = 0;
  const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const failure = error => error.message === 'timeout' ? 'timeout' : error.message === 'protocol_error' ? 'protocol_error' : error.code === -32601 ? 'method_not_found' : error.code === -32602 ? 'invalid_params' : 'rejected';
  const canProbe = modality => d[modality+'_support'] === 'advertised' || (d.host === 'chatgpt' && d[modality+'_support'] === 'not_advertised');
  const d = {error_code:null, declaration:'unknown', protocol:null, host:null, text_support:'unknown', image_support:'unknown',
    receiver_verified:false, text_ack:'not_tested', text_match:false, image_ack:'not_tested', sandbox_verified:false};
  function render() {
    el('diagnostics').textContent = JSON.stringify(d,null,2);
    el('text').disabled = busy || !ready || !verified || !canProbe('text') || nonce !== null;
    el('compare').disabled = busy || d.text_ack !== 'accepted' || !nonce;
    el('image').disabled = busy || !verified || !d.text_match || !canProbe('image') || d.image_ack !== 'not_tested';
  }
  function request(method, params) {
    const id = seq++;
    return new Promise((resolve,reject) => {
      const timer=setTimeout(()=>{pending.delete(id);reject(new Error('timeout'));},15000);
      pending.set(id,{resolve,reject,timer});
      window.parent.postMessage({jsonrpc:'2.0',id,method,params},'*');
    });
  }
  const hash = async bytes => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),x=>x.toString(16).padStart(2,'0')).join('');
  async function accept(result) {
    if (!result?._meta?.original_base64) return;
    const sig=JSON.stringify([result.structuredContent,result._meta.original_base64]);
    if (sig===signature) return;
    signature=sig; const ticket=++epoch;
    verified=null; nonce=null; d.receiver_verified=false; d.text_match=false;
    d.text_ack='not_tested'; d.image_ack='not_tested'; d.error_code=null; el('nonce').textContent=''; el('preview').hidden=true; render();
    try {
      const m=result.structuredContent, b64=result._meta.original_base64;
      if(result.isError || m?.artifact_id!=='small-png' || m.revision!==1 || m.project!=='synthetic-probe' ||
        m.mime_type!=='image/png' || !Number.isInteger(m.size) || m.size<1 || m.size>131072 ||
        typeof b64!=='string' || b64.length>174764 || !/^[A-Za-z0-9+/]*={0,2}$/.test(b64) || !/^[a-f0-9]{64}$/.test(m.sha256)) throw Error();
      const bytes=Uint8Array.from(atob(b64),c=>c.charCodeAt(0));
      if(bytes.length!==m.size || await hash(bytes)!==m.sha256) throw Error();
      if(ticket!==epoch)return;
      verified={bytes,b64,sha256:m.sha256}; d.receiver_verified=true;
      el('preview').src='data:image/png;base64,'+b64; el('preview').hidden=false;
      el('status').textContent='原本受信のbyte・SHA-256一致。モデル到達は未確認。';
    } catch (_) { if(ticket===epoch)el('status').textContent='原本不一致。送信停止。'; }
    render();
  }
  el('text').onclick=async()=>{
    if(el('text').disabled)return;
    nonce=Array.from(crypto.getRandomValues(new Uint8Array(8)),x=>x.toString(16).padStart(2,'0')).join('');
    el('nonce').textContent=nonce; busy=true; const ticket=epoch; render();
    try {
      await request('ui/update-model-context',{content:[{type:'text',text:'Nexus synthetic probe code: '+nonce}]});
      if(ticket===epoch)d.text_ack='accepted';
    } catch(e) { if(ticket===epoch){ d.text_ack=failure(e); d.error_code=e.code ?? null; } }
    finally{busy=false;render();}
  };
  el('compare').onclick=()=>{
    if(el('compare').disabled)return;
    d.text_match=el('answer').value.trim()===nonce; render();
  };
  el('image').onclick=async()=>{
    if(el('image').disabled)return;
    busy=true; const ticket=epoch, original=verified; render();
    try {
      if(await hash(original.bytes)!==original.sha256)throw Error('integrity');
      // Reuse the hash-verified transport encoding. No model transcription or recompression.
      await request('ui/update-model-context',{content:[{type:'image',mimeType:'image/png',data:original.b64}]});
      if(ticket===epoch)d.image_ack='accepted';
    }catch(e){if(ticket===epoch){ d.image_ack=failure(e); d.error_code=e.code ?? null; }}
    finally{busy=false;render();}
  };
  window.addEventListener('message',event=>{
    if(event.source!==window.parent || event.data?.jsonrpc!=='2.0')return;
    const m=event.data,p=pending.get(m.id);
    if(p){pending.delete(m.id);clearTimeout(p.timer);if(m.error){const error=Error('rejected');if(Number.isInteger(m.error.code))error.code=m.error.code;p.reject(error);}else if(!isObject(m.result))p.reject(Error('protocol_error'));else p.resolve(m.result);}
    else if(m.method==='ui/notifications/tool-result')void accept(m.params);
  });
  function legacy(){const meta=window.openai?.toolResponseMetadata;const r=meta?.mcp_tool_result||meta?.call_tool_result;
    if(r)void accept(r);else if(meta?.original_base64)void accept({structuredContent:window.openai.toolOutput,_meta:meta});}
  window.addEventListener('openai:set_globals',legacy);
  request('ui/initialize',{appInfo:{name:'nexus-model-context-probe',version:'0.1.0'},appCapabilities:{},protocolVersion:'2026-01-26'}).then(r=>{
    ready=true; d.protocol=typeof r?.protocolVersion==='string'?r.protocolVersion.slice(0,64):null;
    d.host=typeof r?.hostInfo?.name==='string'?r.hostInfo.name.slice(0,80):null;
    const c=r?.hostCapabilities?.updateModelContext;
    d.declaration=c === undefined ? 'absent' : isObject(c) ? 'object' : 'invalid';
    for(const kind of ['text','image'])d[kind+'_support']=c === undefined ? 'not_advertised' : !isObject(c) ? 'invalid' : c[kind] === undefined ? 'not_advertised' : isObject(c[kind]) ? 'advertised' : 'invalid';
    window.parent.postMessage({jsonrpc:'2.0',method:'ui/notifications/initialized',params:{}},'*');render();legacy();
  }).catch(()=>{el('status').textContent='初期化未達。送信停止。';render();});
  legacy();render();
})();

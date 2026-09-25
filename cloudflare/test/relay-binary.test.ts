import {expect,it} from 'vitest';
import {Buffer} from 'node:buffer';
import {createHash} from 'node:crypto';
import {binaryInput,CHUNK_BYTES,MAX_BINARY_BYTES,chunkPath,readBinary} from '../src/git-binary';
import {binaryResponse} from '../src/git-binary-response';

it('workerd verifies and streams a 64 MiB original without a whole base64/JSON response buffer',async()=>{
  const files:Record<string,unknown>={},chunks: {sha256:string;bytes:number}[]=[],hash=createHash('sha256');
  for(let offset=0;offset<MAX_BINARY_BYTES;offset+=CHUNK_BYTES){
    const bytes=Buffer.alloc(Math.min(CHUNK_BYTES,MAX_BINARY_BYTES-offset),65);
    if(offset===0)bytes.write('%PDF-1.7\n');
    hash.update(bytes);const sha256=createHash('sha256').update(bytes).digest('hex');
    chunks.push({sha256,bytes:bytes.length});
    files[chunkPath('target',sha256)]={schema:1,encoding:'base64',sha256,bytes:bytes.length,content:bytes.toString('base64')};
  }
  const sha256=hash.digest('hex');
  files['projects/target/binary/manifest.json']={schema:1,owner:'owner',generation:'test',mode:'synthetic',project:'target',
    current:[{id:'large',revision:1,sha256,remote:true,projects:['target'],operations:['resume']}]};
  files['projects/target/binary/large/1.json']={schema:2,project:'target',id:'large',revision:1,sha256,
    media_type:'application/pdf',encoding:'chunked-base64',bytes:MAX_BINARY_BYTES,chunks,title:'架空の転送試験',source:'synthetic',
    captured_at:'2026-09-25T00:00:00Z',authority:'source-document-not-native-confirmation'};
  const result=await readBinary(binaryInput.parse({project:'target',detail:'content',original:'large',revision:1,sha256}),
    {owner:'owner',generation:'test',mode:'synthetic'},async p=>files[p]??null,{stream:true});
  const response=binaryResponse('large',result),reader=response.body!.getReader();let bytes=0,max=0;
  for(;;){const p=await reader.read();if(p.done)break;bytes+=p.value.length;max=Math.max(max,p.value.length);}
  expect(bytes).toBe(Number(response.headers.get('Content-Length')));
  expect(bytes).toBeGreaterThan(MAX_BINARY_BYTES*4/3);
  expect(max).toBeLessThan(300000);
},30000);

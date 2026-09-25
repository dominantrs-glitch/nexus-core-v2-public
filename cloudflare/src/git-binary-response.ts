/** Stream a verified attachment without constructing another large JSON/base64 copy. */
import {Buffer} from 'node:buffer';

export function binaryResponse(id:string|number,result:any) {
  const {content,binary_payload,chunks,...original}=result.original;
  const metadata={...result,original},encoded=JSON.stringify(metadata);
  const image=original.media_type.startsWith('image/');
  const start=image?`{"type":"image","mimeType":${JSON.stringify(original.media_type)},"data":"`:
    `{"type":"resource","resource":{"uri":${JSON.stringify(`nexus://shared/${original.project}/${original.id}/${original.revision}/${original.sha256}`)},"mimeType":${JSON.stringify(original.media_type)},"blob":"`;
  const prefix=`{"jsonrpc":"2.0","id":${JSON.stringify(id)},"result":{"content":[${JSON.stringify({type:'text',text:encoded})},${start}`;
  const suffix=(image?'"}':'"}}')+`],"structuredContent":${encoded}}}`;
  // Each nonfinal chunk has a length divisible by 3: concatenation is exact base64.
  const buffers:Buffer[]=binary_payload??[Buffer.from(content,'base64')];
  let offset=-1;
  const encoder=new TextEncoder();
  const length=encoder.encode(prefix).length+encoder.encode(suffix).length+
    buffers.reduce((sum,b)=>sum+Math.ceil(b.length/3)*4,0);
  return new Response(new ReadableStream<Uint8Array>({
    pull(controller){
      if(offset<0){offset=0;controller.enqueue(encoder.encode(prefix));return;}
      if(offset<buffers.length){
        const data=buffers[offset];buffers[offset++]=undefined!;
        controller.enqueue(encoder.encode(Buffer.from(data).toString('base64')));return;
      }
      controller.enqueue(encoder.encode(suffix));controller.close();
    },
    cancel(){buffers.length=0;}
  }),{headers:{'Content-Type':'application/json','Cache-Control':'no-store','Content-Length':String(length)}});
}

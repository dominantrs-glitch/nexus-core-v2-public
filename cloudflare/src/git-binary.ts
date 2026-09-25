/** Explicitly shared originals. No filesystem/URL fetch or model write tool. */
import {z} from 'zod';
import {Buffer} from 'node:buffer';
import {createHash} from 'node:crypto';
const id=z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/),revision=z.number().int().positive().safe();
const digest=z.string().regex(/^[a-f0-9]{64}$/),operation=z.enum(['resume','plan','implement','review']);
const bounded=(max:number)=>z.string().trim().min(1).max(max);
const entry=z.object({id,revision,sha256:digest,remote:z.boolean(),projects:z.array(id).min(1).max(100),
  operations:z.array(operation).min(1).max(4)}).strict();
export const binaryManifest=z.object({schema:z.literal(1),owner:bounded(200),generation:id,
  mode:z.enum(['synthetic','draft-intake']),project:id,current:z.array(entry).max(30)}).strict();
export const MAX_BINARY_BYTES=64*1024*1024, CHUNK_BYTES=192*1024;
const metadata={project:id,id,revision,sha256:digest,
  media_type:z.enum(['image/png','image/jpeg','image/webp','application/pdf']),encoding:z.literal('base64'),
  title:bounded(300),source:bounded(2000),captured_at:z.string().datetime(),
  authority:z.literal('source-document-not-native-confirmation')};
const chunkRef=z.object({sha256:digest,bytes:z.number().int().positive().max(CHUNK_BYTES)}).strict();
export const binaryChunk=z.object({schema:z.literal(1),encoding:z.literal('base64'),sha256:digest,
  bytes:z.number().int().positive().max(CHUNK_BYTES),content:z.string().min(4).max(CHUNK_BYTES/3*4)}).strict();
export const binaryRecord=z.discriminatedUnion('schema',[
  z.object({...metadata,schema:z.literal(1),bytes:z.number().int().positive().max(262144),
    content:z.string().min(4).max(349528)}).strict(),
  z.object({...metadata,schema:z.literal(2),encoding:z.literal('chunked-base64'),
    bytes:z.number().int().positive().max(MAX_BINARY_BYTES),
    chunks:z.array(chunkRef).min(1).max(Math.ceil(MAX_BINARY_BYTES/CHUNK_BYTES))}).strict()
]);
export const chunkPath=(project:string,sha256:string)=>`projects/${project}/binary/chunk-${sha256}/1.json`;
export const binaryInput=z.object({project:id,source_project:id.optional(),operation:operation.default('resume'),
  detail:z.enum(['list','content']).default('list'),offset:z.number().int().safe().nonnegative().default(0),
  snapshot:z.string().regex(/^[a-f0-9]{40}$/).optional(),original:id.optional(),revision:revision.optional(),sha256:digest.optional()}).strict();

export async function readBinary(a:z.infer<typeof binaryInput>,identity:{owner:string;generation:string;mode:string},
  read:(path:string)=>Promise<unknown|null>,options:{prefetch?:(paths:string[])=>Promise<void>;release?:(paths:string[])=>void;stream?:boolean}={}) {
  const project=a.source_project??a.project;
  const raw=await read(`projects/${project}/binary/manifest.json`);
  if(raw===null){
    if(a.detail==='content')throw new Error('binary_unavailable');
    return {status:'not_configured',originals:[],next_offset:null};
  }
  const manifest=binaryManifest.parse(raw);
  if(manifest.project!==project || manifest.owner!==identity.owner || manifest.generation!==identity.generation ||
    manifest.mode!==identity.mode || new Set(manifest.current.map(e=>e.id)).size!==manifest.current.length)
    throw new Error('binary_unavailable');
  const allowed=manifest.current.filter(e=>e.remote&&e.projects.includes(a.project)&&e.operations.includes(a.operation));
  const decode=async(record:{content:string;sha256:string;bytes:number})=>{
    // Re-encoding also rejects noncanonical padding without a large regex stack.
    const bytes=Buffer.from(record.content,'base64');
    if(bytes.toString('base64')!==record.content || bytes.length!==record.bytes ||
      createHash('sha256').update(bytes).digest('hex')!==record.sha256)throw new Error('binary_unavailable');
    return bytes;
  };
  const load=async(ref:z.infer<typeof entry>,content:boolean)=>{
    const record=binaryRecord.parse(await read(`projects/${project}/binary/${ref.id}/${ref.revision}.json`));
    if(record.project!==project || record.id!==ref.id || record.revision!==ref.revision || record.sha256!==ref.sha256)
      throw new Error('binary_unavailable');
    if(record.schema===2 && (record.chunks.reduce((sum,c)=>sum+c.bytes,0)!==record.bytes ||
      record.chunks.slice(0,-1).some(c=>c.bytes!==CHUNK_BYTES)))throw new Error('binary_unavailable');
    if(!content)return record;
    let parts:Buffer[]=[];
    if(record.schema===1)parts=[await decode(record)];
    else{
      const fullHash=createHash('sha256');
      // At most 3 MiB plus JSON overhead, within the chunk-only 4 MiB cap.
      // Release cached base64 immediately; keep only verified binary buffers.
      for(let i=0;i<record.chunks.length;i+=12){
        const batch=record.chunks.slice(i,i+12),paths=[...new Set(batch.map(c=>chunkPath(project,c.sha256)))];
        if(options.prefetch)await options.prefetch(paths);
        for(const ref of batch){
          const chunk=binaryChunk.parse(await read(chunkPath(project,ref.sha256)));
          if(chunk.sha256!==ref.sha256 || chunk.bytes!==ref.bytes)throw new Error('binary_unavailable');
          const bytes=await decode(chunk);parts.push(bytes);fullHash.update(bytes);
        }
        options.release?.(paths);
      }
      if(fullHash.digest('hex')!==record.sha256)throw new Error('binary_unavailable');
    }
    const raw=Buffer.from(parts[0].subarray(0,12)).toString('latin1');
    const matches=record.media_type==='image/png'?raw.startsWith('\x89PNG\r\n\x1a\n'):
      record.media_type==='image/jpeg'?raw.startsWith('\xff\xd8\xff'):
      record.media_type==='image/webp'?raw.startsWith('RIFF')&&raw.slice(8,12)==='WEBP':raw.startsWith('%PDF-');
    if(!matches)throw new Error('binary_unavailable');
    return options.stream?{...record,binary_payload:parts}:{...record,content:parts.map(p=>Buffer.from(p).toString('base64')).join('')};
  };
  if(a.detail==='content') {
    const selected=allowed.find(e=>e.id===a.original&&e.revision===a.revision&&e.sha256===a.sha256);
    if(!selected)throw new Error('binary_unavailable');
    return {status:'retrieved',original:await load(selected,true),verification:'exact-original-bytes-at-this-git-snapshot'};
  }
  const originals=[];
  for(const entry of allowed.slice(a.offset,a.offset+3)) {
    const record=await load(entry,false);
    const {content,chunks,...metadata}=record as typeof record&{content?:string;chunks?:unknown};
    originals.push({...metadata,content_verification:'not_requested'});
  }
  return {status:'listed',originals,next_offset:allowed.length>a.offset+3?a.offset+3:null};
}

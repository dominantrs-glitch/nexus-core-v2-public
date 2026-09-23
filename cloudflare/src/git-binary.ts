/** Small explicitly shared originals. No filesystem/URL fetch or model write tool. */
import {z} from 'zod';
const id=z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/),revision=z.number().int().positive().safe();
const digest=z.string().regex(/^[a-f0-9]{64}$/),operation=z.enum(['resume','plan','implement','review']);
const bounded=(max:number)=>z.string().trim().min(1).max(max);
const entry=z.object({id,revision,sha256:digest,remote:z.boolean(),projects:z.array(id).min(1).max(100),
  operations:z.array(operation).min(1).max(4)}).strict();
export const binaryManifest=z.object({schema:z.literal(1),owner:bounded(200),generation:id,
  mode:z.enum(['synthetic','draft-intake']),project:id,current:z.array(entry).max(30)}).strict();
export const binaryRecord=z.object({schema:z.literal(1),project:id,id,revision,sha256:digest,
  media_type:z.enum(['image/png','image/jpeg','application/pdf']),encoding:z.literal('base64'),
  title:bounded(300),source:bounded(2000),captured_at:z.string().datetime(),bytes:z.number().int().positive().max(262144),
  content:z.string().min(4).max(349528),authority:z.literal('source-document-not-native-confirmation')}).strict();
export const binaryInput=z.object({project:id,source_project:id.optional(),operation:operation.default('resume'),
  detail:z.enum(['list','content']).default('list'),offset:z.number().int().safe().nonnegative().default(0),
  snapshot:z.string().regex(/^[a-f0-9]{40}$/).optional(),original:id.optional(),revision:revision.optional(),sha256:digest.optional()}).strict();

export async function readBinary(a:z.infer<typeof binaryInput>,identity:{owner:string;generation:string;mode:string},
  read:(path:string)=>Promise<unknown|null>) {
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
  const load=async(ref:z.infer<typeof entry>)=>{
    const record=binaryRecord.parse(await read(`projects/${project}/binary/${ref.id}/${ref.revision}.json`));
    if(record.project!==project || record.id!==ref.id || record.revision!==ref.revision || record.sha256!==ref.sha256 ||
      !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(record.content))throw new Error('binary_unavailable');
    const raw=atob(record.content),bytes=Uint8Array.from(raw,c=>c.charCodeAt(0));
    const actual=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(n=>n.toString(16).padStart(2,'0')).join('');
    if(btoa(raw)!==record.content || bytes.length!==record.bytes || actual!==record.sha256)throw new Error('binary_unavailable');
    const matches=record.media_type==='image/png'?raw.startsWith('\x89PNG\r\n\x1a\n'):
      record.media_type==='image/jpeg'?raw.startsWith('\xff\xd8\xff'):raw.startsWith('%PDF-');
    if(!matches)throw new Error('binary_unavailable');
    return record;
  };
  if(a.detail==='content') {
    const selected=allowed.find(e=>e.id===a.original&&e.revision===a.revision&&e.sha256===a.sha256);
    if(!selected)throw new Error('binary_unavailable');
    return {status:'retrieved',original:await load(selected),verification:'exact-original-bytes-at-this-git-snapshot'};
  }
  const originals=[];
  for(const entry of allowed.slice(a.offset,a.offset+3)) {
    const {content,...metadata}=await load(entry);originals.push(metadata);
  }
  return {status:'listed',originals,next_offset:allowed.length>a.offset+3?a.offset+3:null};
}

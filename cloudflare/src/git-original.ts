/** Exact UTF-8 source documents. Admin-managed references, never native approval. */
import { z } from "zod";
import { hash } from "./relay-common";

const id = z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/);
const revision = z.number().int().positive().safe();
const digest = z.string().regex(/^[a-f0-9]{64}$/);
const text = (max:number) => z.string().refine(v => !!v.trim() && new TextEncoder().encode(v).length <= max);
const operation = z.enum(["resume","plan","implement","review"]);
export const originalInput=z.object({project:id,source_project:id.optional(),operation:operation.default('resume'),
  detail:z.enum(['list','content']).default('list'),offset:z.number().int().safe().nonnegative().default(0),
  snapshot:z.string().regex(/^[a-f0-9]{40}$/).optional(),original:id.optional(),revision:revision.optional(),
  sha256:digest.optional()}).strict();
export const originalRef = z.object({project:id,original:id,revision,sha256:digest}).strict();
const entry = z.object({id,revision,sha256:digest,remote:z.boolean(),
  projects:z.array(id).min(1).max(100),operations:z.array(operation).min(1).max(4)}).strict();
const manifest = z.object({schema:z.literal(1),owner:text(200),generation:id,
  mode:z.enum(["synthetic","draft-intake"]),project:id,current:z.array(entry).max(100)}).strict();
export const originalRecord = z.object({schema:z.literal(1),project:id,id,revision,sha256:digest,
  media_type:z.enum(["text/plain","text/markdown"]),encoding:z.literal("utf-8"),
  title:text(300),source:text(2000),captured_at:z.string().datetime(),content:text(24576).refine(v =>
    new TextDecoder("utf-8",{fatal:true,ignoreBOM:true}).decode(new TextEncoder().encode(v))===v),
  authority:z.literal("source-document-not-native-confirmation")}).strict();
export type OriginalRef = z.infer<typeof originalRef>;
export type ContextOriginal = z.infer<typeof originalRecord>;

export async function readOriginalManifest(project:string,
  identity:{owner:string;generation:string;mode:'synthetic'|'draft-intake'},read:(path:string)=>Promise<unknown|null>) {
  const raw=await read(`projects/${project}/originals/manifest.json`);
  if(raw===null)return null;
  const current=manifest.parse(raw);
  if(current.owner!==identity.owner || current.generation!==identity.generation || current.mode!==identity.mode ||
     current.project!==project || new Set(current.current.map(e=>e.id)).size!==current.current.length)
    throw new Error('original_unavailable');
  return current;
}

export async function readGitOriginal(ref:OriginalRef,
  identity:{owner:string;generation:string;mode:"synthetic"|"draft-intake"},
  request:{project:string;operation:z.infer<typeof operation>},
  read:(path:string)=>Promise<unknown|null>):Promise<ContextOriginal> {
  // Caller checks source-project visibility first. No arbitrary URL or path fetch.
  const current=await readOriginalManifest(ref.project,identity,read);
  if(!current)throw new Error('original_unavailable');
  const selected=current.current.find(e=>e.id===ref.original);
  if(!selected || !selected.remote || selected.revision!==ref.revision || selected.sha256!==ref.sha256 ||
     !selected.projects.includes(request.project) || !selected.operations.includes(request.operation))
    throw new Error("original_unavailable");
  const record=originalRecord.parse(await read(`projects/${ref.project}/originals/${ref.original}/${ref.revision}.json`));
  if(record.project!==ref.project || record.id!==ref.original || record.revision!==ref.revision ||
     record.sha256!==ref.sha256 || await hash(record.content)!==ref.sha256)
    throw new Error("original_unavailable");
  return record;
}

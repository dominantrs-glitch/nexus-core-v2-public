/** Relationship suggestions reuse attributed notes and correction history. */
import {z} from 'zod';
const id=z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/);
const revision=z.number().int().safe().nonnegative();
export const relationHeader='【関連案件の候補】\n';
const ref=z.object({project:id,note:id,revision:revision.min(1)}).strict();
export const relationDraft=z.object({schema:z.literal(1),target_project:id,target_revision:revision,
  relation:z.enum(['depends_on','surface_of','shares_context_with','related']),
  status:z.enum(['candidate','withdrawn']),reason:z.string().trim().min(1).max(1000),
  basis:z.array(ref).min(2).max(4)}).strict();
export const relationInput=relationDraft.omit({schema:true}).extend({project:id,expected_revision:revision,
  request_id:z.string().min(1).max(150),source:z.string().min(1).max(2000),supersedes:id.nullable().optional()}).strict();
export function parseRelation(body:string) {
  if(!body.startsWith(relationHeader))return null;
  try{return relationDraft.parse(JSON.parse(body.slice(relationHeader.length)));}
  catch{throw new Error('invalid_relation_candidate');}
}
export const relationBasisKinds=['goal','acceptance','constraint','explicit_choice','source'];

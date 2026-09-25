/** Removal from current use, with a retained attributed tombstone and history. */
import {z} from 'zod';
const id=z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/);
const text=z.string().trim().min(1).max(2000);
export const noteRemovalInput=z.object({action:z.enum(['remove','restore']),project:id,note:id,
  expected_revision:z.number().int().nonnegative().safe(),quote:text,source:text,reason:text}).strict();
export const noteRemovalApplyInput=noteRemovalInput.extend({plan_digest:z.string().regex(/^[a-f0-9]{64}$/),
  request_id:z.string().trim().min(1).max(150)}).strict();
export const removalHeader='【記録の取り消し】\n';
const marker=z.object({schema:z.literal(1),original_note:id,reason:text}).strict();
export function removalMarker(body:string){
  if(!body.startsWith(removalHeader))return null;
  return marker.parse(JSON.parse(body.slice(removalHeader.length)));
}

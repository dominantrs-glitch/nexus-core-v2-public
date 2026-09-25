/** Edit an existing attributed shared rule. Scope and native authority are immutable. */
import {z} from 'zod';
const id=z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/);
export const ruleUpdateInput=z.object({rule:id,project:id,note:id,expected_context_revision:z.number().int().positive().safe(),
  expected_revision:z.number().int().nonnegative().safe(),body:z.string().trim().min(1).max(8000),
  quote:z.string().trim().min(1).max(8000),source:z.string().trim().min(1).max(2000)}).strict();
export const ruleApplyInput=ruleUpdateInput.extend({plan_digest:z.string().regex(/^[a-f0-9]{64}$/),
  request_id:z.string().trim().min(1).max(150)}).strict();

/** Evidence required before proposing a new project or a structural relation. */
import {z} from 'zod';
export const projectRef=z.object({project:z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/),
  note:z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/),revision:z.number().int().positive()}).strict();
export const startReviewInput=z.object({title:z.string().trim().min(1).max(300),purpose:z.string().trim().min(1).max(1000),
  responsibility:z.string().trim().min(1).max(1000),canonical:z.string().trim().min(1).max(500),
  assets:z.array(z.string().min(1).max(300)).max(10),dependencies:z.array(z.string().min(1).max(300)).max(10),
  search_terms:z.array(z.string().trim().min(1).max(100)).min(1).max(8),
  candidates:z.array(z.object({project:projectRef.shape.project,basis:z.array(projectRef).min(1).max(4),
    suggestion:z.enum(['reuse','parent','child','depends_on','surface_of','related','separate']),
    reason:z.string().trim().min(1).max(1000)}).strict()).max(8)}).strict();
export const creationReview=z.object({input:startReviewInput,digest:z.string().regex(/^[a-f0-9]{64}$/)}).strict();

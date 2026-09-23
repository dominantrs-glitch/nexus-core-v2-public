/** Bounded discovery over current, permitted notes. Search never resolves context. */
import {z} from 'zod';
import {hash} from './relay-common';

const id=z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/);
const kinds=z.enum(['goal','acceptance','constraint','explicit_choice','preference','research','proposal','question','correction','source']);
export const searchInput=z.object({query:z.string().min(1).max(200),project:id.optional(),
  kinds:z.array(kinds).min(1).max(10).optional(),cursor:z.string().max(2000).optional(),
  snapshot:z.string().regex(/^[a-f0-9]{40}$/).optional()}).strict();
const position=z.object({schema:z.literal(1),snapshot:z.string().regex(/^[a-f0-9]{40}$/),
  query_digest:z.string().regex(/^[a-f0-9]{64}$/),project_index:z.number().int().safe().nonnegative(),
  note_offset:z.number().int().safe().nonnegative()}).strict();
type Project={id:string;title:string;revision:number;current:string[]};
type Note={id:string;revision:number;kind:string;evidence:string;body:string;quote:string;source:string};
const normalize=(value:string)=>value.normalize('NFKC').toLowerCase();

export async function searchNotes(input:z.infer<typeof searchInput>,snapshot:string,
  projects:{id:string}[],readProject:(id:string)=>Promise<Project>,
  readNote:(project:Project,id:string)=>Promise<Note>) {
  const terms=[...new Set(normalize(input.query).trim().split(/\s+/))];
  if(!terms[0] || terms.length>8)throw new Error('invalid_search_query');
  const queryDigest=await hash(JSON.stringify([terms,input.project??null,[...new Set(input.kinds??[])].sort()]));
  let projectIndex=0,noteOffset=0;
  if(input.cursor!==undefined) {
    let cursor:z.infer<typeof position>;
    try {cursor=position.parse(JSON.parse(atob(input.cursor)));}catch{throw new Error('invalid_search_cursor');}
    if(!input.snapshot || cursor.snapshot!==snapshot || cursor.query_digest!==queryDigest)
      throw new Error('search_cursor_mismatch_restart');
    projectIndex=cursor.project_index;noteOffset=cursor.note_offset;
    if(projectIndex>=projects.length)throw new Error('invalid_search_cursor');
  }
  const start={project_index:projectIndex,note_offset:noteOffset};
  const matches:object[]=[];
  let scanned=0,inspectedProjects=0;
  // Keep a single invocation under upstream subrequest/response budgets. No index
  // can silently become stale; ACL, manifests and notes come from this snapshot.
  while(projectIndex<projects.length && scanned<20 && inspectedProjects<5) {
    const project=await readProject(projects[projectIndex].id);inspectedProjects++;
    if(noteOffset>project.current.length)throw new Error('invalid_search_cursor');
    while(noteOffset<project.current.length && scanned<20) {
      const offset=noteOffset++,note=await readNote(project,project.current[offset]);scanned++;
      if(input.kinds && !input.kinds.includes(note.kind as z.infer<typeof kinds>))continue;
      const fields=[['body',note.body],['quote',note.quote],['source',note.source],['note_id',note.id]];
      const haystack=normalize(fields.map(([,v])=>v).join('\n'));
      if(!terms.every(term=>haystack.includes(term)))continue;
      const field=fields.find(([,v])=>terms.some(term=>normalize(v).includes(term)))!;
      const at=Math.max(0,Math.min(...terms.map(term=>normalize(field[1]).indexOf(term)).filter(i=>i>=0))-70);
      matches.push({project:project.id,title:project.title,project_revision:project.revision,
        note:note.id,revision:note.revision,kind:note.kind,evidence:note.evidence,
        authority:'attributed-input-not-owner-confirmation',binding:false,
        excerpt:field[1].slice(at,at+320),excerpt_field:field[0],excerpt_truncated:at>0||field[1].length>at+320,
        read_offset:Math.floor(offset/10)*10});
    }
    if(noteOffset===project.current.length){projectIndex++;noteOffset=0;}
  }
  const exhausted=projectIndex===projects.length;
  const complete=exhausted && input.cursor===undefined;
  const next=exhausted?null:btoa(JSON.stringify({schema:1,snapshot,query_digest:queryDigest,
    project_index:projectIndex,note_offset:noteOffset}));
  return {matches,snapshot,next_cursor:next,
    search:{status:matches.length?'matches':complete?'no_match':'page_no_match',scope:'permitted_current_notes',
      method:'all_terms_literal_nfkc',fields:['body','quote','source','note_id'],
      notes_scanned:scanned,projects_scanned:inspectedProjects,start,
      end:{project_index:projectIndex,note_offset:noteOffset},exhausted,complete,
      match_count:matches.length,project:input.project??null,kinds:input.kinds??null},
    context_evaluated:false,binding:false,
    instruction:'Candidate discovery only, including projects without saved links. Title similarity is not a relationship. Read matching projects and current required context before using findings. Continue every next_cursor with the same snapshot/query/filters. page_no_match covers this page only; complete is true only when this response alone covered the whole requested scope. Combine all pages before reporting no matches. Search excludes superseded notes, local-only projects, originals and external sources; no_match does not prove their absence. Never infer owner approval or completeness of required context.'};
}

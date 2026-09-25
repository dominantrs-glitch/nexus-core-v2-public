/** Trusted owner-local deletion planning. Deliberately not an MCP mutation. */
import {z} from 'zod';
import {catalogSchema,projectSchema,StoreError,type GitBackend} from './git-store';
import {lifecycleSchema} from './git-lifecycle';
import {contextManifest} from './git-context';
import {parseWork} from './git-work';
import {parseRelation} from './git-relations';
import {hash} from './relay-common';
export const deletionInput=z.object({project:z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/),
  scope:z.literal('current_shared_data'),quote:z.string().trim().min(1).max(2000),source:z.string().trim().min(1).max(2000),
  retained_history_acknowledged:z.literal(true)}).strict();

export async function planDeletion(files:Record<string,unknown>,snapshot:string,args:unknown){
  const input=deletionInput.parse(args),root=catalogSchema.parse(files['nexus.json']);
  const entry=root.projects.find(p=>p.id===input.project&&p.remote);
  if(!entry)throw new StoreError('project_unavailable');
  const p=projectSchema.parse(files[`projects/${entry.id}/manifest.json`]);
  if(p.id!==entry.id||p.revision!==entry.revision||p.write_state==='frozen')throw new StoreError('deletion_project_changed');
  const l=lifecycleSchema.parse(files['lifecycle.json']),blockers=new Set<string>();
  if(l.owner!==root.owner||l.generation!==root.generation)throw new StoreError('lifecycle_invalid_or_unavailable');
  if(l.records[p.id]?.status!=='archived')blockers.add('archive_and_review_project_first');
  if(Object.entries(l.records).some(([id,r])=>id!==p.id&&r.canonical===p.id))blockers.add('incoming_merged_history');
  if(l.relations.some(e=>e.project===p.id||e.target===p.id))blockers.add('organization_relations');
  const config=files['context.json']?contextManifest.parse(files['context.json']):null;
  if(config?.entries.some(e=>e.status==='active'&&[e.source,...e.evidence,...e.reuse_basis].some(r=>
    'project' in r&&r.project===p.id||'source_project' in r&&r.source_project===p.id)))blockers.add('context_or_learning_source');
  const native:any=files['native/catalog.json'];
  if(native?.projects?.some((e:any)=>e.project===p.id||e.shares?.some((s:any)=>s.project===p.id)))blockers.add('native_contract_or_share');
  for(const kind of ['originals','binary']){
    const originals:any=files[`projects/${p.id}/${kind}/manifest.json`];
    if(originals?.current?.some((e:any)=>e.remote&&e.projects?.some((id:string)=>id!==p.id)))
      blockers.add('original_used_by_other_projects');
  }
  let freeTextReferences=0;
  for(const other of root.projects.filter(e=>e.id!==p.id)){
    const manifest=projectSchema.parse(files[`projects/${other.id}/manifest.json`]);
    for(const id of manifest.current){
      const n:any=files[`projects/${other.id}/records/${id}.json`];
      if(!n)throw new StoreError('deletion_reference_scan_incomplete');
      const work=parseWork(n.body),relation=parseRelation(n.body);
      if(work?.kind==='work'&&[...work.value.learning?.corrections??[],...work.value.learning?.checks??[]].some(r=>r.project===p.id))
        blockers.add('work_learning_reference');
      if(relation&&relation.status!=='withdrawn'&&(relation.target_project===p.id||relation.basis.some(r=>r.project===p.id)))
        blockers.add('relation_candidate');
      if(JSON.stringify(n).includes(p.id))freeTextReferences++;
    }
    for(const kind of ['originals','binary']){
      const originals:any=files[`projects/${other.id}/${kind}/manifest.json`];
      if(originals?.current?.some((e:any)=>e.projects?.includes(p.id)))blockers.add('original_share_reference');
    }
  }
  const changes:Record<string,unknown>={};
  for(const [path,value] of Object.entries(files)){
    if(path.startsWith(`projects/${p.id}/`))changes[path]=null;
    else if(/^(?:legacy-)?requests\/[a-f0-9]{64}\.json$/.test(path)&&(value as any)?.result?.project===p.id)
      changes[path]={deleted_project:p.id}; // Prevent old create/save retries resurrecting a removed project.
  }
  const nextRoot=structuredClone(root);nextRoot.projects=nextRoot.projects.filter(e=>e.id!==p.id);
  const nextLifecycle=structuredClone(l);delete nextLifecycle.records[p.id];nextLifecycle.revision++;
  changes['lifecycle.json']=nextLifecycle;
  if(config){
    config.profiles=config.profiles.filter(e=>e.project!==p.id);
    config.entries=config.entries.map(e=>({...e,projects:e.projects.filter(id=>id!==p.id)})).filter(e=>e.projects.length);
    config.revision++;nextRoot.context_revision=config.revision;changes['context.json']=config;
  }
  changes['nexus.json']=nextRoot;
  const impact={project:p.id,title:p.title,revision:p.revision,removed_files:Object.values(changes).filter(v=>v===null).length,
    current_notes:p.current.length,work_items:p.work_index?.length??0,other_current_text_references:freeTextReferences,
    retained:['Earlier Git commits and organization events','Existing local copies and backups','Other projects’ attributed statements'],
    restoration:'Retained history can be restored only through a separately reviewed import; normal archive restore cannot revive deleted data.',
    erasure:'Current shared data removal only. This operation does not erase Git history, remote caches or other copies.'};
  const plan_digest=await hash(JSON.stringify({input,snapshot,root,l,config,changes,impact}));
  return {schema:1,input,snapshot,plan_digest,impact,blockers:[...blockers],changes,owner:root.owner,generation:root.generation};
}

export async function applyDeletion(git:GitBackend,plan:Awaited<ReturnType<typeof planDeletion>>){
  // Called only by the owner-local confirmation runner. Never rebase deletion.
  if(plan.blockers.length)throw new StoreError('deletion_dependencies_unresolved');
  const head=await git.head();
  const current:any=await git.read(head,'nexus.json');
  if(current?.owner!==plan.owner||current?.generation!==plan.generation)throw new StoreError('deletion_identity_mismatch');
  const key=`requests/${await hash(JSON.stringify(['deletion',plan.owner,plan.plan_digest]))}.json`;
  const previous:any=await git.read(head,key);
  if(previous?.deletion_plan===plan.plan_digest&&!current.projects.some((p:any)=>p.id===plan.input.project))
    return {...previous.result,snapshot:head};
  if(head!==plan.snapshot)throw new StoreError('deletion_snapshot_changed_review_again');
  const result={status:'removed_current_shared_data',project:plan.input.project,
    removed_files:plan.impact.removed_files,history_erased:false,binding:false};
  try{await git.commit(head,{...plan.changes,[key]:{deletion_plan:plan.plan_digest,result}});}
  catch(error){
    if(error instanceof StoreError&&error.diagnostic)throw error;
    const observed=await git.head(),receipt:any=await git.read(observed,key);
    if(receipt?.deletion_plan===plan.plan_digest)return {...receipt.result,snapshot:observed};
    throw error;
  }
  const after=await git.head(),catalog:any=await git.read(after,'nexus.json');
  if(catalog.projects.some((p:any)=>p.id===plan.input.project))throw new StoreError('deletion_outcome_unknown_read_current_state');
  return {...result,snapshot:after};
}

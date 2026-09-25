/** Owner-local, immutable-snapshot audit. Reports identifiers, never note bodies. */
import {GitIntake,catalogSchema,projectSchema,noteSchema,type GitBackend} from './git-store';
import {contextManifest} from './git-context';
import {parseWork,workKey,learningAssessment,assessWorkLearning} from './git-work';
import {loadLifecycle,integrity} from './git-lifecycle';
import {readOriginalManifest,readGitOriginal} from './git-original';
import {binaryManifest,binaryRecord,chunkPath,readBinary} from './git-binary';
import {validateNativeFiles} from './git-core';
import {hash} from './relay-common';
import {removalMarker} from './git-note-removal';

export async function auditFiles(files:Record<string,unknown>,snapshot:string) {
  const issues:{type:string;path:string;severity:'error'|'review'}[]=[];
  const add=(type:string,path:string,severity:'error'|'review'='error')=>issues.push({type,path,severity});
  const read=async(path:string)=>structuredClone(files[path]??null);
  const git:GitBackend={head:async()=>snapshot,read:async(_,path)=>read(path),commit:async()=>{throw Error('read_only_audit');}};
  const root=catalogSchema.parse(files['nexus.json']);
  const known=new Set(root.projects.map(p=>p.id)),referenced=new Set<string>();
  const projects=new Map<string,ReturnType<typeof projectSchema.parse>>();
  let currentNotes=0,originals=0,nativeDocuments=0,contextChecks=0;
  for(const e of root.projects){
    const path=`projects/${e.id}/manifest.json`;
    try {
      const p=projectSchema.parse(files[path]);projects.set(p.id,p);
      if(p.id!==e.id||p.revision!==e.revision||p.title!==e.title||p.remote!==e.remote||p.write_state!==e.write_state)
        add('project_catalog_mismatch',path);
      if(new Set(p.current).size!==p.current.length||p.overview&&!p.current.includes(p.overview))add('project_current_index_invalid',path);
      for(const id of p.current){
        const notePath=`projects/${p.id}/records/${id}.json`;referenced.add(notePath);currentNotes++;
        try {
          const n=noteSchema.parse(files[notePath]);
          if(n.project!==p.id||n.id!==id||n.revision>p.revision)throw Error();
          const work=parseWork(n.body),entry=p.work_index?.find(e=>e.note===n.id);
          if(work&&(!entry||entry.id!==work.value.id||entry.kind!==work.kind||entry.status!==work.value.status||
            work.kind==='work'&&entry.key!==workKey(work.value.title)))add('work_index_mismatch',notePath);
          if(work?.kind==='work'&&work.value.status==='done'){
            const key=`projects/${p.id}/learning-evaluations/${id}.json`;
            if(!files[key])add('learning_review_missing',notePath,'review');
            else{
              const assessment=learningAssessment.parse(files[key]);
              if(JSON.stringify(assessment)!==JSON.stringify(learningAssessment.parse(assessWorkLearning(p.id,work.value.learning))))
                add('learning_assessment_mismatch',key);
              if(['review_required','needs_verification'].includes(assessment.status))add(assessment.status,key,'review');
            }
          }
        }catch{add('current_note_invalid_or_missing',notePath);}
      }
      for(const entry of p.work_index??[])if(!p.current.includes(entry.note))add('work_current_note_missing',path);
      for(const entry of p.relation_notes??[])if(!p.current.includes(entry))add('relation_current_note_missing',path);
    }catch{add('project_manifest_invalid_or_missing',path);}
  }
  const reference=(r:any,path:string,current=true)=>{
    if(!r||typeof r.project!=='string'||typeof r.note!=='string')return;
    const p=projects.get(r.project),key=`projects/${r.project}/records/${r.note}.json`,n:any=files[key];referenced.add(key);
    if(!p||!n||n.project!==r.project||n.id!==r.note||n.revision!==r.revision||current&&!p.current.includes(r.note))
      add('note_reference_stale_or_missing',path);
  };
  for(const [path,raw] of Object.entries(files)) {
    const n:any=raw,match=/^projects\/([^/]+)\/records\/([^/]+)\.json$/.exec(path);
    const projectPath=/^projects\/([^/]+)\//.exec(path);
    if(projectPath&&!known.has(projectPath[1]))add('orphan_project_file',path);
    if(match){
      const parsed=noteSchema.safeParse(n),p=projects.get(match[1]);
      if(!parsed.success||!p||n.id!==match[2]||n.project!==p.id||n.revision>p.revision){add('historical_note_invalid',path);continue;}
      if(n.supersedes){
        const priorPath=`projects/${n.project}/records/${n.supersedes}.json`,prior:any=files[priorPath];referenced.add(priorPath);
        if(!prior||prior.revision>=n.revision||prior.project!==n.project)add('broken_correction_history',path);
      }
      try{
        const marker=removalMarker(n.body);
        if(marker){
          const event:any=files[`projects/${n.project}/removals/${n.revision}.json`];
          if(n.supersedes!==marker.original_note||n.captured_kind!=='correction'||n.evidence!=='user_statement'||
            !event||event.schema!==1||event.action!=='remove'||event.project!==n.project||event.note!==n.id||
            event.original_note!==marker.original_note||event.previous_note!==marker.original_note||
            event.quote!==n.quote||event.source!==n.source||event.reason!==marker.reason||event.revision!==n.revision)
            add('withdrawal_audit_invalid',path);
        }
      }catch{add('withdrawal_audit_invalid',path);}
      if(p.current.includes(n.id)){
        try{
          const w=parseWork(n.body);
          if(w?.kind==='work'&&w.value.learning)for(const r of [...w.value.learning.corrections,...w.value.learning.checks])reference(r,path);
          if(n.body.startsWith('【関連案件の候補】\n')){
            const r=JSON.parse(n.body.slice(n.body.indexOf('\n')+1));
            if(r.status!=='withdrawn')for(const ref of r.basis??[])reference(ref,path);
          }
        }catch{add('structured_note_invalid',path);}
      }
    }
    const change=/^projects\/([^/]+)\/changes\/(\d+)\.json$/.exec(path);
    if(change){
      const key=`projects/${change[1]}/records/${n.note}.json`,note:any=files[key];referenced.add(key);
      if(!note||n.project!==change[1]||n.revision!==Number(change[2])||note.revision!==n.revision||note.supersedes!==n.supersedes)
        add('change_record_mismatch',path);
    }
  }
  for(const path of Object.keys(files))if(/^projects\/[^/]+\/records\//.test(path)&&!referenced.has(path))
    add('unreferenced_record_review',path,'review');
  for(const p of projects.values())for(const kind of ['originals','binary']){
    const path=`projects/${p.id}/${kind}/manifest.json`;
    if(!files[path])continue;
    try{
      const m=kind==='originals'?await readOriginalManifest(p.id,root,read):binaryManifest.parse(files[path]);
      if(!m||m.owner!==root.owner||m.generation!==root.generation||m.mode!==root.mode||m.project!==p.id)throw Error();
      const ids=new Set(m.current.map(e=>e.id));
      if(ids.size!==m.current.length)throw Error();
      for(const entry of m.current){
        for(const target of entry.projects)if(!known.has(target))add('original_target_missing',path);
        // Check bytes even for withdrawn/private originals, without exporting them.
        const overlay=async(key:string)=>key===path?{...m,current:m.current.map(e=>e.id===entry.id?{...e,remote:true}:e)}:read(key);
        try{
          if(kind==='originals')await readGitOriginal({project:p.id,original:entry.id,revision:entry.revision,sha256:entry.sha256},root,
            {project:entry.projects[0],operation:entry.operations[0]},overlay);
          else await readBinary({project:entry.projects[0],source_project:p.id,operation:entry.operations[0],detail:'content',offset:0,
            original:entry.id,revision:entry.revision,sha256:entry.sha256},root,overlay);
          originals++;
        }catch{add('original_bytes_invalid_or_missing',`${path}:${entry.id}`);}
      }
      const chunkReferences=new Set<string>();
      if(kind==='binary')for(const [key,value] of Object.entries(files)){
        const r=binaryRecord.safeParse(value);
        if(r.success&&r.data.schema===2&&key===`projects/${p.id}/binary/${r.data.id}/${r.data.revision}.json`&&r.data.project===p.id)
          for(const chunk of r.data.chunks)chunkReferences.add(chunkPath(p.id,chunk.sha256));
      }
      for(const key of Object.keys(files)){
        const parts=key.split('/');
        if(parts[0]==='projects'&&parts[1]===p.id&&parts[2]===kind&&parts.length===5&&!ids.has(parts[3])&&!chunkReferences.has(key))
          add('unindexed_original_review',key,'review');
      }
    }catch{add('original_manifest_invalid',path);}
  }
  try{for(const issue of integrity(await loadLifecycle(git,snapshot,root),root))add(issue.type,'lifecycle.json');}
  catch{add('lifecycle_invalid','lifecycle.json');}
  const native:any=files['native/catalog.json'];
  if(native){
    if(native.owner!==root.owner||native.generation!==root.generation||native.mode!==root.mode)add('native_identity_mismatch','native/catalog.json');
    for(const e of native.projects??[]){
      const path=`native/projects/${e.project}/${e.revision}.json`,doc:any=files[path];
      try{
        const ordered=(v:any):any=>Array.isArray(v)?v.map(ordered):v&&typeof v==='object'?
          Object.fromEntries(Object.keys(v).sort().map(k=>[k,ordered(v[k])])):v;
        if(!doc||doc.project!==e.project||doc.owner!==native.native_owner||doc.revision!==e.revision||
          await hash(JSON.stringify(ordered(doc)))!==e.document_sha256)throw Error();
        await validateNativeFiles(doc.files,e.project,native.native_owner);nativeDocuments++;
      }catch{add('native_document_invalid',path);}
    }
  }
  if(files['context.json'])try{
    const config=contextManifest.parse(files['context.json']);
    for(const e of config.entries)if(e.status==='active'&&(config.schema===1||
      !['history','foundation_rule','one_shot_instruction','proposal'].includes(e.record_kind!)))
      for(const r of [e.source,...e.evidence,...e.reuse_basis])reference(r,'context.json:'+e.id);
    const store=new GitIntake(git,root.owner,root.generation,root.mode,native?.native_owner);
    const task_types=[...new Set(config.entries.flatMap(e=>e.task_types))];
    for(const p of root.projects.filter(p=>p.remote))for(const operation of ['resume','plan','implement','review'] as const){
      if(!config.profiles.some(e=>[p.id,'*'].includes(e.project)&&e.operations.includes(operation)))continue;
      for(const mode of ['delegate','independent'] as const){
        try{
          const view=await store.read({project:p.id,operation,mode,task_types,
            decision_factors:['owner_values','priority','tradeoff','delegated_decision']});
          contextChecks++;if(!view.context.complete)add('required_context_unavailable',`context.json:${p.id}:${operation}:${mode}`);
        }catch{add('context_read_failed',`context.json:${p.id}:${operation}:${mode}`);}
      }
    }
  }catch{add('context_manifest_invalid','context.json');}
  return {schema:1,snapshot,status:issues.some(i=>i.severity==='error')?'issues_found':issues.length?'review_needed':'passed',
    counts:{files:Object.keys(files).length,projects:projects.size,current_notes:currentNotes,originals,native_documents:nativeDocuments,context_checks:contextChecks},
    issues,binding:false,coverage:'One exact Git tree: shared records, correction chains, work/learning references, current original bytes, native packages and required context. Retained history is not garbage.',
    not_checked:['External legacy source currentness','Local private databases, external jobs and credentials','Unreachable Git objects and other branches']};
}

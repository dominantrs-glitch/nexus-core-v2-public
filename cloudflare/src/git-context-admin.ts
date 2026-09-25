/** Trusted owner-local policy maintenance. Deliberately absent from MCP. */
import {readFileSync} from 'node:fs';
import {contextManifest} from './git-context';
import {GitHubBackend} from './github-store';
import {GitIntake,type GitBackend} from './git-store';

async function main(){
  const config=JSON.parse(readFileSync(process.argv[2],'utf8'));
  const candidate=contextManifest.parse(JSON.parse(readFileSync(process.argv[3],'utf8')));
  const git=new GitHubBackend({GIT_REPOSITORY:config.repository,GIT_REPOSITORY_ID:config.repository_id,GIT_BRANCH:config.branch,
    GIT_APP_ID:config.app_id,GIT_INSTALLATION_ID:config.installation_id,GIT_APP_PRIVATE_KEY:readFileSync(config.private_key_file,'utf8')});
  const head=await git.head(),root:any=await git.read(head,'nexus.json'),before:any=await git.read(head,'context.json');
  if(candidate.owner!==config.owner||root.owner!==config.owner||candidate.generation!==config.generation||root.generation!==config.generation||
    candidate.mode!==config.mode||root.mode!==config.mode)throw Error('policy_identity_mismatch');
  if(JSON.stringify(candidate)===JSON.stringify(before))return {status:'already_current',revision:candidate.revision};
  if(candidate.revision!==(root.context_revision??0)+1)throw Error('policy_revision_conflict');
  const cache=new Map<string,Promise<any>>();
  const overlay:GitBackend={head:async()=>head,commit:async()=>{throw Error('preview_only');},read:async(_,path)=>{
    if(path==='context.json')return structuredClone(candidate);
    if(path==='nexus.json')return {...root,context_revision:candidate.revision};
    if(!cache.has(path))cache.set(path,git.read(head,path));return structuredClone(await cache.get(path));
  }};
  const store=new GitIntake(overlay,config.owner,config.generation,config.mode,config.native_owner);
  let checks=0;
  for(const p of root.projects.filter((p:any)=>p.remote))for(const operation of ['resume','plan','implement','review']){
    if(!candidate.profiles.some(profile=>[p.id,'*'].includes(profile.project)&&profile.operations.includes(operation as any)))continue;
    const task_types=[...new Set(candidate.entries.filter(e=>e.projects.some(id=>[p.id,'*'].includes(id))).flatMap(e=>e.task_types))];
    for(const mode of ['delegate','independent']){
      const view=await store.read({project:p.id,detail:'context',operation,mode,task_types,
        decision_factors:['owner_values','priority','tradeoff','delegated_decision']});
      if(!view.context.complete)throw Error('required_policy_context_unavailable');checks++;
    }
  }
  if(process.argv.includes('--apply')){
    if(await git.head()!==head)throw Error('policy_snapshot_changed_preview_again');
    await git.commit(head,{'nexus.json':{...root,context_revision:candidate.revision},'context.json':candidate});
  }
  return {status:process.argv.includes('--apply')?'applied':'validated',revision:candidate.revision,checks,
    binding:false,native_owner_confirmation:'not_inferred'};
}
main().then(result=>console.log(JSON.stringify(result))).catch(()=>{
  // No policy body, credentials, input path or upstream response in diagnostics.
  console.log(JSON.stringify({status:'not_applied_or_outcome_unknown',instruction:'Inspect current policy/revision before retry. Keep the same candidate; never overwrite concurrent changes.'}));process.exitCode=1;
});

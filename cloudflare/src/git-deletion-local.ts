/** Internal owner-local runner used after native confirmation by nexus.deletion. */
import {readFileSync} from 'node:fs';
import {localSnapshot} from './git-snapshot-local';
import {planDeletion,applyDeletion} from './git-deletion';
import {GitHubBackend} from './github-store';
import {StoreError} from './git-store';
async function main(){
  let raw='';process.stdin.setEncoding('utf8');
  for await(const chunk of process.stdin){raw+=chunk;if(raw.length>16000)throw Error('bounded_input_required');}
  const a=JSON.parse(raw),{files,snapshot}=localSnapshot(a.repository,a.ref),plan=await planDeletion(files,snapshot,a.input);
  if(a.operation==='plan'){const {changes,...view}=plan;return view;}
  if(a.operation!=='apply'||a.plan_digest!==plan.plan_digest||!/^local-approval-[1-9][0-9]*$/.test(a.approval_receipt??''))
    throw new StoreError('deletion_review_required');
  const c=JSON.parse(readFileSync(a.config,'utf8'));
  if(c.owner!==plan.owner||c.generation!==plan.generation)throw new StoreError('deletion_identity_mismatch');
  const git=new GitHubBackend({GIT_REPOSITORY:c.repository,GIT_REPOSITORY_ID:c.repository_id,GIT_BRANCH:c.branch,
    GIT_APP_ID:c.app_id,GIT_INSTALLATION_ID:c.installation_id,GIT_APP_PRIVATE_KEY:readFileSync(c.private_key_file,'utf8')});
  return applyDeletion(git,plan);
}
main().then(result=>console.log(JSON.stringify({ok:true,result}))).catch(error=>{
  console.log(JSON.stringify({ok:false,reason:error instanceof StoreError?error.code:'deletion_unavailable_or_outcome_unknown'}));process.exitCode=1;});

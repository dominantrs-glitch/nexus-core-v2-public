import {writeFileSync} from 'node:fs';
import {localSnapshot} from './git-snapshot-local';
import {auditFiles} from './git-audit';
async function main(){
  const [repo,ref,output]=process.argv.slice(2),{files,snapshot}=localSnapshot(repo,ref);
  const result=await auditFiles(files,snapshot);
  writeFileSync(output,JSON.stringify(result,null,2));
  console.log(JSON.stringify({...result,issues:undefined,issue_counts:result.issues.reduce((r:Record<string,number>,i)=>(r[i.type]=(r[i.type]??0)+1,r),{})}));
}
main().catch(()=>{console.log(JSON.stringify({status:'audit_unavailable',production_changed:false}));process.exitCode=1;});

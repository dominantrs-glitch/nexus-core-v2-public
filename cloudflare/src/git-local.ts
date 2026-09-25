/** Trusted owner-local CLI. Same Git implementation as the cloud; no MCP tool. */
import {readFileSync} from "node:fs";
import {z} from "zod";
import {GitHubBackend} from "./github-store";
import {GitIntake, StoreError, dataMode} from "./git-store";
import {GitCoreStore} from "./git-core";

const configSchema = z.object({owner:z.string().min(1),generation:z.string().min(1),
  private_key_file:z.string().min(1),repository:z.string(),repository_id:z.string(),
  branch:z.string(),app_id:z.string(),installation_id:z.string(),mode:dataMode.default("synthetic"),
  native_owner:z.string().min(1).max(200).optional()}).strict();
const requestSchema = z.object({operation:z.enum(["list","read","search","original","binary","saveRelation","work","daily","saveWork","saveHours","create","save","core_read","core_write","lifecycle","previewLifecycle","applyLifecycle","reviewStart","capabilities","previewRule","applyRule","previewNoteRemoval","applyNoteRemoval"]),args:z.unknown()}).strict();
async function run() {
  const config=configSchema.parse(JSON.parse(readFileSync(process.argv[2],"utf8")));
  let raw="";
  process.stdin.setEncoding("utf8");
  for await(const chunk of process.stdin) {
    raw+=chunk.toString(); if(Buffer.byteLength(raw)>1100000)throw new StoreError("invalid_arguments");
  }
  const request=requestSchema.parse(JSON.parse(raw));
  const git=new GitHubBackend({GIT_REPOSITORY:config.repository,GIT_REPOSITORY_ID:config.repository_id,
    GIT_BRANCH:config.branch,GIT_APP_ID:config.app_id,GIT_INSTALLATION_ID:config.installation_id,
    GIT_APP_PRIVATE_KEY:readFileSync(config.private_key_file,"utf8")});
  let result:unknown;
  if(request.operation==="core_read" || request.operation==="core_write") {
    if(!config.native_owner)throw new StoreError("native_owner_route_not_configured");
    const core=new GitCoreStore(git,config.owner,config.generation,config.mode,config.native_owner);
    result=await core[request.operation==="core_read"?"read":"write"](request.args);
  } else {
    if(Buffer.byteLength(raw)>32768)throw new StoreError("invalid_arguments");
    const store=new GitIntake(git,config.owner,config.generation,config.mode,config.native_owner);
    result=await store[request.operation](request.args);
  }
  process.stdout.write(JSON.stringify({ok:true,result}));
}
run().catch(error=>{
  // Never echo a credential, input payload, config path or upstream response.
  process.stdout.write(JSON.stringify({ok:false,reason:error instanceof StoreError?error.code:
    error instanceof z.ZodError?"invalid_arguments_or_configuration":"canonical_unavailable_or_outcome_unknown",
    ...(error instanceof StoreError&&error.diagnostic?{diagnostic:error.diagnostic}:{})}));
  process.exitCode=1;
});

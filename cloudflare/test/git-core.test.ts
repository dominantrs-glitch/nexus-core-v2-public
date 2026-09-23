import {beforeAll,it,expect} from "vitest";
import {execFileSync} from "node:child_process";
import {resolve} from "node:path";
import {createHash} from "node:crypto";
import {GitCoreStore} from "../src/git-core";
import {GitHubBackend} from "../src/github-store";
import {FakeGitHub} from "./git-fixture";
import {GitIntake} from "../src/git-store";
import {gitMcp} from "../src/git-mcp";

let input:any;
beforeAll(()=>{
  const root=resolve(".."),python=resolve(root,process.platform==="win32"?".venv/Scripts/python.exe":".venv/bin/python");
  input=JSON.parse(execFileSync(python,["-m","tests.native_git_fixture"],{cwd:root,encoding:"utf8"}));
});
async function setup() {
  const fake=await new FakeGitHub().init();
  const git=new GitHubBackend(fake.config,fake.fetch);
  const store=new GitCoreStore(git,"owner","test-1","synthetic","native-owner");
  return {fake,git,store};
}
it("imports Python's frozen native checkpoint and recovers lost HTTP replies without duplicated revisions",async()=>{
  const {fake,git,store}=await setup(); fake.losePatchReply=true;
  const saved=await store.write(input),after=fake.branch;
  expect(saved.revision).toBe(1);
  expect(await new GitCoreStore(git,"owner","test-1","synthetic","native-owner").write(input)).toEqual(saved);
  expect(fake.branch).toBe(after);
  const read=await store.read({project:input.project});
  expect(read.document.files).toEqual(input.files);
  expect(read.document.origin).toEqual(input.origin);
  const next=await store.write({...input,expected_revision:1,expected_document_sha256:saved.document_sha256,request_id:"next-native"});
  expect(next.revision).toBe(2);
  expect((await store.read({project:input.project})).document.previous_sha256).toBe(saved.document_sha256);
  expect(await git.read(fake.branch,`native/projects/${input.project}/1.json`)).not.toBeNull();
  expect((await git.read(fake.branch,"nexus.json") as any).projects).toEqual([]);
});
it("conflicting writers cannot overwrite an already advanced checkpoint",async()=>{
  const {store}=await setup();const saved=await store.write(input);
  const next={...input,expected_revision:1,expected_document_sha256:saved.document_sha256};
  const result=await Promise.allSettled([store.write({...next,request_id:"writer-a"}),
    store.write({...next,request_id:"writer-b"})]);
  expect(result.filter(r=>r.status==="fulfilled")).toHaveLength(1);
  expect((await store.read({project:input.project})).document.revision).toBe(2);
});
it("round-trips a real Python native owner operation through the GitHub HTTP adapter with preserved history",async()=>{
  const {store}=await setup();await store.write(input);const view=await store.read({project:input.project});
  const root=resolve(".."),python=resolve(root,process.platform==="win32"?".venv/Scripts/python.exe":".venv/bin/python");
  const update=JSON.parse(execFileSync(python,["-m","tests.native_git_fixture","--advance"],
    {cwd:root,encoding:"utf8",input:JSON.stringify(view)}));
  const result=await store.write(update);expect(result.revision).toBe(2);
  const current=await store.read({project:input.project});
  const oldAudit=JSON.parse(Buffer.from(input.files['approvals.json'],'base64').toString('utf8'));
  const audit=JSON.parse(Buffer.from(current.document.files['approvals.json'],'base64').toString('utf8'));
  expect(audit.approvals.slice(0,-1)).toEqual(oldAudit.approvals);
  expect(audit.approvals.at(-1).kind).toBe('user_uat');
  expect(await store.write(update)).toEqual(result);
});
it("requires pinned generation, source identity, revision, read snapshot and immutable origin",async()=>{
  const {store}=await setup();await store.write(input);const first=await store.read({project:input.project});
  await expect(store.write({...input,expected_generation:"other"})).rejects.toThrow("generation_changed");
  await expect(store.write({...input,request_id:"stale"})).rejects.toThrow("revision_conflict");
  await expect(store.write({...input,expected_revision:1,expected_document_sha256:first.document_sha256,request_id:"changed-origin",origin:{...input.origin,source_digest:"f".repeat(64)}})).rejects.toThrow("origin_changed");
  await expect(store.write({...input,expected_revision:1,request_id:"wrong-checkpoint"})).rejects.toThrow("checkpoint_changed");
  await expect(store.write({...input,expected_revision:1})).rejects.toThrow("different_content");
  await store.write({...input,expected_revision:1,expected_document_sha256:first.document_sha256,request_id:"new"});
  await expect(store.read({project:input.project,snapshot:first.snapshot})).rejects.toThrow("snapshot_changed");
});
it("checks visibility before replay and refuses writes to frozen destinations",async()=>{
  const {git,fake,store}=await setup();const saved=await store.write(input);
  const catalog=await git.read(fake.branch,"native/catalog.json") as any;
  catalog.projects[0].write_state="frozen";await git.commit(fake.branch,{"native/catalog.json":catalog});
  expect(await store.write(input)).toEqual(saved);
  await expect(store.write({...input,expected_revision:1,request_id:"frozen"})).rejects.toThrow("frozen");
  catalog.projects[0].enabled=false;await git.commit(fake.branch,{"native/catalog.json":catalog});
  await expect(store.write(input)).rejects.toThrow("unavailable");
  await expect(store.read({project:input.project})).rejects.toThrow("unavailable");
});
it("refuses other owners, changed bytes, extra files, traversal and oversized packages before writing",async()=>{
  const {fake,git,store}=await setup();const start=fake.branch;
  const files=structuredClone(input.files);
  files['project.json']=Buffer.from('{}').toString('base64');
  await expect(store.write({...input,files})).rejects.toThrow();
  await expect(store.write({...input,files:{...input.files,"../secret":files['project.json']}})).rejects.toThrow();
  await expect(store.write({...input,files:{...input.files,[`artifacts/blobs/${"f".repeat(64)}`]:"YQ=="}})).rejects.toThrow();
  await expect(store.write({...input,files:{...input.files,"project.json":"YQ==".repeat(300000)}})).rejects.toThrow();
  await expect(new GitCoreStore(git,"other","test-1","synthetic","native-owner").write(input)).rejects.toThrow();
  await expect(new GitCoreStore(git,"owner","test-1","synthetic","other-native").write(input)).rejects.toThrow();
  expect(fake.branch).toBe(start);
});
it("rejects missing or corrupt canonical documents without using an older version",async()=>{
  const {fake,git,store}=await setup();await store.write(input);
  const path=`native/projects/${input.project}/1.json`;
  const doc=await git.read(fake.branch,path) as any;doc.owner="forged-owner";
  await git.commit(fake.branch,{[path]:doc});
  await expect(store.read({project:input.project})).rejects.toThrow("invalid");
});
it("rejects rewriting native history even when the modified bundle has valid hashes",async()=>{
  const {store,fake}=await setup();const saved=await store.write(input),head=fake.branch;
  const files=structuredClone(input.files);
  const state=JSON.parse(Buffer.from(files['project.json'],'base64').toString('utf8'));
  state.contracts[0].goal="rewritten historical confirmation";
  const bytes=Buffer.from(JSON.stringify(state));files['project.json']=bytes.toString('base64');
  const manifest=JSON.parse(Buffer.from(files['manifest.json'],'base64').toString('utf8'));
  manifest.files['project.json']=createHash('sha256').update(bytes).digest('hex');
  files['manifest.json']=Buffer.from(JSON.stringify(manifest)).toString('base64');
  await expect(store.write({...input,files,expected_revision:1,expected_document_sha256:saved.document_sha256,request_id:"rewrite"})).rejects.toThrow("history_rewrite");
  expect(fake.branch).toBe(head);
});
it("draft model tools cannot write native state or impersonate native confirmation",async()=>{
  const {fake,git}=await setup();const start=fake.branch;
  const response=await (await gitMcp({jsonrpc:"2.0",id:1,method:"tools/call",params:{name:"core_write",arguments:input}},new GitIntake(git,"owner","test-1"))).json() as any;
  expect(response.result.isError).toBe(true);
  expect(JSON.stringify(response)).toContain("unknown_tool");
  expect(fake.branch).toBe(start);
});

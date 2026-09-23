import { it, expect } from "vitest";
import { GitIntake, StoreError, type GitBackend } from "../src/git-store";
import { gitMcp } from "../src/git-mcp";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { FakeGitHub } from "./git-fixture";
import { GitHubBackend } from "../src/github-store";
import {sourceDocuments} from '../src/source-documents';

it('exposes pinned and latest legacy locators without claiming original retrieval',async()=>{
 const git=new MemoryGit(),store=new GitIntake(git,'owner','test-1');
 const source='git:ai-workspace@'+'a'.repeat(40)+':projects/日本語/a b.md';
 const p=await store.create({title:'Synthetic legacy',source,request_id:'legacy-create'});
 await store.save({project:p.project,kind:'source',body:'old text',source,evidence:'external_source',quote:'',expected_revision:0,request_id:'legacy-note'});
 const v=await store.read({project:p.project});
 expect(v.source_documents).toHaveLength(1);
 if(!v.source_documents)throw new Error('full read must expose source documents');
 expect(v.source_documents[0]).toMatchObject({captured_commit:'a'.repeat(40),status:'reference_only_not_fetched',currentness:'not_checked'});
 expect(v.source_documents[0].captured_url).toContain('/'+'a'.repeat(40)+'/');
 expect(v.source_documents[0].latest_url).toContain('/main/');
 expect(v.source_documents[0].latest_url).toContain('%20');
 expect(v.context.complete).toBe(false);
 expect(await store.read({project:p.project,detail:'overview'})).not.toHaveProperty('source_documents');
 expect(sourceDocuments([{source},{source}])).toHaveLength(1);
 for(const path of ['../secret','brain/../secret','brain/%2e%2e/x','brain//x','brain/x?token=a','brain/x#y','brain/\\x','.git/config']){
  expect(sourceDocuments([{source:'git:ai-workspace@'+'a'.repeat(40)+':'+path}])).toEqual([]);
 }
});

// Versioned backend models a branch, including unreachable competing commits.
class MemoryGit implements GitBackend {
  branch = "0".repeat(40);
  commits = new Map<string, Record<string, unknown>>([[this.branch, {
    "nexus.json": { schema: 1, mode: "synthetic", owner: "owner", generation: "test-1", projects: [] }
  }]]);
  writes = 0;
  loseReply = false;
  failWrite = false;
  async head() { return this.branch; }
  async read(commit: string, path: string) { return structuredClone(this.commits.get(commit)?.[path] ?? null); }
  async commit(base: string, files: Record<string, unknown>) {
    if (this.failWrite) throw new StoreError("canonical_unavailable_or_outcome_unknown");
    const next = (++this.writes).toString(16).padStart(40, "0");
    this.commits.set(next, structuredClone({ ...this.commits.get(base), ...files }));
    if (base !== this.branch) throw new StoreError("canonical_conflict_reread");
    this.branch = next;
    if (this.loseReply) { this.loseReply = false; throw new Error("response lost"); }
  }
}

it("replays actual Python-exported receipts after freeze/restore without duplicate Git writes", async () => {
  const temp = mkdtempSync(join(tmpdir(), "nexus-receipt-test-"));
  try {
    const root = resolve(".."), python = resolve(root, process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
    const code = `
import json, sys
from pathlib import Path
from nexus.intake import Intake
from nexus.git_migration import snapshot, digest, stage
from nexus.intake_freeze import freeze, backup, restore, frozen_snapshot
root = Path(sys.argv[1])
store = Intake(root/'source')
created = store.create('架空の風船','synthetic','create-old',remote=True)
args = dict(project=created['project'],kind='proposal',body='架空メモ',source='synthetic',evidence='model_inference',quote='',expected_revision=0,request_id='save-old')
saved = store.save(**args)
corrected = store.save(**(args | dict(kind='correction',body='訂正した架空メモ',expected_revision=1,request_id='correct-old',supersedes=saved['note'])))
selected = snapshot(store.root,[created['project']])
generation = freeze(store,[created['project']],digest(selected))['generation']
backup(store,generation,root/'backup.json')
restore(root/'backup.json',root/'restored')
stage(frozen_snapshot(Intake(root/'restored'),generation),'owner',root/'stage')
files = {p.relative_to(root/'stage').as_posix():json.loads(p.read_text('utf-8')) for p in (root/'stage').rglob('*.json')}
# This test creates exclusively synthetic input above; real staging remains blocked.
files['nexus.json']['mode']='synthetic'
print(json.dumps(dict(files=files,created=created,saved=saved,corrected=corrected,args=args),ensure_ascii=True))
`;
    const fixture = JSON.parse(execFileSync(python, ["-c", code, temp], {cwd:root,encoding:"utf8"}));
    const fake = await new FakeGitHub().init();
    fake.objects.set(fake.branch,fixture.files); fake.trees.set(fake.branch,fixture.files);
    class HttpGit extends GitHubBackend {
      get branch() {return fake.branch;}
      get writes() {return fake.calls.filter(c=>c.method==="PATCH").length;}
    }
    const git = new HttpGit(fake.config,fake.fetch);
    const store = new GitIntake(git,"owner");
    expect(await store.create({title:"架空の風船",source:"synthetic",request_id:"create-old"})).toEqual(fixture.created);
    expect(await store.save(fixture.args)).toEqual(fixture.saved);
    expect(await store.save({...fixture.args,supersedes:null})).toEqual(fixture.saved);
    expect(await store.save({...fixture.args,kind:"correction",body:"訂正した架空メモ",expected_revision:1,
      request_id:"correct-old",supersedes:fixture.saved.note})).toEqual(fixture.corrected);
    expect(git.writes).toBe(0);
    expect((await store.read({project:fixture.created.project})).notes).toHaveLength(1);
    await expect(store.save({...fixture.args,body:"different"})).rejects.toThrow("different_content");
    await expect(store.create({title:"架空の風船",source:"synthetic",request_id:"save-old"})).rejects.toThrow("different_content");
    const stagedCatalog = await git.read(git.branch,"nexus.json") as any;
    const stagedProject = await git.read(git.branch,`projects/${fixture.created.project}/manifest.json`) as any;
    stagedCatalog.projects[0].write_state="frozen"; stagedProject.write_state="frozen";
    await git.commit(git.branch,{"nexus.json":stagedCatalog,[`projects/${fixture.created.project}/manifest.json`]:stagedProject});
    await expect(store.save({...fixture.args,expected_revision:2,request_id:"new-cloud-save"})).rejects.toThrow("frozen");
    expect((await store.read({project:fixture.created.project})).storage.write_state).toBe("frozen");
    // Already committed receipts remain recoverable without adding a write.
    expect(await store.save(fixture.args)).toEqual(fixture.saved);
    stagedCatalog.projects[0].write_state="active"; stagedProject.write_state="active";
    await git.commit(git.branch,{"nexus.json":stagedCatalog,[`projects/${fixture.created.project}/manifest.json`]:stagedProject});
    await store.save({...fixture.args,expected_revision:2,request_id:"new-cloud-save"});
    expect((await store.read({project:fixture.created.project})).revision).toBe(3);
    expect(await store.save(fixture.args)).toEqual(fixture.saved);
    const catalog = await git.read(git.branch,"nexus.json") as any;
    catalog.generation="another-generation";
    await git.commit(git.branch,{"nexus.json":catalog});
    await expect(store.save(fixture.args)).rejects.toThrow("canonical_invalid");
    catalog.projects[0].remote=false;
    await git.commit(git.branch,{"nexus.json":catalog});
    await expect(store.save(fixture.args)).rejects.toThrow("project_unavailable");
    await expect(store.create({title:"架空の風船",source:"synthetic",request_id:"create-old"})).rejects.toThrow("canonical_invalid");
  } finally { rmSync(temp,{recursive:true,force:true}); }
});
async function fixture() {
  const git = new MemoryGit(), store = new GitIntake(git, "owner");
  const p = await store.create({ title: "試験", source: "synthetic", request_id: "create" });
  const args = { project: p.project, kind: "constraint", body: "無料", source: "synthetic", evidence: "user_statement",
    quote: "無料", expected_revision: 0, request_id: "save" };
  return { git, store, p, args };
}
it("owner-local clients pin canonical generation and fail closed after replacement", async () => {
  const {git,p}=await fixture();
  expect((await new GitIntake(git,"owner","test-1").read({project:p.project})).project).toBe(p.project);
  await expect(new GitIntake(git,"owner","another-generation").read({project:p.project})).rejects.toThrow("generation_changed");
  await expect(new GitIntake(git,"owner","another-generation").create({title:"blocked",source:"synthetic",request_id:"new"}))
    .rejects.toThrow("generation_changed");
});
it("real draft catalogs require explicit matching deployment mode and cannot accept review staging", async () => {
  const {git,p,store}=await fixture();
  const catalog=await git.read(git.branch,"nexus.json") as any;
  catalog.mode="draft-intake";
  await git.commit(git.branch,{"nexus.json":catalog});
  await expect(store.read({project:p.project})).rejects.toThrow("canonical_invalid");
  const real=new GitIntake(git,"owner",undefined,"draft-intake");
  expect((await real.read({project:p.project})).project).toBe(p.project);
  const response=await gitMcp({jsonrpc:"2.0",id:1,method:"tools/list"},real);
  const tools=(await response.json() as any).result.tools;
  expect(tools.find((t:any)=>t.name==="create_project").description).toContain("user authorizes");
  expect(tools.some((t:any)=>t.description.includes("Synthetic pilot only"))).toBe(false);
  catalog.mode="migration-review";
  await git.commit(git.branch,{"nexus.json":catalog});
  await expect(real.list()).rejects.toThrow("canonical_invalid");
});
it("one-project migration mode refuses new projects but preserves old create retries", async()=>{
  const {git,store,p}=await fixture();
  const catalog=await git.read(git.branch,"nexus.json") as any;
  catalog.allow_create=false;
  await git.commit(git.branch,{"nexus.json":catalog});
  expect(await store.create({title:"試験",source:"synthetic",request_id:"create"})).toEqual(p);
  await expect(store.create({title:"new",source:"synthetic",request_id:"new"})).rejects.toThrow("new_projects_not_enabled");
});
it("persists idempotency across new clients, changed payloads and lost replies", async () => {
  const {git,store,p,args} = await fixture();
  git.loseReply = true;
  const result = await store.save(args);
  expect(await new GitIntake(git, "owner").save(args)).toEqual(result);
  expect((await store.read({project:p.project})).revision).toBe(1);
  expect(git.writes).toBe(2);
  await expect(store.save({...args, body:"paid"})).rejects.toThrow("different_content");
  await expect(store.create({title:"other",source:"synthetic",request_id:"save"})).rejects.toThrow("different_content");
});
it("same revision concurrent writes never lose data; explicit reread allows reapplication", async () => {
  const {git,store,p,args} = await fixture();
  const results = await Promise.allSettled([store.save(args), new GitIntake(git,"owner").save({...args,request_id:"second",body:"other"})]);
  expect(results.filter(r => r.status === "fulfilled")).toHaveLength(1);
  const read = await store.read({project:p.project});
  expect(read.notes).toHaveLength(1);
  await expect(store.save({...args,request_id:"third"})).rejects.toThrow("revision_conflict");
  await store.save({...args,request_id:"third",expected_revision:1});
  expect((await store.read({project:p.project})).notes).toHaveLength(2);
});
it("concurrent identical requests recover the same receipt without duplicate notes", async () => {
  const {store,p,args} = await fixture();
  const results = await Promise.all([store.save(args),store.save(args)]);
  expect(results[0]).toEqual(results[1]);
  expect((await store.read({project:p.project})).notes).toHaveLength(1);
});
it("repository-wide conflicts do not drop other project entries", async () => {
  const {store,git} = await fixture();
  const results = await Promise.allSettled([store.create({title:"A",source:"test",request_id:"a"}),
    new GitIntake(git,"owner").create({title:"B",source:"test",request_id:"b"})]);
  expect(results.filter(r => r.status === "fulfilled")).toHaveLength(1);
  const failed = results[0].status === "rejected" ? {title:"A",request_id:"a"} : {title:"B",request_id:"b"};
  await store.create({...failed,source:"test"});
  expect((await store.list()).projects).toHaveLength(3);
});
it("correction preserves classification and immutable history; quotes cannot approve", async () => {
  const {git,store,p,args} = await fixture();
  const first = await store.save(args);
  await expect(store.save({...args,kind:"correction",evidence:"model_inference",quote:"",request_id:"bad",expected_revision:1,supersedes:first.note})).rejects.toThrow("cannot_replace");
  await store.save({...args,kind:"correction",request_id:"good",expected_revision:1,supersedes:first.note,quote:"  正確な原文  "});
  const view = await store.read({project:p.project});
  expect(view.binding).toBe(false); expect(view.notes).toHaveLength(1);
  expect(view.notes[0]).toMatchObject({kind:"constraint",captured_kind:"correction",quote:"  正確な原文  "});
  expect(await git.read(git.branch,`projects/${p.project}/records/${first.note}.json`)).not.toBeNull();
  await expect(store.save({...args,request_id:"stale-target",expected_revision:2,supersedes:first.note})).rejects.toThrow("target_unavailable");
});
it("permissions gate metadata, old pages and idempotent replays", async () => {
  const {git,store,p,args} = await fixture(); await store.save(args);
  const catalog = await git.read(git.branch,"nexus.json") as any;
  catalog.projects[0].remote = false;
  await git.commit(git.branch,{"nexus.json":catalog});
  expect((await store.list()).projects).toEqual([]);
  await expect(store.read({project:p.project})).rejects.toThrow("project_unavailable");
  await expect(store.save(args)).rejects.toThrow("project_unavailable");
  await expect(store.create({title:"試験",source:"synthetic",request_id:"create"})).rejects.toThrow("project_unavailable");
  await expect(new GitIntake(git,"stranger").list()).rejects.toThrow("canonical_invalid");
});
it("distinguishes title no-match, an empty later page, and unavailable search", async () => {
  const {git,store,p}=await fixture();
  expect(await store.list({query:"   "})).toMatchObject({search:{status:"not_searched",scope:"project_titles",total_matches:1}});
  expect(await store.list({query:"unrelated"})).toMatchObject({projects:[],search:{status:"no_match",total_matches:0}});
  const match=await store.list({query:"試験"});
  expect(match.projects.map(x=>x.id)).toEqual([p.project]);
  expect(match.search).toEqual({status:"matches",scope:"project_titles",total_matches:1});
  const afterEnd=await store.list({query:"試験",offset:25,snapshot:match.snapshot});
  expect(afterEnd.projects).toEqual([]);
  expect(afterEnd.search.status).toBe("matches");
  const catalog=await git.read(git.branch,"nexus.json") as any;
  catalog.projects[0].remote=false;
  await git.commit(git.branch,{"nexus.json":catalog});
  expect((await store.list({query:"試験"})).search.status).toBe("no_match");
  git.commits.set(git.branch,{});
  const response=await gitMcp({jsonrpc:"2.0",id:1,method:"tools/call",params:{name:"find_projects",arguments:{query:"試験"}}},store);
  const result=(await response.json() as any).result;
  expect(result.isError).toBe(true);
  expect(JSON.parse(result.content[0].text).status).toBe("search_unavailable");
});
it("paging is tied to a branch snapshot and rejects mutations instead of skipping notes", async () => {
  const {store,p,args} = await fixture();
  for(let i=0;i<11;i++) await store.save({...args,request_id:`s${i}`,expected_revision:i});
  const page = await store.read({project:p.project});
  expect(page.next_offset).toBe(10);
  await expect(store.read({project:p.project,offset:10})).rejects.toThrow("snapshot_required");
  expect((await store.read({project:p.project,offset:10,snapshot:page.snapshot})).notes).toHaveLength(1);
  await store.save({...args,request_id:"after-page",expected_revision:11});
  await expect(store.read({project:p.project,offset:10,snapshot:page.snapshot})).rejects.toThrow("snapshot_changed");
});
it("overview is stale after another note; fresh conversations always get current notes", async () => {
  const {store,p,args} = await fixture();
  await store.save({...args,kind:"proposal",evidence:"model_inference",quote:"",body:"【画面用の概要】\n概要"});
  expect((await store.read({project:p.project})).overview?.current).toBe(true);
  await store.save({...args,request_id:"after",expected_revision:1});
  const read = await store.read({project:p.project});
  expect(read.overview?.current).toBe(false);
  expect(read.notes).toHaveLength(2);
  expect(read.context.categories.learning).toBe("not_configured");
});
it("returns bounded current changes and removals from a same-conversation baseline",async()=>{
  const {git,store,p,args}=await fixture();
  const first=await store.save(args);
  const baseline=await store.read({project:p.project});
  const corrected=await store.save({...args,kind:"correction",body:"無料の範囲を訂正",quote:"無料の範囲を訂正",
    expected_revision:1,request_id:"correct",supersedes:first.note});
  const correctedAgain=await store.save({...args,kind:"correction",body:"無料の範囲を再訂正",quote:"無料の範囲を再訂正",
    expected_revision:2,request_id:"correct-again",supersedes:corrected.note});
  const added=await store.save({...args,kind:"proposal",evidence:"model_inference",quote:"",body:"次の案",
    expected_revision:3,request_id:"add"});
  const delta=await store.read({project:p.project,detail:"changes",since_revision:baseline.revision,
    known_snapshot:baseline.snapshot});
  expect(delta.status).toBe("changes");
  expect(delta.removed_note_ids).toEqual([first.note]);
  expect(delta.notes.map(n=>n.id)).toEqual([correctedAgain.note,added.note]);
  expect(delta.context.complete).toBe(false);
  const unchanged=await store.read({project:p.project,detail:"changes",since_revision:delta.revision,
    known_snapshot:delta.snapshot});
  expect(unchanged).toMatchObject({status:"unchanged",notes:[],removed_note_ids:[]});
  await store.create({title:"unrelated",source:"synthetic",request_id:"other-project"});
  expect((await store.read({project:p.project,detail:"changes",since_revision:delta.revision,
    known_snapshot:delta.snapshot})).status).toBe("unchanged");
  expect((await store.read({project:p.project})).notes.map(n=>n.id)).toEqual([correctedAgain.note,added.note]);
  await expect(store.read({project:p.project,detail:"changes",since_revision:1})).rejects.toThrow("prior_full_read");
  const current=git.commits.get(git.branch)!;
  delete current[`projects/${p.project}/changes/2.json`];
  expect(await store.read({project:p.project,detail:"changes",since_revision:baseline.revision,
    known_snapshot:baseline.snapshot})).toMatchObject({status:"full_read_required",reason:"change_history_unavailable"});
});
it("omits unchanged context bytes but returns changed or unavailable context",async()=>{
  const {git,store,p,args}=await fixture();
  const source=await store.create({title:"Synthetic source",source:"synthetic",request_id:"context-source"});
  const rule=await store.save({project:source.project,kind:"proposal",body:"Synthetic rule",source:"synthetic",
    evidence:"model_inference",quote:"",expected_revision:0,request_id:"context-rule"});
  const catalog=await git.read(git.branch,"nexus.json") as any;
  catalog.context_revision=1;
  const required={global_rules:["rule"],personal:[],learning:[],relations:[]};
  const context={schema:1,mode:"synthetic",owner:"owner",generation:"test-1",revision:1,
    profiles:[{project:p.project,operations:["resume"],required}],
    entries:[{id:"rule",category:"global_rules",projects:[p.project],operations:["resume"],status:"active",
      source:{project:source.project,note:rule.note,revision:1},evidence:[]}]};
  await git.commit(git.branch,{"nexus.json":catalog,"context.json":context});
  const baseline=await store.read({project:p.project});
  expect(baseline.context.items).toHaveLength(1);
  expect(baseline.context_digest).toMatch(/^[a-f0-9]{64}$/);
  await store.save(args);
  const cached=await store.read({project:p.project,detail:"changes",since_revision:baseline.revision,
    known_snapshot:baseline.snapshot,known_context_digest:baseline.context_digest});
  expect(cached.context).toMatchObject({complete:true,unchanged:true,items_omitted:true,items:[]});
  expect(cached.context_digest).toBe(baseline.context_digest);
  expect((await store.read({project:p.project,detail:"changes",since_revision:baseline.revision,
    known_snapshot:baseline.snapshot})).context.items).toHaveLength(1);
  await store.save({project:source.project,kind:"correction",body:"Synthetic revised rule",source:"synthetic",
    evidence:"model_inference",quote:"",expected_revision:1,request_id:"context-revise",supersedes:rule.note});
  const changed=await store.read({project:p.project,detail:"changes",since_revision:baseline.revision,
    known_snapshot:baseline.snapshot,known_context_digest:baseline.context_digest});
  expect(changed.context.complete).toBe(false);
  expect(changed.context_digest).not.toBe(baseline.context_digest);
  expect(changed.context).not.toHaveProperty("items_omitted");
});
it.each(['goal','acceptance','source'])('non-user corrections cannot replace an attributed user %s, including correction chains',async kind=>{
  const {store,p,args,git}=await fixture();
  const first=await store.save({...args,kind});
  const corrected=await store.save({...args,kind:'correction',expected_revision:1,request_id:'user-correction',supersedes:first.note});
  const head=await git.head();
  for(const evidence of ['model_inference','external_source'])
    await expect(store.save({...args,kind:'correction',evidence,quote:'',expected_revision:2,
      request_id:`bad-${evidence}`,supersedes:corrected.note})).rejects.toThrow('cannot_replace');
  expect(await git.head()).toBe(head);
  expect((await store.read({project:p.project})).notes).toMatchObject([{id:corrected.note,evidence:'user_statement'}]);
});
it('model-authored draft goals remain correctable and alternatives can coexist with user goals',async()=>{
  const {store,p,args}=await fixture();
  const first=await store.save({...args,kind:'goal',evidence:'model_inference',quote:''});
  await store.save({...args,kind:'correction',evidence:'model_inference',quote:'',expected_revision:1,
    request_id:'own-revision',supersedes:first.note});
  await store.save({...args,kind:'goal',expected_revision:2,request_id:'user-goal'});
  await store.save({...args,kind:'proposal',evidence:'model_inference',quote:'',expected_revision:3,request_id:'alternative'});
  expect((await store.read({project:p.project})).notes).toHaveLength(3);
});
it("status-only reads fetch just the current summary and never imply complete context", async () => {
  const {store,git,p,args}=await fixture();
  expect((await store.read({project:p.project,detail:"overview"})).overview_status).toBe("missing");
  await store.save(args);
  const summary=await store.save({...args,request_id:"summary",expected_revision:1,kind:"proposal",evidence:"model_inference",quote:"",body:"【画面用の概要】\n現在地"});
  const paths:string[]=[]; const original=git.read.bind(git);
  git.read=async(c,path)=>{paths.push(path);return original(c,path);};
  const view=await store.read({project:p.project,detail:"overview"});
  expect(view).toMatchObject({read_scope:"overview",notes:[],notes_omitted:true,next_offset:null,overview_status:"current",context:{complete:false,status:"not_evaluated_overview"}});
  expect(paths).toEqual(["nexus.json",`projects/${p.project}/manifest.json`,`projects/${p.project}/records/${summary.note}.json`]);
  for(const operation of ["implement","review","plan"])await expect(store.read({project:p.project,detail:"overview",operation})).rejects.toThrow("status_only");
  await expect(store.read({project:p.project,detail:"overview",offset:10,snapshot:view.snapshot})).rejects.toThrow("status_only");
  await store.save({...args,request_id:"new-note",expected_revision:2});
  expect(await store.read({project:p.project,detail:"overview"})).toMatchObject({overview:null,overview_status:"stale"});
  await expect(store.read({project:p.project,detail:"overview",snapshot:view.snapshot})).rejects.toThrow("snapshot_changed");
  const files=git.commits.get(git.branch)! as any; files['nexus.json'].projects[0].remote=false;
  await expect(store.read({project:p.project,detail:"overview"})).rejects.toThrow("project_unavailable");
});
it("failed writes and corrupted repositories never fabricate saved or empty results", async () => {
  const {store,git,p,args} = await fixture(); git.failWrite = true;
  await expect(store.save(args)).rejects.toThrow("unavailable");
  expect((await store.read({project:p.project})).revision).toBe(0);
  git.commits.set(git.branch,{});
  await expect(store.list()).rejects.toThrow("canonical_invalid");
});
it("MCP tools work without a connector and reject authority or shell arguments", async () => {
  const {store,p,args} = await fixture();
  const call = async (name:string,a:unknown) => (await gitMcp({jsonrpc:"2.0",id:1,method:"tools/call",params:{name,arguments:a}},store)).json() as Promise<any>;
  expect((await call("save_project_note",args)).result.structuredContent.revision).toBe(1);
  expect((await call("read_project",{project:p.project})).result.structuredContent.binding).toBe(false);
  expect((await call("save_project_note",{...args,authority:"confirmed"})).result.isError).toBe(true);
  expect((await call("execute_shell",{})).result.isError).toBe(true);
});

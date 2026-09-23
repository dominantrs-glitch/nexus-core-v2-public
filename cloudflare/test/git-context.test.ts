import { it, expect } from "vitest";
import { GitIntake, type GitBackend } from "../src/git-store";
import { contextCategories, type ContextNote } from "../src/git-context";
import { hash } from "../src/relay-common";

class DataGit implements GitBackend {
  serial = 1;
  files: Record<string, any> = {};
  async head() { return this.serial.toString(16).padStart(40, "0"); }
  async read(_commit: string, path: string) { return structuredClone(this.files[path] ?? null); }
  async commit(base: string, files: Record<string, unknown>) {
    if (base !== await this.head()) throw new Error("conflict");
    Object.assign(this.files, structuredClone(files)); this.serial++;
  }
}
async function fixture() {
  const git = new DataGit();
  const operations = ["resume", "plan", "implement", "review"];
  const categories = [...contextCategories];
  const notes: ContextNote[] = [...categories, "proof"].map((name,i) => ({ id: "n-"+name, project: "sources", revision: i+1,
    kind: name === "personal" ? "preference" : name === "proof" ? "source" : "proposal",
    evidence: name === "personal" ? "user_statement" : name === "proof" ? "external_source" : "model_inference",
    body: "synthetic " + name, source: "synthetic fixture", quote: name === "personal" ? "synthetic preference" : "",
    supersedes: null, created: "2026-09-23" }));
  git.files["nexus.json"] = { schema:1,mode:"synthetic",owner:"owner",generation:"test",context_revision:1,
    projects:[{id:"target",title:"target",revision:0,remote:true},{id:"sources",title:"sources",revision:5,remote:true}] };
  for (const p of git.files["nexus.json"].projects) git.files[`projects/${p.id}/manifest.json`] = {...p,source:"synthetic",
    current:p.id === "sources" ? notes.map(n => n.id) : [],overview:null};
  for (const n of notes) git.files[`projects/sources/records/${n.id}.json`] = {...n,captured_kind:n.kind};
  const refs = notes.map(n => ({project:n.project,note:n.id,revision:n.revision}));
  const required = Object.fromEntries(categories.map(c => [c,[c]]));
  git.files["context.json"] = {schema:1,mode:"synthetic",owner:"owner",generation:"test",revision:1,
    profiles:[{project:"target",operations,required}],
    entries:categories.map((c,i) => ({id:c,category:c,projects:["target"],operations,status:"active",source:refs[i],
      evidence:c === "learning" ? [refs[4]] : []}))};
  return {git,store:new GitIntake(git,"owner"),config:git.files["context.json"]};
}
it("ordinary read includes scoped current context and attributed learning evidence", async () => {
  const {store} = await fixture();
  const view = await store.read({project:"target"});
  expect(view.context).toMatchObject({complete:true,status:"resolved",binding:false});
  expect(view.context.items).toHaveLength(4);
  expect(view.context.items[2]).toMatchObject({authority:"candidate",binding:false,evidence:[{id:"n-proof"}],
    verification:"source-references-only-not-native-validated-learning"});
  expect(view.context.items[1]).toMatchObject({note:{quote:"synthetic preference",evidence:"user_statement"}});
});
it("real intake resume routing does not imply implementation readiness or expand project scope", async () => {
  const {git,config} = await fixture();
  git.files["nexus.json"].mode = config.mode = "draft-intake";
  config.profiles[0].operations = ["resume"];
  for (const entry of config.entries) entry.operations = ["resume"];
  const store = new GitIntake(git,"owner","test","draft-intake");
  expect((await store.read({project:"target",operation:"resume"})).context)
    .toMatchObject({complete:true,binding:false,operation:"resume"});
  for (const operation of ["plan","implement","review"])
    expect((await store.read({project:"target",operation})).context)
      .toMatchObject({complete:false,status:"not_configured",items:[]});
  expect((await store.read({project:"sources"})).context.complete).toBe(false);
  expect((await store.read({project:"target",detail:"overview"})).context)
    .toMatchObject({complete:false,status:"not_evaluated_overview",items:[]});
});
it("unconfigured is different from searched with no applicable context", async () => {
  const {store,config,git} = await fixture();
  expect((await store.read({project:"sources"})).context.status).toBe("not_configured");
  config.profiles.push({project:"sources",operations:["resume"],required:Object.fromEntries(contextCategories.map(c => [c,[]]))});
  expect((await store.read({project:"sources"})).context).toMatchObject({complete:true,categories:{personal:"no_match"},items:[]});
  delete git.files["nexus.json"].context_revision;
  expect((await store.read({project:"target"})).context.complete).toBe(false);
});
it.each(["independent","red-team"])("%s excludes personal and learning but retains required rules and relations",async mode => {
  const {store,config} = await fixture();
  config.entries[1].source.project = "denied";
  const result = (await store.read({project:"target",mode})).context;
  expect(result.complete).toBe(true);
  expect(result.items).toHaveLength(2);
  expect(result.categories.personal).toBe("excluded_by_mode");
  expect(JSON.stringify(result)).not.toContain("synthetic preference");
});
it.each(["revoked","stale","missing","scope","operation","suppressed"])("required %s context blocks without disclosing source details",async failure => {
  const {store,config,git} = await fixture();
  if (failure === "revoked") git.files["nexus.json"].projects[1].remote = false;
  if (failure === "stale") git.files["projects/sources/manifest.json"].current = ["n-global_rules","n-learning","n-relations","n-proof"];
  if (failure === "missing") delete git.files["projects/sources/records/n-personal.json"];
  if (failure === "scope") config.entries[1].projects = ["elsewhere"];
  if (failure === "operation") config.entries[1].operations = ["review"];
  if (failure === "suppressed") config.entries[1].status = "suppressed";
  const result = (await store.read({project:"target"})).context;
  expect(result).toMatchObject({complete:false,status:"insufficient_context",categories:{personal:"required_unavailable"}});
  expect(JSON.stringify(result)).not.toContain("synthetic preference");
});
it("optional inaccessible context reports omission without blocking unrelated work",async () => {
  const {store,config} = await fixture(); config.profiles[0].required.personal=[]; config.entries[1].source.project="hidden";
  const result=(await store.read({project:"target"})).context;
  expect(result).toMatchObject({complete:true,categories:{personal:"optional_unavailable"}});
  expect(JSON.stringify(result)).not.toContain("hidden");
});
it("learning with missing/stale evidence or wrong attribution is withheld",async () => {
  const {store,git} = await fixture();
  git.files["projects/sources/records/n-proof.json"].revision=4;
  expect((await store.read({project:"target"})).context).toMatchObject({complete:false,categories:{learning:"required_unavailable"}});
  git.files["projects/sources/records/n-proof.json"].revision=5;
  git.files["projects/sources/records/n-learning.json"].evidence="user_statement";
  expect((await store.read({project:"target"})).context.complete).toBe(false);
});
it("invalid source attribution is never supplied as a personal requirement",async () => {
  const {store,git} = await fixture();
  git.files["projects/sources/records/n-personal.json"].quote="";
  const result=(await store.read({project:"target"})).context;
  expect(result).toMatchObject({complete:false,categories:{personal:"required_unavailable"}});
  expect(JSON.stringify(result)).not.toContain("synthetic preference");
});
it.each(["revision","generation","owner","duplicate","missing"])("rejects %s routing configuration",async failure => {
  const {store,git,config} = await fixture();
  if (failure === "revision") config.revision=2;
  if (failure === "generation") config.generation="old";
  if (failure === "owner") config.owner="stranger";
  if (failure === "duplicate") config.entries.push(config.entries[0]);
  if (failure === "missing") delete git.files["context.json"];
  expect((await store.read({project:"target"})).context).toMatchObject({complete:false,status:"invalid_context_manifest",items:[]});
});
it("bounds output and ties context reads to the same repository snapshot",async () => {
  const {store,git} = await fixture(); const before=await git.head();
  for(const key of Object.keys(git.files).filter(k=>k.includes("/records/"))) git.files[key].body="x".repeat(8000);
  expect((await store.read({project:"target"})).context.status).toBe("context_budget_exceeded");
  git.serial++;
  await expect(store.read({project:"target",snapshot:before})).rejects.toThrow("snapshot_changed");
});
it("draft tools cannot set context policy; ordinary saves preserve its pinned revision",async () => {
  const {store,git} = await fixture();
  await expect(store.save({project:"target",context_revision:2})).rejects.toThrow();
  await store.save({project:"target",kind:"proposal",body:"test",source:"synthetic",evidence:"model_inference",quote:"",expected_revision:0,request_id:"save"});
  expect(git.files["nexus.json"].context_revision).toBe(1);
  expect((await store.read({project:"target"})).context.complete).toBe(true);
});

it("only an explicit trusted profile can raise the default context byte budget",async()=>{
  const {store,git,config}=await fixture();
  for(const key of Object.keys(git.files).filter(k=>k.includes('/records/')))git.files[key].body='x'.repeat(7000);
  expect((await store.read({project:'target'})).context.status).toBe('context_budget_exceeded');
  config.profiles[0].max_bytes=40960;
  const view=await store.read({project:'target'});
  expect(view.context.complete).toBe(true);
  expect(new TextEncoder().encode(JSON.stringify(view.context.items)).length).toBeGreaterThan(32768);
  await expect(store.read({project:'target',max_bytes:49152})).rejects.toThrow();
  config.profiles.push({project:'*',operations:['resume'],required:{global_rules:[],personal:[],learning:[],relations:[]},max_bytes:32768});
  expect((await store.read({project:'target'})).context.status).toBe('context_budget_exceeded');
});
it.each([1023,49153,40960.5])('invalid trusted profile budget %s fails closed',async max_bytes=>{
  const {store,config}=await fixture();config.profiles[0].max_bytes=max_bytes;
  expect((await store.read({project:'target'})).context).toMatchObject({complete:false,status:'invalid_context_manifest',items:[]});
});

async function originalFixture() {
  const f=await fixture();
  const content="\ufeff# 架空の原資料\r\n改行とBOMも保持。\r\n";
  const sha256=await hash(content);
  const source={project:"sources",original:"required-source",revision:1,sha256};
  const entry={id:"required-source",revision:1,sha256,remote:true,projects:["target"],operations:["resume"]};
  const record={schema:1,project:"sources",id:"required-source",revision:1,sha256,content,
    title:"Synthetic original",source:"synthetic test fixture",captured_at:"2026-09-23T00:00:00Z",
    media_type:"text/markdown",encoding:"utf-8",authority:"source-document-not-native-confirmation"};
  const path="projects/sources/originals/required-source/1.json";
  const originalManifest={schema:1,owner:"owner",generation:"test",mode:"synthetic",project:"sources",current:[entry]};
  f.git.files["projects/sources/originals/manifest.json"]=originalManifest;
  f.git.files[path]=record;
  f.config.entries[0].source=source;
  return {...f,record,entry,originalManifest,path,content};
}
it("required UTF-8 original is delivered byte-exact at the same snapshot without approval authority",async()=>{
  const {store,record,content}=await originalFixture();
  const view=await store.read({project:"target"});
  expect(view.context).toMatchObject({complete:true,categories:{global_rules:"selected"}});
  expect(view.context.items[0]).toMatchObject({original:record,binding:false,
    authority:"source-document-not-native-confirmation",verification:"exact-utf8-bytes-at-this-git-snapshot"});
  expect(new TextEncoder().encode((view.context.items[0] as any).original.content))
    .toEqual(new TextEncoder().encode(content));
  expect((await store.read({project:"target",detail:"overview"})).context.items).toEqual([]);
});
it.each(["missing","changed_bytes","hash","stale","denied","operation","scope","project_denied","generation","owner","duplicate","malformed_unicode","oversized"])
  ("required original %s blocks and withholds original text",async failure=>{
  const {store,git,record,entry,originalManifest,path,content}=await originalFixture();
  if(failure==="missing")delete git.files[path];
  if(failure==="changed_bytes")record.content=content.replaceAll("\r\n","\n");
  if(failure==="hash")record.sha256="f".repeat(64);
  if(failure==="stale")entry.revision=2;
  if(failure==="denied")entry.remote=false;
  if(failure==="operation")entry.operations=["review"];
  if(failure==="scope")entry.projects=["sources"];
  if(failure==="project_denied")git.files["nexus.json"].projects[1].remote=false;
  if(failure==="generation")originalManifest.generation="old";
  if(failure==="owner")originalManifest.owner="other";
  if(failure==="duplicate")originalManifest.current.push(entry);
  if(failure==="malformed_unicode")record.content="\ud800";
  if(failure==="oversized")record.content="あ".repeat(8193);
  const view=await store.read({project:"target"});
  expect(view.context).toMatchObject({complete:false,categories:{global_rules:"required_unavailable"}});
  expect(JSON.stringify(view.context)).not.toContain("Synthetic original");
  expect(JSON.stringify(view.context)).not.toContain("required-source");
});
it("text originals cannot masquerade as validated learning or native approval",async()=>{
  const {store,config,record}=await originalFixture();
  config.entries[0].category="learning";
  config.profiles[0].required.learning.push("global_rules");
  expect((await store.read({project:"target"})).context.categories.learning).toBe("required_unavailable");
  config.entries[0].category="global_rules";
  (record as any).authority="confirmed";
  expect((await store.read({project:"target"})).context.categories.global_rules).toBe("required_unavailable");
});

it('lists scoped originals without contents, then retrieves the same byte-exact current original',async()=>{
  const {store,record,content}=await originalFixture();
  const listed=await store.original({project:'target',source_project:'sources'});
  expect(listed).toMatchObject({status:'listed',context_evaluated:false,binding:false,next_offset:null});
  if(!('originals' in listed))throw new Error('expected listing');
  expect(listed.originals).toHaveLength(1);
  expect(listed.originals[0]).not.toHaveProperty('content');
  expect(listed.originals[0]).toMatchObject({id:record.id,sha256:record.sha256,bytes:new TextEncoder().encode(content).length});
  const result=await store.original({project:'target',source_project:'sources',detail:'content',
    original:record.id,revision:1,sha256:record.sha256,snapshot:listed.snapshot});
  expect(result).toMatchObject({status:'retrieved',original:record,verification:'exact-utf8-bytes-at-this-git-snapshot'});
});

it.each(['operation','target_scope','source_denied','target_denied','revoked','changed','corrupt','missing'])
  ('explicit original retrieval respects %s after listing',async failure=>{
    const {store,git,record,entry,path}=await originalFixture();
    const listed=await store.original({project:'target',source_project:'sources'});
    if(failure==='operation')entry.operations=['review'];
    if(failure==='target_scope')entry.projects=['sources'];
    if(failure==='source_denied')git.files['nexus.json'].projects.find((p:any)=>p.id==='sources').remote=false;
    if(failure==='target_denied')git.files['nexus.json'].projects.find((p:any)=>p.id==='target').remote=false;
    if(failure==='revoked')entry.remote=false;
    if(failure==='changed')entry.revision=2;
    if(failure==='corrupt')record.content='modified';
    if(failure==='missing')delete git.files[path];
    await expect(store.original({project:'target',source_project:'sources',detail:'content',
      original:record.id,revision:1,sha256:record.sha256,snapshot:listed.snapshot})).rejects.toThrow(/unavailable/);
  });

it('separates unavailable original content from an unconfigured listing and rejects URL/path input',async()=>{
  const {store,record}=await originalFixture();
  expect(await store.original({project:'target'})).toMatchObject({status:'not_configured',originals:[]});
  await expect(store.original({project:'target',detail:'content'})).rejects.toThrow('invalid_original_request');
  await expect(store.original({project:'target',url:'https://example.invalid'})).rejects.toThrow();
  await expect(store.original({project:'target',original:'../secret',revision:1,sha256:record.sha256,detail:'content'})).rejects.toThrow();
});

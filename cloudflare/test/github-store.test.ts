import {it,expect} from "vitest";
import {GitHubBackend} from "../src/github-store";
import {GitIntake} from "../src/git-store";
import {FakeGitHub} from "./git-fixture";

it("HTTP adapter requests only selected repository contents and commits against exact parent",async()=>{
  const fake = await new FakeGitHub().init();
  const store = new GitIntake(new GitHubBackend(fake.config,fake.fetch),"owner");
  const p = await store.create({title:"架空",source:"synthetic",request_id:"c"});
  const read = await store.read({project:p.project});
  expect(read.title).toBe("架空");
  expect(read.snapshot).toBe(fake.branch);
  const token = fake.calls.find(c=>c.path.includes("access_tokens"))!;
  expect(token.body).toEqual({repository_ids:[42],permissions:{contents:"write"}});
  expect(fake.calls.every(c=>c.redirect==="manual")).toBe(true);
  expect(fake.calls.find(c=>c.path.endsWith("/git/commits")&&c.method==="POST")!.body.parents).toEqual(["0".repeat(39)+"1"]);
  expect(fake.calls.find(c=>c.method==="PATCH")!.body.force).toBe(false);
});
it("HTTP response-loss recovery uses reachable Git receipt with a new adapter",async()=>{
  const fake = await new FakeGitHub().init();fake.losePatchReply=true;
  const args = {title:"架空",source:"synthetic",request_id:"c"};
  const saved = await new GitIntake(new GitHubBackend(fake.config,fake.fetch),"owner").create(args);
  expect(await new GitIntake(new GitHubBackend(fake.config,fake.fetch),"owner").create(args)).toEqual(saved);
  expect(fake.calls.filter(c=>c.method==="PATCH")).toHaveLength(1);
});
it("HTTP adapter reads immutable change events at the current shared head",async()=>{
  const fake=await new FakeGitHub().init();
  const store=new GitIntake(new GitHubBackend(fake.config,fake.fetch),"owner");
  const p=await store.create({title:"架空",source:"synthetic",request_id:"delta-create"});
  const baseline=await store.read({project:p.project});
  const saved=await store.save({project:p.project,kind:"proposal",body:"架空の変更",source:"synthetic",
    evidence:"model_inference",quote:"",expected_revision:0,request_id:"delta-save"});
  const delta=await store.read({project:p.project,detail:"changes",since_revision:baseline.revision,
    known_snapshot:baseline.snapshot});
  expect(delta).toMatchObject({status:"changes",removed_note_ids:[]});
  expect(delta.notes.map(n=>n.id)).toEqual([saved.note]);
  expect(fake.calls.some(c=>c.path.endsWith(`/changes/1.json`))).toBe(true);
});
it("HTTP concurrent writes reject stale parents and preserve current catalog",async()=>{
  const fake = await new FakeGitHub().init();
  const results = await Promise.allSettled(["a","b"].map(name=>new GitIntake(new GitHubBackend(fake.config,fake.fetch),"owner")
    .create({title:name,source:"test",request_id:name})));
  const saved=results.filter(r=>r.status==="fulfilled");
  // Both may succeed if the second begins after the first commits. If they
  // race on one parent, only one succeeds; neither outcome may lose a receipt.
  expect(saved.length).toBeGreaterThanOrEqual(1);
  expect(saved.length).toBeLessThanOrEqual(2);
  expect((await new GitIntake(new GitHubBackend(fake.config,fake.fetch),"owner").list()).projects)
    .toHaveLength(saved.length);
});
it("public repos, redirects and outages fail closed without fallback caches",async()=>{
  const fake = await new FakeGitHub().init();
  const make = ()=>new GitIntake(new GitHubBackend(fake.config,fake.fetch),"owner");
  fake.privateRepo=false;await expect(make().list()).rejects.toThrow("private_repository");
  fake.privateRepo=true;fake.redirectToken=true;
  await expect(make().list()).rejects.toThrow("canonical_unavailable");
  fake.redirectToken=false;fake.failReads=true;
  await expect(make().list()).rejects.toThrow("canonical_unavailable");
});

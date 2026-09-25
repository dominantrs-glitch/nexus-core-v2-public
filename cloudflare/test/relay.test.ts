import { env as bindings } from "cloudflare:workers";
import { applyD1Migrations, createExecutionContext, runInDurableObject, type D1Migration } from "cloudflare:test";
import { beforeAll, beforeEach, afterEach, it, expect, vi } from "vitest";
import worker from "../src/relay";
import { hash, MAX_RESPONSE, DAILY_REQUESTS, DAILY_RESPONSE, REFRESH_TTL, CLIENT_TTL, reserve, type RelayEnv } from "../src/relay-common";
import { FakeGitHub } from "./git-fixture";
import { createPrivateKey } from "node:crypto";
import { GitHubBackend } from "../src/github-store";
import {GitCoreStore} from "../src/git-core";

const raw = bindings as unknown as RelayEnv & { TEST_MIGRATIONS: D1Migration[]; TEST_NATIVE:any; TEST_NATIVE_LEARNING:any };
const origin = "https://nexus.invalid", redirect = "https://client.example/callback";
const connector = "a".repeat(43);
let env: RelayEnv;
const sockets: WebSocket[] = [];
beforeAll(async () => { await applyD1Migrations(raw.DB, raw.TEST_MIGRATIONS); });
beforeEach(async () => {
  env = { ...raw, RELAY_ENABLED: "true", PUBLIC_ORIGIN: origin, OWNER_ID: "123", PROJECT_ID: "synthetic-probe",
    GITHUB_CLIENT_ID: "client", GITHUB_CLIENT_SECRET: "synthetic-only", CONNECTOR_TOKEN_SHA256: await hash(connector),
    ALLOWED_REDIRECTS: JSON.stringify([redirect]) };
  await raw.DB.exec("DELETE FROM relay_budget; DELETE FROM relay_flows; DELETE FROM relay_grants; DELETE FROM relay_used_codes;");
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    // Keep workerd's real Request validation even though upstream replies are mocked.
    const url = new Request(input, init).url;
    if (url === "https://github.com/login/oauth/access_token") return Response.json({ access_token: "synthetic-github-token", scope: "" });
    if (url === "https://api.github.com/user") return Response.json({ id: 123 });
    throw new Error("unexpected external fetch");
  });
});
afterEach(async () => {
  for (const socket of sockets.splice(0)) socket.close(1000, "test complete");
  vi.restoreAllMocks();
});
function request(path: string, init: RequestInit = {}) {
  return worker.fetch(new Request(origin + path, init), env, createExecutionContext());
}
function cookie(response: Response) { return response.headers.get("Set-Cookie")!.split(";")[0]; }
async function flow(response: Response) { return (await response.text()).match(/name="flow" value="([^"]+)"/)![1]; }
async function authorize(existingClient?: string) {
  const registered = existingClient ? null : await request("/oauth/register", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_name: "Synthetic client", redirect_uris: [redirect],
      grant_types: ["authorization_code", "refresh_token"], response_types: ["code"], token_endpoint_auth_method: "none" }) });
  const client = registered ? await registered.json() as any : {client_id: existingClient};
  if (registered) expect(registered.status).toBe(201);
  const verifier = "v".repeat(43);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const challenge = btoa(String.fromCharCode(...new Uint8Array(digest))).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
  const query = new URLSearchParams({ client_id: client.client_id, redirect_uri: redirect, response_type: "code",
    scope: "nexus:read", state: "client-state", code_challenge: challenge, code_challenge_method: "S256", resource: origin + "/mcp" });
  const start = await request("/authorize?" + query);
  expect(start.status).toBe(200);
  expect(start.headers.get("Referrer-Policy")).toBe("same-origin");
  expect(start.headers.get("Content-Security-Policy")).toBe("default-src 'none'; form-action 'self' https://github.com; frame-ancestors 'none'; base-uri 'none'");
  const login = await request("/login", { method: "POST", headers: { Origin: origin, Cookie: cookie(start) },
    body: new URLSearchParams({ flow: await flow(start) }) });
  expect(login.status).toBe(302);
  const github = new URL(login.headers.get("Location")!);
  expect(github.searchParams.has("scope")).toBe(false);
  const callbackPath = "/callback?" + new URLSearchParams({ state: github.searchParams.get("state")!, code: "synthetic-code" });
  const consent = await request(callbackPath, { headers: { Cookie: cookie(login) } });
  expect(consent.status).toBe(200);
  const consentText = await consent.clone().text();
  expect(consentText).toContain("記録の読み取り・保存");
  expect(consentText).not.toContain("<h1>読み取りを許可</h1>");
  expect(consent.headers.get("Referrer-Policy")).toBe("same-origin");
  expect(consent.headers.get("Content-Security-Policy")).toBe("default-src 'none'; form-action 'self' https://client.example; frame-ancestors 'none'; base-uri 'none'");
  const approval = await request("/approve", { method: "POST", headers: { Origin: origin, Cookie: cookie(consent) },
    body: new URLSearchParams({ flow: await flow(consent) }) });
  expect(approval.status).toBe(302);
  const callback = new URL(approval.headers.get("Location")!);
  const tokenBody = new URLSearchParams({ grant_type: "authorization_code", code: callback.searchParams.get("code")!,
    client_id: client.client_id, redirect_uri: redirect, code_verifier: verifier, resource: origin + "/mcp" }).toString();
  const token = await request("/oauth/token", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: tokenBody });
  const result = await token.json() as any;
  expect(token.status).toBe(200);
  expect(result.access_token).toBeTruthy();
  return { token: result.access_token as string, refresh: result.refresh_token as string, client: client.client_id, tokenBody, callbackPath, oldCookie: cookie(login) };
}
async function connectPC() {
  const response = await request("/connect", { headers: { Upgrade: "websocket", Authorization: "Bearer " + connector } });
  expect(response.status).toBe(101);
  const socket = response.webSocket!; socket.accept(); sockets.push(socket); return socket;
}

it("preserves a live connector and replaces only a stale one", async () => {
  await connectPC();
  const connectAgain = () => request("/connect", { headers: { Upgrade: "websocket", Authorization: "Bearer " + connector } });
  expect((await connectAgain()).status).toBe(409);
  const stub = env.TUNNEL.get(env.TUNNEL.idFromName("single-owner-pc"));
  await runInDurableObject(stub, (_instance, ctx) => {
    ctx.getWebSockets()[0].serializeAttachment({ connectedAt: Date.now() - 91_000, retired: false });
  });
  const replacement = await connectPC();
  const pong = new Promise(resolve => replacement.addEventListener("message", e => resolve(e.data), { once: true }));
  replacement.send("ping");
  expect(await pong).toBe("pong");
  expect((await connectAgain()).status).toBe(409);
});

it("uses recent heartbeats across hibernation instead of connection age", async () => {
  const socket = await connectPC();
  const pong = new Promise(resolve => socket.addEventListener("message", e => resolve(e.data), { once: true }));
  socket.send("ping"); expect(await pong).toBe("pong");
  const stub = env.TUNNEL.get(env.TUNNEL.idFromName("single-owner-pc"));
  await runInDurableObject(stub, (_instance, ctx) => {
    ctx.getWebSockets()[0].serializeAttachment({ connectedAt: Date.now() - 91_000, retired: false });
  });
  expect((await request("/connect", { headers: { Upgrade: "websocket", Authorization: "Bearer " + connector } })).status).toBe(409);
});

it("defaults closed and rejects unauthorized connector", async () => {
  env.RELAY_ENABLED = "false";
  expect((await request("/connect")).status).toBe(503);
  env.RELAY_ENABLED = "true";
  expect((await request("/connect", { headers: { Upgrade: "websocket" } })).status).toBe(401);
  expect((await request("/mcp", { method: "POST", body: "{}" })).status).toBe(401);
});
it("completes real provider PKCE flow and blocks code/callback replay", async () => {
  const auth = await authorize();
  expect((await request("/oauth/token", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: auth.tokenBody })).status).toBe(400);
  const replay = await request(auth.callbackPath, { headers: { Cookie: auth.oldCookie } });
  expect(replay.status).toBe(400);
  expect(await replay.json()).toEqual({ error: "authorization_flow_expired_or_mismatched" });
  const missingCookie = await request(auth.callbackPath);
  expect(missingCookie.status).toBe(400);
  expect(await missingCookie.json()).toEqual({ error: "authorization_flow_cookie_missing" });
  const offline = await request("/mcp", { method: "POST", headers: { Authorization: "Bearer " + auth.token, "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }) });
  expect(offline.status).toBe(503);
  expect(await offline.json()).toEqual({ error: "pc_offline" });
});
it("rejects another GitHub user without granting any access", async () => {
  env.OWNER_ID = "456";
  await expect(authorize()).rejects.toThrow();
  expect(await env.DB.prepare("SELECT COUNT(*) AS n FROM relay_grants").first("n")).toBe(0);
});
it("only forwards the exact handoff UI resource after authentication", async () => {
  const auth = await authorize();
  const read = (uri: string) => request("/mcp", { method: "POST",
    headers: { Authorization: "Bearer " + auth.token, "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "resources/read", params: { uri } }) });
  for (const uri of ["file:///secrets", "ui://nexus/original-handoff-v5.html?x=1", "https://example.com", "nexus://original/private"]) {
    expect((await read(uri)).status).toBe(400);
  }
  const allowed = await read("ui://nexus/original-handoff-v5.html");
  expect(allowed.status).toBe(503);
  expect(await allowed.json()).toEqual({ error: "pc_offline" });
});
it("rejects upstream redirects without forwarding credentials", async () => {
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const outbound = new Request(input, init);
    expect(outbound.redirect).toBe("manual");
    return new Response(null, { status: 302, headers: { Location: "https://unexpected.example/" } });
  });
  await expect(authorize()).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(await env.DB.prepare("SELECT COUNT(*) AS n FROM relay_grants").first("n")).toBe(0);
});
it("rejects an unapproved redirect before login", async () => {
  env.ALLOWED_REDIRECTS = "[]";
  await expect(authorize()).rejects.toThrow();
  expect(await env.DB.prepare("SELECT COUNT(*) AS n FROM relay_flows").first("n")).toBe(0);
});
it("revokes grant through strong DB before dispatch", async () => {
  const auth = await authorize();
  await env.DB.prepare("UPDATE relay_grants SET enabled=0 WHERE client_id=?").bind(auth.client).run();
  const response = await request("/mcp", { method: "POST", headers: { Authorization: "Bearer " + auth.token, "Content-Type": "application/json" }, body: "{}" });
  expect(response.status).toBe(403);
});
it("forwards a request and byte-identical large response without persistent blobs", async () => {
  const auth = await authorize();
  const socket = await connectPC();
  const received = new Promise<any>(resolve => socket.addEventListener("message", event => resolve(JSON.parse(event.data as string)), { once: true }));
  const pending = request("/mcp", { method: "POST", headers: { Authorization: "Bearer " + auth.token, "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 7, method: "tools/list" }) });
  const job = await received;
  expect(job.project).toBe("synthetic-probe");
  const bytes = new Uint8Array(30 * 1024 * 1024).fill(97);
  const reply = request("/reply/" + job.id, { method: "POST", headers: { Authorization: "Bearer " + connector,
    "Content-Length": String(bytes.length), "X-Nexus-Status": "200" }, body: bytes });
  const response = await pending;
  expect(response.headers.get("Cache-Control")).toBe("no-store");
  const busy = await request("/mcp", { method: "POST", headers: { Authorization: "Bearer " + auth.token,
    "Content-Type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 8, method: "tools/list" }) });
  expect(busy.status).toBe(503);
  expect(await busy.json()).toEqual({ error: "pc_busy" });
  const result = new Uint8Array(await response.arrayBuffer());
  expect((await reply).status).toBe(204);
  expect(result.length).toBe(bytes.length);
  expect(await crypto.subtle.digest("SHA-256", result)).toEqual(await crypto.subtle.digest("SHA-256", bytes));
  expect((await request("/reply/" + job.id, { method: "POST", headers: { Authorization: "Bearer " + connector } })).status).toBe(409);
});
it("rejects oversized bodies and cross-origin requests", async () => {
  expect((await request("/oauth/register", { method: "POST", body: "x".repeat(16385) })).status).toBe(503);
  expect((await request("/login", { method: "POST", headers: { Origin: "https://evil.example" } })).status).toBe(403);
  expect((await request("/login", { method: "POST", headers: { Origin: "null" } })).status).toBe(403);
  expect((await request("/login", { method: "POST" })).status).toBe(403);
});
it("reserves daily budget atomically", async () => {
  await reserve(env, DAILY_REQUESTS - 1, 0);
  const outcomes = await Promise.allSettled([reserve(env, 1, 0), reserve(env, 1, 0)]);
  expect(outcomes.filter(r => r.status === "fulfilled")).toHaveLength(1);
  expect(MAX_RESPONSE).toBeLessThan(100 * 1024 * 1024);
});
it("reauthorizing the same client preserves other device sessions and explicit revocation", async () => {
  const first = await authorize();
  const second = await authorize(first.client);
  const third = await authorize();
  const call = (token: string) => request("/mcp", {method:"POST",headers:{Authorization:"Bearer "+token,"Content-Type":"application/json"},
    body:JSON.stringify({jsonrpc:"2.0",id:1,method:"ping"})});
  // No PC is connected: passing authentication reaches the disconnected tunnel.
  for (const auth of [first,second,third]) expect((await call(auth.token)).status).toBe(503);
  expect(await env.DB.prepare("SELECT epoch FROM relay_grants WHERE client_id=?").bind(first.client).first("epoch")).toBe(1);
  await env.DB.prepare("UPDATE relay_grants SET epoch=epoch+1,enabled=0 WHERE client_id=?").bind(first.client).run();
  for (const auth of [first,second]) expect((await call(auth.token)).status).toBe(403);
  expect((await call(third.token)).status).toBe(503);
  await expect(authorize(first.client)).rejects.toThrow();
});
it('runs preview, withdrawal, search exclusion and exact restoration through authenticated workerd MCP',async()=>{
  const auth=await authorize(),fake=await new FakeGitHub().init('123');
  Object.assign(env,fake.config,{GIT_WORKSPACE_ENABLED:'true'});
  vi.mocked(fetch).mockImplementation((input,init)=>fake.fetch(input,init));
  const call=async(name:string,args:unknown)=>{
    const response=await request('/mcp',{method:'POST',headers:{Authorization:'Bearer '+auth.token,'Content-Type':'application/json'},
      body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name,arguments:args}})});
    expect(response.status).toBe(200);
    const result=(await response.json() as any).result;
    expect(result.isError).not.toBe(true);return result.structuredContent;
  };
  const {project}=await call('create_project',{title:'Synthetic removal',source:'fixture',request_id:'create'});
  const original=await call('save_project_note',{project,kind:'proposal',body:'synthetic balloon',source:'fixture',
    evidence:'model_inference',quote:'',expected_revision:0,request_id:'save'});
  const input={action:'remove',project,note:original.note,expected_revision:1,quote:'Remove the test note',source:'synthetic owner',reason:'test'};
  const preview=await call('preview_note_removal',input);
  const removed=await call('apply_note_removal',{...input,plan_digest:preview.plan_digest,request_id:'remove'});
  expect((await call('search_project_notes',{project,query:'balloon'})).matches).toEqual([]);
  expect((await call('read_project',{project})).withdrawn_notes[0].note).toBe(removed.note);
  const restore={...input,action:'restore',note:removed.note,expected_revision:2,quote:'Restore the test note'};
  const plan=await call('preview_note_removal',restore);
  await call('apply_note_removal',{...restore,plan_digest:plan.plan_digest,request_id:'restore'});
  expect((await call('read_project',{project})).notes[0]).toMatchObject({body:'synthetic balloon',evidence:'model_inference',quote:''});
});
it("runs authenticated Git draft MCP in workerd without PC and recovers a lost save response", async () => {
  const auth = await authorize();
  const fake = await new FakeGitHub().init("123");
  fake.config.GIT_APP_PRIVATE_KEY = createPrivateKey(fake.config.GIT_APP_PRIVATE_KEY).export({type:"pkcs1",format:"pem"}).toString();
  Object.assign(env,fake.config,{GIT_WORKSPACE_ENABLED:"true"});
  vi.mocked(fetch).mockImplementation(function(this: unknown, input, init) {
    // Native workerd fetch rejects an adapter instance as its receiver. The old
    // arrow-function fake concealed this real-deployment-only failure.
    if (this instanceof GitHubBackend) throw new TypeError("Illegal invocation");
    return fake.fetch(input, init);
  });
  const call = async (name:string,args:unknown) => {
    const response = await request("/mcp",{method:"POST",headers:{Authorization:"Bearer "+auth.token,"Content-Type":"application/json"},
      body:JSON.stringify({jsonrpc:"2.0",id:1,method:"tools/call",params:{name,arguments:args}})});
    expect(response.status).toBe(200);
    return (await response.json() as any).result;
  };
  const created = await call("create_project",{title:"架空案件",source:"synthetic-only",request_id:"cloud-create"});
  const project = created.structuredContent.project;
  const args = {project,kind:"proposal",body:"仮説",source:"synthetic",evidence:"model_inference",quote:"",expected_revision:0,request_id:"cloud-save"};
  fake.losePatchReply=true;
  const saved = await call("save_project_note",args);
  expect(saved.structuredContent.revision).toBe(1);
  expect((await call("save_project_note",args)).structuredContent).toEqual(saved.structuredContent);
  expect((await call("read_project",{project})).structuredContent.notes[0].body).toBe("仮説");
  expect((await call("find_projects",{})).structuredContent.projects).toHaveLength(1);
  const files = fake.objects.get(fake.branch)! as any;
  files["nexus.json"].context_revision = 1;
  files["context.json"] = {schema:1,mode:"synthetic",owner:"123",generation:"test-1",revision:1,
    profiles:[{project,operations:["resume"],required:{global_rules:["rule"],personal:[],learning:[],relations:[]}}],
    entries:[{id:"rule",category:"global_rules",projects:[project],operations:["resume"],status:"active",
      source:{project,note:saved.structuredContent.note,revision:1},evidence:[]}]};
  expect((await call("read_project",{project})).structuredContent.context).toMatchObject({complete:true,
    items:[{note:{body:"仮説"},binding:false}]});
  files["context.json"].entries[0].source.revision = 2;
  expect((await call("read_project",{project})).structuredContent.context).toMatchObject({complete:false,status:"insufficient_context"});
  const content="\ufeff# 架空原文\r\nWorkerでも同じ原文。\r\n", sha256=await hash(content);
  files["context.json"].entries[0].source={project,original:"rules",revision:1,sha256};
  files[`projects/${project}/originals/manifest.json`]={schema:1,owner:"123",generation:"test-1",mode:"synthetic",project,
    current:[{id:"rules",revision:1,sha256,remote:true,projects:[project],operations:["resume"]}]};
  files[`projects/${project}/originals/rules/1.json`]={schema:1,project,id:"rules",revision:1,sha256,
    media_type:"text/markdown",encoding:"utf-8",title:"Synthetic source",source:"workerd fixture",
    captured_at:"2026-09-23T00:00:00Z",content,authority:"source-document-not-native-confirmation"};
  expect((await call("read_project",{project})).structuredContent.context).toMatchObject({complete:true,
    items:[{original:{content,sha256},binding:false}]});
  files[`projects/${project}/originals/rules/1.json`].content="changed";
  const corrupt=(await call("read_project",{project})).structuredContent.context;
  expect(corrupt).toMatchObject({complete:false,status:"insufficient_context"});
  expect(JSON.stringify(corrupt)).not.toContain("Synthetic source");
  // Actual Python-native confirmation fixture, executed by workerd without a PC
  // tunnel. Native source sharing is a separate trusted setting, disabled by default.
  await new GitCoreStore(new GitHubBackend(fake.config),'123','test-1','synthetic','native-owner').write(raw.TEST_NATIVE);
  const nativeFiles=fake.objects.get(fake.branch)! as any;
  nativeFiles['native/catalog.json'].projects[0].shares=[{project,operation:'resume',native_operation:'implement'}];
  nativeFiles['context.json'].entries[0].source={native_project:raw.TEST_NATIVE.project,contract_revision:1,native_operation:'implement'};
  expect((await call('read_project',{project})).structuredContent.context.complete).toBe(false);
  env.GIT_NATIVE_OWNER='native-owner';
  const nativeRead=(await call('read_project',{project})).structuredContent;
  expect(nativeRead.context).toMatchObject({complete:true,items:[{authority:'native-confirmed-source',binding:false,
    native:{confirmation:{kind:'confirmed_contract'},policy_revision:2}}]});
  expect(nativeRead.context.items[0].native.required_context.find((r:any)=>r.id==='synthetic-rule'))
    .toMatchObject({confirmation:{kind:'confirmed_decision'},binding:true});
  expect(JSON.stringify(nativeRead)).not.toContain('capability_id');
  expect((await call('core_write',raw.TEST_NATIVE)).isError).toBe(true);
  nativeFiles['native/catalog.json'].projects[0].shares=[];
  expect((await call('read_project',{project})).structuredContent.context).toMatchObject({complete:false,items:[]});
  const nativeStore=new GitCoreStore(new GitHubBackend(fake.config),'123','test-1','synthetic','native-owner');
  await nativeStore.write(raw.TEST_NATIVE_LEARNING.target);
  await nativeStore.write(raw.TEST_NATIVE_LEARNING.publication);
  const learningFiles=fake.objects.get(fake.branch)! as any;
  for(const entry of learningFiles['native/catalog.json'].projects)entry.shares=[{project,operation:'resume',
    native_operation:entry.project===raw.TEST_NATIVE.project?'review':'implement'}];
  const rule=learningFiles['context.json'].entries[0];rule.category='learning';
  rule.source={native_learning:'shared-lesson:synthetic-principle',source_project:raw.TEST_NATIVE.project,revision:1,
    target:{native_project:raw.TEST_NATIVE_LEARNING.target.project,contract_revision:1,native_operation:'implement'}};
  learningFiles['context.json'].profiles[0].required={global_rules:[],personal:[],learning:['rule'],relations:[]};
  const shared=(await call('read_project',{project})).structuredContent;
  expect(shared.context).toMatchObject({complete:true,items:[{authority:'candidate',binding:false,
    learning:{verification:'current-native-repaired-case-machine-and-contract',principle:'Preserve exact values in a roundtrip.'}}]});
  expect(JSON.stringify(shared)).not.toContain('Keep the exact fictional original.');
  expect((await call('read_project',{project,mode:'independent'})).structuredContent.context)
    .toMatchObject({complete:true,items:[],categories:{learning:'excluded_by_mode'}});
  learningFiles['native/catalog.json'].projects.find((p:any)=>p.project===raw.TEST_NATIVE.project).shares=[];
  expect((await call('read_project',{project})).structuredContent.context).toMatchObject({complete:false,items:[]});
  await env.DB.prepare("UPDATE relay_grants SET enabled=0 WHERE client_id=?").bind(auth.client).run();
  const denied = await request("/mcp",{method:"POST",headers:{Authorization:"Bearer "+auth.token,"Content-Type":"application/json"},body:"{}"});
  expect(denied.status).toBe(403);
});
it("requires the reviewed generation for real draft routing and refuses a replaced canonical", async () => {
  const auth=await authorize();
  const fake=await new FakeGitHub().init("123");
  (fake.objects.get(fake.branch)! as any)["nexus.json"].mode="draft-intake";
  Object.assign(env,fake.config,{GIT_WORKSPACE_ENABLED:"true",GIT_DATA_MODE:"draft-intake"});
  vi.mocked(fetch).mockImplementation((input,init)=>fake.fetch(input,init));
  const call=()=>request("/mcp",{method:"POST",headers:{Authorization:"Bearer "+auth.token,"Content-Type":"application/json"},
    body:JSON.stringify({jsonrpc:"2.0",id:1,method:"tools/call",params:{name:"find_projects",arguments:{}}})});
  expect((await call()).status).toBe(503);
  env.GIT_GENERATION="test-1";
  expect((await (await call()).json() as any).result.structuredContent.projects).toEqual([]);
  (fake.objects.get(fake.branch)! as any)["nexus.json"].generation="replacement-2";
  const denied=await (await call()).json() as any;
  expect(denied.result.isError).toBe(true);
  expect(JSON.stringify(denied)).toContain("canonical_generation_changed_reconfigure");
});
it("issues bounded longer-lived grants and fences simultaneous refresh replay", async () => {
  const auth = await authorize();
  expect(REFRESH_TTL).toBe(30*86400);expect(CLIENT_TTL).toBe(90*86400);
  const refresh = ()=>request("/oauth/token",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},
    body:new URLSearchParams({grant_type:"refresh_token",refresh_token:auth.refresh,client_id:auth.client,resource:origin+"/mcp"})});
  const replies = await Promise.all([refresh(),refresh()]);
  expect(replies.map(r=>r.status).sort()).toEqual([200,400]);
  const expires = await env.DB.prepare("SELECT MAX(expires) AS expires FROM relay_used_codes").first<number>("expires");
  expect(expires! - Date.now()).toBeGreaterThan(29*86400000);
  const winner = await replies.find(r=>r.status===200)!.json() as any;
  expect(winner.refresh_token).toBeTruthy();
  expect(winner.refresh_token).not.toBe(auth.refresh);
});
it("keeps normal workspace use working beyond the interim 2000-request limit", async () => {
  const auth = await authorize();
  await reserve(env, 2000, 0);
  const response = await request("/mcp", { method: "POST", headers: {
    Authorization: "Bearer " + auth.token, "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }) });
  expect(await response.json()).toEqual({ error: "pc_offline" });
  expect(await env.DB.prepare("SELECT requests FROM relay_budget").first("requests")).toBe(2001);
  expect(DAILY_REQUESTS).toBe(10000);
});
it("reports quota exhaustion as retryable 429 without invalidating login", async () => {
  const auth = await authorize();
  await reserve(env, DAILY_REQUESTS, 0);
  const response = await request("/mcp", { method: "POST", headers: {
    Authorization: "Bearer " + auth.token, "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }) });
  expect(response.status).toBe(429);
  expect(Number(response.headers.get("Retry-After"))).toBeGreaterThan(0);
  expect(response.headers.has("WWW-Authenticate")).toBe(false);
  expect(await response.json()).toMatchObject({ error: "daily_limit_reached", reconnect_required: false });
  expect(await env.DB.prepare("SELECT enabled FROM relay_grants").first("enabled")).toBe(1);
  expect(await env.DB.prepare("SELECT requests FROM relay_budget").first("requests")).toBe(DAILY_REQUESTS);
});
it("retains auth and byte caps and permits reservations on a new UTC day", async () => {
  await reserve(env, 0, 0, 100);
  await expect(reserve(env, 0, 0, 1)).rejects.toThrow("budget exhausted");
  await env.DB.exec("DELETE FROM relay_budget");
  await reserve(env, 0, DAILY_RESPONSE);
  await expect(reserve(env, 0, 1)).rejects.toThrow("budget exhausted");
  await env.DB.exec("UPDATE relay_budget SET day='2000-01-01'");
  await reserve(env, 1, 1);
  expect(await env.DB.prepare("SELECT COUNT(*) AS n FROM relay_budget").first("n")).toBe(2);
});
it("fails a waiting client on connector disconnect", async () => {
  const auth = await authorize();
  const socket = await connectPC();
  const received = new Promise<void>(resolve => socket.addEventListener("message", () => resolve(), { once: true }));
  const pending = request("/mcp", { method: "POST", headers: { Authorization: "Bearer " + auth.token, "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 7, method: "tools/list" }) });
  await received;
  socket.close(1000, "synthetic disconnect");
  const response = await pending;
  expect(response.status).toBe(503);
  expect(await response.json()).toEqual({ error: "pc_disconnected" });
});

import { exportPKCS8, generateKeyPair } from "jose";
import type { GitConfig } from "../src/github-store";

/** HTTP-only fake GitHub boundary, used both in Node and actual workerd runtime. */
export class FakeGitHub {
  config!: GitConfig;
  branch = "0".repeat(39)+"1";
  objects = new Map<string, Record<string, unknown>>();
  trees = new Map<string, Record<string, unknown>>();
  commits = new Map<string, {tree: {sha:string}; parents: string[]}>();
  calls: {path:string;method:string;body:any;redirect:string}[] = [];
  count = 1;
  privateRepo = true;
  losePatchReply = false;
  failReads = false;
  redirectToken = false;
  async init(owner="owner") {
    const pair = await generateKeyPair("RS256", {extractable:true});
    this.config = {GIT_REPOSITORY:"synthetic/data",GIT_REPOSITORY_ID:"42",GIT_BRANCH:"main",GIT_APP_ID:"12",
      GIT_INSTALLATION_ID:"34",GIT_APP_PRIVATE_KEY:await exportPKCS8(pair.privateKey)};
    const files = {"nexus.json":{schema:1,mode:"synthetic",owner,generation:"test-1",projects:[]}};
    this.objects.set(this.branch,files); this.trees.set(this.branch,files);
    this.commits.set(this.branch,{tree:{sha:this.branch},parents:[]});
    return this;
  }
  private next() { return (++this.count).toString(16).padStart(40,"0"); }
  fetch: typeof fetch = async (input, init) => {
    const req = new Request(input,init), url = new URL(req.url), path = url.pathname;
    const body = req.method === "GET" ? undefined : JSON.parse(await req.text());
    this.calls.push({path,method:req.method,body,redirect:req.redirect});
    const fail = (status:number) => Response.json({message:"synthetic failure"},{status});
    if (url.origin !== "https://api.github.com") throw new Error("unexpected origin");
    if (path === "/app/installations/34/access_tokens") {
      if (this.redirectToken) return new Response(null,{status:302,headers:{Location:"https://unexpected.invalid"}});
      return Response.json({token:"synthetic-only",expires_at:new Date(Date.now()+3600000).toISOString()});
    }
    if (req.headers.get("Authorization") !== "Bearer synthetic-only") return fail(401);
    if (this.failReads && (req.method === "GET"||path==='/graphql')) return fail(503);
    if(path==='/graphql'){
      const repository:any={databaseId:42,isPrivate:this.privateRepo};
      for(const [key,value] of Object.entries(body.variables))if(/^p\d+$/.test(key)){
        const expression=String(value),commit=expression.slice(0,40),path=expression.slice(41);
        const item=this.objects.get(commit)?.[path];
        repository['b'+key.slice(1)]=item===undefined?null:{__typename:'Blob',text:JSON.stringify(item),isTruncated:false,
          byteSize:new TextEncoder().encode(JSON.stringify(item)).length};
      }
      return Response.json({data:{repository}});
    }
    if (path === "/repos/synthetic/data") return Response.json({private:this.privateRepo,id:42});
    if (path.endsWith("/git/ref/heads/main")) return Response.json({object:{sha:this.branch}});
    if (path.includes("/contents/")) {
      const value = this.objects.get(url.searchParams.get("ref")!)?.[path.split("/contents/")[1]];
      if (value === undefined) return fail(404);
      const bytes = new TextEncoder().encode(JSON.stringify(value));
      return Response.json({type:"file",encoding:"base64",content:btoa(Array.from(bytes,c=>String.fromCharCode(c)).join(""))});
    }
    if (path.includes("/git/commits/") && req.method === "GET") return Response.json(this.commits.get(path.split("/").at(-1)!)!);
    if (path.endsWith("/git/trees")) {
      const sha = this.next(), base = this.trees.get(body.base_tree);
      if (!base) return fail(422);
      const next={...base};
      for(const e of body.tree){if(e.sha===null){if(!(e.path in next))return fail(422);delete next[e.path];}
        else next[e.path]=JSON.parse(e.content);}
      this.trees.set(sha,next);
      return Response.json({sha},{status:201});
    }
    if (path.endsWith("/git/commits")) {
      const sha = this.next();
      this.commits.set(sha,{tree:{sha:body.tree},parents:body.parents});
      this.objects.set(sha,this.trees.get(body.tree)!);
      return Response.json({sha},{status:201});
    }
    if (path.endsWith("/git/refs/heads/main")) {
      if (body.force !== false || this.commits.get(body.sha)?.parents[0] !== this.branch) return fail(422);
      this.branch = body.sha;
      if (this.losePatchReply) {this.losePatchReply=false;throw new Error("response lost after write");}
      return Response.json({object:{sha:this.branch}});
    }
    throw new Error("unexpected fake GitHub route");
  };
}

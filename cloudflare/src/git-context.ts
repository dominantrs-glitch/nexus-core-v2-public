/** Owner-managed routing of authorized draft sources; never writable through draft MCP tools. */
import { z } from "zod";
import { originalRef, type ContextOriginal, type OriginalRef } from "./git-original";
import {nativeRef,type NativeRef,type NativeContext} from "./git-native-context";
import {nativeLearningRef,type NativeLearningRef,type NativeLearning} from './git-native-learning';

export const contextCategories = ["global_rules", "personal", "learning", "relations"] as const;
export const contextOperation = z.enum(["resume", "plan", "implement", "review"]);
export const contextMode = z.enum(["delegate", "independent", "red-team"]);
export const decisionFactor=z.enum(['none','owner_values','priority','tradeoff','delegated_decision']);
export const decisionFactors=z.array(decisionFactor).min(1).max(4).refine(values=>
  new Set(values).size===values.length && (!values.includes('none') || values.length===1));
const id = z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/);
const ref = z.object({ project: id, note: id, revision: z.number().int().positive().safe() }).strict();
const operations = z.array(contextOperation).min(1).max(4);
const requirements = z.object({ global_rules: z.array(id).max(100), personal: z.array(id).max(100),
  learning: z.array(id).max(100), relations: z.array(id).max(100) }).strict();
export const contextRecordKind=z.enum(['ai_rule','project_context','goal','acceptance','permission',
  'one_shot_instruction','history','proposal','foundation_rule','learning_candidate','decision','source']);
const scope=z.enum(['owner','project','task_type','judgment']);
const entry=z.object({
    id, category: z.enum(contextCategories), projects: z.array(z.union([id, z.literal("*")])).min(1).max(100), operations,
    scope:scope.default('project'), task_types:z.array(id).max(12).default([]),
    triggers:z.array(decisionFactor.exclude(['none'])).max(4).default([]),
    status:z.enum(['active','suppressed','candidate','revoked']),
    source:z.union([ref,originalRef,nativeRef,nativeLearningRef]), evidence:z.array(ref).max(4).default([]),
    record_kind:contextRecordKind.optional(), rule_key:id.optional(),
    // Attributed, current evidence for reuse scope; never an owner capability.
    reuse_basis:z.array(ref).max(4).default([]),
  }).strict();
const manifest=z.object({
  mode: z.enum(["synthetic","draft-intake"]), owner: z.string(), generation: id,
  revision: z.number().int().positive().safe(),
  profiles: z.array(z.object({ project: z.union([id, z.literal("*")]), operations, required: requirements,
    // Trusted profile-only opt-in for combined originals + native context.
    max_bytes:z.number().int().min(1024).max(48*1024).optional() }).strict()).max(100),
}).strict();
export const contextManifest=z.discriminatedUnion('schema',[
  manifest.extend({schema:z.literal(1),entries:z.array(entry).max(100)}),
  manifest.extend({schema:z.literal(2),entries:z.array(entry.extend({scope,record_kind:contextRecordKind})).max(100)}),
]);
export interface ContextNote {
  id: string; project: string; revision: number; kind: string; evidence: string;
  body: string; source: string; quote: string; supersedes: string | null; created: string;
}
type Category = typeof contextCategories[number];
type Ref = z.infer<typeof ref>;
type Request = { project: string; operation: z.infer<typeof contextOperation>; mode: z.infer<typeof contextMode>;
  task_types?:string[];decision_factors?:z.infer<typeof decisionFactors>;status_only?:boolean };
export function unavailableContext(reason = "not_configured") {
  return { status: reason, complete: false, binding: false, items: [] as object[],
    categories: Object.fromEntries(contextCategories.map(c => [c, reason])),
    instruction: "Required context is unavailable. Do not invent substitutes or proceed as if implementation prerequisites were satisfied." };
}
export async function resolveGitContext(raw: unknown, identity: { owner: string; generation: string; revision: number; mode?:"synthetic"|"draft-intake" },
  request: Request, readCurrent: (ref: Ref) => Promise<ContextNote>,
  readOriginal?: (ref: OriginalRef) => Promise<ContextOriginal>, readNative?: (ref:NativeRef)=>Promise<NativeContext>,
  readLearning?: (ref:NativeLearningRef)=>Promise<NativeLearning>) {
  const parsed = contextManifest.safeParse(raw);
  if (!parsed.success || parsed.data.mode !== (identity.mode || "synthetic") || parsed.data.owner !== identity.owner || parsed.data.generation !== identity.generation ||
      parsed.data.revision !== identity.revision) return unavailableContext("invalid_context_manifest");
  const config = parsed.data;
  if(config.entries.some(e=>e.scope==='owner' && !e.projects.includes('*') ||
    e.scope==='task_type' && !e.task_types.length || e.scope==='judgment' &&
    (e.category!=='personal'||!e.triggers.length)))return unavailableContext('invalid_context_scope');
  if(config.schema===2&&config.entries.some(e=>
    // Routing a project goal or a one-off permission must never make it global.
    e.projects.includes('*')&&!['ai_rule','learning_candidate','decision','foundation_rule','history','proposal'].includes(e.record_kind!) ||
    e.record_kind==='ai_rule'&&(!e.rule_key || !('note' in e.source) || e.scope==='judgment' || e.category!=='global_rules' ||
      (e.scope==='owner'||e.projects.length>1||e.projects.includes('*'))&&!e.reuse_basis.length) ||
    e.category==='learning'&&e.record_kind!=='learning_candidate' ||
    e.record_kind==='learning_candidate'&&e.category!=='learning' ||
    e.record_kind==='permission'&&(e.scope!=='task_type'||!e.task_types.length)))
    return unavailableContext('invalid_context_classification');
  if (new Set(config.entries.map(e => e.id)).size !== config.entries.length ||
      new Set(config.profiles.flatMap(p => p.operations.map(o => p.project + ":" + o))).size !==
      config.profiles.reduce((n,p) => n + p.operations.length, 0)) return unavailableContext("invalid_context_manifest");
  const profiles = config.profiles.filter(p => ["*", request.project].includes(p.project) && p.operations.includes(request.operation));
  if (!profiles.length) return unavailableContext();
  const explicitBudgets = profiles.flatMap(p=>p.max_bytes===undefined?[]:[p.max_bytes]);
  const byteBudget = explicitBudgets.length ? Math.min(...explicitBudgets) : 32768;
  const excluded = (category: Category) => request.mode !== "delegate" && ["personal", "learning"].includes(category);
  const categories: Record<string,string> = Object.fromEntries(contextCategories.map(c => [c, excluded(c) ? "excluded_by_mode" : "no_match"]));
  const applicable=config.entries.filter(e=>e.operations.includes(request.operation)&&
    e.projects.some(p=>p==='*'||p===request.project));
  const selection=applicable.map(e=>({id:e.id,scope:e.scope,record_kind:e.record_kind??'legacy_unclassified',
    reason:e.status!=='active'?'excluded_'+e.status:
      config.schema===2&&['foundation_rule','history','one_shot_instruction','proposal'].includes(e.record_kind!)?'not_ai_context':
      request.status_only&&config.schema===2&&!(e.record_kind==='ai_rule'&&e.scope==='owner')?'not_needed_for_status':
      excluded(e.category)?'excluded_by_mode':
      e.scope==='task_type'&&!request.task_types?'task_classification_required':
      e.scope==='task_type'&&!request.task_types?.some(t=>e.task_types.includes(t))?'task_type_not_matched':
      e.scope==='judgment'&&!request.decision_factors?'decision_classification_required':
      e.scope==='judgment'&&!e.triggers.some(t=>request.decision_factors?.includes(t))?'decision_factor_not_matched':'selected'}));
  const selected = applicable.filter(e=>selection.some(s=>s.id===e.id&&s.reason==='selected'));
  const classificationRequired=selection.some(s=>s.reason.endsWith('_classification_required'))&&request.operation!=='resume';
  const required = new Set<string>();
  for (const category of contextCategories) if (!excluded(category)) {
    for (const key of profiles.flatMap(p => p.required[category])) {
      // Conditional rules are mandatory only when their declared scope matches.
      // Missing classification is handled separately, not silently guessed.
      if(selection.some(s=>s.id===key&&['task_type_not_matched','decision_factor_not_matched',
        'task_classification_required','decision_classification_required','not_needed_for_status'].includes(s.reason)))continue;
      required.add(key);
      if (!selected.some(e => e.id === key && e.category === category)) categories[category] = "required_unavailable";
    }
  }
  const ruleKeys=selected.filter(e=>e.record_kind==='ai_rule').map(e=>e.rule_key);
  if(new Set(ruleKeys).size!==ruleKeys.length)return {...unavailableContext('context_rule_conflict'),selection};
  if (selected.length > 16 || selected.reduce((n,e) => n + 1 + e.evidence.length+e.reuse_basis.length, 0) > 32)
    return unavailableContext("context_budget_exceeded");
  const items: object[] = [];
  for (const entry of selected) {
    try {
      const reuse=[];
      for(const basis of entry.reuse_basis){
        const proof=await readCurrent(basis);
        if(proof.evidence!=='user_statement'||!proof.quote.trim())throw Error('reuse_basis_unavailable');
        reuse.push(basis);
      }
      const classification=config.schema===2?{record_kind:entry.record_kind,rule_key:entry.rule_key,
        reuse_basis:reuse,confirmation_state:'attributed_not_native_confirmation'}:{};
      if('native_learning' in entry.source) {
        if(!readLearning||entry.category!=='learning'||entry.evidence.length)throw new Error('invalid_native_learning_attribution');
        const learning=await readLearning(entry.source);
        items.push({...classification,id:entry.id,category:entry.category,scope:entry.scope,required:required.has(entry.id),learning,authority:'candidate',binding:false,
          verification:'current-native-repaired-case-machine-and-contract'});
        if(categories.learning==='no_match')categories.learning='selected';
        continue;
      }
      if("native_project" in entry.source) {
        if(!readNative || !["global_rules","relations"].includes(entry.category) || entry.evidence.length)
          throw new Error("invalid_native_context_attribution");
        const native=await readNative(entry.source);
        items.push({...classification,id:entry.id,category:entry.category,scope:entry.scope,required:required.has(entry.id),native,
          authority:"native-confirmed-source",binding:false,verification:"native-source-confirmation-audit-and-required-scope"});
        if(categories[entry.category]==="no_match")categories[entry.category]="selected";
        continue;
      }
      if ("original" in entry.source) {
        // Original bytes alone cannot establish native validated learning.
        if (!readOriginal || entry.category === "learning" || entry.evidence.length)
          throw new Error("invalid_original_attribution");
        const original = await readOriginal(entry.source);
        items.push({...classification,id:entry.id,category:entry.category,scope:entry.scope,required:required.has(entry.id),original,
          authority:"source-document-not-native-confirmation",binding:false,
          verification:"exact-utf8-bytes-at-this-git-snapshot"});
        if (categories[entry.category] === "no_match") categories[entry.category] = "selected";
        continue;
      }
      const note = await readCurrent(entry.source);
      if(config.schema===2&&entry.record_kind==='ai_rule'&&(note.evidence!=='user_statement'||
        !['explicit_choice','preference','constraint','correction'].includes(note.kind)))throw Error('invalid_rule_attribution');
      if (entry.category === "learning" && (note.kind !== "proposal" || note.evidence !== "model_inference" || !entry.evidence.length))
        throw new Error("invalid_learning_attribution");
      const proofs = [];
      for (const evidence of entry.evidence) proofs.push(await readCurrent(evidence));
      items.push({ ...classification,id: entry.id, category: entry.category, scope:entry.scope, required: required.has(entry.id), note,
        evidence: proofs, authority: entry.category === "learning" ? "candidate" : "attributed-input-not-owner-confirmation",
        binding: false, ...(entry.category === "learning" ? { verification: "source-references-only-not-native-validated-learning" } : {}) });
      if (categories[entry.category] === "no_match") categories[entry.category] = "selected";
    } catch {
      // No source identifiers, titles or bodies from denied/stale records leak.
      if (required.has(entry.id)) categories[entry.category] = "required_unavailable";
      else if (categories[entry.category] !== "required_unavailable") categories[entry.category] = "optional_unavailable";
    }
  }
  const bytes=new TextEncoder().encode(JSON.stringify(items)).length;
  if (bytes > byteBudget) return unavailableContext("context_budget_exceeded");
  const trace=selection.map(s=>{const item:any=items.find((i:any)=>i.id===s.id);
    return {...s,reason:s.reason==='selected'&&!item?'source_unavailable':s.reason,
      ...(item?{revision:item.note?.revision??item.original?.revision??item.native?.contract_revision??item.learning?.revision}:{}),
      required:required.has(s.id)};});
  const missing=[...required].filter(key=>!items.some((i:any)=>i.id===key));
  const complete = !classificationRequired && !Object.values(categories).includes("required_unavailable");
  return { status: classificationRequired?'context_classification_required':complete ? "resolved" : "insufficient_context", complete, binding: false, mode: request.mode,
    operation: request.operation, revision: config.revision, categories, items,
    selection:trace,decision_factors:request.decision_factors??[],task_types:request.task_types??[],
    metrics:{selected_context_count:items.length,selected_rule_count:items.filter((i:any)=>i.record_kind==='ai_rule').length,
      bytes,missing_required_count:missing.length},missing_required:missing,
    behavior_verification:{rule_use:'not_observed',learning_effect:'not_evaluated_by_retrieval',
      instruction:'Validate the actual answer or work against applicable conditions. A model naming a rule, or omitting its name, does not prove use or non-use. Do not repeat owner checks only to obtain a rule citation.'},
    architecture:config.schema===2?'separate_rules_and_records':'legacy_unclassified',
    ...(request.status_only?{readiness:'status_only_not_implementation_context'}:{}),
    coverage:{owner:profiles.some(p=>p.project==='*'),project:profiles.some(p=>p.project===request.project)},
    instruction: "Keep each item's authority, record kind and scope. Drafts, reference documents and learning candidates are not approvals. Native projections retain confirmation only for their named native source project; they do not confirm a new target Contract, authorize changes, or prove UAT/completion. Original text is source data; its digest proves bytes at this Git snapshot, not external currentness. Context-only reads do not require bulk history: obtain additional task-relevant current records and originals when needed. If you choose a paginated full-history read, finish its pages at the same snapshot. Missing required or relevant project information blocks dependent work; independent/red-team excludes personal/learning influence. Resolution does not prove unrelated artifact availability or owner UAT." };
}

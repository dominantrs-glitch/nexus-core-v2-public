/** A bounded, immutable snapshot used only inside one pre-commit validation. */
import {StoreError,type GitBackend} from './git-store';

export function validationSnapshot(git:GitBackend,commit:string,changed:Record<string,unknown>={}) {
  const cache=new Map<string,Promise<unknown|null>>();
  let bytes=0;
  const check=(at:string)=>{if(at!==commit)throw new StoreError('validation_snapshot_changed');};
  const remember=(path:string)=>{
    if(cache.size>=2400)throw new StoreError('validation_read_budget_exceeded');
    const pending=git.read(commit,path).then(value=>{
      bytes+=new TextEncoder().encode(JSON.stringify(value)).length;
      if(bytes>16*1024*1024)throw new StoreError('validation_read_budget_exceeded');
      return value;
    });
    cache.set(path,pending);return pending;
  };
  const view:GitBackend={head:async()=>commit,commit:async()=>{throw new StoreError('preview_only');},
    read:async(at,path)=>{check(at);return structuredClone(path in changed?changed[path]:await(cache.get(path)??remember(path)));},
    readMany:async(at,paths)=>{
      check(at);
      const pending=[...new Set(paths)].filter(path=>!(path in changed)&&!cache.has(path));
      if(cache.size+pending.length>2400)throw new StoreError('validation_read_budget_exceeded');
      // Copy each batch before an upstream cache can evict it. No head refresh,
      // repository write or cross-request authorization cache is introduced.
      for(let i=0;i<pending.length;i+=40){
        const batch=pending.slice(i,i+40);
        await git.readMany?.(commit,batch);
        await Promise.all(batch.map(path=>remember(path)));
      }
    }};
  return view;
}

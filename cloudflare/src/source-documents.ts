/** Exact legacy locators only. Does not fetch, authorize, or certify currentness. */
export function sourceDocuments(notes: {source:string}[]) {
  const result: {repository:string;path:string;captured_commit:string;captured_url:string;latest_url:string;
    status:string;currentness:string;authority:string}[]=[];
  const seen=new Set<string>();
  for(const note of notes){
    const match=/^git:ai-workspace@([a-f0-9]{40}):([^\r\n]+)$/.exec(note.source);
    if(!match)continue;
    const [,commit,path]=match;
    if(!/^(brain|projects)\//.test(path)||path.split('/').some(p=>['','.','..'].includes(p))||/[\\:%?#\x00-\x1f]/.test(path))continue;
    const key=commit+':'+path;if(seen.has(key))continue;seen.add(key);
    const encoded=path.split('/').map(p=>encodeURIComponent(p).replace(/[!'()*]/g,c=>'%'+c.charCodeAt(0).toString(16).toUpperCase())).join('/');
    const base='https://github.com/YOUR_GITHUB_ACCOUNT/ai-workspace/blob/';
    result.push({repository:'YOUR_GITHUB_ACCOUNT/ai-workspace',path,captured_commit:commit,
      captured_url:base+commit+'/'+encoded,latest_url:base+'main/'+encoded,
      status:'reference_only_not_fetched',currentness:'not_checked',authority:'historical-source-not-current-confirmation'});
  }
  return result;
}

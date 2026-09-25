/** Local Git objects only. No network, checkout, keys or production writes. */
import {execFileSync} from 'node:child_process';
import {Buffer} from 'node:buffer';
export function localSnapshot(repo:string,ref:string){
  if(!repo||!ref||ref.startsWith('-'))throw Error('arguments_required');
  const git=(args:string[],input?:Buffer)=>execFileSync('git',['-C',repo,...args],{input,maxBuffer:128*1024*1024,windowsHide:true}) as Buffer;
  const decoder=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true});
  const snapshot=decoder.decode(git(['rev-parse','--verify',ref+'^{commit}'])).trim();
  if(!/^[a-f0-9]{40}$/.test(snapshot))throw Error('invalid_revision');
  const paths=decoder.decode(git(['ls-tree','-r','--name-only','-z',snapshot])).split('\0').filter(Boolean);
  if(paths.some(p=>/[\r\n]/.test(p)||!p.endsWith('.json'))||paths.length>20000)throw Error('unsupported_or_unbounded_tree');
  const bytes=git(['cat-file','--batch'],Buffer.from(paths.map(p=>snapshot+':'+p+'\n').join('')));
  let offset=0;const files:Record<string,unknown>={};
  for(const path of paths){
    const end=bytes.indexOf(10,offset),header=decoder.decode(bytes.subarray(offset,end)),m=/^[a-f0-9]{40} blob (\d+)$/.exec(header);
    if(!m)throw Error('invalid_git_object');
    const size=Number(m[1]);offset=end+1;
    files[path]=JSON.parse(decoder.decode(bytes.subarray(offset,offset+size)));offset+=size+1;
  }
  if(offset!==bytes.length)throw Error('unexpected_git_objects');
  return {files,snapshot};
}

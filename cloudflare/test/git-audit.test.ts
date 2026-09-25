import {expect,it} from 'vitest';
import {FakeGitHub} from './git-fixture';
import {GitHubBackend} from '../src/github-store';
import {GitIntake} from '../src/git-store';
import {auditFiles} from '../src/git-audit';

async function setup(){
  const git=await new FakeGitHub().init(),store=new GitIntake(new GitHubBackend(git.config,git.fetch),'owner','test-1');
  const p=await store.create({title:'Audit',source:'synthetic',request_id:'create'});
  const first=await store.save({project:p.project,kind:'goal',body:'first',source:'synthetic',quote:'first',
    evidence:'user_statement',expected_revision:0,request_id:'first'});
  const next=await store.save({project:p.project,kind:'correction',body:'second',source:'synthetic',quote:'second',
    evidence:'user_statement',expected_revision:1,supersedes:first.note,request_id:'second'});
  return {git,p,first,next,files:git.objects.get(git.branch)!};
}
it('keeps historical correction records and checks immutable snapshot metadata without writes',async()=>{
  const {git,files}=await setup(),before=JSON.stringify(files);
  expect(await auditFiles(files,git.branch)).toMatchObject({status:'passed',counts:{projects:1,current_notes:1},issues:[]});
  expect(JSON.stringify(files)).toBe(before);
});
it('identifies missing history, mismatched work references and orphan files without exposing contents',async()=>{
  const {git,files,p,first,next}=await setup();
  delete files[`projects/${p.project}/records/${first.note}.json`];
  (files[`projects/${p.project}/manifest.json`] as any).work_index=[{id:'lost',note:'n-lost',key:'lost',kind:'work',status:'open',date:null,created:'2026-09-24T00:00:00Z'}];
  files['projects/orphan/records/n-secret.json']={body:'never print this secret'};
  const result=await auditFiles(files,git.branch);
  expect(result.status).toBe('issues_found');
  expect(result.issues.map(i=>i.type)).toEqual(expect.arrayContaining(['broken_correction_history','work_current_note_missing','orphan_project_file']));
  expect(JSON.stringify(result)).not.toContain('never print this secret');
});
it('reports forged learning status even when the source references themselves are well formed',async()=>{
  const {git,p}=await setup(),store=new GitIntake(new GitHubBackend(git.config,git.fetch),'owner','test-1');
  const work=await store.saveWork({project:p.project,expected_revision:2,request_id:'work',source:'synthetic',
    evidence:'user_statement',quote:'Do this',item:{id:'one',type:'action',title:'One',status:'done',
      learning:{corrections:[],checks:[],conclusion:'No linked correction'}}});
  const files=git.objects.get(git.branch)!;
  (files[`projects/${p.project}/learning-evaluations/${work.note}.json`] as any).status='candidate_review_available';
  const result=await auditFiles(files,git.branch);
  expect(result.issues).toContainEqual({type:'learning_assessment_mismatch',path:`projects/${p.project}/learning-evaluations/${work.note}.json`,severity:'error'});
});

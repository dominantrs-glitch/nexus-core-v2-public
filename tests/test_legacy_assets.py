from pathlib import Path
import tempfile
import unittest
import json
from unittest.mock import patch
from nexus.legacy_assets import git, capture, verify, extract, capture_extra, catalog


class LegacyAssetsTests(unittest.TestCase):
    def test_archive_keeps_remote_local_and_uncommitted_originals_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);repo=root/'old';repo.mkdir()
            git(repo,'init','-b','main');git(repo,'config','user.name','Synthetic');git(repo,'config','user.email','synthetic@example.invalid')
            git(repo,'config','core.autocrlf','false')
            folder=repo/'projects/example/01_raw';folder.mkdir(parents=True)
            original=folder/'日本語.txt';original.write_bytes(b'first\r\noriginal')
            (repo/'.gitignore').write_text('ignored-secret\n')
            git(repo,'add','.');git(repo,'commit','-m','first');git(repo,'update-ref','refs/remotes/origin/main','HEAD')
            original.write_bytes(b'second original');git(repo,'add','.');git(repo,'commit','-m','local')
            original.write_bytes(b'uncommitted original')
            (repo/'ignored-secret').write_text('must remain local in old repo')
            before=git(repo,'status','--porcelain')
            result=capture(repo,root/'new')
            self.assertEqual(result['status'],'all_selected_bytes_verified');self.assertEqual(result['ignored_files'],1)
            self.assertEqual(git(repo,'status','--porcelain'),before)
            rows=json.loads((root/'new/manifest.json').read_text(encoding='utf-8'))['entries']
            expected={'origin/main':b'first\r\noriginal','HEAD':b'second original','working-tree':b'uncommitted original'}
            for row in rows:
                if row['path'].endswith('日本語.txt'):
                    dest=root/(row['layer'].replace('/','-')+'.txt');extract(root/'new',row['id'],dest)
                    self.assertEqual(dest.read_bytes(),expected[row['layer']])
                    with self.assertRaises(FileExistsError):extract(root/'new',row['id'],dest)
            self.assertEqual(verify(root/'new')['entries'],5)
            raw=b'large ignored original\r\n'
            (repo/'ignored-original.zip').write_bytes(raw)
            with patch('nexus.legacy_assets.EXTRA_CHUNK',4):
                capture_extra(repo,root/'new',[{'path':'ignored-original.zip'}])
            extra=json.loads((root/'new/extra-manifest.json').read_text(encoding='utf-8'))['entries'][0]
            extract(root/'new',extra['id'],root/'restored.zip')
            self.assertEqual((root/'restored.zip').read_bytes(),raw)
            self.assertEqual(verify(root/'new')['entries'],6)
            self.assertFalse(catalog(root/'new',repo)['deletion_ready'])
            with self.assertRaises(ValueError):capture_extra(repo,root/'new',[{'path':'ignored-original.zip'}])
            victim=next(r for r in rows if r['path'].endswith('日本語.txt'))
            (root/'new/blobs'/victim['sha256']).write_bytes(b'corrupt')
            with self.assertRaises(ValueError):verify(root/'new')
            with self.assertRaises(ValueError):extract(root/'new',victim['id'],root/'bad')
            self.assertFalse((root/'bad').exists())


if __name__=='__main__':unittest.main()

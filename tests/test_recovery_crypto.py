from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from nexus.environment_recovery import create, restore
from nexus.recovery_crypto import PortableProtector, new_key
from nexus.recovery_readiness import inspect


class PortableRecoveryTests(unittest.TestCase):
    def test_restore_needs_only_separate_key_and_rejects_wrong_key_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key, wrong = root/'key.json', root/'wrong.json'
            new_key(key)
            new_key(wrong)
            source = root/'private.txt'
            source.write_text('private original unchanged')
            plan = {'schema': 1, 'files': [{'name': 'source.txt', 'path': str(source)}]}
            with patch('nexus.environment_recovery.DpapiProtector', side_effect=AssertionError('Windows auth unavailable')):
                summary = create(plan, root/'package', PortableProtector(key))
                self.assertEqual(summary['protection'], 'portable-aes256gcm-keyfile')
                with self.assertRaises(ValueError):
                    restore(root/'package', root/'wrong-restore', PortableProtector(wrong))
                self.assertFalse((root/'wrong-restore').exists())
                result = restore(root/'package', root/'restored', PortableProtector(key))
                self.assertFalse(result['services_started'])
                self.assertEqual((root/'restored/files/source.txt').read_bytes(), source.read_bytes())
                self.assertNotIn(b'private original', b''.join(p.read_bytes() for p in (root/'package').iterdir()))
                self.assertEqual(inspect(root/'restored')['status'],'data_verified_activation_pending')
                (root/'restored/files/source.txt').write_text('changed after restore')
                with self.assertRaises(ValueError):inspect(root/'restored')
            with self.assertRaises(FileExistsError):
                new_key(key)

    def test_authentication_catches_tamper_even_if_untrusted_outer_hash_is_rewritten(self):
        from nexus.environment_recovery import digest
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);new_key(root/'key');protector=PortableProtector(root/'key')
            (root/'file').write_text('private')
            create({'schema':1,'files':[{'name':'file','path':str(root/'file')}]},root/'package',protector)
            manifest=root/'package/manifest.sealed'
            data=bytearray(manifest.read_bytes());data[-1]^=1;manifest.write_bytes(data)
            summary=json.loads((root/'package/recovery.json').read_text());summary['manifest_sha256']=digest(data)
            (root/'package/recovery.json').write_text(json.dumps(summary))
            with self.assertRaises(ValueError):restore(root/'package',root/'output',PortableProtector(root/'key'))
            self.assertFalse((root/'output').exists())

    def test_binary_db_file_is_preserved_and_windows_device_paths_are_rejected(self):
        from nexus.environment_recovery import relative
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);new_key(root/'key');protector=PortableProtector(root/'key')
            (root/'file.db').write_bytes(b'not sqlite; still an original')
            create({'schema':1,'files':[{'name':'file.db','path':str(root/'file.db')}]},root/'package',protector)
            restore(root/'package',root/'output',protector)
            self.assertEqual((root/'output/files/file.db').read_bytes(),(root/'file.db').read_bytes())
        for path in ['../out', 'CON.txt', 'dir/a.', 'dir/a ', 'AUX', 'dir/LPT1']:
            with self.assertRaises(ValueError):relative(path)


if __name__ == '__main__':unittest.main()

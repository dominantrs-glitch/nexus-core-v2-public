import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import subprocess
import unittest

from nexus.environment_recovery import create, restore
from nexus.artifacts import ArtifactStore, LocalBinaryProvider


class SyntheticProtector:
    """Test-only reversible capsule; the production CLI always uses DPAPI."""
    def protect(self, data, entropy):
        return b'test-sealed:' + bytes(v ^ 0xBA for v in data)

    def unprotect(self, data, entropy):
        if not data.startswith(b'test-sealed:'):
            raise ValueError('not sealed')
        return bytes(v ^ 0xBA for v in data[12:])


class EnvironmentRecoveryTests(unittest.TestCase):
    def test_live_runtime_exclusions_are_explicit_and_db_suffix_uses_sqlite_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'live'
            source.mkdir()
            (source / 'instance.lock').write_text('ephemeral lock')
            db = sqlite3.connect(source / 'state.db')
            try:
                db.execute('pragma journal_mode=WAL')
                db.execute('create table delivered (id integer)')
                db.execute('insert into delivered values (1)')
                db.commit()
                selected = dict(name='service', path=str(source), exclude=['instance.lock'])
                plan = dict(schema=1, roots=[selected])
                with self.assertRaises(ValueError):
                    create(plan, base / 'unexplained', SyntheticProtector())
                selected['exclude_reason'] = 'OS runtime locks must be recreated, never restored.'
                create(plan, base / 'package', SyntheticProtector())
                result = restore(base / 'package', base / 'restored', SyntheticProtector())
                self.assertEqual(result['exclusions'][0]['paths'], ['instance.lock'])
                self.assertFalse((base / 'restored/roots/service/instance.lock').exists())
                with closing(sqlite3.connect(base / 'restored/roots/service/state.db')) as copy:
                    self.assertEqual(copy.execute('select id from delivered').fetchall(), [(1,)])
            finally:
                db.close()

    def test_remote_tracking_only_bundle_restores_exact_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / 'repo'
            def git(*args):
                return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True).stdout
            repo.mkdir()
            git('init')
            git('config', 'user.name', 'Synthetic')
            git('config', 'user.email', 'synthetic@example.invalid')
            (repo / 'source.txt').write_text('original')
            git('add', 'source.txt')
            git('commit', '-m', 'synthetic original')
            expected = git('rev-parse', 'HEAD').decode().strip()
            git('update-ref', 'refs/remotes/origin/main', expected)
            plan = dict(schema=1, repositories=[dict(name='data', path=str(repo), ref='origin/main')])
            create(plan, base / 'package', SyntheticProtector())
            result = restore(base / 'package', base / 'restored', SyntheticProtector())
            self.assertEqual(result['status'], 'isolated_restore_verified')
            restored = base / 'restored/repositories/data.restored.git'
            actual = subprocess.run(['git', '-C', str(restored), 'rev-parse', 'refs/remotes/origin/main'],
                                    check=True, capture_output=True).stdout.decode().strip()
            self.assertEqual(actual, expected)

    def test_wal_original_and_configuration_restore_into_fresh_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / 'data'
            (data / 'artifacts').mkdir(parents=True)
            ArtifactStore(data / 'artifacts' / 'manifest.sqlite3', LocalBinaryProvider(data / 'artifacts' / 'blobs'))
            config = data / 'connection.json'
            config.write_text('synthetic credential: do not print')
            db = sqlite3.connect(data / 'notes.sqlite3')
            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE notes (body TEXT)')
                db.execute("INSERT INTO notes VALUES ('live WAL row')")
                db.commit()
                plan = dict(schema=1, roots=[dict(name='data', path=str(data))], activation=['Reauthorize and review startup before switching'])
                package, stage = base / 'package', base / 'stage'
                report = create(plan, package, SyntheticProtector())
                self.assertFalse(report['activation_performed'])
                self.assertNotIn(b'synthetic credential', b''.join(p.read_bytes() for p in package.iterdir()))
                result = restore(package, stage, SyntheticProtector())
                self.assertEqual(result['status'], 'isolated_restore_verified')
                restored = stage / 'roots' / 'data'
                self.assertEqual((restored / 'connection.json').read_text(), config.read_text())
                with closing(sqlite3.connect(restored / 'notes.sqlite3')) as copy:
                    self.assertEqual(copy.execute('SELECT body FROM notes').fetchone()[0], 'live WAL row')
                self.assertFalse(result['services_started'])
                with self.assertRaises(FileExistsError):
                    restore(package, stage, SyntheticProtector())
            finally:
                db.close()

    def test_changed_capsule_and_escape_paths_fail_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source.txt'
            source.write_text('unchanged')
            plan = dict(schema=1, files=[dict(name='config.txt', path=str(source))])
            package = base / 'package'
            create(plan, package, SyntheticProtector())
            (package / '000000.sealed').write_bytes(b'broken')
            with self.assertRaises(ValueError):
                restore(package, base / 'stage', SyntheticProtector())
            self.assertFalse((base / 'stage').exists())
            plan['files'][0]['name'] = '../escape'
            with self.assertRaises(ValueError):
                create(plan, base / 'bad', SyntheticProtector())
            self.assertEqual(source.read_text(), 'unchanged')

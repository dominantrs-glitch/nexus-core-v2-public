import tempfile
import unittest
from unittest.mock import patch
from nexus.desktop_bridge import dispatch
from nexus.intake import Intake
from nexus.intake import OVERVIEW_HEADER


class DesktopBridgeTests(unittest.TestCase):
    def test_overview_revision_and_history(self):
        with tempfile.TemporaryDirectory() as root:
            w=Intake(root);p=w.create('Example','test','create')['project']
            first=w.save(p,'proposal',OVERVIEW_HEADER+'Plain explanation','intake:r0','model_inference','',0,'overview')
            self.assertTrue(w.read(p)['overview']['current'])
            w.save(p,'constraint','Free only','owner','user_statement','Free only',1,'change')
            self.assertFalse(w.read(p)['overview']['current'])
            w.save(p,'proposal',OVERVIEW_HEADER+'Updated explanation','intake:r2','model_inference','',2,'updated',supersedes=first['note'])
            view=dispatch(dict(action='read',project=p),w)
            self.assertTrue(view['overview']['current'])
            self.assertEqual(view['overview']['text'],'Updated explanation')
            self.assertEqual(len(view['notes']),2)
            with w.db() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM notes').fetchone()[0],3)

    def test_all_note_pages_and_restricted_open(self):
        with tempfile.TemporaryDirectory() as root:
            w = Intake(root)
            project = w.create('Desktop test', 'local test', 'create')['project']
            for i in range(12):
                w.save(project, 'proposal', 'text', 'test', 'model_inference', '', i, 'note-'+str(i))
            self.assertEqual(len(dispatch(dict(action='read', project=project), w)['notes']), 12)
            with patch('nexus.desktop_bridge.os.startfile') as launch:
                for bad in ('../../private', 'cmd.exe', 'https://example.com'):
                    with self.assertRaises(ValueError):
                        dispatch(dict(action='open', project=project, folder=bad), w)
                with self.assertRaises(ValueError):
                    dispatch(dict(action='open', project='unknown', folder='project'), w)
                launch.assert_not_called()
                dispatch(dict(action='open', project=project, folder='05_output'), w)
                launch.assert_called_once_with(str(w.folder(project)/'05_output'))
            with self.assertRaises(ValueError): dispatch(dict(action='save'), w)


if __name__ == '__main__': unittest.main()

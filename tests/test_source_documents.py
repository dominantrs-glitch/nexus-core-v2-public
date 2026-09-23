import unittest
from nexus.source_documents import source_documents


class SourceDocumentTests(unittest.TestCase):
    def test_exact_revision_and_latest_are_distinct_and_not_claimed_read(self):
        sha = 'a' * 40
        source = f'git:ai-workspace@{sha}:projects/日本語/a b.md'
        docs = source_documents([dict(source=source), dict(source=source)])
        self.assertEqual(len(docs), 1)
        self.assertIn('/' + sha + '/', docs[0]['captured_url'])
        self.assertIn('/main/', docs[0]['latest_url'])
        self.assertIn('%20', docs[0]['captured_url'])
        self.assertEqual(docs[0]['currentness'], 'not_checked')

    def test_untrusted_urls_paths_and_unpinned_sources_do_not_become_locators(self):
        sha = 'a' * 40
        sources = ['https://example.invalid/private', 'git:ai-workspace@main:brain/a.md']
        sources += [f'git:ai-workspace@{sha}:{p}' for p in [
            '../secret', 'brain/../secret', 'brain//secret', 'brain/%2e%2e/secret',
            'brain/a?token=abc', 'brain/a#fragment', 'brain/\\secret', '.git/config']]
        self.assertEqual(source_documents([dict(source=s) for s in sources]), [])


if __name__ == '__main__':
    unittest.main()

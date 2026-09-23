"""Expose exact legacy locators without fetching or claiming source freshness."""
import re
from urllib.parse import quote


def source_documents(notes):
    result = []
    seen = set()
    for note in notes:
        match = re.fullmatch(r'git:ai-workspace@([a-f0-9]{40}):([^\r\n]+)', note['source'])
        if not match:
            continue
        commit, path = match.groups()
        if (not path.startswith(('brain/', 'projects/')) or
                any(p in {'', '.', '..'} for p in path.split('/')) or
                any(c in path for c in '\\:%?#') or any(ord(c) < 32 for c in path)):
            continue
        key = (commit, path)
        if key in seen:
            continue
        seen.add(key)
        base = 'https://github.com/YOUR_GITHUB_ACCOUNT/ai-workspace/blob/'
        result.append(dict(repository='YOUR_GITHUB_ACCOUNT/ai-workspace', path=path, captured_commit=commit,
                           captured_url=base + commit + '/' + quote(path, safe='/'),
                           latest_url=base + 'main/' + quote(path, safe='/'),
                           status='reference_only_not_fetched', currentness='not_checked',
                           authority='historical-source-not-current-confirmation'))
    return result

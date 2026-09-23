"""Prepare explicitly selected UTF-8 originals locally. Never uploads or activates."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,79}$')
SECRET = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})')


def prepare(spec, destination, *, binary=False):
    """New local package, byte-exact sources and review metadata; no source changes."""
    if not isinstance(spec, dict) or set(spec) != {'owner', 'generation', 'mode', 'project', 'documents'}:
        raise ValueError('explicit project and document selection required')
    for key in ('generation', 'project'):
        if not isinstance(spec[key], str) or not ID.fullmatch(spec[key]):
            raise ValueError('invalid identity')
    if (spec['mode'] not in ('synthetic', 'draft-intake') or not isinstance(spec['owner'], str)
            or not spec['owner'].strip() or len(spec['owner'].encode('utf-8')) > 200
            or not isinstance(spec['documents'], list) or not 1 <= len(spec['documents']) <= 12):
        raise ValueError('invalid selection')
    files, current, report, ids = {}, [], [], set()
    captured = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    for doc in spec['documents']:
        if not isinstance(doc, dict) or set(doc) != {'id', 'revision', 'file', 'title', 'source', 'media_type', 'projects', 'operations'}:
            raise ValueError('invalid document declaration')
        if (not isinstance(doc['id'], str) or not ID.fullmatch(doc['id']) or doc['id'] in ids
                or type(doc['revision']) is not int or not 1 <= doc['revision'] <= 9007199254740991):
            raise ValueError('invalid document identity')
        ids.add(doc['id'])
        for key, limit in [('title', 300), ('source', 2000)]:
            if not isinstance(doc[key], str) or not doc[key].strip() or len(doc[key].encode('utf-8')) > limit:
                raise ValueError('invalid document metadata')
        formats = ('image/png', 'image/jpeg', 'application/pdf') if binary else ('text/plain', 'text/markdown')
        if (doc['media_type'] not in formats
                or not isinstance(doc['projects'], list) or not 1 <= len(doc['projects']) <= 100
                or any(not isinstance(p, str) or not ID.fullmatch(p) for p in doc['projects'])
                or not isinstance(doc['operations'], list) or not 1 <= len(doc['operations']) <= 4
                or any(o not in ('resume', 'plan', 'implement', 'review') for o in doc['operations'])):
            raise ValueError('explicit scope and supported format required')
        path = Path(doc['file'])
        if path.is_symlink() or not path.is_file():
            raise ValueError('original must be a regular local file')
        limit = 262144 if binary else 24576
        with path.open('rb') as source:
            raw = source.read(limit + 1)
        if not raw or len(raw) > limit:
            raise ValueError('original exceeds source limit')
        if binary:
            signatures = {'image/png': b'\x89PNG\r\n\x1a\n', 'image/jpeg': b'\xff\xd8\xff', 'application/pdf': b'%PDF-'}
            if not raw.startswith(signatures[doc['media_type']]):
                raise ValueError('binary media signature mismatch')
            content = base64.b64encode(raw).decode('ascii')
        else:
            content = raw.decode('utf-8', errors='strict')
            if not content.strip() or SECRET.search(content):
                raise ValueError('empty original or possible secret; manual review required')
        sha256 = hashlib.sha256(raw).hexdigest()
        category = 'binary' if binary else 'originals'
        relative = f"projects/{spec['project']}/{category}/{doc['id']}/{doc['revision']}.json"
        files[relative] = dict(schema=1, project=spec['project'], id=doc['id'], revision=doc['revision'],
            sha256=sha256, media_type=doc['media_type'], encoding='base64' if binary else 'utf-8', title=doc['title'], source=doc['source'],
            captured_at=captured, content=content, authority='source-document-not-native-confirmation')
        if binary:
            files[relative]['bytes'] = len(raw)
        current.append(dict(id=doc['id'], revision=doc['revision'], sha256=sha256, remote=True,
                            projects=doc['projects'], operations=doc['operations']))
        report.append(dict(id=doc['id'], revision=doc['revision'], title=doc['title'], bytes=len(raw), sha256=sha256,
                           file=relative, projects=doc['projects'], operations=doc['operations']))
    files[f"projects/{spec['project']}/{'binary' if binary else 'originals'}/manifest.json"] = dict(schema=1, owner=spec['owner'],
        generation=spec['generation'], mode=spec['mode'], project=spec['project'], current=current)
    review = dict(status='local-review-only', uploaded=False, activated=False, transfer_authorized=False,
                  native_confirmation=False, external_source_currentness='captured-snapshot-only',
                  secret_scan='binary-not-scanned-review-required' if binary else 'limited-pattern-check-not-sensitivity-certification', documents=report)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for relative, value in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
        # Compare the rendered JSON payload to source bytes, including BOM/CRLF.
        if 'content' in value:
            saved = json.loads(target.read_text(encoding='utf-8'))['content']
            restored = base64.b64decode(saved, validate=True) if binary else saved.encode('utf-8')
            if hashlib.sha256(restored).hexdigest() != value['sha256']:
                raise ValueError('staged original integrity failure')
    (destination / 'review.local.json').write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding='utf-8')
    return review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--binary', action='store_true', help='Prepare explicitly selected PNG/JPEG/PDF, at most 256 KiB each')
    args = parser.parse_args()
    print(json.dumps(prepare(json.loads(args.spec.read_text(encoding='utf-8')), args.destination, binary=args.binary), ensure_ascii=False))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()

"""Prepare two harmless originals for a separately authorized client delivery test."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nexus.fixtures import write_png
from nexus.git_original import prepare


def write_pdf(path):
    # A complete one-page PDF fixture; xref offsets refer to the exact emitted bytes.
    text = b'BT /F1 22 Tf 45 140 Td (NEXUS SYNTHETIC TEST 7482) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 500 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(text)).encode() + b' >>\nstream\n' + text + b'\nendstream']
    data, offsets = bytearray(b'%PDF-1.4\n'), [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(str(number).encode() + b' 0 obj\n' + body + b'\nendobj\n')
    xref = len(data)
    data.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    with path.open('xb') as output:
        output.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--generation', required=True)
    parser.add_argument('--project', required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    write_png(args.root / 'synthetic-colors.png', 192, 64, 6)
    write_pdf(args.root / 'synthetic-reading.pdf')
    documents = [dict(id=identity, revision=1, file=str(args.root / name), title='架空の添付確認用資料',
        source='locally-generated-synthetic-client-delivery-fixture', media_type=media,
        projects=[args.project], operations=['resume', 'implement']) for identity, name, media in [
            ('synthetic-image-delivery', 'synthetic-colors.png', 'image/png'),
            ('synthetic-pdf-delivery', 'synthetic-reading.pdf', 'application/pdf')]]
    result = prepare(dict(owner=args.owner, generation=args.generation, mode='draft-intake',
        project=args.project, documents=documents), args.root / 'staged', binary=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()

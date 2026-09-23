"""Bounded local measurements, without prompts, note bodies, keys or model routing."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys


def default_database():
    return Path(os.environ.get('LOCALAPPDATA', '.')) / 'NexusCoreV2' / 'metrics' / 'git-client.sqlite3'


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def record(operation, args, response, elapsed_ms, *, database=None):
    """Best-effort telemetry must not convert a successful save into a failure."""
    try:
        path = Path(database) if database else default_database()
        path.parent.mkdir(parents=True, exist_ok=True)
        request, result = _encoded(args), _encoded(response) if response is not None else None
        with closing(sqlite3.connect(path, timeout=0.2)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS measurements(
                id INTEGER PRIMARY KEY, captured_at TEXT NOT NULL, operation TEXT NOT NULL,
                request_hash TEXT NOT NULL, response_hash TEXT, request_bytes INTEGER NOT NULL,
                response_bytes INTEGER, elapsed_ms INTEGER NOT NULL, success INTEGER NOT NULL)''')
            db.execute('INSERT INTO measurements VALUES(NULL,?,?,?,?,?,?,?,?)', (
                datetime.now(timezone.utc).isoformat(), operation, hashlib.sha256(request).hexdigest(),
                hashlib.sha256(result).hexdigest() if result is not None else None, len(request),
                len(result) if result is not None else None, max(0, int(elapsed_ms)), int(result is not None)))
            db.execute('DELETE FROM measurements WHERE id NOT IN (SELECT id FROM measurements ORDER BY id DESC LIMIT 500)')
        return True
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return False


def report(database=None):
    path = Path(database) if database else default_database()
    rows = []
    if path.exists():
        with closing(sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute('SELECT * FROM measurements ORDER BY id')]
    successful = [r for r in rows if r['success']]
    reads = [r for r in successful if r['operation'] in ('read', 'list', 'search', 'original', 'work', 'core_read')]
    counts = Counter((r['operation'], r['request_hash'], r['response_hash']) for r in reads)
    repeats = sum(n-1 for n in counts.values())
    return dict(scope='owner-local Git client, last 500 observed calls only', calls=len(rows),
        successful=len(successful), failed_or_unknown=len(rows)-len(successful),
        elapsed_ms=sum(r['elapsed_ms'] for r in rows),
        payload_bytes=sum(r['response_bytes'] or 0 for r in rows),
        same_request_same_response_repeats=repeats, by_operation=dict(Counter(r['operation'] for r in rows)),
        token_usage=None, token_status='not_available_from_this_client', model_routing='not_implemented',
        proposals=[] if not repeats else [dict(
            proposal='同内容の再取得が必要な確認だったかを見直し、同じ会話では差分読取を使う候補です。',
            evidence=f'同じ要求・同じ応答の再取得を{repeats}回観測しました。', requires_owner_selection=True,
            caveat='再取得には更新確認の目的もあるため、この回数をそのまま無駄とはみなしません。')],
        limitation='通信本文の再エンコード後のバイト数と呼出時間の実測です。モデルの実トークン数・課金額・思考時間ではありません。'
            'ChatGPT側の呼出や計測開始前の使用量は含みません。本文・検索語・秘密情報は保存していません。')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Measured local read repetition; advisory only')
    parser.add_argument('--database', type=Path)
    parser.add_argument('command', choices=['report'])
    args = parser.parse_args()
    print(json.dumps(report(args.database), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

"""Small local operator menu. No background service or external side effects."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nexus.workflow import main


def run():
    actions = {'1': 'prepare', '2': 'start', '3': 'build', '4': 'handoff',
               '5': 'backup-check', '6': 'accept', '7': 'status'}
    while True:
        print('\nNexus Core V2 - 合成案件のローカル操作')
        print('1 準備・条件案 / 2 条件を本人確認 / 3 制作・再検証 / 4 AI受渡しファイル')
        print('5 復元検査 / 6 最終の本人UAT・完了判定 / 7 現在地 / 8 修正 / 9 バックアップ / 0 終了')
        choice = input('番号: ').strip()
        if choice == '0':
            return
        if choice == '8':
            main(['correct', input('カードに記載する修正内容: ')])
        elif choice == '9':
            destination = input('新規バックアップ先（既存フォルダーは使いません）: ').strip().strip('"')
            if destination:
                main(['export', destination])
        elif choice in actions:
            main([actions[choice]])


if __name__ == '__main__':
    run()

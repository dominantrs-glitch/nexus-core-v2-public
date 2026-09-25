"""Allowlisted connection diagnostics; never serialize an exception or payload."""
import math

REASONS = {
    'owner_stop': '停止ボタンによる停止',
    'cancelled': '接続処理の終了要求',
    'configuration_invalid': '接続設定を確認してください',
    'required_file_missing': '接続に必要なファイルが見つかりません',
    'access_denied': '接続に必要なファイルへのアクセスが拒否されました',
    'unexpected_error': '予期しない接続処理のエラー',
    'restart_limit': '接続処理の再起動が続けて失敗しました',
    'process_exit_unknown': '接続処理が終了しました。詳しい原因は未記録です',
    'status_unavailable': '表示を更新できません。接続処理の稼働と通信状態は別に確認が必要です',
    'status_write_failed': '表示用の記録を更新できません。接続処理は続けます',
    'history_write_failed': '履歴を保存できません。接続処理は続けます',
    'status_write_recovered': '表示用の記録を再開しました',
    'history_write_recovered': '履歴の保存を再開しました',
    'relay_access_denied': '中継への認証・アクセスが拒否されました',
    'relay_rate_limited': '中継から利用制限の応答がありました',
    'connector_already_online': '別のPC接続が中継に残っています',
    'relay_reply_rejected': '中継が応答を受け付けませんでした',
    'transport_timeout': '通信の応答待ちが時間切れになりました',
    'network_unavailable': 'ネットワーク通信に失敗しました',
    'connection_closed': '中継との接続が切れました',
    'invalid_relay_data': '中継からの要求を検証できませんでした',
    'transport_error': '中継との通信に失敗しました',
}
STATES = {'starting', 'connected', 'reconnecting', 'circuit_open', 'restarting',
          'stopped', 'error', 'unresponsive'}
ERROR_KINDS = {'permission', 'missing_file', 'timeout', 'os_error', 'invalid_value', 'unexpected'}


def issue(reason, error=None):
    result = {'reason': reason if reason in REASONS else 'unexpected_error'}
    if error is not None:
        result['error_kind'] = next((name for cls, name in (
            (PermissionError, 'permission'), (FileNotFoundError, 'missing_file'),
            (TimeoutError, 'timeout'), (OSError, 'os_error'), (ValueError, 'invalid_value')
        ) if isinstance(error, cls)), 'unexpected')
        for key in ('errno', 'winerror'):
            number = getattr(error, key, None)
            if type(number) is int and 0 <= number < 65536:
                result[key] = number
    return result


def clean_issue(value):
    if (not isinstance(value, dict) or not isinstance(value.get('reason'), str) or
            value['reason'] not in REASONS):
        return {}
    result = {'reason': value['reason']}
    if isinstance(value.get('error_kind'), str) and value['error_kind'] in ERROR_KINDS:
        result['error_kind'] = value['error_kind']
    for key in ('errno', 'winerror'):
        number = value.get(key)
        if type(number) is int and 0 <= number < 65536:
            result[key] = number
    return result


def clean_events(value):
    if not isinstance(value, list):
        return []
    result = []
    for event in value[-100:]:
        if not isinstance(event, dict):
            continue
        at, state, instance = event.get('time'), event.get('state'), event.get('instance')
        if (type(at) not in (int, float) or not math.isfinite(at) or at < 0 or
                not isinstance(state, str) or state not in STATES or not isinstance(instance, str) or
                not instance.isalnum() or len(instance) > 64):
            continue
        result.append(dict(time=at, state=state, instance=instance, **clean_issue(event)))
    return result

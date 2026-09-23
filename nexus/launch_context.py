"""Foreground launcher using existing Windows current-user protected credential."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def read_windows_token(path):
    if os.name != 'nt':
        raise RuntimeError('Windows current-user credential required')
    encoded = Path(path).read_text(encoding='utf-8-sig').strip()
    if not re.fullmatch(r'[0-9a-fA-F]{40,8192}', encoded):
        raise ValueError('unsupported protected credential')
    raw = bytes.fromhex(encoded)
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, target = Blob(len(raw), buffer), Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError('current Windows user cannot open connector credential')
    try:
        value = ctypes.string_at(target.data, target.size).decode('utf-16-le')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', value):
            raise ValueError('invalid protected connector token')
        return value
    finally:
        ctypes.memset(target.data, 0, target.size)
        kernel.LocalFree(target.data)


def main():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / 'cloudflare/wrangler.relay.remote.jsonc').read_text(encoding='utf-8-sig'))['vars']
    if config['RELAY_ENABLED'] != 'true':
        raise RuntimeError('relay disabled')
    task = Path(os.environ['LOCALAPPDATA']) / 'NexusCoreV2/workspaces/nexus-core-v2-pilot'
    environment = dict(os.environ)
    environment['NEXUS_CONNECTOR_TOKEN'] = read_windows_token(root / 'runtime/connector-token.dpapi')
    try:
        return subprocess.call([sys.executable, '-m', 'nexus.context_connector', '--root', str(task),
            '--grant', str(task / 'context-delivery-grant.json'), '--preview', str(task / 'context-delivery-preview.json'),
            '--origin', config['PUBLIC_ORIGIN'], '--relay-project', config['PROJECT_ID'], '--activate'],
            cwd=root, env=environment)
    finally:
        environment.pop('NEXUS_CONNECTOR_TOKEN', None)


if __name__ == '__main__':
    raise SystemExit(main())

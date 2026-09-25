"""Portable, authenticated owner backups. Keys stay outside the backup package."""
import base64
import json
import os
import subprocess
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def new_key(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: never replace the only key for an existing backup.
    with path.open('x', encoding='utf-8') as out:
        json.dump({'format': 'nexus-recovery-key.v1',
                   'key': base64.b64encode(AESGCM.generate_key(bit_length=256)).decode('ascii')}, out)
    os.chmod(path, 0o600)
    if os.name == 'nt':
        account=subprocess.check_output(['whoami'],text=True,creationflags=subprocess.CREATE_NO_WINDOW).strip()
        subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',account+':(F)'],check=True,
                       stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    return {'status': 'key_created', 'key_contents_returned': False,
            'instruction': 'Keep this key separately from the encrypted backup, including a copy outside this PC.'}


class PortableProtector:
    protection = 'portable-aes256gcm-keyfile'
    credential_portability = 'data portable with the separate recovery key; Windows-bound credentials require reauthorization'

    def __init__(self, key_file):
        original = Path(key_file).absolute()
        if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in [original, *original.parents]):
            raise ValueError('linked recovery key file')
        self.key_file = original.resolve(strict=True)
        if not self.key_file.is_file() or self.key_file.stat().st_size > 1024:
            raise ValueError('invalid recovery key file')
        try:
            value = json.loads(self.key_file.read_text(encoding='utf-8'))
            key = base64.b64decode(value['key'], validate=True)
            if value['format'] != 'nexus-recovery-key.v1' or len(key) != 32:
                raise ValueError()
            self._cipher = AESGCM(key)
        except Exception:
            raise ValueError('invalid recovery key file') from None

    def protect(self, data, entropy):
        nonce = os.urandom(12)
        return b'NXR2' + nonce + self._cipher.encrypt(nonce, data, b'NXR2\0' + entropy)

    def unprotect(self, data, entropy):
        try:
            if not data.startswith(b'NXR2') or len(data) < 32:
                raise ValueError()
            return self._cipher.decrypt(data[4:16], data[16:], b'NXR2\0' + entropy)
        except Exception:
            raise ValueError('recovery key mismatch or damaged encrypted data') from None

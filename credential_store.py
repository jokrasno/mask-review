"""Explicit OS credential stores. No plaintext or generic keyring fallback."""
import hashlib
import json
import os
from pathlib import Path
import sys

from desktop_platform import private_directory, private_write


class CredentialStore:
    def __init__(self, private, protect):
        self.private = Path(private)
        self.path = self.private / 'google-token.dpapi'
        self.protect = protect
        # Resolved private profiles are independent; moving a profile reconnects.
        identity = os.path.normcase(str(self.private.resolve()))
        self.service = 'MaskReview.Google.' + hashlib.sha256(identity.encode()).hexdigest()
        self.username = 'oauth-tokens'

    def _message(self):
        if os.name == 'nt':
            return 'Windows could not access saved Google credentials. Use this Windows user and sign in again.'
        if sys.platform == 'darwin':
            return 'Google credentials unavailable. Run Setup Mask Review.command, unlock your login Keychain, then reconnect.'
        if sys.platform.startswith('linux'):
            return 'Google credentials unavailable. Run sh desktop.sh --setup and unlock Secret Service in your desktop D-Bus session, then reconnect.'
        return 'Google credential storage is unsupported on this operating system. Local editing remains available.'

    def _backend(self):
        # Explicit built-ins bypass user config, plugins and environment-selected
        # backends. Import lazily so missing dependencies never stop local editing.
        if sys.platform == 'darwin':
            from keyring.backends.macOS import Keyring
        elif sys.platform.startswith('linux'):
            from keyring.backends.SecretService import Keyring
        else:
            raise ValueError('Google credential storage is unsupported on this operating system.')
        return Keyring()

    def load(self):
        try:
            private_directory(self.private)
            if os.name == 'nt':
                raw = self.protect(self.path.read_bytes(), True) if self.path.exists() else None
            else:
                raw = self._backend().get_password(self.service, self.username)
            if raw is None:
                return {}
            value = json.loads(raw)
            if (not isinstance(value, dict) or not isinstance(value.get('access_token'), str)
                    or not value['access_token'] or not isinstance(value.get('expires_at', 0), (int, float))
                    or any(k in value and not isinstance(value[k], str) for k in ('refresh_token', 'scope'))):
                raise ValueError('Invalid saved credential')
            return value
        except Exception:
            raise ValueError(self._message()) from None

    def save(self, tokens):
        try:
            private_directory(self.private)
            raw = json.dumps(tokens)
            if os.name == 'nt':
                private_write(self.path, self.protect(raw.encode()))
            else:
                backend = self._backend()
                backend.set_password(self.service, self.username, raw)
                if backend.get_password(self.service, self.username) != raw:
                    raise ValueError('Credential save was not confirmed')
        except Exception:
            raise ValueError(self._message()) from None

    def delete(self):
        try:
            if os.name == 'nt':
                self.path.unlink(missing_ok=True)
            else:
                backend = self._backend()
                if backend.get_password(self.service, self.username) is not None:
                    backend.delete_password(self.service, self.username)
                if backend.get_password(self.service, self.username) is not None:
                    raise ValueError('Credential deletion was not confirmed')
        except Exception:
            raise ValueError('Saved Google credentials could not be removed. Unlock the system credential store and retry Disconnect.') from None

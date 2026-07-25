"""Encrypt the per-user OAuth token at rest in the session (issue #4).

The session cookie is signed (tamper-proof) but its payload is only base64 — so the raw OAuth
token was **readable** by anyone who could see the cookie (the browser, a proxy log, an XSS
exfiltration). We keep the cookie transport but store the token **encrypted** with a key that
lives only server-side, so the token can never be lifted out of the cookie.

A dedicated ``WPSUBMIT_TOKEN_ENCRYPTION_KEY`` (a Fernet key) is preferred; if absent we derive a
key from ``session_secret`` (SHA-256 → urlsafe base64). Deriving is safe for this threat because
the client never holds ``session_secret``, so it still cannot decrypt — a dedicated key just lets
you rotate the two independently.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class TokenCipherError(RuntimeError):
    """The stored token could not be decrypted (wrong key, tampering, or rotation)."""


def _fernet_key_from(secret: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


class TokenCipher:
    """Symmetric encryption for the at-rest OAuth token."""

    def __init__(self, *, encryption_key: str | None, session_secret: str) -> None:
        if encryption_key:
            key = encryption_key.encode()
        else:
            key = _fernet_key_from(session_secret)
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise TokenCipherError("could not decrypt session token") from exc

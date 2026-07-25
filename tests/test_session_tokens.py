"""Token-at-rest encryption (issue #4): the OAuth token is never stored in plaintext."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.auth import TokenCipher, TokenCipherError
from app.main import get_github_client


def test_cipher_roundtrip_and_hides_plaintext():
    cipher = TokenCipher(encryption_key=None, session_secret="s3cret")
    enc = cipher.encrypt("gho_realtoken")
    assert enc != "gho_realtoken"
    assert "gho_realtoken" not in enc  # not recoverable by eyeballing the cookie
    assert cipher.decrypt(enc) == "gho_realtoken"


def test_derived_key_is_deterministic_per_secret():
    a = TokenCipher(encryption_key=None, session_secret="same")
    b = TokenCipher(encryption_key=None, session_secret="same")
    assert b.decrypt(a.encrypt("tok")) == "tok"  # same secret → interoperable
    c = TokenCipher(encryption_key=None, session_secret="different")
    with pytest.raises(TokenCipherError):
        c.decrypt(a.encrypt("tok"))  # different secret → cannot decrypt


def test_explicit_key_is_used():
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(encryption_key=key, session_secret="ignored")
    assert cipher.decrypt(cipher.encrypt("tok")) == "tok"


def test_get_github_client_decrypts_session_token():
    cipher = TokenCipher(encryption_key=None, session_secret="s")
    req = SimpleNamespace(
        session={"gh_token": cipher.encrypt("gho_abc")},
        app=SimpleNamespace(state=SimpleNamespace(token_cipher=cipher)),
    )
    client = get_github_client(req)
    assert client.token == "gho_abc"


def test_get_github_client_401_on_undecryptable_token():
    cipher = TokenCipher(encryption_key=None, session_secret="s")
    other = TokenCipher(encryption_key=None, session_secret="rotated-key")
    session = {"gh_token": other.encrypt("gho_abc")}
    req = SimpleNamespace(
        session=session,
        app=SimpleNamespace(state=SimpleNamespace(token_cipher=cipher)),
    )
    with pytest.raises(HTTPException) as exc:
        get_github_client(req)
    assert exc.value.status_code == 401
    assert session == {}  # session cleared so the user is forced to re-login


def test_get_github_client_401_when_no_token():
    req = SimpleNamespace(session={}, app=SimpleNamespace(state=SimpleNamespace()))
    with pytest.raises(HTTPException) as exc:
        get_github_client(req)
    assert exc.value.status_code == 401

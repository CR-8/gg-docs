"""Secrets at rest and credentials in transit.

seal/unseal: provider tokens, LLM keys, integration keys and the legacy MCP token are stored encrypted
(Fernet: AES-128-CBC + HMAC-SHA256) with a key that is not in the database: TOKEN_ENCRYPTION_KEY, or a key file
created once in DATA_DIR (a different volume from MongoDB's). mint/digest: credentials the portal issues are random
(256 bits) and stored only as SHA-256 digests, so a database dump can't be replayed.
"""
import base64
import hashlib
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

from . import DATA


def _key():
    raw = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
    if not raw:
        path = DATA / "token-encryption.key"
        if not path.exists():
            DATA.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
            print(f"[secret] created {path}; back it up or set TOKEN_ENCRYPTION_KEY", flush=True)
        raw = path.read_text().strip()
    # ponytail: one key; MultiFernet with TOKEN_ENCRYPTION_KEY_OLD when keys need rotating without a re-entry
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


_fernet = None


def seal(text):
    global _fernet
    if not text:
        return None
    _fernet = _fernet or _key()
    return "v1:" + _fernet.encrypt(str(text).encode()).decode()


def unseal(blob):
    """The plaintext, or "" when there is none or it can't be decrypted (the key changed): callers then treat it as
    missing, so people sign in again or re-enter a key instead of the portal failing."""
    global _fernet
    if not blob or not str(blob).startswith("v1:"):
        return ""
    _fernet = _fernet or _key()
    try:
        return _fernet.decrypt(blob[3:].encode()).decode()
    except InvalidToken:
        return ""


def digest(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


def mint(prefix):
    """(plaintext, digest): the plaintext is shown once and never stored."""
    token = prefix + secrets.token_urlsafe(32)
    return token, digest(token)

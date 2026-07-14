"""AES-256-GCM field-level encryption for sensitive PII (e.g. passport numbers).

Raw plaintext is NEVER written to the database — only the base64url-encoded
nonce+ciphertext produced by ``encrypt_field``.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings


def _get_key(settings: Settings) -> bytes:
    if not settings.ENCRYPTION_KEY:
        raise ValueError("ENCRYPTION_KEY is not configured")
    return base64.urlsafe_b64decode(settings.ENCRYPTION_KEY)


def encrypt_field(plaintext: str, settings: Settings) -> str:
    """AES-256-GCM encrypt.  Returns base64url(12-byte nonce || ciphertext+tag)."""
    key = _get_key(settings)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_field(ciphertext_b64: str, settings: Settings) -> str:
    """AES-256-GCM decrypt.  Inverse of ``encrypt_field``."""
    key = _get_key(settings)
    raw = base64.urlsafe_b64decode(ciphertext_b64)
    nonce, ct = raw[:12], raw[12:]
    result = AESGCM(key).decrypt(nonce, ct, None)
    return bytes(result).decode()

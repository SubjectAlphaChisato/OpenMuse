"""Envelope encryption for provider credentials (design section 2).

A per-record data key (DEK) encrypts the secret; the key-encryption key (KEK) wraps the DEK.
The KEK exists only in the broker's environment (OPA_BROKER_KEK, base64 of 32 bytes).
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opa.db.models import BrokerCredential


class VaultError(Exception):
    pass


def generate_kek() -> str:
    return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()


class Vault:
    def __init__(self, kek_b64: str) -> None:
        try:
            kek = base64.b64decode(kek_b64, validate=True)
        except Exception:
            raise VaultError("OPA_BROKER_KEK must be base64") from None
        if len(kek) != 32:
            raise VaultError("OPA_BROKER_KEK must decode to 32 bytes")
        self._kek = AESGCM(kek)

    @staticmethod
    def _seal(key: AESGCM, data: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + key.encrypt(nonce, data, aad)

    @staticmethod
    def _open(key: AESGCM, blob: bytes, aad: bytes) -> bytes:
        try:
            return key.decrypt(blob[:12], blob[12:], aad)
        except Exception:
            raise VaultError("credential could not be decrypted (wrong key or tampered record)") from None

    def encrypt(self, secret: dict[str, Any], connection_id: str) -> tuple[bytes, bytes]:
        dek = AESGCM.generate_key(bit_length=256)
        aad = connection_id.encode()
        wrapped = self._seal(self._kek, dek, aad)
        ciphertext = self._seal(AESGCM(dek), json.dumps(secret).encode(), aad)
        return wrapped, ciphertext

    def decrypt(self, wrapped: bytes, ciphertext: bytes, connection_id: str) -> dict[str, Any]:
        aad = connection_id.encode()
        dek = self._open(self._kek, wrapped, aad)
        return json.loads(self._open(AESGCM(dek), ciphertext, aad))

    async def store(self, db: AsyncSession, connection_id: str, secret: dict[str, Any]) -> BrokerCredential:
        wrapped, ct = self.encrypt(secret, connection_id)
        row = await db.scalar(select(BrokerCredential).where(BrokerCredential.connection_id == connection_id))
        if row is None:
            row = BrokerCredential(connection_id=connection_id, dek_wrapped=wrapped, secret_enc=ct)
            db.add(row)
        else:
            row.dek_wrapped, row.secret_enc = wrapped, ct
        await db.flush()
        return row

    async def load(self, db: AsyncSession, connection_id: str) -> dict[str, Any] | None:
        row = await db.scalar(select(BrokerCredential).where(BrokerCredential.connection_id == connection_id))
        return None if row is None else self.decrypt(row.dek_wrapped, row.secret_enc, connection_id)

    async def delete(self, db: AsyncSession, connection_id: str) -> None:
        row = await db.scalar(select(BrokerCredential).where(BrokerCredential.connection_id == connection_id))
        if row is not None:
            await db.delete(row)

#!/usr/bin/env python3
"""Encrypted protected-memory and protected-artifact storage.

This module deliberately does not provide a software unlock provider. A
production caller must inject a hardware-backed provider such as a YubiKey
FIDO2 HMAC-secret/PRF adapter. Tests may inject a deterministic provider.
"""

import base64
import datetime
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
import struct
import tempfile
import threading
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


SCHEMA_VERSION = 1
VAULT_KEY_BYTES = 32
DATA_KEY_BYTES = 32
NONCE_BYTES = 12
ARTIFACT_NONCE_PREFIX_BYTES = 8
ARTIFACT_CHUNK_SIZE = 1024 * 1024
ARTIFACT_MAGIC = b"EVA-PMF1\x00"


class ProtectedMemoryError(Exception):
    """Base error for protected-memory operations."""


class VaultLockedError(ProtectedMemoryError):
    """Raised when encrypted content is requested without an unlock grant."""


class VaultIntegrityError(ProtectedMemoryError):
    """Raised when protected metadata or ciphertext fails validation."""


class UnlockError(ProtectedMemoryError):
    """Raised when the external key provider cannot unlock the vault."""


class VaultKeyProvider(ABC):
    """Hardware-backed vault-key wrapping contract."""

    provider_name = "unspecified"

    @abstractmethod
    def wrap_vault_key(self, vault_key, challenge):
        """Return provider-specific ciphertext for a vault key."""

    @abstractmethod
    def unwrap_vault_key(self, wrapped_vault_key, challenge):
        """Return the vault key after user-presence authentication."""


class YkmanChallengeResponseProvider(VaultKeyProvider):
    """YubiKey OTP challenge-response compatibility provider.

    The selected OTP slot must already be configured with ``--touch``. This
    provider is a compatibility path for devices without the FIDO2 PRF flow;
    it never falls back to a software secret.
    """

    provider_name = "yubikey-challenge-response"

    def __init__(self, executable="ykman", slot=2, timeout=15):
        self.executable = _require_public_text(executable, "executable", 256)
        try:
            self.slot = int(slot)
        except (TypeError, ValueError):
            raise ValueError("YubiKey challenge-response slot must be 1 or 2")
        if self.slot not in (1, 2):
            raise ValueError("YubiKey challenge-response slot must be 1 or 2")
        self.timeout = max(1, int(timeout))

    def _response(self, challenge):
        challenge = _require_bytes(challenge, "challenge")
        try:
            result = subprocess.run(
                [self.executable, "otp", "calculate", str(self.slot), challenge.hex()],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise UnlockError("YubiKey challenge-response operation failed") from error
        if result.returncode != 0:
            raise UnlockError("YubiKey challenge-response operation was rejected")
        raw_response = (result.stdout or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40,128}", raw_response):
            raise UnlockError("YubiKey returned an invalid challenge-response value")
        return bytes.fromhex(raw_response)

    @staticmethod
    def _derive_key(challenge, response):
        return hashlib.sha256(b"eva-protected-memory-yubikey-v1\x00" + challenge + response).digest()

    def wrap_vault_key(self, vault_key, challenge):
        vault_key = _require_key(vault_key, "vault key")
        challenge = _require_bytes(challenge, "challenge")
        nonce = secrets.token_bytes(NONCE_BYTES)
        wrapped = AESGCM(self._derive_key(challenge, self._response(challenge))).encrypt(
            nonce, vault_key, b"eva-protected-memory-vault-key-v1"
        )
        return nonce + wrapped

    def unwrap_vault_key(self, wrapped_vault_key, challenge):
        wrapped_vault_key = _require_bytes(wrapped_vault_key, "wrapped vault key")
        challenge = _require_bytes(challenge, "challenge")
        if len(wrapped_vault_key) <= NONCE_BYTES:
            raise UnlockError("wrapped vault key is invalid")
        nonce = wrapped_vault_key[:NONCE_BYTES]
        try:
            return AESGCM(self._derive_key(challenge, self._response(challenge))).decrypt(
                nonce, wrapped_vault_key[NONCE_BYTES:], b"eva-protected-memory-vault-key-v1"
            )
        except Exception as error:
            raise UnlockError("YubiKey did not unlock this vault") from error


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _require_bytes(value, name):
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(name + " must be bytes-like")
    return bytes(value)


def _require_key(value, name):
    value = _require_bytes(value, name)
    if len(value) != VAULT_KEY_BYTES:
        raise VaultIntegrityError(name + " must be exactly 32 bytes")
    return value


def _require_public_text(value, name, max_length):
    value = str(value or "").strip()
    if not value:
        raise ValueError(name + " is required")
    if len(value) > max_length or "\x00" in value:
        raise ValueError(name + " is invalid")
    return value


def _aad(record_id, kind, key_version, chunk_index=None):
    value = {
        "record_id": record_id,
        "kind": kind,
        "key_version": int(key_version),
        "schema_version": SCHEMA_VERSION,
    }
    if chunk_index is not None:
        value["chunk_index"] = int(chunk_index)
    return _canonical_json(value)


def _encrypt(key, plaintext, associated_data):
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(_require_key(key, "encryption key")).encrypt(nonce, plaintext, associated_data)
    return nonce, ciphertext


def _decrypt(key, nonce, ciphertext, associated_data):
    try:
        return AESGCM(_require_key(key, "decryption key")).decrypt(nonce, ciphertext, associated_data)
    except Exception as error:
        raise VaultIntegrityError("protected ciphertext authentication failed") from error


def _wrap_data_key(vault_key, data_key, record_id, kind, key_version):
    nonce, wrapped = _encrypt(vault_key, data_key, _aad(record_id, kind + ":data-key", key_version))
    return nonce, wrapped


def _unwrap_data_key(vault_key, wrapped_nonce, wrapped_key, record_id, kind, key_version):
    return _require_key(
        _decrypt(vault_key, wrapped_nonce, wrapped_key, _aad(record_id, kind + ":data-key", key_version)),
        "data key",
    )


def _encode_memory_value(value, mime_type=""):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "type": "binary",
            "mime_type": str(mime_type or "application/octet-stream"),
            "data": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, str):
        return {"type": "text", "value": value}
    try:
        json.dumps(value)
    except (TypeError, ValueError) as error:
        raise TypeError("protected memory value must be text, JSON-compatible, or bytes-like") from error
    return {"type": "json", "value": value}


def _decode_memory_value(payload):
    if not isinstance(payload, dict):
        raise VaultIntegrityError("protected memory payload is invalid")
    value_type = payload.get("type")
    if value_type == "binary":
        try:
            return base64.b64decode(payload["data"], validate=True)
        except Exception as error:
            raise VaultIntegrityError("protected binary payload is invalid") from error
    if value_type == "text":
        return payload.get("value", "")
    if value_type == "json":
        return payload.get("value")
    raise VaultIntegrityError("protected memory value type is invalid")


class ProtectedVault:
    """Separate SQLite index plus encrypted memory and artifact storage."""

    def __init__(self, root_dir, artifact_chunk_size=ARTIFACT_CHUNK_SIZE, artifact_dir=None):
        self.root_dir = Path(root_dir).expanduser()
        self.artifact_dir = Path(artifact_dir).expanduser() if artifact_dir else self.root_dir / "protected-artifacts"
        self.db_path = self.root_dir / "protected.sqlite3"
        self.artifact_chunk_size = int(artifact_chunk_size)
        if self.artifact_chunk_size < 1024:
            raise ValueError("artifact_chunk_size must be at least 1024 bytes")
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._restrict_directory(self.root_dir)
        self._restrict_directory(self.artifact_dir)
        self._lock = threading.RLock()
        self._vault_key = None
        self._provider = None
        self._connection = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.row_factory = sqlite3.Row
        self._restrict_file(self.db_path)
        self._init_schema()

    @staticmethod
    def _restrict_directory(directory):
        if os.name != "nt":
            os.chmod(directory, 0o700)

    @staticmethod
    def _restrict_file(file_path):
        if os.name != "nt":
            os.chmod(file_path, 0o600)

    def _init_schema(self):
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ProtectedMemoryIndex (
                    RecordId TEXT PRIMARY KEY,
                    PublicLabel TEXT NOT NULL,
                    Category TEXT NOT NULL,
                    Status TEXT NOT NULL DEFAULT 'active',
                    RequiresYubiKey INTEGER NOT NULL DEFAULT 1,
                    MimeType TEXT NOT NULL DEFAULT '',
                    SizeBytes INTEGER NOT NULL DEFAULT 0,
                    KeyVersion INTEGER NOT NULL,
                    CreatedAt TEXT NOT NULL,
                    UpdatedAt TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ProtectedMemoryRecords (
                    RecordId TEXT PRIMARY KEY REFERENCES ProtectedMemoryIndex(RecordId),
                    Ciphertext BLOB NOT NULL,
                    Nonce BLOB NOT NULL,
                    WrappedDataKey BLOB NOT NULL,
                    DataKeyNonce BLOB NOT NULL,
                    Algorithm TEXT NOT NULL,
                    SchemaVersion INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ProtectedArtifactIndex (
                    RecordId TEXT PRIMARY KEY,
                    PublicLabel TEXT NOT NULL,
                    Category TEXT NOT NULL,
                    Status TEXT NOT NULL DEFAULT 'active',
                    RequiresYubiKey INTEGER NOT NULL DEFAULT 1,
                    MimeType TEXT NOT NULL DEFAULT '',
                    SizeBytes INTEGER NOT NULL DEFAULT 0,
                    ChunkSize INTEGER NOT NULL,
                    ChunkCount INTEGER NOT NULL,
                    KeyVersion INTEGER NOT NULL,
                    CreatedAt TEXT NOT NULL,
                    UpdatedAt TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ProtectedArtifactRecords (
                    RecordId TEXT PRIMARY KEY REFERENCES ProtectedArtifactIndex(RecordId),
                    OpaqueName TEXT NOT NULL UNIQUE,
                    FileNoncePrefix BLOB NOT NULL,
                    WrappedDataKey BLOB NOT NULL,
                    DataKeyNonce BLOB NOT NULL,
                    Algorithm TEXT NOT NULL,
                    SchemaVersion INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ProtectedMemoryKeySlots (
                    SlotId TEXT PRIMARY KEY,
                    Provider TEXT NOT NULL,
                    Challenge BLOB NOT NULL,
                    WrappedVaultKey BLOB NOT NULL,
                    KeyVersion INTEGER NOT NULL,
                    Status TEXT NOT NULL DEFAULT 'active',
                    CreatedAt TEXT NOT NULL,
                    LastUsedAt TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_protected_memory_status
                    ON ProtectedMemoryIndex(Status);
                CREATE INDEX IF NOT EXISTS idx_protected_artifact_status
                    ON ProtectedArtifactIndex(Status);
                """
            )

    @property
    def is_unlocked(self):
        return self._vault_key is not None

    def _require_unlocked(self):
        if self._vault_key is None:
            raise VaultLockedError("protected memory is locked")
        return self._vault_key

    def enrolled_slots(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT SlotId, Provider, KeyVersion, Status, CreatedAt, LastUsedAt "
                "FROM ProtectedMemoryKeySlots WHERE Status = 'active' ORDER BY CreatedAt"
            ).fetchall()
            return [dict(row) for row in rows]

    def enroll(self, provider, slot_id=None):
        if not isinstance(provider, VaultKeyProvider):
            raise TypeError("provider must implement VaultKeyProvider")
        slot_id = str(slot_id or "slot-" + uuid.uuid4().hex[:12])
        _require_public_text(slot_id, "slot_id", 128)
        challenge = secrets.token_bytes(32)
        vault_key = secrets.token_bytes(VAULT_KEY_BYTES)
        try:
            wrapped_vault_key = _require_bytes(
                provider.wrap_vault_key(vault_key, challenge), "wrapped vault key"
            )
        except Exception as error:
            raise UnlockError("YubiKey enrollment failed") from error
        now = _utc_now()
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT 1 FROM ProtectedMemoryKeySlots WHERE SlotId = ?", (slot_id,)
            ).fetchone()
            if existing:
                raise ValueError("slot_id already exists")
            self._connection.execute(
                "INSERT INTO ProtectedMemoryKeySlots "
                "(SlotId, Provider, Challenge, WrappedVaultKey, KeyVersion, Status, CreatedAt) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?)",
                (slot_id, provider.provider_name, challenge, wrapped_vault_key, 1, now),
            )
        self._provider = provider
        self._vault_key = vault_key
        return {"slot_id": slot_id, "provider": provider.provider_name, "key_version": 1}

    def unlock(self, provider, slot_id=None):
        if not isinstance(provider, VaultKeyProvider):
            raise TypeError("provider must implement VaultKeyProvider")
        with self._lock:
            if slot_id:
                row = self._connection.execute(
                    "SELECT * FROM ProtectedMemoryKeySlots WHERE SlotId = ? AND Status = 'active'",
                    (slot_id,),
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT * FROM ProtectedMemoryKeySlots WHERE Status = 'active' ORDER BY CreatedAt LIMIT 1"
                ).fetchone()
        if not row:
            raise UnlockError("no active protected-memory key slot exists")
        if row["Provider"] != provider.provider_name:
            raise UnlockError("the configured key provider does not match this vault")
        try:
            vault_key = _require_key(
                provider.unwrap_vault_key(row["WrappedVaultKey"], row["Challenge"]),
                "unwrapped vault key",
            )
        except UnlockError:
            raise
        except Exception as error:
            raise UnlockError("YubiKey unlock failed") from error
        self._provider = provider
        self._vault_key = vault_key
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE ProtectedMemoryKeySlots SET LastUsedAt = ? WHERE SlotId = ?",
                (_utc_now(), row["SlotId"]),
            )
        return {"slot_id": row["SlotId"], "provider": row["Provider"], "key_version": row["KeyVersion"]}

    def lock(self):
        self._vault_key = None
        self._provider = None

    def list_metadata(self):
        with self._lock:
            memory_rows = self._connection.execute(
                "SELECT RecordId, PublicLabel, Category, Status, RequiresYubiKey, MimeType, "
                "SizeBytes, KeyVersion, CreatedAt, UpdatedAt FROM ProtectedMemoryIndex "
                "WHERE Status = 'active' ORDER BY CreatedAt"
            ).fetchall()
            artifact_rows = self._connection.execute(
                "SELECT RecordId, PublicLabel, Category, Status, RequiresYubiKey, MimeType, "
                "SizeBytes, ChunkSize, ChunkCount, KeyVersion, CreatedAt, UpdatedAt "
                "FROM ProtectedArtifactIndex WHERE Status = 'active' ORDER BY CreatedAt"
            ).fetchall()
        return [dict(row, kind="memory") for row in memory_rows] + [dict(row, kind="artifact") for row in artifact_rows]

    def put_memory(self, value, public_label="protected memory record", category="general", mime_type=""):
        vault_key = self._require_unlocked()
        public_label = _require_public_text(public_label, "public_label", 160)
        category = _require_public_text(category, "category", 80)
        record_id = "pm-" + uuid.uuid4().hex
        key_version = 1
        payload = _canonical_json(_encode_memory_value(value, mime_type))
        data_key = secrets.token_bytes(DATA_KEY_BYTES)
        data_key_nonce, wrapped_data_key = _wrap_data_key(vault_key, data_key, record_id, "memory", key_version)
        nonce, ciphertext = _encrypt(data_key, payload, _aad(record_id, "memory", key_version))
        now = _utc_now()
        size_bytes = len(payload)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO ProtectedMemoryIndex "
                "(RecordId, PublicLabel, Category, Status, RequiresYubiKey, MimeType, SizeBytes, "
                "KeyVersion, CreatedAt, UpdatedAt) VALUES (?, ?, ?, 'active', 1, ?, ?, ?, ?, ?)",
                (record_id, public_label, category, str(mime_type or ""), size_bytes, key_version, now, now),
            )
            self._connection.execute(
                "INSERT INTO ProtectedMemoryRecords "
                "(RecordId, Ciphertext, Nonce, WrappedDataKey, DataKeyNonce, Algorithm, SchemaVersion) "
                "VALUES (?, ?, ?, ?, ?, 'AES-256-GCM', ?)",
                (record_id, ciphertext, nonce, wrapped_data_key, data_key_nonce, SCHEMA_VERSION),
            )
        return record_id

    def get_memory(self, record_id):
        vault_key = self._require_unlocked()
        with self._lock:
            row = self._connection.execute(
                "SELECT i.*, r.Ciphertext, r.Nonce, r.WrappedDataKey, r.DataKeyNonce, "
                "r.Algorithm, r.SchemaVersion FROM ProtectedMemoryIndex i "
                "JOIN ProtectedMemoryRecords r ON r.RecordId = i.RecordId "
                "WHERE i.RecordId = ? AND i.Status = 'active'",
                (record_id,),
            ).fetchone()
        if not row:
            raise KeyError("protected memory record not found")
        data_key = _unwrap_data_key(
            vault_key, row["DataKeyNonce"], row["WrappedDataKey"], record_id, "memory", row["KeyVersion"]
        )
        payload = _decrypt(data_key, row["Nonce"], row["Ciphertext"], _aad(record_id, "memory", row["KeyVersion"]))
        return {
            "record_id": record_id,
            "public_label": row["PublicLabel"],
            "category": row["Category"],
            "mime_type": row["MimeType"],
            "value": _decode_memory_value(json.loads(payload.decode("utf-8"))),
        }

    def put_artifact(self, content, public_label="protected artifact", category="file", mime_type=""):
        vault_key = self._require_unlocked()
        content = _require_bytes(content, "content")
        public_label = _require_public_text(public_label, "public_label", 160)
        category = _require_public_text(category, "category", 80)
        record_id = "pa-" + uuid.uuid4().hex
        key_version = 1
        chunk_size = self.artifact_chunk_size
        chunk_count = (len(content) + chunk_size - 1) // chunk_size
        data_key = secrets.token_bytes(DATA_KEY_BYTES)
        data_key_nonce, wrapped_data_key = _wrap_data_key(vault_key, data_key, record_id, "artifact", key_version)
        nonce_prefix = secrets.token_bytes(ARTIFACT_NONCE_PREFIX_BYTES)
        opaque_name = record_id + ".pmf"
        target = self.artifact_dir / opaque_name
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.artifact_dir, prefix=".pending-", delete=False) as output:
                temp_path = Path(output.name)
                self._restrict_file(temp_path)
                output.write(ARTIFACT_MAGIC)
                for chunk_index in range(chunk_count):
                    start = chunk_index * chunk_size
                    chunk = content[start:start + chunk_size]
                    nonce = nonce_prefix + chunk_index.to_bytes(4, "big")
                    associated_data = _aad(record_id, "artifact", key_version, chunk_index)
                    encrypted = AESGCM(data_key).encrypt(nonce, chunk, associated_data)
                    output.write(struct.pack(">I", len(encrypted)))
                    output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, target)
            self._restrict_file(target)
            temp_path = None
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
        now = _utc_now()
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    "INSERT INTO ProtectedArtifactIndex "
                    "(RecordId, PublicLabel, Category, Status, RequiresYubiKey, MimeType, SizeBytes, "
                    "ChunkSize, ChunkCount, KeyVersion, CreatedAt, UpdatedAt) VALUES (?, ?, ?, 'active', 1, ?, ?, ?, ?, ?, ?, ?)",
                    (record_id, public_label, category, str(mime_type or ""), len(content), chunk_size, chunk_count, key_version, now, now),
                )
                self._connection.execute(
                    "INSERT INTO ProtectedArtifactRecords "
                    "(RecordId, OpaqueName, FileNoncePrefix, WrappedDataKey, DataKeyNonce, Algorithm, SchemaVersion) "
                    "VALUES (?, ?, ?, ?, ?, 'AES-256-GCM-CHUNKED', ?)",
                    (record_id, opaque_name, nonce_prefix, wrapped_data_key, data_key_nonce, SCHEMA_VERSION),
                )
            except Exception:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                raise
        return record_id

    def _artifact_path(self, opaque_name):
        base = self.artifact_dir.resolve()
        path = (self.artifact_dir / opaque_name).resolve()
        if path.parent != base or path.name != opaque_name:
            raise VaultIntegrityError("protected artifact path is invalid")
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError as error:
            raise VaultIntegrityError("protected artifact file is missing") from error
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise VaultIntegrityError("protected artifact file is not a regular file")
        return path

    def iter_artifact_chunks(self, record_id):
        vault_key = self._require_unlocked()
        with self._lock:
            row = self._connection.execute(
                "SELECT i.*, r.OpaqueName, r.FileNoncePrefix, r.WrappedDataKey, r.DataKeyNonce "
                "FROM ProtectedArtifactIndex i JOIN ProtectedArtifactRecords r ON r.RecordId = i.RecordId "
                "WHERE i.RecordId = ? AND i.Status = 'active'",
                (record_id,),
            ).fetchone()
        if not row:
            raise KeyError("protected artifact not found")
        data_key = _unwrap_data_key(
            vault_key, row["DataKeyNonce"], row["WrappedDataKey"], record_id, "artifact", row["KeyVersion"]
        )
        path = self._artifact_path(row["OpaqueName"])
        with path.open("rb") as source:
            if source.read(len(ARTIFACT_MAGIC)) != ARTIFACT_MAGIC:
                raise VaultIntegrityError("protected artifact header is invalid")
            for chunk_index in range(row["ChunkCount"]):
                length_bytes = source.read(4)
                if len(length_bytes) != 4:
                    raise VaultIntegrityError("protected artifact chunk header is truncated")
                encrypted_length = struct.unpack(">I", length_bytes)[0]
                encrypted = source.read(encrypted_length)
                if len(encrypted) != encrypted_length:
                    raise VaultIntegrityError("protected artifact chunk is truncated")
                nonce = row["FileNoncePrefix"] + chunk_index.to_bytes(4, "big")
                try:
                    yield AESGCM(data_key).decrypt(
                        nonce, encrypted, _aad(record_id, "artifact", row["KeyVersion"], chunk_index)
                    )
                except Exception as error:
                    raise VaultIntegrityError("protected artifact authentication failed") from error
            if source.read(1):
                raise VaultIntegrityError("protected artifact contains trailing data")

    def get_artifact(self, record_id):
        return b"".join(self.iter_artifact_chunks(record_id))

    def delete(self, record_id):
        self._require_unlocked()
        with self._lock:
            row = self._connection.execute(
                "SELECT OpaqueName FROM ProtectedArtifactRecords WHERE RecordId = ?", (record_id,)
            ).fetchone()
            with self._connection:
                self._connection.execute("DELETE FROM ProtectedMemoryRecords WHERE RecordId = ?", (record_id,))
                self._connection.execute("DELETE FROM ProtectedMemoryIndex WHERE RecordId = ?", (record_id,))
                self._connection.execute("DELETE FROM ProtectedArtifactRecords WHERE RecordId = ?", (record_id,))
                self._connection.execute("DELETE FROM ProtectedArtifactIndex WHERE RecordId = ?", (record_id,))
            if row:
                try:
                    self._artifact_path(row["OpaqueName"]).unlink()
                except FileNotFoundError:
                    pass

    def close(self):
        self.lock()
        with self._lock:
            self._connection.close()

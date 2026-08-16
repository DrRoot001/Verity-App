"""Security primitives: password hashing, tokens, envelope encryption.

PRD §10.2 (auth controls), §28.1 (encryption at rest), §28.2 (token rotation).
Kept in ``platform`` because identity, admin and realtime all need them, and
none of them should be reimplementing crypto.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any, Final

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type as Argon2Type
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from verity.platform.config import settings
from verity.platform.errors import AppError, ErrorCode

# ── Passwords (PRD §10.2 security controls) ──────────────────────────

_hasher: Final = PasswordHasher(
    time_cost=settings.argon2_time_cost,
    memory_cost=settings.argon2_memory_kib,
    parallelism=settings.argon2_parallelism,
    hash_len=32,
    salt_len=16,
    type=Argon2Type.ID,
)

MIN_PASSWORD_LENGTH: Final = 10
MAX_PASSWORD_LENGTH: Final = 1024  # bound the work factor; DoS guard


def normalize_password(raw: str) -> str:
    """NFKC so visually identical passwords hash identically across platforms."""
    return unicodedata.normalize("NFKC", raw)


def hash_password(raw: str) -> str:
    return _hasher.hash(normalize_password(raw))


def verify_password(raw: str, stored_hash: str) -> bool:
    try:
        _hasher.verify(stored_hash, normalize_password(raw))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash predates a parameter increase; rehash on next login."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def dummy_verify() -> None:
    """Burn equivalent CPU on unknown-account logins.

    Without this, a missing account returns measurably faster than a wrong
    password, which is a free account-enumeration oracle (PRD §28.3).
    """
    _hasher.verify(_DUMMY_HASH, "not-the-password")


_DUMMY_HASH: Final = _hasher.hash("verity-timing-equalizer")


# ── Opaque tokens (refresh, verification, reset, WS tickets) ─────────


@dataclass(frozen=True, slots=True)
class OpaqueToken:
    """A high-entropy secret and the digest we persist.

    The plaintext goes to the client exactly once; only ``digest`` is stored, so
    a database disclosure does not yield usable tokens.
    """

    plaintext: str
    digest: bytes


def generate_token(byte_length: int = 32) -> OpaqueToken:
    plaintext = secrets.token_urlsafe(byte_length)
    return OpaqueToken(plaintext=plaintext, digest=hash_token(plaintext))


def hash_token(plaintext: str) -> bytes:
    """SHA-256 is correct here: the input is already 256 bits of entropy."""
    return hashlib.sha256(plaintext.encode("utf-8")).digest()


def tokens_equal(candidate: str, stored_digest: bytes) -> bool:
    return hmac.compare_digest(hash_token(candidate), stored_digest)


def hash_ip(ip: str | None) -> bytes | None:
    """Store a keyed digest so session rows never hold a raw IP (PRD §29.1)."""
    if not ip:
        return None
    key = settings.data_encryption_key.get_secret_value().encode() or b"verity-local"
    return hmac.new(key, ip.encode("utf-8"), hashlib.sha256).digest()


# ── Access tokens (JWT, EdDSA) ───────────────────────────────────────

_ALGORITHM: Final = "EdDSA"
_ISSUER: Final = "verity"


class TokenAudience:
    ACCESS = "verity:access"
    WEBSOCKET = "verity:ws"


def issue_access_token(
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    audience: str = TokenAudience.ACCESS,
    ttl_seconds: int | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    private_key = settings.jwt_private_key_pem.get_secret_value()
    if not private_key:
        raise AppError.internal("Signing key is not configured.")

    now = dt.datetime.now(dt.UTC)
    ttl = ttl_seconds if ttl_seconds is not None else settings.access_token_ttl_seconds
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "aud": audience,
        "sub": str(user_id),
        "sid": str(session_id),
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=ttl)).timestamp()),
        "jti": secrets.token_urlsafe(12),
        **(extra_claims or {}),
    }
    return jwt.encode(claims, private_key, algorithm=_ALGORITHM, headers={"kid": settings.jwt_kid})


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID
    audience: str
    expires_at: dt.datetime
    raw: dict[str, Any]


def decode_access_token(token: str, *, audience: str = TokenAudience.ACCESS) -> AccessTokenClaims:
    public_key = settings.jwt_public_key_pem
    if not public_key:
        raise AppError.internal("Verification key is not configured.")

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            public_key,
            algorithms=[_ALGORITHM],
            audience=audience,
            issuer=_ISSUER,
            options={"require": ["exp", "iat", "sub", "sid", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AppError(ErrorCode.TOKEN_EXPIRED, "Your session has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AppError(ErrorCode.TOKEN_INVALID, "This credential is not valid.") from exc

    try:
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            session_id=uuid.UUID(payload["sid"]),
            audience=str(payload["aud"]),
            expires_at=dt.datetime.fromtimestamp(payload["exp"], tz=dt.UTC),
            raw=payload,
        )
    except (KeyError, ValueError) as exc:
        raise AppError(ErrorCode.TOKEN_INVALID, "This credential is malformed.") from exc


# ── Envelope encryption (PRD §28.1) ──────────────────────────────────

_AAD_SEPARATOR: Final = b"|"


def _data_key() -> bytes:
    raw = settings.data_encryption_key.get_secret_value()
    if not raw:
        raise AppError.internal("Data encryption key is not configured.")
    return base64.b64decode(raw)


def encrypt_field(plaintext: str, *, context: str) -> bytes:
    """AES-256-GCM with the field name bound as additional authenticated data.

    Binding ``context`` prevents a ciphertext from being moved between columns
    or between users and still decrypting.
    """
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_data_key()).encrypt(
        nonce, plaintext.encode("utf-8"), context.encode("utf-8")
    )
    return nonce + _AAD_SEPARATOR + ciphertext


def decrypt_field(blob: bytes, *, context: str) -> str:
    nonce, _, ciphertext = blob.partition(_AAD_SEPARATOR)
    plaintext = AESGCM(_data_key()).decrypt(nonce, ciphertext, context.encode("utf-8"))
    return plaintext.decode("utf-8")

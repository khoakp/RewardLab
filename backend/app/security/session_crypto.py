"""Authenticated encryption for session state and secrets (Milestone M3.2).

Uses AES-256-GCM authenticated encryption from the verified `cryptography` library.
Binds encryption to provider ID, account ID, and key version as Additional Authenticated Data (AAD)
to cryptographically prevent cross-account ciphertext transplantation.
"""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SessionCryptoError(Exception):
    """Base exception for session cryptographic operations."""


class SessionKeyConfigurationError(SessionCryptoError):
    """Raised when encryption key is missing, malformed, or misconfigured."""


class SessionDecryptionError(SessionCryptoError):
    """Raised when decryption fails due to invalid key or corrupted data."""


class SessionTamperedError(SessionDecryptionError):
    """Raised when ciphertext authentication tag fails or account binding mismatches."""


def generate_session_encryption_key() -> str:
    """Generates a secure 256-bit (32-byte) key as urlsafe base64 string."""
    raw_key = AESGCM.generate_key(bit_length=256)
    return base64.urlsafe_b64encode(raw_key).decode("utf-8")


def parse_encryption_key(key: str | bytes) -> bytes:
    """Parses and validates a 32-byte encryption key from base64, hex, or raw bytes."""
    if isinstance(key, bytes):
        if len(key) == 32:
            return key
        # Try decoding bytes as utf-8 string if it might be base64 or hex
        try:
            key_str = key.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise SessionKeyConfigurationError("Encryption key must be exactly 32 bytes.") from None
    else:
        key_str = key.strip()

    # Try urlsafe base64 decoding (44 chars for 32 bytes)
    try:
        decoded = base64.urlsafe_b64decode(key_str.encode("utf-8"))
        if len(decoded) == 32:
            return decoded
    except (binascii.Error, ValueError):
        pass

    # Try hex decoding (64 chars for 32 bytes)
    try:
        if len(key_str) == 64:
            decoded = bytes.fromhex(key_str)
            if len(decoded) == 32:
                return decoded
    except (binascii.Error, ValueError):
        pass

    raise SessionKeyConfigurationError(
        "Encryption key must be a 32-byte key (raw, 44-char base64, or 64-char hex)."
    )


class SessionCrypto:
    """AES-256-GCM authenticated encryption service with account-bound AAD."""

    NONCE_SIZE = 12  # Standard 96-bit nonce for AES-GCM
    TAG_SIZE = 16  # Standard 128-bit authentication tag

    def __init__(self, key: str | bytes | None = None) -> None:
        if key is None:
            env_key = os.environ.get("ADBAP_SESSION_ENCRYPTION_KEY")
            if not env_key:
                raise SessionKeyConfigurationError(
                    "ADBAP_SESSION_ENCRYPTION_KEY environment variable is not set."
                )
            key = env_key

        self._raw_key = parse_encryption_key(key)
        self._aesgcm = AESGCM(self._raw_key)

    def __repr__(self) -> str:
        return f"<SessionCrypto alg='AES-256-GCM' key='[REDACTED]' nonce={self.NONCE_SIZE}>"

    @staticmethod
    def _build_aad(provider_id: str, account_id: str, key_version: int) -> bytes:
        """Constructs Additional Authenticated Data binding ciphertext to context."""
        return f"{provider_id}:{account_id}:{key_version}".encode()

    def encrypt(
        self,
        plaintext: str | bytes,
        provider_id: str,
        account_id: str,
        key_version: int = 1,
    ) -> str:
        """Encrypts plaintext using AES-256-GCM with account-bound AAD.

        Returns:
            Urlsafe base64 string: nonce (12 bytes) + ciphertext + auth tag (16 bytes).
        """
        payload_bytes = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext

        nonce = os.urandom(self.NONCE_SIZE)
        aad = self._build_aad(provider_id, account_id, key_version)
        ciphertext = self._aesgcm.encrypt(nonce, payload_bytes, aad)
        combined = nonce + ciphertext
        return base64.urlsafe_b64encode(combined).decode("utf-8")

    def decrypt(
        self,
        ciphertext_b64: str,
        provider_id: str,
        account_id: str,
        key_version: int = 1,
    ) -> str:
        """Decrypts and authenticates ciphertext using AES-256-GCM.

        Verifies that:
        1. Encryption key matches.
        2. Ciphertext and authentication tag have not been altered.
        3. Provider ID, Account ID, and Key Version match the original encryption context.

        Raises:
            SessionTamperedError: If decryption, tag validation, or AAD validation fails.
        """
        try:
            combined = base64.urlsafe_b64decode(ciphertext_b64.encode("utf-8"))
        except (binascii.Error, ValueError) as err:
            raise SessionTamperedError(
                "Invalid session ciphertext: base64 decode failed."
            ) from err

        min_len = self.NONCE_SIZE + self.TAG_SIZE
        if len(combined) < min_len:
            raise SessionTamperedError(
                f"Invalid session ciphertext: payload shorter than minimum ({min_len} bytes)."
            )

        nonce = combined[: self.NONCE_SIZE]
        ciphertext = combined[self.NONCE_SIZE :]
        aad = self._build_aad(provider_id, account_id, key_version)

        try:
            decrypted_bytes = self._aesgcm.decrypt(nonce, ciphertext, aad)
            return decrypted_bytes.decode("utf-8")
        except InvalidTag as err:
            raise SessionTamperedError(
                "Session decryption failed: data corrupted, invalid key, or cross-account mismatch."
            ) from err
        except UnicodeDecodeError as err:
            raise SessionTamperedError("Decrypted session data is not valid UTF-8.") from err

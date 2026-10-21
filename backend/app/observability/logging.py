import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode

SENSITIVE_KEYS = {
    "password",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "cookie",
    "authorization",
    "auth_state",
    "session_key",
    "session_id",
    "session",
    "api_key",
    "credentials",
}

SENSITIVE_KEY_PATTERN = (
    r"(?:password|access_token|refresh_token|api_key|session_key|session_id|"
    r"auth_state|authorization|credentials|secret|cookie|session|token)"
)

CREDENTIALS_URL_REGEX = re.compile(r"://([^:]+):([^@]+)@")
BEARER_TOKEN_REGEX = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/-]+=*)")
JSON_ASSIGNMENT_REGEX = re.compile(
    rf'(?i)(["\']{SENSITIVE_KEY_PATTERN}["\']\s*:\s*)(["\'])(?!(?:\[REDACTED\]|Bearer\b))(.*?)(["\'])'
)
ASSIGNMENT_REGEX = re.compile(
    rf'(?i)(["\']?{SENSITIVE_KEY_PATTERN}["\']?\s*[:=]\s*)(["\']?)(?!(?:\[REDACTED\]|Bearer\b))([^"\'\s,;}}&]+)(\2)'
)
TRAILING_KEY_REGEX = re.compile(
    rf'(?i)(["\']?{SENSITIVE_KEY_PATTERN}["\']?\s*[:=]\s*)(["\']?)$'
)


def redact_url_credentials(url: str) -> str:
    """Mask embedded userinfo and sensitive query params in URLs."""
    if not url or not isinstance(url, str):
        return url

    sanitized = url
    if "?" in sanitized:
        base_url, query_str = sanitized.split("?", 1)
        try:
            query_pairs = parse_qsl(query_str, keep_blank_values=True)
            redacted_pairs = [
                (k, "[REDACTED]" if k.lower() in SENSITIVE_KEYS else v) for k, v in query_pairs
            ]
            sanitized = f"{base_url}?{urlencode(redacted_pairs)}"
            sanitized = sanitized.replace("%5BREDACTED%5D", "[REDACTED]")
        except Exception:
            pass

    sanitized = CREDENTIALS_URL_REGEX.sub(r"://[REDACTED]:[REDACTED]@", sanitized)
    return sanitized


def redact_free_text(text: str, max_length: int = 65536) -> str:
    """Sanitize free-text strings by masking credentials, tokens, and sensitive key-values.

    Protects:
    - URL userinfo and sensitive query parameters
    - Bearer authorization headers/tokens
    - Key=value, key: value, and JSON-encoded credential pairs
    - Fail-closed truncation at max_length with [TRUNCATED] marker (never appends raw suffix)
    """
    if not text or not isinstance(text, str):
        return text

    is_truncated = len(text) > max_length
    target = text[:max_length]

    # 1. Redact embedded URL credentials and query parameters
    sanitized = redact_url_credentials(target)

    # 2. Redact Bearer authorization tokens
    sanitized = BEARER_TOKEN_REGEX.sub(r"\1[REDACTED]", sanitized)

    # 3. Redact JSON-style string assignments: "password": "value"
    sanitized = JSON_ASSIGNMENT_REGEX.sub(r"\1\2[REDACTED]\4", sanitized)

    # 4. Redact key=val or key: val assignments
    sanitized = ASSIGNMENT_REGEX.sub(r"\1\2[REDACTED]\4", sanitized)

    # 5. Redact trailing sensitive key severed at truncation boundary
    sanitized = TRAILING_KEY_REGEX.sub(r"\1[REDACTED]\2", sanitized)

    if is_truncated:
        sanitized = f"{sanitized} [TRUNCATED]"

    return sanitized


def redact_data(data: Any) -> Any:
    """Recursively redact sensitive keys in dictionaries or collections, and free-text strings."""
    if isinstance(data, dict):
        cleaned: dict[str, Any] = {}
        for k, v in data.items():
            if str(k).lower() in SENSITIVE_KEYS:
                cleaned[k] = "[REDACTED]"
            elif isinstance(v, (dict, list, tuple, set)):
                cleaned[k] = redact_data(v)
            elif isinstance(v, str):
                cleaned[k] = redact_free_text(v)
            else:
                cleaned[k] = v
        return cleaned
    if isinstance(data, list):
        return [redact_data(item) for item in data]
    if isinstance(data, tuple):
        return tuple(redact_data(item) for item in data)
    if isinstance(data, set):
        return {redact_data(item) for item in data}
    if isinstance(data, str):
        return redact_free_text(data)
    return data


class RedactionFilter(logging.Filter):
    """Logging filter ensuring no plain secrets appear in formatted log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_free_text(record.msg)
        if isinstance(record.args, dict):
            record.args = redact_data(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(redact_data(a) for a in record.args)
        return True


def get_logger(name: str = "adbap") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
        handler.setFormatter(formatter)
        handler.addFilter(RedactionFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger

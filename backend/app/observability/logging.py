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

CREDENTIALS_URL_REGEX = re.compile(r"://([^:]+):([^@]+)@")


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
        except Exception:
            pass

    sanitized = CREDENTIALS_URL_REGEX.sub(r"://[REDACTED]:[REDACTED]@", sanitized)
    return sanitized


def redact_data(data: Any) -> Any:
    """Recursively redact sensitive keys in dictionaries or collections."""
    if isinstance(data, dict):
        cleaned: dict[str, Any] = {}
        for k, v in data.items():
            if str(k).lower() in SENSITIVE_KEYS:
                cleaned[k] = "[REDACTED]"
            elif isinstance(v, (dict, list)):
                cleaned[k] = redact_data(v)
            elif isinstance(v, str):
                cleaned[k] = redact_url_credentials(v)
            else:
                cleaned[k] = v
        return cleaned
    if isinstance(data, list):
        return [redact_data(item) for item in data]
    if isinstance(data, str):
        return redact_url_credentials(data)
    return data


class RedactionFilter(logging.Filter):
    """Logging filter ensuring no plain secrets appear in formatted log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_url_credentials(record.msg)
            for key in SENSITIVE_KEYS:
                if key in record.msg.lower():
                    # Pattern for key=val or key: val
                    record.msg = re.sub(
                        rf"(?i)({key}\s*[:=]\s*['\"]?)([^'\";\s,]+)",
                        r"\1[REDACTED]",
                        record.msg,
                    )
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

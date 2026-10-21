import logging

from app.observability.logging import RedactionFilter, redact_data, redact_url_credentials


def test_redact_url_credentials() -> None:
    raw_url = "http://myuser:supersecret123@127.0.0.1:8001/api?token=abc12345&other=keep"
    redacted = redact_url_credentials(raw_url)
    assert "supersecret123" not in redacted
    assert "abc12345" not in redacted
    assert "[REDACTED]" in redacted
    assert "keep" in redacted


def test_redact_data_dictionary() -> None:
    data = {
        "account_id": "acc_01",
        "password": "plaintext_password",
        "nested": {
            "token": "secret_bearer_token",
            "safe_field": "visible_info",
        },
        "url": "http://admin:topsecret@localhost:8001/",
    }
    redacted = redact_data(data)
    assert redacted["account_id"] == "acc_01"
    assert redacted["password"] == "[REDACTED]"
    assert redacted["nested"]["token"] == "[REDACTED]"
    assert redacted["nested"]["safe_field"] == "visible_info"
    assert "topsecret" not in redacted["url"]


def test_redaction_filter_masks_log_record() -> None:
    filter_ = RedactionFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="User session token=abcdef98765 initialized",
        args=(),
        exc_info=None,
    )
    filter_.filter(record)
    assert "abcdef98765" not in record.msg
    assert "[REDACTED]" in record.msg

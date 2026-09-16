import pytest

from hcs_ai.local_codex.credentials import CredentialProvider


def test_reads_app_password_from_environment(monkeypatch):
    monkeypatch.setenv("LOCAL_CODEX_GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    provider = CredentialProvider()
    assert provider.has_email_app_password() is True
    assert provider.get_email_app_password() == "abcdefghijklmnop"


def test_missing_password_raises(monkeypatch):
    monkeypatch.delenv("LOCAL_CODEX_GMAIL_APP_PASSWORD", raising=False)
    provider = CredentialProvider()
    with pytest.raises(RuntimeError, match="email app password is not configured"):
        provider.get_email_app_password()

from __future__ import annotations

import os


class CredentialProvider:
    ENV_NAME = "LOCAL_CODEX_GMAIL_APP_PASSWORD"

    def _from_environment(self) -> str | None:
        value = os.getenv(self.ENV_NAME)
        if not value:
            return None
        normalized = "".join(value.split())
        return normalized or None

    def has_email_app_password(self) -> bool:
        return self._from_environment() is not None

    def get_email_app_password(self) -> str:
        value = self._from_environment()
        if value is None:
            raise RuntimeError("email app password is not configured")
        return value

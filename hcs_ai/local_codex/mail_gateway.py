from __future__ import annotations

from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

from .models import MailMessage, TaskControl


TASK_SUBJECT_PREFIX = "TASK:"

def _visible_reply_text(body: str) -> str:
    lines = body.splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^On .+ wrote:$", stripped, flags=re.IGNORECASE):
            break
        if stripped.startswith(">"):
            break
        kept.append(line)
    return "\n".join(kept).strip()



def _plain_text_body(message) -> str:
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = (part.get("Content-Disposition") or "").lower()
            if content_type == "text/plain" and "attachment" not in disposition:
                content = part.get_content()
                return content if isinstance(content, str) else str(content)
        return ""
    if message.get_content_type() != "text/plain":
        return ""
    content = message.get_content()
    return content if isinstance(content, str) else str(content)


def parse_mail_message(raw_bytes: bytes) -> MailMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    sender = parseaddr(message.get("From", ""))[1].strip().lower()
    subject = str(message.get("Subject", "")).strip()
    message_id = str(message.get("Message-ID", "")).strip()
    in_reply_to = str(message.get("In-Reply-To", "")).strip() or None
    references_header = str(message.get("References", "")).strip()
    references = references_header.split() if references_header else []
    sent_at: datetime | None = None
    date_header = message.get("Date")
    if date_header:
        try:
            sent_at = parsedate_to_datetime(str(date_header))
        except (TypeError, ValueError, OverflowError):
            sent_at = None
    return MailMessage(
        sender=sender,
        subject=subject,
        body=_plain_text_body(message).strip(),
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        sent_at=sent_at,
    )


def is_trusted_sender(message: MailMessage, trusted_sender: str) -> bool:
    return message.sender.lower() == trusted_sender.strip().lower()


def is_new_task(message: MailMessage, prefix: str = TASK_SUBJECT_PREFIX) -> bool:
    return message.subject.lstrip().lower().startswith(prefix.lower())


def parse_control(message: MailMessage) -> TaskControl | None:
    value = _visible_reply_text(message.body).strip().casefold()
    mapping = {control.value: control for control in TaskControl}
    return mapping.get(value)


def extract_freeform_instruction(message: MailMessage) -> str | None:
    if parse_control(message) is not None:
        return None
    value = _visible_reply_text(message.body).strip()
    return value or None

import imaplib
import re
import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Callable


class MailGateway:
    def __init__(
        self,
        *,
        account: str,
        app_password_provider: Callable[[], str],
        seen_path: Path,
        imap_host: str = "imap.gmail.com",
        imap_port: int = 993,
        smtp_host: str = "smtp.gmail.com",
        smtp_port: int = 465,
        imap_factory=None,
        smtp_factory=None,
    ):
        self.account = account
        self.app_password_provider = app_password_provider
        self.seen_path = seen_path
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.imap_factory = imap_factory or imaplib.IMAP4_SSL
        self.smtp_factory = smtp_factory or smtplib.SMTP_SSL
        self._seen = self._load_seen()

    def _load_seen(self) -> set[str]:
        if not self.seen_path.exists():
            return set()
        data = json.loads(self.seen_path.read_text(encoding="utf-8"))
        return set(data.get("message_ids", []))

    def _save_seen(self) -> None:
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.seen_path.with_suffix(self.seen_path.suffix + ".tmp")
        tmp.write_text(json.dumps({"message_ids": sorted(self._seen)}, indent=2), encoding="utf-8")
        tmp.replace(self.seen_path)

    def is_processed(self, message_id: str) -> bool:
        return bool(message_id) and message_id in self._seen

    def mark_processed(self, message_id: str) -> None:
        if not message_id:
            return
        self._seen.add(message_id)
        self._save_seen()

    def poll(self) -> list[MailMessage]:
        password = self.app_password_provider()
        imap = self.imap_factory(self.imap_host, self.imap_port)
        try:
            status, _ = imap.login(self.account, password)
            if status != "OK":
                raise RuntimeError("IMAP login failed")
            status, _ = imap.select("INBOX")
            if status != "OK":
                raise RuntimeError("IMAP inbox selection failed")
            status, data = imap.search(None, "ALL")
            if status != "OK":
                raise RuntimeError("IMAP search failed")
            ids = data[0].split() if data and data[0] else []
            result: list[MailMessage] = []
            seen_this_poll: set[str] = set()
            for message_id in ids:
                status, payload = imap.fetch(message_id, "(RFC822)")
                if status != "OK" or not payload:
                    continue
                raw = next((item[1] for item in payload if isinstance(item, tuple) and len(item) > 1), None)
                if raw is None:
                    continue
                parsed = parse_mail_message(raw)
                if parsed.message_id and (parsed.message_id in self._seen or parsed.message_id in seen_this_poll):
                    continue
                if parsed.message_id:
                    seen_this_poll.add(parsed.message_id)
                result.append(parsed)
            return result
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        in_reply_to: str | None = None,
        references: list[str] | None = None,
    ) -> str:
        message = EmailMessage()
        message["From"] = self.account
        message["To"] = to
        message["Subject"] = subject
        message_id = make_msgid(domain="gmail.com")
        message["Message-ID"] = message_id
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = " ".join(references)
        message.set_content(body)

        password = self.app_password_provider()
        context = ssl.create_default_context()
        with self.smtp_factory(self.smtp_host, self.smtp_port, context=context) as smtp:
            smtp.login(self.account, password)
            smtp.send_message(message)
        return message_id

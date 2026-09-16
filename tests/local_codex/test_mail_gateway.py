from email.message import EmailMessage

from hcs_ai.local_codex.mail_gateway import (
    extract_freeform_instruction,
    is_new_task,
    is_trusted_sender,
    parse_control,
    parse_mail_message,
)
from hcs_ai.local_codex.models import TaskControl


def make_raw(sender, subject, body, message_id="<m1@example>", in_reply_to=None, references=None):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "localcodex.alexandria@gmail.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    msg.set_content(body)
    return msg.as_bytes()


def test_accepts_only_exact_trusted_sender():
    message = parse_mail_message(make_raw("Matthew <schoolfieldmatt@gmail.com>", "TASK: Test", "Do thing"))
    assert is_trusted_sender(message, "schoolfieldmatt@gmail.com") is True
    attacker = parse_mail_message(make_raw("schoolfieldmatt@gmail.com.evil@example.com", "TASK: Test", "Do thing"))
    assert is_trusted_sender(attacker, "schoolfieldmatt@gmail.com") is False


def test_new_task_requires_prefix():
    assert is_new_task(parse_mail_message(make_raw("schoolfieldmatt@gmail.com", "TASK: Maze World", "Fix"))) is True
    assert is_new_task(parse_mail_message(make_raw("schoolfieldmatt@gmail.com", "Maze World", "Fix"))) is False


def test_parses_controls_case_insensitively():
    for text, expected in [
        (" approve ", TaskControl.APPROVE),
        ("REJECT", TaskControl.REJECT),
        (" pause ", TaskControl.PAUSE),
        ("Resume", TaskControl.RESUME),
        ("cancel", TaskControl.CANCEL),
    ]:
        msg = parse_mail_message(make_raw("schoolfieldmatt@gmail.com", "Re: task", text))
        assert parse_control(msg) is expected


def test_freeform_instruction_is_not_mistaken_for_control():
    msg = parse_mail_message(make_raw("schoolfieldmatt@gmail.com", "Re: task", "Do not modify tests."))
    assert parse_control(msg) is None
    assert extract_freeform_instruction(msg) == "Do not modify tests."


def test_thread_headers_are_preserved():
    msg = parse_mail_message(make_raw(
        "schoolfieldmatt@gmail.com",
        "Re: TASK: Test",
        "APPROVE",
        message_id="<m2@example>",
        in_reply_to="<m1@example>",
        references=["<root@example>", "<m1@example>"],
    ))
    assert msg.message_id == "<m2@example>"
    assert msg.in_reply_to == "<m1@example>"
    assert msg.references == ["<root@example>", "<m1@example>"]

from pathlib import Path

from hcs_ai.local_codex.mail_gateway import MailGateway


class FakeIMAP:
    def __init__(self, messages=None):
        self.messages = messages or []
        self.logged_in = None

    def login(self, account, password):
        self.logged_in = (account, password)
        return "OK", []

    def select(self, mailbox):
        return "OK", []

    def search(self, charset, criterion):
        ids = b" ".join(str(i + 1).encode() for i in range(len(self.messages)))
        return "OK", [ids]

    def fetch(self, message_id, query):
        index = int(message_id) - 1
        return "OK", [(b"RFC822", self.messages[index])]

    def logout(self):
        return "BYE", []


class FakeSMTP:
    def __init__(self):
        self.logged_in = None
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, account, password):
        self.logged_in = (account, password)

    def send_message(self, msg):
        self.sent.append(msg)


def test_poll_deduplicates_message_ids(tmp_path: Path):
    raw = make_raw("schoolfieldmatt@gmail.com", "TASK: Test", "Do thing", message_id="<same@example>")
    fake_imap = FakeIMAP([raw, raw])
    gateway = MailGateway(
        account="localcodex.alexandria@gmail.com",
        app_password_provider=lambda: "abcdefghijklmnop",
        seen_path=tmp_path / "mail_seen.json",
        imap_factory=lambda host, port: fake_imap,
        smtp_factory=lambda host, port, context=None: FakeSMTP(),
    )
    first = gateway.poll()
    assert len(first) == 1
    gateway.mark_processed(first[0].message_id)
    assert gateway.poll() == []


def test_send_sets_reply_headers(tmp_path: Path):
    fake_smtp = FakeSMTP()
    gateway = MailGateway(
        account="localcodex.alexandria@gmail.com",
        app_password_provider=lambda: "abcdefghijklmnop",
        seen_path=tmp_path / "mail_seen.json",
        imap_factory=lambda host, port: FakeIMAP(),
        smtp_factory=lambda host, port, context=None: fake_smtp,
    )
    message_id = gateway.send(
        "schoolfieldmatt@gmail.com",
        "Re: TASK: Test",
        "Status",
        in_reply_to="<m1@example>",
        references=["<root@example>", "<m1@example>"],
    )
    sent = fake_smtp.sent[-1]
    assert sent["In-Reply-To"] == "<m1@example>"
    assert "<root@example>" in sent["References"]
    assert message_id == sent["Message-ID"]


def test_control_ignores_gmail_quoted_history():
    body = "APPROVE\n\nOn Sun, Sep 7, 2026 at 4:00 PM Alexandria <localcodex.alexandria@gmail.com> wrote:\n> confirmation"
    msg = parse_mail_message(make_raw("schoolfieldmatt@gmail.com", "Re: task", body))
    assert parse_control(msg) is TaskControl.APPROVE


def test_freeform_instruction_excludes_quoted_history():
    body = "Do not modify tests.\nKeep the change small.\n\nOn Sun, Sep 7, 2026 at 4:00 PM Alexandria wrote:\n> old message"
    msg = parse_mail_message(make_raw("schoolfieldmatt@gmail.com", "Re: task", body))
    assert extract_freeform_instruction(msg) == "Do not modify tests.\nKeep the change small."

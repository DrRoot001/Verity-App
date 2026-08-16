"""Transactional email (PRD §38 provider abstraction, §10.10 notifications).

Two providers behind one protocol. ``ConsoleEmailProvider`` is the configured
provider for local development: it writes each message to disk and logs the
path, so verification and reset flows are genuinely completable offline rather
than blocked on an SMTP account. ``SmtpEmailProvider`` is the deployed path.

Message bodies live in ``TEMPLATES`` rather than inline at call sites so the
admin content surface (PRD §18.1) has one place to manage them later.
"""

from __future__ import annotations

import pathlib
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from verity.platform.config import settings
from verity.platform.logging import get_logger

log = get_logger("platform.email")


@dataclass(frozen=True, slots=True)
class Email:
    to: str
    subject: str
    text_body: str


@runtime_checkable
class EmailProvider(Protocol):
    name: str

    async def send(self, message: Email) -> None: ...


class ConsoleEmailProvider:
    """Writes messages to ``.data/outbox`` and logs the path."""

    name = "console"

    def __init__(self, outbox: pathlib.Path | None = None) -> None:
        self._outbox = outbox or pathlib.Path(".data/outbox")

    async def send(self, message: Email) -> None:
        self._outbox.mkdir(parents=True, exist_ok=True)
        # Recipient is part of the filename for easy local lookup, sanitized so
        # an address can never escape the outbox directory.
        safe_recipient = "".join(c if c.isalnum() or c in "._-@" else "_" for c in message.to)
        path = self._outbox / f"{safe_recipient}-{abs(hash(message.subject)) % 10**8}.txt"
        path.write_text(
            f"To: {message.to}\nSubject: {message.subject}\n\n{message.text_body}\n",
            encoding="utf-8",
        )
        log.info("email_written", provider=self.name, path=str(path))


class SmtpEmailProvider:
    name = "smtp"

    def __init__(self, smtp_url: str) -> None:
        parsed = urlparse(smtp_url)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 587
        self._username = parsed.username
        self._password = parsed.password

    async def send(self, message: Email) -> None:
        import asyncio

        await asyncio.to_thread(self._send_blocking, message)

    def _send_blocking(self, message: Email) -> None:
        payload = EmailMessage()
        payload["From"] = settings.email_from
        payload["To"] = message.to
        payload["Subject"] = message.subject
        payload.set_content(message.text_body)

        with smtplib.SMTP(self._host, self._port, timeout=15) as client:
            client.starttls(context=ssl.create_default_context())
            if self._username and self._password:
                client.login(self._username, self._password)
            client.send_message(payload)
        log.info("email_sent", provider=self.name)


TEMPLATES: dict[str, tuple[str, str]] = {
    "verify_email": (
        "Confirm your Verity address",
        "Welcome to Verity.\n\nConfirm your email address to start building your "
        "interview workspace:\n\n{link}\n\nThis link expires in 24 hours. "
        "If you didn't create an account, you can ignore this message.",
    ),
    "password_reset": (
        "Reset your Verity password",
        "We received a request to reset your password.\n\n{link}\n\n"
        "This link expires in 30 minutes and can be used once. If you didn't "
        "request it, your password is unchanged and no action is needed.",
    ),
    "password_changed": (
        "Your Verity password was changed",
        "Your password was just changed and all sessions were signed out.\n\n"
        "If this wasn't you, reset your password immediately: {link}",
    ),
}


def render(template_key: str, *, to: str, **variables: str) -> Email:
    subject, body = TEMPLATES[template_key]
    return Email(to=to, subject=subject, text_body=body.format(**variables))


def build_email_provider() -> EmailProvider:
    if settings.email_provider == "smtp":
        from verity.platform.config import get_settings

        smtp_url = getattr(get_settings(), "smtp_url", "")
        if smtp_url:
            return SmtpEmailProvider(smtp_url)
        log.warning("smtp_url_missing_falling_back_to_console")
    return ConsoleEmailProvider()


provider: EmailProvider = build_email_provider()

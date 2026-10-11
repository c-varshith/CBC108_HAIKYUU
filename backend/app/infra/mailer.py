"""Outgoing mail. `console` logs the message (dev/demo); `smtp` sends it."""
from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import Protocol

from app.config import Settings

log = logging.getLogger("calibrex.mail")


class Mailer(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleMailer:
    async def send(self, to: str, subject: str, body: str) -> None:
        log.info("mail (console backend, not sent)", extra={"to": to, "subject": subject, "body": body})


class SmtpMailer:
    def __init__(self, s: Settings):
        self.s = s

    async def send(self, to: str, subject: str, body: str) -> None:
        import aiosmtplib
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.s.mail_from, to, subject
        msg.set_content(body)
        await aiosmtplib.send(msg, hostname=self.s.smtp_host, port=self.s.smtp_port, username=self.s.smtp_user,
                              password=self.s.smtp_password, start_tls=self.s.smtp_starttls, timeout=20)


def make_mailer(s: Settings) -> Mailer:
    return SmtpMailer(s) if s.mail_backend == "smtp" else ConsoleMailer()

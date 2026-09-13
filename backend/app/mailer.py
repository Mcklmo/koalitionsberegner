"""Sending the usage reports by email, with nothing but the standard library.

Plain SMTP with a login — Gmail with an app password, or any provider's
submission port. Port 465 speaks TLS from the first byte; any other port is
upgraded with STARTTLS before the password is sent, and a server that cannot
upgrade is a failure rather than a plaintext login.

Like :mod:`app.wishlist`, nothing the server says reaches a caller: the reason
goes to the log, and the caller learns only that the report was not sent.
"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from collections.abc import Sequence
from email.message import EmailMessage
from typing import Protocol

from .observability import io_span

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20.0

#: The port that is TLS from the start; every other one uses STARTTLS.
IMPLICIT_TLS_PORT = 465


class MailUnavailable(Exception):
    """The report could not be sent. Never carries the mail server's own words."""


class Mailer(Protocol):
    enabled: bool

    def send(self, subject: str, body: str) -> None: ...


class DisabledMailer:
    """No SMTP configured: reports can still be read at ``/api/admin/usage``."""

    enabled = False

    def send(self, subject: str, body: str) -> None:
        raise MailUnavailable("usage reports are not configured")


class SmtpMailer:
    enabled = True

    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str,
        password: str,
        sender: str,
        recipients: Sequence[str],
        smtp=smtplib.SMTP,
        smtp_ssl=smtplib.SMTP_SSL,
    ):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender
        self._recipients = tuple(recipients)
        self._smtp = smtp
        self._smtp_ssl = smtp_ssl

    def message(self, subject: str, body: str) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self._sender
        message["To"] = ", ".join(self._recipients)
        message.set_content(body)
        # The report is a table laid out with spaces. Mail clients such as Gmail
        # show plain text in a proportional font, which squeezes the indentation
        # and breaks the columns apart; the same text in <pre> keeps them.
        message.add_alternative(
            f'<pre style="font-family: ui-monospace, Menlo, Consolas, monospace; '
            f'font-size: 13px; line-height: 1.4">{html.escape(body)}</pre>',
            subtype="html",
        )
        return message

    def send(self, subject: str, body: str) -> None:
        message = self.message(subject, body)
        context = ssl.create_default_context()
        try:
            with io_span(log, "smtp", "send", host=self._host, recipients=len(self._recipients)):
                if self._port == IMPLICIT_TLS_PORT:
                    server = self._smtp_ssl(
                        self._host, self._port, timeout=TIMEOUT_SECONDS, context=context
                    )
                else:
                    server = self._smtp(self._host, self._port, timeout=TIMEOUT_SECONDS)
                with server:
                    if self._port != IMPLICIT_TLS_PORT:
                        server.starttls(context=context)
                    server.login(self._username, self._password)
                    server.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("a usage report could not be sent: %s", type(exc).__name__)
            raise MailUnavailable("could not send the report") from None

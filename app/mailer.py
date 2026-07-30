"""Sending mail.

One narrow job: hand a coach the link to their own commission statement. The
transport is plain SMTP, because AWAKEN's mail already lives on Google
Workspace — Google has published SPF and DKIM for the domain, so a message sent
through their SMTP is authenticated the same as one typed in Gmail, and a reply
lands in the inbox the gym already reads. That is worth more here than the
dashboards a dedicated sending service would add, at a volume of roughly seven
messages a month.

Nothing in here knows about commissions. It takes a recipient, a subject and a
body, and reports back whether the server accepted it.
"""

from __future__ import annotations

import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

# A deliberately forgiving check. Its job is to catch a typo or an empty cell
# before we hand the address to a server, not to adjudicate RFC 5322.
_ADDR = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def looks_like_email(value: str | None) -> bool:
    return bool(value and _ADDR.match(value.strip()))


@dataclass(frozen=True)
class MailConfig:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    from_addr: str = ""
    from_name: str = "AWAKEN Fitness Center"
    reply_to: str = ""
    use_tls: bool = True          # STARTTLS on 587; set false only for port 465
    timeout: int = 20

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.from_addr)

    @property
    def missing(self) -> list:
        """Which settings are absent — so the screen can say what to fix."""
        pairs = [("SMTP_HOST", self.host), ("SMTP_USER", self.user),
                 ("SMTP_PASSWORD", self.password), ("MAIL_FROM", self.from_addr)]
        return [k for k, v in pairs if not v]


def config_from_env() -> MailConfig:
    """Read the mail settings. Defaults suit Google Workspace, so in practice
    only SMTP_USER, SMTP_PASSWORD and MAIL_FROM need setting."""
    env = os.environ.get
    try:
        port = int(env("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    user = (env("SMTP_USER", "") or "").strip()
    return MailConfig(
        host=(env("SMTP_HOST", "smtp.gmail.com") or "").strip(),
        port=port,
        user=user,
        password=env("SMTP_PASSWORD", "") or "",
        # Google rewrites the From to the authenticated account anyway unless
        # the address is a verified alias, so defaulting to the login is the
        # honest choice.
        from_addr=(env("MAIL_FROM", "") or user).strip(),
        from_name=(env("MAIL_FROM_NAME", "AWAKEN Fitness Center") or "").strip(),
        reply_to=(env("MAIL_REPLY_TO", "") or "").strip(),
        use_tls=(env("SMTP_TLS", "1") or "1").strip().lower() not in ("0", "false", "no"),
    )


def build_message(cfg: MailConfig, to: str, subject: str,
                  text: str, html: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.from_name, cfg.from_addr)) if cfg.from_name else cfg.from_addr
    msg["To"] = to
    msg["Subject"] = subject
    if cfg.reply_to:
        msg["Reply-To"] = cfg.reply_to
    # A stable domain in the Message-ID keeps threading sane across sends.
    msg["Message-ID"] = make_msgid(domain=cfg.from_addr.split("@")[-1] or None)
    # Statements are personal, not marketing — asking robots not to auto-reply
    # is the polite half of that.
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


class Mailer:
    """Sends messages. `transport` exists so tests can watch what would go out
    without a server; production leaves it None and real SMTP is used."""

    def __init__(self, cfg: MailConfig | None = None, transport=None):
        self.cfg = cfg or config_from_env()
        self.transport = transport

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> tuple:
        """Returns (ok, detail). `detail` is empty on success and a short,
        human-readable reason otherwise — it ends up on screen, so it should
        read like a sentence rather than a stack trace."""
        cfg = self.cfg
        if not cfg.configured:
            return False, "Mail is not set up yet (missing %s)" % ", ".join(cfg.missing)
        if not looks_like_email(to):
            return False, "%s is not a valid email address" % (to or "(blank)")
        msg = build_message(cfg, to.strip(), subject, text, html)
        if self.transport is not None:
            return self.transport(msg)
        try:
            if cfg.port == 465 and not cfg.use_tls:
                server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout,
                                          context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
            with server:
                server.ehlo()
                if cfg.use_tls and cfg.port != 465:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                server.login(cfg.user, cfg.password)
                server.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            # Overwhelmingly the first thing that goes wrong: a Google account
            # password used where an app password is required.
            return False, ("The mail server rejected the login. Google needs an "
                           "app password here, not the account password.")
        except smtplib.SMTPRecipientsRefused:
            return False, "The mail server would not accept %s" % to
        except smtplib.SMTPException as exc:
            return False, "The mail server refused it: %s" % exc
        except OSError as exc:
            return False, "Could not reach the mail server: %s" % exc
        return True, ""

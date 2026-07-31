"""The mailer, without a mail server.

Everything here is about the decisions the module makes before it opens a
socket: what counts as an address, what it refuses to attempt, and what the
message looks like when it goes out.
"""

from app.mailer import MailConfig, Mailer, build_message, looks_like_email


CFG = MailConfig(host="smtp.example.com", port=587, user="pay@awakengym.com",
                 password="app-password", from_addr="pay@awakengym.com",
                 from_name="AWAKEN Fitness Center")


def collector():
    """A transport that keeps what it was given instead of sending it."""
    seen = []

    def transport(msg):
        seen.append(msg)
        return True, ""
    return seen, transport


# ---------------------------------------------------------------- addresses

def test_plain_address_is_accepted():
    assert looks_like_email("coach@awakengym.com")


def test_blank_and_malformed_are_not():
    for bad in ("", None, "   ", "coach", "coach@", "@awakengym.com",
                "coach@localhost", "two@@at.com", "has space@a.com"):
        assert not looks_like_email(bad), bad


def test_surrounding_whitespace_is_tolerated():
    assert looks_like_email("  coach@awakengym.com  ")


# ------------------------------------------------------------ configuration

def test_config_is_incomplete_without_a_password():
    cfg = MailConfig(host="h", user="u", from_addr="f@x.com")
    assert not cfg.configured
    assert cfg.missing == ["SMTP_PASSWORD"]


def test_a_full_config_is_ready():
    assert CFG.configured
    assert CFG.missing == []


def test_unconfigured_mailer_refuses_and_says_what_is_missing():
    ok, why = Mailer(MailConfig(), transport=lambda m: (True, "")).send(
        "coach@awakengym.com", "s", "t")
    assert not ok
    assert "SMTP_HOST" in why and "SMTP_PASSWORD" in why


def test_an_app_password_pasted_with_spaces_still_works(monkeypatch):
    """Google shows it as "abcd efgh ijkl mnop"; copying it verbatim is the
    normal thing to do, and must not fail authentication."""
    from app.mailer import config_from_env
    monkeypatch.setenv("SMTP_USER", "admin@awakengym.com")
    monkeypatch.setenv("MAIL_FROM", "admin@awakengym.com")
    monkeypatch.setenv("SMTP_PASSWORD", "abcd efgh ijkl mnop")
    cfg = config_from_env()
    assert cfg.password == "abcdefghijklmnop"
    assert cfg.configured


def test_a_password_with_stray_newlines_is_cleaned(monkeypatch):
    from app.mailer import config_from_env
    monkeypatch.setenv("SMTP_USER", "admin@awakengym.com")
    monkeypatch.setenv("SMTP_PASSWORD", "  abcdefghijklmnop\n")
    assert config_from_env().password == "abcdefghijklmnop"


def test_a_blank_password_stays_blank(monkeypatch):
    """Whitespace-only must not read as configured — the screen has to keep
    saying the credential is missing."""
    from app.mailer import config_from_env
    monkeypatch.setenv("SMTP_USER", "admin@awakengym.com")
    monkeypatch.setenv("MAIL_FROM", "admin@awakengym.com")
    monkeypatch.setenv("SMTP_PASSWORD", "   ")
    cfg = config_from_env()
    assert cfg.password == ""
    assert not cfg.configured
    assert cfg.missing == ["SMTP_PASSWORD"]


def test_env_defaults_point_at_google():
    from app.mailer import config_from_env
    cfg = config_from_env()
    assert cfg.host == "smtp.gmail.com"
    assert cfg.port == 587
    assert cfg.use_tls


# ------------------------------------------------------------------ sending

def test_a_bad_address_is_never_attempted():
    seen, transport = collector()
    ok, why = Mailer(CFG, transport=transport).send("not-an-address", "s", "t")
    assert not ok
    assert "not a valid email" in why
    assert seen == []          # the transport was never reached


def test_a_good_send_reports_no_reason():
    seen, transport = collector()
    ok, why = Mailer(CFG, transport=transport).send(
        "coach@awakengym.com", "Your July commission", "body")
    assert ok and why == ""
    assert len(seen) == 1


def test_the_message_carries_sender_recipient_and_subject():
    seen, transport = collector()
    Mailer(CFG, transport=transport).send("coach@awakengym.com", "Subject here", "body")
    msg = seen[0]
    assert msg["To"] == "coach@awakengym.com"
    assert msg["Subject"] == "Subject here"
    assert "AWAKEN Fitness Center" in msg["From"]
    assert "pay@awakengym.com" in msg["From"]


def test_html_rides_alongside_the_plain_text():
    seen, transport = collector()
    Mailer(CFG, transport=transport).send(
        "coach@awakengym.com", "s", "plain body", "<p>rich body</p>")
    msg = seen[0]
    assert msg.is_multipart()
    kinds = {p.get_content_type() for p in msg.walk() if not p.is_multipart()}
    assert kinds == {"text/plain", "text/html"}
    assert "plain body" in msg.get_body(("plain",)).get_content()
    assert "rich body" in msg.get_body(("html",)).get_content()


def test_text_only_send_stays_a_single_part():
    seen, transport = collector()
    Mailer(CFG, transport=transport).send("coach@awakengym.com", "s", "just text")
    assert not seen[0].is_multipart()


def test_a_reply_to_is_set_when_configured():
    cfg = MailConfig(**{**CFG.__dict__, "reply_to": "admin@awakengym.com"})
    seen, transport = collector()
    Mailer(cfg, transport=transport).send("coach@awakengym.com", "s", "t")
    assert seen[0]["Reply-To"] == "admin@awakengym.com"


def test_no_reply_to_header_when_not_configured():
    seen, transport = collector()
    Mailer(CFG, transport=transport).send("coach@awakengym.com", "s", "t")
    assert seen[0]["Reply-To"] is None


def test_statements_are_marked_auto_generated():
    msg = build_message(CFG, "coach@awakengym.com", "s", "t")
    assert msg["Auto-Submitted"] == "auto-generated"


def test_every_message_gets_its_own_id():
    a = build_message(CFG, "coach@awakengym.com", "s", "t")
    b = build_message(CFG, "coach@awakengym.com", "s", "t")
    assert a["Message-ID"] and a["Message-ID"] != b["Message-ID"]
    assert "awakengym.com" in a["Message-ID"]


def test_a_transport_failure_is_passed_back_verbatim():
    ok, why = Mailer(CFG, transport=lambda m: (False, "mailbox full")).send(
        "coach@awakengym.com", "s", "t")
    assert not ok and why == "mailbox full"

"""IMAP and SMTP mailbox adapter for personal and custom-domain accounts.

This adapter also carries Eva's direct relay route, because relaying as
`eva@<owned-domain>` is an ordinary authenticated SMTP submission from the
user's existing account.

Transport rules:

- IMAP always uses implicit TLS. SMTP uses STARTTLS on the submission port or
  implicit TLS on 465. Plaintext submission requires an explicit
  `smtp_allow_plaintext` setting, which exists only so Eva can hand mail to an
  internal MTA on a trusted network; it is never the default and never applies
  to an authenticated account.
- Certificates are verified against the system trust store. There is no option
  to disable verification, because a mail password would be the thing exposed.
- Fetches are bounded by message count and per-message body size, so a large
  mailbox cannot exhaust memory or flood a prompt.
- Only headers and a bounded plain-text preview are returned to callers. Full
  bodies are fetched only for an explicitly identified message.

Credentials are passed in per call and are never stored on the instance,
logged, or included in an error message.

Two authentication mechanisms are supported. `password` is a classic or
app-specific password. `xoauth2` presents a short-lived OAuth access token and
is preferred wherever the provider offers it: nothing durable is stored, the
grant is scope-limited, and the user can revoke it without changing a password.
Google requires XOAUTH2 for OAuth mail access; app passwords are its legacy path.
"""

import email
import email.header
import email.message
import email.utils
import imaplib
import smtplib
import ssl
from email.message import EmailMessage

from bridge import email_policy

DEFAULT_TIMEOUT = 30
MAX_FETCH_MESSAGES = 50
MAX_PREVIEW_CHARS = 2000
MAX_BODY_CHARS = 200000
IMPLICIT_TLS_SMTP_PORT = 465
AUTH_MECHANISMS = ("password", "xoauth2")


def xoauth2_string(address, access_token):
    """Return the SASL XOAUTH2 initial response for a mailbox and access token."""
    if not address or not access_token:
        raise MailboxError("XOAUTH2 requires a mailbox address and an access token")
    return f"user={address}\x01auth=Bearer {access_token}\x01\x01"


class MailboxError(Exception):
    """Raised when a mailbox operation fails. Never contains credentials."""


def _tls_context():
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _decode_header(value):
    """Decode an RFC 2047 header into plain text without raising."""
    if not value:
        return ""
    try:
        parts = email.header.decode_header(str(value))
    except (ValueError, UnicodeDecodeError):
        return str(value)
    decoded = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                decoded.append(text.decode(charset or "utf-8", "replace"))
            except (LookupError, UnicodeDecodeError):
                decoded.append(text.decode("utf-8", "replace"))
        else:
            decoded.append(str(text))
    return " ".join("".join(decoded).split())


def _plain_text_body(message, limit):
    """Return a bounded plain-text body, preferring text/plain over HTML."""
    if not isinstance(message, email.message.Message):
        return ""
    candidates = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_disposition() == "attachment":
                continue
            candidates.append(part)
    else:
        candidates.append(message)

    chosen = next((p for p in candidates if p.get_content_type() == "text/plain"), None)
    chosen = chosen or next((p for p in candidates if p.get_content_type() == "text/html"), None)
    if chosen is None:
        return ""
    try:
        payload = chosen.get_payload(decode=True)
    except (AssertionError, ValueError):
        return ""
    if payload is None:
        return ""
    charset = chosen.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, "replace")
    except (LookupError, UnicodeDecodeError):
        text = payload.decode("utf-8", "replace")
    return text[:limit]


def summarize_message(raw_bytes, preview_chars=MAX_PREVIEW_CHARS):
    """Return bounded header fields plus a preview for one raw message."""
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise MailboxError("A message could not be parsed")
    try:
        message = email.message_from_bytes(bytes(raw_bytes))
    except (ValueError, TypeError, AttributeError):
        raise MailboxError("A message could not be parsed") from None
    return {
        "id": _decode_header(message.get("Message-ID"))[:200],
        "from": _decode_header(message.get("From"))[:320],
        "to": _decode_header(message.get("To"))[:640],
        "subject": _decode_header(message.get("Subject"))[:400],
        "received": _decode_header(message.get("Date"))[:80],
        "preview": " ".join(_plain_text_body(message, preview_chars).split())[:preview_chars],
    }


def build_message(normalized, from_address, reply_to=""):
    """Compose an RFC 5322 message from an already-normalized send request."""
    message = EmailMessage()
    message["From"] = from_address
    message["To"] = ", ".join(normalized.get("to") or [])
    if normalized.get("cc"):
        message["Cc"] = ", ".join(normalized["cc"])
    message["Subject"] = normalized.get("subject") or ""
    message["Date"] = email.utils.formatdate(localtime=True)
    message["Message-ID"] = email.utils.make_msgid()
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(normalized.get("body") or "")
    return message


class ImapSmtpMailbox:
    """One IMAP/SMTP account. Settings come from an account record."""

    def __init__(self, settings, address, timeout=DEFAULT_TIMEOUT):
        self.settings = settings if isinstance(settings, dict) else {}
        self.address = email_policy.normalize_address(address)
        if not self.address:
            raise MailboxError("Mailbox address is not a valid email address")
        self.timeout = timeout

    @property
    def auth_mechanism(self):
        mechanism = str(self.settings.get("auth_mechanism") or "password").lower()
        return mechanism if mechanism in AUTH_MECHANISMS else "password"

    def _imap_authenticate(self, connection, secret):
        if self.auth_mechanism == "xoauth2":
            response = xoauth2_string(self.address, secret)
            connection.authenticate("XOAUTH2", lambda _challenge: response.encode("utf-8"))
        else:
            connection.login(self.address, secret)

    def _imap_connect(self, password):
        host = self.settings.get("imap_host")
        port = int(self.settings.get("imap_port") or 993)
        if not host:
            raise MailboxError("No IMAP host is configured")
        if not self.settings.get("imap_tls", True):
            raise MailboxError("IMAP requires TLS")
        try:
            connection = imaplib.IMAP4_SSL(
                host=host, port=port, ssl_context=_tls_context(), timeout=self.timeout
            )
        except (OSError, ssl.SSLError, imaplib.IMAP4.error) as exc:
            raise MailboxError(f"Could not reach the IMAP server: {type(exc).__name__}") from None
        try:
            self._imap_authenticate(connection, password)
        except imaplib.IMAP4.error:
            try:
                connection.logout()
            except (OSError, imaplib.IMAP4.error):
                pass
            raise MailboxError(
                "The mail server rejected the access token" if self.auth_mechanism == "xoauth2"
                else "The mail server rejected the account credentials"
            ) from None
        return connection

    def fetch_recent(self, password, folder="INBOX", limit=10, unseen_only=False):
        """Return bounded summaries of the most recent messages."""
        limit = max(1, min(int(limit or 10), MAX_FETCH_MESSAGES))
        connection = self._imap_connect(password)
        try:
            status, _ = connection.select(self._safe_folder(folder), readonly=True)
            if status != "OK":
                raise MailboxError("The requested mail folder is unavailable")
            criteria = "UNSEEN" if unseen_only else "ALL"
            status, data = connection.search(None, criteria)
            if status != "OK":
                raise MailboxError("The mail server refused the search")
            identifiers = (data[0] or b"").split()[-limit:]
            messages = []
            for identifier in reversed(identifiers):
                status, payload = connection.fetch(identifier, "(RFC822)")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                messages.append(summarize_message(payload[0][1]))
            return messages
        finally:
            self._close(connection)

    def fetch_message(self, password, folder, message_id):
        """Return one full message body, bounded in size."""
        connection = self._imap_connect(password)
        try:
            status, _ = connection.select(self._safe_folder(folder), readonly=True)
            if status != "OK":
                raise MailboxError("The requested mail folder is unavailable")
            status, payload = connection.fetch(self._safe_identifier(message_id), "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                raise MailboxError("The requested message could not be read")
            summary = summarize_message(payload[0][1], preview_chars=MAX_PREVIEW_CHARS)
            parsed = email.message_from_bytes(payload[0][1])
            summary["body"] = _plain_text_body(parsed, MAX_BODY_CHARS)
            return summary
        finally:
            self._close(connection)

    def delete_message(self, password, folder, message_id):
        """Mark one message deleted and expunge it."""
        connection = self._imap_connect(password)
        try:
            status, _ = connection.select(self._safe_folder(folder))
            if status != "OK":
                raise MailboxError("The requested mail folder is unavailable")
            identifier = self._safe_identifier(message_id)
            status, _ = connection.store(identifier, "+FLAGS", "\\Deleted")
            if status != "OK":
                raise MailboxError("The message could not be marked for deletion")
            connection.expunge()
            return True
        finally:
            self._close(connection)

    def send(self, password, normalized, from_address="", reply_to=""):
        """Submit one already-authorized message over SMTP."""
        sender = email_policy.normalize_address(from_address) or self.address
        host = self.settings.get("smtp_host")
        port = int(self.settings.get("smtp_port") or 587)
        if not host:
            raise MailboxError("No SMTP host is configured")

        message = build_message(normalized, sender, reply_to)
        envelope_recipients = email_policy.all_recipients(normalized)
        context = _tls_context()
        try:
            if port == IMPLICIT_TLS_SMTP_PORT:
                client = smtplib.SMTP_SSL(host, port, timeout=self.timeout, context=context)
            else:
                client = smtplib.SMTP(host, port, timeout=self.timeout)
        except (OSError, smtplib.SMTPException, ssl.SSLError) as exc:
            raise MailboxError(f"Could not reach the SMTP server: {type(exc).__name__}") from None
        try:
            client.ehlo()
            if port != IMPLICIT_TLS_SMTP_PORT:
                if self.settings.get("smtp_starttls", True):
                    client.starttls(context=context)
                    client.ehlo()
                elif not self.settings.get("smtp_allow_plaintext"):
                    raise MailboxError("SMTP requires STARTTLS on this port")
                elif password:
                    raise MailboxError("Credentials are never sent over an unencrypted SMTP session")
            if password:
                if self.auth_mechanism == "xoauth2":
                    response = xoauth2_string(sender, password)
                    client.auth("XOAUTH2", lambda _challenge=None: response)
                else:
                    client.login(self.address, password)
            client.send_message(message, from_addr=sender, to_addrs=envelope_recipients)
        except smtplib.SMTPAuthenticationError:
            raise MailboxError(
                "The mail server rejected the access token" if self.auth_mechanism == "xoauth2"
                else "The mail server rejected the account credentials"
            ) from None
        except smtplib.SMTPRecipientsRefused:
            raise MailboxError("The mail server refused every recipient") from None
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise MailboxError(f"The message could not be sent: {type(exc).__name__}") from None
        finally:
            try:
                client.quit()
            except (smtplib.SMTPException, OSError):
                pass
        return {"message_id": message["Message-ID"], "recipient_count": len(envelope_recipients)}

    @staticmethod
    def _safe_folder(folder):
        """Return a quoted folder name, refusing IMAP command injection."""
        name = str(folder or "INBOX").strip() or "INBOX"
        if len(name) > 200 or any(ch in name for ch in '"\\\r\n\x00'):
            raise MailboxError("Invalid mail folder name")
        return f'"{name}"'

    @staticmethod
    def _safe_identifier(message_id):
        """Return a numeric IMAP sequence identifier."""
        text = str(message_id or "").strip()
        if not text.isdigit() or len(text) > 12:
            raise MailboxError("Invalid message identifier")
        return text

    @staticmethod
    def _close(connection):
        try:
            connection.close()
        except (OSError, imaplib.IMAP4.error):
            pass
        try:
            connection.logout()
        except (OSError, imaplib.IMAP4.error):
            pass

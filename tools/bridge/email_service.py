"""Email orchestration: account persistence, credentials, and adapter dispatch.

This layer sits between the bridge HTTP handlers and the mailbox adapters. It
owns three responsibilities and deliberately no others:

1. Persisting non-secret account records and the operator allowlist.
2. Holding mailbox credentials **in memory only**. A mail password or OAuth
   token is never written to `EVA_CONFIG_DIR`, telemetry, or an audit record.
   Eva Standalone re-supplies credentials from `safeStorage` after a restart,
   the same way the vault key is re-established rather than persisted.
3. Dispatching an already-authorized operation to the correct adapter.

Authorization decisions belong to `email_policy` and `email_accounts`. This
module calls them; it never re-implements or relaxes them.
"""

import json
import os
import sys
import threading

from bridge import config as _cfg
from bridge import email_accounts
from bridge import email_policy
from bridge.audit import audit_event
from bridge.mailbox_imap import ImapSmtpMailbox, MailboxError

_EMAIL_CONFIG_PATH = _cfg.EMAIL_CONFIG_PATH
_config_lock = threading.RLock()
_credential_lock = threading.RLock()

# Account id -> secret. Process memory only; never serialized.
_credentials = {}

MAX_FETCH_LIMIT = 50


class EmailServiceError(Exception):
    """Raised when an email operation cannot be completed."""


def _default_document():
    return {"accounts": [], "allowlist": []}


def load_config():
    """Return the persisted account document. Never raises."""
    document = _default_document()
    try:
        if os.path.isfile(_EMAIL_CONFIG_PATH):
            with open(_EMAIL_CONFIG_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                accounts, _ = email_accounts.normalize_accounts(data.get("accounts"))
                document["accounts"] = accounts
                document["allowlist"] = [
                    entry for entry in (
                        str(v).strip().lower() for v in data.get("allowlist") or []
                    ) if entry
                ][:email_policy.MAX_ALLOWLIST_ENTRIES]
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[Email] Could not load account config: {exc}", file=sys.stderr)
    return document


def save_config(document):
    """Persist accounts and allowlist atomically. Returns True on success."""
    try:
        os.makedirs(os.path.dirname(_EMAIL_CONFIG_PATH), exist_ok=True)
        temporary = _EMAIL_CONFIG_PATH + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, _EMAIL_CONFIG_PATH)
        return True
    except (OSError, TypeError) as exc:
        print(f"[Email] Could not save account config: {exc}", file=sys.stderr)
        return False


def replace_accounts(raw_accounts, raw_allowlist=None):
    """Validate and persist the full account set. Returns (document, errors)."""
    accounts, errors = email_accounts.normalize_accounts(raw_accounts)
    with _config_lock:
        document = load_config()
        document["accounts"] = accounts
        if raw_allowlist is not None:
            document["allowlist"] = [
                entry for entry in (
                    str(v).strip().lower() for v in raw_allowlist or []
                ) if entry
            ][:email_policy.MAX_ALLOWLIST_ENTRIES]
        save_config(document)
        _forget_orphaned_credentials({a["id"] for a in accounts})
        return document, errors


def public_accounts(document=None):
    """Return account records annotated with credential readiness, never secrets."""
    document = document if isinstance(document, dict) else load_config()
    with _credential_lock:
        held = set(_credentials)
    listed = []
    for account in document.get("accounts", []):
        entry = dict(account)
        entry["credential_present"] = account["id"] in held
        listed.append(entry)
    return listed


def set_credential(account_id, secret):
    """Hold one mailbox secret in process memory."""
    account_id = str(account_id or "").strip()
    if not account_id:
        raise EmailServiceError("An account id is required")
    if not isinstance(secret, str) or not secret:
        raise EmailServiceError("A non-empty credential is required")
    with _credential_lock:
        _credentials[account_id] = secret
    audit_event("email_credential_set", correlation_id=account_id, outcome="stored")
    return True


def clear_credential(account_id):
    """Forget one held secret."""
    with _credential_lock:
        existed = _credentials.pop(str(account_id or ""), None) is not None
    return existed


def _forget_orphaned_credentials(known_ids):
    with _credential_lock:
        for account_id in list(_credentials):
            if account_id not in known_ids:
                _credentials.pop(account_id, None)


def _credential_for(account_id):
    with _credential_lock:
        secret = _credentials.get(str(account_id or ""))
    if not secret:
        raise EmailServiceError("No credential is available for this account; unlock it in Settings")
    return secret


def _account_or_error(document, account_id, capability):
    for account in document.get("accounts", []):
        if account["id"] == str(account_id or ""):
            if not email_accounts.usable(account, capability):
                raise EmailServiceError(f"This account cannot {capability} mail right now")
            return account
    raise EmailServiceError("Unknown email account")


def _adapter_for(account):
    backend = account.get("backend")
    if backend == "imap_smtp":
        return ImapSmtpMailbox(account.get("settings") or {}, account["address"])
    raise EmailServiceError(f"The {backend} backend is not connected yet")


def fetch_messages(account_id, folder="INBOX", limit=10, unseen_only=False):
    """Return bounded message summaries for one account."""
    document = load_config()
    account = _account_or_error(document, account_id, "read")
    limit = max(1, min(int(limit or 10), MAX_FETCH_LIMIT))
    try:
        messages = _adapter_for(account).fetch_recent(
            _credential_for(account["id"]), folder=folder, limit=limit, unseen_only=unseen_only
        )
    except MailboxError as exc:
        audit_event("email_fetch", correlation_id=account["id"], outcome="failed")
        raise EmailServiceError(str(exc)) from None
    audit_event(
        "email_fetch", correlation_id=account["id"], outcome="ok",
        backend=account["backend"], message_count=len(messages), folder=str(folder)[:60],
    )
    return messages


def delete_message(account_id, folder, message_id):
    """Delete one message after confirming the account allows it."""
    document = load_config()
    account = _account_or_error(document, account_id, "delete")
    try:
        _adapter_for(account).delete_message(_credential_for(account["id"]), folder, message_id)
    except MailboxError as exc:
        audit_event("email_delete", correlation_id=account["id"], outcome="failed")
        raise EmailServiceError(str(exc)) from None
    audit_event(
        "email_delete", correlation_id=account["id"], outcome="ok",
        backend=account["backend"], folder=str(folder)[:60],
    )
    return True


def authorize(request, account_id="", from_address="", confirmation=None):
    """Run the full send authorization without delivering anything."""
    document = load_config()
    account, error = email_accounts.select_send_account(
        document.get("accounts", []), account_id=account_id, from_address=from_address
    )
    if error:
        return {"decision": "rejected", "reason": error}
    return email_accounts.authorize_send_for_account(
        account, request, document.get("allowlist", []), confirmation,
        accounts=document.get("accounts", []),
    )


def send_message(request, account_id="", from_address="", confirmation=None):
    """Authorize and deliver one message.

    A `needs_confirmation` or `rejected` decision is returned unchanged and
    nothing is delivered. Only an `allowed` decision reaches an adapter.
    """
    decision = authorize(request, account_id, from_address, confirmation)
    if decision.get("decision") != "allowed":
        audit_event(
            "email_send", correlation_id=str(account_id or "auto"),
            outcome=decision.get("decision", "rejected"),
        )
        return decision

    document = load_config()
    account = _account_or_error(document, decision["account_id"], "send")
    normalized = decision["request"]

    if account["backend"] == "eva_direct":
        results = _deliver_direct(account, document, normalized, decision["delivery_plan"])
    else:
        results = [_deliver_simple(account, normalized, account["address"])]

    audit_event(
        "email_send", correlation_id=account["id"], outcome="sent",
        **email_policy.audit_fields(normalized, backend=account["backend"]),
    )
    return {"decision": "sent", "account_id": account["id"], "deliveries": results}


def _deliver_simple(account, normalized, from_address):
    try:
        result = _adapter_for(account).send(
            _credential_for(account["id"]), normalized, from_address=from_address
        )
    except MailboxError as exc:
        audit_event("email_send", correlation_id=account["id"], outcome="failed")
        raise EmailServiceError(str(exc)) from None
    return {"route": "account", "recipient_count": result["recipient_count"]}


def _deliver_direct(account, document, normalized, plan):
    """Deliver Eva's own identity across its internal and relay routes."""
    deliveries = []
    for route in plan.get("routes", []):
        scoped = dict(normalized)
        scoped["to"] = [r for r in normalized.get("to", []) if r in route["recipients"]]
        scoped["cc"] = [r for r in normalized.get("cc", []) if r in route["recipients"]]
        scoped["bcc"] = [r for r in normalized.get("bcc", []) if r in route["recipients"]]
        if not email_policy.all_recipients(scoped):
            continue

        if route["route"] == "internal":
            starttls = bool(route.get("smtp_starttls", True))
            mailbox = ImapSmtpMailbox(
                {"smtp_host": route["smtp_host"], "smtp_port": route["smtp_port"],
                 "smtp_starttls": starttls, "smtp_allow_plaintext": not starttls,
                 "imap_host": "", "imap_tls": True},
                account["address"],
            )
            # An internal MTA accepts relay from the trusted network without submission auth.
            secret = ""
        else:
            relay = _account_or_error(document, route["relay_account_id"], "send")
            mailbox = _adapter_for(relay)
            secret = _credential_for(relay["id"])

        try:
            result = mailbox.send(secret, scoped, from_address=plan["from"])
        except MailboxError as exc:
            audit_event(
                "email_send", correlation_id=account["id"], outcome="failed",
                route=route["route"],
            )
            raise EmailServiceError(f"{route['route']} delivery failed: {exc}") from None
        deliveries.append({"route": route["route"], "recipient_count": result["recipient_count"]})
    return deliveries


def morning_mail_summary(limit_per_account=5):
    """Return an untrusted-framed digest for the startup briefing.

    Never raises: a briefing must degrade rather than fail. Accounts that are
    locked or unreachable are reported by name only.
    """
    document = load_config()
    selected = email_accounts.read_accounts(document.get("accounts", []), morning_only=True)
    if not selected:
        return "", []

    blocks = []
    unavailable = []
    for account in selected:
        try:
            messages = fetch_messages(account["id"], limit=limit_per_account, unseen_only=True)
        except EmailServiceError:
            unavailable.append(account["label"])
            continue
        if not messages:
            continue
        blocks.append(email_policy.mail_prompt_data_block(
            f"Unread mail - {account['label']}", messages
        ))
    return "\n\n".join(block for block in blocks if block), unavailable

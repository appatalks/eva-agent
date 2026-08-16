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
MAX_ACCOUNT_SUMMARY = 8


class EmailServiceError(Exception):
    """Raised when an email operation cannot be completed."""


class EmailValidationError(EmailServiceError):
    """Raised when a caller supplies invalid email configuration."""


class EmailPersistenceError(EmailServiceError):
    """Raised when valid email configuration cannot be persisted."""


def _default_document():
    return {"accounts": [], "allowlist": []}


def _load_raw_config():
    """Return the persisted document without rewriting opaque provider records."""
    document = _default_document()
    try:
        if os.path.isfile(_EMAIL_CONFIG_PATH):
            with open(_EMAIL_CONFIG_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                document = dict(data)
                document["accounts"] = [
                    dict(account) for account in data.get("accounts") or []
                    if isinstance(account, dict)
                ]
                document["allowlist"] = [
                    entry for entry in (
                        str(v).strip().lower() for v in data.get("allowlist") or []
                    ) if entry
                ][:email_policy.MAX_ALLOWLIST_ENTRIES]
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[Email] Could not load account config: {exc}", file=sys.stderr)
    return document


def load_config():
    """Return the normalized runtime view. Never raises or exposes secrets."""
    raw = _load_raw_config()
    accounts, _ = email_accounts.normalize_accounts(raw.get("accounts"))
    return {"accounts": accounts, "allowlist": raw.get("allowlist", [])}


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
    if not isinstance(raw_accounts, list):
        raise EmailValidationError("accounts must be a list")
    accounts, errors = email_accounts.normalize_accounts(raw_accounts)
    with _config_lock:
        document = _load_raw_config()
        if errors:
            return document, errors
        replacement = dict(document)
        replacement["accounts"] = accounts
        if raw_allowlist is not None:
            replacement["allowlist"] = [
                entry for entry in (
                    str(v).strip().lower() for v in raw_allowlist or []
                ) if entry
            ][:email_policy.MAX_ALLOWLIST_ENTRIES]
        if not save_config(replacement):
            raise EmailPersistenceError("Email settings could not be saved")
        _reconcile_credentials(document.get("accounts", []), accounts)
        return load_config(), []


def upsert_account(raw_account):
    """Create or replace one account without rewriting unrelated accounts."""
    account, error = email_accounts.normalize_account(raw_account)
    if error:
        raise EmailValidationError(error)
    with _config_lock:
        document = _load_raw_config()
        existing_accounts = list(document.get("accounts", []))
        previous = next(
            (existing for existing in existing_accounts if existing.get("id") == account["id"]),
            None,
        )
        prospective = list(existing_accounts)
        if previous:
            index = next(
                i for i, existing in enumerate(prospective)
                if existing.get("id") == account["id"]
            )
            prospective[index] = account
        else:
            prospective.append(account)
        _validate_account_collection(prospective)
        replacement = dict(document)
        replacement["accounts"] = prospective
        if not save_config(replacement):
            raise EmailPersistenceError("Email settings could not be saved")
        if previous and _credential_binding(previous) != _credential_binding(account):
            clear_credential(account["id"])
        return load_config()


def delete_account(account_id):
    """Delete one account after the updated document is safely persisted."""
    account_id = str(account_id or "").strip()
    if not account_id:
        raise EmailValidationError("An account id is required")
    with _config_lock:
        document = _load_raw_config()
        existing = document.get("accounts", [])
        if not any(account.get("id") == account_id for account in existing):
            raise EmailValidationError("Unknown email account")
        replacement = dict(document)
        replacement["accounts"] = [
            account for account in existing if account.get("id") != account_id
        ]
        if not save_config(replacement):
            raise EmailPersistenceError("Email settings could not be saved")
        clear_credential(account_id)
        return load_config()


def update_allowlist(raw_allowlist):
    """Replace approved recipients without rewriting mailbox records."""
    if not isinstance(raw_allowlist, list):
        raise EmailValidationError("allowlist must be a list")
    with _config_lock:
        document = _load_raw_config()
        replacement = dict(document)
        replacement["allowlist"] = [
            entry for entry in (
                str(value).strip().lower() for value in raw_allowlist
            ) if entry
        ][:email_policy.MAX_ALLOWLIST_ENTRIES]
        if not save_config(replacement):
            raise EmailPersistenceError("Approved recipients could not be saved")
        return load_config()


def _validate_account_collection(accounts):
    """Check collection invariants without normalizing unrelated opaque records."""
    if len(accounts) > email_accounts.MAX_ACCOUNTS:
        raise EmailValidationError(f"no more than {email_accounts.MAX_ACCOUNTS} accounts are allowed")
    seen_ids = set()
    seen_addresses = set()
    for item in accounts:
        account_id = str(item.get("id") or "").strip()
        if account_id:
            if account_id in seen_ids:
                raise EmailValidationError("duplicate account id")
            seen_ids.add(account_id)
        address = email_policy.normalize_address(item.get("address"))
        backend = str(item.get("backend") or "").strip().lower()
        if address and backend:
            key = (backend, address)
            if key in seen_addresses:
                raise EmailValidationError("duplicate address for this backend")
            seen_addresses.add(key)


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
    account = next(
        (item for item in load_config().get("accounts", []) if item["id"] == account_id),
        None,
    )
    if not account:
        raise EmailValidationError("Unknown email account")
    if account.get("backend") == "eva_direct":
        raise EmailValidationError("Eva's direct identity does not use a credential")
    with _credential_lock:
        _credentials[account_id] = secret
    audit_event("email_credential_set", correlation_id=account_id, outcome="stored")
    return True


def clear_credential(account_id):
    """Forget one held secret."""
    with _credential_lock:
        existed = _credentials.pop(str(account_id or ""), None) is not None
    return existed


def _credential_binding(account):
    """Return the non-secret connection identity a held credential is bound to."""
    settings = (account or {}).get("settings") or {}
    return (
        (account or {}).get("backend"),
        (account or {}).get("address"),
        settings.get("imap_host"),
        settings.get("imap_port"),
        settings.get("smtp_host"),
        settings.get("smtp_port"),
        settings.get("auth_mechanism"),
    )


def _reconcile_credentials(previous_accounts, replacement_accounts):
    previous = {
        account["id"]: account for account in previous_accounts if account.get("id")
    }
    replacement = {
        account["id"]: account for account in replacement_accounts if account.get("id")
    }
    with _credential_lock:
        for account_id in list(_credentials):
            if account_id not in replacement or (
                account_id in previous
                and _credential_binding(previous[account_id]) != _credential_binding(replacement[account_id])
            ):
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
        deliveries, failures = _deliver_direct(account, document, normalized, decision["delivery_plan"])
        if not deliveries:
            audit_event("email_send", correlation_id=account["id"], outcome="failed")
            raise EmailServiceError("; ".join(failures) or "delivery failed")
        if failures:
            # Some recipients already have the message. Say so, so the user does
            # not resend and deliver it twice to the routes that succeeded.
            audit_event(
                "email_send", correlation_id=account["id"], outcome="partial",
                **email_policy.audit_fields(normalized, backend=account["backend"]),
            )
            return {
                "decision": "partially_sent",
                "account_id": account["id"],
                "deliveries": deliveries,
                "failures": failures,
            }
        if deliveries and all(delivery.get("route") == "local_mta" for delivery in deliveries):
            audit_event(
                "email_send", correlation_id=account["id"], outcome="submitted",
                **email_policy.audit_fields(normalized, backend=account["backend"]),
            )
            return {
                "decision": "submitted",
                "account_id": account["id"],
                "deliveries": deliveries,
                "warning": "The local mail system accepted the message; final delivery is not verified.",
            }
    else:
        deliveries = [_deliver_simple(account, normalized, account["address"])]

    audit_event(
        "email_send", correlation_id=account["id"], outcome="sent",
        **email_policy.audit_fields(normalized, backend=account["backend"]),
    )
    return {"decision": "sent", "account_id": account["id"], "deliveries": deliveries}


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
    """Deliver Eva's own identity across its internal and relay routes.

    Returns (deliveries, failures). A route that fails does not abort the others:
    each route is an independent submission, and losing the record of a route
    that already delivered would invite a duplicate resend.
    """
    deliveries = []
    failures = []
    for route in plan.get("routes", []):
        scoped = dict(normalized)
        scoped["to"] = [r for r in normalized.get("to", []) if r in route["recipients"]]
        scoped["cc"] = [r for r in normalized.get("cc", []) if r in route["recipients"]]
        scoped["bcc"] = [r for r in normalized.get("bcc", []) if r in route["recipients"]]
        if not email_policy.all_recipients(scoped):
            continue

        try:
            if route["route"] in {"internal", "local_mta"}:
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
            result = mailbox.send(secret, scoped, from_address=plan["from"])
        except (MailboxError, EmailServiceError) as exc:
            audit_event(
                "email_send", correlation_id=account["id"], outcome="route-failed",
                route=route["route"],
            )
            failures.append(f"{route['route']} delivery failed: {exc}")
            continue
        deliveries.append({"route": route["route"], "recipient_count": result["recipient_count"]})
    return deliveries, failures


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


def capability_summary():
    """Describe Eva's current email ability truthfully for prompt context.

    Derived from live configuration rather than a static claim, so Eva never
    asserts an ability she does not currently have. Never raises.
    """
    try:
        document = load_config()
        accounts = public_accounts(document)
    except Exception:
        return (
            "[Email Capability]\n"
            "Email status could not be read. Do not claim you can send or read email."
        )

    if not accounts:
        return (
            "[Email Capability]\n"
            "No email account is configured, so you cannot read or send email right now. "
            "If the user asks, say so plainly and offer to set one up in Settings. "
            "Never claim a message was sent."
        )

    lines = []
    for account in accounts[:MAX_ACCOUNT_SUMMARY]:
        abilities = "/".join(account.get("capabilities") or []) or "none"
        state = account.get("status", "unknown")
        if state == "connected" and account.get("backend") != "eva_direct" and not account.get("credential_present"):
            state = "locked (needs sign-in)"
        notes = []
        if account.get("morning_pull"):
            notes.append("in morning routine")
        if account.get("backend") == "eva_direct":
            mode = (account.get("settings") or {}).get("delivery_mode", "internal")
            notes.append(f"Eva's own identity, {mode} delivery")
        suffix = f"; {', '.join(notes)}" if notes else ""
        lines.append(
            f"  - {account.get('label')} <{account.get('address')}>: {abilities}; {state}{suffix}"
        )

    return (
        "[Email Capability]\n"
        "You can work with these mailboxes:\n"
        + "\n".join(lines)
        + "\n"
        "You never choose a recipient on your own authority. An address must already be "
        "approved, or the user confirms that exact message first; the bridge enforces this "
        "and will refuse otherwise. A locked account needs the user to sign in before you "
        "can use it. Never state that mail was sent, read, or deleted unless the operation "
        "actually returned success."
    )

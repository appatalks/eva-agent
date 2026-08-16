"""Multi-account email registry: routing, capabilities, and provider defaults.

Eva reaches several mailboxes at once - a Microsoft 365 tenant through Work IQ,
personal Gmail or Outlook, custom domains over IMAP/SMTP, and her own direct
sending identity. This module owns which account handles a given operation and
what that account is permitted to do. It is pure: no network, no credential, no
filesystem access.

Secrets never live in an account record. A record holds host names, ports, and
capability flags; passwords, app passwords, and OAuth tokens are stored by the
credential layer and looked up by account id.

`eva_direct` is deliberately stricter than every other backend. Mail sent from
Eva's own identity cannot inherit a user mailbox's reputation, so it may only go
to recipients who explicitly accepted Eva as a direct sender. That list is a
consent record, not a convenience allowlist, and an unlisted recipient is
rejected outright rather than offered for confirmation.

Eva's direct identity delivers over two routes:

- **internal** - a single configured internal MTA, used for recipients in the
  configured internal domains. Delivery never performs a per-recipient host
  lookup, so a recipient address cannot steer a connection at an arbitrary host
  on the local network.
- **relay** - the user's existing authenticated SMTP account, used for everyone
  else. Eva's From address must be aligned with that account's domain, because a
  cross-domain From fails SPF and DMARC and is silently filtered. Alignment is
  checked when the account is saved, not when a message fails to arrive.

A relay route additionally requires that `eva@<domain>` already exist as a
permitted send-as identity on the relaying provider. This module cannot verify
that remotely; it reports the requirement so setup can surface it.
"""

import re

from bridge import email_policy

BACKENDS = ("workiq", "gmail_oauth", "imap_smtp", "eva_direct")
CAPABILITIES = ("read", "send", "delete")
STATUSES = ("connected", "needs_auth", "disabled", "error")
DELIVERY_MODES = ("auto", "relay", "internal")

MAX_ACCOUNTS = 12
MAX_LABEL_CHARS = 60
MAX_HOST_CHARS = 253

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")

# Domains whose mail is reached through a hosted API rather than IMAP/SMTP.
_OAUTH_DOMAINS = {
    "gmail.com": "gmail_oauth",
    "googlemail.com": "gmail_oauth",
}
_MICROSOFT_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}

_BACKEND_DEFAULT_CAPABILITIES = {
    "workiq": ("read", "send", "delete"),
    "gmail_oauth": ("read", "send", "delete"),
    "imap_smtp": ("read", "send", "delete"),
    "eva_direct": ("send",),
}


def suggest_backend(address):
    """Guess the backend for an address so setup needs as little input as possible."""
    canonical = email_policy.normalize_address(address)
    if not canonical:
        return ""
    domain = canonical.rsplit("@", 1)[-1]
    if domain in _OAUTH_DOMAINS:
        return _OAUTH_DOMAINS[domain]
    if domain in _MICROSOFT_DOMAINS:
        return "workiq"
    return "imap_smtp"


def suggest_imap_smtp_hosts(address):
    """Return conventional host names for a custom domain, to be probed before use.

    These are a starting guess in the style of client autoconfiguration. The
    connection layer must verify them; an unreachable guess is not an error.
    """
    canonical = email_policy.normalize_address(address)
    if not canonical:
        return {}
    domain = canonical.rsplit("@", 1)[-1]
    return {
        "imap_host": f"imap.{domain}",
        "imap_port": 993,
        "imap_tls": True,
        "smtp_host": f"smtp.{domain}",
        "smtp_port": 587,
        "smtp_starttls": True,
    }


def _clean(value, limit):
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()
    return text[:limit]


def _normalize_host(value):
    host = _clean(value, MAX_HOST_CHARS).lower()
    return host if _HOST_RE.fullmatch(host) else ""


def _normalize_port(value, default):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def normalize_account(raw):
    """Validate one account record. Returns (account, error)."""
    if not isinstance(raw, dict):
        return None, "account must be an object"

    account_id = _clean(raw.get("id"), 64)
    if not _ID_RE.fullmatch(account_id):
        return None, "account id must be 1-64 characters of A-Z, a-z, 0-9, hyphen, or underscore"

    address = email_policy.normalize_address(raw.get("address"))
    if not address:
        return None, "account address is not a valid email address"

    backend = _clean(raw.get("backend"), 32).lower() or suggest_backend(address)
    if backend not in BACKENDS:
        return None, f"backend must be one of: {', '.join(BACKENDS)}"

    allowed = _BACKEND_DEFAULT_CAPABILITIES[backend]
    requested = raw.get("capabilities")
    if requested is None:
        capabilities = list(allowed)
    else:
        if not isinstance(requested, (list, tuple)):
            return None, "capabilities must be a list"
        capabilities = [c for c in CAPABILITIES if c in {str(v).strip().lower() for v in requested}]
    # A backend can never gain a capability it does not implement.
    capabilities = [c for c in capabilities if c in allowed]
    if not capabilities:
        return None, "account must retain at least one supported capability"

    status = _clean(raw.get("status"), 16).lower()
    if status not in STATUSES:
        status = "needs_auth"

    account = {
        "id": account_id,
        "label": _clean(raw.get("label"), MAX_LABEL_CHARS) or address,
        "backend": backend,
        "address": address,
        "capabilities": capabilities,
        "status": status,
        "morning_pull": bool(raw.get("morning_pull", "read" in capabilities)),
        "default_send": bool(raw.get("default_send")),
        "allowlist": sorted(set(
            entry for entry in (
                str(v).strip().lower() for v in raw.get("allowlist") or []
            ) if entry
        ))[:email_policy.MAX_ALLOWLIST_ENTRIES],
    }

    if backend == "imap_smtp":
        settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
        defaults = suggest_imap_smtp_hosts(address)
        imap_host = _normalize_host(settings.get("imap_host") or defaults["imap_host"])
        smtp_host = _normalize_host(settings.get("smtp_host") or defaults["smtp_host"])
        if not imap_host or not smtp_host:
            return None, "IMAP and SMTP host names must be valid domain names"
        account["settings"] = {
            "imap_host": imap_host,
            "imap_port": _normalize_port(settings.get("imap_port"), 993),
            "imap_tls": bool(settings.get("imap_tls", True)),
            "smtp_host": smtp_host,
            "smtp_port": _normalize_port(settings.get("smtp_port"), 587),
            "smtp_starttls": bool(settings.get("smtp_starttls", True)),
        }
        if not account["settings"]["imap_tls"]:
            return None, "IMAP must use TLS"
    elif backend == "eva_direct":
        settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
        consent = []
        for value in settings.get("direct_consent") or []:
            entry = str(value or "").strip().lower()
            if entry.startswith("@"):
                if _HOST_RE.fullmatch(entry[1:]):
                    consent.append(entry)
                continue
            canonical = email_policy.normalize_address(entry)
            if canonical:
                consent.append(canonical)
        mode = _clean(settings.get("delivery_mode"), 16).lower()
        if mode not in DELIVERY_MODES:
            mode = "internal" if settings.get("internal_only", True) else "auto"
        internal_domains = sorted({
            domain for domain in (
                _normalize_host(value) for value in settings.get("internal_domains") or []
            ) if domain
        })[:50]
        account["settings"] = {
            "direct_consent": sorted(set(consent))[:email_policy.MAX_ALLOWLIST_ENTRIES],
            "delivery_mode": mode,
            "internal_domains": internal_domains,
            "internal_smtp_host": _normalize_host(settings.get("internal_smtp_host")),
            "internal_smtp_port": _normalize_port(settings.get("internal_smtp_port"), 25),
            "relay_account_id": _clean(settings.get("relay_account_id"), 64),
        }
        if mode in ("internal", "auto") and internal_domains and not account["settings"]["internal_smtp_host"]:
            return None, "internal delivery requires an internal SMTP host"
        if mode == "relay" and not account["settings"]["relay_account_id"]:
            return None, "relay delivery requires a relay account"
        account["morning_pull"] = False
    else:
        account["settings"] = {}

    return account, ""


def normalize_accounts(raw_accounts):
    """Validate a list of accounts. Returns (accounts, errors)."""
    accounts = []
    errors = []
    seen_ids = set()
    seen_addresses = set()
    for index, raw in enumerate(list(raw_accounts or [])[:MAX_ACCOUNTS]):
        account, error = normalize_account(raw)
        if error:
            errors.append(f"account {index}: {error}")
            continue
        if account["id"] in seen_ids:
            errors.append(f"account {index}: duplicate account id")
            continue
        key = (account["backend"], account["address"])
        if key in seen_addresses:
            errors.append(f"account {index}: duplicate address for this backend")
            continue
        seen_ids.add(account["id"])
        seen_addresses.add(key)
        accounts.append(account)
    return accounts, errors


def usable(account, capability):
    """Return True when an account is connected and offers a capability."""
    return (
        isinstance(account, dict)
        and account.get("status") == "connected"
        and capability in (account.get("capabilities") or [])
    )


def read_accounts(accounts, morning_only=False):
    """Return accounts Eva may read, optionally only those in the morning routine."""
    selected = [a for a in accounts or [] if usable(a, "read")]
    if morning_only:
        selected = [a for a in selected if a.get("morning_pull")]
    return selected


def select_send_account(accounts, account_id="", from_address=""):
    """Choose the account that will send. Returns (account, error)."""
    candidates = [a for a in accounts or [] if usable(a, "send")]
    if not candidates:
        return None, "no connected account can send mail"

    wanted_id = _clean(account_id, 64)
    if wanted_id:
        for account in candidates:
            if account["id"] == wanted_id:
                return account, ""
        return None, "the requested account cannot send mail"

    wanted_address = email_policy.normalize_address(from_address)
    if wanted_address:
        for account in candidates:
            if account["address"] == wanted_address:
                return account, ""
        return None, "no connected sending account matches that from address"

    for account in candidates:
        if account.get("default_send"):
            return account, ""
    # Eva's own identity is never an implicit default; a user mailbox is preferred.
    preferred = [a for a in candidates if a["backend"] != "eva_direct"]
    if preferred:
        return preferred[0], ""
    return None, "only Eva's direct identity is available; choose it explicitly"


def effective_allowlist(account, global_allowlist):
    """Merge the operator-wide allowlist with an account's own entries."""
    entries = list(global_allowlist or [])
    entries.extend((account or {}).get("allowlist") or [])
    return entries


def direct_consent_failures(account, recipients):
    """Return recipients that have not accepted Eva as a direct sender."""
    if not isinstance(account, dict) or account.get("backend") != "eva_direct":
        return []
    settings = account.get("settings") or {}
    addresses, domains = email_policy.normalize_allowlist(settings.get("direct_consent") or [])
    return [r for r in recipients or [] if not email_policy.is_allowlisted(r, addresses, domains)]


def domain_of(address):
    """Return the lowercase domain of an address, or an empty string."""
    canonical = email_policy.normalize_address(address)
    return canonical.rsplit("@", 1)[-1] if canonical else ""


def domains_aligned(from_address, relay_address):
    """Return True when a From domain can pass SPF/DMARC through a relay domain.

    Exact match, or From at a subdomain of the relay domain. A cross-domain From
    is never treated as aligned, because the resulting mail fails authentication
    at the receiving provider.
    """
    from_domain = domain_of(from_address)
    relay_domain = domain_of(relay_address)
    if not from_domain or not relay_domain:
        return False
    return from_domain == relay_domain or from_domain.endswith("." + relay_domain)


def is_internal_recipient(account, address):
    """Return True when a recipient belongs to a configured internal domain."""
    settings = (account or {}).get("settings") or {}
    domain = domain_of(address)
    if not domain:
        return False
    for internal in settings.get("internal_domains") or []:
        if domain == internal or domain.endswith("." + internal):
            return True
    return False


def plan_direct_delivery(account, recipients, accounts=None):
    """Split recipients across Eva's internal and relay routes.

    Returns (plan, error). A message may legitimately span both routes; each
    route is reported separately so the caller delivers once per route rather
    than guessing a single destination.
    """
    if not isinstance(account, dict) or account.get("backend") != "eva_direct":
        return None, "delivery planning applies only to Eva's direct identity"

    settings = account.get("settings") or {}
    mode = settings.get("delivery_mode") or "internal"
    internal = [r for r in recipients or [] if is_internal_recipient(account, r)]
    external = [r for r in recipients or [] if r not in internal]

    if mode == "internal" and external:
        return None, "Eva's direct identity is configured for internal delivery only"
    if mode == "relay":
        internal, external = [], list(recipients or [])

    routes = []
    if internal:
        host = settings.get("internal_smtp_host")
        if not host:
            return None, "internal delivery requires an internal SMTP host"
        routes.append({
            "route": "internal",
            "recipients": internal,
            "smtp_host": host,
            "smtp_port": settings.get("internal_smtp_port") or 25,
        })

    if external:
        relay_id = settings.get("relay_account_id")
        if not relay_id:
            return None, "sending outside the internal network requires a relay account"
        relay = next((a for a in accounts or [] if a.get("id") == relay_id), None)
        if not relay:
            return None, "the configured relay account no longer exists"
        if not usable(relay, "send"):
            return None, "the configured relay account cannot send mail"
        if not domains_aligned(account.get("address"), relay.get("address")):
            return None, (
                "Eva's address must be on the same domain as the relay account, "
                "otherwise the message fails SPF and DMARC"
            )
        routes.append({
            "route": "relay",
            "recipients": external,
            "relay_account_id": relay["id"],
            "relay_backend": relay["backend"],
        })

    if not routes:
        return None, "no recipients to deliver"
    return {"from": account.get("address"), "routes": routes}, ""


def authorize_send_for_account(account, request, global_allowlist, confirmation=None, accounts=None):
    """Apply account-specific rules on top of the shared send policy."""
    if not usable(account, "send"):
        return {"decision": "rejected", "reason": "the selected account cannot send mail"}

    result = email_policy.authorize_send(
        request, effective_allowlist(account, global_allowlist), confirmation
    )
    if result["decision"] == "rejected":
        return result

    if account.get("backend") == "eva_direct":
        missing = direct_consent_failures(account, email_policy.all_recipients(result["request"]))
        if missing:
            # Consent is not confirmable in the moment: the recipient must opt in first.
            return {
                "decision": "rejected",
                "reason": "these recipients have not accepted mail from Eva's direct identity",
                "unconsented_recipients": missing,
            }
        plan, plan_error = plan_direct_delivery(
            account, email_policy.all_recipients(result["request"]), accounts
        )
        if plan_error:
            return {"decision": "rejected", "reason": plan_error}
        result["delivery_plan"] = plan

    result["account_id"] = account["id"]
    result["backend"] = account["backend"]
    return result

"""ag2_gateway_credentials — the CANONICAL pure credential contract.

THE single source of AG2 Space gateway credential PARSING/PRECEDENCE
semantics. Both consumers use machine-generated copies of this file
(`tools/sync_gateway_credentials.py`):

    packages/ag2-sparrow/ag2_sparrow/gateway_credentials.py   (generated)
    skills/agent-room-ops/gateway_credentials.py              (generated)

Neutral top-level home by owner ruling (Feature Haul 2026-08-05): sparrow is
a CONSUMER of the contract, not its owner — canonical-in-sparrow would
invite sparrow's runtime lifecycle (device-env discovery, rotation, reload,
DNS policy) to leak into the shared module. This module is strictly pure:

    it never reads os.environ, touches the filesystem, calls a vault,
    prints diagnostics, refreshes tokens, sends HTTP, mutates globals,
    or decides agent identity.

It also holds the masking contract: the secret registry (an object the caller
owns), the renderings a secret can take, and what a log line, a status field
or a gateway-supplied value may carry (code_field, redact_text, safe_status,
describe).

Consumers own those as runtime adapters and feed plain values in.

Promotion contract: when a THIRD consumer appears (CLI/MCP/external), this
becomes the independent ag2-gateway-client package; the generated copies
convert to a dependency with this API unchanged.
"""
from __future__ import annotations

import errno as _errno
import math
import os.path
import re
import shlex
import socket
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

# Canonical alias precedence (earlier wins). GATEWAY_* primary; RELAY_* and
# REMOTE_TASK_* transition aliases; AG2_* legacy.
TOKEN_ALIAS_PRECEDENCE: "tuple[str, ...]" = (
    "GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")
URL_ALIAS_PRECEDENCE: "tuple[str, ...]" = (
    "GATEWAY_URL", "RELAY_URL", "REMOTE_TASK_URL", "AG2_REMOTE_URL")

# The combined onboarding form separates URL from secret with "|" — which
# some transports URL-encode as %7C/%7c. Only a value STARTING with an
# http(s) scheme is combined; a bare secret is opaque and never touched even
# if it contains either separator (#2307: secrets are split, never mutated).
# A literal "|" is PREFERRED over %7C/%7c when both appear: a raw pipe
# cannot legally occur inside a URL, so when one exists it IS the separator
# — this keeps a URL half that carries an encoded %7C intact (#2670 review).
_ENCODED_SEPARATOR_RE = re.compile(r"%7[Cc]")


@dataclass(frozen=True)
class GatewayCredentials:
    """Normalized resolution result.

    ``token`` is excluded from repr so a stray log/print of this object can
    never leak the secret. ``source`` is a qualified, secret-free origin tag
    the consumer supplies — e.g. ``env:REMOTE_TASK_TOKEN``,
    ``dotenv:/path/to/file``, ``vault:REMOTE_TASK_TOKEN``, ``none``.
    ``base_url`` is '' when unconfigured (callers degrade gracefully)."""
    base_url: str
    token: str = field(repr=False, default="")
    source: str = "none"


def parse_onboarding_token(raw: str) -> "tuple[str, str]":
    """Split an onboarding string into (url_from_token, secret).

    Case-insensitive scheme detection; splits at the first literal ``|``,
    falling back to the first ``%7C``/``%7c`` only when no literal pipe
    exists; both halves returned verbatim (never mutated). The literal pipe
    is preferred because a raw ``|`` cannot legally occur inside a URL — so
    when one is present it must be the separator, and a URL half that
    legitimately carries an encoded ``%7C`` stays intact (#2670 review). A
    bare secret — no leading scheme — is ('', raw) untouched even if it
    contains a separator. A scheme-prefixed value with no separator is
    ('', raw) too (the caller's URL-less guard speaks)."""
    if not raw.lower().startswith(("http://", "https://")):
        return "", raw
    i = raw.find("|")
    if i != -1:
        return raw[:i], raw[i + 1:]
    m = _ENCODED_SEPARATOR_RE.search(raw)
    if m is None:
        return "", raw
    return raw[: m.start()], raw[m.end():]


def normalize_gateway_url(url: str) -> str:
    """Canonical URL normalization: strip trailing slashes, nothing else."""
    return (url or "").rstrip("/")


def resolve_alias_precedence(values: Mapping[str, str],
                             names: Sequence[str]) -> "tuple[str, str]":
    """First non-empty value in canonical order → (value, winning_name);
    ('', '') when none. Pure: ``values`` is any mapping the CALLER built —
    never os.environ implicitly."""
    for name in names:
        v = values.get(name) or ""
        if v:
            return v, name
    return "", ""


def normalize_credentials(raw_token_value: str,
                          explicit_url: str = "",
                          fallback_url: str = "",
                          source: str = "none") -> GatewayCredentials:
    """Compose parse + URL chain into the value object. URL precedence:
    explicit (alias chain) > url-embedded-in-token > caller fallback (e.g. a
    device-env file's REMOTE_TASK_URL line). ``source`` must already be the
    qualified secret-free tag; it is kept only when a token resolved."""
    url_from_token, token = parse_onboarding_token(raw_token_value) if raw_token_value else ("", "")
    base = normalize_gateway_url(explicit_url or url_from_token or fallback_url)
    return GatewayCredentials(base_url=base, token=token,
                              source=source if token else "none")


# Masking, still pure: a SecretRegistry is an object its caller owns and fills with plain values;
# nothing here reads a credential source, keeps process-wide state, or writes anywhere.

WITHHELD = "<withheld>"
WITHHELD_TEXT = "<text withheld: it may contain a credential>"
REDACTED_URL = "<unparseable gateway URL>"
REDACTED_TEXT = "<text withheld: it may contain the gateway URL>"
BARE_SECRET_MIN = 8  # a bare URL piece shorter than this would withhold ordinary words
_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def fold(text: str) -> str:
    """The form secrets are matched in: case-folded. A token is case-sensitive as a credential,
    but an echo of it in another case still reveals it."""
    return str(text).casefold()


def _utf8(s: str) -> bytes:
    try:
        return s.encode("utf-8", "surrogateescape")  # a non-UTF-8 environment byte, as its byte
    except UnicodeEncodeError:
        return s.encode("utf-8", "backslashreplace")


def forms(secret: str) -> "set[str]":
    """The ways a secret can be rendered in diagnostic text: raw, escaped as repr and
    unicode_escape render it, and percent-encoded or -decoded. Total over any str."""
    out = set()
    for s in {secret, urllib.parse.unquote(secret), urllib.parse.unquote_plus(secret)}:
        raw = _utf8(s)
        out |= {s, repr(s)[1:-1], s.encode("unicode_escape").decode("ascii"),
                ascii(s)[1:-1], s.encode("ascii", "backslashreplace").decode("ascii"),
                urllib.parse.quote(raw, safe=""), urllib.parse.quote(raw, safe="/=&?#:@"),
                urllib.parse.quote_plus(raw, safe=""), urllib.parse.quote_plus(raw, safe="/=&?#:@")}
    return {f for f in out if f}


def _views(text: str) -> "set[str]":
    return {fold(v) for v in (text, urllib.parse.unquote(text), urllib.parse.unquote_plus(text))}


class SecretRegistry:
    """The secrets one process holds, each with every rendering of it, case-folded. A token is
    opaque and matched at any length; a bare URL piece passes min_len=BARE_SECRET_MIN."""

    def __init__(self) -> None:
        self._secrets: "dict[str, frozenset[str]]" = {}
        self._lock = threading.Lock()

    def register(self, value: Any, min_len: int = 1) -> None:
        if not isinstance(value, str) or not value or len(value) < min_len:
            return
        rendered = frozenset(fold(f) for f in forms(value))
        with self._lock:
            self._secrets[value] = rendered

    def register_gateway_url(self, url: str) -> None:
        """Every credential part of a gateway URL: each delimited part at any length, and the bare
        userinfo, username, password, query values and fragment when long enough alone."""
        url = str(url or "")
        for part in secret_parts(url):
            self.register(part)
        userinfo, query, fragment = _split_by_hand(url)
        username, _, password = userinfo.partition(":")
        for value in (userinfo, username, password, fragment,
                      *(pair.partition("=")[2] for pair in re.split(r"[&;]", query))):
            self.register(value, min_len=BARE_SECRET_MIN)

    def _rendered(self) -> "list[frozenset[str]]":
        with self._lock:
            return list(self._secrets.values())

    def contains(self, text: str) -> bool:
        """Whether any registered secret survives in `text`, in any rendering and letter case."""
        views = _views(str(text))
        return any(f in v for fs in self._rendered() for f in fs for v in views)

    def holds_or_is_piece(self, value: str) -> bool:
        """Whether `value` holds a registered secret or is itself a piece of one."""
        token = fold(value)
        return self.contains(value) or any(token in f for fs in self._rendered() for f in fs)

    def scrub(self, text: str) -> str:
        """`text`, or WITHHELD_TEXT when a registered secret survives in it."""
        return WITHHELD_TEXT if self.contains(text) else str(text)


def redact_url(value: str) -> str:
    """Scheme, host, port and path of a gateway URL: userinfo, query and fragment dropped. Fails
    closed: REDACTED_URL when it does not parse as one."""
    try:
        p = urllib.parse.urlsplit(str(value))
        if not p.scheme or not p.hostname:
            return REDACTED_URL
        host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
        if p.port:
            host = f"{host}:{p.port}"
        return urllib.parse.urlunsplit((p.scheme, host, p.path, "", ""))
    except Exception:  # noqa: BLE001 - redaction must never raise
        return REDACTED_URL


def _split_by_hand(url: str) -> "tuple[str, str, str]":
    before, _, fragment = url.partition("#")
    _, _, query = before.partition("?")
    userinfo = before.partition("://")[2].partition("/")[0].rpartition("@")[0]
    return userinfo, query, fragment


def secret_parts(url: str) -> "list[str]":
    """Each part of the URL that may carry a credential, with the delimiter the URL itself puts
    before it, so a part of any length is matched where URL text renders it. Split by hand as
    well as by urlsplit, which drops tab, CR and LF from the parts it returns."""
    splits = [_split_by_hand(url)]
    try:
        p = urllib.parse.urlsplit(url)
        splits.append((p.netloc.rpartition("@")[0], p.query, p.fragment))
    except Exception:  # noqa: BLE001 - the hand split stands alone
        pass
    parts = []
    for userinfo, query, fragment in splits:
        if userinfo:
            parts.append(userinfo + "@")
            if ":" in userinfo:
                parts.append(":" + userinfo.partition(":")[2] + "@")
        if query:
            parts.append("?" + query)
            for pair in re.split(r"[&;]", query):
                key, eq, value = pair.partition("=")
                parts += [pair, "=" + value] if eq else ["?" + pair, "&" + pair, ";" + pair]
        if fragment:
            parts.append("#" + fragment)
    return [p for p in dict.fromkeys(parts) if p not in ("=", "?", "&", ";", "#", "@", ":@")]


def _url_form_re(form: str) -> "re.Pattern[str]":
    """`form` with its scheme and host matched in any case, and the rest exactly."""
    scheme, sep, rest = form.partition("://")
    if not sep:
        return re.compile(re.escape(form))
    authority, slash, tail = rest.partition("/")
    userinfo, at, host = authority.rpartition("@")
    return re.compile(f"(?i:{re.escape(scheme + sep)})" + re.escape(userinfo + at)
                      + f"(?i:{re.escape(host)})" + re.escape(slash + tail))


def redact_text(text: str, gateway_url: str) -> str:
    """Diagnostic text with the full gateway URL replaced by its redacted form. Fails closed:
    REDACTED_TEXT when any credential-bearing part of the URL survives in any rendering."""
    text, url = str(text), str(gateway_url or "")
    if not url:
        return text
    for form in sorted(forms(url), key=len, reverse=True):
        text = _url_form_re(form).sub(lambda _m: redact_url(url), text)
    views = _views(text)
    for part in secret_parts(url):
        if any(fold(form) in view for form in forms(part) for view in views):
            return REDACTED_TEXT
    return text


def code_field(value: Any, registry: "SecretRegistry | None" = None, gateway_url: str = "") -> "str | None":
    """A gateway-supplied value usable as a structured field (an error name, an approval code):
    a short token of letters, digits and `_.-` that occurs in no form of the gateway URL, holds
    no registered secret and is no piece of one, in any letter case. None otherwise."""
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        return None
    url = str(gateway_url or "")
    url_forms = {url, urllib.parse.unquote(url), urllib.parse.unquote(urllib.parse.unquote(url))}
    if any(fold(value) in fold(form) for form in url_forms if form):
        return None
    return None if registry is not None and registry.holds_or_is_piece(value) else value


def safe_status(value: Any, net: Callable[[str], str]) -> Any:
    """`value` with every string value, at any depth, passed through `net`; keys through `net`
    too. Finite numbers, booleans and None pass. Anything else is withheld, never str()-ed."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else WITHHELD
    if isinstance(value, str):
        return net(value)
    if isinstance(value, dict):
        return {net(k) if isinstance(k, str) else WITHHELD: safe_status(v, net) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_status(v, net) for v in value]
    return WITHHELD


_EAI = {v: k for k, v in vars(socket).items() if k.startswith("EAI_") and isinstance(v, int)}


def _errno_name(exc: BaseException) -> str:
    code = getattr(exc, "errno", None) if isinstance(exc, OSError) else None
    if not isinstance(code, int):
        return ""
    # A resolver error carries an EAI_* code, not an errno.
    return _EAI.get(code, "") if isinstance(exc, socket.gaierror) else _errno.errorcode.get(code, "")


def describe(exc: BaseException) -> str:
    """`<Type>[(<reason type>/<ERRNO>)][ <ERRNO>][[<key>]][ (HTTP <n>)][ at <file>:<line>]`. Never
    the exception's own text: a KeyError's key only when the failing code wrote it as a string
    literal, a location only as the innermost frame's file name and line."""
    fields = getattr(exc, "__dict__", {})
    out = type(exc).__name__
    reason = fields.get("reason")
    if isinstance(reason, BaseException):
        name = _errno_name(reason)
        out += f"({type(reason).__name__}{'/' + name if name else ''})"
    elif _errno_name(exc):
        out += f" {_errno_name(exc)}"
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if isinstance(exc, KeyError) and len(exc.args) == 1 and isinstance(exc.args[0], str) \
            and tb is not None and exc.args[0] in tb.tb_frame.f_code.co_consts:
        out += f"[{exc.args[0]!r}]"
    status = fields.get("code")
    if isinstance(status, int) and not isinstance(status, bool):
        out += f" (HTTP {status})"
    if tb is not None:
        out += f" at {os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}"
    return out


_CREDENTIAL_NAME_RE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|AUTH", re.IGNORECASE)
_ASSIGNMENT_LINE_RE = re.compile(r"^(\s*)(export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def mask_assignment(key: str, value: str, registry: "SecretRegistry | None" = None) -> str:
    """The value of `key=value` as it may be shown: withheld for a credential-named key, an
    onboarding string or one holding a registered secret; a URL by its redacted form."""
    url, _secret = parse_onboarding_token(value)
    if _CREDENTIAL_NAME_RE.search(key) or url or (registry is not None and registry.contains(value)):
        return WITHHELD
    if "://" in value:
        return redact_url(value)
    return value


def mask_env_assignments(text: str, registry: "SecretRegistry | None" = None) -> str:
    """A unit file, .env or config as it may be shown (an install diff): every assignment on a
    systemd `Environment=` line (one or several, quoted or not) and every `KEY=value` line masked
    by mask_assignment; any other line withheld whole when it holds a registered secret."""
    out = []
    for line in text.splitlines(True):
        end = "\n" if line.endswith("\n") else ""
        body = line[:len(line) - len(end)]
        stripped = body.lstrip()
        indent = body[:len(body) - len(stripped)]
        if stripped.startswith("Environment="):
            try:
                words = shlex.split(stripped[len("Environment="):])
            except ValueError:
                out.append(f"{indent}Environment={WITHHELD}{end}")
                continue
            shown = []
            for word in words:
                key, eq, value = word.partition("=")
                item = f"{key}={mask_assignment(key, value, registry)}" if eq else WITHHELD
                shown.append(shlex.quote(item) if any(c.isspace() for c in item) else item)
            out.append(f"{indent}Environment={' '.join(shown)}{end}")
            continue
        m = _ASSIGNMENT_LINE_RE.match(body)
        if m:
            value = mask_assignment(m.group(3), parse_env_value(m.group(4)), registry)
            out.append(f"{m.group(1)}{m.group(2) or ''}{m.group(3)}={value}{end}")
            continue
        out.append((WITHHELD_TEXT if registry is not None and registry.contains(body) else body) + end)
    return "".join(out)


def parse_env_value(raw):
    """A credential value as every reader sees it: whitespace and surrounding quotes off (the
    one definition, so the environment cannot disagree with a file about a quoted line)."""
    return raw.strip().strip("'\"") if raw else raw

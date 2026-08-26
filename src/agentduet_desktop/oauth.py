"""Sign in, and hold the tokens that come back.

Replaces the one thing a human still had to issue by hand. First sign-in provisions a connector
server-side, so `connector_uuid` becomes an OUTPUT of logging in rather than a prerequisite
somebody must be given.

WHAT THIS MODULE IS RESPONSIBLE FOR, AND WHAT IT IS NOT

The SDK owns the reconnect loop, so it is the only thing that knows when a connect is about to
happen. It calls `token_provider()` before each attempt and sends the result as a Bearer
credential. It knows nothing about OAuth — string in, header out — and **the refresh token never
enters it**. So our side is a token store and a refresh clock, not a protocol implementation.

THE ENDPOINT IS NEVER HARDCODED. It is read from `$AGENTDUET_OAUTH_URL` at use time, like every
other credential in this package. That is not only the read-at-use-time rule: the integration
environment is an internal hostname, this repository is public, and a default would put it in the
source.

CONTRACT NOTES worth not re-deriving (wss-edge#52, as shipped 2026-08-25):

- **PKCE S256 only.** `plain` is rejected. There is no client secret — a desktop binary cannot
  hold one.
- **The redirect is stricter than "any loopback port".** Host must be literally `127.0.0.1` or
  `[::1]`; **`localhost` is rejected**. Path must be exactly `/callback`, with no query and no
  fragment. Any port is fine, which is what lets us use whichever one the site bound.
- **The refresh token rotates on every response.** Persist the new one before doing anything
  else: losing it signs the owner out. A retry with the just-replaced token is forgiven for 30
  seconds — that grace exists for a lost response, not for sloppiness — and older than that
  revokes the whole family.
- **A valid access token is reusable across any number of connects.** The handshake checks
  signature, expiry and scope only, so refresh is housekeeping before expiry rather than work
  before every connect.
- **`invalid_grant` on refresh means signed out.** Not retryable. Clear the store and show the
  sign-in screen.
- **Google only, today.** Microsoft is refused upstream because Entra does not issue
  `email_verified`, and the verified email is the identity-linking key — an unflagged one is an
  account-takeover path. Apple was never in v1. Both stay stubs in the UI.
"""

import json
import logging
import os
import secrets
import time
import urllib.parse

from . import paths

logger = logging.getLogger("dduet.oauth")

#: Our registered public client. One client, no secret.
CLIENT_ID = "agentduet-desktop"

#: The only provider that works today. See the module docstring.
PROVIDER = "google"

#: Fixed by the server's redirect validation — not a preference.
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PATH = "/callback"

#: Refresh when the access token has less than this left. The SDK may connect at any moment, and
#: handing it a credential that expires mid-handshake is the failure this margin exists to avoid.
REFRESH_MARGIN_SECONDS = 60

#: Long enough for a slow network, short enough that a connect attempt is not left hanging.
HTTP_TIMEOUT = 20

#: Where the tokens live. NOT `.env`: that is configuration a person edits, and this rotates on
#: every refresh. A file the owner might have open in an editor is the wrong home for something
#: rewritten several times an hour.
STORE = paths.RUN / "oauth.json"


class SignedOut(RuntimeError):
    """No usable credential, and refreshing cannot fix it.

    Raised from `token_provider`, which the SDK treats as "stop the reconnect loop" rather than
    "retry" — correct, because every cause is terminal until a human signs in again.
    """


def endpoint() -> str:
    """The authorization server, or "" when none is configured."""
    return (os.getenv("AGENTDUET_OAUTH_URL") or "").rstrip("/")


def available() -> bool:
    """Whether signing in is possible at all on this install."""
    return bool(endpoint())


# ---- the store ---------------------------------------------------------------------------

def _read() -> dict:
    try:
        return json.loads(STORE.read_text())
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    """Persist atomically, then tighten the mode.

    ATOMIC BECAUSE A TORN WRITE IS A SIGN-OUT. The refresh token rotates, so the copy on disk is
    the only way back; a half-written file loses it as surely as deleting it. Written to a
    neighbour and renamed, which is atomic on the same filesystem.
    """
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    try:
        tmp.chmod(0o600)          # no-op on Windows — see the checklist
    except OSError:
        pass
    tmp.replace(STORE)


def signed_in() -> bool:
    return bool(_read().get("refresh_token"))


def email() -> str:
    return _read().get("email", "")


def connector_uuid() -> str:
    """The connector this account owns. Re-echoed on every refresh, so never cached separately."""
    return _read().get("connector_uuid", "")


def sign_out() -> None:
    """Forget everything. Used on `invalid_grant`, and by the owner."""
    STORE.unlink(missing_ok=True)
    logger.info("signed out — token store cleared")


def _save(payload: dict) -> None:
    """Store a token response. Both grants return the same shape."""
    _write({
        "access_token": payload.get("access_token", ""),
        "refresh_token": payload.get("refresh_token", ""),
        "connector_uuid": payload.get("connector_uuid", ""),
        "email": payload.get("email", ""),
        # Absolute, not a duration: a duration is meaningless after a restart.
        "expires_at": time.time() + float(payload.get("expires_in") or 0),
    })


# ---- signing in --------------------------------------------------------------------------

def begin(port: int) -> tuple[str, str, str]:
    """Start a sign-in. Returns (authorize_url, state, code_verifier).

    The caller keeps `state` and `verifier` until the callback arrives — `state` to prove the
    redirect belongs to this attempt, `verifier` to prove we are the client that began it.
    """
    import base64
    import hashlib

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri(port),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "provider": PROVIDER,
    })
    return f"{endpoint()}/oauth/authorize?{query}", state, verifier


def redirect_uri(port: int) -> str:
    """Exactly what the server will accept. `localhost` here is rejected upstream."""
    return f"http://{REDIRECT_HOST}:{port}{REDIRECT_PATH}"


def _post_token(form: dict) -> dict:
    """POST the token endpoint and return the parsed body, or raise."""
    import httpx

    url = f"{endpoint()}/oauth/token"
    try:
        r = httpx.post(url, data=form, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        # A NETWORK FAULT IS NOT A SIGN-OUT. Leave the store alone so a later attempt can
        # succeed; only the server saying `invalid_grant` is terminal.
        raise RuntimeError(f"could not reach the sign-in service: {exc}") from None

    if r.status_code == 429:
        raise RuntimeError("the sign-in service is rate-limiting us; try again shortly")
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f"unreadable response from the sign-in service ({r.status_code})") from None
    if r.status_code >= 400:
        err = body.get("error", "")
        if err == "invalid_grant":
            raise SignedOut(body.get("error_description") or "the sign-in has expired")
        raise RuntimeError(body.get("error_description") or err or f"sign-in failed ({r.status_code})")
    return body


def complete(code: str, verifier: str, port: int) -> str:
    """Exchange the authorization code. Returns the email signed in as."""
    body = _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri(port),
    })
    _save(body)
    logger.info("signed in as %s; connector %s", body.get("email"), body.get("connector_uuid"))
    return body.get("email", "")


def refresh() -> str:
    """Swap the refresh token for a new access token. Returns the access token."""
    stored = _read()
    token = stored.get("refresh_token")
    if not token:
        raise SignedOut("not signed in")
    body = _post_token({
        "grant_type": "refresh_token",
        "refresh_token": token,
        "client_id": CLIENT_ID,
    })
    # SAVE BEFORE RETURNING. The server has already rotated; if we hand the caller a token and
    # then fail to persist, the refresh token on disk is the spent one.
    _save(body)
    return body.get("access_token", "")


# ---- what the SDK calls ------------------------------------------------------------------

def token_provider() -> str:
    """Return a Bearer credential, refreshing first if the cached one is nearly spent.

    The SDK calls this before EVERY connect attempt, so it must be cheap in the common case —
    which it is: a valid token is reusable across any number of connects, so this usually reads a
    file and returns.
    """
    stored = _read()
    if not stored.get("refresh_token"):
        raise SignedOut("not signed in")
    access = stored.get("access_token", "")
    if access and stored.get("expires_at", 0) - time.time() > REFRESH_MARGIN_SECONDS:
        return access
    return refresh()

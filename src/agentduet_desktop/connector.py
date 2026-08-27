"""The B3 connector — the credential that gives this install a phone number and a channel.

WHY IT IS NOT A TOOL THE ASSISTANT CAN CALL

Two secrets go in here. To hand a secret to the owner's assistant, the owner has to type it into
a chat box — and that message is sent to the model provider as part of the prompt AND written to
`run/owner_chat.json` in plaintext, where the chat panel renders it back. So credentials are
entered on a page and go straight to `$AGENTDUET_HOME/.env`; the assistant is told to say so rather
than offered a tool that invites the paste.

WHY VERIFY BEFORE SAVING

The same reason `attach_model` verifies: a saved-but-wrong credential produces the worst failure
— an install that looks configured and silently answers nothing. A wrong connector uuid fails at
runtime, hours later, as "no inbound messages ever arrive".

THE ONE-CLIENT CONSTRAINT MAKES VERIFYING AWKWARD

Checking a connector means connecting to it, and only one client may hold a connector at a time.
So we refuse to live-test the uuid the running daemon is already holding — there we save and
report that it takes effect on restart. A DIFFERENT uuid is safe to test, which is the case that
matters: the owner is adding or changing one.
"""

import asyncio
import logging
import os

logger = logging.getLogger("dduet.connector")

API_KEY = "AGENTDUET_API_KEY"
UUID = "AGENTDUET_CONNECTOR_UUID"

#: Where an OAuth sign-in would begin. UNSET today, on purpose.
#:
#: WHY THIS EXISTS BEFORE THE FEATURE DOES
#:
#: The connector is the last thing a human still has to issue by hand, and it is what stops a
#: stranger installing this unaided. OAuth is the fix: sign in, and first authorisation
#: provisions a connector and hands it back — the uuid becomes an OUTPUT of logging in rather
#: than a prerequisite someone must be given. That is requested but not built.
#:
#: So the installer is shaped for it now and reads this to decide. Unset — today — and step 3 is
#: exactly the manual form it always was, with no dead button promising a sign-in that cannot
#: happen. Set it, and sign-in becomes the primary path with "Enter key manually instead" beside
#: it. No redesign when the backend lands, and nothing misleading before it does.
OAUTH_URL = "AGENTDUET_OAUTH_URL"


def oauth_available() -> bool:
    """Whether a sign-in path exists to offer. False until an endpoint is configured."""
    from . import oauth
    return oauth.available()

#: The sample rate of call audio on this channel, for `CallAudioConfig`.
#:
#: IT LIVED IN `voice.py`, and the daemon read it on every channel open — so carrying a call, on
#: a product where no agent speaks, loaded the answering agent and everything behind it to learn
#: one integer. It describes what the leg delivers, which is a property of the channel.
#:
#: Qwen omni realtime emits 24 kHz (`output_audio_format="pcm24"`); the SDK default is 16 kHz,
#: which plays that audio 1.5x too slow and a fifth too low. Overridable because the right value
#: depends on the voice model AND on what the leg actually delivers, and getting it wrong is not
#: subtle — slow-and-deep audio one way, an ASR that transcribes nothing the other.
#: 8000 / 16000 / 24000 only.
CALL_SAMPLE_RATE = int(os.getenv("SECRETARY_CALL_SAMPLE_RATE", "24000"))

#: Long enough to fail a bad credential, short enough that a page is not left hanging.
VERIFY_TIMEOUT = 20


def environment() -> str:
    """Which backend this instance talks to, as something a person can compare.

    NOTHING RECORDED THIS, and two failure modes hid in the gap. Signing in against a
    non-production endpoint provisions a connector on THAT environment, and a signed-in install
    ignores AGENTDUET_CONNECTOR_UUID entirely — the connector is a claim inside the token. So a
    sandbox sign-in silently moves a production install off its own connector, its DID stops
    reaching it, and no error appears anywhere. The other mode is duller and just as annoying:
    an endpoint that was configured once, tested, and then not written down.

    Derived rather than stored, so it cannot drift from what the daemon is actually using.
    """
    from . import oauth
    url = oauth.endpoint()
    if not url:
        return "production (api key)" if os.getenv(API_KEY) else "not configured"
    host = url.split("://")[-1].split("/")[0]
    # Anything that is not the production host is worth naming in full: the whole point is to
    # notice when an instance is pointed somewhere unexpected.
    kind = "production" if host.startswith("wss-prod.") else host
    return f"{kind} (sign-in)" if oauth.signed_in() else f"{kind} (sign-in available)"


def configured() -> bool:
    """Can this install reach the platform at all — by either route.

    SIGNING IN COUNTS. It provisions the connector server-side and returns a rotating token, so a
    signed-in install has no `AGENTDUET_API_KEY` and no `AGENTDUET_CONNECTOR_UUID` and is
    nonetheless fully configured. Checking only the two environment variables would have shown
    such an owner a setup page they had already finished.
    """
    from . import oauth
    if oauth.signed_in():
        return True
    return bool(os.getenv(API_KEY) and os.getenv(UUID))


def in_use(connector_uuid: str) -> bool:
    """True if the running daemon already holds this connector."""
    return bool(connector_uuid) and os.getenv(UUID) == connector_uuid


async def verify(api_key: str, connector_uuid: str) -> tuple[bool, str]:
    """Connect once and disconnect. (ok, message)."""
    try:
        from agentduet import SessionManager, SessionManagerConfig
    except ImportError as exc:
        return False, f"the SDK is not available ({exc})"

    cfg = SessionManagerConfig(api_key=api_key, connector_uuid=connector_uuid)
    try:
        async with asyncio.timeout(VERIFY_TIMEOUT):
            async with SessionManager(cfg) as sm:
                # Reaching here means the credential authenticated and the socket opened.
                return True, f"connected (client {getattr(sm, 'id', '?')})"
    except TimeoutError:
        return False, ("no response within "
                       f"{VERIFY_TIMEOUT}s — check the connector uuid and your network")
    except Exception as exc:
        # Authentication failures surface as SDK exceptions; report the type, not a stack.
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"

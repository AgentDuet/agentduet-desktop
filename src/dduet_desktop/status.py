"""What the DDUET side is actually doing, for the owner's header.

WHY THIS EXISTS

`/api/state` reported the queue and nothing about the channel, so the owner's view could not
distinguish "nobody has written to you" from "you are not connected to anything". Those look
identical in a dashboard of empty columns, and the second one is the owner's problem to fix.

WHY IT IS IN MEMORY AND NOT A FILE

The daemon and the owner site run in ONE process — `main()` starts the site, then the channel, on
the same loop. A module-level dict is therefore always current and cannot go stale the way a
`run/channel.json` written on transition would if the process died between write and read.

THE NUMBER IS LEARNED FROM CALLS ONLY — AND DDUET HAS NO NUMBER AT ALL

The SDK never tells a client which numbers its connector holds, and there is no lookup to ask.
A number can therefore only be observed on an inbound event — but only a CALL carries one:
`subscriber` on a call is the line it runs on, whereas on a DDUET message `subscriber` is the
CONNECTOR UUID (see the SDK's dduet_echo_bot). DDUET is Nexus web chat; its participants are
identified by email and no phone number exists in the conversation.

So: no number until a real call arrives on a DID attached to this connector. `set_number` enforces
that shape, because displaying a uuid under the word "number" is worse than displaying nothing.
"""

import json
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger("dduet.status")

#: Where a call-learned number is kept. Imported lazily-ish via paths so a throwaway
#: $DDUET_HOME gets its own.
from . import paths
NUMBER_FILE = paths.RUN / "channel-number"

#: unset → no connector credentials at all (the ordinary state of a fresh install)
#: off → deliberately disabled with SECRETARY_CHANNEL=0
#: connecting → credentials present, first attempt in flight
#: live → connected; inbound is being served
#: retrying → was configured, connection failed, backing off
_state = {
    "channel": "unset",
    "voice": False,
    "number": "",
    "detail": "",
    "since": "",
}


def set_channel(channel: str, detail: str = "") -> None:
    if _state["channel"] != channel:
        _state["since"] = datetime.now().isoformat(timespec="seconds")
    _state["channel"] = channel
    _state["detail"] = detail


def set_voice(on: bool) -> None:
    _state["voice"] = bool(on)


#: E.164-ish. Guards against a caller identity that is not a number at all — on DDUET the
#: subscriber is the connector uuid, and rendering that as "your number" is worse than
#: rendering nothing.
_E164 = re.compile(r"^\+?[0-9][0-9 ()-]{5,}$")


def set_number(number: str) -> None:
    """Learned from an inbound CALL. First one wins for display purposes.

    Persisted because it is only observable while a call is happening: calls do not write the
    sessions store (that is DDUET's reply path), so without this the number vanished on every
    restart and only came back if someone rang again.
    """
    number = (number or "").strip()
    if number and not _state["number"] and _E164.match(number):
        _state["number"] = number
        try:
            NUMBER_FILE.parent.mkdir(parents=True, exist_ok=True)
            NUMBER_FILE.write_text(number)
        except OSError as exc:
            logger.debug("could not persist the number: %s", exc)


def load_number(sessions_file) -> None:
    """Recover the last known number after a restart, before any new inbound arrives.

    Two sources, because two channels learn it differently: the file a CALL wrote, and the
    sessions store DDUET writes. The store holds DDUET subscribers too (connector uuids);
    `set_number`'s shape check is what keeps those out, so this can read it without knowing
    which channel wrote each row.
    """
    try:
        if NUMBER_FILE.exists():
            set_number(NUMBER_FILE.read_text().strip())
            if _state["number"]:
                return
    except OSError as exc:
        logger.debug("could not read the stored number: %s", exc)
    try:
        if not sessions_file.exists():
            return
        data = json.loads(sessions_file.read_text())
        for entry in sorted(data.values(), key=lambda e: e.get("last_seen", ""), reverse=True):
            if entry.get("subscriber"):
                _state["number"] = entry["subscriber"]
                return
    except (OSError, ValueError) as exc:
        logger.debug("could not recover the number from sessions: %s", exc)


def snapshot() -> dict:
    """What the header renders. `configured` is about credentials, `channel` about the socket —
    the owner needs to be pointed at the connector page in one case and at their network in the
    other."""
    return dict(
        _state,
        configured=bool(os.getenv("AGENTDUET_API_KEY") and os.getenv("AGENTDUET_CONNECTOR_UUID")),
    )

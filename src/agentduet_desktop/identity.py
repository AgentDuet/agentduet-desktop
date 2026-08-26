"""Who is asking — one place that decides what an identity IS.

THE MODEL (decided 2026-07-29, DDUET backend side):

    The CHANNEL issues the identity and marks it verified or unverified.
    A message carries an identity reference. There is no per-message verification flag.

A logged-in person gets their real identity (`+6591234567` = Pauline, verified). A walk-up
visitor gets a transient identity of the channel's making, unverified.

WHY THIS IS STRONGER THAN LABELLING MESSAGES

The sender never chooses their identity, so an unverified visitor typing "I am Pauline" is
just text in a message — not a claim on her identity, and nothing that can be filed against
her. That property is what this module exists to guarantee locally.

An earlier version of the design put the flag on the message, reasoning that a durable flag
would let a later unverified claim inherit an earlier verification. That only holds if the
SENDER supplies the identity — a weakness of this POC's simulator, not a requirement of the
channel. With the channel issuing identities there is nothing to inherit.

WHAT THIS FIXES CONCRETELY

The owner's view showed "13 open" against Pauline: one ask from the real Pauline and twelve
from senders who merely typed her number. Verification had specifically NOT established any
relationship between those twelve and her, yet they were counted under her name — which also
means an unverified sender could inflate a real contact's badge and misdirect the owner.

STILL OPEN (deliberately — see my-agenda.md; do not resolve these here)

- **Transient identity lifetime.** Per session, or stable across visits? Per-session means an
  unverified visitor has no history and can hold no grants. Stable makes it a soft identity
  that can be stolen.
- **Upgrade path.** When a visitor logs in mid-conversation they were a visitor and are now
  Pauline. Link/merge, or switch? Without a merge signal the earlier turns are orphaned;
  with a naive merge, a session that began anonymous absorbs verified history and grants.

Until the channel issues transient ids, an unverified sender is namespaced by what they
claimed, so it can never collide with the verified identity of the same name. When DDUET
starts issuing ids, `claimed` simply becomes that id and the prefix falls away.
"""
from __future__ import annotations

#: Namespace for identities the channel has not verified. Chosen so it cannot occur in a
#: real address or phone number, and so `verified` is derivable from the id alone — the
#: whole point being that the two can never disagree.
VISITOR = "visitor:"


def resolve(claimed: str, verified: bool | None) -> tuple[str, bool]:
    """(identity_id, verified) for one inbound message.

    `claimed` is whatever the channel handed us. `verified` is the channel's assertion about
    it. Returns the identity the agent should use everywhere — logging, threads, memory,
    permissions — so that no caller can pair a verified flag with the wrong identity.
    """
    who = (claimed or "").strip()
    if verified:
        return who, True
    return (VISITOR + who if who else VISITOR + "anonymous"), False


def is_verified(identity_id: str) -> bool:
    """Verification is a property OF THE IDENTITY, so it is readable from the id."""
    return not (identity_id or "").startswith(VISITOR)


def claimed_by(identity_id: str) -> str:
    """What an unverified visitor claimed to be, or "" — for display only.

    Never use this to look anything up. It is the sender's assertion about themselves, which
    is exactly the thing that has not been established.
    """
    return identity_id[len(VISITOR):] if (identity_id or "").startswith(VISITOR) else ""


def display(identity_id: str) -> str:
    """How to name this identity to the owner, without implying a relationship."""
    claim = claimed_by(identity_id)
    if not claim:
        return identity_id
    return f"unverified visitor (claims {claim})" if claim != "anonymous" \
        else "unverified visitor"

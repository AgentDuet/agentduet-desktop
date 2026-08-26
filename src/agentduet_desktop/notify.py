"""Escalation to the owner.

DDUET is passive — there is no way to *send* the owner a message on it. But this agent
runs on the owner's own machine, so a desktop notification is both the simplest and the
most appropriate channel. Falls back to stdout when notify-send is unavailable.
"""
from __future__ import annotations


import shutil
import subprocess


def escalate_to_owner(asker: str, question: str, reason: str) -> None:
    title = f"Secretary: needs you ({reason.removeprefix('policy:')})"
    body = f"{asker}\n{question}"

    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "--urgency=normal", "--app-name=Secretary", title, body],
            check=False,
        )
    print(f"\n  ⚠ ESCALATED [{reason}] {asker}: {question}\n")

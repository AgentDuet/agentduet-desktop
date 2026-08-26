"""Run the owner site + channel simulator WITHOUT the carrier.

The daemon needs a live connector; the decision core does not. This starts just the web
faces so you can exercise `brain.handle_query()` — the real path — before DDUET inbound
exists. No AgentDuet credentials required, no connector claimed (so it never clashes
with the bank demo's "one client per connector" rule).

    SECRETARY_SIM=1 .venv/bin/python run_sim.py

Real daemon (needs the connector): ./start.sh
"""
from __future__ import annotations


import asyncio
import logging
import os

try:
    from dotenv import load_dotenv
    load_dotenv(paths.ENV_FILE)   # instance, never a CWD .env — see secretary_agent.py
except ImportError:
    pass
os.environ.setdefault("SECRETARY_SIM", "1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

from . import web  # noqa: E402  (after load_dotenv, so the model key is visible)


async def _warm() -> None:
    """Index granted folders up front, so the first question isn't the slowest."""
    from . import folder_index, people, permissions
    folders = set(permissions.load().get("default", {}).get("folders", []))
    for cfg in permissions.load().get("askers", {}).values():
        folders.update(cfg.get("folders", []))
    for who in people.list_profiles():
        folders.update(people.folders_for(who, True))
    built = await asyncio.to_thread(folder_index.warm, sorted(folders))
    for b in built:
        logging.info("indexed %s — %d files, %d chunks%s", b["folder"],
                     b["file_count"], b["chunk_count"],
                     f" ({b['reread']} re-read)" if b["reread"] else " (unchanged)")


async def main() -> None:
    url = await web.start()
    asyncio.create_task(_warm())
    token = url.split("t=")[-1]
    print(f"\n  owner view : {url}")
    print(f"  simulator  : http://127.0.0.1:{web.PORT}/sim?t={token}")
    print("  (no carrier connection — brain only)\n")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

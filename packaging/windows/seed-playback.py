"""Populate the Windows CI disposable instance with a configured, offline Recorder library."""
import math
import os
import struct
from pathlib import Path
import wave

from datetime import datetime, timezone

from agentduet_desktop import __version__, calls, carry, names, paths, settings, update

if paths.HOME != Path(os.environ["RUNNER_TEMP"]) / "Recorder smoke":
    raise SystemExit("Expected the disposable Windows workflow instance")
settings.save_connector("fixture-local-VSC0", "00000000-0000-4000-8000-000000000032")
settings.set_setting("name", "Alex Morgan")
settings.set_setting("phone", "+12025550123")
folder = carry.recordings()
folder.mkdir(parents=True, exist_ok=True)
# A successful prior check, so About exercises cached equal-version rendering offline.
update._save({"platform": "windows", "current": __version__, "version": __version__,
              "checked": datetime.now(timezone.utc).isoformat(), "reachable": True, "newer": False})
name = "20261003T100000-ci-call.wav"
with wave.open(str(folder / name), "wb") as output:
    output.setparams((2, 2, 24000, 0, "NONE", "uncompressed"))
    second = b"".join(struct.pack("<hh", *(int(2500 * math.sin(2 * math.pi * hz * i / 24000))
                                          for hz in (330, 440))) for i in range(24000))
    output.writeframes(second * 24)
names.set_typed("+12025550123", "Alex Morgan")
calls.record("ci-missed", "+12025550123", "carried", note="missed")
calls.record("ci-call", "+12025550123", "carried", recordings=[name])

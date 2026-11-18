"""Populate the Windows CI disposable instance with a configured, offline Recorder library."""
import math
import os
import struct
from pathlib import Path
import wave

from agentduet_desktop import calls, carry, paths, settings

if paths.HOME != Path(os.environ["RUNNER_TEMP"]) / "Recorder smoke":
    raise SystemExit("Expected the disposable Windows workflow instance")
settings.save_connector("fixture-local-only", "00000000-0000-4000-8000-000000000032")
folder = carry.recordings()
folder.mkdir(parents=True, exist_ok=True)
name = "20261003T100000-ci-call.wav"
with wave.open(str(folder / name), "wb") as output:
    output.setparams((2, 2, 24000, 0, "NONE", "uncompressed"))
    second = b"".join(struct.pack("<hh", *(int(2500 * math.sin(2 * math.pi * hz * i / 24000))
                                          for hz in (330, 440))) for i in range(24000))
    output.writeframes(second * 24)
calls.record("ci-call", "+12025550123", "carried", recordings=[name])

"""Populate the Windows CI disposable instance with a configured, offline Recorder library."""
import os
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
    output.writeframes(bytes(4 * 24000 * 8))
calls.record("ci-call", "+12025550123", "carried", recordings=[name])

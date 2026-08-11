"""Frozen-binary entry point.

PyInstaller needs a real script, not a console-script name from pyproject.toml — the entry
points metadata does not exist inside a frozen bundle.
"""

import sys

from agentduet_desktop.cli import main

if __name__ == "__main__":
    sys.exit(main())

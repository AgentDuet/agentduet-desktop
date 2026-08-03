"""DDuet Desktop — a personal secretary that answers external parties on your behalf.

The framework. Everything the owner can change lives in $DDUET_HOME (default ~/.dduet),
seeded once from `templates/`; working capabilities to copy are in `examples/`.
"""

__version__ = "0.1.0a2"

#: Set at build time by the PyInstaller spec so a bug report identifies the exact build. An
#: alpha moves faster than its version number: "0.1.0a2" is true of a dozen different binaries,
#: and the first question about any report is which one.
try:
    from ._build import COMMIT as __commit__          # type: ignore
except ImportError:
    __commit__ = ""


def version_string() -> str:
    """What to print, and what a bug report should quote."""
    import sys
    where = "installed" if getattr(sys, "frozen", False) else "from source"
    return f"{__version__}" + (f" ({__commit__})" if __commit__ else "") + f" — {where}"

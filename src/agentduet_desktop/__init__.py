"""AgentDuet Desktop — a personal secretary that answers external parties on your behalf.

The framework. Everything the owner can change lives in $AGENTDUET_HOME (default ~/.dduet),
seeded once from `templates/`; working capabilities to copy are in `examples/`.
"""

__version__ = "0.1.0a7"

#: Set at build time by the PyInstaller spec so a bug report identifies the exact build. An
#: alpha moves faster than its version number: "0.1.0a2" is true of a dozen different binaries,
#: and the first question about any report is which one.
try:
    from ._build import BUILT as __built__            # type: ignore
    from ._build import COMMIT as __commit__          # type: ignore
except ImportError:
    __commit__ = __built__ = ""


def build_id() -> str:
    """A string that names THIS binary and no other, safe to use as a directory name.

    WHY THE INSTALL LAYOUT NEEDS THIS AND NOT JUST __version__

    `versions/0.1.0a2` is keyed on the version alone, so every build of an alpha is the same
    directory. Consequences, all observed on 2026-08-03: a new build silently overwrote the
    installed one, `is_installed()` answered "yes" about a binary three commits stale so the
    installer showed its own step as complete, and rollback between two 0.1.0a2 builds was not
    expressible at all. Keying on the build makes each one a distinct, orderable directory.

    Falls back to the bare version from source, where there is no build to identify.
    """
    if not __commit__:
        return __version__
    # The commit answers "which code", the timestamp answers "which build, and which is newer".
    # One '+' separates version from build; everything after it is dot-separated, so a dirty
    # tree reads as `0.1.0a2+e3a70f0.dirty.2026...` rather than sprouting a second '+'.
    return (f"{__version__}+{__commit__.replace('+', '.')}"
            + (f".{__built__}" if __built__ else ""))


def version_string() -> str:
    """What to print, and what a bug report should quote."""
    import sys
    # "binary", not "installed": frozen says how it was built, not where it lives. Whether it
    # is installed in the right place is a different question, and `status` is what answers it.
    where = "binary" if getattr(sys, "frozen", False) else "from source"
    stamp = f" ({__commit__}{' ' + __built__ if __built__ else ''})" if __commit__ else ""
    return f"{__version__}{stamp} — {where}"

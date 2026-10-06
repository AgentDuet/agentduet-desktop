#!/bin/sh
# THE PINNED BUILD SET (2026-10-06): requirements.txt, compiled from packaging/requirements.in for
# the machine the release is built on — macOS on Apple silicon, Python 3.12.
#
# WHY. pyproject.toml says ranges ("aiohttp>=3.9"), so every build installed whatever was newest
# that day: two builds of one commit could ship different libraries, and GitHub's dependency graph
# saw only those ranges — no versions, nothing indirect, none of the AI runtimes in the extras —
# so Dependabot had nothing to check. A requirements.txt is what the graph reads in full.
#
# HOW IT IS USED: as pip CONSTRAINTS (build.yml sets PIP_CONSTRAINT), not as a list to install.
# A constraint fixes a version without installing the package, so the recorder still installs
# no AI at all while every edition gets the same pinned versions.
#
# Run this after changing a dependency, or to take updates; tests/test_rules.py checks that the
# lock still covers pyproject.toml. Dependabot opens PRs against requirements.txt for security fixes.
set -eu
cd "$(dirname "$0")/.."
# RESOLVED FOR THIS MACHINE — run it on a Mac with Apple silicon — as pip does on the build runner.
# Not `--python-platform aarch64-apple-darwin`: uv then assumes macOS 13 and quietly prefers older
# wheels (onnxruntime 1.23 for the 1.30 b8 shipped, whose only Apple-silicon wheel needs 14), and
# this uv does not read MACOSX_DEPLOYMENT_TARGET when choosing. The floor is enforced on the built
# bundle instead (packaging/check-minos.py): a wheel needing a newer macOS fails the build by name.
[ "$(uname -s)-$(uname -m)" = "Darwin-arm64" ] || { echo "run this on a Mac with Apple silicon" >&2; exit 1; }
uv pip compile packaging/requirements.in -o requirements.txt \
  --python-version 3.12 \
  --prerelease if-necessary-or-explicit \
  --no-emit-package agentduet-desktop \
  --custom-compile-command "packaging/lock.sh" "$@"
# VERSIONS ALREADY IN requirements.txt ARE KEPT unless something forces a change, so one new
# dependency does not upgrade everything: `packaging/lock.sh --upgrade` takes every update,
# `--upgrade-package aiohttp` one.

#!/bin/bash
# Build llama.cpp's own `llama-server`, the engine the local models run in (2026-10-09).
#
#   packaging/build-llama-server.sh <out-dir>        # writes <out-dir>/llama-server
#
# WHY A SERVER, NOT llama-cpp-python. That package is one maintainer, and its releases trail
# llama.cpp by weeks: EmbeddingGemma 2 reached llama.cpp on 2026-10-06 and no release could load
# it. `llama-server` is maintained by the llama.cpp team itself, so a model works the day it lands.
# Moving piece by piece; search was first (`llamaserver.py`).
#
# PINNED TO ONE COMMIT, so two builds of one tag run the same engine. Bump it deliberately, and
# re-run the search checks in tests/test_rules.py against the new binary.
#
# NO TLS: it only ever talks to this app, over loopback — and a Homebrew OpenSSL found on the
# build machine would be linked by path and missing on every other Mac. NO WEB UI either.
#
# ONE STATIC FILE: no libllama/libggml beside it to collide with llama-cpp-python's copies in the
# same bundle, and the Metal shaders compiled in rather than read from disk.
set -euo pipefail

LLAMA_CPP_COMMIT="de7fa0a3c6a2e1b4cd9f22eb8d6bf5b12dbdb63b"   # master, 2026-10-08; has #30054
OUT="${1:?usage: build-llama-server.sh <out-dir>}"
MIN_MACOS="${MACOSX_DEPLOYMENT_TARGET:-14.0}"

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
SRC="${LLAMA_CPP_SRC:-$(mktemp -d)/llama.cpp}"

if [ ! -d "$SRC/.git" ]; then
  git init -q "$SRC"
  git -C "$SRC" remote add origin https://github.com/ggml-org/llama.cpp.git
fi
git -C "$SRC" fetch -q --depth 1 origin "$LLAMA_CPP_COMMIT"
git -C "$SRC" checkout -q --force FETCH_HEAD

cmake -S "$SRC" -B "$SRC/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_DEPLOYMENT_TARGET="$MIN_MACOS" \
  -DBUILD_SHARED_LIBS=OFF \
  -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON \
  -DGGML_NATIVE=OFF \
  -DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
  -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF \
  > "$OUT/llama-server.build.log" 2>&1
cmake --build "$SRC/build" --config Release --target llama-server -j "$(sysctl -n hw.ncpu 2>/dev/null || nproc)" \
  >> "$OUT/llama-server.build.log" 2>&1

cp "$SRC/build/bin/llama-server" "$OUT/llama-server"
# ITS LICENCES TRAVEL WITH IT: llama.cpp is MIT, and the server compiles in vendored code that
# carries its own notices (licenses/). build.yml appends this to THIRD-PARTY-NOTICES.txt.
{
  echo "llama.cpp ($LLAMA_CPP_COMMIT), built into Contents/MacOS/llama-server"
  echo; cat "$SRC/LICENSE"
  for f in "$SRC"/licenses/*; do [ -f "$f" ] && { echo; echo "---- $(basename "$f")"; cat "$f"; }; done
} > "$OUT/llama-server.LICENSES.txt"
echo "$LLAMA_CPP_COMMIT" > "$OUT/llama-server.commit"
echo "$OUT/llama-server (llama.cpp $LLAMA_CPP_COMMIT)"

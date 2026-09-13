# Why this font is here

`material-symbols-rounded.woff2` is Material Symbols Rounded, **subset to the fifteen icons the
pages use**. 22 KB; the full variable font is about 3.7 MB.

It used to be loaded from Google Fonts. The failure mode is loud rather than graceful: with no
font, each icon renders as its own **ligature name**, so on 2026-09-13 — after a reboot, with the
app starting before the network was up — the sidebar read `smart_toy Personal Assistant` and the
titlebar `settings Settings`. Offline is most of what this product claims, so the file belongs in
the binary.

## Regenerating it, when the icon list changes

Adding a sixteenth icon to any page means refetching, or the new one renders as its name while
every other icon is fine — which is a confusing way to discover this file exists.

```bash
ICONS=$(grep -ho 'material-symbols-rounded">[a-z_]*' src/agentduet_desktop/*.html \
        | sed 's/.*>//' | sort -u | paste -sd, -)
CSS=$(curl -s -A 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 \
(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36' \
  "https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200&icon_names=$ICONS")
curl -s "$(echo "$CSS" | grep -o 'https://fonts.gstatic.com/[^)]*')" \
  -o src/agentduet_desktop/fonts/material-symbols-rounded.woff2
echo "$ICONS" | tr ',' '\n' > src/agentduet_desktop/fonts/icons.txt   # what was fetched
```

**Send a browser user-agent.** Google serves `ttf` to an unrecognised agent and `woff2` to a
modern browser, and the `@font-face` in `app.css` declares `format("woff2")` — so a `ttf` fetched
by a bare `curl` is silently the wrong file.

## How the test checks it, and what it cannot check

`icons.txt` records the list this font was fetched FOR, and `tests/test_rules.py` compares the
pages against it — so adding an icon without refetching fails the suite, naming the icon.

It does not read the font itself. A woff2 is Brotli-compressed and needs `fontTools`, and the
suite deliberately runs with no venv and no third-party imports. So the sidecar is the record,
and the one thing that defeats it is editing `icons.txt` by hand without refetching — which is a
deliberate act rather than the oversight this guards against. The test does check the file really
is woff2 (`wOF2` magic), which catches the user-agent trap above.

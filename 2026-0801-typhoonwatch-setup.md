# 2026-0801 · 风眼 TYPHOONWATCH — setup

Two halves that never touch each other directly:

```
GitHub Actions runner (US)        your repo               phone / GitHub Pages
  metoc.navy.mil  ──curl──►  data/jtwc.json  ──same-origin fetch──►  index.html
  (no CORS, blocks bots)     data/graphics/*.gif                     (offline via sw.js)
```

The runner does the fetching because a browser cannot: `metoc.navy.mil` sends no
CORS headers and blocked my own request from this container with bot detection.
Once the data sits in your repo, the page is same-origin and everything works,
including offline.

## Files

| Path | What it does |
|---|---|
| `.github/workflows/jtwc-watch.yml` | Runs every 3 h at :45, plus a manual button |
| `scripts/fetch_jtwc.py` | Fetches, parses, writes `data/jtwc.json`, mirrors the `.gif` |
| `index.html` | The PWA — single file, bilingual, zh default |
| `sw.js` | Network-first shell + data, stale-while-revalidate for the rest |
| `manifest.json`, `icon.svg` | Install-to-homescreen |
| `data/jtwc.sample.json` | Demo payload for offline UI work (`?demo=1`) |

## First run

1. Create the repo `typhoonwatch`, push these files.
2. **Settings → Actions → General → Workflow permissions → Read and write.**
   Without this the commit step fails with a 403.
3. **Settings → Pages → Deploy from branch → `main` / root.**
4. **Actions → JTWC watch → Run workflow.** Watch the log: it prints the storm
   ids it found and every error it hit.
5. Open `https://<you>.github.io/typhoonwatch/`.

Quiet period with no active storms is the normal case — the page will say so
rather than showing an empty screen.

## Local testing

```bash
python scripts/fetch_jtwc.py --selftest    # 25 parser assertions, no network
python scripts/fetch_jtwc.py --fixture     # writes data/jtwc.sample.json
```

Then open `index.html?demo=1` from `file://` to see a rendered storm without
waiting for a real typhoon. The demo banner is amber and says so — the app
never treats `demo:true` data as real.

## What I could not verify

I have no network path to `metoc.navy.mil` from here, so **the parser is tested
against fixtures written from JTWC's published bulletin format, not against a
live bulletin.** The first real run is the real test. If a bulletin does not
parse, the app shows the storm with `parsed:false`, lists the problems, and
still shows the raw text — it will never invent a position. Send me the raw
text from `data/jtwc.json` and I will fix the regex.

Filenames most likely to need correction after the first run:

- `<id>prog.txt` for the prognostic reasoning — the suffix has changed before.
- `abpwsair.txt` / `abiosair.txt` for the significant tropical weather advisory.

Both fail soft: the app just omits that section.

## Release checklist

- `python scripts/fetch_jtwc.py --selftest` green
- `node --check` on the extracted script
- `VERSION` in `index.html` and `CACHE_VERSION` in `sw.js` bumped and matching
- ship `index.html`, `sw.js`, `manifest.json`, `icon.svg`

## Honest limits

- JTWC products are produced for US government agencies. This mirror is a
  reader, not a warning service.
- For official Malaysian warnings, MET Malaysia remains the authority.
- The distance readout is spherical geometry against a fixed town coordinate.
  It is not a forecast for that town.

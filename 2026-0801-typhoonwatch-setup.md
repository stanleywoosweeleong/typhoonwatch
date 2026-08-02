# 2026-0801 · 风眼 TYPHOONWATCH — setup

```
GitHub Actions runner        your repo            phone / GitHub Pages
  jma.go.jp  ──fetch──►  data/typhoon.json  ──same-origin──►  index.html
                                                              (offline via sw.js)
```

## Why JMA and not JTWC

`metoc.navy.mil` returns **HTTP 403 to every request from a GitHub Actions
runner** — verified 2026-08-01 with urllib and curl, browser headers, both
JTWC hosts, and the NRL mirror. It is an IP-range policy, not a client
problem, so no header or TLS trick fixes it. A Cloudflare Worker would sit on
datacenter IPs too. NOAA's ATCF directory carries only Atlantic and Pacific
files from NHC — no `bwp`/`bio`/`bsh`.

JMA is the better source anyway: it is the WMO-designated Regional Specialized
Meteorological Centre for the western North Pacific — the official authority
for this basin — and it publishes 10-minute sustained winds, the same
convention MET Malaysia uses. It also publishes the 予報円 (forecast circle),
which JTWC's GIF only draws and never gives as data.

Probe result, for the record:

| Source | Reachable |
|---|---|
| metoc.navy.mil (JTWC, both hosts) | 403 |
| nrlmry.navy.mil | 403 |
| jma.go.jp | OK |
| gdacs.org | OK |
| ftp.nhc.noaa.gov | OK (wrong basins) |
| agora.ex.nii.ac.jp (Digital Typhoon) | OK |

## Files

| Path | What it does |
|---|---|
| `.github/workflows/typhoon-watch.yml` | Runs every 3 h at :45, plus a manual button |
| `scripts/fetch_typhoon.py` | Fetches JMA, normalises, writes `data/typhoon.json` |
| `index.html` | The PWA — single file, bilingual, zh default |
| `sw.js`, `manifest.json`, `icon.svg` | Offline shell |
| `data/typhoon.sample.json` | Demo payload (`?demo=1`) |

Delete the old `scripts/fetch_jtwc.py`, `.github/workflows/jtwc-watch.yml`
and `data/jtwc*.json` when you push — they are dead.

## JMA endpoints used

- `https://www.jma.go.jp/bosai/typhoon/data/targetTc.json` — active TC list
- `https://www.jma.go.jp/bosai/typhoon/data/{tcCode}/specifications.json` — per storm

These are the site's own internal JSON, not a contracted API. They can change
without notice. The fetcher fails soft and the app shows staleness loudly.

## Local testing

```bash
python scripts/fetch_typhoon.py --selftest   # 31 assertions, no network
python scripts/fetch_typhoon.py --fixture    # writes data/typhoon.sample.json
python scripts/fetch_typhoon.py --probe      # check JMA + GDACS reachability
```

Then open `index.html?demo=1` from `file://`.

## Release checklist

- `python scripts/fetch_typhoon.py --selftest` green
- `node --check` on the extracted script and on `sw.js`
- `VERSION` in `index.html` and `CACHE_VERSION` in `sw.js` matching
- ship `index.html`, `sw.js`, `manifest.json`, `icon.svg`

## Map data

The coastline embedded in `index.html` is Natural Earth 1:50m via the
`world-atlas` package, clipped to 92-172E / 12S-48N and simplified with
Douglas-Peucker at 0.09 deg. Verified against known coastal cities: Manila
7 km, Hong Kong 11 km, Tokyo 16 km. Guam, Saipan, Okinawa and Palau are kept
deliberately as typhoon landmarks. About 46 KB of the file. It is a schematic
outline for orientation, not a navigation chart.

## Reference places

18 points in two groups. Malaysia (7) drives the monsoon-surge footnote;
Region (11) covers where WNP tracks actually end — Da Nang, Haikou, Sanya,
Guangzhou, Hong Kong, Manila, Kaohsiung, Taipei, Naha, Kagoshima, Tokyo. All
are inside JMA's area of responsibility and inside the region map frame.
Non-Malaysian selections get a neutral footnote instead of the monsoon text.

To add one: append to `REFS` in `index.html` with `grp`, `zh`, `en`, `lat`,
`lon`. The test harness checks every point falls inside `REGION` and within
120 km of a coastline vertex, which catches transposed lat/lon.

## Pressure layer (v2.4.0)

Two products from Open-Meteo (ECMWF IFS), both plotted as the published field:

- **Places** — MSLP and 24 h change at every reference point, every run (18 pts).
- **Grid** — an MSLP field for isobars, only on UTC hours divisible by 6
  (598 pts over 100-122E / 3S-22N, **1 deg** spacing, 12 chunked calls).
  The box reaches 22N to include Hainan and the northern South China Sea,
  where the surges that hit the east coast originate.

Total ≈ 2,536 point-requests/day against a ~10,000 free allowance. The grid is
carried forward between refreshes — on a non-grid run the fetcher must NOT
write `grid: None`, or five runs in six wipe it (this was the v2.4.1 bug).

`parse_refs()` reads `REFS` straight out of `index.html`, so the app stays the
single source of truth for reference places — add one there and the fetcher
picks it up with no second edit.

Isobars are drawn by marching squares at **1 hPa** (not the usual 4 hPa —
tropical gradients are too slack for that to show anything). The app names no
low and classifies no circulation: a closed contour is for the reader. Toggle
with the 等压线 / Isobars button.

## Two modes (v2.7.0)

`MODE` is `simple` by default and persisted. Simple mode hides everything in
`FULL_IDS` and renders one card: the selected place, a regime badge, and three
to five plain sentences that keep the REASON in ("north is rising and we are
not") rather than handing down a verdict. Detailed mode is the full app.

Deliberately absent: any share, broadcast or WhatsApp feature. This app reads a
model's pressure trend and infers a tendency — forwarded text loses its caveats
in one hop. Rain broadcasting stays in RainBulletin, which reports observed
radar. Anyone who wants to know opens this app themselves.

## Honest limits

- JMA covers the western North Pacific only, and numbers only the cyclones it
  formally bulletins. GDACS (v2.1.0) fills the gap for sub-typhoon systems —
  including South China Sea lows, the ones that actually matter here — but its
  naming and grading are looser. Events within 350 km of a JMA storm are tagged
  as duplicates and suppressed from the map.
- The "≈ Cat X" badge converts JMA's 10-minute wind to a 1-minute basis before
  comparing to Saffir-Simpson. It is a conversion, labelled as one.
- For official Malaysian warnings, MET Malaysia remains the authority.

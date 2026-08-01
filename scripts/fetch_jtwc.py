#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_jtwc.py — mirror JTWC tropical products into data/jtwc.json

Runs on a GitHub Actions runner (US egress, normal User-Agent), because
metoc.navy.mil sends no CORS headers and blocks unusual clients, so a
browser on GitHub Pages can never fetch it directly.

Design rules (Stanley's, applied here):
  * loud failure over silent stale data  -> `generated` only advances on a
    successful fetch; a failed run keeps the old storms but flips fetch_ok
    to false and stamps last_attempt, so the app can shout.
  * never fabricate                      -> a bulletin that does not parse is
    kept as raw text with parsed=false. No guessed positions, ever.

Usage:
    python scripts/fetch_jtwc.py            # normal run
    python scripts/fetch_jtwc.py --selftest # parse built-in fixtures, no network
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
GFX = os.path.join(DATA, "graphics")
OUT = os.path.join(DATA, "jtwc.json")


# metoc.navy.mil sits behind a WAF that rejects non-browser clients. A plain
# library User-Agent gets a 403, so we present as a browser and fall back to
# curl, which has a different TLS handshake and sometimes passes where
# Python's does not.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",          # urllib will not gunzip for us
    "Upgrade-Insecure-Requests": "1",
    "Connection": "close",
}

# Known hosts serving the same product tree. Tried in order; the first that
# answers wins and is used for every product in that run.
HOSTS = [
    "https://www.metoc.navy.mil/jtwc",
    "https://www.metoc.dc3n.navy.mil/jtwc",
]
HOST = HOSTS[0]          # rebound by pick_host()

BASINS = {
    "wp": "Western Pacific",
    "io": "North Indian Ocean",
    "sh": "Southern Hemisphere",
    "cp": "Central Pacific",
    "ep": "Eastern Pacific",
}

TIMEOUT = 30
RETRIES = 3


# ---------------------------------------------------------------- networking

def _urllib_get(url, binary):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
    return raw if binary else raw.decode("utf-8", "replace")


def _curl_get(url, binary):
    """Fallback: curl's TLS fingerprint differs from Python's."""
    import subprocess
    cmd = ["curl", "-sSL", "--fail", "--max-time", str(TIMEOUT),
           "-A", UA,
           "-H", "Accept-Language: en-US,en;q=0.9",
           "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
           url]
    p = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 10)
    if p.returncode != 0:
        raise RuntimeError("curl exit %d: %s" % (p.returncode, p.stderr.decode()[:200].strip()))
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def get(url, binary=False):
    """GET with retries, urllib first then curl. Raises the last error."""
    last = None
    for attempt in range(RETRIES):
        for fn in (_urllib_get, _curl_get):
            try:
                return fn(url, binary)
            except urllib.error.HTTPError as e:
                last = RuntimeError("HTTP %s %s" % (e.code, e.reason))
                if e.code == 404:
                    raise           # a missing product is an answer, not a failure
            except Exception as e:  # noqa: BLE001
                last = e
        if attempt < RETRIES - 1:
            time.sleep(2 ** attempt * 3)
    raise last


def pick_host():
    """Set HOST to the first mirror that answers. Returns the index HTML."""
    global HOST
    errs = []
    for h in HOSTS:
        try:
            html = get(h + "/jtwc.html")
            HOST = h
            print("host ok: %s" % h)
            return html
        except Exception as e:  # noqa: BLE001
            errs.append("%s -> %s" % (h, e))
            print("host failed: %s -> %s" % (h, e), file=sys.stderr)
    raise RuntimeError("; ".join(errs))


def products():
    return HOST + "/products/"


# ------------------------------------------------------------------- parsing

LATLON = re.compile(r"(\d{1,2}(?:\.\d)?)\s*([NS])\s+(\d{1,3}(?:\.\d)?)\s*([EW])")


def _pt(m):
    """Regex match -> (lat, lon) signed decimal degrees."""
    lat = float(m.group(1)) * (1 if m.group(2) == "N" else -1)
    lon = float(m.group(3)) * (1 if m.group(4) == "E" else -1)
    return lat, lon


def parse_warning(text):
    """Parse a JTWC WTPN/WTIO/WTXS tropical cyclone warning.

    Returns dict with parsed=True and the fields it could read, or
    parsed=False plus `problems` — never a half-filled position.
    """
    out = {"parsed": False, "problems": []}
    t = text.upper()

    m = re.search(r"SUBJ/([^/]+?)\s+WARNING\s+NR\s*(\d+)", t)
    if m:
        out["name"] = m.group(1).strip()
        out["warning_no"] = int(m.group(2))
    else:
        m2 = re.search(r"SUBJ/(.+?)//", t, re.S)
        if m2:
            out["name"] = " ".join(m2.group(1).split())
        out["problems"].append("no SUBJ/... WARNING NR line")

    # Warning position block: "011200Z --- NEAR 14.2N 115.6E"
    m = re.search(r"(\d{6}Z)\s*-+\s*NEAR\s+" + LATLON.pattern, t)
    if m:
        out["dtg"] = m.group(1)
        lat, lon = _pt(LATLON.search(m.group(0)))
        out["lat"], out["lon"] = lat, lon
    else:
        out["problems"].append("no warning position line")

    m = re.search(r"MOVEMENT\s+PAST\s+SIX\s+HOURS\s*-\s*(\d{1,3})\s*DEGREES\s+AT\s+(\d{1,3})\s*KTS?", t)
    if m:
        out["move_deg"] = int(m.group(1))
        out["move_kt"] = int(m.group(2))

    m = re.search(r"MAX\s+SUSTAINED\s+WINDS\s*-\s*(\d{2,3})\s*KT,?\s*GUSTS\s+(\d{2,3})\s*KT", t)
    if m:
        out["wind_kt"] = int(m.group(1))
        out["gust_kt"] = int(m.group(2))
    else:
        out["problems"].append("no max sustained winds line")

    m = re.search(r"POSITION\s+ACCURATE\s+TO\s+WITHIN\s+(\d{2,3})\s*NM", t)
    if m:
        out["posit_accuracy_nm"] = int(m.group(1))

    # Forecast points: "A. 12 HRS, VALID AT:  020000Z --- 14.9N 113.8E"
    fcst = []
    for fm in re.finditer(
        r"([A-Z])\.\s*(\d{1,3})\s*HRS?,\s*VALID\s+AT:?\s*(\d{6}Z)\s*-+\s*" + LATLON.pattern,
        t,
    ):
        lat, lon = _pt(LATLON.search(fm.group(0)))
        tau = int(fm.group(2))
        tail = t[fm.end(): fm.end() + 400]
        wm = re.search(r"MAX\s+SUSTAINED\s+WINDS\s*-\s*(\d{2,3})\s*KT", tail)
        fcst.append({
            "tau": tau,
            "valid": fm.group(3),
            "lat": lat,
            "lon": lon,
            "wind_kt": int(wm.group(1)) if wm else None,
        })
    fcst.sort(key=lambda p: p["tau"])
    out["forecast"] = fcst
    if not fcst:
        out["problems"].append("no forecast points")

    # Good enough to plot only if we have an actual current fix.
    out["parsed"] = "lat" in out and "wind_kt" in out
    return out


def parse_abpw(text):
    """Pull invest areas out of the Significant Tropical Weather Advisory."""
    t = text.upper()
    out = {"disturbances": [], "problems": []}

    m = re.search(r"^ABPW\d*\s+\w+\s+(\d{6})", t, re.M)
    if m:
        out["dtg"] = m.group(1) + "Z"

    # "(INVEST 93W) ... IS NOW LOCATED NEAR 9.0N 130.1E"
    for block in re.split(r"\n\s*\(\d+\)\s*", t)[1:]:
        idm = re.search(r"INVEST\s+(\d{2}[WESABCP])", block)
        # JTWC phrases the current fix three ways. "NOW LOCATED NEAR" wins,
        # because the same sentence usually also carries the PREVIOUS position.
        posm = None
        for pat in (r"NOW\s+LOCATED\s+NEAR\s+", r"LOCATED\s+NEAR\s+", r"NEAR\s+"):
            posm = re.search(pat + LATLON.pattern, block)
            if posm:
                break
        potm = re.search(r"POTENTIAL\s+FOR\s+THE\s+DEVELOPMENT[^.]*?IS\s+(LOW|MEDIUM|HIGH)", block, re.S)
        if not (idm and posm):
            continue
        lat, lon = _pt(LATLON.search(posm.group(0)))
        out["disturbances"].append({
            "id": idm.group(1),
            "lat": lat,
            "lon": lon,
            "potential": potm.group(1) if potm else "UNKNOWN",
        })
    if not out["disturbances"]:
        out["problems"].append("no invest areas found (may simply be a quiet period)")
    return out


def find_active(index_html):
    """Storm ids referenced by the JTWC landing page, e.g. 'wp0126'."""
    ids = set(re.findall(r"(?:products/)?((?:wp|io|sh|cp|ep)\d{4})(?:web)?\.(?:txt|gif|kmz)",
                         index_html, re.I))
    return sorted(i.lower() for i in ids)


# ------------------------------------------------------------------ selftest

FIX_WARNING = """
WTPN31 PGTW 011200
MSGID/GENADMIN/JOINT TYPHOON WRNCEN PEARL HARBOR HI//
SUBJ/TYPHOON 05W (KAJIKI) WARNING NR 012//
RMKS/
1. TYPHOON 05W (KAJIKI) WARNING POSITION:
   011200Z --- NEAR 14.2N 115.6E
   MOVEMENT PAST SIX HOURS - 290 DEGREES AT 12 KTS
   POSITION ACCURATE TO WITHIN 025 NM
   POSITION BASED ON CENTER LOCATED BY SATELLITE
   PRESENT WIND DISTRIBUTION:
   MAX SUSTAINED WINDS - 075 KT, GUSTS 090 KT
   RADIUS OF 034 KT WINDS - 120 NM NORTHEAST QUADRANT
2. FORECASTS:
   A. 12 HRS, VALID AT:
      020000Z --- 14.9N 113.8E
      MAX SUSTAINED WINDS - 085 KT, GUSTS 105 KT
   B. 24 HRS, VALID AT:
      021200Z --- 15.6N 111.9E
      MAX SUSTAINED WINDS - 090 KT, GUSTS 110 KT
   C. 36 HRS, VALID AT:
      030000Z --- 16.1N 110.2E
      MAX SUSTAINED WINDS - 080 KT, GUSTS 100 KT
//
"""

FIX_ABPW = """
ABPW10 PGTW 010600
SUBJ/SIGNIFICANT TROPICAL WEATHER ADVISORY FOR THE WESTERN AND
SOUTH PACIFIC OCEAN/010600Z-011800ZAUG2026//
RMKS/
2. TROPICAL DISTURBANCE SUMMARY:
   A. WESTERN NORTH PACIFIC AREA
      (1) AN AREA OF CONVECTION (INVEST 93W) PREVIOUSLY LOCATED
      NEAR 8.2N 132.5E IS NOW LOCATED NEAR 9.0N 130.1E. ANIMATED
      MULTISPECTRAL IMAGERY DEPICTS FLARING CONVECTION.
      THE POTENTIAL FOR THE DEVELOPMENT OF A SIGNIFICANT TROPICAL
      CYCLONE WITHIN THE NEXT 24 HOURS IS MEDIUM.
      (2) AN AREA OF CONVECTION (INVEST 94W) HAS PERSISTED
      NEAR 5.1N 110.8E. THE POTENTIAL FOR THE DEVELOPMENT OF A
      SIGNIFICANT TROPICAL CYCLONE WITHIN THE NEXT 24 HOURS IS LOW.
//
"""

FIX_INDEX = """
<a href="/jtwc/products/wp0526.tcw">Warning</a>
<a href="products/wp0526web.txt">Text</a>
<img src="/jtwc/products/wp0526.gif">
<a href="/jtwc/products/io0126web.txt">IO</a>
"""


def selftest():
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got != want:
            ok = False
            print("FAIL %-28s got=%r want=%r" % (label, got, want))
        else:
            print("pass %-28s %r" % (label, got))

    w = parse_warning(FIX_WARNING)
    check("warning.parsed", w["parsed"], True)
    check("warning.name", w["name"], "TYPHOON 05W (KAJIKI)")
    check("warning.no", w["warning_no"], 12)
    check("warning.dtg", w["dtg"], "011200Z")
    check("warning.lat", w["lat"], 14.2)
    check("warning.lon", w["lon"], 115.6)
    check("warning.wind", w["wind_kt"], 75)
    check("warning.gust", w["gust_kt"], 90)
    check("warning.move", (w["move_deg"], w["move_kt"]), (290, 12))
    check("warning.posit_nm", w["posit_accuracy_nm"], 25)
    check("warning.fcst_n", len(w["forecast"]), 3)
    check("warning.fcst_taus", [p["tau"] for p in w["forecast"]], [12, 24, 36])
    check("warning.fcst0", (w["forecast"][0]["lat"], w["forecast"][0]["lon"]), (14.9, 113.8))
    check("warning.fcst0_wind", w["forecast"][0]["wind_kt"], 85)
    check("warning.problems", w["problems"], [])

    a = parse_abpw(FIX_ABPW)
    check("abpw.dtg", a["dtg"], "010600Z")
    check("abpw.n", len(a["disturbances"]), 2)
    check("abpw.d0", (a["disturbances"][0]["id"], a["disturbances"][0]["potential"]), ("93W", "MEDIUM"))
    check("abpw.d1_pos", (a["disturbances"][1]["lat"], a["disturbances"][1]["lon"]), (5.1, 110.8))

    check("index.ids", find_active(FIX_INDEX), ["io0126", "wp0526"])

    # A bulletin that does not parse must say so, not invent a position.
    bad = parse_warning("SUBJ/SOMETHING ODD//\nNO POSITION HERE\n")
    check("bad.parsed", bad["parsed"], False)
    check("bad.has_lat", "lat" in bad, False)

    # Southern hemisphere / western longitudes keep their sign.
    sh = parse_warning(FIX_WARNING.replace("14.2N 115.6E", "14.2S 115.6W"))
    check("sh.lat", sh["lat"], -14.2)
    check("sh.lon", sh["lon"], -115.6)

    print("\n%s" % ("ALL TESTS PASSED" if ok else "TESTS FAILED"))
    return 0 if ok else 1


# ---------------------------------------------------------------------- main

def load_previous():
    try:
        with open(OUT, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save(doc):
    os.makedirs(DATA, exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, OUT)


def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prev = load_previous()
    errors = []

    try:
        index_html = pick_host()
    except Exception as e:  # noqa: BLE001
        # Total failure: keep the old payload, but make the failure visible.
        doc = dict(prev)
        doc["fetch_ok"] = False
        doc["last_attempt"] = now
        doc.setdefault("generated", None)
        doc["errors"] = ["index fetch failed: %s" % e]
        save(doc)
        print("index fetch failed: %s (kept previous data)" % e, file=sys.stderr)
        return 0  # do not fail the workflow; the app shows the staleness

    ids = find_active(index_html)
    print("active ids: %s" % (ids or "none"))

    storms = []
    for sid in ids:
        basin = BASINS.get(sid[:2], sid[:2].upper())
        rec = {"id": sid, "basin_code": sid[:2].upper(), "basin": basin,
               "number": int(sid[2:4]), "year": 2000 + int(sid[4:6])}
        got_text = False
        for suffix in ("web.txt", ".tcw"):
            try:
                txt = get(products() + sid + suffix)
                if "SUBJ" not in txt.upper():
                    continue
                rec["warning_text"] = txt.strip()
                rec.update(parse_warning(txt))
                got_text = True
                break
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    errors.append("%s%s: HTTP %s" % (sid, suffix, e.code))
            except Exception as e:  # noqa: BLE001
                errors.append("%s%s: %s" % (sid, suffix, e))
        if not got_text:
            errors.append("%s: no readable warning text" % sid)
            continue

        try:
            prog = get(products() + sid + "prog.txt")
            if "PROGNOSTIC" in prog.upper() or len(prog) > 200:
                rec["prog_reasoning"] = prog.strip()
        except Exception:
            pass  # reasoning discussion is optional

        try:
            gif = get(products() + sid + ".gif", binary=True)
            os.makedirs(GFX, exist_ok=True)
            with open(os.path.join(GFX, sid + ".gif"), "wb") as f:
                f.write(gif)
            rec["graphic"] = "graphics/%s.gif" % sid
        except Exception as e:  # noqa: BLE001
            errors.append("%s.gif: %s" % (sid, e))

        storms.append(rec)

    abpw = None
    for name in ("abpwsair.txt", "abiosair.txt", "abpwweb.txt"):
        try:
            txt = get(products() + name)
            if "TROPICAL" in txt.upper():
                abpw = parse_abpw(txt)
                abpw["text"] = txt.strip()
                abpw["file"] = name
                break
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (name, e))

    doc = {
        "generated": now,
        "last_attempt": now,
        "fetch_ok": True,
        "source": "Joint Typhoon Warning Center (JTWC), U.S. Navy/Air Force",
        "source_url": HOST + "/jtwc.html",
        "storms": storms,
        "abpw": abpw,
        "errors": errors,
    }
    save(doc)

    # Drop graphics for storms that are no longer active.
    keep = {s["id"] + ".gif" for s in storms}
    if os.path.isdir(GFX):
        for f in os.listdir(GFX):
            if f.endswith(".gif") and f not in keep:
                os.remove(os.path.join(GFX, f))

    print("wrote %s: %d storm(s), %d error(s)" % (OUT, len(storms), len(errors)))
    for e in errors:
        print("  ! %s" % e)
    return 0


def fixture():
    """Write data/jtwc.sample.json from the fixtures, for offline UI work.

    Marked demo:true so the app can refuse to ever treat it as real.
    """
    w = parse_warning(FIX_WARNING)
    a = parse_abpw(FIX_ABPW)
    a["text"] = FIX_ABPW.strip()
    rec = {"id": "wp0526", "basin_code": "WP", "basin": BASINS["wp"],
           "number": 5, "year": 2026, "warning_text": FIX_WARNING.strip()}
    rec.update(w)
    doc = {
        "demo": True,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_attempt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fetch_ok": True,
        "source": "FIXTURE — not real data",
        "source_url": HOST + "/jtwc.html",
        "storms": [rec],
        "abpw": a,
        "errors": [],
    }
    os.makedirs(DATA, exist_ok=True)
    p = os.path.join(DATA, "jtwc.sample.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=True)
    print("wrote %s" % p)
    return 0


def probe():
    """Print exactly what each host/method does. Read this in the Actions log."""
    import subprocess
    print("== curl version ==")
    subprocess.run(["curl", "--version"])
    for h in HOSTS:
        for path in ("/jtwc.html", "/products/abpwsair.txt"):
            url = h + path
            print("\n== %s ==" % url)
            for name, fn in (("urllib", _urllib_get), ("curl", _curl_get)):
                try:
                    body = fn(url, False)
                    head = " ".join(body[:160].split())
                    print("  %-7s OK  %6d bytes  %s" % (name, len(body), head))
                except Exception as e:  # noqa: BLE001
                    print("  %-7s FAIL %s" % (name, e))
    return 0


if __name__ == "__main__":
    if "--probe" in sys.argv:
        sys.exit(probe())
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--fixture" in sys.argv:
        sys.exit(fixture())
    sys.exit(main())

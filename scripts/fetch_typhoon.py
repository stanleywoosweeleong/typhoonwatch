#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_typhoon.py — mirror JMA tropical cyclone data into data/typhoon.json

WHY JMA AND NOT JTWC
--------------------
metoc.navy.mil returns HTTP 403 to every request from a GitHub Actions runner
(confirmed 2026-08-01: urllib and curl, both mirrors, browser headers). It is
an IP-range policy, not a client problem, so no header trick fixes it.

JMA is the better source anyway for this app's purpose: it is the WMO-designated
Regional Specialized Meteorological Centre for the western North Pacific — the
official authority for that basin — and it uses 10-minute sustained winds, the
same convention MET Malaysia uses.

Design rules (unchanged):
  * loud failure over silent stale data -> `generated` only advances on success
  * never fabricate                     -> unparseable entries keep raw JSON and
                                           carry parsed:false

Usage:
    python scripts/fetch_typhoon.py
    python scripts/fetch_typhoon.py --selftest
    python scripts/fetch_typhoon.py --fixture
    python scripts/fetch_typhoon.py --probe
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "typhoon.json")

BASE = "https://www.jma.go.jp/bosai/typhoon/data"
TARGETS = BASE + "/targetTc.json"

UA = ("TyphoonWatch/2.0 (+https://github.com/stanleywoosweeleong/typhoonwatch; "
      "non-commercial farmer weather app)")
HEADERS = {"User-Agent": UA, "Accept": "application/json,*/*", "Accept-Encoding": "identity"}
TIMEOUT = 30
RETRIES = 3

# JMA writes movement direction as a Japanese compass word.
COURSE_DEG = {
    "北": 0, "北北東": 22.5, "北東": 45, "東北東": 67.5,
    "東": 90, "東南東": 112.5, "南東": 135, "南南東": 157.5,
    "南": 180, "南南西": 202.5, "南西": 225, "西南西": 247.5,
    "西": 270, "西北西": 292.5, "北西": 315, "北北西": 337.5,
    "不定": None,
}

# JMA's two qualitative ladders. Kept as codes; the app supplies zh/en wording.
INTENSITY = {"-": None, "強い": "strong", "非常に強い": "very_strong", "猛烈な": "violent"}
SCALE = {"-": None, "大型": "large", "超大型": "very_large"}


# ---------------------------------------------------------------- networking

def get(url):
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < RETRIES - 1:
                time.sleep(2 ** attempt * 3)
    raise last


def get_json(url):
    return json.loads(get(url))


# ------------------------------------------------------------------- parsing

def _num(v):
    """JMA sends numbers as strings. Return float or None, never a guess."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso_utc(block, key="validtime"):
    t = (block.get(key) or {}).get("UTC")
    return t if isinstance(t, str) else None


def _point(block):
    """One 実況/推定/予報 entry -> flat dict. Position is required."""
    pos = block.get("position") or {}
    deg = pos.get("deg")
    if not (isinstance(deg, list) and len(deg) == 2):
        return None
    out = {
        "lat": _num(deg[0]),
        "lon": _num(deg[1]),
        "valid": _iso_utc(block),
        "tau": block.get("advancedHours"),
        "pressure_hpa": _num(block.get("pressure")),
        "category": ((block.get("category") or {}).get("en")),
        "category_jp": ((block.get("category") or {}).get("jp")),
        "intensity": INTENSITY.get(block.get("intensity")),
        "scale": SCALE.get(block.get("scale")),
        "location_jp": block.get("location"),
        "accuracy_jp": pos.get("accuracy"),
    }
    if out["lat"] is None or out["lon"] is None:
        return None

    wind = block.get("maximumWind") or {}
    sus, gust = wind.get("sustained") or {}, wind.get("gust") or {}
    out["wind_ms"] = _num(sus.get("m/s"))
    out["wind_kt"] = _num(sus.get("kt"))
    out["wind_note_jp"] = sus.get("note")
    out["gust_ms"] = _num(gust.get("m/s"))
    out["gust_kt"] = _num(gust.get("kt"))

    course = block.get("course")
    out["course_jp"] = course
    out["move_deg"] = COURSE_DEG.get(course) if course else None
    speed = block.get("speed") or {}
    out["move_kt"] = _num(speed.get("kt"))
    out["move_kmh"] = _num(speed.get("km/h"))
    note = speed.get("note")
    out["speed_note_jp"] = note.get("jp") if isinstance(note, dict) else note

    circ = block.get("probabilityCircleRadius") or {}
    out["circle_km"] = _num(circ.get("km"))

    def radii(key):
        rows = block.get(key) or []
        got = []
        for r in rows:
            area = r.get("area")
            area = area.get("jp") if isinstance(area, dict) else area
            km = _num((r.get("range") or {}).get("km"))
            if km is not None:
                got.append({"area_jp": area, "km": km})
        return got

    out["storm_radii"] = radii("stormWarning")   # 暴風域 / 暴風警戒域, >=25 m/s
    out["gale_radii"] = radii("galeWarning")     # 強風域, >=15 m/s
    return out


def parse_spec(spec):
    """specifications.json (a list) -> one storm record."""
    rec = {"parsed": False, "problems": [], "forecast": []}
    if not isinstance(spec, list) or not spec:
        rec["problems"].append("specifications.json was not a non-empty list")
        return rec

    title = spec[0] if spec[0].get("part") == "title" else None
    if title:
        rec["typhoon_number"] = title.get("typhoonNumber")
        name = title.get("name") or {}
        rec["name_jp"] = name.get("jp")
        rec["name_en"] = name.get("en")
        rec["category"] = (title.get("category") or {}).get("en")
        rec["issued"] = (title.get("issue") or {}).get("UTC")
    else:
        rec["problems"].append("no title part")

    for block in spec[1:]:
        part = block.get("part")
        part_jp = part.get("jp") if isinstance(part, dict) else part
        p = _point(block)
        if p is None:
            rec["problems"].append("unreadable position in part %r" % part_jp)
            continue
        p["part_jp"] = part_jp
        # advancedHours 0 on the 実況 block is the current analysis
        if p.get("tau") in (0, None) and "current" not in rec:
            rec["current"] = p
        else:
            rec["forecast"].append(p)

    rec["forecast"].sort(key=lambda x: x.get("tau") or 0)
    rec["parsed"] = "current" in rec
    if not rec["parsed"]:
        rec["problems"].append("no current analysis block")
    return rec


# ------------------------------------------------------------------ fixtures
# Built from JMA's documented schema. Values are illustrative, not a real storm.

FIX_TARGETS = [{"tropicalCyclone": "TC2611", "typhoonNumber": "2609",
                "category": "TY", "issue": "2026-07-05T13:05:00+09:00"}]

FIX_SPEC = [
    {"part": "title", "typhoonNumber": "2609",
     "name": {"jp": "バービー", "en": "BARBIE"},
     "category": {"jp": "台風", "en": "TY"},
     "issue": {"JST": "2026-07-05T13:05:00+09:00", "UTC": "2026-07-05T04:05:00Z"}},
    {"part": {"jp": "実況"},
     "maximumWind": {"sustained": {"m/s": "55", "kt": "105", "note": "中心付近"},
                     "gust": {"m/s": "75", "kt": "150"}},
     "galeWarning": [{"area": "南", "range": {"km": 500, "nm": 270}},
                     {"area": "北", "range": {"km": 390, "nm": 210}}],
     "stormWarning": [{"area": {"jp": "全域"}, "range": {"km": 140, "nm": 75}}],
     "advancedHours": 0, "category": {"jp": "台風", "en": "TY"},
     "scale": "-", "intensity": "猛烈な",
     "position": {"deg": [13.1, 148.7], "dm": [[13, 5], [148, 40]], "accuracy": "正確"},
     "location": "マリアナ諸島", "course": "西北西",
     "speed": {"km/h": "10", "kt": "6"}, "pressure": "920",
     "validtime": {"JST": "2026-07-05T12:00:00+09:00", "UTC": "2026-07-05T03:00:00Z"}},
    {"part": {"jp": "予報　２４時間後"},
     "maximumWind": {"sustained": {"m/s": "55", "kt": "110", "note": "中心付近"},
                     "gust": {"m/s": "80", "kt": "155"}},
     "stormWarning": [{"area": {"jp": "全域"}, "range": {"km": 250, "nm": 135}}],
     "advancedHours": 24, "category": {"jp": "台風", "en": "TY"}, "intensity": "猛烈な",
     "position": {"deg": [14.6, 144.3], "dm": [[14, 35], [144, 20]]},
     "probabilityCircleRadius": {"km": 65, "nm": 35},
     "location": "マリアナ諸島", "course": "西北西",
     "speed": {"km/h": "20", "kt": "11"}, "pressure": "905",
     "validtime": {"JST": "2026-07-06T12:00:00+09:00", "UTC": "2026-07-06T03:00:00Z"}},
    {"part": {"jp": "予報　４８時間後"},
     "maximumWind": {"sustained": {"m/s": "50", "kt": "100"},
                     "gust": {"m/s": "70", "kt": "140"}},
     "advancedHours": 48, "category": {"jp": "台風", "en": "TY"}, "intensity": "非常に強い",
     "position": {"deg": [16.0, 140.1], "dm": [[16, 0], [140, 6]]},
     "probabilityCircleRadius": {"km": 155, "nm": 85},
     "location": "日本の南", "course": "西北西",
     "speed": {"km/h": "15", "kt": "8"}, "pressure": "925",
     "validtime": {"JST": "2026-07-07T12:00:00+09:00", "UTC": "2026-07-07T03:00:00Z"}},
]


def selftest():
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got != want:
            ok = False
            print("FAIL %-26s got=%r want=%r" % (label, got, want))
        else:
            print("pass %-26s %r" % (label, got))

    r = parse_spec(FIX_SPEC)
    check("parsed", r["parsed"], True)
    check("problems", r["problems"], [])
    check("typhoon_number", r["typhoon_number"], "2609")
    check("name_en", r["name_en"], "BARBIE")
    check("issued", r["issued"], "2026-07-05T04:05:00Z")
    c = r["current"]
    check("cur.lat/lon", (c["lat"], c["lon"]), (13.1, 148.7))
    check("cur.wind_kt", c["wind_kt"], 105.0)
    check("cur.wind_ms", c["wind_ms"], 55.0)
    check("cur.gust_kt", c["gust_kt"], 150.0)
    check("cur.pressure", c["pressure_hpa"], 920.0)
    check("cur.course_deg", c["move_deg"], 292.5)
    check("cur.move_kt", c["move_kt"], 6.0)
    check("cur.intensity", c["intensity"], "violent")
    check("cur.scale", c["scale"], None)
    check("cur.accuracy", c["accuracy_jp"], "正確")
    check("cur.storm_radii", c["storm_radii"], [{"area_jp": "全域", "km": 140.0}])
    check("cur.gale_n", len(c["gale_radii"]), 2)
    check("cur.no_circle", c["circle_km"], None)
    check("fcst_n", len(r["forecast"]), 2)
    check("fcst_taus", [f["tau"] for f in r["forecast"]], [24, 48])
    check("fcst0.circle_km", r["forecast"][0]["circle_km"], 65.0)
    check("fcst1.circle_km", r["forecast"][1]["circle_km"], 155.0)
    check("fcst1.intensity", r["forecast"][1]["intensity"], "very_strong")
    check("circle grows", r["forecast"][1]["circle_km"] > r["forecast"][0]["circle_km"], True)

    # A slow-moving storm reports a note instead of a speed.
    slow = json.loads(json.dumps(FIX_SPEC))
    slow[1]["speed"] = {"note": {"jp": "ほとんど停滞"}}
    slow[1]["course"] = "不定"
    s = parse_spec(slow)["current"]
    check("slow.move_kt", s["move_kt"], None)
    check("slow.note", s["speed_note_jp"], "ほとんど停滞")
    check("slow.course_deg", s["move_deg"], None)

    # A tropical depression has no name block.
    td = json.loads(json.dumps(FIX_SPEC))
    del td[0]["name"]
    check("td.name_en", parse_spec(td)["name_en"], None)

    # Southern latitudes / western longitudes keep their sign.
    sh = json.loads(json.dumps(FIX_SPEC))
    sh[1]["position"]["deg"] = [-13.1, -148.7]
    shc = parse_spec(sh)["current"]
    check("sh.lat/lon", (shc["lat"], shc["lon"]), (-13.1, -148.7))

    # Garbage must not produce a position.
    bad = parse_spec([{"part": "title"}, {"part": {"jp": "実況"}, "position": {}}])
    check("bad.parsed", bad["parsed"], False)
    check("bad.no_current", "current" in bad, False)

    # --- GDACS ---
    fc = {"features": [
        {"geometry": {"coordinates": [114.5, 22.9]},
         "properties": {"eventid": "1001067", "episodeid": "3", "eventname": "KAJIKI",
                        "iscurrent": "true", "alertlevel": "Orange",
                        "severitydata": {"severity": 120, "severityunit": "km/h",
                                         "severitytext": "Tropical Storm"},
                        "affectedcountries": [{"iso3": "CHN"}],
                        "url": {"report": "https://www.gdacs.org/report.aspx"}}},
        {"geometry": {"coordinates": [157.5, 20.8]},
         "properties": {"eventid": "1001070", "eventname": "DOLPHIN",
                        "iscurrent": "true", "alertlevel": "Green",
                        "severitydata": {"severity": 185, "severityunit": "km/h"}}},
        {"geometry": {"coordinates": [60.0, 15.0]},
         "properties": {"eventid": "1000999", "eventname": "OLD", "iscurrent": "false"}},
        {"geometry": {}, "properties": {"eventid": "x", "iscurrent": "true"}},
    ]}
    ev, probs = parse_gdacs(fc)
    check("gdacs.n", len(ev), 2)
    check("gdacs.problems", probs, [])
    check("gdacs.name", ev[0]["name"], "KAJIKI")
    check("gdacs.pos", (ev[0]["lat"], ev[0]["lon"]), (22.9, 114.5))
    check("gdacs.alert", ev[0]["alert"], "orange")
    check("gdacs.sev", (ev[0]["severity"], ev[0]["severity_unit"]), (120.0, "km/h"))
    check("gdacs.drops_stale", [e["name"] for e in ev], ["KAJIKI", "DOLPHIN"])
    check("gdacs.bad_shape", parse_gdacs({})[1], ["GDACS response had no features list"])

    jma = [{"id": "TC2615", "current": {"lat": 20.8, "lon": 157.5}}]
    tagged = tag_duplicates(json.loads(json.dumps(ev)), jma)
    check("dup.far_kept", tagged[0]["also_jma"], None)
    check("dup.near_tagged", tagged[1]["also_jma"], "TC2615")
    check("hav 22.9N114.5E->Kelantan", round(hav_km(22.9, 114.5, 6.13, 102.24) / 10) * 10, 2280)

    print("\n%s" % ("ALL TESTS PASSED" if ok else "TESTS FAILED"))
    return 0 if ok else 1



# ------------------------------------------------------------------- GDACS
# JMA lists only cyclones RSMC Tokyo issues formal bulletins for. A South
# China Sea depression that never reaches typhoon grade — exactly the kind
# that drives a monsoon surge onto the Malaysian east coast — can be absent
# from targetTc.json entirely. GDACS aggregates several agencies and fills
# that gap. It is a secondary source and is labelled as such in the app.

GDACS_URL = ("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
             "?eventlist=TC")


def _truthy(v):
    return str(v).strip().lower() in ("true", "1", "yes")


def parse_gdacs(fc):
    """geoJSON FeatureCollection -> list of current tropical cyclone events."""
    out, problems = [], []
    feats = (fc or {}).get("features")
    if not isinstance(feats, list):
        return [], ["GDACS response had no features list"]
    for f in feats:
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates")
        if not (isinstance(coords, list) and len(coords) >= 2):
            continue                      # no position -> not usable, skip
        if not _truthy(props.get("iscurrent")):
            continue                      # only live systems
        sev = props.get("severitydata") or {}
        out.append({
            "id": str(props.get("eventid") or ""),
            "episode": str(props.get("episodeid") or ""),
            "name": props.get("eventname") or props.get("name") or None,
            "lon": _num(coords[0]),
            "lat": _num(coords[1]),
            "alert": (props.get("alertlevel") or "").lower() or None,
            "severity": _num(sev.get("severity")),
            "severity_unit": sev.get("severityunit"),
            "severity_text": sev.get("severitytext"),
            "from": props.get("fromdate"),
            "to": props.get("todate"),
            "countries": props.get("affectedcountries") or [],
            "url": ((props.get("url") or {}).get("report")
                    if isinstance(props.get("url"), dict) else None),
        })
    return [e for e in out if e["lat"] is not None and e["lon"] is not None], problems


def hav_km(a, b, c, d):
    r = 3.141592653589793 / 180
    import math
    s = (math.sin((c - a) * r / 2) ** 2 +
         math.cos(a * r) * math.cos(c * r) * math.sin((d - b) * r / 2) ** 2)
    return 2 * 6371 * math.asin(min(1, math.sqrt(s)))


def tag_duplicates(events, storms, radius_km=350):
    """Mark GDACS events that are the same system JMA already reports."""
    for e in events:
        e["also_jma"] = None
        for s in storms:
            c = s.get("current") or {}
            if c.get("lat") is None:
                continue
            if hav_km(e["lat"], e["lon"], c["lat"], c["lon"]) <= radius_km:
                e["also_jma"] = s.get("id")
                break
    return events


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


def build(targets, fetch_spec, errors):
    storms = []
    for t in targets:
        tc = t.get("tropicalCyclone")
        if not tc:
            errors.append("target entry without tropicalCyclone: %r" % t)
            continue
        try:
            spec = fetch_spec(tc)
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (tc, e))
            continue
        rec = parse_spec(spec)
        rec["id"] = tc
        rec["target_category"] = t.get("category")
        rec["target_issue"] = t.get("issue")
        rec["raw"] = spec
        storms.append(rec)
    return storms


def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prev = load_previous()
    errors = []

    try:
        targets = get_json(TARGETS)
    except Exception as e:  # noqa: BLE001
        doc = dict(prev)
        doc["fetch_ok"] = False
        doc["last_attempt"] = now
        doc.setdefault("generated", None)
        doc["errors"] = ["targetTc.json fetch failed: %s" % e]
        save(doc)
        print("targetTc fetch failed: %s (kept previous data)" % e, file=sys.stderr)
        return 0

    print("active tropical cyclones: %s" % ([t.get("tropicalCyclone") for t in targets] or "none"))
    storms = build(targets, lambda tc: get_json("%s/%s/specifications.json" % (BASE, tc)), errors)

    others = []
    try:
        events, probs = parse_gdacs(get_json(GDACS_URL))
        others = tag_duplicates(events, storms)
        errors.extend(probs)
        print("GDACS current TC events: %d (%d also in JMA)"
              % (len(others), sum(1 for e in others if e.get("also_jma"))))
    except Exception as e:  # noqa: BLE001
        errors.append("GDACS: %s" % e)     # secondary source: never fatal

    save({
        "generated": now,
        "last_attempt": now,
        "fetch_ok": True,
        "source": "Japan Meteorological Agency — RSMC Tokyo Typhoon Center",
        "source_url": "https://www.jma.go.jp/bosai/map.html#contents=typhoon",
        "wind_averaging": "10-minute sustained",
        "storms": storms,
        "others": others,
        "others_source": "GDACS (JRC/European Commission), aggregating several agencies",
        "errors": errors,
    })
    print("wrote %s: %d storm(s), %d error(s)" % (OUT, len(storms), len(errors)))
    for e in errors:
        print("  ! %s" % e)
    return 0


def fixture():
    errors = []
    storms = build(FIX_TARGETS, lambda tc: FIX_SPEC, errors)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = {"demo": True, "generated": now, "last_attempt": now, "fetch_ok": True,
           "source": "FIXTURE — not real data", "source_url": "",
           "wind_averaging": "10-minute sustained", "storms": storms, "errors": errors}
    os.makedirs(DATA, exist_ok=True)
    p = os.path.join(DATA, "typhoon.sample.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=True)
    print("wrote %s" % p)
    return 0


def probe():
    for label, url in [("JMA targets", TARGETS),
                       ("GDACS TC list",
                        "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=TC")]:
        print("\n== %-14s %s" % (label, url))
        try:
            body = get(url)
            print("  OK   %7d bytes  %s" % (len(body), " ".join(body[:160].split())))
        except Exception as e:  # noqa: BLE001
            print("  FAIL %s" % e)
    return 0


if __name__ == "__main__":
    if "--probe" in sys.argv:
        sys.exit(probe())
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--fixture" in sys.argv:
        sys.exit(fixture())
    sys.exit(main())

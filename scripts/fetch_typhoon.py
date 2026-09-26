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
import re
import shutil
import subprocess
import sys
import tempfile
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
# GDACS runs a search behind its event-list endpoint and is much slower
# than JMA's static JSON. Give it longer and one more go before the run
# gives up on the secondary source.
GDACS_TIMEOUT = 75
GDACS_RETRIES = 4

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

def get(url, timeout=None, retries=None):
    """Fetch a URL as text.

    timeout/retries can be raised per call. GDACS's event-list endpoint is
    routinely slow — it is a search, not a static file — and 30 s x 3 was
    losing the whole secondary source to "The read operation timed out"
    while JMA came back fine.
    """
    last = None
    tmo = TIMEOUT if timeout is None else timeout
    tries = RETRIES if retries is None else retries
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=tmo) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < tries - 1:
                time.sleep(2 ** attempt * 3)
    raise last


def get_json(url, timeout=None, retries=None):
    return json.loads(get(url, timeout, retries))


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


def _raises(fn):
    try:
        fn()
        return False
    except Exception:  # noqa: BLE001
        return True


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

    # --- pressure ---
    refs = parse_refs(open(os.path.join(ROOT, "index.html"), encoding="utf-8").read())
    # Bump when REFS changes in index.html. The fixed number is the point:
    # it catches a regex regression that silently drops places.
    check("refs.n", len(refs), 30)
    check("refs.ids_unique", len({r["id"] for r in refs}), len(refs))
    check("refs.first", refs[0]["id"], "triang")
    check("refs.coords_numeric",
          all(isinstance(r["lat"], float) and isinstance(r["lon"], float) for r in refs), True)
    check("refs.in_grid_or_region",
          all(-15 <= r["lat"] <= 50 and 90 <= r["lon"] <= 175 for r in refs), True)
    check("refs.comments_ignored", any(r["id"] == "tokyo" for r in refs), True)
    check("refs.parse_empty_raises",
          _raises(lambda: parse_refs("no refs here")), True)

    la, lo = grid_points()
    check("grid.n", len(la), 598)
    check("grid.corners", (min(la), max(la), min(lo), max(lo)), (-3.0, 22.0, 100.0, 122.0))
    check("grid.step_resolves_vortex", GRID["step"] * 111 < 150, True)
    check("grid.covers_nw_borneo",
          sum(1 for a, o in zip(la, lo) if 1 <= a <= 5 and 109 <= o <= 115), 35)
    check("grid.covers_hainan",
          any(abs(a - 20.0) < 0.6 and abs(o - 110.0) < 0.6 for a, o in zip(la, lo)), True)
    check("grid.covers_sandakan_lon", max(lo) >= 118.12, True)
    check("grid.daily_budget", 18 * 8 + len(la) * (24 // GRID_EVERY_H), 2536)

    # 25 hourly stamps: index 0 is exactly 24 h before index 24
    times = ["2026-08-01T%02d:00" % h for h in range(1, 24)] + \
            ["2026-08-02T00:00", "2026-08-02T01:00"]
    ent = {"hourly": {"time": times, "pressure_msl": [1010.0] + [None] * 23 + [1008.5]}}
    check("tend.span_is_24h", (len(times), times[0], times[24]),
          (25, "2026-08-01T01:00", "2026-08-02T01:00"))
    check("tend.value_and_change", pick_tendency(ent, "2026-08-02T01:00"),
          (1008.5, -1.5, "2026-08-02T01:00Z"))
    check("tend.no_history", pick_tendency({"hourly": {"time": ["2026-08-02T01:00"],
          "pressure_msl": [1009.0]}}, "2026-08-02T01:00"), (1009.0, None, "2026-08-02T01:00Z"))
    check("tend.empty", pick_tendency({}), (None, None, None))
    check("tend.all_null", pick_tendency({"hourly": {"time": ["2026-08-02T01:00"],
          "pressure_msl": [None]}}, "2026-08-02T01:00"), (None, None, None))
    # Codex 2026-09-26: a 2020 series was accepted as today's reading
    old_series = {"hourly": {"time": ["2020-01-0%dT00:00" % d for d in (1, 2)],
                             "pressure_msl": [1010.0, 1008.0]}}
    check("tend.rejects_old_series", pick_tendency(old_series, "2026-08-02T01:00"),
          (None, None, None))
    # a missing hour must not make "24 slots back" pass for "24 hours back"
    # series 08-01T00..08-02T01 with 08-01T05 missing: 24 slots back from the
    # end lands on 08-01T00 (25 h back, 1012); 24 h back is 08-01T01 (1010)
    gap_t = ["2026-08-01T00:00"] + [t for t in times if t != "2026-08-01T05:00"]
    gap_v = [1012.0, 1010.0] + [1011.0] * (len(gap_t) - 3) + [1008.5]
    gap = {"hourly": {"time": gap_t, "pressure_msl": gap_v}}
    check("tend.gap_uses_timestamp", pick_tendency(gap, "2026-08-02T01:00")[1], -1.5)

    # --- grid refresh by age (Codex #5) ---
    t0 = datetime(2026, 9, 26, 5, 31, tzinfo=timezone.utc)
    check("grid.due_when_none", grid_due(None, t0), True)
    check("grid.due_when_undated", grid_due({"values": []}, t0), True)
    # the live case: a late run at 05:31 with a grid from 12:00 the day before
    check("grid.due_17h_old", grid_due({"time": "2026-09-25T12:00Z"}, t0), True)
    check("grid.not_due_3h_old", grid_due({"time": "2026-09-26T02:30Z"}, t0), False)
    check("grid.due_late_run", grid_due({"time": "2026-09-26T00:00Z"}, t0), True)
    # the carry-forward bug: a non-grid run must not wipe the previous grid
    old = {"nx": 3, "ny": 3, "time": "2026-08-02T00:00Z", "values": [1] * 9}
    global fetch_series
    real_series = fetch_series
    t_run = datetime(2026, 8, 2, 1, 10, tzinfo=timezone.utc)
    one_ref = [{"id": "triang", "lat": 3.2, "lon": 102.4}]
    try:
        fetch_series = lambda la, lo, extra: [ent]
        kept = fetch_pressure(one_ref, False, old, [], t_run)
        check("grid.carried_forward", kept["grid"], old)
        check("pres.place_has_valid", kept["places"]["triang"]["valid"], "2026-08-02T01:00Z")
        check("grid.none_when_never_had",
              fetch_pressure(one_ref, False, None, [], t_run)["grid"], None)

        def grid_fails(la, lo, extra):
            if extra.startswith("current"):
                raise OSError("timed out")
            return [ent]
        fetch_series = grid_fails
        errs = []
        kept = fetch_pressure(one_ref, True, old, errs, t_run)
        check("grid.fail_keeps_old", kept["grid"], old)
        check("grid.fail_keeps_places", "triang" in kept["places"], True)
        check("grid.fail_is_loud", len(errs), 1)
        fetch_series = lambda la, lo, extra: [old_series]
        check("pres.all_old_raises",
              _raises(lambda: fetch_pressure(one_ref, False, None, [], t_run)), True)
    finally:
        fetch_series = real_series

    # --- a failed download is not a departure (Codex #1) ---
    tg = [{"tropicalCyclone": "TC2611", "typhoonNumber": "2609", "category": "TY"},
          {"tropicalCyclone": "TC2612", "typhoonNumber": "2610", "category": "TS"}]
    good = build(tg[:1], lambda tc: FIX_SPEC, [])[0]
    prev_doc = {"generated": "2026-07-05T03:00:00Z", "storms": [good]}

    def timeout(tc):
        raise OSError("The read operation timed out")
    errs = []
    got = build(tg, timeout, errs, prev_doc["storms"], "2026-07-05T06:00:00Z")
    check("fail.keeps_every_target", [s["id"] for s in got], ["TC2611", "TC2612"])
    check("fail.errors_loud", len(errs), 2)
    check("fail.flagged", [s.get("unavailable") for s in got], [True, True])
    check("fail.keeps_last_good_pos", (got[0]["current"]["lat"], got[0]["issued"]),
          (13.1, "2026-07-05T04:05:00Z"))
    check("fail.new_has_no_position", "current" in got[1], False)
    check("fail.new_not_parsed", got[1]["parsed"], False)
    check("fail.since", got[0]["unavailable_since"], "2026-07-05T06:00:00Z")
    again = build(tg, timeout, [], got, "2026-07-05T09:00:00Z")
    check("fail.since_kept", again[0]["unavailable_since"], "2026-07-05T06:00:00Z")
    check("fail.one_problem_line",
          sum(1 for p in again[0]["problems"] if p.startswith("download failed")), 1)
    check("fail.not_gone",
          track_gone(got, prev_doc, "2026-07-05T06:00:00Z", target_ids(tg)), [])
    # even if the storm list were somehow empty, the target list decides
    check("fail.target_list_decides",
          track_gone([], prev_doc, "2026-07-05T06:00:00Z", target_ids(tg)), [])
    left = track_gone([], prev_doc, "2026-07-05T06:00:00Z", [])
    check("gone.real_departure", [g["id"] for g in left], ["TC2611"])
    back = build(tg[:1], lambda tc: FIX_SPEC, [], got, "2026-07-05T09:00:00Z")[0]
    check("fail.recovers_clean", back.get("unavailable"), None)

    # --- GDACS: iscurrent alone is not enough ---
    stale_fc = {"features": [
        {"geometry": {"coordinates": [83.7, 18.1]},
         "properties": {"eventid": "1001326", "eventname": "ONE-26", "iscurrent": "true",
                        "todate": "2026-09-24T00:00:00"}},
        {"geometry": {"coordinates": [-110.1, 17.4]},
         "properties": {"eventid": "1001325", "eventname": "POLO-26", "iscurrent": "true",
                        "todate": "2026-09-26T03:00:00"}}]}
    check("gdacs.drops_old_todate",
          [e["name"] for e in parse_gdacs(stale_fc, "2026-09-26T05:31:28Z")[0]], ["POLO-26"])
    check("gdacs.no_now_no_filter", len(parse_gdacs(stale_fc)[0]), 2)

    # --- model rain beside the pressure reading (the 转干 veto) ---
    rt = ["2026-08-02T%02d:00" % h for h in range(0, 24)] + \
         ["2026-08-03T%02d:00" % h for h in range(0, 24)]
    wet = {"hourly": {"time": rt, "precipitation": [0.0] * 5 + [3.0, 4.0] + [0.1] * 41}}
    check("rain.next24_sum_max", rain_window(wet, "2026-08-02T04:00Z"), (9.2, 4.0))
    check("rain.excludes_valid_hour", rain_window(wet, "2026-08-02T06:00Z")[0], 2.4)
    part = {"hourly": {"time": rt[:30], "precipitation": [0.0] * 30}}
    check("rain.partial_is_unknown", rain_window(part, "2026-08-02T10:00Z"), (None, None))
    hole = {"hourly": {"time": rt, "precipitation": [0.0] * 10 + [None] + [0.0] * 37}}
    check("rain.null_is_unknown", rain_window(hole, "2026-08-02T04:00Z"), (None, None))

    check("aslist.object", len(_as_list({"a": 1})), 1)
    check("aslist.array", len(_as_list([{"a": 1}, {"b": 2}])), 2)

    # --- squall index through SumatraSquall's own engine ---
    ok = selftest_squall(check) and ok

    print("\n%s" % ("ALL TESTS PASSED" if ok else "TESTS FAILED"))
    return 0 if ok else 1


def _synth_om(url, scen):
    """Port of SumatraSquall tests/smoke.js synth(): a squall night (SW
    steering, high CAPE, pre-dawn land breezes meeting over the Strait, rain
    and gusts) or a calm easterly one."""
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(url).query)
    lats = [float(x) for x in q["latitude"][0].split(",")]
    lons = [float(x) for x in q["longitude"][0].split(",")]
    vars_ = q["hourly"][0].split(",")
    d0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    t0 = int(d0.timestamp()) - 86400
    times = [t0 + i * 3600 for i in range(96)]
    AX = [[5.4, 98.8], [4.6, 99.35], [4.0, 99.65], [3.0, 100.65], [2.2, 101.45], [1.7, 102.5], [1.15, 103.45]]

    def axis_lon(lat):
        for a, b in zip(AX, AX[1:]):
            if b[0] <= lat <= a[0]:
                return a[1] + (b[1] - a[1]) * (a[0] - lat) / (a[0] - b[0])
        return 98.8 if lat > 5.4 else 103.45
    out = []
    for lat, lon in zip(lats, lons):
        west, near = lon < axis_lon(lat), abs(lon - axis_lon(lat)) < 0.7
        hourly = {"time": times}
        for v in vars_:
            col = []
            for ts in times:
                h = datetime.fromtimestamp(ts + 8 * 3600, timezone.utc).hour
                predawn = 0 <= h <= 8
                if scen == "squall":
                    x = (240 if v in ("wind_direction_850hPa", "wind_direction_700hPa") else
                         32 if v in ("wind_speed_850hPa", "wind_speed_700hPa") else
                         ((235 if west else 60) if h <= 7 else 200) if v == "wind_direction_10m" else
                         (14 if h <= 7 else 8) if v == "wind_speed_10m" else
                         2300 if v == "cape" else
                         (6 if (len(lons) > 40 and near and predawn) else
                          4 if (len(lons) <= 40 and lon < 104 and 4 <= h <= 7) else 0)
                         if v == "precipitation" else
                         (62 if (lon < 104 and 4 <= h <= 7) else 20) if v == "wind_gusts_10m" else None)
                else:
                    x = (80 if v.startswith("wind_direction") else 18 if v.startswith("wind_speed") else
                         300 if v == "cape" else 0 if v == "precipitation" else
                         25 if v == "wind_gusts_10m" else None)
                col.append(x)
            hourly[v] = col
        out.append({"latitude": lat, "longitude": lon, "hourly": hourly})
    return out


def selftest_squall(check):
    """Needs node and a copy of SumatraSquall's index.html (env SQUALL_HTML,
    or ../SumatraSquall-main/index.html next to this repo). Without them the
    squall part is skipped and SAYS so — the live run still reports failures
    in `errors`."""
    path = os.environ.get("SQUALL_HTML") or \
        os.path.join(os.path.dirname(ROOT), "SumatraSquall-main", "index.html")
    if not shutil.which("node") or not os.path.exists(path):
        print("SKIP squall tests (need node and SQUALL_HTML=<SumatraSquall index.html>)")
        return True
    html = open(path, encoding="utf-8").read()
    global get_json
    real = get_json
    good = [True]

    def chk(label, got, want):
        if got != want:
            good[0] = False
        check(label, got, want)
    now = datetime.now(timezone.utc).timestamp()
    try:
        for scen in ("squall", "calm"):
            get_json = lambda url, timeout=None, retries=None, s=scen: _synth_om(url, s)
            errs = []
            sq = fetch_squall(now, errs, html)
            n0 = sq["nights"][0]
            chk("squall.%s.no_errors" % scen, errs, [])
            chk("squall.%s.two_nights" % scen, len(sq["nights"]), 2)
            chk("squall.%s.source" % scen, sq["source"].startswith("SumatraSquall ssw-"), True)
            chk("squall.%s.level" % scen, n0["level"] in (("likely", "strong") if scen == "squall" else ("low",)), True)
            if scen == "squall":
                chk("squall.penang_signal", sq["nights"][0]["towns"]["penang"]["first"] is not None, True)
                chk("squall.steer_from_sw", 200 <= n0["parts"]["steer"]["from"] <= 290, True)
        # no 850/700 hPa wind: the engine withholds a level rather than guess
        def no_upper(url, timeout=None, retries=None):
            if "850hPa" in url:
                raise urllib.error.HTTPError(url, 503, "busy", {}, None)
            return _synth_om(url, "squall")
        get_json = no_upper
        errs = []
        sq = fetch_squall(now, errs, html)
        chk("squall.no_upper_blocked", (sq["nights"][0]["level"], sq["nights"][0]["blocked"]), (None, "missing"))
        chk("squall.no_upper_is_loud", len(errs), 1)
        chk("squall.broken_page_raises",
            _raises(lambda: fetch_squall(now, [], "<html>nothing</html>")), True)
    finally:
        get_json = real
    return good[0]




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


# GDACS updates a live cyclone every 6 h or so. `iscurrent` alone has been
# seen still "true" on an event whose last episode was 2+ days old (ONE-26,
# mirrored 2026-09-26 with todate 2026-09-24T00:00), so check the date too.
GDACS_MAX_AGE_H = 36


def parse_gdacs(fc, now=None):
    """geoJSON FeatureCollection -> list of current tropical cyclone events."""
    out, problems = [], []
    feats = (fc or {}).get("features")
    if not isinstance(feats, list):
        return [], ["GDACS response had no features list"]
    t_now = _utc(now) if now else None
    for f in feats:
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates")
        if not (isinstance(coords, list) and len(coords) >= 2):
            continue                      # no position -> not usable, skip
        if not _truthy(props.get("iscurrent")):
            continue                      # only live systems
        t_to = _utc(props.get("todate"))
        if t_now is not None and t_to is not None and \
                (t_now - t_to).total_seconds() > GDACS_MAX_AGE_H * 3600:
            print("GDACS: skipped %s — flagged current but last updated %s"
                  % (props.get("eventname") or props.get("eventid"), props.get("todate")))
            continue
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



# ---------------------------------------------------------------- pressure
# Two products, both from Open-Meteo (ECMWF IFS), both plotted as the
# published field. Nothing here classifies anything: no "vortex detected",
# no invented index. Isobars are a standard rendering of a model field, and
# a pressure tendency is an observation, not a diagnosis.
#
#   1. pressure + 24 h change at every reference place  (every run, 30 pts)
#   2. a coarse MSLP grid for contouring                (every 6 h, 208 pts)
#
# The grid is deliberately a Malaysia-focused box rather than the whole
# region frame, to keep the load on a free tier proportionate.

OM = "https://api.open-meteo.com/v1/forecast"
OM_MODEL = "ecmwf_ifs025"

# 1 deg (~110 km), tight on Malaysia and the South China Sea. At the old
# 2 deg a feature needed ~500 km to resolve, so anything vortex-sized came out
# as concentric diamonds. Box reaches 22N to include Hainan and the northern
# SCS, where the surges that reach the east coast originate.
GRID = {"w": 100.0, "e": 122.0, "s": -3.0, "n": 22.0, "step": 1.0}
GRID_EVERY_H = 6          # refresh the grid only on these UTC hours
CHUNK = 50                # locations per HTTP call


def grid_points():
    lats, lons = [], []
    la = GRID["s"]
    while la <= GRID["n"] + 1e-9:
        lo = GRID["w"]
        while lo <= GRID["e"] + 1e-9:
            lats.append(round(la, 2)); lons.append(round(lo, 2))
            lo += GRID["step"]
        la += GRID["step"]
    return lats, lons


def _as_list(payload):
    """Open-Meteo returns an object for one location, a list for many."""
    return payload if isinstance(payload, list) else [payload]


def parse_cnames(html):
    """Read the CNAME keys out of index.html — same single-source-of-truth trick
    as parse_refs. Returns the set of English names the app can render in
    Chinese."""
    m = re.search(r"var CNAME\s*=\s*\{(.*?)\n\};", html, re.S)
    if not m:
        raise ValueError("CNAME block not found in index.html")
    return set(re.findall(r'"([^"]+)"\s*:\s*\[', m.group(1)))


GONE_KEEP_H = 24        # how long a departed system stays listed


def track_gone(storms, prev, now, listed=None):
    """JMA's targetTc.json IS the monitoring list: a system on it is being
    monitored, and when JMA is done the entry disappears. Until now the card
    just vanished from the app, which looks exactly like the app losing track
    of it. So compare this run's ids against the last run's and carry the
    departures for a day, with the time each was last seen. This reports a
    change in what the SOURCE published — it decides nothing about the storm.

    `listed` is the id list from targetTc.json. Departures are judged against
    THAT, never against the storms we happened to download: a timed-out
    detail request is our failure, not JMA dropping the system.
    """
    here = set(listed) if listed is not None else {s.get("id") for s in storms if s.get("id")}
    gone = []
    for g in (prev.get("gone") or []):          # keep recent ones, drop old
        if g.get("id") in here:
            continue
        try:
            age = (datetime.fromisoformat(now.replace("Z", "+00:00"))
                   - datetime.fromisoformat(g["last_seen"].replace("Z", "+00:00")))
            if age.total_seconds() <= GONE_KEEP_H * 3600:
                gone.append(g)
        except Exception:  # noqa: BLE001
            pass
    known = {g.get("id") for g in gone}
    for p in (prev.get("storms") or []):        # on the last list, not on this one
        pid = p.get("id")
        if not pid or pid in here or pid in known:
            continue
        gone.append({"id": pid,
                     "name_en": p.get("name_en"),
                     "typhoon_number": p.get("typhoon_number"),
                     "last_seen": p.get("issued") or prev.get("generated") or now})
    gone.sort(key=lambda g: g.get("last_seen") or "", reverse=True)
    if gone:
        print("gone: %s" % ", ".join((g.get("name_en") or g["id"]) for g in gone))
    return gone


def check_names(storms, html, errors):
    """The Typhoon Committee retires names every year and the replacements can
    be used the moment they are adopted, so a hand-maintained table WILL go out
    of date. The app already fails safe — an unknown name shows romanised only,
    never a transliterated guess — but silently, which is the failure mode this
    project refuses. So the moment JMA uses a name the app cannot render, say
    so in `errors`, which surfaces as the amber banner on the page. That turns
    "remember to check every year" into "the app tells you the day it matters".
    """
    try:
        known = parse_cnames(html)
    except Exception as e:  # noqa: BLE001
        errors.append("name table: %s" % e)
        return
    missing = sorted({s.get("name_en") for s in storms
                      if s.get("name_en") and s["name_en"] not in known})
    for n in missing:
        errors.append("no Chinese name for %s — refresh CNAME from hko.gov.hk" % n)
    if missing:
        print("name table: MISSING %s" % ", ".join(missing))
    else:
        print("name table: %d names, all current storms covered" % len(known))


def parse_refs(html):
    """Read REFS out of index.html so the app stays the single source of truth."""
    m = re.search(r"var REFS = \[(.*?)\n\];", html, re.S)
    if not m:
        raise ValueError("REFS block not found in index.html")
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    out = []
    for row in re.finditer(r"\{([^{}]*)\}", body):
        d = {}
        for k, v in re.findall(r'(\w+)\s*:\s*("[^"]*"|-?[\d.]+)', row.group(1)):
            d[k] = v.strip('"') if v.startswith('"') else float(v)
        if "id" in d and "lat" in d and "lon" in d:
            out.append({"id": d["id"], "lat": d["lat"], "lon": d["lon"]})
    if not out:
        raise ValueError("REFS block parsed to nothing")
    return out


def fetch_series(lats, lons, extra):
    """One or more chunked calls; returns the concatenated per-location list."""
    got = []
    for i in range(0, len(lats), CHUNK):
        la = ",".join(str(x) for x in lats[i:i + CHUNK])
        lo = ",".join(str(x) for x in lons[i:i + CHUNK])
        url = "%s?latitude=%s&longitude=%s&models=%s&timezone=UTC&%s" % (OM, la, lo, OM_MODEL, extra)
        got.extend(_as_list(get_json(url)))
    return got


PRES_MAX_AGE_H = 3       # a "current" reading further than this from now is not current
# Refresh the grid by its AGE, not by the clock. The old rule ("only when the
# UTC hour is divisible by 6") silently skipped every refresh whenever GitHub
# started the run late: a 03:45 slot that actually ran at 05:31 carried a grid
# that was already 17 h old forward again. Runs come every ~3 h, so refreshing
# once the grid is 5 h old keeps the ~6-hourly rhythm and survives lateness.
GRID_REFRESH_H = GRID_EVERY_H - 1


def _utc(s):
    """'2026-08-02T01:00', '...Z' or '...+00:00' -> aware datetime, else None."""
    if not isinstance(s, str) or not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _stamp(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def pick_tendency(entry, now_iso=None, max_age_h=PRES_MAX_AGE_H):
    """Value at the hour nearest now, the change over the preceding 24 h, and
    the valid time of that value.

    Two checks that were missing: the value must actually be CURRENT (within
    max_age_h of now — a series of 2020 timestamps used to be accepted as
    today's reading), and the comparison value must be exactly 24 h earlier
    BY TIMESTAMP, not merely 24 slots back, which a gap in the series breaks.
    """
    h = entry.get("hourly") or {}
    times, vals = h.get("time") or [], h.get("pressure_msl") or []
    if not times or len(times) != len(vals):
        return None, None, None
    now = now_iso or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
    idx = None
    for i, t in enumerate(times):
        if t >= now:
            idx = i
            break
    if idx is None:
        idx = len(times) - 1
    while idx >= 0 and vals[idx] is None:      # step back over gaps
        idx -= 1
    if idx < 0:
        return None, None, None
    t_val, t_now = _utc(times[idx]), _utc(now)
    if t_val is None or t_now is None or \
            abs((t_now - t_val).total_seconds()) > max_age_h * 3600:
        return None, None, None
    cur = float(vals[idx])
    prev = None
    for t, v in zip(times, vals):
        tt = _utc(t)
        if tt is not None and (t_val - tt).total_seconds() == 24 * 3600:
            prev = v
            break
    return cur, (None if prev is None else round(cur - float(prev), 1)), _stamp(t_val)


# The pressure rules can say 转干 (drier). On 2026-09-26 they said it for Penang
# and Alor Setar while the west coast flooded — a 24 h pressure change cannot
# see a squall or a thunderstorm. So "drier" is now only allowed when the SAME
# model also has little rain there over the next 24 h. These two numbers are
# printed in the app next to the rule that uses them.
DRY_RAIN_MM = 5.0        # model total over the next 24 h at or above this: no 转干
DRY_RATE_MM = 2.5        # any single hour at or above this (his "workers stop"): no 转干


def rain_window(entry, valid):
    """Model rain over the 24 h after `valid` (the hour the pressure is read
    at): (total mm, wettest hour mm), or (None, None) unless all 24 hours are
    present — a partial window is not "little rain", it is unknown."""
    h = entry.get("hourly") or {}
    times, vals = h.get("time") or [], h.get("precipitation") or []
    t0 = _utc(valid)
    if t0 is None or not times or len(times) != len(vals):
        return None, None
    got = []
    for t, v in zip(times, vals):
        tt = _utc(t)
        if tt is None:
            continue
        dt = (tt - t0).total_seconds()
        if 0 < dt <= 24 * 3600:
            if v is None:
                return None, None
            got.append(float(v))
    if len(got) != 24:
        return None, None
    return round(sum(got), 1), round(max(got), 1)


def grid_due(prev_grid, now=None):
    """True when the carried grid is missing, undated, or GRID_REFRESH_H old."""
    if not prev_grid:
        return True
    t = _utc(prev_grid.get("time"))
    if t is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - t).total_seconds() >= GRID_REFRESH_H * 3600


def fetch_pressure(refs, want_grid, prev_grid=None, errors=None, now=None):
    """prev_grid is carried forward on runs that do not refresh it — otherwise
    the map loses its isobars for five hours out of every six. A failed grid
    refresh also keeps the old grid (its own `time` says how old it is) rather
    than throwing away the place readings that did arrive."""
    now = now or datetime.now(timezone.utc)
    out = {"model": "ECMWF IFS via Open-Meteo", "places": {}, "grid": prev_grid,
           "time": _stamp(now)}
    entries = fetch_series([r["lat"] for r in refs], [r["lon"] for r in refs],
                           "hourly=pressure_msl,precipitation&past_days=1&forecast_days=2")
    for r, e in zip(refs, entries):
        cur, chg, valid = pick_tendency(e, now.strftime("%Y-%m-%dT%H:00"))
        if cur is not None:
            rain, rmax = rain_window(e, valid)
            out["places"][r["id"]] = {"hpa": round(cur, 1), "change24": chg, "valid": valid,
                                      "rain24": rain, "rainmax": rmax}
    if not out["places"]:
        raise ValueError("no current pressure reading at any reference place")
    if want_grid:
        try:
            out["grid"] = fetch_grid(now)
        except Exception as e:  # noqa: BLE001
            if errors is not None:
                errors.append("pressure grid: %s (kept the one from %s)"
                              % (e, (prev_grid or {}).get("time") or "never"))
    return out


def fetch_grid(now):
    lats, lons = grid_points()
    vals, stamps = [], set()
    for e in fetch_series(lats, lons, "current=pressure_msl"):
        cur = e.get("current") or {}
        v = cur.get("pressure_msl")
        vals.append(None if v is None else round(float(v), 1))
        if cur.get("time"):
            stamps.add(cur["time"])
    # Stamp the grid with the model's own valid time, not with when we ran.
    t = _utc(min(stamps)) if stamps else None
    if t is None:
        t = now.replace(minute=0, second=0, microsecond=0)
    if abs((now - t).total_seconds()) > PRES_MAX_AGE_H * 3600:
        raise ValueError("grid valid time %s is not current" % _stamp(t))
    if len(vals) != len(lats):
        raise ValueError("grid returned %d of %d points" % (len(vals), len(lats)))
    if sum(1 for v in vals if v is not None) < len(vals) * 0.9:
        raise ValueError("grid came back mostly empty")
    return {"w": GRID["w"], "e": GRID["e"], "s": GRID["s"], "n": GRID["n"],
            "step": GRID["step"],
            "nx": int(round((GRID["e"] - GRID["w"]) / GRID["step"])) + 1,
            "ny": int(round((GRID["n"] - GRID["s"]) / GRID["step"])) + 1,
            "values": vals, "time": _stamp(t)}


# ------------------------------------------------------------------ squall
# The Sumatra squall setup index, computed by SumatraSquall's OWN engine. The
# engine is loaded out of that app's published page at run time (see
# scripts/squall.js), so a threshold changed there is used here on the next
# run and the two apps cannot drift. This file only does the networking.

SQUALL_APP = "https://stanleywoosweeleong.github.io/SumatraSquall/"
SQUALL_JS = os.path.join(ROOT, "scripts", "squall.js")
SQUALL_TIMEOUT = 60


def run_squall_js(mode, html_path, stdin=None):
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node is not installed on this runner")
    p = subprocess.run([node, SQUALL_JS, mode, html_path], input=stdin,
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        last = (p.stderr or "").strip().splitlines()
        raise RuntimeError("squall.js %s: %s" % (mode, last[-1] if last else "exit %d" % p.returncode))
    return json.loads(p.stdout)


def _first_ok(options, what):
    """SumatraSquall's own fallback rule: only a rejected model (HTTP 400)
    moves on to the next model id; a busy or dead server does not."""
    last = None
    for mid, url in options:
        try:
            return {"id": mid, "json": get_json(url, timeout=SQUALL_TIMEOUT, retries=2)}
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 400:
                break
        except Exception as e:  # noqa: BLE001
            last = e
            break
    raise RuntimeError("%s: %s" % (what, last))


def fetch_squall(now_ts, errors, html=None):
    """-> the `squall` block for typhoon.json. Raises if the index cannot be
    computed at all; partial data (no 850/700 hPa wind, no towns) is passed
    through exactly as the app treats it — the engine then withholds a level."""
    html = html if html is not None else get(SQUALL_APP + "index.html")
    fd, path = tempfile.mkstemp(suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)
        plan = run_squall_js("plan", path)
        raw = {"now": int(now_ts), "grid": _first_ok(plan["urls"]["grid"], "squall grid")}
        for part in ("upper", "towns"):
            raw[part] = None
            if plan["urls"].get(part):
                try:
                    raw[part] = _first_ok(plan["urls"][part], "squall " + part)
                except Exception as e:  # noqa: BLE001
                    errors.append(str(e))
        return run_squall_js("analyze", path, json.dumps(raw))
    finally:
        os.unlink(path)


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


def target_ids(targets):
    """The ids JMA says it is monitoring right now. This, not the list of
    storms we managed to download, is what decides whether a system has left."""
    return [t.get("tropicalCyclone") for t in targets if t.get("tropicalCyclone")]


def unavailable_record(t, prev_rec, err, now):
    """A storm JMA still lists but whose bulletin we could not download.

    Dropping it (the old behaviour) made track_gone() report it as "dropped
    from JMA's list" — a false statement — and with every download failing
    the page could show no storms while JMA was monitoring several. So keep
    it, and say plainly that this run did not get it:
      * seen before  -> the last good record, flagged unavailable, never
                        presented as current
      * never seen   -> an empty record carrying only what targetTc.json said
    Nothing here is guessed; a placeholder has no position at all.
    """
    tc = t.get("tropicalCyclone")
    if prev_rec and prev_rec.get("id") == tc:
        rec = json.loads(json.dumps(prev_rec))          # deep copy
        rec["problems"] = [p for p in (rec.get("problems") or [])
                           if not str(p).startswith("download failed")]
    else:
        rec = {"id": tc, "parsed": False, "forecast": [], "problems": [],
               "typhoon_number": t.get("typhoonNumber"),
               "category": t.get("category")}
    rec["unavailable"] = True
    rec["unavailable_since"] = (prev_rec or {}).get("unavailable_since") or now
    rec["problems"].append("download failed: %s" % err)
    rec["target_category"] = t.get("category")
    rec["target_issue"] = t.get("issue")
    return rec


def build(targets, fetch_spec, errors, prev_storms=None, now=None):
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    before = {p.get("id"): p for p in (prev_storms or []) if p.get("id")}
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
            storms.append(unavailable_record(t, before.get(tc), e, now))
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
    storms = build(targets, lambda tc: get_json("%s/%s/specifications.json" % (BASE, tc)),
                   errors, prev.get("storms"), now)

    others = []
    try:
        events, probs = parse_gdacs(get_json(GDACS_URL, timeout=GDACS_TIMEOUT,
                                             retries=GDACS_RETRIES), now)
        others = tag_duplicates(events, storms)
        errors.extend(probs)
        print("GDACS current TC events: %d (%d also in JMA)"
              % (len(others), sum(1 for e in others if e.get("also_jma"))))
    except Exception as e:  # noqa: BLE001
        errors.append("GDACS: %s" % e)     # secondary source: never fatal

    try:
        app_html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    except Exception as e:  # noqa: BLE001
        app_html = ""
        errors.append("index.html: %s" % e)
    if app_html:
        check_names(storms, app_html, errors)

    pressure = None
    want_grid = False
    prev_p = prev.get("pressure") or None
    try:
        refs = parse_refs(app_html)
        prev_grid = ((prev_p or {}).get("grid")) or None
        want_grid = grid_due(prev_grid)
        pressure = fetch_pressure(refs, want_grid, prev_grid, errors)
        g = pressure["grid"]
        print("pressure: %d place(s), grid %s (valid %s, %s)" % (
              len(pressure["places"]),
              "%dx%d" % (g["nx"], g["ny"]) if g else "none",
              (g or {}).get("time"),
              "refresh attempted" if want_grid else "carried forward"))
    except Exception as e:  # noqa: BLE001
        errors.append("pressure: %s" % e)      # optional layer, never fatal
        if prev_p:
            # Keep the numbers for reference, but mark them: the app shows
            # them with their age and gives NO wetter/drier reading from them.
            pressure = dict(prev_p)
            pressure["stale"] = True
            pressure.setdefault("stale_since", now)

    squall = None
    prev_sq = prev.get("squall") or None
    if want_grid:
        # Open-Meteo counts every location as a call, 600 a minute on the free
        # tier. The grid just used ~600; the squall set is another ~360. Wait
        # out the minute rather than lose the squall run to HTTP 429. Daily
        # total stays ~5,500 of 10,000 (places 30x8, grid 598x4, squall 363x8).
        time.sleep(61)
    try:
        squall = fetch_squall(datetime.now(timezone.utc).timestamp(), errors)
        n0 = squall["nights"][0]
        print("squall: %s, night of %s -> %s (%s)" % (squall["source"], n0["m0"],
              n0["score"], n0["level"] or n0["blocked"]))
    except Exception as e:  # noqa: BLE001
        errors.append("squall index: %s" % e)
        if prev_sq:
            squall = dict(prev_sq)     # its own `at` dates it; the app ages it out
            squall["stale"] = True

    save({
        "gone": track_gone(storms, prev, now, target_ids(targets)),
        "squall": squall,
        "generated": now,
        "last_attempt": now,
        "fetch_ok": True,
        "source": "Japan Meteorological Agency — RSMC Tokyo Typhoon Center",
        "source_url": "https://www.jma.go.jp/bosai/map.html#contents=typhoon",
        "wind_averaging": "10-minute sustained",
        "storms": storms,
        "pressure": pressure,
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
                       ("Open-Meteo", OM + "?latitude=3.22&longitude=102.42&current=pressure_msl"),
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
    if "--selftest-squall" in sys.argv:
        _ok = [True]

        def _chk(label, got, want):
            print("%s %-26s %r" % ("pass" if got == want else "FAIL", label, got))
            if got != want:
                _ok[0] = False
        r = selftest_squall(_chk)
        print("\n%s" % ("SQUALL TESTS PASSED" if (r and _ok[0]) else "SQUALL TESTS FAILED"))
        sys.exit(0 if (r and _ok[0]) else 1)
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--fixture" in sys.argv:
        sys.exit(fixture())
    sys.exit(main())

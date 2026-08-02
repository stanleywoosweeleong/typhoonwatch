#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_tw.py — regenerate the TWMAP character table inside index.html

Traditional Chinese is derived from the Simplified copy at render time, so
Chinese text has ONE source of truth. This script rebuilds the character map
from index.html's own strings whenever that copy changes.

    pip install opencc-python-reimplemented
    python scripts/make_tw.py            # rewrite TWMAP in place
    python scripts/make_tw.py --check    # fail if TWMAP is out of date

WHY THERE ARE OVERRIDES
OpenCC is right in general and wrong in this domain:
  干  is always "dry" here (轉乾), never 幹
  里  inside a phrase is 裡, but 公里 must keep 里  -> handled by TWPROT
  台  stays 台 in 台湾/台北; 台风 becomes 颱風       -> handled by TWPROT
  钟/为/着/冲/汇/志/湿 pick the form a Taiwanese reader expects
Anything not in the map passes through unchanged, which degrades to readable
Simplified rather than to wrong Traditional.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "index.html")

OVERRIDES = {
    "干": "乾",   # dry, not 幹
    "里": "裡",   # inside; 公里 protected by TWPROT
    "台": "台",   # 台湾/台北; 台风 protected by TWPROT
    "钟": "鐘",   # 分鐘, not 鍾
    "为": "為",
    "着": "著",
    "冲": "沖",   # 沖繩, 沖走
    "汇": "彙",   # 彙整
    "志": "誌",   # 日誌
    "湿": "濕",   # the familiar form; OpenCC prefers 溼
}


def build(html):
    from opencc import OpenCC
    cc = OpenCC("s2t")
    strings = {m.group(1) for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', html)
               if re.search(r"[\u4e00-\u9fff]", m.group(1))}
    chars = {c for s in strings for c in s if "\u4e00" <= c <= "\u9fff"}
    out = {}
    for c in sorted(chars):
        t = OVERRIDES.get(c) or cc.convert(c)
        if t != c and len(t) == 1:
            out[c] = t
    return out, len(strings)


def main():
    html = open(PAGE, encoding="utf-8").read()
    cmap, n = build(html)
    blob = json.dumps(cmap, ensure_ascii=False, separators=(",", ":"))
    m = re.search(r"var TWMAP=(\{.*?\});", html, re.S)
    if not m:
        print("TWMAP block not found in index.html", file=sys.stderr)
        return 2
    if "--check" in sys.argv:
        same = m.group(1) == blob
        print("TWMAP is %s (%d chars from %d strings)"
              % ("up to date" if same else "STALE — run make_tw.py", len(cmap), n))
        return 0 if same else 1
    open(PAGE, "w", encoding="utf-8").write(html[:m.start(1)] + blob + html[m.end(1):])
    print("TWMAP rewritten: %d chars from %d strings" % (len(cmap), n))
    return 0


if __name__ == "__main__":
    sys.exit(main())

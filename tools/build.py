#!/usr/bin/env python3
"""Validate events.json and refresh the built-in snapshot inside index.html.

Run from anywhere:  python3 tools/build.py
Exit code is non-zero if the data is invalid, so a CI job can block a bad update.
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "events.json"
PAGE = ROOT / "index.html"

STATUSES = {"confirmed", "tentative", "awaiting"}
VERIFIED = {"official", "secondary", "aggregator", "tracker"}
BASES = {"expected", "last_edition", "reported"}
DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def fail(errors):
    print("events.json has problems:")
    for e in errors:
        print("  -", e)
    sys.exit(1)


def validate(data):
    errors = []
    meta = data.get("meta", {})
    sectors = {s["id"] for s in meta.get("sectors", [])}
    groups = {g["id"] for g in meta.get("sector_groups", [])}
    regions = set(meta.get("regions", []))
    for s in meta.get("sectors", []):
        if s["group"] not in groups:
            errors.append(f"sector {s['id']}: unknown group {s['group']}")
    seen = set()
    for ev in data.get("events", []):
        i = ev.get("id", "<missing id>")
        if i in seen:
            errors.append(f"{i}: duplicate id")
        seen.add(i)
        for f in ("name", "status", "sectors", "regions"):
            if not ev.get(f):
                errors.append(f"{i}: missing {f}")
        if ev.get("status") not in STATUSES:
            errors.append(f"{i}: bad status {ev.get('status')!r}")
        if ev.get("verified_by") not in VERIFIED:
            errors.append(f"{i}: bad verified_by {ev.get('verified_by')!r}")
        for s in ev.get("sectors", []):
            if s not in sectors:
                errors.append(f"{i}: unknown sector {s}")
        for r in ev.get("regions", []):
            if r not in regions:
                errors.append(f"{i}: unknown region {r}")
        for f in ("start", "end"):
            v = ev.get(f)
            if v is not None and not DATE_RE.match(v):
                errors.append(f"{i}: {f} must be YYYY-MM or YYYY-MM-DD, got {v!r}")
        if ev.get("start") and ev.get("end") and ev["end"] < ev["start"]:
            errors.append(f"{i}: end is before start")
        if ev.get("status") != "awaiting" and not ev.get("start"):
            errors.append(f"{i}: {ev.get('status')} events need a start date")
        for f in ("link", "source_url"):
            v = ev.get(f)
            if v and urlparse(v).scheme not in ("http", "https"):
                errors.append(f"{i}: {f} must be an http(s) URL")
        a = ev.get("audience")
        if a is not None:
            if not isinstance(a.get("value"), int) or a["value"] <= 0:
                errors.append(f"{i}: audience.value must be a positive integer")
            if a.get("basis") not in BASES:
                errors.append(f"{i}: audience.basis must be one of {sorted(BASES)}")
    return errors


def main():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    errors = validate(data)
    if errors:
        fail(errors)
    # Compact snapshot; "</" is escaped so the JSON can never close the script tag.
    snapshot = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = PAGE.read_text(encoding="utf-8")
    pattern = re.compile(r'(<script id="seed" type="application/json">)(.*?)(</script>)', re.S)
    if not pattern.search(page):
        fail(['index.html has no <script id="seed"> block'])
    PAGE.write_text(pattern.sub(lambda m: m.group(1) + snapshot + m.group(3), page, count=1), encoding="utf-8")
    print(f"OK: {len(data['events'])} events validated; snapshot embedded in index.html")


if __name__ == "__main__":
    main()

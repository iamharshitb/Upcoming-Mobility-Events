#!/usr/bin/env python3
"""Free checker for events.json. No API keys, no AI, standard library only.

What it does (settings live in tools/settings.json)
  1. Re-reads each listed event's own page and looks for dates:
       - awaiting event, exactly one future date near its name   -> sets that date (status: tentative)
       - month-level event, exactly one date inside its month     -> sets the exact date
       - exact date still on the page                             -> refreshes last_verified
       - exact date gone, the page shows exactly one other date within 120 days (and the event's name is
         on the page)                                             -> moves the event to the new date (status: tentative, noted)
       - anything else odd (date far away, several dates, page unreadable) -> listed in the report, nothing changed
  2. Reads the listing pages in tools/sources.json: fills dates for awaiting events and follows date changes of
     directory-only events. It adds brand-new events only if "add_new_events" is true in settings.json.
  3. Writes a Markdown report and, when something changed, a dated entry at the top of update-log.md.

Usage:  python3 tools/update.py [--report update-report.md] [--dry-run]
The workflow commits the result straight to the repo, so a bad reading can go live: every automatic change is marked
tentative with a note, and "apply_date_changes": false in settings.json turns the date-moving off.
"""
import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "events.json"
SOURCES = ROOT / "tools" / "sources.json"
SETTINGS = ROOT / "tools" / "settings.json"
LOG = ROOT / "update-log.md"
DEFAULTS = {"add_new_events": True, "apply_date_changes": True}
SHIFT_LIMIT_DAYS = 120  # a genuine reschedule is close to the old date; a date a year away is another edition
UA = "Mozilla/5.0 (compatible; IndiaMobilityEventsCheck/1.0)"
MAX_NEW = 15
MIN_TEXT = 800  # shorter pages are treated as JavaScript-rendered or blocked

MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MON = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
DASH = r"\s*(?:[-–—]|to|&|and)\s*"
DATE_PATTERNS = [
    ("dm-dm", re.compile(rf"\b{DAY}\s+{MON}\.?{DASH}{DAY}\s+{MON}\.?,?\s+(\d{{4}})\b", re.I)),
    ("d-dm", re.compile(rf"\b{DAY}{DASH}{DAY}\s+{MON}\.?,?\s+(\d{{4}})\b", re.I)),
    ("md-md", re.compile(rf"\b{MON}\.?\s+{DAY}{DASH}{MON}\.?\s+{DAY},?\s+(\d{{4}})\b", re.I)),
    ("m-dd", re.compile(rf"\b{MON}\.?\s+{DAY}{DASH}{DAY},?\s+(\d{{4}})\b", re.I)),
    ("dm", re.compile(rf"\b{DAY}\s+{MON}\.?,?\s+(\d{{4}})\b", re.I)),
    ("md", re.compile(rf"\b{MON}\.?\s+{DAY},?\s+(\d{{4}})\b", re.I)),
]

CITIES = {
    "delhi": "Delhi NCR", "new delhi": "Delhi NCR", "noida": "Delhi NCR", "greater noida": "Delhi NCR",
    "gurugram": "Delhi NCR", "gurgaon": "Delhi NCR", "ghaziabad": "Delhi NCR", "faridabad": "Delhi NCR",
    "bengaluru": "Bengaluru", "bangalore": "Bengaluru", "mumbai": "Mumbai", "navi mumbai": "Mumbai",
    "pune": "Pune", "pimpri-chinchwad": "Pune", "chennai": "Chennai",
    "hyderabad": "Other India", "kolkata": "Other India", "ahmedabad": "Other India", "jaipur": "Other India",
    "indore": "Other India", "goa": "Other India", "coimbatore": "Other India", "nagpur": "Other India",
    "bhubaneswar": "Other India", "lucknow": "Other India", "kochi": "Other India", "chandigarh": "Other India",
    "vadodara": "Other India", "surat": "Other India", "visakhapatnam": "Other India", "bhopal": "Other India",
    "nashik": "Other India", "manesar": "Delhi NCR", "aurangabad": "Other India", "mysuru": "Other India",
}
# (sector id, pattern). An event is in scope if at least one pattern matches its title.
SECTOR_RULES = [
    ("ev", r"\b(ev|evs|e-?mobility|electric (?:vehicle|motor|mobility|bus|scooter|two|three)\w*|charging)\b"),
    ("battery", r"\b(batter(?:y|ies)|energy storage|lithium)\b"),
    ("altfuel", r"\b(bio-?fuel|bio-?energy|hydrogen|ethanol|cng|lng|biogas)\b"),
    ("components", r"\b(components?|aftermarket|spare parts|automechanika|autotechnicia|auto ?tech\w*|tyres?|tires?)\b"),
    ("oem", r"\b(auto ?expo|auto ?show|motor ?show|automobile|automotive|vehicles?)\b"),
    ("cv", r"\b(commercial vehicles?|trucks?|buses|bus)\b"),
    ("semis", r"\b(semiconductors?|electronics|pcb)\b"),
    ("software", r"\b(ai|iot|software|connected|autonomous|telematics)\b"),
    ("cyber", r"\bcyber\w*\b"),
    ("mfg", r"\b(manufacturing|automation|industry 4\.0|machine tools?|robotics?)\b"),
    ("logistics", r"\b(logistics|warehous\w*|supply chain|freight|fleet)\b"),
    ("urban", r"\b(urban|smart (?:cit\w+|mobility)|metro)\b"),
]


# ---------------------------------------------------------------- fetching and text
def fetch(url, timeout=25):
    """Return (html, error). Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
                                               "Accept-Language": "en-IN,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(3_000_000)
            charset = r.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace"), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:  # timeouts, DNS, TLS ...
        return None, type(e).__name__


class _Text(HTMLParser):
    """HTML to text. Every tag boundary becomes a space, so '<span>2027</span><span>Pune</span>' doesn't fuse."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        self.out.append(" \n " if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article") else " ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip = max(0, self.skip - 1)
        self.out.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def to_text(raw_html):
    p = _Text()
    try:
        p.feed(raw_html)
    except Exception:
        pass
    text = re.sub(r"[ \t\r\f\v\u00a0]+", " ", "".join(p.out)).strip()
    return re.sub(r"(\d)\s+(st|nd|rd|th)\b", r"\1\2", text)  # "4 <sup>th</sup>" -> "4th"


# ---------------------------------------------------------------- dates
def _mon(s):
    return MONTHS[s[:3].lower()]


def _date(y, m, d):
    try:
        return dt.date(int(y), int(m), int(d))
    except ValueError:
        return None


def extract_dates(text, today):
    """Future date ranges found in text: [{'start': date, 'end': date, 'pos': int}]"""
    found, claimed = [], []
    for kind, rx in DATE_PATTERNS:
        for m in rx.finditer(text):
            if any(m.start() < b and a < m.end() for a, b in claimed):
                continue
            g = m.groups()
            if kind == "dm-dm":
                s, e = _date(g[4], _mon(g[1]), g[0]), _date(g[4], _mon(g[3]), g[2])
            elif kind == "d-dm":
                s, e = _date(g[3], _mon(g[2]), g[0]), _date(g[3], _mon(g[2]), g[1])
            elif kind == "md-md":
                s, e = _date(g[4], _mon(g[0]), g[1]), _date(g[4], _mon(g[2]), g[3])
            elif kind == "m-dd":
                s, e = _date(g[3], _mon(g[0]), g[1]), _date(g[3], _mon(g[0]), g[2])
            elif kind == "dm":
                s = e = _date(g[2], _mon(g[1]), g[0])
            else:
                s = e = _date(g[2], _mon(g[0]), g[1])
            if not s or not e or e < s or (e - s).days > 14:
                continue
            claimed.append((m.start(), m.end()))
            if e >= today and s <= today + dt.timedelta(days=730):
                found.append({"start": s, "end": e, "pos": m.start()})
    return sorted(found, key=lambda c: c["pos"])


def fmt_range(s, e):
    if s == e:
        return f"{s.day} {s:%b %Y}"
    if (s.month, s.year) == (e.month, e.year):
        return f"{s.day}-{e.day} {s:%b %Y}"
    return f"{s.day} {s:%b} - {e.day} {e:%b %Y}"


# ---------------------------------------------------------------- known events
def key_of(name):
    n = re.sub(r"\(.*?\)", " ", name)
    n = re.sub(r"\b(\d{4}|\d+(?:st|nd|rd|th)|edition)\b", " ", n, flags=re.I)
    stop = {"the", "of", "and", "for", "in", "at", "by"}
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9&'+-]+", n) if w.lower() not in stop]
    return " ".join(words[:3]).lower()


def near_dates(text, candidates, name):
    """(candidates near the event's name, whether the name was found on the page)"""
    key = key_of(name)
    low = text.lower()
    if len(key) < 6 or key not in low:
        return candidates, False
    spots = [m.start() for m in re.finditer(re.escape(key), low)][:40]
    return [c for c in candidates if any(abs(c["pos"] - p) <= 350 for p in spots)], True


def is_listing_url(url):
    u = urllib.parse.urlparse(url)
    return (u.netloc.endswith("tradeindia.com") and re.match(r"^/tradeshows/(automobile|industrial|city)", u.path)) \
        or u.netloc.endswith("10times.com")


def page_url_for(ev):
    for u in (ev.get("link"), ev.get("source_url")):
        if u and not is_listing_url(u):
            return u
    return None


STALE = re.compile(r"[^.]*\b(?:not (?:yet )?announced|not been announced|no later edition|dates? (?:are )?not (?:yet )?(?:published|announced)|no 6th edition)[^.]*\.\s*", re.I)


CHANGED = re.compile(r"Date changed from .*?please confirm\.\s*", re.I)


def apply_change(ev, old, new, url, today, link_if_missing=False):
    """Move an event to a new date range. Always downgraded to tentative and annotated."""
    (old_s, old_e), (s, e) = old, new
    ev["start"], ev["end"], ev["status"] = s.isoformat(), e.isoformat(), "tentative"
    if ev.get("verified_by") in ("tracker", "aggregator"):
        ev["verified_by"] = "secondary" if "tradeindia" not in url and "10times" not in url else "aggregator"
    ev["source_url"], ev["last_verified"] = url, today.isoformat()
    if link_if_missing and not ev.get("link"):
        ev["link"] = url
    base = STALE.sub("", CHANGED.sub("", ev.get("notes") or "")).strip()
    what = "Date set to" if old_s is None else f"Date changed from {fmt_range(old_s, old_e)} to"
    ev["notes"] = (base + f" {what} {fmt_range(s, e)} (auto-detected {today:%d %b %Y}); please confirm.").strip()


def check_event(ev, text, cands, today, rep, cfg=DEFAULTS):
    name, url = ev["name"], page_url_for(ev)
    if len(text) < MIN_TEXT:
        rep["review"].append(f"**{name}**: page looks JavaScript-rendered or blocked, so it can't be checked by script. Check by hand: {url}")
        return
    near, found_name = near_dates(text, cands, name)
    distinct = sorted({(c["start"], c["end"]) for c in near})
    start = ev.get("start")
    label = "" if found_name else " (event name not found on the page, so lower confidence)"

    if not start:  # awaiting
        if len(distinct) == 1:
            s, e = distinct[0]
            ev["start"], ev["end"], ev["status"] = s.isoformat(), e.isoformat(), "tentative"
            if ev.get("verified_by") in ("tracker", "aggregator"):
                ev["verified_by"] = "secondary"
            ev["source_url"], ev["last_verified"] = url, today.isoformat()
            ev["notes"] = (STALE.sub("", ev.get("notes") or "").strip() + f" Auto-detected {fmt_range(s, e)} on the page; please confirm.").strip()
            rep["proposed"].append(f"**{name}**: new date {fmt_range(s, e)}{label} — [source]({url})")
        elif len(distinct) > 1:
            rep["review"].append(f"**{name}**: several future dates on the page ({', '.join(fmt_range(*d) for d in distinct[:4])}); pick by hand — {url}")
        else:
            rep["nochange"] += 1
        return

    exact = len(start) == 10
    cur_s = dt.date.fromisoformat(start if exact else start + "-01")
    if exact:
        cur_e = dt.date.fromisoformat(ev.get("end") or start)
        if any(c["start"] == cur_s for c in near):
            ev["last_verified"] = today.isoformat()
            rep["still_ok"] += 1
        elif len(distinct) == 1:
            s, e = distinct[0]
            if cfg["apply_date_changes"] and found_name and abs((s - cur_s).days) <= SHIFT_LIMIT_DAYS:
                apply_change(ev, (cur_s, cur_e), (s, e), url, today)
                rep["proposed"].append(f"**{name}**: date changed from {fmt_range(cur_s, cur_e)} to {fmt_range(s, e)} — [source]({url})")
            else:
                rep["review"].append(f"**{name}**: date may have changed. Now {fmt_range(cur_s, cur_e)}, page shows {fmt_range(s, e)}{label} — {url}")
        else:
            rep["review"].append(f"**{name}**: {fmt_range(cur_s, cur_e)} is not visible on the page any more — {url}")
        return

    end_key = (ev.get("end") or start)
    in_range = [d for d in distinct if start <= d[0].strftime("%Y-%m") <= end_key]
    if len(in_range) == 1:
        s, e = in_range[0]
        ev["start"], ev["end"], ev["source_url"], ev["last_verified"] = s.isoformat(), e.isoformat(), url, today.isoformat()
        ev["notes"] = ((ev.get("notes") or "").strip() + f" Exact date auto-detected ({fmt_range(s, e)}); please confirm.").strip()
        rep["proposed"].append(f"**{name}**: exact date {fmt_range(s, e)} (was month-level){label} — [source]({url})")
    elif len(in_range) > 1:
        rep["review"].append(f"**{name}**: several dates in its month on the page; pick by hand — {url}")
    else:
        rep["nochange"] += 1


# ---------------------------------------------------------------- new listings
def norm(name):
    n = re.sub(r"\(.*?\)", " ", name.lower())
    n = re.sub(r"\b(\d{4}|\d+(?:st|nd|rd|th)|edition|india|indian|the|in)\b", " ", n)
    return re.sub(r"[^a-z0-9]+", "", n)


def same_event(a, b):
    a, b = norm(a), norm(b)
    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 6 and (a in b or b in a)))


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def clean_title(t):
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"\s+in\s+[A-Za-z .-]+\s+India$", "", t, flags=re.I).strip()


def find_city(*chunks):
    blob = " ".join(chunks).lower()
    best = None
    for c in sorted(CITIES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(c)}\b", blob):
            best = c
            break
    return best


def sectors_for(title):
    hits = [s for s, rx in SECTOR_RULES if re.search(rx, title, re.I)]
    return hits[:3]


def parse_listing(raw, base, today):
    """Items from anchors to /tradeshows/<id>/<slug>.html.

    Each item's dates and city come only from its own card: the HTML from its first anchor up to the next
    event's anchor (max 1500 chars). If the layout doesn't allow that, items simply come out without a date
    and are skipped and listed in the report, instead of picking up a neighbour's details.
    """
    rx = re.compile(r'<a\b[^>]*href="([^"]*?/tradeshows/\d+/[^"]+?\.html)"[^>]*>(.*?)</a>', re.I | re.S)
    ms = [(urllib.parse.urljoin(base, m.group(1)), m.start(), m.group(2)) for m in rx.finditer(raw)]
    items, seen = [], set()
    for i, (href, start, _) in enumerate(ms):
        if href in seen:
            continue
        j = i + 1
        while j < len(ms) and ms[j][0] == href:
            j += 1
        stop = ms[j][1] if j < len(ms) else len(raw)
        inner = [x[2] for x in ms[i:j]]
        title = max((clean_title(t) for t in inner), key=len)
        if len(title) < 4:
            continue
        seen.add(href)
        card = to_text(raw[start: min(stop, start + 1500)])
        dates = extract_dates(card, today)
        raw_title = max((html.unescape(re.sub(r"<[^>]+>", " ", t)) for t in inner), key=len)
        items.append({"title": title, "url": href, "dates": dates[0] if dates else None,
                      "city": find_city(raw_title, card)})
    return items


def add_listings(data, listings, today, rep, cfg=DEFAULTS):
    events = data["events"]
    added = 0
    for src_url, raw in listings:
        for it in parse_listing(raw, src_url, today):
            name = it["title"]
            match = next((e for e in events if same_event(e["name"], name)), None)
            if match:
                d = it["dates"]
                if not d:
                    continue
                if not match.get("start"):  # awaiting: the directory now shows a date
                    if cfg["apply_date_changes"] and d["start"] <= today + dt.timedelta(days=548):
                        apply_change(match, (None, None), (d["start"], d["end"]), it["url"], today, link_if_missing=True)
                        rep["proposed"].append(f"**{match['name']}**: new date {fmt_range(d['start'], d['end'])} from the directory listing — [listing]({it['url']})")
                elif match.get("verified_by") == "aggregator" and len(match["start"]) == 10 and match["start"] != d["start"].isoformat():
                    old_s = dt.date.fromisoformat(match["start"])
                    old_e = dt.date.fromisoformat(match.get("end") or match["start"])
                    if cfg["apply_date_changes"] and abs((d["start"] - old_s).days) <= SHIFT_LIMIT_DAYS:
                        apply_change(match, (old_s, old_e), (d["start"], d["end"]), it["url"], today)
                        rep["proposed"].append(f"**{match['name']}**: date changed from {fmt_range(old_s, old_e)} to {fmt_range(d['start'], d['end'])} (directory listing) — [listing]({it['url']})")
                    else:
                        rep["review"].append(f"**{match['name']}**: listing now shows {fmt_range(d['start'], d['end'])}, we have {match['start']} — {it['url']}")
                continue
            if not cfg["add_new_events"]:
                continue
            secs = sectors_for(name)
            if not secs:
                continue  # not a mobility topic
            if not it["city"]:
                rep["skipped"].append(f"{name} (city not recognised as Indian)")
                continue
            if not it["dates"] or it["dates"]["start"] > today + dt.timedelta(days=548):
                rep["skipped"].append(f"{name} (no usable date on the listing)")
                continue
            if added >= MAX_NEW:
                rep["skipped"].append(f"{name} (over the {MAX_NEW}-per-run cap)")
                continue
            s, e = it["dates"]["start"], it["dates"]["end"]
            base_id, n = slug(name), 1
            eid = base_id
            while any(x["id"] == eid for x in events):
                n += 1
                eid = f"{base_id}-{n}"
            events.append({
                "id": eid, "name": name, "start": s.isoformat(), "end": e.isoformat(),
                "location": it["city"].title(), "regions": [CITIES[it["city"]]], "sectors": secs,
                "organiser": None, "link": it["url"], "status": "tentative", "verified_by": "aggregator",
                "source_url": it["url"], "audience": None, "last_edition": None,
                "notes": "Auto-added from a directory listing; organiser, venue and audience not stated. Sectors guessed from the title. Check the link before merging.",
                "last_verified": today.isoformat(), "first_seen": today.isoformat(), "approved": True,
            })
            rep["new"].append(f"**{name}** — {fmt_range(s, e)}, {it['city'].title()} — [listing]({it['url']}) · sectors: {', '.join(secs)}")
            added += 1


# ---------------------------------------------------------------- main
def run(data, sources, today, fetcher=fetch, pause=1.0, settings=None):
    cfg = {**DEFAULTS, **(settings or {})}
    rep = {"proposed": [], "new": [], "review": [], "skipped": [], "fetch_failed": [], "nochange": 0, "still_ok": 0, "nopage": []}
    cache = {}

    def get(url):
        if url not in cache:
            raw, err = fetcher(url)
            cache[url] = (raw, err)
            if pause:
                time.sleep(pause)
        return cache[url]

    page_cache, fails = {}, {}
    for ev in data["events"]:
        if ev.get("approved") is False:
            continue
        if ev.get("end") and ev["end"] < today.isoformat()[:len(ev["end"])]:
            rep["ended"] = rep.get("ended", 0) + 1  # already over: nothing to check
            continue
        url = page_url_for(ev)
        if not url:
            rep["nopage"].append(ev["name"])
            continue
        raw, err = get(url)
        if raw is None:
            fails.setdefault(url, [err, []])[1].append(ev["name"])
            continue
        if url not in page_cache:
            text = to_text(raw)
            page_cache[url] = (text, extract_dates(text, today))
        text, cands = page_cache[url]
        check_event(ev, text, cands, today, rep, cfg)

    for url, (err, names) in fails.items():
        more = "…" if len(names) > 3 else ""
        rep["fetch_failed"].append(f"{url} ({err}), used by {len(names)} event{'s' if len(names) != 1 else ''}: {', '.join(names[:3])}{more}")
    rep["urls_failed"], rep["urls_total"] = len(fails), len({page_url_for(e) for e in data["events"] if page_url_for(e)})
    listings = []
    for s in sources.get("listings", []):
        raw, err = get(s["url"])
        if raw is None:
            rep["fetch_failed"].append(f"listing {s['url']} ({err})")
        else:
            listings.append((s["url"], raw))
    add_listings(data, listings, today, rep, cfg)
    data["meta"]["generated"] = today.isoformat()
    return rep


def render_report(rep, today):
    def sec(title, rows):
        return [f"### {title} ({len(rows)})", *(f"- {r}" for r in rows), ""] if rows else []
    warn = []
    if rep.get("urls_total") and rep.get("urls_failed", 0) * 2 >= rep["urls_total"]:
        warn = [f"> **Warning:** {rep['urls_failed']} of {rep['urls_total']} event pages could not be fetched. GitHub's servers may be blocked by these sites, so treat this month's result as incomplete.", ""]
    lines = [f"## Events check, {today:%d %b %Y}", "", *warn,
             "Changes under *Dates updated* and *New events added* were applied automatically and are marked tentative in the data. "
             "Items under *Needs a look* were left unchanged.", "",
             f"{rep['still_ok']} exact dates still visible on their pages, {rep['nochange']} pages with nothing new.", ""]
    lines += sec("New events added", rep["new"])
    lines += sec("Dates updated", rep["proposed"])
    lines += sec("Needs a look (nothing was changed)", rep["review"])
    lines += sec("Could not be fetched", rep["fetch_failed"])
    lines += sec("No page to check", rep["nopage"])
    lines += sec("Listing items skipped", rep["skipped"][:25])
    if rep["fetch_failed"]:
        lines += ["Sites that block scripts or need JavaScript can't be read by this check; ask Claude for a deeper refresh every few months to cover them.", ""]
    return "\n".join(lines)


LOG_HEAD = "# Update log\n\nChanges the automatic check made to events.json, newest first. Everything listed here is marked tentative on the site until you confirm it.\n\n"


def log_entry(rep, today):
    rows = []
    for title, key in (("Dates updated", "proposed"), ("New events added", "new")):
        if rep.get(key):
            rows += [f"**{title}**", *(f"- {r}" for r in rep[key]), ""]
    return f"## {today:%d %b %Y}\n\n" + "\n".join(rows) + "\n" if rows else ""


def update_log(path, entry, today):
    if not entry:
        return
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    body = old[len(LOG_HEAD):] if old.startswith(LOG_HEAD) else old
    # a second run on the same day replaces that day's entry
    body = re.sub(rf"^## {today:%d %b %Y}\n.*?(?=^## |\Z)", "", body, flags=re.S | re.M)
    lines = (entry + body).splitlines()[:400]
    path.write_text(LOG_HEAD + "\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="update-report.md")
    ap.add_argument("--dry-run", action="store_true", help="print the report but don't write events.json")
    a = ap.parse_args()
    today = dt.date.today()
    data = json.loads(DATA.read_text(encoding="utf-8"))
    sources = json.loads(SOURCES.read_text(encoding="utf-8")) if SOURCES.exists() else {}
    settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    rep = run(data, sources, today, settings=settings)
    report = render_report(rep, today)
    print(report)
    Path(a.report).write_text(report + "\n", encoding="utf-8")
    if not a.dry_run:
        DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        update_log(LOG, log_entry(rep, today), today)
    return 0


if __name__ == "__main__":
    sys.exit(main())

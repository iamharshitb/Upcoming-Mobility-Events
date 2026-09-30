#!/usr/bin/env python3
"""Offline tests for tools/update.py.  Run: python3 tools/test_update.py"""
import copy
import datetime as dt
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update as u  # noqa: E402

TODAY = dt.date(2026, 9, 30)
D = dt.date


def one(text):
    r = u.extract_dates(text, TODAY)
    return [(c["start"], c["end"]) for c in r]


class Dates(unittest.TestCase):
    def test_formats(self):
        cases = {
            "4-9 February 2027": [(D(2027, 2, 4), D(2027, 2, 9))],
            "4th to 9th February 2027": [(D(2027, 2, 4), D(2027, 2, 9))],
            "February 4-9, 2027": [(D(2027, 2, 4), D(2027, 2, 9))],
            "17–19 Nov 2026": [(D(2026, 11, 17), D(2026, 11, 19))],
            "30 Oct – 2 Nov 2026": [(D(2026, 10, 30), D(2026, 11, 2))],
            "Oct 30 - Nov 2, 2026": [(D(2026, 10, 30), D(2026, 11, 2))],
            "9 October 2026": [(D(2026, 10, 9), D(2026, 10, 9))],
            "October 9, 2026": [(D(2026, 10, 9), D(2026, 10, 9))],
            "22 - 24 Oct, 2026": [(D(2026, 10, 22), D(2026, 10, 24))],
            "Sept 5 2026": [],  # past
            "45 February 2027": [],  # invalid day
            "1-2 Jan 2020": [],  # past
            "1 Jan 2031": [],  # too far out
        }
        for text, want in cases.items():
            self.assertEqual(one(text), want, text)

    def test_no_double_count(self):
        self.assertEqual(one("Held 4-9 February 2027 at Bharat Mandapam"), [(D(2027, 2, 4), D(2027, 2, 9))])

    def test_inline_tags_do_not_fuse_text(self):
        self.assertEqual(one(u.to_text("<span>Pune India</span><span>5-7 Feb 2027</span><span>Pune</span>")), [(D(2027, 2, 5), D(2027, 2, 7))])
        self.assertEqual(one(u.to_text("<b>4</b><sup>th</sup> to <b>9</b><sup>th</sup> February 2027")), [(D(2027, 2, 4), D(2027, 2, 9))])

    def test_html_to_text_skips_scripts(self):
        t = u.to_text("<div>Hello</div><script>var d='1 Jan 2027'</script><p>World</p>")
        self.assertIn("Hello", t)
        self.assertIn("World", t)
        self.assertNotIn("var d", t)


def ev(**kw):
    base = {"id": "x", "name": "Example Mobility Expo 2027", "start": None, "end": None, "location": "Delhi", "regions": ["Delhi NCR"],
            "sectors": ["ev"], "organiser": "Org", "link": "https://example.org/expo", "status": "awaiting",
            "verified_by": "tracker", "source_url": "https://example.org/expo", "audience": None,
            "last_edition": None, "notes": "Next edition not announced.", "last_verified": None,
            "first_seen": "2026-09-30", "approved": True}
    base.update(kw)
    return base


def page(body):
    return "<html><body>" + "<p>filler text about the industry and its exhibitors.</p>" * 30 + body + "</body></html>"


def rep():
    return {"proposed": [], "new": [], "review": [], "skipped": [], "fetch_failed": [], "nochange": 0, "still_ok": 0, "nopage": []}


def run_with(events, pages, listings=None):
    data = {"meta": {"generated": "2026-01-01"}, "events": copy.deepcopy(events)}
    fetcher = lambda url: (pages.get(url), None if url in pages else "HTTP 404")  # noqa: E731
    sources = {"listings": [{"url": x} for x in (listings or {})]}
    pages = {**pages, **(listings or {})}
    r = u.run(data, sources, TODAY, fetcher=fetcher, pause=0)
    return data, r


class KnownEvents(unittest.TestCase):
    URL = "https://example.org/expo"

    def test_awaiting_single_date_is_proposed(self):
        d, r = run_with([ev()], {self.URL: page("<h1>Example Mobility Expo 2027</h1><p>Dates: 12-14 March 2027, Pune</p>")})
        e = d["events"][0]
        self.assertEqual((e["start"], e["end"], e["status"]), ("2027-03-12", "2027-03-14", "tentative"))
        self.assertEqual(e["verified_by"], "secondary")
        self.assertNotIn("not announced", e["notes"])
        self.assertEqual(len(r["proposed"]), 1)

    def test_awaiting_several_dates_is_review_only(self):
        d, r = run_with([ev()], {self.URL: page("<p>Example Mobility Expo 2027 on 12-14 March 2027 or 20-22 April 2027</p>")})
        self.assertIsNone(d["events"][0]["start"])
        self.assertEqual(len(r["review"]), 1)

    def test_other_events_dates_on_a_shared_page_are_ignored(self):
        body = "<p>Example Mobility Expo 2027: date to be announced.</p>" + "<p>filler</p>" * 200 + "<p>Totally Different Summit 5-6 May 2027</p>"
        d, r = run_with([ev()], {self.URL: page(body)})
        self.assertIsNone(d["events"][0]["start"])
        self.assertEqual(r["nochange"], 1)

    def test_exact_date_still_present_refreshes_last_verified(self):
        e = ev(start="2027-03-12", end="2027-03-14", status="confirmed")
        d, r = run_with([e], {self.URL: page("<p>Example Mobility Expo 2027, 12-14 March 2027</p>")})
        self.assertEqual(d["events"][0]["last_verified"], "2026-09-30")
        self.assertEqual(r["still_ok"], 1)

    def test_changed_date_is_reported_not_applied(self):
        e = ev(start="2027-03-12", end="2027-03-14", status="confirmed")
        d, r = run_with([e], {self.URL: page("<p>Example Mobility Expo 2027, 19-21 March 2027</p>")})
        self.assertEqual(d["events"][0]["start"], "2027-03-12")
        self.assertIn("may have changed", r["review"][0])

    def test_month_level_becomes_exact(self):
        e = ev(start="2027-03", end="2027-03", status="tentative")
        d, r = run_with([e], {self.URL: page("<p>Example Mobility Expo 2027 — 12-14 March 2027</p>")})
        self.assertEqual(d["events"][0]["start"], "2027-03-12")
        self.assertEqual(d["events"][0]["status"], "tentative")

    def test_js_rendered_page_flagged(self):
        d, r = run_with([ev()], {self.URL: "<html><body><div id=app></div></body></html>"})
        self.assertIn("JavaScript", r["review"][0])

    def test_fetch_failure_is_reported_and_data_kept(self):
        d, r = run_with([ev()], {})
        self.assertEqual(len(r["fetch_failed"]), 1)
        self.assertIsNone(d["events"][0]["start"])

    def test_event_without_a_page_is_listed(self):
        d, r = run_with([ev(link=None, source_url=None)], {})
        self.assertEqual(r["nopage"], ["Example Mobility Expo 2027"])


LISTING = """
<div class="card"><a href="/tradeshows/900001/india-ev-charging-expo-2027.html"><h3>India EV Charging Expo 2027 in Hyderabad India</h3></a>
  <span>12-14 Jan 2027</span><span>Hyderabad, India</span></div>
<div class="card"><a href="/tradeshows/900002/water-expo-2027.html">WATER EXPO 2027 in Pune India</a><span>5-7 Feb 2027</span></div>
<div class="card"><a href="/tradeshows/900003/china-battery-fair-2027.html">China Battery Fair 2027</a><span>1-3 Mar 2027</span><span>Wenzhou, China</span></div>
<div class="card"><a href="/tradeshows/900004/bengaluru-logistics-summit-2027.html">Bengaluru Logistics Summit 2027</a><span>Mar 10-11, 2027</span><span>Bengaluru</span></div>
<div class="card"><a href="/tradeshows/900005/battery-show-india-2026.html">The Battery Show India 2026 in Greater Noida India</a><span>22-24 Oct 2026</span></div>
<div class="card"><a href="/tradeshows/900006/india-electric-motor-expo-2026.html">India Electric Motor Expo 2026 in Pune India</a><span>16-18 Oct 2026</span></div>
<div class="card"><a href="/tradeshows/900007/ev-fleet-forum-2027.html"><img src="x.png"></a><span>3-4 Apr 2027</span><span>Chennai</span><a href="/tradeshows/900007/ev-fleet-forum-2027.html">EV Fleet Forum 2027</a></div>
"""


class Listings(unittest.TestCase):
    BASE = "https://www.tradeindia.com/tradeshows/automobile/"

    def setUp(self):
        self.existing = [
            ev(id="bs", name="The Battery Show India 2026 (4th edition)", start="2026-10-22", end="2026-10-24", status="confirmed", verified_by="official", link="https://www.thebatteryshowindia.com/", source_url="https://www.thebatteryshowindia.com/"),
            ev(id="em", name="India Electric Motor Expo 2026", start="2026-10-15", end="2026-10-17", status="tentative", verified_by="aggregator", link=self.BASE, source_url=self.BASE),
        ]

    def test_new_indian_mobility_events_added_others_skipped(self):
        d, r = run_with(self.existing, {"https://www.thebatteryshowindia.com/": page("<p>The Battery Show India 2026 22-24 October 2026</p>")}, {self.BASE: LISTING})
        names = [e["name"] for e in d["events"]]
        self.assertIn("India EV Charging Expo 2027", names)
        self.assertIn("Bengaluru Logistics Summit 2027", names)
        self.assertNotIn("WATER EXPO 2027", names)  # not mobility
        self.assertNotIn("China Battery Fair 2027", names)  # not in India
        self.assertEqual(sum(n.startswith("The Battery Show") for n in names), 1)  # duplicate not re-added
        new = next(e for e in d["events"] if e["name"] == "India EV Charging Expo 2027")
        self.assertEqual((new["start"], new["regions"], new["verified_by"], new["status"]), ("2027-01-12", ["Other India"], "aggregator", "tentative"))
        self.assertIn("ev", new["sectors"])
        img_first = next(e for e in d["events"] if e["name"] == "EV Fleet Forum 2027")  # date sits between image and title anchors
        self.assertEqual((img_first["start"], img_first["regions"]), ("2027-04-03", ["Chennai"]))
        self.assertTrue(any("China Battery" in s for s in r["skipped"]))

    def test_changed_listing_date_for_aggregator_event_is_reported(self):
        d, r = run_with(self.existing, {"https://www.thebatteryshowindia.com/": page("<p>The Battery Show India 2026 22-24 October 2026</p>")}, {self.BASE: LISTING})
        self.assertTrue(any("India Electric Motor Expo" in x and "listing now shows" in x for x in r["review"]))
        self.assertEqual(next(e for e in d["events"] if e["id"] == "em")["start"], "2026-10-15")  # unchanged

    def test_rerun_adds_nothing_twice(self):
        d, _ = run_with(self.existing, {"https://www.thebatteryshowindia.com/": page("x")}, {self.BASE: LISTING})
        d2, r2 = run_with(d["events"], {"https://www.thebatteryshowindia.com/": page("x")}, {self.BASE: LISTING})
        self.assertEqual(r2["new"], [])
        self.assertEqual(len(d2["events"]), len(d["events"]))

    def test_output_still_passes_build_validation(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import build
        real = json.loads((Path(__file__).resolve().parent.parent / "events.json").read_text(encoding="utf-8"))
        data = {"meta": real["meta"], "events": real["events"] + []}
        listing_pages = {self.BASE: LISTING}
        fetcher = lambda url: (listing_pages.get(url), None if url in listing_pages else "HTTP 404")  # noqa: E731
        u.run(data, {"listings": [{"url": self.BASE}]}, TODAY, fetcher=fetcher, pause=0)
        self.assertEqual(build.validate(data), [])


class Report(unittest.TestCase):
    def test_report_renders(self):
        r = rep()
        r["new"].append("**A** — 1 Jan 2027")
        r["fetch_failed"].append("B (HTTP 403)")
        text = u.render_report(r, TODAY)
        self.assertIn("New events added (1)", text)
        self.assertIn("Could not be fetched (1)", text)


if __name__ == "__main__":
    unittest.main(verbosity=1)

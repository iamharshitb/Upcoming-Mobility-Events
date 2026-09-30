# India Mobility Events

A filterable list of upcoming mobility-ecosystem events in India. Static site: plain HTML/CSS/JS, no build step, no external services.

```
index.html      the whole web app (reads events.json; keeps a built-in snapshot as a fallback)
events.json     the data: one record per event
tools/build.py  validates events.json and refreshes the snapshot inside index.html
tools/update.py the free monthly checker (no API keys, standard library only)
tools/sources.json  listing pages the checker scans for new events
tools/test_update.py  offline tests for the checker
.github/workflows/monthly-update.yml  runs the checker on the 1st of every month
```

## Put it online (GitHub Pages)

1. Create a repo and add these files (keep the `tools/` folder).
2. Repo Settings > Pages > Deploy from branch > `main` / root.
3. Open the Pages URL. Filters are shareable: the URL hash records them (for example `#s=ev,battery&m=2026-11`).

To preview locally, run `python3 -m http.server` in this folder and open http://localhost:8000. Opening `index.html` straight from disk works too, but shows the built-in snapshot with a banner.

## Updating the data by hand

Edit `events.json`, run `python3 tools/build.py` (it fails loudly on bad data), commit and push.

| Field | Meaning |
|---|---|
| `start`, `end` | `YYYY-MM-DD` for exact dates, `YYYY-MM` for month-level. `null` when the next edition isn't announced. Events disappear from the page the day after `end`. |
| `status` | `confirmed` = exact dates from the organiser or two independent sources. `tentative` = month-level, or exact from one non-organiser source. `awaiting` = recurring event, next edition not announced. |
| `verified_by` | `official` (organiser site/calendar), `secondary` (trade press, exhibitor, listing page), `aggregator` (directory only), `tracker` (carried over from the team sheet, not re-checked). |
| `audience` | `{value, basis, note}`; basis is `expected`, `last_edition` or `reported`. Leave `null` when no figure was found. |
| `sectors`, `regions` | Ids from `meta.sectors` and `meta.regions`. Add a sector by adding it to `meta.sectors`; the filters pick it up. |
| `approved` | `false` hides an event without deleting it. |

## Monthly auto-check (free, no API keys)

On the 1st of every month (09:00 IST) a GitHub Action runs `tools/update.py`, which:

- re-reads each event's own page and looks for dates: proposes a date for "awaiting" events, an exact date for month-level ones, and refreshes `last_verified` when the current date is still on the page;
- scans the listing pages in `tools/sources.json` for events that aren't here yet and adds the ones that look like mobility events, in India, with a date in the next 18 months;
- opens a **pull request** with a report. Merging it is the approval and publishes the changes; nothing goes live otherwise.

Anything odd (a date that seems to have changed, several dates on a page, a page that needs JavaScript, a site that blocks the check) is listed in the report and left unchanged.

**One-time setup:** repo Settings > Actions > General > Workflow permissions: choose *Read and write permissions* and tick *Allow GitHub Actions to create and approve pull requests*. Then open the Actions tab > *Monthly events check* > *Run workflow* once to see the first report.

**Your monthly routine (about 5 minutes):** open the pull request, click the source links for anything added or changed, delete or fix lines you don't trust (edit `events.json` in the branch), then merge.

**Limits:** a plain script can't read sites that need JavaScript or block bots, can't judge context like a person, and leaves organiser and audience blank for new listings. Every few months, ask Claude for a deeper refresh (it can read the sites the script can't) and commit the updated `events.json`.

**Keep it alive:** GitHub pauses scheduled workflows in public repos after 60 days without repo activity. The job opens a pull request every month (it always updates the "last checked" date), so merging it counts as activity. If you ever stop merging, re-enable the workflow from the Actions tab.

Add a listing source by adding its URL to `tools/sources.json` (TradeIndia-style listing pages work best). Run the checker's tests with `python3 tools/test_update.py`.

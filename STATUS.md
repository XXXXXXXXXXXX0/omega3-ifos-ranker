# Status

Last updated: 2026-11-25. Branch: `claude/keen-ptolemy-j47bf2`.

## Resume here

All stages are done; the draft pull request to `main` is open. After review
the score changed to per capsule (D-35): 83 ranked, 10 flavored. The top 10 is
provisional only because 21 contenders have no online source for their oil
form (D-36). D-33 is resolved.

To continue label verification later: `python -m omega3 agent-queue` lists
what is waiting; with an API key, `llm.api_enabled: true` and
`python -m omega3 agent-run` does it automatically.

## Stages

| Stage | State | Tests |
|---|---|---|
| 0 Investigate, review, plan | done | n/a |
| 1 Foundation and listing scraper | done | pass |
| 2 Report fetcher and raw cache | done | pass |
| 3 Extractor | done | pass |
| 4 Filters, scoring, ranking | done | pass |
| 5 Label verification agent | done (in-session; top 10 provisional) | pass |
| 6 Reports, changelog, re-runs | done | pass |
| 7 README, final run, PR | done | pass |

`python -m pytest -q` -> `312 passed` (2026-11-25).

## On hold (2026-11-25)

Nothing runs until the owner reviews DECISIONS_FOR_ANGELA.md (O-3). The
watchdog schedule is off (O-2).

## Post-PR changes (2026-11-25)

Per-capsule score (D-35) and both form-note wordings (D-33, parser v6). The
extract re-ran on all reports, then `python -m omega3 rank`: 83 ranked, 10
flavored, 0 oil weight unavailable. Changelog
`reports/changelogs/2026-11-25T042027.224Z.md` has 93 list or rank changes. I
checked all ten new top-10 batches against their PDF text: lot, PV, AV, TOTOX,
EPA and DHA all match. 5 more label checks were done in-session (D-36).

## Stage 7 evidence

Live incremental `python -m omega3 run`, run `2026-11-25T02:47:40.178Z`,
exit 0. Crawl: 838 products (6 new), 51 pages changed, 4,496 lots, 8
delisted. Fetch: 76 pending, 61 downloaded (17.9 MB), 15 errors (the known
dead links). Extract: all 61 read by the parser. 2 LLM entries were served
from the cache (still failed, as intended). Lists: 11 ranked, 3 flavored, 208
form unverified, 79 oil weight unavailable, 7 monoglyceride, 11 incomplete,
18 pet, 501 excluded. Changelog `reports/changelogs/2026-11-25T024740.178Z.md`:
80 new batches, 8 gone, 41 list or rank changes. 927 HTTP requests. The new
#7 (UGAN0004 lot 2608202) and #10 (SPRT0048 lot S26E206) were checked
against their PDF text and every value matches. 14 PDFs are kept for the
ranked and flavored batches.

## Stage 6 evidence

`python -m omega3 rank` wrote `reports/2026-11-25/report.md` and `.html`,
`reports/changelogs/<run>.md` and `data/exports/*.csv`. Every top-10 value
(TOTOX, PV, AV, acid value, EPA, DHA) was found in the text of its source
PDF, which is committed in `data/raw/pdf_kept/` (15 PDFs, one per ranked or
flavored batch). Re-run behaviour is tested: a second run inside the refresh
window makes zero HTTP requests and yields an identical ranking and an empty
changelog; a later run reports new, expired and moved entries.

## Stage 5 evidence

82 product sessions, in best-possible-score order, done in-session (O-1).
None used more than the 8-call cap (37 used 1, 33 used 2). Every stored form
and oil weight has a verbatim quote found on a page fetched in that product's
session (`data/traces/agent/<product>.json`). Result: 12 ranked, 3 flavored.
Stopping threshold 10.21; 261 candidates could still reach the top 10 (198
not yet checked, 63 checked without the label giving both facts). See D-30.

## Stage 4 evidence

Ranking run on the full database (as of 2026-11-25), before any label
verification: 0 ranked, 214 form unverified, 90 oil weight unavailable,
6 monoglyceride, 11 incomplete data, 15 pet, 496 excluded (314 on the
oxidation cutoffs, 114 not capsules, 40 with no unexpired batch, 18
chewables, 5 ethyl ester, 3 phospholipid/krill, others). 304 products pass
every check that can be made without the label and wait for it.

Six random exclusions checked against their numbers, e.g. IGLT0005 lot 1035
TOTOX 10.68 (not below 10), UGAN0004 lot 2612807 PV 3.24 (above 3), VTRM0001
lot UC260112 TOTOX 17.65 and AV 13.23: all correct.

## Stage 3 evidence

`python -m omega3 extract`: 4,041 parser ok, 9 LLM fallback (in-session;
7 ok, 2 failed on purpose), 2 raw-material certificates. Templates: T1 3,574,
T1-2024 478, RM 2. TOTOX reported 4,035, computed 1.

Five random rows against their PDFs (values identical in every case):

| Report | Extracted | PDF line |
|---|---|---|
| U.G.A. Nutraceuticals 2606101 | mg per 2 softgels, EPA 71 DHA 269 -> 35.5/134.5 per cap, PV 0.41 AV 4.99 TOTOX 5.81 acid 0.1 | `EPA 45 71 Yes`, `Peroxide <= 5 meq/kg 0.41 meq/kg Yes`, `TOTOX <= 19.5 5.81 Yes` |
| Vivo Brands 25090SZ011 | mg per softgel, EPA 721 DHA 201, PV 0.14 AV 3.53 TOTOX 3.81 acid 0.05 | `EPA 665 721 Yes`, `Anisidine <= 20 3.53 Yes` |
| 18 12 1000mg L.70123 | mg / 3 capsules, EPA 567 DHA 390 -> 189/130 per cap, PV 2.5 AV 6.2 TOTOX 11.2 acid 0.64 | `EPA 540 567 Yes`, `TOTOX <= 19.5 11.2 Yes` |
| GNC Hong Kong 1069DJ2521 | mg per 2 softgels, EPA 724 DHA 577 -> 362/288.5, PV 0.23 AV 10.06 TOTOX 10.52 | `DHA 480 577 Yes`, `Anisidine <= 20 10.06 Yes` |
| Igennus 1045 | mg per cap, EPA 731 DHA 382, PV 0.2 AV 4.8 TOTOX 5.2 acid 0.41 | `EPA 670 731 Yes`, `Peroxide <= 5 meq/kg 0.20 meq/kg Yes` |

## Stage 2 evidence

4,069 report files listed for capsule products; 4,054 downloaded, 15 dead
links (HTTP 404 on andi.nutrasource.ca). 1.12 GB of PDFs in the gitignored
local cache; text layers and tables committed (17 MB, `data/raw/text/`).
A second fetch requests nothing (tested).

## Stage 1 evidence (2026-11-17)

`python -m pytest -q` -> `137 passed in 3.26s` (stage 1: 31 tests; later-stage
code already in the tree: 106).

Live run, `python -m omega3 crawl`:

| | |
|---|---|
| Products listed (API `totalCount` 832) | 832 |
| Product pages fetched, first run | 832 (interrupted once by a container restart, resumed from cache) |
| Second run | 832 pages from cache, 13 HTTP requests (listing only) |
| Capsules-only filter: include | 699 (Capsules 349, Softgels 346, chewable softgels 4) |
| Pending on report unit (chewables) | 9 |
| Excluded before any download | 124 (Liquids 97, Chewables 10, Food 6, Gummies 5, Jelly 2, Syrups 2, Mists 1, Tablets 1) |
| Lots listed | 4,424 (122 duplicate dropdown entries dropped) |
| Expired lots in listing | 0 |
| Reports to download | 4,069 |

Spot-check against the live site: PLLF0001 parsed as Softgels, 3 lots
(F2604-0538 exp 2028-04-30, F2602/0392 exp 2028-03-22, 2513007 exp
2027-07-31), matching the dropdown on its product page.

Found and fixed during the live run: one product page with malformed
brand-supplied HTML that swallowed the Product Type field; RTCP radiation
certificates filed under the IFOS dropdown (89 lots listed twice with two
different files). Both have regression tests.

## Phase 0 evidence

- Listing endpoint returned 832 IFOS products (totalCount 832, 832 unique
  ProductNum) on 2026-11-17.
- 86 product pages sampled: product types Capsules 37, Softgels 30,
  Liquids 14, Chewables 3, Food 1, Gummies 1; 362 lots, none expired.
- 27 report PDFs sampled, all with text layers; templates T1, T1-2024, RM.
- robots.txt: www allows all; certifications and andi have none. No terms of
  use page found.
- Fixtures committed: 13 PDFs, 11 product pages, 1 listing page
  (`tests/fixtures/`, 4.6 MB).

## Usage so far

Token usage is not readable from inside the session (see D-11). Measurable:
Stage 2 made 4,069 requests to andi.nutrasource.ca (1.12 GB) at 1 request
per second. Stages 3 and 4 made none. Stage 5 made about 160 requests to brand
sites and the NIH label database, plus 7 hosted web searches.
about 130 HTTP requests in Phase 0 and about 870 in Stage 1 (listing plus 832
product pages), all at 1 request/second or slower. Effort: Phase 0 high,
Stage 1 medium.

## Notifications

- "Omega-3 build started." sent, ntfy returned HTTP 200.
- Stage 0 to 5 notifications sent, HTTP 200.
- BUILD COMPLETE sent at the end of Stage 7.

## Session override in force

No `OMEGA3_ANTHROPIC_API_KEY`: LLM fallback and form verification are done
in-session; API paths are built but off (`llm.api_enabled: false`). See O-1.

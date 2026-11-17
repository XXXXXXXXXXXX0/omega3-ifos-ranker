# Status

Last updated: 2026-11-17 01:15 UTC, end of Stage 1. Branch: `claude/keen-ptolemy-j47bf2`.

## Resume here

Next: **Stage 2 (report fetcher)**: run `python -m omega3 fetch` (resumable;
it skips every report already in the manifest). Code for stages 2 and 3 is
already written and tested; stage 4 (`omega3/rank.py`) is written, tests
pending. Nothing is blocked. To resume in a new session: read SPEC.md, PLAN.md and
DECISIONS_FOR_ANGELA.md, then start the next stage listed here.

## Stages

| Stage | State | Tests |
|---|---|---|
| 0 Investigate, review, plan | done | n/a (no build code yet) |
| 1 Foundation and listing scraper | done | 137 passed (all stages so far) |
| 2 Report fetcher and raw cache | running: full download | code tests pass |
| 3 Extractor | | |
| 4 Filters, scoring, ranking | | |
| 5 Label verification agent | | |
| 6 Reports, changelog, re-runs | | |
| 7 README, final run, PR | | |

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
about 130 HTTP requests in Phase 0 and about 870 in Stage 1 (listing plus 832
product pages), all at 1 request/second or slower. Effort: Phase 0 high,
Stage 1 medium.

## Notifications

- "Omega-3 build started." sent, ntfy returned HTTP 200.
- Stage 0 and Stage 1 notifications sent, HTTP 200.

## Session override in force

No `OMEGA3_ANTHROPIC_API_KEY`: LLM fallback and form verification are done
in-session; API paths are built but off (`llm.api_enabled: false`). See O-1.

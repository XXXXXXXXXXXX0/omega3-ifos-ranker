# Status

Last updated: 2026-11-17, end of Phase 0. Branch: `claude/keen-ptolemy-j47bf2`.

## Resume here

Next: **Stage 1 (foundation and listing scraper)** in PLAN.md. Nothing is
blocked. To resume in a new session: read SPEC.md, PLAN.md and
DECISIONS_FOR_ANGELA.md, then start the next stage listed here.

## Stages

| Stage | State | Tests |
|---|---|---|
| 0 Investigate, review, plan | done | n/a (no build code yet) |
| 1 Foundation and listing scraper | next | |
| 2 Report fetcher and raw cache | | |
| 3 Extractor | | |
| 4 Filters, scoring, ranking | | |
| 5 Label verification agent | | |
| 6 Reports, changelog, re-runs | | |
| 7 README, final run, PR | | |

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
about 130 HTTP requests to Nutrasource hosts (10 listing, 87 product pages,
27 PDFs, a few probes), all at 1 request/second or slower.

## Notifications

- "Omega-3 build started." sent, ntfy returned HTTP 200.

## Session override in force

No `OMEGA3_ANTHROPIC_API_KEY`: LLM fallback and form verification are done
in-session; API paths are built but off (`llm.api_enabled: false`). See O-1.

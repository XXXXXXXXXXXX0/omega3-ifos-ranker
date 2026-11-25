# Build plan

Written at the end of Phase 0 (2026-11-17). Stages are built in this order.
A stage is complete only when its tests and all earlier tests pass.

Verification, every stage: `pytest` against the real fixtures in
`tests/fixtures/` (13 IFOS PDFs, 11 product pages, 1 listing page), plus a
spot-check of pipeline output against the source PDF, recorded in STATUS.md.

## Pipeline

```
listing API -> product pages -> report PDFs -> text layer -> template parser
   (raw cache, hashed)                              |  fail -> LLM fallback (cached)
                                                    v
                                         normalized SQLite
                                                    v
                          deterministic filters + form-from-listing-text
                                                    v
                       label verification agent (form, oil weight, flags)
                                                    v
                             deterministic ranker -> report + changelog
```

## Layout

```
config.yaml              every threshold, weight, margin, switch
omega3/                  package (cli, scrape, extract, db, rank, agent, report)
tests/                   pytest + fixtures
data/raw/listing/        listing JSON per run
data/raw/pages/          product page HTML (one per ProductNum)
data/raw/text/           text layer of every report (committed)
data/raw/pdf/            raw PDFs (gitignored cache)
data/raw/pdf_kept/       raw PDFs for ranked batches (committed)
data/cache/llm/          LLM extraction results, keyed by PDF hash
data/cache/agent/        verification results, keyed by ProductNum
data/traces/agent/       full decision trace per product
data/omega3.sqlite       normalized database
data/exports/            CSV dumps of the main tables (diffable)
reports/<date>/          report.html, report.md, changelog.md
```

## Stage 1: Foundation and listing scraper (effort: medium)

Built: package skeleton, `config.yaml` and loader, SQLite schema (products,
batches, measurements, exclusions, runs; indexed on product and lot), polite
HTTP client (1 req/s, retries with backoff, identifying User-Agent), listing
client (paged JSON), product-page parser (name, summary, product type, RDA,
website, brand, lots with expiry and PDF filename), brand-name lookup,
pre-download format decision.

Acceptance:
- Parsing the 11 fixture pages gives the expected product type, lot count,
  lot numbers, expiry dates and PDF filenames (asserted per fixture).
- Format decision: Liquids/Gummies/Food rejected before download;
  Capsules/Softgels accepted; Chewables decided by the D-2 rule.
- Live run enumerates all IFOS products (count equals the API `totalCount`)
  and stores one page per product; second run makes zero page requests for
  unchanged products within the refresh window.
- Schema has indexes on product and lot; upserts are idempotent.

## Stage 2: Report fetcher and raw cache (effort: medium)

Built: PDF downloader for accepted products only, manifest (URL, filename,
SHA-256, size, ETag, Last-Modified, PDF creation date, first-seen,
last-checked), text-layer extraction to `data/raw/text/`, skip logic.

Acceptance:
- Fixture PDFs hash to the values in `tests/fixtures/pdf/MANIFEST.json`.
- A second run downloads nothing (asserted with a counting fake transport).
- A new filename for an existing lot is fetched and recorded as a revision.
- Full crawl completes; every accepted lot has a manifest row or a logged
  download error. Counts recorded in STATUS.md.

## Stage 3: Extractor (effort: high)

Built: template detection (T1, T1-2024, RM, unknown), row parser driven by the
terminology table, unit parser and conversion to mg per capsule, expiry
parser, `<x` handling, plausible-range validation, TOTOX cross-check and
computed TOTOX, unrecognized-label log, LLM fallback (in-session queue and
cache; Haiku API path behind `llm.api_enabled: false`).

Acceptance:
- For each of the 12 batch-report fixtures, every extracted value equals a
  hand-transcribed expected value (EPA, DHA, total omega-3, PV, AV, TOTOX,
  acid value, all contaminants with limit and compliance, unit, lot, expiry).
- RM fixture is classified RM and yields no measurements.
- Unit tests: every unit in F-7 converts correctly; per-mL and per-g are
  flagged as not per-capsule; short terms (TG, EE, PL, AV, PV, FFA) match as
  whole words only.
- Out-of-range values are rejected and routed to the fallback queue.
- An LLM result is cached by PDF hash and never requested twice (tested with
  a fake client); the API client is never constructed when the flag is off.
- Full run: parse rate and every fallback case reported in STATUS.md, with 5
  rows compared side by side against their PDFs.

## Stage 4: Filters, scoring and ranking (effort: high)

Built: pure functions, no I/O, no LLM. Form classification from listing text,
flavored and pet detection, expiry drop, most-recent-batch selection,
inconsistent-batch flag with TOTOX range, oxidation and acid-value cutoffs,
contaminant compliance, incomplete-data routing, oxidation-load score,
anchored 10% tie groups with AV then EPA+DHA tiebreak, separate lists,
exclusion counts by reason.

Acceptance (table-driven tests on synthetic batches, boundaries included):
- TOTOX 9.99 passes, 10.0 fails; PV 3.0 passes, 3.01 fails; AV 8.0 passes,
  8.01 fails; acid value 3.0 passes, 3.01 fails; TOTOX 8.0 is preferred tier.
- Missing PV or AV goes to incomplete data; missing acid value or contaminants
  is flagged and kept.
- A failed contaminant excludes; EE, PL and krill exclude; MAG has its own
  list; no stated form is unverified, never TG.
- Expired batch dropped; newest report date chosen; another unexpired batch
  failing a cutoff sets "inconsistent batches" with the TOTOX range.
- Score equals TOTOX x 1000 / (EPA+DHA mg per g oil); missing oil weight
  excludes from ranking; null EPA is not zero and is labelled "DHA only".
- Tie test: scores 10.0, 10.9, 11.5 with different AVs order as specified.
- Every threshold is read from `config.yaml` (test changes one and sees the
  outcome change).

## Stage 5: Label verification agent (effort: high)

Built: tools (web search, fetch page with keyword-windowed text, read cached
report, read-only SQL), per-product budget (8 tool calls, output cap), trace
writer, result cache keyed by product and page hash, write-back to SQLite,
candidate queue with optimistic-bound ordering and stopping rule, the API
tool-use loop (off), and the in-session driver used for this build.

Acceptance:
- 9th tool call for a product is refused and logged.
- Fetch tool refuses non-http(s) URLs, private addresses and binary content;
  fetched text is returned as data only.
- SQL tool rejects anything but a single SELECT.
- A cached product is not re-verified unless its page hash changes.
- Conflicting or implied evidence produces "unverified" with the reasoning.
- Every decision writes a trace with each URL checked and an evidence quote.
- API loop passes its tests against a fake client; no network.
- Live: the in-session run verifies candidates in bound order until the
  stopping rule holds for the top 10. Counts in STATUS.md.

## Stage 6: Reports, changelog, re-run orchestration (effort: medium)

Built: `python -m omega3 run` (all stages, incremental), HTML and Markdown
report (top 10 with reasoning, all separate lists, exclusion counts, display
columns from the spec), CLI table, dated changelog (new batches, newly expired
batches, ranking changes versus the previous run), CSV exports.

Acceptance:
- Report contains every display column and every separate list.
- Changelog test: two synthetic runs produce the right new, expired and
  moved entries.
- Idempotence: running twice on the same cache makes no network calls and
  yields an identical ranking and an empty changelog.
- Top-10 numbers spot-checked against the source PDFs.

## Stage 7: README, final run, pull request (effort: medium)

Built: README covering every topic the spec lists, final full run committed,
STATUS.md closed out, draft pull request to `main`.

Acceptance: full test suite green; README covers each required topic; PR open.

## Estimated usage

| Stage | Effort | Why |
|---|---|---|
| 0 | high | investigation, spec review |
| 1 | medium | routine scraping |
| 2 | medium | mostly waiting on a polite crawl |
| 3 | high | parsers, units, validation |
| 4 | high | the locked criteria live here |
| 5 | high | in-session verification of many products |
| 6 | medium | formatting, orchestration |
| 7 | medium | writing |

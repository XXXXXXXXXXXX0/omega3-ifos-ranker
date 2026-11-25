# omega3-ifos-ranker

Ranks triglyceride-form fish oil capsules by **low oxidation** and **high
EPA+DHA concentration**, using the batch reports of the International Fish Oil
Standards program (IFOS, run by Nutrasource). A small, re-runnable data
pipeline: a scraper, a deterministic report parser with an LLM fallback, a
tool-using label verification agent, and a ranking step that is plain code.

**Latest report:** [`reports/latest/report.md`](reports/latest/report.md)
(also [`report.html`](reports/latest/report.html)), with the run's changelog in
[`reports/latest/changelog.md`](reports/latest/changelog.md).

Read the caveats below before acting on a ranking. This is a screening tool
built on lab reports that brands pay for, not medical advice.

## What it does

```
IFOS listing API -> product pages -> report PDFs -> text layer -> parser -> SQLite
   (raw cache, hashed)                     (LLM fallback, cached) |
                                                                  v
                          filters and form from IFOS text  -> label agent -> ranker -> report + changelog
```

1. **Crawl** the public JSON endpoint behind the IFOS product search (832
   products in November 2026) and each product page (lots, expiry dates,
   product type, report file names).
2. **Filter before downloading**: only capsule and softgel products' unexpired
   lots are fetched.
3. **Fetch** each report PDF once, hash it, and keep its text layer and table
   cells (`data/raw/text/`). Raw PDFs live in a local cache; the ones behind
   ranked batches are committed (`data/raw/pdf_kept/`).
4. **Extract** values cell by cell from the PDF tables with a deterministic
   parser (99.7% of 4,054 reports). The rest go to an LLM fallback whose
   output passes through the same parsing and range checks.
5. **Verify labels** for products that pass every other check: the oil form
   and the capsule's oil weight, which IFOS reports do not contain.
6. **Rank** with pure code, and write the report, a dated changelog and CSV
   exports.

## Running it

```bash
pip install -r requirements.txt
python -m omega3 run            # everything, incremental
python -m omega3 run --offline  # re-rank from the database, no network
python -m omega3 top            # print the current top 10
python -m pytest -q             # 305 tests
```

Stages can also be run separately: `crawl`, `fetch`, `extract`, `rank`. The
label agent has `agent-queue` (what is waiting, best case first) and
`agent-run` (the API agent, see below), plus in-session helpers
(`agent-explore`, `agent-fetch`, `agent-tool`, `agent-record`).

Every threshold, weight, margin and switch is in [`config.yaml`](config.yaml).
The decisions behind the design, and the open questions, are in
[`DECISIONS_FOR_ANGELA.md`](DECISIONS_FOR_ANGELA.md).

### Turning on the API paths

By default, the LLM fallback and the label agent are carried out by whoever
runs the pipeline (in this build, a Claude Code session), and the API paths
are off. To let the pipeline call the Claude API itself:

1. put the key in `OMEGA3_ANTHROPIC_API_KEY` (environment or a gitignored
   `.env`; the pipeline never reads `ANTHROPIC_API_KEY`),
2. set `llm.api_enabled: true` in `config.yaml`,
3. `pip install anthropic`, then `python -m omega3 run`.

Extraction uses Claude Haiku with a JSON-schema response, several reports per
request; the agent uses Claude Sonnet with the hosted web search tool. Both
are tested against fake clients only: they have not yet made a real API
call, so expect a shakedown run.

## Design choices

**Deterministic first, LLM only where needed.** IFOS reports are generated from
a Word template, so a table parser reads almost all of them exactly. The LLM
is a fallback for the few it cannot read (9 of 4,054 this run), and it only
transcribes cells: numbers are parsed, unit-converted and range-checked by the
same code as everything else, and every result is cached by the PDF's SHA-256
so no report is ever sent twice.

**A cheap model for extraction, an agent only for the label.** Transcribing a
table is a small, bounded job. Finding a product's oil form and capsule weight
on the open web is not, so that is the only place an agent is used. It has four
tools (web search, fetch page, read the cached IFOS report, read-only SQL), a
hard limit of 8 tool calls per product, and a token cap. It records every URL
it checks. A value is stored only if a verbatim quote supporting it is found
on a page fetched in that product's own session; otherwise the product stays
unverified, with the reasoning logged (`data/traces/agent/`).

**Ranking is pure code.** `omega3/rank.py` has no I/O and no LLM, so the same
database always produces the same ranking. The reasoning text in the report is
assembled from the numbers.

**Caching and re-runs.** Each batch is keyed on IFOS product number plus lot.
A re-run only fetches what is new: product pages older than a day, report
files not already in the manifest (a revised report gets a new file name),
and extraction of new files or of everything when the parser version changes
(about 5 seconds). Label results are cached per product and re-checked against
their pages, not re-verified, unless the evidence has disappeared. Each run
writes a dated changelog: new batches, expired or withdrawn batches, and every
change of list or rank.

**Everything is in the repo**: listing data, product pages, report text layers
and hashes, the SQLite database, LLM and agent results, agent traces, reports,
changelogs and CSV exports. The raw PDFs (about 1.1 GB) are the exception: only
the ones behind ranked batches and the test fixtures are committed; the rest
can be fetched again and checked against their stored hash
(`storage.commit_raw_pdfs`).

**Untrusted input.** Web pages and PDFs are treated as data. Nothing fetched is
executed or followed as an instruction; the fetch tool refuses non-public
addresses and anything that is not HTML, JSON or a PDF text layer, and the SQL
tool opens the database read-only.

## The score: oxidation load

```
oxidation load = TOTOX x grams of oil needed to deliver 1000 mg EPA+DHA
```

Lower is better. It folds two things into one number: how oxidized the oil
was when tested, and how much of it you would swallow to get a given dose. A
very fresh but dilute oil can lose to a slightly less fresh, highly
concentrated one, because you take in less oxidized material per gram of
EPA+DHA.

It is **a proxy**. TOTOX is a lab index (2 x PV + AV), not a measure of harm,
and the score assumes oxidation products scale with the amount of oil, which
is a simplification. Scores within 10% of each other are treated as ties
(measurement noise), broken by lower AV, then by more EPA+DHA per capsule.

**PV vs AV.** Peroxide value measures primary oxidation products
(hydroperoxides). They form early, then break down into secondary products
(aldehydes), which the anisidine value measures. So PV rises and then falls
as oil ages: a low PV can mean fresh oil or oil that is already past that
stage. AV accumulates, which makes it the more reliable record of an oil's
history, and is why it is the first tiebreaker.

**Unit conversion.** IFOS reports state EPA and DHA per capsule, per *N*
capsules, per gram, per 100 g, per mL, per teaspoon, or as area %. Per-*N*
amounts are divided by *N*. Per-gram amounts give the concentration directly.
Per-capsule amounts are turned into mg per gram of oil using the capsule's oil
(fill) weight from the label, or from an explicit statement in the brand's own
IFOS listing text ("each softgel contains 1000 mg fish oil"). Without an oil
weight the product is flagged and left out of the ranking, never guessed. Per
mL, per teaspoon and per serving amounts are not converted. Original value,
original unit and the conversion applied are all stored.

## Why these rules

These are the owner's criteria. They are locked in `config.yaml` and are not
changed by the pipeline.

**Triglyceride (TG and rTG) only.** Fish oil concentrates come as natural
triglycerides, re-esterified triglycerides (rTG) or ethyl esters (EE). Studies
generally find triglyceride forms absorb at least as well as ethyl esters, and
better when taken with a low-fat meal, though the size of the difference is
debated. A product counts as TG only when a source says so outright: the IFOS
listing, the IFOS report's own comment ("EFA tested as triglyceride (TG)
form"), or the label. Generic "fish oil" is never assumed to be TG.

**Capsules only.** Liquids are exposed to air every time the bottle is
opened, so a production-time oxidation result says less about what is
swallowed weeks later. Capsules also make per-dose comparisons
straightforward. Chewable softgels are kept and flagged; gummies and chews
are excluded.

**Krill and phospholipid oils are excluded, even in capsules.** Their omega-3s
are mostly in phospholipid form, a different product class from concentrated
TG fish oil. The anisidine test is unreliable on them, and IFOS sometimes does
not run it at all: one report in this data says "Anisidine value testing not
performed due to the phospholipid form of product". Without AV, the
oxidation load cannot be computed on the same footing.

**No pricing.** Prices vary by country, retailer and week, and the point is
the oil itself. Cost per dose is easy to add on top of this ranking for a given
market.

**Your cutoffs are stricter than IFOS's.** IFOS passes oil up to TOTOX 19.5,
PV 5 and AV 20. This tool requires TOTOX below 10, PV at most 3 and AV at most
8, with TOTOX at or below 8 marked as a preferred tier. Acid value must be at
most 3 mg KOH/g (the same as IFOS). Every contaminant IFOS reports must be within
its stated limit. Flavored products are ranked separately, because flavorings
can push the anisidine reading up.

**Batches.** Expired batches are dropped. A product is ranked on its most
recently reported unexpired batch. IFOS publishes no test date, so the report
issue date stands in for it. If any other unexpired batch fails a cutoff, the
product is flagged "inconsistent batches" with the TOTOX range across them.

## What IFOS data can and cannot tell you

- **Brand-paid, opt-in testing.** Brands submit and pay for these tests. The
  832 products are those that chose to be listed, not a market survey.
- **Tested at production, not on the shelf.** Oxidation continues after
  testing. A good number at manufacture says nothing about storage,
  shipping heat, or a bottle open for two months.
- **No test date, no oil form, no capsule weight.** IFOS reports carry none of
  these, which is why the label step exists.
- **Area % and labels can lag the lot.** Labels found online sometimes
  describe an earlier formulation; where a label disagreed with the current
  IFOS lot, it was not used.

## Results of this run

See the [report](reports/latest/report.md). In brief (as of 2026-11-25):

RESULTS_SUMMARY

The top 10 is **provisional**. Ranking a product needs its oil form and its
capsule oil weight from the label. Many candidates still pass every other
check but have not been verified, and their best case (if the capsule were
pure omega-3) could still beat the 10th score. The report lists them. Finishing
them needs the API agent (`agent-run`) or more in-session verification.

## Layout

```
config.yaml           every threshold and switch
omega3/               scrape/, extract/, agent/, rank.py, report.py, pipeline.py, cli.py
tests/                pytest suites per stage, real IFOS fixtures in tests/fixtures/
data/omega3.sqlite    products, batches, reports, extractions, measurements, label facts, runs, results
data/raw/             listing JSON, product pages, report text layers, kept PDFs
data/cache/           LLM transcriptions and label results
data/traces/agent/    one decision trace per product
data/exports/         CSV copies of the main tables
reports/<date>/       report.md, report.html; reports/changelogs/ per run
SPEC.md, PLAN.md, STATUS.md, DECISIONS_FOR_ANGELA.md
```

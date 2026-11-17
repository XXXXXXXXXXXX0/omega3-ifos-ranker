# Decisions for Angela

Running log of every proposed change, judgment call, assumption and open
question, with rationale. Updated and committed after every stage. Newest
stage at the bottom. This file replaces approval checkpoints.

Legend: **ADOPTED** = technical change, applied. **PROPOSAL** = touches a
locked criterion; built to spec as written, not applied. **OPEN** = needs
your answer. **NOTE** = something you should know.

---

## Session override (2026-11-17)

**O-1. ADOPTED (your instruction): no `OMEGA3_ANTHROPIC_API_KEY`; LLM work is
done in-session.** You said the missing key is intentional and not a blocker.
So for this session:

- The LLM extraction fallback and the form verification agent are performed by
  the Claude Code session itself, not by Haiku/Sonnet over the API.
- Results are written to the same cache files, SQLite tables and trace files
  the API path would write. Each record carries `model: "in-session"` so the
  two sources can always be told apart.
- The API code paths (Haiku extraction fallback, tool-using verification
  agent) are still built, but sit behind `llm.api_enabled: false` in
  `config.yaml`. Turning them on later needs the key in the environment and
  that one flag flipped. They are unit-tested with a fake client only; **they
  have never made a real API call**, so expect to shake them down the first
  time you enable them.
- Consequence you should know: the spec says "NOT Fable" for these calls. This
  session's model is doing them. Cost shows up as session usage, not API spend,
  and the run report says `agent API cost: $0 (in-session mode)`.
- SPEC.md ENVIRONMENT, Extraction and FORM VERIFICATION AGENT sections updated.

---

## Phase 0a: what the data actually looks like

**F-1. Enumeration works through a public JSON endpoint.**
`certifications.nutrasource.ca/umbraco/surface/NutrasourceContent/GetFilteredProducts?forCertification=IFOS&pageNumber=N&pageSize=100`
returns every IFOS product: **832 products across 362 brand prefixes** on
2026-11-17. It is the endpoint the site's own search page calls. Page size 200
works, 1000 returns HTTP 500 (server JSON length cap), so the scraper uses 100.

**F-2. Each product page lists its lots.** `/certified-products/product?id=<ProductNum>`
gives product name, summary text, **Product Type** (Capsules, Softgels,
Liquids, Chewables, Gummies, Food), recommended daily allowance, brand website,
and a dropdown of lots, each with **lot number and expiry date** and the PDF
filename. PDF URL is `https://andi.nutrasource.ca/files/<filename>`.

| Visible before opening a PDF? | |
|---|---|
| Lot number | yes |
| Expiry date | yes (full date) |
| Product format | yes (Product Type) |
| Test date | **no, and not in the PDF either** (see F-5) |

**F-3. The site lists only unexpired lots.** In an 86-product sample (362
lots) the earliest expiry was 2026-11-19, two days after the crawl. So
"skip expired before downloading" is mostly done for us; the code still
enforces it by date.

**F-4. Terms / robots: no prohibition found.** `www.nutrasource.ca/robots.txt`
is `User-agent: * / Allow: /`. `certifications.` and `andi.` have no robots.txt
(404). No terms-of-use page exists on either site (tried the usual paths; the
footer links only a privacy policy, which says nothing about automated access).
Collection proceeds at 1 request/second with an identifying User-Agent.
I am not a lawyer; "no prohibition found" is not the same as "permission
granted". The reports are published for consumers to read, and this tool reads
them once and caches.

**F-5. Report templates found.** All 27 sampled PDFs are Word-generated,
2 pages, with a real text layer (no OCR needed):

- **T1 "category table" (2025 to 2026)**: header on page 1 (brand, product,
  lot, expiry, directions), four tables on page 2. Two header sub-layouts
  (the "IFOS Compliance" heading wraps differently) and two producers
  (Word, Acrobat PDFMaker).
- **T1-2024 "Category 1: to 4:"**: same tables, older wording ("Total
  Oxidation" instead of "TOTOX", "Inorganic Arsenic", "YES" in capitals,
  pass/fail summary lines on page 1).
- **RM "Raw Materials Certificate"**: 1 page, no batch results, lot shown as
  "Various SKUs". Not a batch report.
- **Legacy "Consumer Report" with charts**: **none found in the sample.** The
  listing only carries unexpired lots, so reports older than about two years
  have aged out. The extractor classifies every PDF in the full crawl and logs
  anything that is not T1/T1-2024/RM; those go to the LLM fallback. I could not
  build a fixture for a template that is no longer served.

**F-6. What the reports do NOT contain** (this matters, see P-1 and D-3):
test date, oil form (TG/EE), capsule fill weight (grams of oil per capsule),
flavoring, servings per bottle, antioxidants, packaging.

**F-7. Units seen in 27 reports:** mg/cap, mg per cap, mg/softgel, mg/capsule,
mg per 2 softgels, mg/5 softgels, mg/2 capsules, mg per g, mg/5 mL, mg/2.5 mL,
mg/1.5 tsp, mg/vial, mg/chewy. Expiry formats: `30-Apr-2028`, `10/2027`,
`03/12/2026 (DD/MM/YY)`, `15/05/2027`, `30/NOV/2026`.

**F-8. Scale.** About 81% of sampled products are capsule-type, averaging
about 4 lots each: roughly **3,300 PDFs at about 300 KB, about 1 GB**.

**F-9. ConsumerLab and Labdoor add nothing usable.**
- ConsumerLab: 19 omega-3 products in the current review, results behind a
  paid membership. Not collectable, not redistributable.
- Labdoor: the fish-oil ranking currently shows 6 graded products (4 more
  "upcoming", 1 expired). Letter grades, no batch, no expiry, footer dated 2019.
- Neither is used. IFOS's limitation (tested at production, not off the shelf)
  stays a documented caveat in the README.

---

## Phase 0b: spec review

### Locked-criteria items (built as written; proposals NOT applied)

**P-1. PROPOSAL / OPEN: "most recently tested batch" cannot be applied
literally; there is no test date.** Not in the listing, not in the PDF.
Built: the closest available proxy, **report issue date = the PDF's embedded
creation date** (set when Nutrasource generated the report), falling back to
the HTTP `Last-Modified` header, then to latest expiry. Stored in a column
named `report_date`, never called a test date, and the output labels it
"report date". Caveat: a *revised* report (Rev 1, Rev 2) gets a new creation
date, so a re-issued older batch can look newer than it is.
Alternative you may prefer: use latest expiry date (later expiry usually means
later production). One config line: `batches.recency_key`.

**P-2. PROPOSAL: the oxidation-load score needs capsule oil weight, which IFOS
does not publish.** Score = TOTOX x grams of oil per 1000 mg EPA+DHA. For
"mg per g" reports that is direct. For per-capsule reports the oil weight
exists only on the product label. Built as written: the verification agent
looks up fill weight alongside oil form, and **a product with no oil-weight
evidence is flagged and excluded from the ranking, not guessed** (spec: "If
capsule weight is unavailable, flag and exclude"). Expect this to shrink the
ranked list noticeably; the report counts it.
Proposal for you to consider (not applied): a second, clearly labelled ranking
on **TOTOX alone**, which needs no label data and covers every product.

**P-3. NOTE (interpretation, not a change): 10% tie rule.** "Within 10% of
each other" is not transitive (A~B and B~C does not give A~C), so a sort needs
a concrete rule. Built: sort by score; the best unassigned score anchors a tie
group; everything within 10% *of that anchor* joins; inside a group order by
lower AV, then higher EPA+DHA per capsule. Margin in config.

**P-4. NOTE (interpretation): values reported as "<x".** Heavy metals are
routinely "<0.0125 ppm"; PV/AV can be too. Built: stored with the `<` flag;
for cutoffs and scoring the bound itself is used (worst case for the product).

**P-5. NOTE: TOTOX cross-check.** Where a report gives PV, AV and TOTOX, the
extractor verifies TOTOX = 2*PV + AV within rounding and flags mismatches.
The reported value is still the one used, per spec.

### Judgment calls the spec did not anticipate

**D-1. ADOPTED: pet products go to their own list.** IFOS certifies cat and
dog fish oils in capsule form ("vetwish anchovy pet fish oil for cat"). The
spec is silent. Ranking cat supplements in a human top 10 is plainly not the
intent, so they are detected by name/summary keywords and listed separately.
Toggle: `filters.exclude_pet_products`. Tell me if you want them in.

**D-2. ADOPTED: how "Chewables" is handled.** Spec: capsules only, exclude
gummies, flag chewable softgels. IFOS's "Chewables" type mixes both (chewable
softgels and "Gummy Chews"/"CHEWY"). Rule: a Chewables product is kept and
flagged *chewable* only if its name, summary or report unit says softgel or
capsule; otherwise it is excluded as "chewable, not a softgel". "Food" type is
excluded as not a capsule.

**D-3. ADOPTED: the verification agent also collects the label facts IFOS
lacks.** Spec already has it capture antioxidants and packaging on the same
visits. It now also records, from the same pages: oil weight per capsule,
serving size, servings per bottle, flavoring. No extra tool calls allowed for
these; same 8-call cap.

**D-4. ADOPTED: verify only products that could be ranked, best candidates
first.** Spec says run the agent for every form-unverified product. Verifying
a product that already fails an oxidation cutoff costs calls and changes
nothing. So the agent queue is: capsule products that pass every other hard
filter, ordered by best-possible score (optimistic bound assuming the capsule
is 100% omega-3). This also gives a hard stopping rule: once the next
candidate's best-possible score cannot beat or tie the 10th-ranked verified
product, **the top 10 cannot change** no matter what the rest turn out to be.
Products not reached stay in "form unverified" with reason "not attempted",
and the report states how many.

**D-5. ADOPTED: raw PDFs are not all committed; text layers and hashes are.**
About 1 GB of PDFs, almost all of it identical logos. Plain git handles that
badly; Git LFS on a private repo has storage and bandwidth quotas I cannot see
from here, and every future cloud session would re-download it. Instead:
- committed: SHA-256, size, URL, HTTP validators and PDF metadata for every
  report (the manifest), plus the **full extracted text layer** of every PDF
  (about 1 KB each), which is everything the extractor reads;
- committed: the raw PDF for every batch that appears in the ranked output and
  for every test fixture, so the published numbers can be checked against
  source inside the repo;
- not committed: the remaining raw PDFs. They sit in a gitignored local cache
  and are re-downloadable from the stored URL and verifiable by hash.
Re-runs do not re-download: a report is skipped if its filename is already in
the manifest (Nutrasource puts the report number and revision in the
filename, so a changed report is a new filename).
Switch: `storage.commit_raw_pdfs: ranked | all | none`. If you want literal
"everything in the repo", set `all` and enable LFS; I did not want to spend
your LFS quota without asking.

**D-6. ADOPTED: Raw Materials Certificates are excluded** with reason
"raw-material certificate, no batch results" (they list ingredient SKUs, not a
finished-product batch).

**D-7. ADOPTED: flavored detection.** From product name and summary keywords
(lemon, orange, citrus, strawberry, berry, mint, flavour/flavor, and so on)
plus the label when the agent visits. A product with no flavor evidence either
way is treated as unflavored and marked "flavor not stated".

**D-8. ADOPTED: batch key.** Spec: brand + product + lot. Built: IFOS
`ProductNum` + lot, with brand and product name stored alongside. ProductNum
is Nutrasource's own stable ID; names get edited (trademark symbols, pack
sizes) and would split one product into two.

**D-9. ADOPTED: form from IFOS's own listing text counts as evidence.** If the
IFOS product name or summary states the form using the spec's terminology
list ("rTG", "ethyl ester", "krill"), that classifies the product
deterministically, with the IFOS page as source URL and the matched phrase as
evidence. Only products with no stated form go to the agent. Conflicting terms
in the same text (TG and EE both present) leave it unverified.

**D-10. ADOPTED: reasoning text in the report is generated by code** from the
numbers (templated sentences), not by an LLM. Keeps "ranking is pure code"
true end to end.

**D-11. NOTE: usage reporting.** The spec asks STATUS.md for "usage so far".
A session cannot read its own token meter. STATUS.md reports what is
measurable: HTTP requests, PDFs downloaded, tool calls, and a low/medium/high
effort label per stage.

**D-12. NOTE: in-session token cap.** The agent's per-product token cap is
enforced in the API path by counting API usage. In-session there is no token
count, so the same cap is applied to characters of tool output returned
(configurable), alongside the hard 8-tool-call limit.

### Housekeeping you should know about

**H-1. NOTE: the spec was not at `SPEC.md`.** It was at
`claude/omega3-build-spec.md` on `main`. Per its own instruction I copied it to
`SPEC.md` (first commit, verbatim) and treat that as the source of truth. The
original is untouched and will drift; delete it when convenient.

**H-2. NOTE: your Stop hook is not active.** `settings.json` is at
`claude/.claude/settings.json`. Claude Code only reads `.claude/settings.json`
at the repo root, so the "Claude Code stopped" push never fires. I did not
move or edit it (you said not to touch it). To activate: move it to
`.claude/settings.json`. The GitHub watchdog workflow is in the right place.

**H-3. NOTE: the watchdog only runs from the default branch.** Scheduled
workflows fire from `main`; it checks out with full history, so commits on
this work branch do count as activity. I commit at least every 45 minutes
during long crawls so it stays quiet.

**H-4. NOTE: the ntfy topic is hard-coded in both alerting files.** Anyone
with repo read access can publish to or subscribe to that topic. Fine for a
private repo; rotate it if the repo ever goes public.

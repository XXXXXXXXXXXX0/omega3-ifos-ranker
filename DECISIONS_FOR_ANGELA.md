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

---

## Stages 1-4: what the full dataset changed (2026-11-17 to 2026-11-25)

**D-13. ADOPTED: duplicate lots on a product page.** 122 dropdown entries
repeat a lot already listed: the same report under two expiry dates, or an
RTCP radiation certificate filed in the IFOS dropdown (89 lots). One entry is
kept per lot, preferring the IFOS report over an RTCP certificate.

**D-14. ADOPTED: the report's "EFA tested as ..." comment counts as form
evidence; the "mg per softgel as TG" unit does not.** 213 reports carry a
comment such as "EFA tested as re-esterified triglyceride (rTG) form" and 167
express potency "as TG". Where a product's IFOS listing also states its form,
the comment agreed on the family in all 99 such reports, and it tells EE, TG
and rTG apart, so I treat it as the testing lab's explicit statement. "as TG"
in a unit can be just how a lab expresses amounts, so it is parsed (it is
still per softgel) and recorded in `basis_expressed_as`, but it is **not**
used as evidence of form. See P-7.

**D-15. ADOPTED: a detection limit above IFOS's limit.** 12 products have a
metal reported like "<0.1 ppm" against a 0.05 ppm limit. The number cannot
show a pass or a fail, so IFOS's own verdict is used and the product is
flagged "compliance taken from IFOS's verdict only". Strictly evaluating at
the bound (P-4) would exclude products IFOS certifies; I judged that the
wrong reading of "must pass IFOS's stated limit". Tell me if you want them
excluded instead.

**D-16. ADOPTED: a printed number over the printed limit fails, even when
IFOS marks it compliant.** Seen in 8 reports (mercury 0.840 ppm against 0.1,
lead 0.067 against 0.05, dioxin-like PCBs 2.36 against 1.5, arsenic 0.130
mg/kg against 0.1 ppm). Some are probably IFOS typos, but your rule is the
stated limit, so the number governs. All 8 are older lots, so in this run
they set the "inconsistent batches" flag rather than excluding a product.

**D-17. ADOPTED: contaminant units are converted before comparing** (mg/kg =
ppm, ug/kg = ppb, ng/kg = ppt; "WHO-TEQ" ignored). A printed limit of 0 is a
misprint (nine reports print "<= 0 ppt" for dioxins), so the verdict is used.

**D-18. ADOPTED: EPA + DHA may exceed total omega-3 by up to 5%** (config
`validation.epa_dha_over_total_tolerance`). Labs measure the total separately,
and a 3.5% overshoot (1148 vs 1109) is method noise, not a parse error. Gross
cases (2121 vs 397) are still rejected.

**D-19. ADOPTED: misprinted numbers are kept as text and never read.**
"<00016 ppm" (lost decimal point) or "0. 30 meq/kg" is not turned into a
value by the parser. Contaminants fall back to IFOS's verdict; oxidation
values go to the LLM fallback.

**D-20. NOTE: the LLM fallback, done in-session (O-1).** The parser read
4,041 of 4,054 reports (99.7%), plus 2 raw-material certificates. Nine went
to the fallback, which I transcribed cell by cell into the same payload the
Haiku path produces (`data/cache/llm/<sha256>.json`, `model: "in-session"`);
the pipeline then applied its own parsing and range checks. 7 now pass. 2 stay
failed on purpose: an EPA misprint (1989 mg against a spec of 180 and a total
of 397), and a report whose potency unit is printed "N/A". Every judgment made
while transcribing is in that payload's `notes` and becomes a warning on the
record, e.g. reading "0. 30" as 0.30 because the report's own TOTOX only
works with 0.30, and refusing to relabel a "Total Omega-3" row that is
probably DHA.

**D-21. ADOPTED: algal oils and multi-ingredient products are flagged, not
excluded.** Your goal says fish oil, but source is not among the locked
filters. 8 products look algal and 64 are multi-ingredient (prenatal, lutein,
CoQ10). Display flags only. OPEN: should algal oils be a separate list?

**D-22. ADOPTED: re-run mechanics.** Extractions carry a parser version and
are redone when it changes (about 5 seconds for all 4,054). A crawl inside the
24-hour refresh window makes no requests; failed downloads are retried after
the window, not on every run.

**D-23. NOTE: resilience.** The container restarted three times during the
build. Everything is resumable from the database, the raw cache and the last
commit; downloads commit every 10 files.

### Locked-criteria proposals from the full data (not applied)

**P-6. PROPOSAL: estimate oil weight from IFOS area-% figures.** Some reports
print EPA or DHA as both mg per capsule and % of fatty acids, which gives
oil weight = mg / %. Your rule says the oil weight comes from the label, and
area % is a share of fatty acids, not of oil weight (about 5% off for a
triglyceride), so this is **off**. Turning it on would give a score to 8 more
products without a label lookup: `oil_weight.sources: [label, ifos_area_pct]`.

**P-7. PROPOSAL: treat "mg per softgel as TG" as form evidence.** It would
settle the form of only 4 current candidates (all WHC), so I left it off
(D-14).

---

## Stage 5: label verification (in-session, 2026-11-25)

**D-24. ADOPTED: form vocabulary in other languages.** Many brands label in
Spanish, Portuguese, Italian, German, French, Turkish, Korean or Chinese. The
spec's terms are extended with direct translations ("triglicéridos",
"come trigliceridi", "Ethylester", "알티지", "甘油三酯", ...), under the same
whole-word matching rules. Blood-lipid phrases ("niveles de triglicéridos")
are excluded as in English.

**D-25. ADOPTED: an oil weight stated outright in the brand's IFOS listing
text counts as a label value.** Examples: "Each softgel capsule contains 2000
mg fish oil", "Each daily dose of 2 capsules provides 3,000 mg of fish oil",
and IFOS's own template "Each 1700 mg capsule contains a minimum 60% omega-3".
These are direct statements, not calculations. Strict patterns only: a number
in a product name ("Omega 3 1400mg") is not used, since it is often the
omega-3 content, and two disagreeing statements cancel. Shown in the report
as "oil weight from IFOS listing text". Switch: drop `ifos_listing` from
`oil_weight.sources`. In this run it supplies the oil weight for 4 of the 12
ranked products.

**D-26. ADOPTED: label sources the agent can read.** Besides brand pages:
the NIH Dietary Supplement Label Database (a public API with transcribed
supplement-facts panels, used for US brands and counted as "label"), Shopify
product JSON (`/products/<handle>.json`, which carries the description a page
loads with JavaScript), Shopify's product search (`/search/suggest.json`) to
find the right product page, and PDF label text. The fetch tool reads JSON
and PDF as text only; nothing is executed.

**D-27. ADOPTED: pet products found on brand pages.** CHKA0002's brand site
describes itself as dog and cat treats and supplements, which the IFOS listing
does not show. The decision schema gained a validated `pet` field, and such
products go to the pet list. THPT0001 ("Think Pets") was caught by adding the
brand name to pet detection.

**D-28. ADOPTED: comparison copy does not block a verified label.** Listing
text such as "three times the absorption of ethyl ester" names two forms, which
settles nothing, so it no longer overrides a verified label or a report
statement. A listing that names a single form different from the label still
leaves the product unverified.

**D-29. NOTE: how the evidence check behaved.** Every stored form and oil
weight is backed by a verbatim quote found on a page fetched in that product's
own session. The check rejected one of my own quotes (I had copied the
sibling page's wording, "With a capsule filling weight", where the page
fetched for PNZL0002 reads "With capsule filling weight") until I used the
exact text. Values I could only have derived, such as oil from "500 mg EPA,
90% EPA concentrate", were not recorded. Where a label disagreed with the
current IFOS lot (a 2018 Renew Life label, an NIH record whose serving size
did not fit), the label value was not used.

**D-30. NOTE: what the stopping rule could and could not do.** In-session I
worked the queue in best-possible-score order, using 82 product sessions
(none over the 8-call cap; 37 needed a single call). Result: 12 products
ranked plus 3 flavored. The stopping threshold (10th score plus 10%) is 10.21.
Because the best-possible score must assume a capsule is pure omega-3 when the
label is unknown, **198 unverified candidates could still reach the top 10**.
The top 10 is therefore published as **provisional**, and the report lists
the in-reach candidates. To finish: set `llm.api_enabled: true`, provide
`OMEGA3_ANTHROPIC_API_KEY`, run `python -m omega3 agent-run`. My estimate at
the configured cap of 60k tokens per product is roughly $10-25 for the 198,
but the API path has never run against the live API (O-1).

**D-31. NOTE: yield.** Most failures were sites that render without
JavaScript, refuse automated access (403/429/connection resets) or have no
English. Where a label was reached, the oil weight was usually there; the
oil form often was not. Many brands never state it. That is the binding
constraint, and it is the spec working as written ("never assumed TG").

### Proposals from Stage 5 (not applied)

**P-8. PROPOSAL: a tighter best-possible bound from the IFOS minimum
concentration.** Many IFOS summaries say "a minimum N% omega-3 concentration".
Using it would bound the oil weight from above for the stopping rule only,
never for a score, and could prune a large part of the 198. It changes which
products are checked, not how anything is scored. Off until you agree.

**P-9. OPEN: "min 80% TG" products.** MEIN0001's label states the omega-3
is a minimum 80% in triglyceride form. I counted it as TG because the label
states TG, but a minority may be in another form. Tell me if a stated TG share
below 100% should go to "form unverified".

## Stage 7: live re-run (2026-11-25)

**D-32. NOTE: the re-run changed the top 10.** A full incremental
`python -m omega3 run` against the live site found 6 new products, 80 new
batches and 8 withdrawn ones (51 product pages had changed since the crawl a
week earlier). It made 927 HTTP requests and downloaded 61 new reports, all
read by the parser. 15 report links are still dead, the same ones as before.
Effects on the ranking, all from new lots:
- Omegor Vitality 1000 (UGAN0004) entered at #7. Its newest lot, 2608202,
  passes (TOTOX 6.55). The lot it was ranked on before failed.
- Omegor Vitality 500 (UGAN0005) dropped out. Its new lot, 2608095, fails
  (TOTOX 13.22, PV 3.86).
- Sports Research Triple Strength (SPRT0048) fell from #5 to #10 on its new
  lot S26E206 (TOTOX 7.32, up from the lot it was ranked on).
- A M B Well PGFO (AMBW0002, #11 before) dropped out. Its new lot, L100869,
  has PV 4.63.

Now 11 ranked and 3 flavored, and the stopping threshold is 9.66. I checked
the new #7 and #10 against their PDFs and every value matches. This is how the
ranking is meant to work, since each product is ranked on its newest batch.
It also shows that one new lot can move a product five places or drop it.
Read a rank as "this batch", not "this brand".

**D-33. RESOLVED (2026-11-25): "tested as" and "expressed as" are the same
note, and it does state the form.** You asked me to look into the
inconsistency. What the 4,115 reports show:
- The note appears on reports from 3 brands (U.G.A., Pleo/Aquaomega, Vivn).
- "Expressed as" first appears on 2026-05-12. 7 of the 8 products that use it
  said "tested as" before that date, so this is a wording change in the
  report template, not a different statement.
- The note names a different form for different products, and the same form
  for a product across all its lots: "ethyl ester (EE)" on all 71 notes for
  meaQuor (UGAN0001) and all 48 for UGAN0014, "re-esterified triglyceride" on
  others, "triglyceride" on the rest. A calculation convention would read the
  same on every report. This one follows the product.
- Where a label is also on file (UGAN0018), it agrees.

So my concern was wrong: the note is form evidence, and the honest fix was the
opposite of the one I floated. The parser now reads both wordings (parser
version 6), so a product does not lose its form when its older "tested as"
lots expire. Omegor Vitality 1000 keeps TG. Test:
`test_report_form_note_accepts_both_template_wordings`.

**D-34. NOTE: IFOS sometimes lists a report number as the lot.** Omegor Ultra
1200 (UGAN0010) shows "Lot #: 50092026" on its IFOS page. The report file is
"... 2612907 50092026 Product Approval Report.pdf", so 50092026 is the report
number and the batch is 2612907. The pipeline keeps IFOS's lot label as
given. The product is excluded on its TOTOX anyway.

## After the first PR (2026-11-25)

**D-35. OWNER DECISION: score per capsule, not per gram of oil.** You said
you do not need the oil weight, and chose (when asked) the per-capsule score
using IFOS-measured EPA+DHA:

    oxidation load = TOTOX x capsules needed for 1000 mg EPA+DHA

This replaces the locked spec formula (TOTOX x grams of oil per 1000 mg). The
old one stays available as `scoring.basis: oil_weight` in `config.yaml`, and
both are tested. Effects on the same data:
- 83 ranked and 10 flavored, up from 11 and 3. The 79 "oil weight
  unavailable" products all have measured mg per capsule and are now ranked
  or flavored. 0 remain on that list. It now holds only per-gram reports with
  no known capsule weight, and there are none at the moment.
- The new top 3: BIO-H rTG 97% (1.83), Hong Kong Healthcare Ultra Pure
  (2.13), Pharma New Zealand 1440mg (2.25, the old #1). Coco March (old #2)
  is now #11.
- The score no longer depends on the label, so "best possible score" is the
  real score. The top-10 threshold is 5.12, and 21 form-unverified products
  are under it.
- What it gives up: a large capsule counts the same as a small one. A product
  that packs 1,400 mg into a 2 g capsule scores the same as one that packs it
  into 1.5 g, though you would swallow a third more oil, and so more oxidized
  material. Oil weight is still shown wherever it is known, for reference.

**D-36. NOTE: the 21 form-unverified contenders.** All 21 have now been
looked for (5 more in this round: MNSA0001, SWIT0002, HURR0001, GNCC0008,
WHCC0001). None has a label or brand page online that states the form.
Either the brand site refuses automated access, or the only text is a health
claim about "triglyceride levels". One lead I did not use: a third-party
review site says UnoCardio 1000 is "r-triglyceride" with "a pleasant orange
flavor". That is not the label. If it is right, the product belongs in the
flavored list, not the top 10. The quickest way to settle any of the 21 is a
photo of the bottle or an email to the brand, recorded with `agent-record`.

**O-2. OWNER CHANGE: watchdog turned off.** You lifted the "do not modify
watchdog.yml" rule and asked me to turn the watchdog off once the build was
done. Its schedule was removed, in commit 41c221e on `main`, since a schedule
only runs from `main`. It can still be run by hand from the Actions tab. To
turn it back on, restore the two commented lines in the file. The
`.claude/settings.json` rule still stands. The Stop hook in
`claude/.claude/settings.json` stays where it is, and inactive, until you
plan another unattended run.

**O-3. ON HOLD: no further runs until you have reviewed this file.** No
pipeline runs, label checks or unattended sessions until you have been
through the adopted, proposed and open items here. The 21 unverified
contenders (D-36) are part of that review.

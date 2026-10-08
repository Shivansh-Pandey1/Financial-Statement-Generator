# fsgen — Trustworthy financial statements from a messy trial balance

A prototype for the *AI Agentic Engineer* take-home. It takes an ERP trial balance (TB), a chart of accounts (COA),
FX rates and a batch of unposted manual journal entries, and produces a **Profit & Loss**, a **Balance Sheet** and a
**Statement of Changes in Equity**, with a verification layer that refuses to release numbers it cannot prove, and
line-by-line lineage back to the source rows.

> **One-line philosophy:** code computes every number; an LLM may only *propose*; a deterministic gate decides;
> anything uncertain goes to a human. The system never plugs a balance and never invents a rate.

Related documents: [ARCHITECTURE.md](ARCHITECTURE.md) (the design write-up) · [REFLECTION.md](REFLECTION.md) (one-page reflection).

---

## Contents
1. [Quick start](#1-quick-start)
2. [What you get on the supplied data](#2-what-you-get-on-the-supplied-data)
3. [How it works](#3-how-it-works)
4. [Where AI is used and where it is not](#4-where-ai-is-used-and-where-it-is-not)
5. [Defects found in the input data](#5-defects-found-in-the-input-data)
6. [Manual adjustment decisions](#6-manual-adjustment-decisions)
7. [Verification checks](#7-verification-checks)
8. [Traceability for auditors](#8-traceability-for-auditors)
9. [Human-in-the-loop](#9-human-in-the-loop)
10. [Accounting assumptions and policies](#10-accounting-assumptions-and-policies)
11. [Outputs](#11-outputs)
12. [The Streamlit UI](#12-the-streamlit-ui)
13. [Code layout](#13-code-layout)
14. [Testing](#14-testing)
15. [Limitations and what is not built](#15-limitations-and-what-is-not-built)
16. [FAQ / troubleshooting](#16-faq--troubleshooting)

---

## 1. Quick start

Requirements: **Python 3.9+**. The core has **no third-party dependencies**.

```bash
# Navigate to project directory
python -m fsgen.run                  # reads ./inputs, writes ./output, exits 1 when the result is blocked
python -m fsgen.run --trace 1110     # show the lineage behind one account
python -m unittest discover tests    # 20 tests, offline, no API key needed
```

Optional extras:

```bash
# Web UI
python3 -m pip install -r requirements.txt
python3 -m streamlit run app.py       # opens http://localhost:8501

# Apply a human-approved mapping (writes to a different folder so the reference output is untouched)
python3 -m fsgen.run --overrides examples/mapping_overrides.json --out output_override

# Choose where the foreign-currency translation difference goes (default: oci -> 3310; alt: pnl -> 7310)
python3 -m fsgen.run --translation-policy pnl --out output_pnl

# LLM mapping advisor (proposals only, see section 4)
python3 -m pip install anthropic
ANTHROPIC_API_KEY=... python3 -m fsgen.run --advisor llm
```

The exit code is meaningful: **0** = statements ready for review, **1** = blocked. That makes it usable in CI.

> If `streamlit: command not found`, use `python3 -m streamlit run app.py` (pip placed the script outside your PATH).

---

## 2. What you get on the supplied data

The provided data is deliberately broken, so the *correct* outcome is **`DRAFT - BLOCKED (do not release)`**. The system
still produces everything it can and tells you precisely why it will not sign off.

| Figure (USD) | Value | Note |
|---|---:|---|
| Net income | 4,783,500 | Computed two independent ways; they agree |
| Total assets | 36,223,560.20 | Includes provisional GBP translation |
| Total liabilities | 18,770,000 | After accepted adjustments |
| TB imbalance (nominal Dr − Cr) | −4,800 | Credits exceed debits; **not plugged**, shown on the face of the balance sheet |
| Suspense account `9999` (not in COA) | 12,400 | Held in an explicit *Unmapped* line, not classified |
| Provisional FX translation difference | 187,260.20 | Posted to a flagged system line (policy switchable) |
| Adjustments | 6 accepted · 2 rejected · 2 quarantined | See section 6 |
| Blocking verification checks failing | 3 (V1, V3, V6) | Plus one warning (V9) |
| Issues rated blocker/high | 13 | Full list in `output/report.md` |

Open `output/report.md` after a run for the full statements, equity roll-forward, verification table, adjustment verdicts
and the complete issue list.

---

## 3. How it works

A deterministic pipeline of small, single-purpose stages. There is no agent "swarm": the steps that need judgement are
isolated behind gates, and the rest is plain code.

```
 inputs/                                   (never modified; a test enforces this)
   │
   ▼
 ┌────────────┐   ┌────────────┐   ┌─────────────────┐   ┌───────────────┐
 │ 1 Load COA │──▶│ 2 Map TB   │──▶│ 3 Translate FX  │──▶│ 4 Post system │
 │ + COA lint │   │ accounts   │   │ to USD, record  │   │ translation   │
 └────────────┘   │ (gate)     │   │ rate + fallback │   │ difference    │
                  └─────┬──────┘   └─────────────────┘   └──────┬────────┘
                        │ unmapped / ambiguous                  │
                        ▼                                       ▼
                 UNMAPPED bucket                     ┌────────────────────┐
                 + human queue                       │ 5 Triage manual    │
                                                     │ adjustments        │──▶ rejected / quarantined
                                                     │ post accepted only │    (with plain-English reason)
                                                     └─────────┬──────────┘
                                                               ▼
                                         ┌──────────────────────────────────────┐
                                         │ 6 Build P&L, Balance Sheet, SOCIE    │
                                         └─────────────────┬────────────────────┘
                                                           ▼
                                         ┌──────────────────────────────────────┐
                                         │ 7 Verify (10 checks, incl. an        │
                                         │   independent recompute)             │
                                         └───────┬───────────────────┬──────────┘
                                          all blocking pass     any blocking fails
                                                 ▼                   ▼
                                       READY FOR REVIEW      DRAFT - BLOCKED
                                       (+ lineage, manifest) (+ residual quantified, no plug)
```

### Stage by stage

1. **Load COA and lint it.** Builds the hierarchy and reports COA defects (empty header, cash-flow category `TBD`, a
   parent typed as `Expense`, missing normal balance). A code with children is treated as a header regardless of its type.
2. **Map TB accounts to the COA.** The mapping gate (section 4) decides per row: auto-accept, or park the balance in an
   `UNMAPPED:<code>` bucket and escalate.
3. **Translate to USD.** Balance-sheet accounts use the period-end rate; P&L accounts use the period average. Every row
   records the local amount, the rate and *which* rate type was used. Missing rates use a **visible** fallback chain
   (`period_end → period_average → opening`) and raise a HIGH issue.
4. **Post the translation difference.** Translating foreign cash gives a different USD number than its nominal amount.
   That difference is posted to a clearly labelled system line (`SYS-CTA`), never absorbed silently.
5. **Triage adjustments.** Each journal entry is accepted, rejected or quarantined (section 6). Only accepted entries are
   posted, and an entry is accepted or rejected **as a whole**, never partially.
6. **Build statements.** Roll-up up the COA tree on each account type's natural side. Net income is computed from the
   tree *and* from the ledger. The equity roll-forward uses the prior TB as opening balances.
7. **Verify.** Ten checks (section 7). If any blocking check fails the status is `DRAFT - BLOCKED`.

---

## 4. Where AI is used and where it is not

| Task | Who does it | Why |
|---|---|---|
| Reading files, FX, roll-ups, net income, equity walk, verification | **Code** | Arithmetic must be exact and reproducible |
| Validating journal entries (Dr = Cr, account exists, period, self-offset) | **Code** | The rules are crisp |
| Deciding if a mapping is accepted | **Code gate** | A wrong mapping silently misstates a statement |
| Suggesting a COA account for something unknown | **LLM (optional) → human approves** | The one place with genuine ambiguity |
| Explaining a rejection in plain English | **Templates** (LLM-ready) | Kept deterministic so output is reproducible |

### The mapping gate (`fsgen/mapper.py`)

* **Auto-accepted:** only an *exact code match whose name agrees* (confidence 1.0), or an explicit **human override**.
* **Everything else is a suggestion.** Fuzzy name/range matching produces a best guess with a confidence score. If that
  confidence is below **0.90** the balance goes to the `UNMAPPED` bucket and an issue is raised. A prior-period
  account `6905 "Sundry Operating Expenses"` scores 0.89 against `6900`, so it is deliberately *not* auto-merged.

### The optional LLM advisor (`fsgen/advisor.py`, `--advisor llm`)

Off by default, so the default run is fully deterministic and offline. When enabled it is consulted **only for rows the
gate already could not auto-accept**, and its answer is treated as untrusted input:

* A suggested code that does **not** exist as a postable account in your COA is **discarded** (no hallucinated accounts).
* Its confidence is **capped below the 0.90 bar**, so it can never approve a mapping by itself. It can only put a better
  suggestion (with a reason) in front of a human.
* If the API fails or returns garbage, the pipeline continues on the deterministic path.
* It never touches exact matches, so numbers for already-clean accounts cannot change.

All four behaviours are covered by tests using a fake client (no network, no key).

---

## 5. Defects found in the input data

The PDF lists some seeded defects; the inputs README says there are more. What the system detects:

| # | Defect | Where | How it is handled |
|---|---|---|---|
| 1 | TB does not balance (Dr − Cr = −4,800 nominal) | `trial_balance.csv` | Check V1 fails; shown as "Unreconciled TB difference"; **no plug** |
| 2 | Account `9999 Suspense - Unmapped` not in COA | TB L63 | `UNMAPPED` bucket, escalated (best guess confidence 0.27) |
| 3 | `6310` appears twice (245,000 and 38,500) | TB L48–L49 | Flagged HIGH, rows summed, needs confirmation it is not a double post |
| 4 | GBP period-end rate missing | `fx_rates.csv` | Visible fallback to average 1.264; swing vs opening rate ≈ $5.4k; HIGH issue |
| 5 | An undocumented `opening` rate type exists | `fx_rates.csv` | Used for prior-period comparatives |
| 6 | Prior TB out of balance by ≈ 2.83M and has only one P&L account | `prior_period_tb.csv` | P&L comparatives not produced; BS comparatives marked indicative |
| 7 | Account renamed/recoded `6905` → `6900`? | prior vs COA | Flagged as probable rename, not auto-merged (confidence 0.89) |
| 8 | Opening retained earnings 7.24M ≠ prior closing 5.18M | TB vs prior TB | Check V9 warns; SOCIE shows 2,060,000 as *unexplained* |
| 9 | `8000 Income Tax Expense` typed `Expense` but is a parent | COA | Treated as a header; INFO issue |
| 10 | `1290 Other Assets` header has no children | COA | WARN |
| 11 | Cash-flow category `TBD` on `1150` and `2170` | COA | WARN (blocks a future cash-flow statement) |
| 12 | P&L accounts carry a `cf_category` | COA | INFO (treated as cash-flow hints) |
| 13 | PPE cost 17,007,700 (+5.2M vs prior) with no supporting capex data | TB | Not auto-detectable; listed as a question for finance (ARCHITECTURE §6) |
| 14 | Multi-currency rows for the same code (`1110` in USD/EUR/GBP) | TB | **Legitimate** per the inputs README, correctly *not* flagged as duplicates |

---

## 6. Manual adjustment decisions

| Entry | Description | Decision | Reason |
|---|---|---|---|
| JE-001 | Q4 bonus accrual | ✅ Accepted | Balanced, valid accounts |
| JE-002 | Marketing reclass from T&E | ❌ **Rejected** | Debits 28,500 ≠ credits 25,000 (off by 3,500). Never partially posted |
| JE-003 | EUR cash FX revaluation | ⚠️ **Quarantined** | Booked 11,200 cannot be reproduced from the rate table (10,730 or 19,810 depending on basis), **and** the pipeline already translates the cash, so posting it would count the FX gain twice |
| JE-004 | Bad debt top-up | ✅ Accepted | |
| JE-005 | Conference travel reclass | ⚠️ **Quarantined** | Account `6315` is not in the COA (nearest `6310`, confidence 0.64 < 0.90) |
| JE-006 | Depreciation catch-up | ✅ Accepted (note) | TB depreciation already equals the whole accumulated-depreciation movement; this adds 215,000 on top. Plausible, worth a look |
| JE-007 | Deferred tax true-up | ✅ Accepted | |
| JE-008 | Intercompany settlement (UK sub) | ❌ **Rejected** | Every line hits `2170`, so it nets to zero while claiming a settlement. Likely mis-keyed (circular) |
| JE-009 | Legal fee accrual | ✅ Accepted | |
| JE-010 | Current portion of LTD reclass | ✅ Accepted | |

Rejected and quarantined entries are excluded from the statements and listed with a plain-English reason for the finance owner.

---

## 7. Verification checks

Run after every build. **Blocking** failures set the status to `DRAFT - BLOCKED`.

| Check | What it proves | Blocking? | Result on supplied data |
|---|---|---|---|
| V1 | Source TB balances (nominal Dr = Cr) | yes | ❌ −4,800 |
| V2 | **Independent recompute**: a second, separate implementation rebuilds assets, liabilities, equity and net income from the raw rows and must match the statement build | yes | ✅ |
| V3 | Accounting equation A = L + E (+ net income) on mapped accounts | yes | ❌ −17,200 |
| V4 | The V3 gap equals exactly *ledger residual − unmapped balances* (nothing unexplained) | yes | ✅ |
| V5 | Net income via the P&L tree equals net income via the ledger | yes | ✅ |
| V6 | No unmapped balances on the statements | yes | ❌ 12,400 |
| V7 | Every account's lineage sums to its balance | yes | ✅ |
| V8 | Balances sit on their normal side | warn | ✅ |
| V9 | Opening retained earnings = prior-period closing | warn | ⚠️ 2,060,000 gap |
| V10 | SOCIE closing equity = balance-sheet equity + net income | yes | ✅ |

V2 is the important one: a bug in the ledger or tree builder cannot hide by being wrong in both places. A test deliberately
sabotages the builder (+1,000 on assets) and asserts V2 catches it.

**Self-correction policy:** the loop is bounded and non-destructive. It excludes failing adjustments and re-runs; it
never forces a balance, plugs an account, or fabricates a rate. If something cannot be fixed without judgement it stops,
quantifies the residual, and asks a human.

---

## 8. Traceability for auditors

Every ledger account carries a list of **sources**:

* `tb` — the TB row (`trial_balance.csv:L48`), currency, local amount, rate, rate type (including `FALLBACK` flags), USD amount
* `adj` — an adjustment line (`JE-004#line2`) with its memo
* `system` — system entries such as `SYS-CTA`, with the assumption stated

```bash
python3 -m fsgen.run --trace 6310
```
```
trial_balance.csv:L48   245,000.00
trial_balance.csv:L49    38,500.00
```
(the command prints the full JSON; the two lines above are the `ref` / `usd_net` pairs from it)

Check V7 enforces that these sources sum to each account's balance, and statement lines are sums of accounts, so any
cell on the Balance Sheet resolves to concrete source rows. `run_manifest.json` stores SHA-256 prefixes of every input,
the thresholds, the translation policy, the advisor used and any human overrides. Re-running identical inputs yields
**byte-identical outputs** (tested).

---

## 9. Human-in-the-loop

Anything the gate cannot auto-accept is escalated, and a human answers by supplying an override file:

```json
{
  "9999": {
    "target": "6900",
    "approved_by": "controller@example.com",
    "reason": "Suspense balance confirmed as miscellaneous operating expense per AP review"
  }
}
```

```bash
python3 -m fsgen.run --overrides examples/mapping_overrides.json --out output_override
```

* The override applies to both TB rows and adjustment lines, with full provenance (`approved_by`, `reason`) recorded in
  `mapping.json` and listed in `run_manifest.json`.
* An override pointing at a non-postable account (for example a header) is **ignored**.
* After this override the suspense balance is gone (V6 passes), but the run **stays blocked** on the genuinely
  unresolved data defects (TB imbalance, retained-earnings gap). Overrides resolve ambiguity; they do not hide errors.
* `examples/mapping_overrides.json` is a **demo** file; the reference run in `output/` does not use it.

---

## 10. Accounting assumptions and policies

These are the judgement calls. Each is surfaced in the output rather than buried in code, and each is a question for a
finance owner (see ARCHITECTURE §7).

| Assumption | Choice made | Switchable? |
|---|---|---|
| Functional currency / period | USD, 2024-Q4 | `Config` |
| EUR/GBP TB rows | Treated as **local currency** amounts | no (inputs README convention) |
| Rates | Balance sheet at period-end, P&L at period-average | no |
| Translation difference | Posted provisionally to `3310 FX Translation Reserve` | `--translation-policy pnl` posts to `7310` instead |
| TB timing | **Pre-closing** (P&L accounts present, retained earnings = opening) | no |
| Natural sides | Assets/expenses debit; everything else credit; contra accounts (`1121`, `3400`) shown negative | no |
| Auto-accept bar for mappings | 0.90 | `Config.auto_map_threshold` |

For a USD-functional entity, US GAAP remeasurement of foreign cash would normally go through P&L, which is why the
`pnl` switch exists. It changes net income from 4,783,500 to 4,970,760.20 and leaves total assets unchanged (tested).

---

## 11. Outputs

Written to `output/` on every run:

| File | Contents |
|---|---|
| `report.md` | Human-readable: P&L, Balance Sheet (with indicative prior column), SOCIE, verification table, adjustment verdicts, issues |
| `statements.json` | The same statements as structured data (trees with code, name, value, children) |
| `lineage.json` | Per-account balance and every contributing source |
| `verification.json` | The ten checks with pass/fail, detail and blocking flag |
| `issues.json` | Every issue with severity (`blocker`/`high`/`warn`/`info`), subject, message, recommended action |
| `mapping.json` | Per-row mapping decision: target, confidence, method, reason |
| `adjustments.json` | Verdict and reasons per journal entry |
| `run_manifest.json` | Input hashes, thresholds, policy, advisor, overrides, status |

---

## 12. The Streamlit UI

```bash
python3 -m streamlit run app.py
```

* Header banner with the status, plus metrics: net income, total assets, failed blocking checks, blocker/high issues.
* Tabs: **P&L**, **Balance Sheet**, **Equity (SOCIE)**, **Verification**, **Adjustments** (with each entry's lines),
  **Issues** (filter by severity), **Trace a number** (pick an account, see its sources), **Mapping** (confidence scores),
  **Downloads**.
* The sidebar lets you **upload a replacement for any input file**; the whole pipeline re-runs. The bundled `inputs/`
  folder is copied to a temp directory first, so the originals are never altered.

---

## 13. Code layout

```
fsgen/
  models.py        Value types (Money, Issue, Source, Balance, Node, Mapping) and Config (every policy knob)
  io_utils.py      CSV reading with file:line refs, hashing, JSON serialisation of Decimal/dataclasses
  coa.py           Chart of accounts hierarchy, header detection, sign conventions, COA lint
  fx.py            Rate table with a visible fallback chain
  mapper.py        The mapping gate: deterministic proposal, human overrides, validated advisor hook
  advisor.py       Optional LLM proposer (validate-or-discard, confidence-capped, fails safe)
  ledger.py        TB -> USD ledger with lineage; duplicate detection; translation-difference posting
  adjustments.py   Journal entry triage: balance, accounts, period, self-offset, FX reval, depreciation overlap
  statements.py    COA roll-up, P&L, Balance Sheet, equity roll-forward (SOCIE)
  verify.py        The ten checks, including the independent shadow recompute
  pipeline.py      run_pipeline(): inputs -> Result. Pure: no printing, no writing
  render.py        Markdown report
  cli.py / run.py  Command-line entry (python -m fsgen.run)
app.py             Streamlit UI (thin; calls run_pipeline)
tests/             20 offline tests
examples/          Sample human override file
inputs/            Provided data (never modified)
output/            Reference run
```

Design choices worth knowing:

* **`run_pipeline` is a pure function** returning a `Result`; the CLI and the UI are thin shells around it.
* **All money is `Decimal`**, never float.
* **Policy lives in `Config`**, not scattered constants.
* **Type hints throughout**, stdlib-only core, Python 3.9 compatible.

---

## 14. Testing

```bash
python3 -m unittest discover tests
```

20 tests, run in about a quarter of a second, no network or API key. They cover:

* **Reference numbers:** net income, total assets, TB residual, unmapped balance.
* **Adjustment triage:** each of the ten journal entries gets its expected verdict.
* **Defect detection:** every seeded defect is found; legitimate multi-currency rows are *not* flagged.
* **Lineage:** every account's sources sum to its balance.
* **All ten checks run**, and V2 agrees with the build.
* **SOCIE:** the 2,060,000 retained-earnings gap is surfaced as unexplained; closing equity ties to the balance sheet.
* **Safety properties:** inputs are never modified; outputs are byte-identical across runs.
* **Human-in-the-loop:** override resolves suspense with an audit trail; overrides to headers are ignored.
* **LLM advisor:** hallucinated codes discarded; valid suggestions never auto-accepted; outages don't break the run; exact matches untouched.
* **Policy switch:** the `pnl` policy moves exactly the translation difference into income.
* **No false alarms:** once the data defects are fixed, the same code returns READY.
* **Verifier power:** sabotaging the statement builder is caught by V2.
* **CLI:** exit codes and `--trace`.

CI configuration for GitHub Actions is in `.github/workflows/ci.yml`.

---

## 15. Limitations and what is not built

**Built:** mapping gate with human overrides, optional LLM advisor, adjustment triage, FX with visible fallbacks, P&L,
Balance Sheet, SOCIE, ten-check verifier, lineage, manifest, translation-policy switch, Streamlit UI, tests, CI.

**Designed but not built** (see ARCHITECTURE):

* **Cash Flow Statement.** Needs a balanced prior TB and resolved `TBD` cash-flow categories, both missing in the data.
* **Intercompany elimination.** The data has no counterparty entity IDs, so only the self-offsetting case (JE-008) is caught.
* **LLM-written explanations.** Explanations are templated so output stays reproducible offline.

**Known limits:**

* The live LLM call has been tested only against a fake client (no API key in the test environment).
* Fuzzy matching is simple string similarity, which is a weak signal; production would use embeddings plus hierarchy features.
* Single entity and single COA. Consolidation needs per-entity ledgers and per-account translation policy.
* Everything is in memory; millions of GL lines would need columnar storage and lineage by reference.
* The FX and pre-closing assumptions in section 10 need confirmation from a finance owner.

---

## 16. FAQ / troubleshooting

**Why is the result "BLOCKED"? Is it broken?** No, that is the intended result for this data. The TB is out of balance and
contains an unmapped suspense account and a missing FX rate. A system that produced a clean-looking statement here would be wrong.

**Why not just plug the 4,800 difference?** Because it would hide an error. It is shown explicitly so a human decides.

**Why is JE-003 quarantined when the amount looks reasonable?** The amount cannot be reproduced from the rate table, and
the pipeline already translates the cash, so it would double count.

**Can I try my own data?** Yes: upload files in the UI sidebar, or point `--inputs` at a folder with the same five files.

**`streamlit: command not found`?** Use `python3 -m streamlit run app.py`.

**`ModuleNotFoundError: No module named 'fsgen'`?** Run commands from the project root (the `tsl` folder), not from inside `fsgen/` or `inputs/`.

**Does anything change my input files?** No. The pipeline only reads them, and a test asserts they are byte-identical after a run.

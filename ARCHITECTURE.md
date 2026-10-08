# Agentic financial statement generation — architecture

## 0. Position in one paragraph
"Generate financials" is not one task. It is ~8 sub-problems with very different reliability needs. Arithmetic, FX, roll-ups and
verification must be **deterministic code** (a wrong number is a restatement). LLMs earn their keep only where the input is
*language or judgment* and the output is a **proposal that a deterministic gate accepts or escalates**: mapping unknown
accounts, explaining rejections, narrating variances, triaging issues. An LLM never emits a figure that reaches a statement.

## 1. Decomposition and who does each part

| # | Sub-problem | Owner | Why |
|---|---|---|---|
| 1 | Ingest + schema/duplicate/balance checks on TB | Code | Exact, cheap, must be reproducible |
| 2 | TB account → COA node mapping | Code first (exact code), LLM *proposes* for the rest, human approves | Only place with real ambiguity; hallucination risk is highest here |
| 3 | Adjustment validation (Dr=Cr, accounts exist, period, self-offsetting, recompute FX/accrual amounts) | Code | Rules are crisp; LLM only writes the plain-English reason |
| 4 | FX translation / remeasurement | Code, policy table | Rate choice is accounting policy, not inference. Missing rate = explicit fallback + flag |
| 5 | Intercompany elimination | Code matches pairs; LLM suggests candidate pairs for near-misses | Circularity/self-offset is a graph check |
| 6 | Statement build (roll-up, signs, net income, equity walk, later CFS) | Code | Arithmetic |
| 7 | Verification | Code, **independent second computation path** | See §4 |
| 8 | Explanation, variance narrative, issue triage, review-queue UX | LLM | Language task; outputs checked against numbers it is handed |

## 2. Agent topology
**One deterministic pipeline with a small number of LLM "advisor" tools, not a swarm.** Orchestration is a plain state machine:

```
 ingest → validate → map ──(escalate)──► human review queue
                      │
                  triage adjustments ──(reject/quarantine)──► finance owner
                      │
                  FX + post accepted JEs → build statements → verify ──fail──► BLOCKED + diagnosis
                                                                 │ pass
                                                                 ▼ release candidate + lineage + manifest
```
Only two components call an LLM: **Mapper-advisor** (`fsgen/advisor.py`, opt-in with `--advisor llm`; its suggestion is validated against the real COA — an unknown code is discarded — and its confidence is capped below the auto-accept bar, so it can only ever add a better suggestion to the human queue; outages fall back silently to the deterministic path; all tested with a fake client) and
**Explainer** (turn structured issue records into finance-user prose; it is given the facts and cannot change them).
A multi-agent swarm adds latency, cost and non-determinism to steps that are a 200-line script; I'd add a second agent only
when a task needs a different tool set or a context window that would be polluted (e.g. a reconciliation investigator
digging through 100k GL lines).

## 3. Failure modes and handling (all implemented in the prototype unless marked †)

| Failure | Handling |
|---|---|
| Hallucinated mapping | Auto-accept only exact code match with name agreement (conf ≥ 0.90). Fuzzy/LLM proposals are suggestions: balance is parked in an explicit `UNMAPPED` bucket shown on the statement and the statements are blocked. Approved mappings persist in a versioned mapping table keyed by (entity, source code, name hash) so the human answers once. |
| Dr ≠ Cr after adjustments | Entry-level check. Whole entry rejected (never partially posted) with a plain-English reason (JE-002: off by 3,500). Ledger-level check recomputed after posting. |
| Account fits no COA node | Quarantine, show nearest candidate and confidence (JE-005 → 6310, 0.64; 9999 suspense: no good candidate, 0.27). |
| FX rate gap | Explicit fallback chain (period_end → period_average → opening), each use is a HIGH issue naming the rate and the affected balances; never silent. (GBP period-end missing here; GBP cash 412,300 translated at 1.264 provisionally. Swing vs opening rate 1.251 is ≈ $5.4k.) |
| Circular / self-offsetting IC | Same-account-both-sides and (†) cycle detection across the entity graph; JE-008 rejected. † Counterparty entity IDs are absent from the data so true IC elimination is designed, not built. |
| Adjustment double counts system work | JE-003 (manual FX reval) overlaps the pipeline's own translation, and its amount can't be reproduced from the rate table (10,730 vs 19,810 vs booked 11,200): quarantined. |
| TB doesn't balance | Never plugged. Shown as "Unreconciled TB difference" on the face of the BS, verification fails, status = `DRAFT – BLOCKED`. |
| Duplicate code | Same code + same currency → flagged, rows summed, exact repeats would be quarantined. Different currency is legitimate (README convention), not flagged. |

## 4. Validation and self-correction loop
Verification recomputes from a **different path** than the build (ledger vs. statement tree), so a bug in one can't hide in both:

V1 source TB balanced · V2 **independent recompute** (a second, separate implementation from raw rows; a test sabotages the builder and proves it catches it) · V3 A = L + E(+NI) · V4 statement gap reconciles exactly to
(ledger residual − unmapped) · V5 net income via tree = via ledger · V6 no unmapped balances · V7 every account's lineage sums to its balance ·
V8 balances on normal side · V9 opening RE = prior closing RE · V10 SOCIE closes to balance-sheet equity.

On failure the loop is **bounded and non-destructive**: (1) classify the failure (data defect vs. my defect); (2) attempt only
whitelisted remediations that need no judgement — exclude failing adjustment, re-run (done in triage), re-ask the Mapper-advisor at
most twice with the validation error attached †; (3) otherwise stop with `BLOCKED`, the residual quantified, and candidate causes
(†: subset-sum search of TB rows/JEs equal to the residual — here the 4,800 residual matches nothing). The system **never** forces
a balance or invents a rate. Release requires all blocking checks green *or* an explicit, logged human waiver per check.

## 5. Traceability
Every ledger account carries a `src` list: TB row (`trial_balance.csv:L48`, local amount, currency, rate, rate type, USD) and
adjustment lines (`JE-004#line2`), plus system entries (`SYS-CTA`). Statement cells are sums of accounts; V7 enforces the sums.
`python -m fsgen.run --trace 1110` prints the chain for a cell. A `run_manifest.json` stores input hashes, thresholds and status;
re-running the same inputs produces byte-identical output (tested). For production: content-addressed immutable runs, append-only
mapping/override log with user + timestamp + reason, and every LLM call stored with prompt, model version, and the validator's verdict.

## 6. Prototype slice: Statement generator (P&L + BS) with verifier
Chosen because it forces every other part to exist at a thin level (mapper gate, adjustment triage, FX) and tests whether the
verifier genuinely catches errors. See `fsgen/`. Result on the supplied data: **BLOCKED**, correctly. Net income 4,783,500 (computed, lineage-complete); BS cannot balance because the source is
defective: TB is 4,800 out of balance in credits, 12,400 sits in an unmapped suspense code, GBP rate missing. Those are shown, not hidden.

### Defects found (beyond the PDF's list)
- Repeated 6310 (245,000 + 38,500) — different amounts, so a possible double-post, not provably one.
- TB imbalance is **4,800** (nominal), not obviously explained by anything; with suspense it is 17,200 against mapped accounts.
- Rejected/quarantined JEs: 002 (Dr≠Cr), 003 (irreproducible + double count), 005 (6315), 008 (self-offset). JE-006 accepted but flagged (850k TB depreciation already equals the whole accumulated-depreciation movement).
- GBP period-end missing; an extra `opening` rate type exists that the README doesn't mention.
- Prior TB: out of balance by ~2.83M (nominal), only one P&L account, so PL comparatives are impossible; 6905 vs 6900 rename (conf 0.89 — just under the bar, by design); current opening RE 7.24M ≠ prior RE 5.18M (2.06M unexplained).
- COA: 8000 typed `Expense` but is a parent; 1290 empty header; CF category TBD on 1150/2170; 1121 and 3400 contra-account sign handling; P&L accounts carrying cf_category.
- PPE cost 17,007,700 (+5.2M vs prior) — a round-number oddity worth asking about; no capex data to support it.

## 7. Clarifying questions I would have asked (and the assumption I made instead)
1. Are EUR/GBP TB rows in local currency? *Assumed yes (README says so)*; the USD equivalent they were carried at is unknown, hence the provisional CTA.
2. Entity is USD-functional: should translation difference hit OCI (3310) or P&L remeasurement (7310)? *Assumed 3310, flagged; needs controller.*
3. Is the TB pre- or post-closing? *Assumed pre-closing (P&L accounts present, RE = opening)*. The prior TB looks post-closing, which is why the RE roll doesn't tie.
4. Which rate for GBP period-end, and is "opening" the prior period-end rate?
5. Is the TB single-entity? IC payable 1.24M has no counterparty, so elimination can't be tested.
6. Materiality threshold for auto-waiving small differences like 4,800?

## 8. Built vs designed only
**Built:** mapper gate with human overrides (`--overrides`), LLM mapping advisor (opt-in), adjustment triage, FX with visible fallbacks, P&L, Balance Sheet, SOCIE roll-forward (anything not supported by an input is shown as *unexplained*, not invented), 10-check verifier, lineage, idempotent manifest, translation-policy switch (`--translation-policy oci|pnl`), Streamlit UI, 20 tests, CI.
**Designed only:** Cash Flow Statement (indirect method from BS deltas + COA cf_category; blocked on TBD tags and a balanced prior TB), true multi-entity intercompany elimination (no counterparty IDs in the data), LLM *explainer* (explanations are templated so the output is reproducible offline).

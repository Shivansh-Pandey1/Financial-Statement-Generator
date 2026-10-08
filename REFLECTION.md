# Reflection

**With 3 months instead of 8 hours.** Build the Cash Flow Statement on the same ledger/lineage model (SOCIE already exists), productionise the mapping memory (approved mappings keyed per entity with provenance) and an
evaluation set of real, anonymised TBs with known-good statements; I'd measure mapper precision/recall and verifier catch-rate
before trusting any of it. Add real intercompany
elimination with counterparty entity IDs, a review UI where each blocker is one click to approve / override with a logged
reason, and the LLM advisors behind a strict schema-validate-or-discard wrapper.

**Where the prototype breaks at scale.** Fuzzy name matching is O(accounts × COA) and string similarity is a weak signal —
it would need embeddings plus hierarchy-aware features. One COA/one entity is baked in: consolidation needs per-entity ledgers,
intercompany netting, per-entity functional currency and a translation policy per account class (my single "translate cash and
park the difference in 3310" shortcut is wrong for many structures). Everything is in memory with Decimal; thousands of accounts
are fine, millions of GL lines need columnar storage and lineage by reference rather than embedded lists. The adjustment checks
are hand-written rules; a real batch of hundreds of JEs needs rule configuration per client.

**AI tools.** I used Claude and Cursor to read the brief, profile the seeded data, and draft the code, tests and docs. It was fast at spotting data defects and scaffolding. Where it went wrong or needed correcting: my first net-income cross-check failed because I assumed the COA's `Header` type marks parents (8000 is typed `Expense` yet has children) and I'd mixed revenue/expense signs in the non-operating roll-up — the verifier caught it, which is the point. Early output also flagged multi-currency rows for the same account as duplicates, contradicting the README; I fixed that and added a test.

**What I think is underestimated.** Accounting *policy* hidden inside "arithmetic": which FX rate, where translation differences go,
whether a TB is pre- or post-closing, what counts as a restatement vs a current-period correction. These look like data
problems but are judgement calls with legal consequences, so the product's real job is surfacing the decision to the right
person with the evidence, not resolving it. The second is trust: one silent plug destroys an auditor's confidence in every number
the system ever produced.

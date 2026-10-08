"""Orchestration: pure function from input folder to a Result. No printing, no writing (see cli.py)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from . import adjustments as adj
from .coa import load_coa
from .fx import RateTable
from .io_utils import read_csv, sha
from .ledger import Ledger, build_ledger, post_translation_difference
from .mapper import Advisor, Mapper
from .models import ZERO, Config, Issue
from .statements import Statements, generate, socie
from .verify import Check, shadow_totals, verify

INPUT_FILES = ("trial_balance.csv", "prior_period_tb.csv", "chart_of_accounts.csv", "fx_rates.csv", "manual_adjustments.json")


@dataclass
class Result:
    status: str
    blocked: bool
    statements: Statements
    socie: List[Dict]
    checks: List[Check]
    issues: List[Issue]
    verdicts: List[adj.Verdict]
    ledger: Ledger
    comparatives: Dict[str, Decimal]
    mapping: Dict[str, List[dict]]
    unmapped_net: Decimal
    residual: Decimal
    manifest: Dict


def load_overrides(path: Optional[Path]) -> Dict[str, dict]:
    """Human-approved mappings: {"9999": {"target": "6900", "approved_by": "...", "reason": "..."}}."""
    return json.loads(Path(path).read_text()) if path else {}


def run_pipeline(inp: Path, cfg: Config = Config(), overrides: Optional[Dict[str, dict]] = None,
                 advisor: Optional[Advisor] = None) -> Result:
    inp = Path(inp)
    issues: List[Issue] = []
    coa = load_coa(inp, issues)
    rates = RateTable(inp, cfg, issues)
    mapper = Mapper(coa, cfg, overrides, advisor)

    # current period: map -> translate -> triage adjustments -> post accepted only
    tb_rows, prior_rows = read_csv(inp / "trial_balance.csv"), read_csv(inp / "prior_period_tb.csv")
    ledger, mapping = build_ledger(tb_rows, "trial_balance.csv", coa, rates, mapper, cfg, "period_end", "current", issues)
    tb_nominal_net = ledger.nominal_total()
    post_translation_difference(ledger, cfg, issues)
    verdicts = adj.triage(inp, coa, rates, mapper, cfg, tb_rows, prior_rows, issues)
    adj.post_accepted(ledger, verdicts)
    st = generate(coa, ledger)
    ni = -sum((b.net for c, b in ledger.items() if c in coa and coa[c]["statement"] == "PL"), ZERO)

    # prior period (comparatives) through the same machinery, opening rates
    prior_issues: List[Issue] = []
    prior, prior_map = build_ledger(prior_rows, "prior_period_tb.csv", coa, RateTable(inp, cfg, prior_issues), mapper, cfg,
                                    "opening", "prior", prior_issues)
    issues += [i for i in prior_issues if i.code != "FX_MISSING_RATE"]
    p_nominal = prior.nominal_total()
    issues.append(Issue(
        "PRIOR_UNBALANCED", "high", "prior_period_tb.csv",
        f"Prior-period TB is itself out of balance by {p_nominal:,.2f} (Dr - Cr, nominal) and contains only one P&L account "
        f"({', '.join(sorted(c for c in prior if c not in coa or coa[c]['statement'] == 'PL'))}). It looks like a balance-sheet-only "
        "extract missing P&L/closing entries, so P&L comparatives are not produced and BS comparatives are indicative only.",
        "Obtain a complete, balanced prior TB.", str(p_nominal)))
    for m in prior_map:
        if m["method"] == "fuzzy_name_and_range" and m["confidence"] >= 0.8:
            issues.append(Issue("PRIOR_RENAMED", "warn", m["code"],
                                f"Prior {m['code']} '{m['name']}' is absent from the COA; closest is {m['target']} "
                                f"(confidence {m['confidence']}). Treated as a probable rename but not auto-merged."))
    prior_re = coa.signed("3200", prior["3200"].net) if "3200" in prior else None

    socie_rows = socie(coa, ledger, prior, ni)
    key_of = {s.ref: k for k, b in ledger.items() for s in b.sources if s.kind == "tb"}
    adj_lines = [l for v in verdicts if v.status == "accepted" for l in v.lines]
    shadow = shadow_totals(tb_rows, key_of, adj_lines, rates, coa, cfg)
    checks = verify(cfg, coa, ledger, st, ni, tb_nominal_net, prior_re, shadow, socie_rows, issues)

    blocked = any(c.blocking for c in checks)
    status = "DRAFT - BLOCKED (do not release)" if blocked else "READY FOR REVIEW"
    manifest = dict(inputs={f: sha(inp / f) for f in INPUT_FILES}, auto_map_threshold=cfg.auto_map_threshold,
                    translation_policy=cfg.translation_policy, advisor=type(advisor).__name__ if advisor else "none",
                    human_overrides=sorted(overrides or {}), status=status)
    comparatives = {c: coa.signed(c, b.net) for c, b in prior.items() if c in coa}
    return Result(status, blocked, st, socie_rows, checks, issues, verdicts, ledger, comparatives,
                  dict(current=mapping, prior=prior_map), ledger.unmapped_net(), ledger.net_total(), manifest)

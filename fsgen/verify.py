"""Verification. Check V2 is a second, independent implementation (raw rows -> totals) so a bug in the
ledger/tree build cannot hide by being wrong in both places."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional

from .coa import Coa
from .fx import RateTable
from .ledger import Ledger
from .models import ZERO, Config, Issue, money
from .statements import Statements


@dataclass
class Check:
    check: str
    passed: bool
    detail: str
    blocking: bool


def shadow_totals(tb_rows: List[dict], key_of: Dict[str, str], adj_lines: List[dict], rates: RateTable, coa: Coa,
                  cfg: Config) -> Dict[str, Decimal]:
    net: Dict[str, Decimal] = defaultdict(Decimal)
    cta = ZERO
    for r in tb_rows:
        key = key_of[r["_ref"]]
        if key not in coa:
            continue  # unmapped: excluded here, reported separately by V6
        wanted = "period_end" if coa[key]["statement"] == "BS" else "period_average"
        rate, _ = rates.get(r["currency"], wanted, "shadow")
        nominal = money(r["debit"]) - money(r["credit"])
        usd = (nominal * rate).quantize(Decimal("0.01"))
        net[key] += usd
        cta += usd - nominal
    for l in adj_lines:
        net[l["account"]] += money(l["debit"]) - money(l["credit"])
    net["3310" if cfg.translation_policy == "oci" else "7310"] -= cta
    by_type = lambda t: sum((v for k, v in net.items() if coa[k]["account_type"] in t), ZERO)
    return dict(assets=by_type({"Asset"}), liabilities=-by_type({"Liability"}), equity=-by_type({"Equity"}),
                net_income=-sum((v for k, v in net.items() if coa[k]["statement"] == "PL"), ZERO))


def verify(cfg: Config, coa: Coa, ledger: Ledger, st: Statements, ni: Decimal, tb_nominal_net: Decimal, prior_re: Optional[Decimal],
           shadow: Dict[str, Decimal], socie_rows: List[dict], issues: List[Issue]) -> List[Check]:
    tol = cfg.tolerance
    checks: List[Check] = []

    def chk(name: str, ok: bool, detail: str, blocker: bool = True) -> None:
        checks.append(Check(name, bool(ok), detail, blocker and not ok))

    A, L, E = (n.value if n else ZERO for n in (st.assets, st.liabilities, st.equity))
    ledger_net, unmapped = ledger.net_total(), ledger.unmapped_net()
    gap = A - L - (E + ni)

    chk("V1 source TB balanced (nominal Dr = Cr)", abs(tb_nominal_net) <= tol,
        f"Dr - Cr = {tb_nominal_net:,.2f} before any FX or adjustments")
    diffs = {k: v - got for k, v, got in (("assets", shadow["assets"], A), ("liabilities", shadow["liabilities"], L),
                                          ("equity", shadow["equity"], E), ("net income", shadow["net_income"], ni))}
    chk("V2 independent recompute from raw rows matches statements", all(abs(d) <= tol for d in diffs.values()),
        "second implementation vs statement build, differences: " + ", ".join(f"{k} {d:,.2f}" for k, d in diffs.items()))
    chk("V3 accounting equation A = L + E (mapped accounts)", abs(gap) <= tol,
        f"A {A:,.2f} - L {L:,.2f} - (E {E:,.2f} + current NI {ni:,.2f}) = {gap:,.2f}")
    chk("V4 statement gap reconciles to ledger residual and unmapped balances", abs(gap - (ledger_net - unmapped)) <= tol,
        f"statement gap {gap:,.2f} = ledger residual {ledger_net:,.2f} - unmapped {unmapped:,.2f}")
    chk("V5 net income independent recompute", abs(st.net_income - ni) <= tol, f"P&L tree {st.net_income:,.2f} vs ledger sum {ni:,.2f}")
    chk("V6 no unmapped balances on statements", abs(unmapped) <= tol, f"unmapped net Dr {unmapped:,.2f}")
    bad = [c for c, b in ledger.items() if abs(sum((s.usd_net for s in b.sources), ZERO) - b.net) > tol]
    chk("V7 lineage sums to every account balance", not bad, f"mismatches: {bad or 'none'}")
    odd = [c for c, b in ledger.items() if c in coa and coa[c]["normal_balance"] in ("Debit", "Credit") and b.net != 0
           and not c.startswith("73") and c != "7400" and (b.net > 0) != (coa[c]["normal_balance"] == "Debit")]
    chk("V8 balances sit on their normal side", not odd, f"abnormal: {odd or 'none'}", blocker=False)
    if prior_re is not None:
        open_re = coa.signed("3200", ledger["3200"].net)
        chk("V9 opening RE = prior-period closing RE", abs(open_re - prior_re) <= tol,
            f"current 3200 {open_re:,.2f} vs prior TB 3200 {prior_re:,.2f}; unexplained movement {open_re - prior_re:,.2f} "
            "(prior TB has no P&L accounts, so NI/dividends cannot be bridged)", blocker=False)
    total = socie_rows[-1]
    chk("V10 SOCIE closing equity = balance sheet equity + net income", abs(total["closing"] - (E + ni)) <= tol,
        f"SOCIE closing {total['closing']:,.2f} vs BS {E + ni:,.2f}; unexplained equity movement {total['unexplained_movement']:,.2f}")
    for c in checks:
        if not c.passed:
            issues.append(Issue("VERIFY_FAIL", "blocker" if c.blocking else "warn", c.check, c.detail,
                                "Statements are labelled DRAFT; no plug has been posted." if c.blocking else "Review."))
    return checks

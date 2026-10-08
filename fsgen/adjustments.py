from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import List

from .coa import Coa
from .fx import RateTable
from .ledger import Ledger
from .mapper import Mapper
from .models import Config, Issue, Source, money


@dataclass
class Verdict:
    id: str
    description: str
    source: str
    status: str  # accepted | rejected | quarantined
    reasons: List[str] = field(default_factory=list)
    lines: List[dict] = field(default_factory=list)  # lines with approved account substitutions applied

    def downgrade(self, status: str) -> None:
        if self.status == "accepted":
            self.status = status


def triage(inp: Path, coa: Coa, rates: RateTable, mapper: Mapper, cfg: Config, tb_rows: List[dict],
           prior_rows: List[dict], issues: List[Issue]) -> List[Verdict]:
    doc = json.loads((inp / "manual_adjustments.json").read_text())
    verdicts = []
    for je in doc["entries"]:
        v = Verdict(je["id"], je["description"], je["source"], "accepted")
        for l in je["lines"]:
            m = mapper.map(l["account"], je["description"] + " " + l.get("memo", ""))
            acct = m.target if (m.auto and m.target != l["account"]) else l["account"]
            if acct != l["account"]:
                v.reasons.append(f"Account {l['account']} replaced by {acct} ({m.reason}).")
            v.lines.append(dict(l, account=acct))
            if not coa.is_postable(acct):
                v.downgrade("quarantined")
                v.reasons.append(
                    f"Account {l['account']} is not a postable account in the chart of accounts (closest: {m.target} "
                    f"{coa[m.target]['account_name'] if m.target else '-'!r}, confidence {m.confidence} - below the "
                    f"{cfg.auto_map_threshold} auto-accept bar). Needs a finance owner to confirm the right account.")
        dr = sum(money(l["debit"]) for l in v.lines)
        cr = sum(money(l["credit"]) for l in v.lines)
        if abs(dr - cr) > cfg.tolerance:
            v.status = "rejected"
            v.reasons.insert(0, f"Debits ({dr:,.2f}) do not equal credits ({cr:,.2f}); out of balance by {abs(dr - cr):,.2f}. "
                                "An unbalanced entry would silently break the balance sheet, so none of it is posted.")
        accts = {l["account"] for l in v.lines}
        if len(v.lines) >= 2 and len(accts) == 1:
            v.status = "rejected"
            v.reasons.append(
                f"Every line hits the same account ({next(iter(accts))}), so the entry nets to zero and changes nothing, yet the "
                "description says a settlement happened. A real settlement must touch another account (cash, or the "
                "counterparty's intercompany receivable). Likely a mis-keyed entry.")
        if not je["date"].startswith(tuple(_period_months(cfg.period))):
            v.downgrade("quarantined")
            v.reasons.append(f"Date {je['date']} is outside {doc['period']}.")
        _check_fx_reval(v, tb_rows, rates)
        _check_depreciation_overlap(v, tb_rows, prior_rows, issues)
        verdicts.append(v)
    for v in verdicts:
        if v.status != "accepted":
            issues.append(Issue("ADJ_" + v.status.upper(), "high", v.id, " ".join(v.reasons),
                                "Entry excluded from statements; finance owner to correct and resubmit."))
    return verdicts


def _period_months(period: str):
    year, q = period.split("-Q")
    first = (int(q) - 1) * 3 + 1
    return [f"{year}-{m:02d}" for m in range(first, first + 3)]


def _check_fx_reval(v: Verdict, tb_rows, rates: RateTable) -> None:
    """The pipeline already translates foreign cash itself; a manual reval would double count, and its amount must
    be reproducible from the rate table."""
    if v.status != "accepted" or not any(l["account"] == "7310" for l in v.lines) or "FX" not in v.description:
        return
    eur = sum((money(r["debit"]) - money(r["credit"]) for r in tb_rows if r["currency"] == "EUR" and r["account_code"] == "1110"),
              Decimal(0))
    end, avg, opn = (rates.rates[("EUR", t)] for t in ("period_end", "period_average", "opening"))
    booked = money(next(l["credit"] for l in v.lines if l["account"] == "7310"))
    v.status = "quarantined"
    v.reasons.append(
        f"Booked uplift {booked:,.2f} cannot be reproduced: EUR {eur:,.2f} x (period-end {end} - average {avg}) = "
        f"{(eur * (end - avg)).quantize(Decimal('0.01')):,.2f}; x (period-end - opening {opn}) = "
        f"{(eur * (end - opn)).quantize(Decimal('0.01')):,.2f}. Also, this pipeline already translates EUR/GBP cash at the "
        "period-end rate, so posting this on top would count the FX gain twice. Held until the reval basis is confirmed.")


def _check_depreciation_overlap(v: Verdict, tb_rows, prior_rows, issues: List[Issue]) -> None:
    if v.status != "accepted" or not {"6500", "1211"} <= {l["account"] for l in v.lines}:
        return
    net = lambda rows, c: sum((money(r["debit"]) - money(r["credit"]) for r in rows if r["account_code"] == c), Decimal(0))
    expense, movement = net(tb_rows, "6500"), net(prior_rows, "1211") - net(tb_rows, "1211")
    if expense == movement:
        issues.append(Issue("ADJ_REVIEW", "info", v.id, (
            f"Accepted, but note TB depreciation expense ({expense:,.0f}) already equals the full movement in accumulated "
            f"depreciation vs prior period ({-net(tb_rows, '1211'):,.0f} - {-net(prior_rows, '1211'):,.0f}); this entry adds "
            "on top. Plausible, worth a look.")))


def post_accepted(ledger: Ledger, verdicts: List[Verdict]) -> None:
    for v in verdicts:
        if v.status != "accepted":
            continue
        for i, l in enumerate(v.lines, 1):
            n = money(l["debit"]) - money(l["credit"])
            ledger.post(l["account"], n, Decimal(0), Source("adj", f"{v.id}#line{i}", n, memo=l.get("memo", "")))

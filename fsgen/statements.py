from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional

from .coa import Coa
from .ledger import Ledger
from .models import ZERO, Node


def build_tree(coa: Coa, ledger: Ledger, root: str, income_effect: bool = False) -> Optional[Node]:
    """Roll balances up the COA hierarchy. income_effect=True shows credit-positive values (mixed-sign groups)."""
    def val(c: str) -> Decimal:
        n = ledger[c].net
        return -n if income_effect else coa.signed(c, n)

    def node(c: str) -> Optional[Node]:
        kids = [k for k in (node(k) for k in sorted(coa.children.get(c, []))) if k]
        if kids or coa[c]["account_type"] == "Header":
            if not kids:
                return None
            own = [Node(c, coa[c]["account_name"] + " (own balance)", val(c))] if c in ledger else []
            kids = own + kids
            return Node(c, coa[c]["account_name"], sum((k.value for k in kids), ZERO), kids)
        return Node(c, coa[c]["account_name"], val(c)) if c in ledger else None

    return node(root)


@dataclass
class Statements:
    revenue: Optional[Node]
    cogs: Optional[Node]
    opex: Optional[Node]
    non_operating: Optional[Node]
    tax: Optional[Node]
    gross_profit: Decimal
    operating_income: Decimal
    pre_tax_income: Decimal
    net_income: Decimal
    assets: Optional[Node]
    liabilities: Optional[Node]
    equity: Optional[Node]


def generate(coa: Coa, ledger: Ledger) -> Statements:
    v = lambda n: n.value if n else ZERO
    rev, cogs, opex = (build_tree(coa, ledger, r) for r in ("4000", "5000", "6000"))
    nonop, tax = build_tree(coa, ledger, "7000", income_effect=True), build_tree(coa, ledger, "8000")
    gross = v(rev) - v(cogs)
    op = gross - v(opex)
    pre = op + v(nonop)
    return Statements(rev, cogs, opex, nonop, tax, gross, op, pre, pre - v(tax),
                      *(build_tree(coa, ledger, r) for r in ("1000", "2000", "3000")))


EQUITY_COMPONENTS = [("3100", "Common stock"), ("3110", "Additional paid-in capital"), ("3200", "Retained earnings"),
                     ("3310", "FX translation reserve"), ("3400", "Treasury stock")]


def socie(coa: Coa, ledger: Ledger, prior: Ledger, net_income: Decimal) -> List[Dict]:
    """Opening (prior TB) -> net income -> other movements -> closing (current). With no dividend / restatement /
    OCI data in the inputs, anything beyond net income is surfaced as 'unexplained', never invented."""
    rows = []
    for code, label in EQUITY_COMPONENTS:
        opening = coa.signed(code, prior[code].net) if code in prior else ZERO
        closing = coa.signed(code, ledger[code].net) if code in ledger else ZERO
        ni = net_income if code == "3200" else ZERO
        closing += ni
        rows.append(dict(component=f"{code} {label}", opening=opening, net_income=ni,
                         unexplained_movement=closing - opening - ni, closing=closing))
    rows.append(dict(component="Total equity", **{k: sum((r[k] for r in rows), ZERO) for k in
                                                   ("opening", "net_income", "unexplained_movement", "closing")}))
    return rows

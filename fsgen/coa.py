from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Dict, List

from .io_utils import read_csv
from .models import Issue


class Coa:
    """Chart of accounts with hierarchy helpers. A code with children is a header even if typed otherwise (8000)."""

    def __init__(self, rows: List[Dict[str, str]]):
        self.accounts: Dict[str, Dict[str, str]] = {r["account_code"]: r for r in rows}
        self.children: Dict[str, List[str]] = {}
        for c, r in self.accounts.items():
            self.children.setdefault(r["parent_code"], []).append(c)

    def __contains__(self, code: str) -> bool:
        return code in self.accounts

    def __getitem__(self, code: str) -> Dict[str, str]:
        return self.accounts[code]

    def is_postable(self, code: str) -> bool:
        return code in self.accounts and self.accounts[code]["account_type"] != "Header"

    def postable(self):
        return [c for c in self.accounts if self.is_postable(c)]

    def signed(self, code: str, net: Decimal) -> Decimal:
        """Present on the natural side of the account type (assets/expenses Dr, everything else Cr)."""
        t = self.accounts[code]["account_type"] if code in self.accounts else "Asset"
        return net if t in ("Asset", "Expense") else -net


def load_coa(inp: Path, issues: List[Issue]) -> Coa:
    coa = Coa(read_csv(inp / "chart_of_accounts.csv"))
    for c, r in coa.accounts.items():
        if r["account_type"] == "Header" and c not in coa.children:
            issues.append(Issue("COA_EMPTY_HEADER", "warn", c, f"Header {c} '{r['account_name']}' has no children mapped.",
                                "Confirm whether it is dead or accounts are missing."))
        if r["cf_category"] == "TBD":
            issues.append(Issue("COA_AMBIGUOUS_CF", "warn", c, f"{c} '{r['account_name']}' has cash-flow category TBD.",
                                "Finance to assign Operating/Investing/Financing before the cash flow statement is built."))
        if not r["parent_code"] and r["account_type"] == "Header" and c == "8000":
            issues.append(Issue("COA_ORPHAN_ROOT", "info", c, "8000 Income Tax Expense is a root with no parent; rendered as its own P&L section."))
        if r["normal_balance"] == "" and r["account_type"] == "Header":
            issues.append(Issue("COA_NO_NORMAL_BAL", "info", c, f"Header {c} has no normal_balance (harmless, headers never hold balances)."))
    typed_parent = [c for c, r in coa.accounts.items() if r["account_type"] != "Header" and c in coa.children]
    for c in typed_parent:
        issues.append(Issue("COA_TYPED_PARENT", "info", c, f"{c} is typed '{coa[c]['account_type']}' but has children; treated as a header."))
    tagged = sorted(c for c, r in coa.accounts.items() if r["statement"] == "PL" and r["cf_category"] and r["account_type"] != "Header")
    issues.append(Issue("COA_PL_CF_TAG", "info", ",".join(tagged), "P&L accounts carry a cf_category (non-cash add-backs / investing "
                        "gains). Treated as cash-flow-statement hints, irrelevant to P&L and BS."))
    return coa

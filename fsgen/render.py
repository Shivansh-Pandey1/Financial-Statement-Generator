from __future__ import annotations

from decimal import Decimal
from typing import Dict, Optional

from .models import SEVERITY_ORDER, Node


def f(x: Optional[Decimal]) -> str:
    return "" if x is None else f"{x:,.0f}" if x == x.to_integral() else f"{x:,.2f}"


def section(node: Optional[Node], comp: Optional[Dict[str, Decimal]] = None):
    if not node:
        return []
    out = []
    for d, n in node.flatten():
        hdr = bool(n.children)
        prior = (f(comp[n.code]) if n.code in comp else "-") if comp is not None and not hdr else ""
        name = f"{n.code} {n.name}"
        out.append(f"| {'&nbsp;&nbsp;' * d}{'**' + name + '**' if hdr else name} | {f(n.value)} | {prior} |")
    return out


def render_report(r) -> str:
    st = r.statements
    L = ["# Financial statements - 2024-Q4 (USD)", f"**Status: {r.status}**", "", "## Profit & Loss",
         "| Line | Current | |", "|---|---:|---|"]
    L += section(st.revenue) + section(st.cogs) + [f"| **Gross profit** | {f(st.gross_profit)} | |"]
    L += section(st.opex) + [f"| **Operating income** | {f(st.operating_income)} | |"]
    L += section(st.non_operating) + [f"| **Pre-tax income** | {f(st.pre_tax_income)} | |"]
    L += section(st.tax) + [f"| **Net income** | {f(st.net_income)} | |", ""]
    L += ["## Balance Sheet", "| Line | Current | Prior (indicative) |", "|---|---:|---:|"]
    L += section(st.assets, r.comparatives)
    if r.unmapped_net:
        L += [f"| &nbsp;&nbsp;**UNMAPPED / suspense (held, not classified)** | {f(r.unmapped_net)} | |"]
    L += section(st.liabilities, r.comparatives) + section(st.equity, r.comparatives)
    L += [f"| &nbsp;&nbsp;Current-period net income (unclosed) | {f(st.net_income)} | |",
          f"| **Unreconciled TB difference (not plugged)** | {f(-r.residual)} | |", ""]
    L += ["## Statement of Changes in Equity", "| Component | Opening (prior TB) | Net income | Unexplained movement | Closing |",
          "|---|---:|---:|---:|---:|"]
    L += [f"| {x['component']} | {f(x['opening'])} | {f(x['net_income'])} | {f(x['unexplained_movement'])} | {f(x['closing'])} |"
          for x in r.socie]
    L += ["", "_Unexplained = movement not supported by any input (dividends, restatements, OCI, translation). Surfaced, not invented._", ""]
    L += ["## Verification", "| Check | Result | Detail |", "|---|---|---|"]
    for c in r.checks:
        L += [f"| {c.check} | {'PASS' if c.passed else ('FAIL (blocking)' if c.blocking else 'WARN')} | {c.detail} |"]
    L += ["", "## Manual adjustments"]
    for v in r.verdicts:
        L += [f"- **{v.id}** ({v.description}): **{v.status.upper()}**"] + [f"  - {x}" for x in v.reasons]
    L += ["", "## Issues"]
    for i in sorted(r.issues, key=lambda i: SEVERITY_ORDER[i.severity]):
        L += [f"- [{i.severity.upper()}] `{i.code}` {i.subject}: {i.message} -> {i.action}"]
    return "\n".join(L) + "\n"

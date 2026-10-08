"""python -m fsgen.run [--inputs inputs] [--out output] [--trace CODE] [--overrides FILE] [--advisor llm]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .io_utils import dumps
from .models import Config
from .pipeline import Result, load_overrides, run_pipeline
from .render import render_report


def write_outputs(r: Result, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    st = r.statements
    files = {
        "report.md": render_report(r),
        "lineage.json": dumps(dict(r.ledger)),
        "issues.json": dumps(r.issues),
        "verification.json": dumps(r.checks),
        "mapping.json": dumps(r.mapping),
        "adjustments.json": dumps([dict(id=v.id, status=v.status, reasons=v.reasons) for v in r.verdicts]),
        "statements.json": dumps(dict(status=r.status, profit_and_loss=dict(
            revenue=st.revenue, cogs=st.cogs, opex=st.opex, non_operating=st.non_operating, tax=st.tax,
            gross_profit=st.gross_profit, operating_income=st.operating_income, non_operating_net=st.pre_tax_income - st.operating_income,
            pre_tax_income=st.pre_tax_income, net_income=st.net_income),
            balance_sheet=dict(assets=st.assets, liabilities=st.liabilities, equity=st.equity),
            socie=r.socie, net_income=st.net_income)),
        "run_manifest.json": dumps(r.manifest),
    }
    for name, text in files.items():
        (out / name).write_text(text)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inputs", default="inputs")
    ap.add_argument("--out", default="output")
    ap.add_argument("--trace", help="print lineage for an account code from the last run")
    ap.add_argument("--overrides", help="JSON of human-approved mappings (see examples/mapping_overrides.json)")
    ap.add_argument("--advisor", choices=["none", "llm"], default="none", help="llm needs ANTHROPIC_API_KEY; proposals only")
    ap.add_argument("--translation-policy", choices=["oci", "pnl"], default="oci")
    a = ap.parse_args(argv)
    out = Path(a.out)
    if a.trace:
        lin = json.loads((out / "lineage.json").read_text())
        print(json.dumps(lin.get(a.trace, f"no lineage for {a.trace}"), indent=2))
        return 0
    advisor = None
    if a.advisor == "llm":
        from .advisor import LLMAdvisor
        advisor = LLMAdvisor()
    r = run_pipeline(Path(a.inputs), Config(translation_policy=a.translation_policy), load_overrides(a.overrides), advisor)
    write_outputs(r, out)
    print((out / "report.md").read_text().split("## Issues")[0])
    print(f"status: {r.status}\nwrote {out}/ (report.md, statements.json, lineage.json, issues.json, ...)")
    return 1 if r.blocked else 0


if __name__ == "__main__":
    sys.exit(main())

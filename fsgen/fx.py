from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Tuple

from .io_utils import read_csv
from .models import Config, Issue


class RateTable:
    """Rate lookup with a *visible* fallback chain. It never invents a rate silently."""

    def __init__(self, inp: Path, cfg: Config, issues: List[Issue]):
        self.cfg, self.issues = cfg, issues
        self.rates: Dict[Tuple[str, str], Decimal] = {
            (r["currency"], r["rate_type"]): Decimal(r["rate"]) for r in read_csv(inp / "fx_rates.csv")}

    def get(self, ccy: str, wanted: str, subject: str) -> Tuple[Decimal, str]:
        if ccy == self.cfg.functional_ccy:
            return Decimal(1), wanted
        if (ccy, wanted) in self.rates:
            return self.rates[(ccy, wanted)], wanted
        for fb in ("period_average", "opening"):
            if (ccy, fb) in self.rates:
                r = self.rates[(ccy, fb)]
                self.issues.append(Issue(
                    "FX_MISSING_RATE", "high", f"{ccy}/{wanted}",
                    f"No {wanted} rate for {ccy}. Provisionally using {fb} {r} ({subject}).",
                    f"Treasury to supply the {ccy} {wanted} rate and re-run. Every {ccy} balance is provisional until then."))
                return r, f"{fb}(FALLBACK for {wanted})"
        raise ValueError(f"No usable FX rate for {ccy}")

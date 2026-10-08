from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Tuple

from .coa import Coa
from .fx import RateTable
from .mapper import Mapper
from .models import ZERO, Balance, Config, Issue, Source, money


class Ledger(Dict[str, Balance]):
    """account key -> Balance. Keys prefixed UNMAPPED: hold balances no approved mapping exists for."""

    def post(self, key: str, usd: Decimal, nominal: Decimal, src: Source) -> None:
        b = self.setdefault(key, Balance())
        b.net += usd
        b.nominal += nominal
        b.sources.append(src)

    def net_total(self) -> Decimal:
        return sum((b.net for b in self.values()), ZERO)

    def nominal_total(self) -> Decimal:
        return sum((b.nominal for b in self.values()), ZERO)

    def unmapped_net(self) -> Decimal:
        return sum((b.net for k, b in self.items() if k.startswith("UNMAPPED")), ZERO)


def build_ledger(rows: List[dict], file: str, coa: Coa, rates: RateTable, mapper: Mapper, cfg: Config,
                 rate_type: str, label: str, issues: List[Issue]) -> Tuple[Ledger, List[dict]]:
    ledger, seen, log = Ledger(), {}, []
    for r in rows:
        code, name, ccy = r["account_code"], r["account_name"], r["currency"]
        dr, cr = money(r["debit"]), money(r["credit"])
        m = mapper.map(code, name)
        log.append(dict(src=r["_ref"], code=code, name=name, target=m.target, confidence=m.confidence, method=m.method,
                        **({"reason": m.reason} if m.reason else {})))
        if m.auto:
            key = m.target
        else:
            key = f"UNMAPPED:{code}"
            guess = coa[m.target]["account_name"] if m.target else "-"
            issues.append(Issue(
                "MAP_ESCALATE", "high", f"{label}:{code}",
                f"{r['_ref']}: '{code} {name}' not auto-mappable (best guess {m.target} '{guess}', confidence {m.confidence}, "
                f"{m.method}). Balance Dr {dr} / Cr {cr}." + (f" Advisor: {m.reason}" if m.reason else ""),
                "Held in an explicit 'Unmapped' bucket on the statements until a human approves a target.", str(dr - cr)))
        prev = [p for p in seen.get(key, []) if p["ccy"] == ccy]
        if prev and not key.startswith("UNMAPPED"):
            same = [p for p in prev if p["dr"] == dr and p["cr"] == cr]
            issues.append(Issue(
                "TB_DUP_CODE", "high" if label == "current" else "warn", f"{label}:{code}",
                f"Account {code} appears more than once in {file} ({', '.join(p['ref'] for p in prev)} and {r['_ref']}) "
                f"with {'identical' if same else 'different'} amounts in {ccy}.",
                "Rows summed (an exact repeat would instead be quarantined). Confirm it is not a double-posted export line.",
                str(dr - cr)))
        seen.setdefault(key, []).append(dict(ref=r["_ref"], dr=dr, cr=cr, ccy=ccy))
        stmt = coa[code]["statement"] if code in coa else "BS"
        rate, rtype = rates.get(ccy, rate_type if stmt == "BS" else "period_average", r["_ref"])
        nominal = dr - cr
        usd = (nominal * rate).quantize(Decimal("0.01"))
        ledger.post(key, usd, nominal, Source("tb", r["_ref"], usd, ccy, nominal, rate, rtype))
    return ledger, log


def post_translation_difference(ledger: Ledger, cfg: Config, issues: List[Issue]) -> Decimal:
    """Foreign-currency rows translate to a different USD figure than their nominal amount. The difference is posted
    to an explicit, flagged system line (never silently absorbed) so the ledger stays internally consistent."""
    diff = ledger.net_total() - ledger.nominal_total()
    if not diff:
        return diff
    acct = "3310" if cfg.translation_policy == "oci" else "7310"
    b = ledger.setdefault(acct, Balance())
    b.net -= diff
    b.sources.append(Source("system", "SYS-CTA", -diff, memo=(
        f"Provisional translation difference on foreign-currency cash -> {acct} "
        "(assumes TB foreign rows are in local currency; policy to be confirmed)")))
    where = ("3310 FX Translation Reserve. For a USD-functional entity ASC 830 remeasurement would normally hit P&L (7310) "
             "instead. Controller to decide." if acct == "3310" else
             "7310 unrealized FX (P&L remeasurement policy). Controller to confirm.")
    issues.append(Issue("CTA_PROVISIONAL", "warn", acct, f"Translating EUR/GBP cash at closing rates created {diff:,.2f} of FX "
                        f"difference, posted provisionally to {where}", amount=str(diff)))
    return diff

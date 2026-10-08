"""Account -> COA mapping gate.

Policy: only an exact code match (with agreeing name) or an explicit human override is ever auto-accepted.
Fuzzy and LLM output are *suggestions* capped below the auto-accept threshold, so they can only escalate.
"""
from __future__ import annotations

import difflib
from typing import Dict, Optional, Protocol, Tuple

from .coa import Coa
from .models import Config, Mapping


class Advisor(Protocol):
    def suggest(self, code: str, name: str, coa: Coa) -> Optional[Tuple[str, float, str]]:
        """Return (coa_code, confidence, reason) or None."""


def propose_mapping(code: str, name: str, coa: Coa) -> Tuple[Optional[str], float, str]:
    """Deterministic proposal (unchanged reference algorithm)."""
    if coa.is_postable(code):
        sim = difflib.SequenceMatcher(None, name.lower(), coa[code]["account_name"].lower()).ratio()
        return code, (1.0 if sim > 0.6 else 0.7), "exact_code" if sim > 0.6 else "exact_code_name_mismatch"
    best: Tuple[Optional[str], float] = (None, 0.0)
    for c in coa.postable():
        sim = difflib.SequenceMatcher(None, name.lower(), coa[c]["account_name"].lower()).ratio()
        sibling = 1.0 if c[:2] == code[:2] else (0.5 if c[:1] == code[:1] else 0.0)
        conf = round(0.6 * sim + 0.4 * sibling * (sim > 0.3), 2)  # sibling range only counts if names are plausibly related
        if conf > best[1]:
            best = (c, conf)
    return best[0], best[1], "fuzzy_name_and_range"


class Mapper:
    def __init__(self, coa: Coa, cfg: Config, overrides: Optional[Dict[str, dict]] = None, advisor: Optional[Advisor] = None):
        self.coa, self.cfg = coa, cfg
        self.overrides = overrides or {}
        self.advisor = advisor

    def map(self, code: str, name: str) -> Mapping:
        ov = self.overrides.get(code)
        if ov and self.coa.is_postable(ov["target"]):
            who = ov.get("approved_by", "unknown")
            return Mapping(code, name, ov["target"], 1.0, "human_override",
                           f"Approved by {who}: {ov.get('reason', '')}".strip(), auto=True)
        target, conf, how = propose_mapping(code, name, self.coa)
        m = Mapping(code, name, target, conf, how, auto=conf >= self.cfg.auto_map_threshold and target == code)
        if not m.auto and self.advisor:
            m = self._consult(m)
        return m

    def _consult(self, m: Mapping) -> Mapping:
        """Accept an advisor suggestion only if it names a real postable account; cap its confidence below the bar."""
        try:
            s = self.advisor.suggest(m.source_code, m.source_name, self.coa)
        except Exception:  # advisor outage must never break the pipeline
            return m
        if not s:
            return m
        target, conf, reason = s
        if not self.coa.is_postable(target):  # hallucinated account -> discard
            return m
        conf = min(float(conf), self.cfg.auto_map_threshold - 0.01)
        if m.target is None or conf > m.confidence:
            return Mapping(m.source_code, m.source_name, target, round(conf, 2), "llm_suggestion", reason, auto=False)
        return m

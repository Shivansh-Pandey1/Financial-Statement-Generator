"""Shared value types. Money is always Decimal; nothing here does I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

ZERO = Decimal(0)
SEVERITY_ORDER = {"blocker": 0, "high": 1, "warn": 2, "info": 3}


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class Config:
    """Policy knobs. Defaults reproduce the reference run; nothing is hidden in code."""
    period: str = "2024-Q4"
    functional_ccy: str = "USD"
    auto_map_threshold: float = 0.90
    tolerance: Decimal = Decimal("0.01")
    # Where the foreign-cash translation difference is parked: "oci" -> 3310, "pnl" -> 7310 (ASC 830 remeasurement)
    translation_policy: str = "oci"


@dataclass
class Issue:
    code: str
    severity: str  # blocker | high | warn | info
    subject: str
    message: str
    action: str = ""
    amount: str = ""


@dataclass
class Source:
    """One lineage atom: a TB row, an adjustment line or a system entry."""
    kind: str  # tb | adj | system
    ref: str
    usd_net: Decimal
    ccy: str = ""
    local_net: Optional[Decimal] = None
    rate: Optional[Decimal] = None
    rate_type: str = ""
    memo: str = ""


@dataclass
class Balance:
    net: Decimal = ZERO       # Dr - Cr, functional currency
    nominal: Decimal = ZERO   # Dr - Cr, untranslated (used for the TB-balance check)
    sources: List[Source] = field(default_factory=list)


@dataclass
class Node:
    code: str
    name: str
    value: Decimal
    children: List["Node"] = field(default_factory=list)

    def flatten(self, depth: int = 0):
        yield depth, self
        for c in self.children:
            yield from c.flatten(depth + 1)


@dataclass
class Mapping:
    source_code: str
    source_name: str
    target: Optional[str]
    confidence: float
    method: str
    reason: str = ""
    auto: bool = False

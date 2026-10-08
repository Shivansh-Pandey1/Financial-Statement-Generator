from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Dict, List


def read_csv(path: Path) -> List[Dict[str, str]]:
    """Rows plus a `_ref` (file:line) so every number can point back to its source line."""
    with open(path, newline="") as f:
        return [dict(r, _ref=f"{Path(path).name}:L{i}") for i, r in enumerate(csv.DictReader(f), start=2)]


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def _default(o):
    if isinstance(o, Decimal):
        return str(o)
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    raise TypeError(type(o))


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, default=_default)

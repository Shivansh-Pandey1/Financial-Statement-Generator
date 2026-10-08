"""Optional LLM mapping advisor. Off by default (offline runs are fully deterministic).

The model only *proposes*; mapper.Mapper validates the code against the real COA and caps confidence below the
auto-accept threshold, so a hallucination can at worst produce a wrong suggestion in a human review queue.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional, Tuple

from .coa import Coa

PROMPT = """You map ERP accounts to a chart of accounts (COA). Reply with ONLY a JSON object:
{{"code": "<COA code from the list>", "confidence": <0..1>, "reason": "<one sentence>"}}
or {{"code": null, "confidence": 0, "reason": "..."}} if nothing fits. Never invent a code.

Account to map: code={code} name={name!r}

Candidate COA accounts:
{candidates}
"""


class LLMAdvisor:
    def __init__(self, client=None, model: Optional[str] = None):
        if client is None:
            import anthropic  # lazy: only needed when this advisor is enabled
            client = anthropic.Anthropic()
        self.client = client
        self.model = model or os.environ.get("FSGEN_MODEL", "claude-sonnet-5-5")

    def suggest(self, code: str, name: str, coa: Coa) -> Optional[Tuple[str, float, str]]:
        cands = "\n".join(f"{c} | {coa[c]['account_name']} | {coa[c]['account_type']}" for c in coa.postable())
        msg = self.client.messages.create(
            model=self.model, max_tokens=300,
            messages=[{"role": "user", "content": PROMPT.format(code=code, name=name, candidates=cands)}])
        text = "".join(getattr(b, "text", "") for b in msg.content)
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        if not data.get("code"):
            return None
        return str(data["code"]), float(data.get("confidence", 0)), str(data.get("reason", ""))

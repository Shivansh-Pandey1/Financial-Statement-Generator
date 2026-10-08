"""Run: python -m unittest discover tests   (stdlib only; no network, no API key)"""
import csv
import json
import shutil
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from fsgen.advisor import LLMAdvisor
from fsgen.cli import main, write_outputs
from fsgen.models import Config
from fsgen.pipeline import load_overrides, run_pipeline

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "inputs"


def copy_inputs() -> Path:
    d = Path(tempfile.mkdtemp())
    for f in INPUTS.iterdir():
        shutil.copy(f, d)
    return d


class FakeClient:
    """Stands in for the Anthropic client."""
    def __init__(self, text):
        self.messages = self
        self.text = text

    def create(self, **_):
        return type("M", (), {"content": [type("B", (), {"text": self.text})()]})()


class Reference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = run_pipeline(INPUTS)
        cls.issue_codes = {i.code for i in cls.r.issues}
        cls.verdict = {v.id: v.status for v in cls.r.verdicts}

    def test_blocked_not_plugged(self):
        self.assertTrue(self.r.blocked)
        self.assertIn("BLOCKED", self.r.status)

    def test_reference_numbers(self):
        self.assertEqual(self.r.statements.net_income, D("4783500.00"))
        self.assertEqual(self.r.statements.assets.value, D("36223560.20"))
        self.assertEqual(self.r.residual, D("-4800.00"))
        self.assertEqual(self.r.unmapped_net, D("12400.00"))

    def test_adjustment_triage(self):
        self.assertEqual(self.verdict["JE-002"], "rejected")
        self.assertEqual(self.verdict["JE-005"], "quarantined")
        self.assertEqual(self.verdict["JE-008"], "rejected")
        self.assertEqual(self.verdict["JE-003"], "quarantined")
        for j in ("JE-001", "JE-004", "JE-006", "JE-007", "JE-009", "JE-010"):
            self.assertEqual(self.verdict[j], "accepted", j)

    def test_seeded_defects_found(self):
        self.assertTrue({"TB_DUP_CODE", "MAP_ESCALATE", "FX_MISSING_RATE", "PRIOR_UNBALANCED", "COA_EMPTY_HEADER",
                         "COA_AMBIGUOUS_CF", "PRIOR_RENAMED", "COA_TYPED_PARENT"} <= self.issue_codes)
        # same account in different currencies is legitimate, must not be flagged
        self.assertFalse([i for i in self.r.issues if i.code == "TB_DUP_CODE" and i.subject == "current:1110"])

    def test_lineage_ties_out(self):
        for code, b in self.r.ledger.items():
            self.assertEqual(sum(s.usd_net for s in b.sources), b.net, code)

    def test_every_check_ran(self):
        self.assertEqual([c.check[:3].strip() for c in self.r.checks], ["V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9", "V10"])
        v2 = next(c for c in self.r.checks if c.check.startswith("V2"))
        self.assertTrue(v2.passed, v2.detail)  # independent recompute agrees with the statement build

    def test_socie_surfaces_unexplained(self):
        re_row = next(x for x in self.r.socie if x["component"].startswith("3200"))
        self.assertEqual(re_row["unexplained_movement"], D("2060000.00"))
        total = self.r.socie[-1]
        self.assertEqual(total["closing"], self.r.statements.equity.value + self.r.statements.net_income)

    def test_inputs_never_modified(self):
        before = {f.name: f.read_bytes() for f in INPUTS.iterdir()}
        run_pipeline(INPUTS)
        self.assertEqual(before, {f.name: f.read_bytes() for f in INPUTS.iterdir()})

    def test_idempotent_outputs(self):
        a, b = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        write_outputs(run_pipeline(INPUTS), a)
        write_outputs(run_pipeline(INPUTS), b)
        for f in a.iterdir():
            self.assertEqual(f.read_text(), (b / f.name).read_text(), f.name)


class HumanInTheLoop(unittest.TestCase):
    def test_default_run_has_no_overrides(self):
        self.assertEqual(run_pipeline(INPUTS).manifest["human_overrides"], [])

    def test_override_resolves_suspense_and_is_audited(self):
        r = run_pipeline(INPUTS, overrides=load_overrides(ROOT / "examples/mapping_overrides.json"))
        self.assertEqual(r.unmapped_net, 0)
        self.assertEqual(r.manifest["human_overrides"], ["9999"])
        m = next(x for x in r.mapping["current"] if x["code"] == "9999")
        self.assertEqual((m["target"], m["method"]), ("6900", "human_override"))
        self.assertIn("controller@example.com", m["reason"])
        self.assertEqual(r.ledger["6900"].net, D("152400.00"))  # 140,000 + 12,400, lineage shows both rows
        self.assertTrue(next(c for c in r.checks if c.check.startswith("V6")).passed)

    def test_override_to_non_postable_account_is_ignored(self):
        r = run_pipeline(INPUTS, overrides={"9999": {"target": "1100"}})  # 1100 is a header
        self.assertEqual(r.unmapped_net, D("12400.00"))


class Advisor(unittest.TestCase):
    def test_hallucinated_code_is_discarded(self):
        adv = LLMAdvisor(FakeClient('{"code": "9998", "confidence": 0.99, "reason": "made up"}'))
        r = run_pipeline(INPUTS, advisor=adv)
        m = next(x for x in r.mapping["current"] if x["code"] == "9999")
        self.assertNotEqual(m["method"], "llm_suggestion")
        self.assertEqual(r.unmapped_net, D("12400.00"))

    def test_valid_suggestion_never_auto_accepted(self):
        adv = LLMAdvisor(FakeClient('Sure: {"code": "6900", "confidence": 0.99, "reason": "looks like misc opex"}'))
        r = run_pipeline(INPUTS, advisor=adv)
        m = next(x for x in r.mapping["current"] if x["code"] == "9999")
        self.assertEqual((m["target"], m["method"]), ("6900", "llm_suggestion"))
        self.assertLess(m["confidence"], Config().auto_map_threshold)  # capped: a human must still approve
        self.assertEqual(r.unmapped_net, D("12400.00"))  # still escalated

    def test_advisor_outage_does_not_break_pipeline(self):
        class Down:
            messages = type("M", (), {"create": staticmethod(lambda **_: (_ for _ in ()).throw(RuntimeError("503")))})()
        r = run_pipeline(INPUTS, advisor=LLMAdvisor(Down()))
        self.assertTrue(r.blocked)

    def test_advisor_cannot_touch_exact_matches(self):
        adv = LLMAdvisor(FakeClient('{"code": "6900", "confidence": 0.99, "reason": "x"}'))
        base, withadv = run_pipeline(INPUTS), run_pipeline(INPUTS, advisor=adv)
        self.assertEqual(base.statements.net_income, withadv.statements.net_income)
        self.assertEqual(base.statements.assets.value, withadv.statements.assets.value)


class Policy(unittest.TestCase):
    def test_pnl_translation_policy_moves_difference_to_income(self):
        oci, pnl = run_pipeline(INPUTS), run_pipeline(INPUTS, Config(translation_policy="pnl"))
        diff = oci.ledger["3310"].net - pnl.ledger["3310"].net  # credit moved out of 3310 (more negative net)
        self.assertEqual(pnl.statements.net_income - oci.statements.net_income, -diff)
        self.assertEqual(pnl.statements.assets.value, oci.statements.assets.value)


class CleanData(unittest.TestCase):
    def test_fixed_data_comes_out_ready(self):
        """Fix the data defects and the same code must pass - the verifier must not cry wolf."""
        d = copy_inputs()
        rows = [r for r in csv.DictReader(open(d / "trial_balance.csv"))
                if r["account_code"] != "9999" and r["currency"] == "USD" and not (r["account_code"] == "6310" and r["debit"] == "38500.00")]
        net = sum(D(r["debit"]) - D(r["credit"]) for r in rows)
        for r in rows:
            if r["account_code"] == "3200":
                r["credit"] = str(D(r["credit"]) + net)
        with open(d / "trial_balance.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["account_code", "account_name", "currency", "debit", "credit"])
            w.writeheader()
            w.writerows(rows)
        r = run_pipeline(d)
        self.assertFalse(r.blocked, [c for c in r.checks if c.blocking])

    def test_verifier_catches_a_broken_statement_builder(self):
        """Sabotage the tree build (+1,000 on assets). The independent V2 recompute must notice."""
        from unittest import mock
        from fsgen import pipeline
        real = pipeline.generate

        def sabotaged(coa, ledger):
            st = real(coa, ledger)
            st.assets.value += D("1000")
            return st

        with mock.patch.object(pipeline, "generate", sabotaged):
            r = run_pipeline(INPUTS)
        v2 = next(c for c in r.checks if c.check.startswith("V2"))
        self.assertFalse(v2.passed)
        self.assertTrue(v2.blocking)


class Cli(unittest.TestCase):
    def test_exit_code_and_trace(self):
        out = tempfile.mkdtemp()
        self.assertEqual(main(["--inputs", str(INPUTS), "--out", out]), 1)
        self.assertEqual(main(["--inputs", str(INPUTS), "--out", out, "--trace", "1110"]), 0)
        self.assertEqual(json.loads((Path(out) / "run_manifest.json").read_text())["advisor"], "none")


if __name__ == "__main__":
    unittest.main()

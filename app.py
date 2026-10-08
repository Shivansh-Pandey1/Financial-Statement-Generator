"""Streamlit UI: streamlit run app.py"""
import json
import shutil
import tempfile
from decimal import Decimal as D
from pathlib import Path

import pandas as pd
import streamlit as st

from fsgen.cli import write_outputs
from fsgen.models import Config
from fsgen.pipeline import load_overrides, run_pipeline

ROOT = Path(__file__).parent
FILES = ["trial_balance.csv", "prior_period_tb.csv", "chart_of_accounts.csv", "fx_rates.csv", "manual_adjustments.json"]

st.set_page_config(page_title="Financial Statement Generator", layout="wide")
st.title("Financial Statement Generator")
st.caption("TB + COA + adjustments → P&L and Balance Sheet, with verification and lineage. Deterministic core; nothing is plugged.")

# ---- inputs: bundled data by default, optional upload per file
with st.sidebar:
    st.header("Inputs")
    st.write("Using `inputs/` unless you upload a replacement.")
    uploads = {f: st.file_uploader(f, key=f) for f in FILES}


@st.cache_data(show_spinner=False)
def execute(upload_bytes: tuple):
    work = Path(tempfile.mkdtemp())
    inp, out = work / "inputs", work / "output"
    shutil.copytree(ROOT / "inputs", inp)
    for name, data in upload_bytes:
        (inp / name).write_bytes(data)
    r = run_pipeline(inp, Config(), load_overrides(None))
    write_outputs(r, out)
    rd = lambda f: json.loads((out / f).read_text())
    return dict(rc=int(r.blocked), report=(out / "report.md").read_text(), statements=rd("statements.json"), issues=rd("issues.json"),
                checks=rd("verification.json"), adj=rd("adjustments.json"), lineage=rd("lineage.json"), mapping=rd("mapping.json"),
                manifest=rd("run_manifest.json"))


res = execute(tuple((n, u.getvalue()) for n, u in uploads.items() if u))
status = res["statements"]["status"]
(st.error if res["rc"] else st.success)(f"**{status}**")

num = lambda x: float(D(x))


def tree_df(node):
    return pd.DataFrame([dict(Code=c, Account=("   " * d) + n, Amount=num(v), Header=h)
                         for d, c, n, v, h in _flat(node)]) if node else pd.DataFrame()


def _flat(node, d=0):
    out = [(d, node["code"], node["name"], node["value"], bool(node["children"]))]
    for c in node["children"]:
        out += _flat(c, d + 1)
    return out


pl, bs = res["statements"]["profit_and_loss"], res["statements"]["balance_sheet"]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Net income", f"{num(res['statements']['net_income']):,.0f}")
c2.metric("Total assets", f"{num(bs['assets']['value']):,.0f}")
c3.metric("Blocking checks failed", sum(1 for c in res["checks"] if c["blocking"]))
c4.metric("Issues (blocker/high)", sum(1 for i in res["issues"] if i["severity"] in ("blocker", "high")))

t_pl, t_bs, t_eq, t_ver, t_adj, t_iss, t_trace, t_map, t_dl = st.tabs(
    ["P&L", "Balance Sheet", "Equity (SOCIE)", "Verification", "Adjustments", "Issues", "Trace a number", "Mapping", "Downloads"])

with t_pl:
    for k in ("revenue", "cogs", "opex", "non_operating", "tax"):
        st.dataframe(tree_df(pl[k]), hide_index=True, use_container_width=True)
    a, b, c = st.columns(3)
    a.metric("Gross profit", f"{num(pl['gross_profit']):,.0f}")
    b.metric("Operating income", f"{num(pl['operating_income']):,.0f}")
    c.metric("Net income", f"{num(pl['net_income']):,.0f}")

with t_bs:
    for k in ("assets", "liabilities", "equity"):
        st.dataframe(tree_df(bs[k]), hide_index=True, use_container_width=True)
    st.info("Unmapped/suspense balances and the unreconciled TB difference are shown in the report, not plugged. See Verification.")

with t_eq:
    sd = pd.DataFrame(res["statements"]["socie"])
    for c in sd.columns[1:]:
        sd[c] = sd[c].astype(float)
    st.dataframe(sd, hide_index=True, use_container_width=True)
    st.caption("Unexplained = movement no input supports (dividends, restatements, OCI). Surfaced, not invented.")

with t_ver:
    df = pd.DataFrame(res["checks"])
    df["result"] = df.apply(lambda r: "PASS" if r.passed else ("FAIL (blocking)" if r.blocking else "WARN"), axis=1)
    st.dataframe(df[["check", "result", "detail"]], hide_index=True, use_container_width=True)

with t_adj:
    full = {v["id"]: v for v in json.loads((ROOT / "inputs/manual_adjustments.json").read_text())["entries"]}
    for a in res["adj"]:
        icon = {"accepted": "✅", "rejected": "❌", "quarantined": "⚠️"}[a["status"]]
        with st.expander(f"{icon} {a['id']} — {a['status'].upper()}"):
            for r in a["reasons"]:
                st.write(r)
            if a["id"] in full:
                st.dataframe(pd.DataFrame(full[a["id"]]["lines"]), hide_index=True)

with t_iss:
    sev = st.multiselect("Severity", ["blocker", "high", "warn", "info"], default=["blocker", "high", "warn"])
    st.dataframe(pd.DataFrame(res["issues"]).query("severity in @sev"), hide_index=True, use_container_width=True)

with t_trace:
    code = st.selectbox("Account", sorted(res["lineage"]))
    e = res["lineage"][code]
    st.metric("USD balance (Dr − Cr)", f"{num(e['net']):,.2f}")
    st.dataframe(pd.DataFrame(e["sources"]), hide_index=True, use_container_width=True)

with t_map:
    st.dataframe(pd.DataFrame(res["mapping"]["current"]).sort_values("confidence"), hide_index=True, use_container_width=True)
    st.caption("Prior-period mapping")
    st.dataframe(pd.DataFrame(res["mapping"]["prior"]).sort_values("confidence").head(5), hide_index=True)

with t_dl:
    st.download_button("report.md", res["report"], "report.md")
    st.download_button("statements.json", json.dumps(res["statements"], indent=2), "statements.json")
    st.download_button("issues.json", json.dumps(res["issues"], indent=2), "issues.json")
    st.json(res["manifest"])

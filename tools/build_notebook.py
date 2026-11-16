"""Build notebooks/walkthrough.ipynb from committed results (no Azure needed), then execute it in place.

    .venv/bin/python tools/build_notebook.py
"""
import subprocess
import sys
from pathlib import Path

import nbformat as nbf

ROOT = Path(__file__).resolve().parents[1]
md, code = nbf.v4.new_markdown_cell, nbf.v4.new_code_cell

cells = [
    md("""# failed-bank-docai: walkthrough

When a bank fails, someone has to turn its loan files into numbers people can trust, fast. This notebook walks
through what this project measured, using only files committed to the repo (plus public FDIC data), so it runs
without any cloud account.

*Independent personal research project. Loan files are synthetic; the FDIC data and the agreements are public.*"""),
    code("""import json, sys, collections
from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt

ROOT = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
sys.path.insert(0, str(ROOT / "src"))
SYN = ROOT / "samples" / "synthetic"
pd.set_option("display.max_colwidth", 80)"""),
    md("""## 1. Context: bank failures since 1934 (public FDIC data)

The 2008–2012 wave (mortgage bubble) and 2023 (Silicon Valley Bank, Signature, First Republic). The data is
downloaded on first run if it isn't cached."""),
    code("""import fdic_sources as fs
path = fs.PUBLIC / "failures.json"
if not path.exists():
    fs.fetch_failures(fs._session())
fails = pd.DataFrame(json.loads(path.read_text()))
fails = fails[fails["RESTYPE"] == "FAILURE"]
by_year = fails.groupby(fails["FAILYR"].astype(int)).size()
ax = by_year.plot(kind="bar", figsize=(13, 3.5), color=["#c0392b" if 2008 <= y <= 2012 or y == 2023 else "#7f8c8d" for y in by_year.index])
ax.set_xticks(range(0, len(by_year), 5)); ax.set_xticklabels(by_year.index[::5])
ax.set_title("U.S. bank failures per year (red: 2008-2012 and 2023)"); ax.set_xlabel(""); plt.tight_layout(); plt.show()
print("2008-2012:", by_year.loc[2008:2012].sum(), "| 1993-2007:", by_year.loc[1993:2007].sum())"""),
    md("""## 2. The synthetic loan files

Every fictional bank copies a real failed bank's loan mix and has its own layout, wording, formats and note style.
Validation, test and holdout banks use styles never seen in training."""),
    code("""idx = pd.read_csv(SYN / "index.csv")
summary = idx.groupby("split").agg(banks=("bank_id", "nunique"), loan_files=("loan_id", "count"), pages=("pages", "sum"))
summary.loc[["train", "train_aug", "train_plain", "train_hard", "val", "test", "holdout", "holdout2"]]"""),
    code("""label = json.loads((SYN / "labels" / "B5-0002.json").read_text())
print("documents:", [(d["doc_type"], d["page_from"], d["scanned"]) for d in label["documents"]])
print("fields:", {k: label["fields"][k] for k in ["original_principal", "note_rate_pct", "maturity_date", "current_upb"]})
print("planted flags:", label["flags"][:2])"""),
    md("""## 3. U1: extraction on banks the model never saw

`correct` = matches the planted answer; `wrong` = a value was returned but it's wrong (dangerous);
`missing` = nothing returned (safe: a person fills it in)."""),
    code("""def summary(method):
    df = pd.read_csv(SYN / "eval" / f"{method}.csv")
    g = df.groupby("split")["outcome"].value_counts().unstack(fill_value=0)
    g["recall"] = g["correct"] / g.sum(axis=1)
    g["method"] = method
    return g.reset_index()

table = pd.concat([summary(m) for m in ["rules", "fbd-neural-v1", "fbd-neural-v2", "fbd-neural-v3"]])
table = table[table["split"].isin(["val", "test", "holdout", "holdout2"])]
pivot = table.pivot(index="method", columns="split", values="recall").reindex(columns=["val", "test", "holdout", "holdout2"])
pivot.style.format("{:.1%}", na_rep="—")"""),
    code("""ax = pivot.T.plot(kind="bar", figsize=(10, 3.5), rot=0)
ax.set_ylabel("share of values read correctly"); ax.set_ylim(0, 1.05); ax.set_title("Rules vs custom models, by held-out bank")
plt.legend(loc="lower right"); plt.tight_layout(); plt.show()"""),
    md("""**Routing:** a value is auto-accepted only if the model is confident, it passes the validity and plausibility
rules, and it agrees with the bank's data-room schedule. Everything else goes to a person. The number that matters
is the *silent error*: wrong and auto-accepted."""),
    code("""import route
rows = route.load("fbd-neural-v3")
val = [r for r in rows if r["split"] == "val"]
cut = route.choose_cutoff(val, 0.02, True)
out = []
for split in ["val", "test", "holdout", "holdout2"]:
    m = route.apply([r for r in rows if r["split"] == split], cut, True)
    out.append({"bank set": split, "values": m["n"], "sent to a person": f"{m['review_rate']:.1%}", "silent errors": m["silent"]})
pd.DataFrame(out)"""),
    md("""## 4. U2: risk flags from notes, letters and credit memos

Precise methods are never wrong but miss half the flags at a new bank; the LLM finds every flag but raises false
alarms. The hybrid accepts what both agree on and sends the rest to a person."""),
    code("""tf = json.loads((SYN / "eval" / "text_flags.json").read_text())
rows = []
for split in ["val", "test"]:
    for m in ["keywords", "tfidf", "llm"]:
        a = tf[split][m]["ALL"]
        rows.append({"split": split, "method": m, "precision": a["precision"], "recall": a["recall"]})
    h = tf[split]["hybrid"]
    rows.append({"split": split, "method": "hybrid (auto part)", "precision": h["auto_precision"], "recall": h["recall_after_review"]})
pd.DataFrame(rows).style.format({"precision": "{:.0%}", "recall": "{:.0%}"})"""),
    md("""## 5. U3: terms from 22 real purchase-and-assumption agreements

Checked against the FDIC's public bid summaries. Only ~3% of the agreements' pages were read."""),
    code("""pa = pd.read_csv(SYN / "eval" / "pa_terms.csv")
pa = pa[pa["outcome"] != "no_truth"]
res = pa.groupby(["field", "method"])["outcome"].value_counts().unstack(fill_value=0)
res["correct / total"] = res["correct"].astype(str) + " / " + res.sum(axis=1).astype(str)
res[["correct / total", "wrong"]].unstack("method")"""),
    md("""## Takeaways

1. Rules written from familiar banks find nothing at a new bank; a model trained on varied banks reads 96–99.7%.
2. How you augment matters: random omissions and decoy numbers made the model worse.
3. Every safety check was found by a held-out bank breaking something, so new banks start at full review.
4. With confidence + rules + reconciliation, every bank tested ended with zero silent errors.
5. On real agreements, the LLM with verbatim-quote checks made no wrong readings; its misses were blanks."""),
]

nb = nbf.v4.new_notebook()
nb["cells"] = cells
nb["metadata"]["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
out = ROOT / "notebooks" / "walkthrough.ipynb"
out.parent.mkdir(exist_ok=True)
nbf.write(nb, out)
subprocess.run([str(Path(sys.executable).parent / "jupyter"), "nbconvert", "--to", "notebook", "--execute", "--inplace",
                "--ExecutePreprocessor.timeout=600", str(out)], check=True, cwd=out.parent)
print("executed", out)

"""Data mining on REAL failures: which balance sheets preceded the costliest bank failures of 2008-2012?

Data (public, FDIC): every failure 2008-2012 (kb.failure: assets, estimated cost to the Deposit Insurance Fund) joined
to the bank's LAST Call Report filed before it failed (BankFind financials API): loan mix, brokered deposits, equity,
noncurrent loans. Target: loss rate = estimated cost / total assets at failure.

Methods (textbook references in docs/MINING.md)
  1. Association rules (Han & Kamber ch. 5; Hastie, Tibshirani & Friedman §14.2): items are "high X" flags (top
     quartile of each ratio among failed banks); consequent = "costly failure" (top-third loss rate). Rules with up to
     two antecedents, scored by support, confidence and lift. Because many rules are tried, each lift is compared with
     a permutation baseline (loss rates shuffled 200 times): only rules beating the 95th percentile of the best
     shuffled lift are reported as findings.
  2. Clustering (James et al., ISLR ch. 10): k-means on standardized loan-mix shares, k by silhouette; each cluster
     profiled by its mix, size, count and median loss rate.
Caveat: these are failed banks only (no survivors), so findings describe which failures cost the most, not which
banks fail. Cost is the FDIC's estimate, revised over time.

    .venv/bin/python src/failure_mining.py fetch     # last pre-failure Call Report per 2008-2012 failure (~465 calls)
    .venv/bin/python src/failure_mining.py run       # features, rules, clusters → SQL (kb.mining_*) + docs/mining.json
"""
from __future__ import annotations

import itertools
import json
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, connect_sql  # noqa: E402
from fdic_sources import FDIC_API, PUBLIC, USER_AGENT  # noqa: E402

OUT = PUBLIC / "failure_profiles"
FIELDS = ["CERT", "REPDTE", "ASSET", "DEP", "BRO", "EQ", "NCLNLS", "LNLSNET", "LNRECONS", "LNRENRES", "LNRERES",
          "LNREMULT", "LNREAG", "LNCI", "LNCON"]
RATIOS = {   # feature: (numerator, denominator, label)
    "construction_share": ("LNRECONS", "LNLSNET", "construction & land development loans"),
    "cre_share": ("LNRENRES", "LNLSNET", "commercial real estate (nonresidential) loans"),
    "residential_share": ("LNRERES", "LNLSNET", "1-4 family residential loans"),
    "multifamily_share": ("LNREMULT", "LNLSNET", "multifamily loans"),
    "farm_share": ("LNREAG", "LNLSNET", "farmland loans"),
    "ci_share": ("LNCI", "LNLSNET", "commercial & industrial loans"),
    "consumer_share": ("LNCON", "LNLSNET", "consumer loans"),
    "brokered_share": ("BRO", "DEP", "brokered deposits (share of deposits)"),
    "equity_ratio": ("EQ", "ASSET", "equity capital / assets"),
    "noncurrent_ratio": ("NCLNLS", "LNLSNET", "noncurrent loans / loans"),
    "loan_to_asset": ("LNLSNET", "ASSET", "loans / assets"),
}
LOW_IS_RISK = {"equity_ratio"}          # the item for equity is "low equity" (bottom quartile)
MIX = ["construction_share", "cre_share", "residential_share", "multifamily_share", "farm_share", "ci_share",
       "consumer_share"]


# ---------------------------------------------------------------- fetch
def fetch() -> None:
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect_sql()
    cur = conn.cursor()
    cur.execute("SELECT cert, fail_date FROM kb.failure WHERE fail_year BETWEEN 2008 AND 2012 AND cert IS NOT NULL")
    todo = [(c, d) for c, d in cur.fetchall() if not (OUT / f"{c}.json").exists()]
    print(f"{len(todo)} to fetch")
    for i, (cert, fail_date) in enumerate(todo, 1):
        day = str(fail_date).replace("-", "")[:8]
        for attempt in range(6):   # the public API rate-limits bursts (429): back off, honour Retry-After
            r = s.get(f"{FDIC_API}/financials", timeout=60, params={
                "filters": f"CERT:{cert} AND REPDTE:[* TO {day}]", "fields": ",".join(FIELDS),
                "sort_by": "REPDTE", "sort_order": "DESC", "limit": 1})
            if r.status_code != 429:
                break
            time.sleep(int(r.headers.get("Retry-After") or 0) or 15 * (attempt + 1))
        r.raise_for_status()
        data = r.json().get("data") or []
        (OUT / f"{cert}.json").write_text(json.dumps(data[0]["data"] if data else {}))
        if i % 50 == 0:
            print(f"  {i}/{len(todo)}")
        time.sleep(1.0)   # polite pacing for a public API


# ---------------------------------------------------------------- features
def profiles(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute("""SELECT cert, bank, state, fail_date, fail_year, assets_k, cost_k, resolution
                   FROM kb.failure WHERE fail_year BETWEEN 2008 AND 2012 AND cert IS NOT NULL""")
    rows = []
    for cert, bank, state, fdate, year, assets, cost, resolution in cur.fetchall():
        f = OUT / f"{cert}.json"
        cr = json.loads(f.read_text()) if f.exists() else {}
        if not cr or not cost or not assets or not cr.get("LNLSNET"):
            continue
        r = {"cert": cert, "bank": bank, "state": state, "fail_date": str(fdate), "fail_year": year,
             "assets_k": assets, "cost_k": cost, "loss_rate": cost / assets, "resolution": resolution,
             "report_date": cr.get("REPDTE")}
        for k, (num, den, _) in RATIOS.items():
            d = cr.get(den) or 0
            r[k] = (cr.get(num) or 0) / d if d else None
        rows.append(r)
    return rows


def quantile(vals: list[float], q: float) -> float:
    v = sorted(vals)
    pos = (len(v) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def items_for(rows: list[dict]) -> tuple[list[set], dict]:
    """Each bank → the set of 'high X' items it has (top quartile among failed banks; low equity = bottom quartile)."""
    cut = {}
    for k in RATIOS:
        vals = [r[k] for r in rows if r[k] is not None]
        if vals:
            cut[k] = quantile(vals, 0.25 if k in LOW_IS_RISK else 0.75)
    baskets = []
    for r in rows:
        b = set()
        for k, c in cut.items():
            if r[k] is None:
                continue
            if (k in LOW_IS_RISK and r[k] <= c) or (k not in LOW_IS_RISK and r[k] >= c):
                b.add(k)
        baskets.append(b)
    return baskets, cut


# ---------------------------------------------------------------- association rules (exhaustive, up to 2 antecedents)
def rules(baskets: list[set], target: list[bool], min_support: float = 0.05, max_len: int = 2) -> list[dict]:
    n, base = len(baskets), sum(target) / len(target)
    out = []
    keys = sorted(set().union(*baskets))
    for size in range(1, max_len + 1):
        for ante in itertools.combinations(keys, size):
            has = [all(a in b for a in ante) for b in baskets]
            cnt = sum(has)
            if cnt / n < min_support:
                continue
            hit = sum(1 for h, t in zip(has, target) if h and t)
            conf = hit / cnt
            out.append({"antecedent": list(ante), "support": cnt / n, "count": cnt, "confidence": conf,
                        "lift": conf / base if base else None})
    return sorted(out, key=lambda r: -r["lift"])


def permutation_threshold(baskets: list[set], target: list[bool], runs: int = 200, seed: int = 7) -> float:
    """95th percentile of the BEST lift found when the costly/not labels are shuffled: what chance alone produces."""
    rng = random.Random(seed)
    best = []
    t = list(target)
    for _ in range(runs):
        rng.shuffle(t)
        r = rules(baskets, t)
        best.append(r[0]["lift"] if r else 1.0)
    return quantile(best, 0.95)


# ---------------------------------------------------------------- clustering (k-means on standardized loan mix)
def kmeans(X: list[list[float]], k: int, seed: int = 0, iters: int = 100) -> tuple[list[int], list[list[float]]]:
    rng = random.Random(seed)
    cent = [list(X[i]) for i in rng.sample(range(len(X)), k)]   # k-means++ would be nicer; 20 restarts compensate
    lab = [0] * len(X)
    for _ in range(iters):
        new = [min(range(k), key=lambda c: sum((a - b) ** 2 for a, b in zip(x, cent[c]))) for x in X]
        if new == lab:
            break
        lab = new
        for c in range(k):
            pts = [x for x, l in zip(X, lab) if l == c]
            if pts:
                cent[c] = [sum(col) / len(pts) for col in zip(*pts)]
    return lab, cent


def silhouette(X: list[list[float]], lab: list[int]) -> float:
    def d(a, b):
        return math.sqrt(sum((p - q) ** 2 for p, q in zip(a, b)))
    ks = set(lab)
    if len(ks) < 2:
        return -1.0
    s = []
    for i, x in enumerate(X):
        same = [d(x, y) for j, y in enumerate(X) if lab[j] == lab[i] and j != i]
        if not same:
            s.append(0.0)
            continue
        a = sum(same) / len(same)
        b = min(sum(d(x, y) for j, y in enumerate(X) if lab[j] == c) / max(1, lab.count(c)) for c in ks if c != lab[i])
        s.append((b - a) / max(a, b))
    return sum(s) / len(s)


def cluster(rows: list[dict]) -> tuple[list[int], int, dict]:
    X0 = [[r[k] or 0.0 for k in MIX] for r in rows]
    mu = [sum(c) / len(c) for c in zip(*X0)]
    sd = [math.sqrt(sum((v - m) ** 2 for v in c) / len(c)) or 1.0 for c, m in zip(zip(*X0), mu)]
    X = [[(v - m) / s for v, m, s in zip(x, mu, sd)] for x in X0]
    scores = {}
    best = None
    for k in range(2, 7):
        lab, _ = min((kmeans(X, k, seed) for seed in range(20)),
                     key=lambda lc: sum(sum((a - b) ** 2 for a, b in zip(x, lc[1][l])) for x, l in zip(X, lc[0])))
        scores[k] = round(silhouette(X, lab), 3)
        if best is None or scores[k] > scores[best[0]]:
            best = (k, lab)
    return best[1], best[0], scores


def describe_cluster(members: list[dict], everyone: list[dict]) -> str:
    """Label a cluster by the loan types it holds MORE of than the average failed bank (largest z-scores)."""
    z = {}
    for k in MIX:
        allv = [r[k] or 0.0 for r in everyone]
        mu = sum(allv) / len(allv)
        sd = math.sqrt(sum((v - mu) ** 2 for v in allv) / len(allv)) or 1.0
        z[k] = (sum(r[k] or 0.0 for r in members) / len(members) - mu) / sd
    top = [k for k in sorted(z, key=lambda k: -z[k]) if z[k] > 0.25][:2] or [max(z, key=z.get)]
    names = [RATIOS[k][2].split(" (")[0].replace(" loans", "") for k in top]
    return "more " + " and ".join(names) + " than the average failed bank"


# ---------------------------------------------------------------- run
DDL = """
IF OBJECT_ID('kb.failure_profile') IS NULL
CREATE TABLE kb.failure_profile (
    cert int NOT NULL CONSTRAINT pk_failure_profile PRIMARY KEY, bank nvarchar(120), state nchar(2), fail_date date,
    report_date nvarchar(8), assets_k bigint, cost_k bigint, loss_rate float, costly bit, cluster_id int,
    construction_share float, cre_share float, residential_share float, multifamily_share float, farm_share float,
    ci_share float, consumer_share float, brokered_share float, equity_ratio float, noncurrent_ratio float,
    loan_to_asset float);
IF OBJECT_ID('kb.mining_rule') IS NULL
CREATE TABLE kb.mining_rule (
    rule_id int NOT NULL CONSTRAINT pk_mining_rule PRIMARY KEY, antecedent nvarchar(300) NOT NULL,
    support float, banks int, confidence float, lift float, beats_chance bit, chance_lift_95 float);
IF OBJECT_ID('kb.failure_cluster') IS NULL
CREATE TABLE kb.failure_cluster (
    cluster_id int NOT NULL CONSTRAINT pk_failure_cluster PRIMARY KEY, label nvarchar(200), banks int,
    median_loss_rate float, costly_share float, median_assets_k float, mix_json nvarchar(2000));
"""


def run() -> dict:
    conn = connect_sql()
    rows = profiles(conn)
    cut_cost = quantile([r["loss_rate"] for r in rows], 2 / 3)
    for r in rows:
        r["costly"] = r["loss_rate"] >= cut_cost
    baskets, cuts = items_for(rows)
    target = [r["costly"] for r in rows]
    found = rules(baskets, target)
    chance = permutation_threshold(baskets, target)
    for f in found:
        f["beats_chance"] = f["lift"] > chance
    labels, k, sil = cluster(rows)
    for r, l in zip(rows, labels):
        r["cluster_id"] = l
    clusters = []
    for c in range(k):
        m = [r for r in rows if r["cluster_id"] == c]
        lr = sorted(r["loss_rate"] for r in m)
        clusters.append({"cluster_id": c, "label": describe_cluster(m, rows), "banks": len(m),
                         "median_loss_rate": lr[len(lr) // 2], "costly_share": sum(r["costly"] for r in m) / len(m),
                         "median_assets_k": sorted(r["assets_k"] for r in m)[len(m) // 2],
                         "mix": {k2: round(sum(r[k2] or 0 for r in m) / len(m), 3) for k2 in MIX}})
    cur = conn.cursor()
    cur.execute(DDL)
    for t in ("kb.failure_profile", "kb.mining_rule", "kb.failure_cluster"):
        cur.execute(f"DELETE FROM {t}")
    cols = ["cert", "bank", "state", "fail_date", "report_date", "assets_k", "cost_k", "loss_rate", "costly",
            "cluster_id", *RATIOS]
    cur.executemany(f"INSERT INTO kb.failure_profile ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [tuple(r[c] for c in cols) for r in rows])
    label = lambda k2: ("low " if k2 in LOW_IS_RISK else "high ") + RATIOS[k2][2]  # noqa: E731
    cur.executemany("""INSERT INTO kb.mining_rule (rule_id, antecedent, support, banks, confidence, lift, beats_chance,
                       chance_lift_95) VALUES (?,?,?,?,?,?,?,?)""",
                    [(i, " AND ".join(label(a) for a in f["antecedent"]), round(f["support"], 4), f["count"],
                      round(f["confidence"], 4), round(f["lift"], 3), f["beats_chance"], round(chance, 3))
                     for i, f in enumerate(found, 1)])
    cur.executemany("""INSERT INTO kb.failure_cluster (cluster_id, label, banks, median_loss_rate, costly_share,
                       median_assets_k, mix_json) VALUES (?,?,?,?,?,?,?)""",
                    [(c["cluster_id"], c["label"], c["banks"], round(c["median_loss_rate"], 4),
                      round(c["costly_share"], 3), c["median_assets_k"], json.dumps(c["mix"])) for c in clusters])
    conn.commit()
    summary = {"failures_with_profile": len(rows), "costly_cut_loss_rate": round(cut_cost, 4),
               "base_rate_costly": round(sum(target) / len(target), 3),
               "item_cutoffs": {k2: round(v, 4) for k2, v in cuts.items()},
               "chance_lift_95": round(chance, 3),
               "rules_beating_chance": [{**f, "antecedent": [label(a) for a in f["antecedent"]]}
                                        for f in found if f["beats_chance"]][:15],
               "top_rules_any": [{**f, "antecedent": [label(a) for a in f["antecedent"]]} for f in found[:10]],
               "k": k, "silhouette": sil, "clusters": clusters}
    (ROOT / "docs" / "mining.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "fetch":
        fetch()
        return
    s = run()
    print(f"{s['failures_with_profile']} failures · costly = loss rate >= {s['costly_cut_loss_rate']:.1%} "
          f"(base rate {s['base_rate_costly']:.0%}) · chance lift (95th pct of shuffled best) {s['chance_lift_95']}")
    for f in s["rules_beating_chance"][:10]:
        print(f"  lift {f['lift']:.2f}  conf {f['confidence']:.0%}  support {f['support']:.0%} ({f['count']} banks): "
              f"{' AND '.join(f['antecedent'])}")
    print(f"k-means k={s['k']} (silhouette {s['silhouette']})")
    for c in sorted(s["clusters"], key=lambda c: -c["median_loss_rate"]):
        print(f"  {c['label']:<60} {c['banks']:>3} banks · median loss {c['median_loss_rate']:.1%} · "
              f"costly {c['costly_share']:.0%}")


if __name__ == "__main__":
    main()

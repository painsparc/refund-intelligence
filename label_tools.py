"""
label_tools.py  -  two-annotator labelling workflow for the INDEPENDENT NLP/sentiment benchmark (offline; not part of the real-time system)

    python eval_data.py labelling-sheet --n 200          # 1. random real reviews (Olist + Amazon), blank labels   -> data/manual_label_sheet.csv
    python label_tools.py split                          # 2. two blind copies for two teammates                   -> data/labelling/annotator_A.csv, annotator_B.csv
         (each teammate fills ONLY their own file, without looking at the other one; criteria: docs/labelling_guidelines.md)
    python label_tools.py reconcile                      # 3. agreement (Cohen's kappa) + disagreement list        -> data/labelling/agreement.json, disagreements.csv
         (the two annotators discuss every row of disagreements.csv and fill the `final_*` columns)
    python label_tools.py finalize                       # 4. only when every disagreement is resolved             -> data/manual_labels_real.csv

Label origins are kept apart (see data/README.md): developer-authored set, independent human labels (this tool), source labels (star ratings),
synthetic labels (eval_labels). Nothing here writes into the database. Star ratings are hidden from annotators so they cannot copy them.
"""
import argparse
import csv
import json
import os
import random
import sys

DIR = os.path.join("data", "labelling")
SHEET = os.path.join("data", "manual_label_sheet.csv")
FIELDS = ["label_category", "text_supports_claim", "label_sentiment"]
CATEGORIES = ["damaged", "defective", "wrong_item", "not_as_described", "size_issue", "changed_mind", "not_delivered", "unclear"]   # = intelligence.CATEGORY_LABELS + unclear
SENTIMENTS = ["negative", "neutral", "positive"]
VALID = {"label_category": set(CATEGORIES), "text_supports_claim": {"0", "1"}, "label_sentiment": set(SENTIMENTS)}
OUT_COLUMNS = ["id", "text", "claimed_reason", "label_category", "text_supports_claim", "label_sentiment", "tag", "source"]
REASONS = [c for c in CATEGORIES if c != "unclear"]


def read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write(path, rows, columns):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def cohen_kappa(a, b):
    """Cohen's kappa for two raters over the same items (None if undefined: no items, or both raters used one single label)."""
    n = len(a)
    if n == 0:
        return None
    labels = sorted(set(a) | set(b))
    po = sum(x == y for x, y in zip(a, b)) / n
    pe = sum((a.count(l) / n) * (b.count(l) / n) for l in labels)
    return None if pe >= 1.0 else round((po - pe) / (1 - pe), 4)


def split(sheet=SHEET, outdir=DIR, seed=7):
    """Two identical blind copies. `claimed_reason` is ASSIGNED here (seeded, uniformly from the seven reasons) so that both supporting and
    conflicting reason/text pairs occur; it is NOT something the reviewer said. Ratings are removed so annotators cannot copy them."""
    rows = read(sheet)
    if not rows:
        raise SystemExit(f"{sheet} is empty")
    rnd = random.Random(seed)
    for r in rows:
        r["claimed_reason"] = r.get("claimed_reason") or rnd.choice(REASONS)
        for f in FIELDS:
            r[f] = ""
    cols = ["id", "text", "claimed_reason", *FIELDS, "notes"]
    paths = []
    for who in ("A", "B"):
        p = os.path.join(outdir, f"annotator_{who}.csv")
        write(p, rows, cols)
        paths.append(p)
    # the reason assignment is stored once so the final file is reproducible
    write(os.path.join(outdir, "assigned_claims.csv"), [{k: r.get(k, "") for k in ("id", "claimed_reason", "source")} for r in rows],
          ["id", "claimed_reason", "source"])
    return paths


def validate(rows, who):
    """Return a list of problems (empty = file is complete and uses only allowed values)."""
    bad = []
    for r in rows:
        for f in FIELDS:
            v = (r.get(f) or "").strip()
            if v not in VALID[f]:
                bad.append(f"annotator {who}, id {r.get('id')}: {f}={v!r} not in {sorted(VALID[f])}")
    return bad


def reconcile(dirpath=DIR):
    a, b = read(os.path.join(dirpath, "annotator_A.csv")), read(os.path.join(dirpath, "annotator_B.csv"))
    problems = validate(a, "A") + validate(b, "B")
    if problems:
        todo = {w: len({p.split(", id ")[1].split(":")[0] for p in problems if p.startswith(f"annotator {w},")}) for w in ("A", "B")}
        print(f"NOT READY: annotator A still has {todo['A']} of {len(a)} rows with a blank or invalid label, annotator B {todo['B']} of {len(b)}.\n"
              "Each teammate fills THEIR OWN file (data/labelling/annotator_A.csv or _B.csv, open in Excel) first; then run reconcile again.\n")
        raise SystemExit("first problems:\n  " + "\n  ".join(problems[:30]) + (f"\n  ... and {len(problems) - 30} more" if len(problems) > 30 else ""))
    bid = {r["id"]: r for r in b}
    if set(bid) != {r["id"] for r in a}:
        raise SystemExit("annotator files do not contain the same ids")
    pairs = [(r, bid[r["id"]]) for r in a]
    report = {"n": len(pairs), "fields": {}}
    dis = []
    for f in FIELDS:
        x, y = [p[0][f].strip() for p in pairs], [p[1][f].strip() for p in pairs]
        report["fields"][f] = {"raw_agreement": round(sum(i == j for i, j in zip(x, y)) / len(x), 4), "cohens_kappa": cohen_kappa(x, y),
                               "disagreements": sum(i != j for i, j in zip(x, y))}
    for ra, rb in pairs:
        diff = [f for f in FIELDS if ra[f].strip() != rb[f].strip()]
        if diff:
            row = {"id": ra["id"], "text": ra["text"], "claimed_reason": ra["claimed_reason"], "differing_fields": ";".join(diff)}
            for f in FIELDS:
                row[f"A_{f}"], row[f"B_{f}"] = ra[f].strip(), rb[f].strip()
                row[f"final_{f}"] = "" if f in diff else ra[f].strip()      # agreed fields are pre-filled; disputed ones need a joint decision
            row["resolution_note"] = ""
            dis.append(row)
    report["rows_with_any_disagreement"] = len(dis)
    cols = ["id", "text", "claimed_reason", "differing_fields"] + [f"{p}_{f}" for f in FIELDS for p in ("A", "B", "final")] + ["resolution_note"]
    write(os.path.join(dirpath, "disagreements.csv"), dis, cols)
    json.dump(report, open(os.path.join(dirpath, "agreement.json"), "w"), indent=2)
    return report


def finalize(dirpath=DIR, out=os.path.join("data", "manual_labels_real.csv")):
    """Merge agreed labels + resolved disagreements into data/manual_labels_real.csv (the file eval_data.py picks up). Refuses if anything is open."""
    a = read(os.path.join(dirpath, "annotator_A.csv"))
    if not os.path.exists(os.path.join(dirpath, "disagreements.csv")):
        raise SystemExit("run `python label_tools.py reconcile` first (it needs both annotator files fully labelled)")
    dis = {r["id"]: r for r in read(os.path.join(dirpath, "disagreements.csv"))}
    sheet = {r["id"]: r for r in read(SHEET)} if os.path.exists(SHEET) else {}
    rows, open_items = [], []
    for r in a:
        final = {f: r[f].strip() for f in FIELDS}
        d = dis.get(r["id"])
        if d:
            for f in FIELDS:
                v = (d.get(f"final_{f}") or "").strip()
                if v not in VALID[f]:
                    open_items.append(f"{r['id']}: final_{f}={v!r}")
                final[f] = v
        rows.append({"id": r["id"], "text": r["text"], "claimed_reason": r["claimed_reason"], **final, "tag": "real",
                     "source": sheet.get(r["id"], {}).get("source", "")})
    if open_items:
        raise SystemExit("unresolved disagreements:\n  " + "\n  ".join(open_items[:30]))
    write(out, rows, OUT_COLUMNS)
    return out, len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["split", "reconcile", "finalize"])
    ap.add_argument("--sheet", default=SHEET), ap.add_argument("--dir", default=DIR), ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    if a.cmd == "split":
        print("wrote", *split(a.sheet, a.dir, a.seed), "- give one file to each teammate; they must not compare until both are done")
    elif a.cmd == "reconcile":
        r = reconcile(a.dir)
        print(json.dumps(r, indent=2))
        print(f"fill the final_* columns in {os.path.join(a.dir, 'disagreements.csv')} together, then run `python label_tools.py finalize`")
    else:
        path, n = finalize(a.dir)
        print(f"wrote {path} ({n} rows); `python eval_data.py evaluate` now reports it separately from the developer-authored set")


if __name__ == "__main__":
    sys.exit(main())

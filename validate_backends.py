"""
validate_backends.py  -  real-weights validation of the OPTIONAL transformer / sentiment / CLIP backends (offline; not part of the real-time system)

    pip install -r requirements-nlp.txt
    python validate_backends.py                                  # defaults: the models named in intelligence.py
    python validate_backends.py --claim-model MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7 \
                                --sentiment-model cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual \
                                --pairs data/image_text_pairs.csv

Needs internet access to the model hub (the build environment had none, so this has NEVER been run with real weights - the report this writes says
so honestly when a model cannot load). It
  1. checks the packages and records versions,
  2. evaluates claim-reason classification (overall, negation, paraphrase, hard cases) and sentiment on every label file that exists
     (data/manual_claims_labelled.csv = developer-authored; data/manual_labels_real.csv = independent team labels), rules/VADER baseline vs transformer,
  3. evaluates CLIP text-image consistency on data/image_text_pairs.csv (columns image_path,category,consistent[0/1]; image_path relative to data/),
  4. proves the fallback: with an unloadable model the system keeps running on the rules and says which backend ran,
and writes reports/backend_validation.json and docs/backend_validation.md (selected backend, metrics, failure cases, fallback behaviour).
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///eval_data.db")

import argparse
import csv
import json
import sys
from datetime import datetime

import eval_data as ed
import intelligence as ai

LABEL_FILES = {"developer-authored set": os.path.join("data", "manual_claims_labelled.csv"),
               "team labels of real reviews": os.path.join("data", "manual_labels_real.csv")}
CLIP_MISMATCH_THRESHOLD = 0.4          # intelligence.py raises `image_text_mismatch` below this consistency


def reset_backends():
    ai._NLP.update(fn=None, tried=False, backend="rules")
    ai._SENT.update(fn=None, tried=False, backend="vader")
    ai._IMG_TEXT.update(fn=None, tried=False)


def versions():
    out = {}
    for pkg in ("transformers", "torch", "cv2", "PIL"):
        try:
            m = __import__(pkg)
            out[pkg] = getattr(m, "__version__", "installed")
        except Exception as e:  # noqa: BLE001
            out[pkg] = f"NOT AVAILABLE ({type(e).__name__}: {e})"
    return out


def evaluate_labels(path):
    r = ed._eval_label_file(path)
    return {"n": r["n"], "systems": r["systems"], "sentiment": r["sentiment"], "errors": r["errors"]}


def auc(scores, labels):
    """Rank AUC of `scores` for label 1 (None when one class is missing)."""
    pos, neg = [s for s, l in zip(scores, labels) if l == 1], [s for s, l in zip(scores, labels) if l == 0]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 4)


def validate_text(claim_model, sentiment_model):
    res = {}
    os.environ.pop("CLAIM_NLP", None), os.environ.pop("CLAIM_NLP_MODEL", None), os.environ.pop("SENTIMENT_MODEL", None)
    reset_backends()
    for name, p in LABEL_FILES.items():
        if os.path.exists(p):
            res.setdefault(name, {})["baseline (rules + VADER)"] = evaluate_labels(p)
    os.environ["CLAIM_NLP"] = "transformer"
    if claim_model:
        os.environ["CLAIM_NLP_MODEL"] = claim_model
    if sentiment_model:
        os.environ["SENTIMENT_MODEL"] = sentiment_model
    reset_backends()
    loaded_claim = ai._text_classifier() is not None
    ai._sentiment_fn()
    status = {"claim_backend_requested": os.getenv("CLAIM_NLP_MODEL", "default zero-shot model in intelligence.py"),
              "claim_backend_loaded": ai._NLP["backend"] if loaded_claim else "NOT LOADED -> rules",
              "sentiment_backend_loaded": ai._SENT["backend"]}
    for name, p in LABEL_FILES.items():
        if os.path.exists(p):
            res.setdefault(name, {})["transformer run"] = evaluate_labels(p)
    return status, res


def validate_clip(pairs_path, clip_model):
    if not os.path.exists(pairs_path):
        return {"status": "not_run", "reason": f"{pairs_path} not found. Provide image_path,category,consistent (1 = the photo shows the claimed "
                                               "condition, 0 = it does not), e.g. 50 damaged-product photos + 50 intact-product photos."}
    os.environ["IMAGE_TEXT"] = "clip"
    if clip_model:
        os.environ["CLIP_MODEL"] = clip_model
    reset_backends()
    fn = ai._image_text_fn()
    if not fn:
        return {"status": "not_run", "reason": "CLIP could not be loaded (see the printed '[text-image consistency unavailable]' message)"}
    rows = list(csv.DictReader(open(pairs_path, encoding="utf-8")))
    scores, labels, failures = [], [], []
    for r in rows:
        p = os.path.join(os.path.dirname(pairs_path), r["image_path"])
        s = fn(open(p, "rb").read(), r["category"])
        if s is None:
            continue
        scores.append(s), labels.append(int(r["consistent"]))
        if (s >= CLIP_MISMATCH_THRESHOLD) != bool(labels[-1]):
            failures.append({"image": r["image_path"], "category": r["category"], "consistency": round(s, 3), "gold_consistent": labels[-1]})
    flagged = [s < CLIP_MISMATCH_THRESHOLD for s in scores]
    inconsistent = [l == 0 for l in labels]
    tp = sum(f and i for f, i in zip(flagged, inconsistent))
    return {"status": "run", "model": os.getenv("CLIP_MODEL", "openai/clip-vit-base-patch32"), "n": len(scores), "auc_consistent_vs_not": auc(scores, labels),
            "accuracy_at_system_threshold": round(sum((s >= CLIP_MISMATCH_THRESHOLD) == bool(l) for s, l in zip(scores, labels)) / max(len(scores), 1), 4),
            "mismatch_flag_precision": round(tp / max(sum(flagged), 1), 4), "mismatch_flag_recall": round(tp / max(sum(inconsistent), 1), 4),
            "system_threshold": CLIP_MISMATCH_THRESHOLD, "failure_cases": failures[:25]}


def validate_fallback():
    """With a model name that cannot load, the claim pipeline must keep working on the rules and name the backend it used."""
    os.environ["CLAIM_NLP"] = "transformer"
    os.environ["CLAIM_NLP_MODEL"] = "no-such-org/no-such-model"
    os.environ["IMAGE_TEXT"] = "clip"
    os.environ["CLIP_MODEL"] = "no-such-org/no-such-clip"
    reset_backends()
    cat, backend = ai.classify_text_category("The jar arrived cracked at the base.")
    interp = ai.interpret_claim({"reason": "damaged", "description": "The jar arrived cracked at the base.", "amount": 10})
    ok = backend == "rules" and cat == "damaged" and ai._image_text_fn() is None
    return {"fallback_ok": ok, "category": cat, "backend_reported": backend, "interpret_claim_backend": interp.get("nlp_backend"),
            "image_text_scorer_available": ai._image_text_fn() is not None}


def write_markdown(rep):
    L = ["# Backend validation (auto-generated)\n", f"Generated {rep['generated_at']} by `python validate_backends.py`.\n",
         "## Environment\n", *[f"- {k}: {v}" for k, v in rep["environment"].items()], "", "## Selected backends\n",
         *[f"- {k}: {v}" for k, v in rep["text_backends"].items()], ""]
    for name, runs in rep["text_results"].items():
        L.append(f"## Claim NLP + sentiment on the {name}\n")
        for run, r in runs.items():
            L.append(f"### {run} (n={r['n']})\n")
            for sysname, d in r["systems"].items():
                L.append(f"- **{sysname}**: " + ", ".join(f"{k}={v}" for k, v in d.items()))
            L.append("- **sentiment**: " + ", ".join(f"{k}={v}" for k, v in r["sentiment"].items()))
            L.append(f"- failure cases: {len(r['errors']['category_errors_rules'])} category errors (rules), "
                     f"{len(r['errors']['sentiment_negative_missed'])} negative texts missed - listed in `reports/backend_validation.json`\n")
    c = rep["clip"]
    L.append("## CLIP text-image consistency\n")
    L.append(f"**NOT RUN** - {c['reason']}\n" if c["status"] != "run" else "\n".join(f"- {k}: {v}" for k, v in c.items() if k != "failure_cases") + "\n")
    L.append("## Fallback behaviour\n")
    L += [f"- {k}: {v}" for k, v in rep["fallback"].items()]
    L.append("\n> Interpretation rule: a transformer is adopted only if it beats the rules on the INDEPENDENT team labels (not the developer-authored set), "
             "keeps negation accuracy, and the failure cases above are acceptable. Otherwise the rules stay the default.")
    open(os.path.join(ed.DOCS, "backend_validation.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--claim-model"), ap.add_argument("--sentiment-model"), ap.add_argument("--clip-model")
    ap.add_argument("--pairs", default=os.path.join("data", "image_text_pairs.csv"))
    a = ap.parse_args(argv)
    env = versions()
    status, text = validate_text(a.claim_model, a.sentiment_model)
    clip = validate_clip(a.pairs, a.clip_model)
    fb = validate_fallback()
    rep = {"generated_at": datetime.utcnow().isoformat(timespec="seconds"), "environment": env, "text_backends": status, "text_results": text,
           "clip": clip, "fallback": fb}
    os.makedirs(ed.REPORTS, exist_ok=True), os.makedirs(ed.DOCS, exist_ok=True)
    json.dump(rep, open(os.path.join(ed.REPORTS, "backend_validation.json"), "w"), indent=2, default=str, ensure_ascii=False)
    write_markdown(rep)
    print(json.dumps({"text_backends": status, "clip": clip.get("status"), "fallback_ok": fb["fallback_ok"]}, indent=2))
    print(f"wrote {ed.REPORTS}/backend_validation.json and {ed.DOCS}/backend_validation.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())

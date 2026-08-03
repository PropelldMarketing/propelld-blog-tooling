"""
triage_review_links.py — LLM triage of the audit's REVIEW-flagged links.

The audit left 3,408 links flagged REVIEW (mostly cross-category with no
bridge exception) with nobody to review them. This script has Haiku decide
each one with a confidence score, applies decisions only when confident,
and leaves a small human queue.

Bands (deliberately KEEP-biased — wrongly deleting a good link costs more
than keeping a mediocre one):
  KEEP with confidence >= 0.70  -> auto-KEEP
  KILL with confidence >= 0.90  -> auto-KILL
  everything else               -> HUMAN (short queue for manual review)

Outputs:
  out/review-triage.csv                    every decision + reason
  out/internal-links-inventory-triaged.csv full inventory with REVIEW rows
                                           replaced by triage decisions;
                                           feed this to bulk_apply_audit
                                           after reviewing the HUMAN queue.

Cost: ~3,400 links in batches of 20 ≈ 170 Haiku calls ≈ $3-5.

USAGE:
  python scripts/triage_review_links.py --dry-run
  python scripts/triage_review_links.py --apply
  python scripts/triage_review_links.py --apply --limit 100   # calibration run

Env: ANTHROPIC_API_KEY
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import anthropic
except ImportError:
    anthropic = None

MODEL = "claude-haiku-4-5"
BATCH = 20
KEEP_CONF = 0.70
KILL_CONF = 0.90

PROMPT = """You are auditing internal links on Propelld, an Indian education-loan platform's blog. Each link below was flagged "cross-category" (source article and target article belong to different content categories). Decide for each whether the link genuinely helps a reader of the source article (KEEP) or is an irrelevant/confusing jump that should be removed (KILL).

Judge by: would a reader of the SOURCE article plausibly want the TARGET article at that point? Related student journeys count as helpful (exam -> loan for that course, study abroad -> loan, course -> career). Random jumps between unrelated exams, unrelated courses, or loan posts linking to unrelated exam trivia do not.

LINKS:
{items}

Return ONLY JSON:
{{"decisions": [{{"id": 1, "action": "KEEP" or "KILL", "confidence": 0.0-1.0, "reason": "under 15 words"}}, ...]}}"""


def load_titles(path):
    df = pd.read_excel(path) if path.endswith(".xlsx") else pd.read_csv(path)
    ren = {o: n for o, n in [("URL", "url"), ("Title", "title"), ("Tier", "tier"),
                             ("Category", "category")] if o in df.columns}
    df = df.rename(columns=ren)
    df["url"] = df["url"].astype(str).str.rstrip("/")
    cols = [c for c in ["title", "category", "tier"] if c in df.columns]
    return df.set_index("url")[cols].to_dict("index")


def describe(url, meta):
    m = meta.get(url.rstrip("/"), {})
    title = m.get("title") or url.rsplit("/", 1)[-1].replace("-", " ")
    return f"{title} [category: {m.get('category', '?')}, tier: {m.get('tier', '?')}]"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--inventory", default="data/internal-links-inventory.csv")
    p.add_argument("--tier-file", default="data/posts-with-tiers.xlsx")
    p.add_argument("--output", default="out/review-triage.csv")
    p.add_argument("--merged-output", default="out/internal-links-inventory-triaged.csv")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--sleep", type=float, default=0.2)
    p.add_argument("--model", default=MODEL)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    if not a.apply and not a.dry_run:
        print("Must pass --apply or --dry-run")
        sys.exit(1)

    inv = pd.read_csv(a.inventory)
    meta = load_titles(a.tier_file)
    review = inv[inv["action"] == "REVIEW"].reset_index(drop=True)
    # reverse-waterfall-2 REVIEWs stay for human eyes; triage the cross-category bulk
    review = review[review["reason"] == "cross-category"].reset_index(drop=True)
    if a.limit:
        review = review.head(a.limit)
    print(f"{len(review)} cross-category REVIEW links to triage")

    if a.dry_run:
        items = "\n".join(
            f"{j+1}. SOURCE: {describe(r.source_url, meta)}\n"
            f"   TARGET: {describe(r.target_url, meta)}\n"
            f"   anchor text: \"{r.anchor_text}\""
            for j, r in enumerate(review.head(BATCH).itertuples()))
        print(PROMPT.format(items=items)[:2500])
        return
    if anthropic is None:
        print("ERROR: pip install anthropic")
        sys.exit(1)
    ac = anthropic.Anthropic()

    decisions = []
    for i in range(0, len(review), BATCH):
        batch = review.iloc[i:i + BATCH]
        items = "\n".join(
            f"{j+1}. SOURCE: {describe(r.source_url, meta)}\n"
            f"   TARGET: {describe(r.target_url, meta)}\n"
            f"   anchor text: \"{r.anchor_text}\""
            for j, r in enumerate(batch.itertuples()))
        try:
            resp = ac.messages.create(
                model=a.model, max_tokens=2500,
                messages=[{"role": "user", "content": PROMPT.format(items=items)}])
            text = resp.content[0].text
            obj, _ = json.JSONDecoder().raw_decode(text[text.find("{"):])
            by_id = {int(d.get("id", 0)): d for d in obj.get("decisions", [])}
        except Exception as e:
            print(f"  batch {i//BATCH}: error {str(e)[:120]}")
            by_id = {}
        for j, (_, row) in enumerate(batch.iterrows()):
            d = by_id.get(j + 1, {})
            act = str(d.get("action", "")).upper()
            try:
                conf = float(d.get("confidence", 0))
            except (TypeError, ValueError):
                conf = 0.0
            if act == "KEEP" and conf >= KEEP_CONF:
                final = "KEEP"
            elif act == "KILL" and conf >= KILL_CONF:
                final = "KILL"
            else:
                final = "HUMAN"
            decisions.append({**row.to_dict(),
                              "llm_action": act or "NONE",
                              "llm_confidence": conf,
                              "llm_reason": str(d.get("reason", ""))[:200],
                              "final_action": final})
        if (i // BATCH) % 10 == 9:
            print(f"  [{i + len(batch)}/{len(review)}] triaged")
        time.sleep(a.sleep)

    out = pd.DataFrame(decisions)
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(a.output, index=False)
    print(f"\n✓ Wrote {a.output}")
    print(out["final_action"].value_counts().to_string())

    # merged inventory: REVIEW rows replaced by triage outcome
    key = ["source_url", "target_url", "anchor_text", "position_in_field"]
    dec_map = out.set_index(key)["final_action"].to_dict() if len(out) else {}
    merged = inv.copy()

    def remap(r):
        if r["action"] != "REVIEW":
            return r["action"]
        f = dec_map.get((r["source_url"], r["target_url"],
                         r["anchor_text"], r["position_in_field"]))
        if f == "KEEP":
            return "KEEP"
        if f == "KILL":
            return "KILL"
        return "REVIEW"  # HUMAN queue or untriaged stays REVIEW
    merged["action"] = merged.apply(remap, axis=1)
    merged.to_csv(a.merged_output, index=False)
    print(f"✓ Wrote {a.merged_output}")
    print(merged["action"].value_counts().to_string())
    human = out[out["final_action"] == "HUMAN"]
    print(f"\nHuman queue: {len(human)} links "
          f"({100 * len(human) / max(len(out), 1):.0f}% of triaged)")


if __name__ == "__main__":
    main()

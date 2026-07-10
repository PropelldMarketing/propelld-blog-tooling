"""
priority_scorer.py -- compute 6-input tier score for every blog post.

Formula (strategy doc v3 sec 4):
  Score = 0.30*CommercialIntent + 0.20*LeadVolume + 0.15*ConversionProximity
        + 0.15*GSC_Post_Update + 0.15*SearchVolume + 0.05*GSC_Pre_Update

USAGE:
  python scripts/priority_scorer.py \
      --screaming-frog data/propelld_internal_html.xlsx \
      --gsc-export data/gsc_16mo.xlsx \
      --leads-csv data/seo_leads_main.csv \
      --disbursement-csv data/mis_disbursements.csv \
      --tier-overrides data/tier_overrides.csv \
      --pillar-shortlists data/pillar_shortlists.csv \
      --output out/posts-with-tiers.xlsx --apply
"""

import argparse, json, sys
from pathlib import Path
import numpy as np
import pandas as pd

POST_UPDATE_SHARE = 0.432  # doc sec 4: 43.2% of catalogue clicks post-Jun-7-2025

def load_grammar():
    p = Path(__file__).parent.parent / "data" / "category_grammar_rules.json"
    return json.load(open(p))

def log_normalize(s):
    a = np.log1p(s.astype(float))
    if a.max() == a.min(): return pd.Series([50] * len(a), index=s.index)
    return (a - a.min()) / (a.max() - a.min()) * 100

def score_commercial(url, title, lex):
    text = (str(url) + " " + str(title)).lower()
    s = 0
    for term, w in lex.get("high", {}).items():
        if term in text: s += w
    for term, w in lex.get("medium", {}).items():
        if term in text: s += w
    return min(100, s)

def infer_category(url, title):
    text = (str(url) + " " + str(title)).lower()
    SCH = ["scholarship","pmrf","ssp-scholarship","vidyasaarathi","inspire-scholarship","post-matric","pre-matric","prize-money-scholarship","minority-scholarship"]
    FIN = ["cibil","credit-score","best-student-loan-apps","tax-benefit","80e","moratorium","loan-insurance","instant-loan","low-credit","loan-transfer","loan-refinance","loan-repayment","loan-prepayment","laptop-quotation","education-loan-forum"]
    LOAN = ["loan","emi","interest-rate","lender","nbfc","credila","sbi","hdfc","icici","axis","avanse","incred","auxilo","union-bank","pnb","bank-of-baroda","idfc","muthoot","indian-bank","yes-bank"]
    ABROAD = ["abroad","study-in-","mba-in-","ms-in-","usa","uk-","canada","germany","france","australia","ireland","singapore","new-zealand","luxembourg","italy","poland","japan","dubai","cost-of-living","ielts","gmat","gre","toefl"]
    EXAM = ["exam","cutoff","rank","syllabus","registration","admit-card","answer-key","result","pattern","counselling","predictor","jee","neet","cat-exam","gate","cuet","cet","ipmat","iiser","kiitee","polycet","eamcet","icet","cusat"]
    if any(k in text for k in SCH): return "Government Schemes & Scholarships"
    if any(k in text for k in FIN): return "Finance & Credit Education"
    if any(k in text for k in LOAN): return "Education Loans"
    if any(k in text for k in ABROAD): return "Study Abroad"
    if any(k in text for k in EXAM): return "Exams & Counselling"
    return "Courses & Careers"

def assign_tier(score, is_pillar):
    if is_pillar: return "T1P"
    if score >= 70: return "T1"
    if score >= 55: return "T2"
    if score >= 35: return "T3"
    return "T4"

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--screaming-frog", default=None, help="Screaming Frog crawl xlsx (proprietary, not in repo)")
    p.add_argument("--gsc-export", default=None, help="GSC 16-month export xlsx (proprietary, not in repo)")
    p.add_argument("--leads-csv", default=None, help="CRM leads xlsx (proprietary, not in repo)")
    p.add_argument("--disbursement-csv", default=None)
    p.add_argument("--tier-overrides", default=None)
    p.add_argument("--pillar-shortlists", default=None)
    p.add_argument("--output", default="out/posts-with-tiers.xlsx")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()

    # Post-parse check: 3 required files can't be committed to repo (proprietary).
    # If any is missing, print a helpful error instead of a cryptic argparse crash.
    missing = [name for name, val in [("--screaming-frog", a.screaming_frog),
                                       ("--gsc-export", a.gsc_export),
                                       ("--leads-csv", a.leads_csv)] if not val]
    if missing:
        import sys
        print(f"ERROR: priority_scorer needs 3 input files (not in the repo — proprietary data).", file=sys.stderr)
        print(f"       Missing: {', '.join(missing)}", file=sys.stderr)
        print(f"       Upload those files into the repo's data/ folder and commit them, then re-run.", file=sys.stderr)
        print(f"       Example extra_args for the workflow:", file=sys.stderr)
        print(f"         --screaming-frog data/propelld_internal_html.xlsx --gsc-export data/gsc-export.xlsx --leads-csv data/seo-leads.xlsx --apply", file=sys.stderr)
        sys.exit(2)

    grammar = load_grammar()

    print("Loading Screaming Frog...")
    sf = pd.read_excel(a.screaming_frog, sheet_name="1 - HTML")
    sf["url"] = sf["Address"].str.replace("https://propelld.com", "", regex=False)
    sf["is_blog"] = sf["url"].str.match(r"^/site/blog/[a-z0-9\-]+/?$", na=False)
    blog = sf[sf["is_blog"] & (sf["Status Code"] == 200)].copy()
    blog["url"] = blog["url"].str.rstrip("/")
    blog = blog.sort_values("Unique Inlinks", ascending=False).drop_duplicates("url")
    df = blog[["url","Title 1","Word Count","Unique Inlinks","Crawl Depth"]].rename(
        columns={"Title 1":"title","Word Count":"word_count",
                 "Unique Inlinks":"current_inlinks","Crawl Depth":"crawl_depth"})
    print(f"  Blog posts: {len(df)}")

    print("Loading GSC...")
    gsc = pd.read_excel(a.gsc_export, sheet_name="Pages")
    gsc["url"] = gsc["Top pages"].str.replace("https://propelld.com","",regex=False).str.rstrip("/")
    gsc = gsc[["url","Clicks","Impressions","Position"]].rename(
        columns={"Clicks":"gsc_clicks","Impressions":"gsc_impr","Position":"gsc_pos"})

    print("Loading leads...")
    if a.leads_csv.endswith(".xlsx"):
        raw = pd.read_excel(a.leads_csv, sheet_name="main sheet")
    else:
        raw = pd.read_csv(a.leads_csv)
    seo = raw[raw["Source"] == "SEO Leads"]
    leads = seo["Campaign Name"].value_counts().rename_axis("url").reset_index(name="lead_count_90d")
    leads["url"] = leads["url"].astype(str).str.rstrip("/")

    disb = pd.DataFrame(columns=["url","disbursed_loans","disbursed_amount_inr"])
    if a.disbursement_csv:
        d = pd.read_excel(a.disbursement_csv, sheet_name="Disbursed Leads") if a.disbursement_csv.endswith(".xlsx") else pd.read_csv(a.disbursement_csv)
        d = d[d["Source"] == "SEO Leads"]
        disb = d.groupby("Campaign / URL")["Loan Amount (₹)"].agg(["count","sum"]).reset_index()
        disb.columns = ["url","disbursed_loans","disbursed_amount_inr"]
        disb["url"] = disb["url"].astype(str).str.rstrip("/")

    print("Merging + scoring...")
    df = df.merge(gsc, on="url", how="left").fillna({"gsc_clicks":0,"gsc_impr":0,"gsc_pos":100})
    df = df.merge(leads, on="url", how="left").fillna({"lead_count_90d":0})
    df["lead_count_90d"] = df["lead_count_90d"].astype(int)
    df = df.merge(disb, on="url", how="left").fillna({"disbursed_loans":0,"disbursed_amount_inr":0})
    df["category"] = df.apply(lambda r: infer_category(r["url"], r.get("title","")), axis=1)

    lex = grammar["commercial_keywords_lexicon"]
    prox = grammar["conversion_proximity_map"]
    df["s1_commercial_intent"] = df.apply(lambda r: score_commercial(r["url"], r.get("title",""), lex), axis=1)
    df["s2_lead_volume"] = log_normalize(df["lead_count_90d"])
    df["s3_conversion_proximity"] = df["category"].map(prox)
    df["s4_gsc_post"] = log_normalize(df["gsc_clicks"] * POST_UPDATE_SHARE)
    df["s5_search_volume"] = log_normalize(df["gsc_impr"])
    df.loc[df["gsc_impr"] == 0, "s5_search_volume"] = 30
    df["s6_gsc_pre"] = log_normalize(df["gsc_clicks"] * (1 - POST_UPDATE_SHARE))
    df["priority_score"] = (df["s1_commercial_intent"]*0.30 + df["s2_lead_volume"]*0.20
        + df["s3_conversion_proximity"]*0.15 + df["s4_gsc_post"]*0.15
        + df["s5_search_volume"]*0.15 + df["s6_gsc_pre"]*0.05).round(1)

    pillar_urls = set()
    if a.pillar_shortlists and Path(a.pillar_shortlists).exists():
        ps = pd.read_csv(a.pillar_shortlists)
        confirmed = ps[ps["URL"].notna() & ~ps["Status"].fillna("").str.contains("MISSING|pending — URL", na=False)]
        pillar_urls = set(confirmed["URL"].str.rstrip("/"))
    df["is_pillar"] = df["url"].isin(pillar_urls)
    df["tier"] = df.apply(lambda r: assign_tier(r["priority_score"], r["is_pillar"]), axis=1)

    if a.tier_overrides and Path(a.tier_overrides).exists():
        ov = pd.read_csv(a.tier_overrides)
        ov["URL_n"] = ov["URL"].str.rstrip("/")
        n = 0
        for _, r in ov.iterrows():
            t = str(r["New Tier"]).split(" ")[0]
            c = str(r["Category"]) if pd.notna(r.get("Category")) else None
            mask = df["url"] == r["URL_n"]
            if mask.any():
                if t in ("T0","T1P","T1","T2","T3","T4"):
                    df.loc[mask, "tier"] = t
                if c:
                    df.loc[mask, "category"] = c
                n += 1
        print(f"  Overrides applied: {n}")

    targets = grammar["tier_inbound_targets"]
    df["target_inlinks"] = df["tier"].map(lambda t: targets.get(t, {}).get("target_min", 5))
    df["deficiency"] = (df["target_inlinks"] - df["current_inlinks"]).clip(lower=0).astype(int)
    df = df.sort_values("priority_score", ascending=False).reset_index(drop=True)
    df.insert(0, "rank", range(1, len(df) + 1))

    print("\nTier distribution:")
    print(df["tier"].value_counts().reindex(["T1P","T1","T2","T3","T4"]).fillna(0).astype(int))
    print(f"\nDeficiency to close: {df['deficiency'].sum():,} inbound links")

    if not a.apply:
        print(f"\nDRY-RUN. Pass --apply to write {a.output}")
        return
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(a.output, index=False)
    print(f"✓ Wrote {a.output}")

if __name__ == "__main__":
    main()

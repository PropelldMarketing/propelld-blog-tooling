# propelld-blog-tooling

Tooling for the Propelld blog architecture + internal linking rebuild.

## What's in here

| Folder | Purpose |
|---|---|
| `scripts/` | The 8 Python scripts that power the rebuild (see Scripts table below) |
| `lib/` | Shared Python modules — Webflow client, GSC client, anchor library, snapshots |
| `data/` | Tier overrides, pillar shortlists, category grammar rules, anchor library |
| `.github/workflows/` | GitHub Actions for nightly, weekly, and manual runs |

## Scripts

| Script | What it does | Run when |
|---|---|---|
| `refresh_hub_data.py` | Recomputes `post-count`, `companion-facets`, `related-articles` for every CMS item | Nightly (cron) |
| `publish_facet_items.py` | Bulk-publishes all 70 draft facet items (Courses, Lenders, Regions) plus the 14 Exams drafts | Manual, once templates are live |
| `check_facet_content.py` | Lists which facet items are missing `hub-intro`, meta-title, meta-description, featured image | Manual, before publish |
| `audit_internal_links.py` | Crawls Screaming Frog export, classifies every internal link, outputs kill/rewrite/keep list | Manual, Phase 2 of linking rebuild |
| `priority_scorer.py` | Computes 6-input tier score for every blog post | Manual, before Phase 2 |
| `link_recommender.py` | Generates per-post link recommendations CSV | Manual, Phase 3 |
| `bulk_apply_links.py` | Bulk-PATCHes Webflow with new internal links | Manual, Phase 5 (with snapshots) |
| `link_health_report.py` | Weekly metrics: Gini coefficient, tier distribution, orphan count, validation overlap | Weekly (cron) |
| `rollback.py` | Restores blog post HTML from snapshot | Emergency only |

## Setup

Required Python 3.8+. Install dependencies:

```bash
pip install -r requirements.txt
```

Required environment variables:

```bash
WEBFLOW_API_TOKEN=...     # Webflow Data API token with CMS read+write + Sites read
AHREFS_API_TOKEN=...      # Optional, for richer scoring inputs
S3_BUCKET=...             # For HTML snapshots (or use GCS_BUCKET)
SLACK_WEBHOOK=...         # Optional, for failure alerts
```

Set these as GitHub Actions secrets for scheduled runs. See `.github/workflows/` for usage.

## GitHub Actions

| Workflow | Schedule | What it runs |
|---|---|---|
| `nightly.yml` | Daily at 02:00 IST | `refresh_hub_data.py` + `refresh_links.py` |
| `weekly-health.yml` | Monday 04:00 IST | `link_health_report.py` |
| `manual-dispatch.yml` | On demand via Actions UI | Any script chosen via workflow input |

## Safety rules

All scripts default to **dry-run**. They require `--apply` to actually write changes. They snapshot every modified item to `S3_BUCKET` before any PATCH. They're idempotent — re-running on already-processed items is a no-op.

## Reference

See `propelld-blog-internal-linking-strategy-v3.docx` for the full strategy.

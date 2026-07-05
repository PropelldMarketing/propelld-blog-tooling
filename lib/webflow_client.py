"""
Webflow API client — thin wrapper around the Webflow Data API v2.

Handles: pagination, rate limiting, both blog body fields
(post-body + post-body-2nd-half), and idempotent PATCH.

Reads WEBFLOW_API_TOKEN from env.
"""

import os
import time
import requests

API_BASE = "https://api.webflow.com/v2"

# From audit (see project-state-snapshot.md §3):
SITE_ID = "63a98d7ca3749777345ba1fd"
COLLECTIONS = {
    "blog_posts":      "65a121c8dfbfd2af6bac3f2e",
    "hindi_blogs":     "666830a340bbdfbf83373721",
    "tamil_blogs":     "66b34a4fd14b9b57a2dad6d0",
    "categories":      "65a121c8dfbfd2af6bac3e46",
    "courses":         "6a0d870f601c9d1e423a0bd2",
    "lenders":         "6a0d8710807b4792f70f774a",
    "regions":         "6a0d8718faae524c59d4df50",
    "exams_accepted":  "66de84c04907156359f7fa2d",
}

# Body fields on Blog Posts collection — links can live in EITHER field
BLOG_BODY_FIELDS = ["post-body", "post-body-2nd-half"]

# Additional fields that hold content but are treated differently
BLOG_FAQ_FIELD = "faqs"


class WebflowClient:
    def __init__(self, token=None, throttle_per_sec=5):
        self.token = token or os.environ.get("WEBFLOW_API_TOKEN")
        if not self.token:
            raise RuntimeError("WEBFLOW_API_TOKEN not set")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self.throttle = 1.0 / max(throttle_per_sec, 1)
        self._last_call = 0.0

    def _wait(self):
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.throttle:
            time.sleep(self.throttle - elapsed)
        self._last_call = time.monotonic()

    def _request(self, method, path, **kwargs):
        self._wait()
        url = f"{API_BASE}{path}"
        for attempt in range(5):
            r = self.session.request(method, url, timeout=60, **kwargs)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 2 ** attempt))
                time.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json() if r.text else None
        r.raise_for_status()

    # ---------- Items ----------
    def list_items(self, collection_id, limit=100):
        offset = 0
        while True:
            data = self._request("GET", f"/collections/{collection_id}/items",
                                 params={"limit": limit, "offset": offset})
            for item in data.get("items", []):
                yield item
            total = data.get("pagination", {}).get("total", 0)
            offset += limit
            if offset >= total:
                break

    def get_item(self, collection_id, item_id):
        return self._request("GET", f"/collections/{collection_id}/items/{item_id}")

    def update_item(self, collection_id, item_id, field_data):
        """PATCH item as draft. Only send fields that changed."""
        return self._request("PATCH", f"/collections/{collection_id}/items/{item_id}",
                             json={"fieldData": field_data})

    def publish_items(self, collection_id, item_ids, chunk=100):
        results = []
        for i in range(0, len(item_ids), chunk):
            batch = item_ids[i:i + chunk]
            results.append(self._request(
                "POST", f"/collections/{collection_id}/items/publish",
                json={"itemIds": batch}))
        return results


def get_blog_body(item):
    """Return the combined body HTML (both halves) for a blog post item."""
    fd = item.get("fieldData", {})
    return {
        "post-body": fd.get("post-body", "") or "",
        "post-body-2nd-half": fd.get("post-body-2nd-half", "") or "",
    }


def set_blog_body(existing_field_data, new_bodies):
    """Merge new body HTML into a fieldData patch, preserving other fields."""
    patch = {}
    for k in BLOG_BODY_FIELDS:
        if k in new_bodies:
            patch[k] = new_bodies[k]
    return patch

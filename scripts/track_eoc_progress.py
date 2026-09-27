#!/usr/bin/env python3
"""Print a one-shot EOC ingest progress report for tracking."""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime

POLICY_ID = "46fa523c-af06-4295-99c2-1a795652e7aa"
URL = f"http://127.0.0.1:8000/policies/{POLICY_ID}"


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=20) as resp:
            d = json.load(resp)
    except Exception as exc:
        print(f"ERROR: could not reach API ({exc})")
        return 1

    ep = d.get("episodes") or []
    pages = (d.get("sections") or {}).get("pages") or list(range(50, 133))
    counts = d.get("item_counts") or {}

    rec: list[tuple[int, str]] = []
    for e in ep:
        s = e.get("summary") or ""
        if e.get("status") == "completed" and (
            "Recovered missed lines on page" in s or "Read chart rows on page" in s
        ):
            m = re.search(r"page (\d+)", s)
            if m:
                rec.append((int(m.group(1)), e.get("created_at") or ""))

    cur = cur_ts = cur_sum = st = None
    for e in reversed(ep):
        if e.get("stage") != "extract":
            continue
        m = re.search(r"page (\d+)", e.get("summary") or "")
        if not m:
            # still in group extract?
            m2 = re.search(r"group (\d+) of (\d+)", e.get("summary") or "")
            if m2:
                print("=== 5-MIN UPDATE ===")
                print(f"status: {d.get('status')} | running: {d.get('ingestion_running')}")
                print(f"phase: main extract group {m2.group(1)}/{m2.group(2)}")
                print(f"ROWS:  {counts}")
                print("latest:", e.get("status"), e.get("summary"))
                return 0
            continue
        cur = int(m.group(1))
        cur_ts = e.get("created_at")
        cur_sum = e.get("summary")
        st = e.get("status")
        break

    status = d.get("status")
    if status == "draft":
        print("=== 5-MIN UPDATE ===")
        print("status: draft | DONE")
        print("phase: finished")
        print(f"ROWS:  {counts.get('total')} total | {counts.get('auto_approved')} auto | {counts.get('pending_review')} review")
        print("ETA:   0 min — ready for human review / Go Live")
        return 0

    if cur is None:
        print("=== 5-MIN UPDATE ===")
        print(f"status: {status} | running: {d.get('ingestion_running')}")
        print(f"ROWS:  {counts}")
        if ep:
            print("latest:", ep[-1].get("status"), ep[-1].get("summary"))
        return 0

    idx = pages.index(cur) if cur in pages else max(0, len(rec) - 1)
    left = max(0, len(pages) - idx - 1)
    done = idx + 1
    per = 60.0
    if len(rec) >= 2:
        t0 = datetime.fromisoformat(rec[0][1].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(rec[-1][1].replace("Z", "+00:00"))
        per = max(20.0, (t1 - t0).total_seconds() / max(1, len(rec) - 1))
    eta = per * left / 60.0
    pct = 100.0 * done / len(pages)

    print("=== 5-MIN UPDATE ===")
    print(f"status: {status} | running: {d.get('ingestion_running')}")
    print("phase: cleanup — recover missed benefit lines")
    print("DONE:  main extract 83/83 groups")
    print(f"DONE:  cleanup through page {cur}  ({done}/{len(pages)} chart pages, {pct:.0f}%)")
    print(f"LEFT:  ~{left} chart pages (then short grid-fill → draft)")
    print(
        f"ROWS:  {counts.get('total')} total | "
        f"{counts.get('auto_approved')} auto | "
        f"{counts.get('pending_review')} review"
    )
    print(f"PACE:  ~{per:.0f}s per cleanup page")
    print(f"ETA:   ~{eta:.0f} minutes remaining")
    print(f"latest: {st} {cur_sum}")
    print(f"@ {cur_ts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Gumroad auto-list status packager (no network).

Reads:
  - PRODUCTS/GUMROAD_AUTOLIST/catalog.json
  - LEDGER/GUMROAD_PRODUCTS.csv (if present)

Writes:
  - output/ecom_autolist/latest.md
  - output/ecom_autolist/latest.html
  - output/ecom_autolist/manifest.json
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple


BASE = Path(__file__).resolve().parent.parent
CATALOG = BASE / "PRODUCTS" / "GUMROAD_AUTOLIST" / "catalog.json"
LEDGER = BASE / "LEDGER" / "GUMROAD_PRODUCTS.csv"

OUT = BASE / "output" / "ecom_autolist"
OUT_MD = OUT / "latest.md"
OUT_HTML = OUT / "latest.html"
OUT_MANIFEST = OUT / "manifest.json"


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_catalog_items(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sku = str(it.get("sku") or "").strip()
        name = str(it.get("name") or "").strip()
        rel = str(it.get("file_relpath") or "").strip()
        price_cents = int(it.get("price_cents") or 0)
        publish = bool(it.get("publish") is True)
        if not sku or not name:
            continue
        out.append(
            {
                "sku": sku,
                "name": name,
                "file_relpath": rel,
                "price_cents": max(0, price_cents),
                "publish": publish,
            }
        )
    return out


def load_latest_ledger_by_sku(path: Path) -> Tuple[Dict[str, Dict[str, str]], str]:
    by_sku: Dict[str, Dict[str, str]] = {}
    last_ts = ""
    if not path.exists():
        return by_sku, last_ts
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sku = (row.get("sku") or "").strip()
                ts = (row.get("timestamp") or "").strip()
                if sku:
                    by_sku[sku] = {k: (v or "").strip() for k, v in row.items()}
                if ts:
                    last_ts = ts
    except Exception:
        return by_sku, last_ts
    return by_sku, last_ts


def build_rows(items: List[Dict[str, Any]], ledger_by_sku: Dict[str, Dict[str, str]]) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    live = created = failed = pending = 0
    rows: List[Dict[str, str]] = []
    for it in items:
        sku = it["sku"]
        r = ledger_by_sku.get(sku, {})
        status = (r.get("status") or "PENDING").strip().upper()
        pid = (r.get("gumroad_product_id") or "").strip()
        short_url = (r.get("short_url") or "").strip()
        if status == "LIVE":
            live += 1
        elif status == "CREATED":
            created += 1
        elif status in {"FAILED"}:
            failed += 1
        else:
            pending += 1
        rows.append(
            {
                "sku": sku,
                "name": str(it.get("name") or ""),
                "price": f"${(int(it.get('price_cents') or 0) / 100):.2f}",
                "publish": "YES" if bool(it.get("publish")) else "NO",
                "status": status,
                "product_id": pid,
                "short_url": short_url,
            }
        )
    return rows, {"live": live, "created": created, "failed": failed, "pending": pending}


def render_md(items: List[Dict[str, Any]], ledger_by_sku: Dict[str, Dict[str, str]], last_ts: str) -> Tuple[str, Dict[str, int]]:
    rows, summary = build_rows(items, ledger_by_sku)

    lines: List[str] = []
    lines.append("# Ecom Auto-List (Gumroad)")
    lines.append("")
    lines.append(f"Generated: {now_iso()}")
    lines.append(f"Ledger last event: {last_ts or 'n/a'}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Catalog items: {len(items)}")
    lines.append(f"- LIVE: {summary.get('live', 0)}")
    lines.append(f"- CREATED: {summary.get('created', 0)}")
    lines.append(f"- FAILED: {summary.get('failed', 0)}")
    lines.append(f"- PENDING: {summary.get('pending', 0)}")
    lines.append("")
    lines.append("## Items")
    lines.append("")
    lines.append("| SKU | Name | Price | Publish | Status | Short URL | Product ID |")
    lines.append("|---|---|---:|:---:|:---:|---|---|")
    for r in rows:
        lines.append(
            f"| {r['sku']} | {r['name']} | {r['price']} | {r['publish']} | {r['status']} | {r['short_url']} | {r['product_id']} |"
        )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n", summary


def write_html(md_text: str) -> None:
    escaped = md_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>PRINTMAXX Gumroad Auto-List</title>
  <style>
    body {{ margin: 0; background: #0b0b0b; color: #eaeaea; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }}
    .wrap {{ max-width: 1100px; margin: 0 auto; padding: 18px; }}
    pre {{ white-space: pre-wrap; line-height: 1.5; font-size: 13px; }}
    a {{ color: #00aaff; }}
  </style>
</head>
<body>
  <div class="wrap"><pre>{escaped}</pre></div>
</body>
</html>
"""
    OUT_HTML.write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    if not args.write:
        ap.print_help()
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    items = load_catalog_items(CATALOG)
    ledger_by_sku, last_ts = load_latest_ledger_by_sku(LEDGER)
    md, summary = render_md(items, ledger_by_sku, last_ts)
    OUT_MD.write_text(md, encoding="utf-8")
    write_html(md)
    OUT_MANIFEST.write_text(
        json.dumps(
            {
                "generated_at": now_iso(),
                "catalog": str(CATALOG),
                "ledger": str(LEDGER),
                "catalog_count": len(items),
                "live_count": int(summary.get("live", 0)),
                "created_count": int(summary.get("created", 0)),
                "failed_count": int(summary.get("failed", 0)),
                "pending_count": int(summary.get("pending", 0)),
                "ledger_last_event": last_ts,
            },
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"gumroad_autolist_packager: wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

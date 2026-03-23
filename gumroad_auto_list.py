#!/usr/bin/env python3
"""
Gumroad Auto-Lister (API-based, non-interactive)
================================================
Creates and publishes Gumroad products from a local catalog + local files.

Design goals:
  - Non-interactive: safe to run from Ship Captain on a worker node.
  - Truth-first logging: writes an idempotent ledger of what happened.
  - Minimal deps: stdlib only.

Requires:
  - GUMROAD_ACCESS_TOKEN (env var OR SECRETS/PAYMENT_INFO.md)

Catalog default:
  - PRODUCTS/GUMROAD_AUTOLIST/catalog.json (seed PDFs ready-to-sell)

Outputs:
  - LEDGER/GUMROAD_PRODUCTS.csv
  - output/ecom_autolist/latest.md + latest.html + manifest.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


BASE = Path(__file__).resolve().parent.parent
SECRETS_FILE = BASE / "SECRETS" / "PAYMENT_INFO.md"
DEFAULT_CATALOG = BASE / "PRODUCTS" / "GUMROAD_AUTOLIST" / "catalog.json"

LEDGER_DIR = BASE / "LEDGER"
LEDGER_DIR.mkdir(parents=True, exist_ok=True)
LEDGER_CSV = LEDGER_DIR / "GUMROAD_PRODUCTS.csv"

OUT_DIR = BASE / "output" / "ecom_autolist"
OUT_MD = OUT_DIR / "latest.md"
OUT_HTML = OUT_DIR / "latest.html"
OUT_MANIFEST = OUT_DIR / "manifest.json"

API_BASE = "https://api.gumroad.com"


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_secrets_kv(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    out: Dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            key = k.strip().upper()
            val = v.strip()
            if key:
                out[key] = val
    except Exception:
        return {}
    return out


def gumroad_token() -> str:
    tok = (os.environ.get("GUMROAD_ACCESS_TOKEN") or "").strip()
    if tok:
        return tok
    secrets = _read_secrets_kv(SECRETS_FILE)
    return (secrets.get("GUMROAD_ACCESS_TOKEN") or "").strip()


def ensure_ledger() -> None:
    if LEDGER_CSV.exists():
        return
    with open(LEDGER_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "timestamp",
                "sku",
                "name",
                "status",
                "gumroad_product_id",
                "short_url",
                "notes",
            ]
        )


def load_existing_by_sku() -> Dict[str, Dict[str, str]]:
    if not LEDGER_CSV.exists():
        return {}
    out: Dict[str, Dict[str, str]] = {}
    try:
        with open(LEDGER_CSV, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sku = (row.get("sku") or "").strip()
                if sku:
                    out[sku] = row
    except Exception:
        return out
    return out


def append_ledger(
    sku: str,
    name: str,
    status: str,
    product_id: str = "",
    short_url: str = "",
    notes: str = "",
) -> None:
    ensure_ledger()
    with open(LEDGER_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([now_iso(), sku, name, status, product_id, short_url, notes[:800]])


def http_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    data_bytes: Optional[bytes] = None,
    timeout: int = 60,
) -> Tuple[bool, Dict]:
    if headers is None:
        headers = {}
    headers = dict(headers)
    headers.setdefault("User-Agent", "PRINTMAXX-Gumroad-Autolister/1.0")
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            return False, {"error": "non-dict json response"}
        return True, payload
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return False, {"error": f"http_{e.code}", "body": body[:2000]}
    except Exception as e:
        return False, {"error": str(e)}


def form_urlencode(fields: Dict[str, str]) -> bytes:
    return urllib.parse.urlencode(fields).encode("utf-8")


def multipart_encode(fields: Dict[str, str], file_field: str, file_path: Path) -> Tuple[bytes, str]:
    boundary = f"----printmaxx-{int(time.time() * 1000)}"
    crlf = "\r\n"

    parts: List[bytes] = []
    for k, v in fields.items():
        parts.append(f"--{boundary}{crlf}".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{k}"{crlf}{crlf}'.encode("utf-8"))
        parts.append((v or "").encode("utf-8"))
        parts.append(crlf.encode("utf-8"))

    filename = file_path.name
    file_bytes = file_path.read_bytes()
    parts.append(f"--{boundary}{crlf}".encode("utf-8"))
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"{crlf}'.encode("utf-8")
    )
    parts.append(f"Content-Type: application/octet-stream{crlf}{crlf}".encode("utf-8"))
    parts.append(file_bytes)
    parts.append(crlf.encode("utf-8"))
    parts.append(f"--{boundary}--{crlf}".encode("utf-8"))

    body = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


@dataclass(frozen=True)
class CatalogItem:
    sku: str
    name: str
    price_cents: int
    description_md: str
    file_relpath: str
    publish: bool
    tags: List[str]


def load_catalog(path: Path) -> List[CatalogItem]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("catalog must be a JSON object")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("catalog.items must be a list")
    out: List[CatalogItem] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        sku = str(raw.get("sku") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not sku or not name:
            continue
        price_cents = int(raw.get("price_cents") or 0)
        desc = str(raw.get("description_md") or "").strip()
        rel = str(raw.get("file_relpath") or "").strip()
        publish = bool(raw.get("publish") is True)
        tags_raw = raw.get("tags") if isinstance(raw.get("tags"), list) else []
        tags: List[str] = []
        for t in tags_raw:
            s = str(t or "").strip()
            if s:
                tags.append(s)
        out.append(
            CatalogItem(
                sku=sku,
                name=name,
                price_cents=max(0, price_cents),
                description_md=desc,
                file_relpath=rel,
                publish=publish,
                tags=tags,
            )
        )
    return out


def create_product(token: str, item: CatalogItem) -> Tuple[bool, str, str, str]:
    """
    Returns: (ok, product_id, short_url, notes)
    """
    url = f"{API_BASE}/v2/products"
    fields = {
        "access_token": token,
        "name": item.name,
        "price": str(item.price_cents),
        "description": item.description_md,
    }
    ok, payload = http_json("POST", url, headers={"Content-Type": "application/x-www-form-urlencoded"}, data_bytes=form_urlencode(fields), timeout=90)
    if not ok or not payload.get("success"):
        return False, "", "", f"create_failed: {payload.get('error') or payload.get('body') or 'unknown'}"
    prod = payload.get("product") if isinstance(payload.get("product"), dict) else {}
    pid = str(prod.get("id") or "").strip()
    short_url = str(prod.get("short_url") or "").strip()
    if not pid:
        return False, "", "", "create_failed: missing product.id"
    return True, pid, short_url, "created"


def upload_file(token: str, product_id: str, file_path: Path) -> Tuple[bool, str]:
    url = f"{API_BASE}/v2/products/{urllib.parse.quote(product_id)}/files"
    fields = {"access_token": token}
    body, ctype = multipart_encode(fields, "file", file_path)
    ok, payload = http_json("POST", url, headers={"Content-Type": ctype}, data_bytes=body, timeout=240)
    if not ok or not payload.get("success"):
        return False, f"upload_failed: {payload.get('error') or payload.get('body') or 'unknown'}"
    return True, "uploaded"


def enable_product(token: str, product_id: str) -> Tuple[bool, str]:
    url = f"{API_BASE}/v2/products/{urllib.parse.quote(product_id)}/enable"
    fields = {"access_token": token}
    ok, payload = http_json(
        "PUT",
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data_bytes=form_urlencode(fields),
        timeout=90,
    )
    if not ok or not payload.get("success"):
        return False, f"enable_failed: {payload.get('error') or payload.get('body') or 'unknown'}"
    return True, "enabled"


def render_report(rows: List[Dict[str, str]]) -> str:
    lines: List[str] = []
    lines.append("# Gumroad Auto-List Report")
    lines.append("")
    lines.append(f"Generated: {now_iso()}")
    lines.append("")
    if not rows:
        lines.append("- No ledger rows yet.")
        return "\n".join(lines).rstrip() + "\n"

    # Tail view: last 30 actions
    tail = rows[-30:]
    lines.append("## Recent Actions (tail)")
    lines.append("")
    for r in reversed(tail):
        sku = (r.get("sku") or "").strip()
        status = (r.get("status") or "").strip()
        pid = (r.get("gumroad_product_id") or "").strip()
        short_url = (r.get("short_url") or "").strip()
        notes = (r.get("notes") or "").strip()
        lines.append(f"- {sku} | {status} | {pid} | {short_url}")
        if notes:
            lines.append(f"  - {notes[:220]}")

    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_html(md_text: str) -> None:
    escaped = md_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Gumroad Auto-List</title>
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


def load_ledger_rows(max_rows: int = 5000) -> List[Dict[str, str]]:
    if not LEDGER_CSV.exists():
        return []
    rows: List[Dict[str, str]] = []
    try:
        with open(LEDGER_CSV, "r", newline="", encoding="utf-8") as f:
            for i, row in enumerate(csv.DictReader(f)):
                if i >= max_rows:
                    break
                rows.append({k: (v or "") for k, v in row.items()})
    except Exception:
        return []
    return rows


def write_outputs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = load_ledger_rows()
    md = render_report(rows)
    OUT_MD.write_text(md, encoding="utf-8")
    write_html(md)
    OUT_MANIFEST.write_text(
        json.dumps(
            {
                "generated_at": now_iso(),
                "ledger": str(LEDGER_CSV),
                "count_rows": len(rows),
                "latest_md": str(OUT_MD),
                "latest_html": str(OUT_HTML),
            },
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    ap.add_argument("--apply", action="store_true", help="Actually create/upload/enable products")
    ap.add_argument("--max", type=int, default=10, help="Max items to process")
    ap.add_argument("--force", action="store_true", help="Re-run even if SKU already in ledger")
    args = ap.parse_args()

    token = gumroad_token()
    if not token:
        print("gumroad_auto_list: missing GUMROAD_ACCESS_TOKEN (env or SECRETS/PAYMENT_INFO.md)")
        return 2

    try:
        catalog = load_catalog(Path(args.catalog))
    except Exception as e:
        print(f"gumroad_auto_list: failed to load catalog: {e}")
        return 2

    ensure_ledger()
    existing = load_existing_by_sku()

    processed = 0
    created = 0
    failed = 0
    skipped = 0

    for item in catalog:
        if processed >= max(1, int(args.max)):
            break
        if (not args.force) and item.sku in existing:
            skipped += 1
            continue

        file_path = (BASE / item.file_relpath).resolve()
        if not file_path.exists():
            failed += 1
            append_ledger(item.sku, item.name, "FAILED", notes=f"missing file: {item.file_relpath}")
            processed += 1
            continue

        processed += 1
        if not args.apply:
            append_ledger(item.sku, item.name, "DRY_RUN", notes=f"would create+upload+{'enable' if item.publish else 'leave disabled'}")
            continue

        ok, pid, short_url, notes = create_product(token, item)
        if not ok:
            failed += 1
            append_ledger(item.sku, item.name, "FAILED", notes=notes)
            continue

        ok2, notes2 = upload_file(token, pid, file_path)
        if not ok2:
            failed += 1
            append_ledger(item.sku, item.name, "FAILED", product_id=pid, short_url=short_url, notes=notes2)
            continue

        if item.publish:
            ok3, notes3 = enable_product(token, pid)
            if not ok3:
                failed += 1
                append_ledger(item.sku, item.name, "FAILED", product_id=pid, short_url=short_url, notes=notes3)
                continue

        created += 1
        append_ledger(item.sku, item.name, "LIVE" if item.publish else "CREATED", product_id=pid, short_url=short_url, notes="ok")

    write_outputs()
    print(f"gumroad_auto_list: processed={processed} created={created} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())


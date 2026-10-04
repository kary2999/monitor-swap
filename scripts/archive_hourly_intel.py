#!/usr/bin/env python3
"""Archive one hourly intelligence report to Feishu Docx + Bitable.

The Docx stores full details and original links. The 资料库 table stores an
idempotent index record pointing to that document. Secrets are read from the
dedicated FEISHU_KB_* environment only and are never printed.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
ENV_PATH = HOME / ".env"
SETUP_PATH = HOME / "feishu_hermes_kb_setup.json"
VERIFIED_PATH = HOME / "feishu_hermes_kb_verified.json"
INDEX_PATH = HOME / "hourly-intel" / "archive-index.json"


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    values.update(os.environ)
    return values


def api_host(domain: str) -> str:
    return "https://open.larksuite.com" if "lark" in domain.lower() else "https://open.feishu.cn"


def request_json(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"code": -1, "msg": "non-JSON HTTP error"}
        return exc.code, payload


def require_ok(stage: str, response: tuple[int, dict[str, Any]]) -> dict[str, Any]:
    status, payload = response
    if not 200 <= status < 300 or payload.get("code", 0) != 0:
        safe = {"stage": stage, "http_status": status, "code": payload.get("code"), "msg": payload.get("msg")}
        raise RuntimeError(json.dumps(safe, ensure_ascii=False))
    return payload


def text_block(content: str, block_type: int = 2) -> dict[str, Any]:
    key = {2: "text", 3: "heading1", 4: "heading2", 5: "heading3"}[block_type]
    return {
        "block_type": block_type,
        key: {
            "elements": [{"text_run": {"content": str(content)}}],
            "style": {},
        },
    }


def report_blocks(report: dict[str, Any]) -> list[dict[str, Any]]:
    links = [str(url) for url in report.get("original_urls", []) if url]
    blocks: list[dict[str, Any]] = [
        text_block("原始链接", 3),
        *[text_block(url) for url in links],
        text_block("结论", 3),
        text_block(report.get("summary", "")),
    ]
    markets = list(report.get("market_snapshot") or [])
    if markets:
        blocks.append(text_block("固定行情｜多源中位公允价", 3))
        for market in markets:
            change = market.get("change_24h_pct")
            change_text = "暂无" if change is None else f"{float(change):+.2f}%"
            blocks.append(text_block(
                f"{market.get('pair')}｜{market.get('fair_price')}｜24h {change_text}"
            ))
    blocks.append(text_block("相关新闻详情", 3))
    for item in report.get("items", []):
        blocks.extend(
            [
                text_block(f"{item.get('rank', '')}｜{item.get('title', '')}", 4),
                text_block(f"是什么：{item.get('what') or item.get('body', '')}"),
                text_block(f"解决什么：{item.get('problem', '')}"),
                text_block(f"怎么做到：{item.get('mechanism', '')}"),
                text_block(f"谁在做：{item.get('who', '')}"),
                text_block(f"当前阶段：{item.get('maturity', '')}"),
                text_block(f"与你的关系：{item.get('relevance', '')}"),
                text_block(f"来源：{item.get('url') or item.get('source', '')}"),
            ]
        )
    blocks.extend(
        [
            text_block("本小时动作", 3),
            text_block(report.get("action", "")),
            text_block(report.get("footer", "")),
        ]
    )
    return blocks


def load_index() -> dict[str, Any]:
    if not INDEX_PATH.exists():
        return {"reports": {}}
    try:
        parsed = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"reports": {}}
    if not isinstance(parsed, dict):
        return {"reports": {}}
    parsed.setdefault("reports", {})
    return parsed


def save_index(index: dict[str, Any]) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = INDEX_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, INDEX_PATH)
    os.chmod(INDEX_PATH, 0o600)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: archive_hourly_intel.py REPORT.json")
    report_path = Path(sys.argv[1]).expanduser().resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_id = str(report.get("report_id") or report_path.stem)
    title = str(report.get("doc_title") or f"[AI Agent 情报] {report.get('title', report_id)}").replace("\n", " ")

    env = load_env()
    app_id = env.get("FEISHU_KB_APP_ID") or env.get("LARK_KB_APP_ID")
    app_secret = env.get("FEISHU_KB_APP_SECRET") or env.get("LARK_KB_APP_SECRET")
    domain = env.get("FEISHU_KB_DOMAIN") or env.get("LARK_KB_DOMAIN") or "lark"
    if not app_id or not app_secret:
        raise SystemExit("missing dedicated FEISHU_KB_APP_ID/FEISHU_KB_APP_SECRET")

    setup = json.loads(SETUP_PATH.read_text(encoding="utf-8"))
    verified = json.loads(VERIFIED_PATH.read_text(encoding="utf-8"))
    app_token = setup["resources"]["bitable"]["app_token"]
    table = next(item for item in verified["bitable"]["tables"] if item["table"] == "资料库")
    table_id = table["table_id"]
    base = api_host(domain)

    auth = require_ok(
        "tenant_access_token",
        request_json(
            "POST",
            f"{base}/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": app_id, "app_secret": app_secret},
        ),
    )
    token = auth["tenant_access_token"]
    index = load_index()
    entry = index["reports"].get(report_id, {})

    doc_id = entry.get("document_id")
    created_doc = False
    if not doc_id:
        created = require_ok(
            "create_document",
            request_json("POST", f"{base}/open-apis/docx/v1/documents", {"title": title}, token),
        )
        doc_id = created["data"]["document"]["document_id"]
        entry["document_id"] = doc_id
        index["reports"][report_id] = entry
        save_index(index)
        created_doc = True

    blocks_url = f"{base}/open-apis/docx/v1/documents/{doc_id}/blocks"
    existing = require_ok("read_document_before_write", request_json("GET", blocks_url, token=token))
    existing_items = existing.get("data", {}).get("items", [])
    child_count = max(0, len(existing_items) - 1)
    if child_count:
        delete_url = f"{base}/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children/batch_delete?document_revision_id=-1"
        require_ok(
            "clear_document",
            request_json("DELETE", delete_url, {"start_index": 0, "end_index": child_count}, token),
        )

    blocks = report_blocks(report)
    append_url = f"{base}/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children?document_revision_id=-1"
    for start in range(0, len(blocks), 50):
        require_ok(
            "write_document",
            request_json("POST", append_url, {"children": blocks[start : start + 50], "index": -1}, token),
        )

    verified_doc = require_ok("verify_document", request_json("GET", blocks_url, token=token))
    block_count = len(verified_doc.get("data", {}).get("items", []))
    if block_count < len(blocks) + 1:
        raise RuntimeError(f"verify_document: expected at least {len(blocks) + 1} blocks, got {block_count}")

    doc_host = "www.larksuite.com" if "larksuite" in base else "www.feishu.cn"
    doc_url = f"https://{doc_host}/docx/{doc_id}"
    archive_type = str(report.get("archive_type") or "AI Agent 小时情报")
    archive_tags = str(report.get("archive_tags") or "AI Agent, MCP, CLI, 自动化, 开源项目")
    fields = {
        "标题": title[:500],
        "类型": archive_type[:100],
        "链接": {"text": "查看完整云文档", "link": doc_url},
        "摘要": str(report.get("summary", ""))[:1000],
        "标签": archive_tags[:500],
        "状态": "已归档",
    }
    record_id = entry.get("record_id")
    records_base = f"{base}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    if record_id:
        require_ok("update_bitable_record", request_json("PUT", f"{records_base}/{record_id}", {"fields": fields}, token))
        created_record = False
    else:
        record = require_ok("create_bitable_record", request_json("POST", records_base, {"fields": fields}, token))
        record_id = record["data"]["record"]["record_id"]
        entry["record_id"] = record_id
        entry["document_id"] = doc_id
        entry["updated_at"] = int(time.time())
        index["reports"][report_id] = entry
        save_index(index)
        created_record = True

    checked = require_ok("verify_bitable_record", request_json("GET", f"{records_base}/{record_id}", token=token))
    checked_fields = checked.get("data", {}).get("record", {}).get("fields", {})
    if checked_fields.get("标题") != fields["标题"] or checked_fields.get("状态") != "已归档":
        raise RuntimeError("verify_bitable_record: persisted fields mismatch")

    print(
        json.dumps(
            {
                "archived": True,
                "report_id": report_id,
                "document_created": created_doc,
                "document_verified": True,
                "document_block_count": block_count,
                "record_created": created_record,
                "record_verified": True,
                "document_url": doc_url,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

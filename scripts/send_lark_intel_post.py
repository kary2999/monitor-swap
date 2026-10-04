#!/usr/bin/env python3
"""Send an interactive hourly-intelligence card to Lark and verify it."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
ARCHIVE_INDEX = HOME / "hourly-intel" / "archive-index.json"


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    path = HOME / ".env"
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    values.update(os.environ)
    return values


def latest_dm_chat_id() -> str | None:
    db = sqlite3.connect(HOME / "state.db")
    try:
        row = db.execute(
            "SELECT chat_id FROM sessions "
            "WHERE lower(source) = 'feishu' AND chat_type = 'dm' "
            "AND chat_id IS NOT NULL AND chat_id <> '' "
            "ORDER BY last_activity_at DESC LIMIT 1"
        ).fetchone()
        return str(row[0]) if row else None
    finally:
        db.close()


def latest_open_id() -> str | None:
    pattern = re.compile(r"sender=user:(ou_[A-Za-z0-9]+)")
    found = None
    logs = sorted((HOME / "logs").glob("gateway.log*"), key=lambda path: path.stat().st_mtime)
    for log in logs:
        for match in pattern.finditer(log.read_text(encoding="utf-8", errors="ignore")):
            found = match.group(1)
    return found


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
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"code": -1, "msg": "non-JSON HTTP error"}
        return exc.code, payload


def document_url(report_id: str) -> str:
    index = json.loads(ARCHIVE_INDEX.read_text(encoding="utf-8"))
    document_id = index["reports"][report_id]["document_id"]
    env = load_env()
    domain = (env.get("FEISHU_KB_DOMAIN") or env.get("LARK_KB_DOMAIN") or "lark").lower()
    host = "www.larksuite.com" if "lark" in domain else "www.feishu.cn"
    return f"https://{host}/docx/{document_id}"


def post_content(report: dict[str, Any], doc_url: str) -> str:
    items = list(report.get("items") or [])
    markets = list(report.get("market_snapshot") or [])
    if not 0 <= len(items) <= 10 or (not items and not markets):
        raise ValueError(f"expected market data or up to 10 items, got {len(items)} items")
    lines: list[list[dict[str, str]]] = [
        [{"tag": "text", "text": str(report.get("summary") or "本小时数字资产与金融要闻")}],
        [{"tag": "text", "text": ""}],
    ]
    if markets:
        lines.append([{"tag": "text", "text": "固定行情｜多源中位公允价"}])
        for market in markets:
            pair = str(market.get("pair") or "")
            price = float(market.get("fair_price") or 0)
            if pair in {"刷卡付CNY", "换汇付CNY"}:
                lines.append([{"tag": "text", "text": f"{pair}  ¥{price:,.0f}"}])
                continue
            if pair in {"刷卡净损失", "USDT买入溢价"}:
                lines.append([{"tag": "text", "text": f"{pair}  {price:+.2f}%"}])
                continue
            change = market.get("change_24h_pct")
            change_text = "暂无" if change is None else f"{float(change):+.2f}%"
            price_text = f"{price:,.2f}" if price >= 100 else f"{price:.6f}".rstrip("0").rstrip(".")
            lines.append([{"tag": "text", "text": f"{pair}  {price_text}  ｜24h {change_text}"}])
        lines.append([{"tag": "text", "text": ""}])
    if items:
        lines.append([{"tag": "text", "text": f"相关新闻｜{len(items)} 条"}])
    for item in items:
        rank = str(item.get("rank") or "").zfill(2)
        title = str(item.get("title") or "")
        body = str(item.get("body") or item.get("what") or "")
        source = str(item.get("source") or "")
        url = str(item.get("url") or "")
        lines.append([{"tag": "text", "text": f"{rank}｜{title}\n{body}\n来源：{source}  "}, {"tag": "a", "text": "原文", "href": url}])
        lines.append([{"tag": "text", "text": ""}])
    lines.append([{"tag": "a", "text": "查看完整分析与原始链接", "href": doc_url}])
    lines.append([{"tag": "text", "text": "\n仅供信息参考，不构成投资建议。"}])
    return json.dumps({"zh_cn": {"title": str(report.get("lark_title") or report.get("doc_title") or "每小时数字资产情报"), "content": lines}}, ensure_ascii=False)


def card_content(report: dict[str, Any], doc_url: str) -> str:
    items = list(report.get("items") or [])
    markets = list(report.get("market_snapshot") or [])
    if not 0 <= len(items) <= 10 or (not items and not markets):
        raise ValueError(f"expected market data or up to 10 items, got {len(items)} items")

    def cell(content: str, weight: int, align: str = "left") -> dict[str, Any]:
        return {
            "tag": "column",
            "width": "weighted",
            "weight": weight,
            "vertical_align": "center",
            "elements": [{"tag": "markdown", "content": content, "text_align": align}],
        }

    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": str(report.get("summary") or "本小时数字资产与金融要闻")},
    ]
    if markets:
        elements.extend([
            {"tag": "hr"},
            {"tag": "markdown", "content": "**固定行情｜多源中位公允价**"},
            {
                "tag": "column_set",
                "flex_mode": "none",
                "horizontal_spacing": "8px",
                "columns": [cell("**交易对**", 3), cell("**公允价格**", 3, "right"), cell("**24h**", 2, "right")],
            },
        ])
        for market in markets:
            pair = str(market.get("pair") or "")
            change = market.get("change_24h_pct")
            price = float(market.get("fair_price") or 0)
            # 信用卡口径指标：金额带 ¥，损失带颜色百分比
            if pair in {"刷卡付CNY", "换汇付CNY"}:
                price_text = f"¥{price:,.0f}"
                elements.append({
                    "tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
                    "columns": [cell(pair, 3), cell(price_text, 3, "right"), cell("", 2, "right")],
                })
                continue
            if pair == "刷卡净损失":
                color = "red" if price > 0 else "green"
                elements.append({
                    "tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
                    "columns": [cell(pair, 3), cell(f"<font color='{color}'>{price:+.2f}%</font>", 3, "right"), cell("", 2, "right")],
                })
                continue
            if pair == "USDT买入溢价":
                color = "red" if price >= 0 else "green"  # >0 USDT贵=刷卡吃亏(红)
                elements.append({
                    "tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
                    "columns": [cell(pair, 3), cell(f"<font color='{color}'>{price:+.2f}%</font>", 3, "right"), cell("", 2, "right")],
                })
                continue
            if change is None:
                change_text = "暂无"
            else:
                value = float(change)
                color = "green" if value >= 0 else "red"
                change_text = f"<font color='{color}'>{value:+.2f}%</font>"
            price_text = f"{price:,.2f}" if price >= 100 else f"{price:.6f}".rstrip("0").rstrip(".")
            elements.append({
                "tag": "column_set",
                "flex_mode": "none",
                "horizontal_spacing": "8px",
                "columns": [cell(pair, 3), cell(price_text, 3, "right"), cell(change_text, 2, "right")],
            })
    if items:
        elements.extend([{"tag": "hr"}, {"tag": "markdown", "content": f"**相关新闻｜{len(items)} 条**"}])
        for index, item in enumerate(items):
            rank = str(item.get("rank") or "").zfill(2)
            title = str(item.get("title") or "")
            body = str(item.get("body") or item.get("what") or "")
            source = str(item.get("source") or "")
            url = str(item.get("url") or "")
            elements.append({
                "tag": "markdown",
                "content": f"**{rank}｜[{title}]({url})**\n{body}\n<font color='grey'>来源：{source}</font>",
            })
            if index < len(items) - 1:
                elements.append({"tag": "hr"})
    elements.extend([
        {"tag": "hr"},
        {"tag": "markdown", "content": f"[查看完整分析与原始链接]({doc_url})"},
        {"tag": "markdown", "content": "<font color='grey'>仅供信息参考，不构成投资建议。</font>"},
    ])
    card = {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": str(report.get("lark_title") or report.get("doc_title") or "每小时数字资产情报")},
            "subtitle": {"tag": "plain_text", "content": "行情与权威资讯"},
            "template": "green",
        },
        "body": {"elements": elements},
    }
    return json.dumps(card, ensure_ascii=False)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: send_lark_intel_post.py REPORT.json")
    report = json.loads(Path(sys.argv[1]).expanduser().resolve().read_text(encoding="utf-8"))
    report_id = str(report.get("report_id") or "")
    doc_url = document_url(report_id)
    env = load_env()
    config = yaml.safe_load((HOME / "config.yaml").read_text(encoding="utf-8")) or {}
    feishu = (((config.get("gateway") or {}).get("platforms") or {}).get("feishu") or {})
    home = feishu.get("home_channel") or {}
    chat_id = latest_dm_chat_id() or home.get("chat_id") or env.get("FEISHU_HOME_CHANNEL")
    app_id = env.get("FEISHU_APP_ID")
    secret = env.get("FEISHU_APP_SECRET")
    domain = env.get("FEISHU_DOMAIN", "lark").lower()
    base = "https://open.larksuite.com" if "lark" in domain else "https://open.feishu.cn"
    if not report_id or not chat_id or not app_id or not secret:
        raise SystemExit("missing report id or configured Feishu DM credentials")

    status, auth = request_json(
        "POST",
        f"{base}/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": secret},
    )
    if status != 200 or auth.get("code", 0) != 0:
        raise SystemExit("Feishu authentication failed")
    token = auth["tenant_access_token"]
    content = card_content(report, doc_url)
    send_url = f"{base}/open-apis/im/v1/messages?" + urllib.parse.urlencode({"receive_id_type": "chat_id"})
    send_status, sent = request_json(
        "POST", send_url, {"receive_id": chat_id, "msg_type": "interactive", "content": content}, token
    )
    if sent.get("code") == 230002:
        open_id = latest_open_id()
        if open_id:
            send_url = f"{base}/open-apis/im/v1/messages?" + urllib.parse.urlencode({"receive_id_type": "open_id"})
            send_status, sent = request_json(
                "POST", send_url, {"receive_id": open_id, "msg_type": "interactive", "content": content}, token
            )
    message_id = sent.get("data", {}).get("message_id")
    if send_status != 200 or sent.get("code") != 0 or not message_id:
        safe = {"stage": "send", "http_status": send_status, "code": sent.get("code"), "msg": sent.get("msg")}
        raise RuntimeError(json.dumps(safe, ensure_ascii=False))
    checked_status, checked = request_json("GET", f"{base}/open-apis/im/v1/messages/{message_id}", token=token)
    items = checked.get("data", {}).get("items", [])
    verified = checked_status == 200 and checked.get("code") == 0 and any(item.get("msg_type") == "interactive" for item in items)
    print(json.dumps({"sent": True, "verified": verified, "msg_type": "interactive", "item_count": len(report.get("items") or [])}, ensure_ascii=False))
    return 0 if verified else 4


if __name__ == "__main__":
    raise SystemExit(main())
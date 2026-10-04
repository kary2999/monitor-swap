#!/usr/bin/env python3
"""Translate final crypto-news items with the configured chat model, without agent context."""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES_ROOT = HOME / "hermes-agent"
MODEL = "gpt-6-astra"
PROVIDER = "openai-codex"
JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

# 该脚本作为独立子进程运行，不继承 cron 环境的代理设置；
# LLM 走境外链路，无代理会直接超时，故在此显式注入。
PROXY_URL = os.environ.get("HERMES_HTTP_PROXY", "http://127.0.0.1:7890")
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.setdefault(_key, PROXY_URL)


def parse_json_array(text: str) -> list[dict[str, Any]]:
    match = JSON_FENCE_RE.search(text)
    candidate = match.group(1) if match else text
    start = candidate.find("[")
    end = candidate.rfind("]")
    if start < 0 or end < start:
        raise ValueError("LLM response does not contain a JSON array")
    payload = json.loads(candidate[start : end + 1])
    if not isinstance(payload, list):
        raise ValueError("LLM response must be a JSON array")
    return payload


def call_gpt55(items: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    sys.path.insert(0, str(HERMES_ROOT))
    sys.path.insert(0, str(HERMES_ROOT / "agent"))
    from agent.auxiliary_client import call_llm

    compact = [
        {"rank": item.get("rank"), "title": item.get("title", ""), "body": item.get("body", "")}
        for item in items
    ]
    prompt = (
        "把下面最终入选的数字资产与金融新闻翻译成自然、准确、简洁的简体中文。"
        "保留 BTC、ETH、ETF、DeFi、Web3 等通用缩写和专有名词；不得补充原文没有的事实、数字或判断。"
        "只输出 JSON 数组，不要 Markdown。每项严格包含 rank、title_zh、body_zh，rank 与输入一致。\n"
        + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    )
    response = call_llm(
        provider=PROVIDER,
        model=MODEL,
        api_mode="codex_responses",
        reasoning_config={"enabled": False},
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2500,
        timeout=90,
    )
    content = str(response.choices[0].message.content or "")
    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    return content, {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "reasoning_tokens": int(getattr(details, "reasoning_tokens", 0) or 0),
    }


def translate_report(
    report: dict[str, Any],
    *,
    call: Callable[[list[dict[str, Any]]], tuple[str, dict[str, int]]] = call_gpt55,
) -> dict[str, Any]:
    translated_report = copy.deepcopy(report)
    items = list(translated_report.get("items") or [])
    if not items:
        translated_report["llm_translation"] = {
            "provider": PROVIDER,
            "model": MODEL,
            "reasoning": "none",
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "item_count": 0,
        }
        return translated_report

    raw, usage = call(items)
    translations = parse_json_array(raw)
    by_rank = {int(item["rank"]): item for item in translations if isinstance(item, dict) and "rank" in item}
    if len(by_rank) != len(items):
        raise ValueError(f"expected {len(items)} translations, got {len(by_rank)}")

    for item in items:
        rank = int(item["rank"])
        translated = by_rank.get(rank) or {}
        title_zh = str(translated.get("title_zh") or "").strip()
        body_zh = str(translated.get("body_zh") or "").strip()
        if not title_zh or not body_zh:
            raise ValueError(f"translation for rank {rank} is incomplete")
        item["original_title"] = item.get("title", "")
        item["original_body"] = item.get("body", "")
        item["title"] = title_zh
        item["body"] = body_zh
        item["what"] = body_zh

    translated_report["llm_translation"] = {
        "provider": PROVIDER,
        "model": MODEL,
        "reasoning": "none",
        **usage,
        "item_count": len(items),
    }
    total = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    translated_report["summary"] = (
        f"固定行情 {len(translated_report.get('market_snapshot') or [])} 项（脚本）；"
        f"相关新闻 {len(items)} 条由 {MODEL} 批量中文化，无推理，消耗 {total} Token。"
    )
    translated_report["footer"] = (
        "行情：多源中位数公允价，脚本计算、零 Token｜"
        f"资讯：规则筛选后由 {MODEL} 一次批量中文化，推理关闭"
    )
    return translated_report


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp = Path(handle.name)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: translate_crypto_news.py REPORT.json")
    path = Path(sys.argv[1]).expanduser().resolve()
    report = json.loads(path.read_text(encoding="utf-8"))
    translated = translate_report(report)
    write_json(path, translated)
    status = translated["llm_translation"]
    print(json.dumps({"translated": True, **status}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

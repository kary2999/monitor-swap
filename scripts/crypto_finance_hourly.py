#!/usr/bin/env python3
"""Token-free hourly crypto/finance intelligence collector.

Fetches RSS/Atom feeds, ranks relevant entries deterministically, writes the
existing report JSON schema, archives it to Lark Docs/Bitable, sends a Lark
rich-text post, and commits deduplication state only after verified delivery.
No LLM or Hermes agent call is used.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import ssl
import statistics
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
OUTPUT_DIR = HOME / "hourly-intel" / "crypto-finance"
STATE_PATH = OUTPUT_DIR / "feed-state.json"
PYTHON = HOME / "hermes-agent" / "venv" / "bin" / "python"
ARCHIVER = HOME / "scripts" / "archive_hourly_intel.py"
SENDER = HOME / "scripts" / "send_lark_intel_post.py"
TRANSLATOR = HOME / "scripts" / "translate_crypto_news.py"
USER_AGENT = "HermesCryptoIntel/1.0 (+personal RSS reader)"

# 境外源（CoinDesk/LLM 等）在无代理时不可达；cron 环境下不会继承 shell 的代理变量，
# 因此在此显式注入本地代理，保证脚本独立可运行。
PROXY_URL = os.environ.get("HERMES_HTTP_PROXY", "http://127.0.0.1:7890")
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.setdefault(_key, PROXY_URL)

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr?symbols=%5B%22BTCUSDT%22,%22ETHUSDT%22%5D"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,tether&vs_currencies=usd,cny&include_24hr_change=true"
COINBASE_RATES_URL = "https://api.coinbase.com/v2/exchange-rates?currency={}"
BINANCE_P2P_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
ECB_FX_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
ER_API_URL = "https://open.er-api.com/v6/latest/JPY"

# 信用卡成本参数：VIP3 手续费 0.85% + ATM 提现 2%，综合最高 2.85%
CARD_FEE_RATE = 0.0285

FEEDS = (
    ("CoinDesk", "coindesk.com", 78, "https://www.coindesk.com/arc/outboundfeeds/rss"),
    ("The Block", "theblock.co", 82, "https://www.theblock.co/rss.xml"),
    ("Decrypt", "decrypt.co", 72, "https://decrypt.co/feed"),
    ("Blockworks", "blockworks.co", 78, "https://blockworks.com/feed"),
    ("SEC", "sec.gov", 160, "https://www.sec.gov/news/pressreleases.rss"),
    ("CFTC", "cftc.gov", 160, "https://www.cftc.gov/RSS/RSSGP/rssgp.xml"),
    ("Federal Reserve", "federalreserve.gov", 160, "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("ECB", "ecb.europa.eu", 160, "https://www.ecb.europa.eu/rss/press.html"),
    ("BIS", "bis.org", 160, "https://www.bis.org/doclist/all_pressrels.rss"),
    ("CNBC Finance", "cnbc.com", 88, "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
)

STRONG_KEYWORDS = (
    "bitcoin", "btc", "ethereum", "ether", "eth", "crypto", "cryptocurrency",
    "digital asset", "digital currency", "blockchain", "stablecoin", "tokenized",
    "tokenisation", "tokenization", "defi", "web3", "custody", "crypto exchange",
    "wallet", "mining", "staking", "etf", "layer 2", "smart contract",
)
MACRO_KEYWORDS = (
    "federal reserve", "fomc", "interest rate", "rate hike", "rate cut",
    "inflation", "liquidity", "treasury", "central bank", "monetary policy",
)
HIGH_IMPACT = (
    "hack", "breach", "exploit", "ransom", "lawsuit", "enforcement", "approve",
    "approval", "ban", "regulation", "legislation", "launch", "mainnet", "shutdown",
    "acquire", "acquisition", "bankruptcy", "liquidation", "reserve", "custody",
)
TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
WORD_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Entry:
    title: str
    summary: str
    url: str
    published: datetime
    source: str
    source_weight: int


def strip_markup(value: str | None) -> str:
    text = html.unescape(TAG_RE.sub(" ", value or ""))
    return SPACE_RE.sub(" ", text).strip()


def canonical_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key.lower() not in TRACKING_KEYS and not key.lower().startswith("utm_")]
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, urllib.parse.urlencode(query), ""))


def parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    raw = value.strip()
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def child_text(node: ET.Element, names: Iterable[str]) -> str:
    wanted = {name.lower() for name in names}
    for child in node.iter():
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in wanted and child.text:
            return child.text
    return ""


def entry_link(node: ET.Element) -> str:
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1].lower() != "link":
            continue
        href = child.attrib.get("href")
        if href:
            return href
        if child.text:
            return child.text
    return ""


def parse_feed(raw: bytes, source: str, source_weight: int) -> list[Entry]:
    root = ET.fromstring(raw)
    nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
    entries: list[Entry] = []
    for node in nodes:
        title = strip_markup(child_text(node, ("title",)))
        summary = strip_markup(child_text(node, ("description", "summary", "content", "encoded")))
        url = canonical_url(entry_link(node))
        published = parse_datetime(child_text(node, ("pubdate", "published", "updated", "date")))
        if title and url.startswith(("http://", "https://")):
            entries.append(Entry(title, summary, url, published, source, source_weight))
    return entries


def build_ssl_context() -> ssl.SSLContext:
    candidates = sorted((HOME / "hermes-agent" / "venv" / "lib").glob("python*/site-packages/certifi/cacert.pem"))
    cafile = str(candidates[0]) if candidates else None
    return ssl.create_default_context(cafile=cafile)


def build_market_quote(
    pair: str,
    current_samples: dict[str, float],
    change_samples: dict[str, float],
) -> dict:
    current = {name: float(value) for name, value in current_samples.items() if value is not None}
    changes = {name: float(value) for name, value in change_samples.items()}
    if not current:
        raise ValueError(f"{pair} requires at least one price source")
    return {
        "pair": pair,
        "fair_price": round(statistics.median(current.values()), 8),
        "change_24h_pct": round(statistics.median(changes.values()), 4) if changes else None,
        "source_count": len(current),
        "sources": list(current),
    }


def pct_change(current: float, previous: float) -> float:
    if previous <= 0:
        raise ValueError("previous price must be positive")
    return (current / previous - 1.0) * 100.0


def parse_ecb_fx(raw: bytes) -> dict:
    root = ET.fromstring(raw)
    days: list[dict[str, float]] = []
    for node in root.iter():
        if not node.attrib.get("time"):
            continue
        rates = {
            child.attrib["currency"]: float(child.attrib["rate"])
            for child in node
            if child.attrib.get("currency") in {"USD", "JPY", "CNY"} and child.attrib.get("rate")
        }
        if {"USD", "JPY", "CNY"} <= set(rates):
            days.append(rates)
    if len(days) < 2:
        raise ValueError("ECB feed requires at least two daily observations")

    def crosses(rates: dict[str, float]) -> dict[str, float]:
        return {
            "JPY_USD": rates["USD"] / rates["JPY"],
            "JPY_CNY": rates["CNY"] / rates["JPY"],
            "USD_CNY": rates["CNY"] / rates["USD"],
        }

    return {"current": crosses(days[0]), "previous": crosses(days[1])}


def fetch_bytes(url: str, accept: str = "application/json") -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(request, timeout=20, context=build_ssl_context()) as response:
        return response.read()


def fetch_json(url: str) -> dict | list:
    return json.loads(fetch_bytes(url))


def fetch_binance_p2p_buy_usdt() -> float:
    """抓币安 P2P（C2C）法币区 CNY 买 USDT 的买盘最优价。

    返回商家挂单中「买入 USDT」的最低要价（= 你实际下单能拿到的最优价格）。
    该价反映 CNY 侧真实购买 USDT 的成交成本，用于折算真实 USD/USDT。
    """
    body = json.dumps({
        "page": 1,
        "rows": 10,
        "payTypes": [],
        "countries": [],
        "publisherType": None,
        "asset": "USDT",
        "fiat": "CNY",
        "tradeType": "BUY",
    }).encode()
    request = urllib.request.Request(
        BINANCE_P2P_URL,
        data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20, context=build_ssl_context()) as response:
        payload = json.loads(response.read())
    prices = [float(item["adv"]["price"]) for item in payload["data"] if item["adv"].get("price")]
    if not prices:
        raise ValueError("Binance P2P returned no CNY buy-USDT advertisements")
    return min(prices)


def fetch_market_data() -> tuple[list[dict], dict[str, str]]:
    failures: dict[str, str] = {}
    payloads: dict = {}
    try:
        raw_binance = fetch_json(BINANCE_TICKER_URL)
        payloads["binance"] = {item["symbol"]: item for item in raw_binance}
    except Exception as exc:
        failures["Binance"] = f"{type(exc).__name__}: {exc}"
    try:
        payloads["coingecko"] = fetch_json(COINGECKO_URL)
    except Exception as exc:
        failures["CoinGecko"] = f"{type(exc).__name__}: {exc}"
    try:
        rates = fetch_json(COINBASE_RATES_URL.format("USD"))["data"]["rates"]
        payloads["coinbase"] = {
            "BTC_USD": 1 / float(rates["BTC"]),
            "ETH_USD": 1 / float(rates["ETH"]),
            "USDT_USD": 1 / float(rates["USDT"]),
            "JPY_USD": 1 / float(rates["JPY"]),
            "JPY_CNY": float(rates["CNY"]) / float(rates["JPY"]),
            "USDT_CNY": float(rates["CNY"]) / float(rates["USDT"]),
        }
    except Exception as exc:
        failures["Coinbase"] = f"{type(exc).__name__}: {exc}"
    try:
        ecb = parse_ecb_fx(fetch_bytes(ECB_FX_URL, "application/xml"))
        er_rates = fetch_json(ER_API_URL)["rates"]
        current = ecb["current"]
        current["JPY_USD"] = [current["JPY_USD"], float(er_rates["USD"]), payloads["coinbase"]["JPY_USD"]]
        current["JPY_CNY"] = [current["JPY_CNY"], float(er_rates["CNY"]), payloads["coinbase"]["JPY_CNY"]]
        current["USD_CNY"] = [current["USD_CNY"], float(er_rates["CNY"]) / float(er_rates["USD"])]
        payloads["fx"] = ecb
    except Exception as exc:
        failures["FX basket"] = f"{type(exc).__name__}: {exc}"
    try:
        payloads["p2p_usdt_cny"] = fetch_binance_p2p_buy_usdt()
    except Exception as exc:
        failures["Binance P2P"] = f"{type(exc).__name__}: {exc}"
    try:
        return build_market_snapshot(payloads), failures
    except Exception as exc:
        failures["market_snapshot"] = f"{type(exc).__name__}: {exc}"
        return [], failures


def build_market_snapshot(payloads: dict) -> list[dict]:
    binance = payloads["binance"]
    gecko = payloads["coingecko"]
    coinbase = payloads["coinbase"]
    fx = payloads["fx"]
    tether_usd = float(gecko["tether"]["usd"])
    cb_usdt_usd = float(coinbase["USDT_USD"])

    quotes = []
    for pair, symbol, asset, cb_key in (
        ("BTC/USDT", "BTCUSDT", "bitcoin", "BTC_USD"),
        ("ETH/USDT", "ETHUSDT", "ethereum", "ETH_USD"),
    ):
        asset_change = float(gecko[asset]["usd_24h_change"])
        usdt_change = float(gecko["tether"].get("usd_24h_change") or 0.0)
        quotes.append(build_market_quote(
            pair,
            {
                "Binance": float(binance[symbol]["lastPrice"]),
                "CoinGecko": float(gecko[asset]["usd"]) / tether_usd,
                "Coinbase": float(coinbase[cb_key]) / cb_usdt_usd,
            },
            {
                "Binance": float(binance[symbol]["priceChangePercent"]),
                "CoinGecko": ((1 + asset_change / 100) / (1 + usdt_change / 100) - 1) * 100,
            },
        ))

    source_names = ("ECB", "ExchangeRate-API", "Coinbase")
    jpy_usd_values = list(fx["current"]["JPY_USD"])
    usd_jpy_values = [1.0 / float(value) for value in jpy_usd_values]
    quotes.append(build_market_quote(
        "USD/JPY",
        {source_names[index]: value for index, value in enumerate(usd_jpy_values)},
        {"ECB reference": pct_change(statistics.median(usd_jpy_values), 1.0 / float(fx["previous"]["JPY_USD"]))},
    ))

    jpy_cny_values = list(fx["current"]["JPY_CNY"])
    jpy_cny_median = statistics.median(jpy_cny_values)
    quotes.append(build_market_quote(
        "JPY/CNY",
        {source_names[index]: value for index, value in enumerate(jpy_cny_values)},
        {"ECB reference": pct_change(jpy_cny_median, float(fx["previous"]["JPY_CNY"]))},
    ))

    usd_cny_values = list(fx["current"]["USD_CNY"])
    synthetic_usdt_cny = statistics.median(usd_cny_values) * statistics.median((tether_usd, cb_usdt_usd))
    previous_usd_cny = float(fx["previous"]["USD_CNY"])
    usd_cny_change = pct_change(statistics.median(usd_cny_values), previous_usd_cny)
    gecko_usdt_change = float(gecko["tether"].get("cny_24h_change") or 0.0)
    # USDT/CNY 以币安 P2P 买家实价为准（与用户在币安下单看到的价格一致）。
    # 纸面价（CoinGecko/Coinbase/FX 合成）仅作对照与回退：P2P 抓取失败时才退回多源中位数。
    p2p_usdt_cny = payloads.get("p2p_usdt_cny")
    if p2p_usdt_cny:
        p2p_value = float(p2p_usdt_cny)
        usdt_cny_quote = {
            "pair": "USDT/CNY",
            "fair_price": round(p2p_value, 8),
            "change_24h_pct": round(pct_change(p2p_value, float(gecko["tether"]["cny"])), 4)
            if float(gecko["tether"]["cny"]) > 0 else None,
            "source_count": 1,
            "sources": ["Binance P2P"],
            "detail": {
                "口径": "币安 P2P（C2C）CNY 买 USDT 实价，与下单页买家商户价一致",
                "纸面价对照": {
                    "CoinGecko": round(float(gecko["tether"]["cny"]), 4),
                    "Coinbase": round(float(coinbase["USDT_CNY"]), 4),
                    "FX synthetic": round(synthetic_usdt_cny, 4),
                },
            },
        }
        quotes.append(usdt_cny_quote)
    else:
        quotes.append(build_market_quote(
            "USDT/CNY",
            {
                "CoinGecko": float(gecko["tether"]["cny"]),
                "Coinbase": float(coinbase["USDT_CNY"]),
                "FX synthetic": synthetic_usdt_cny,
            },
            {"CoinGecko": gecko_usdt_change, "FX synthetic": usd_cny_change},
        ))
    # 真实 USD/USDT：以 CNY 为媒介 —— 币安 P2P 买 USDT 的 CNY 价 ÷ 真实 USD/CNY 汇率。
    # 含义：为了拿到 1 USDT 你付出多少真实美元。用于对照信用卡按 1 USDT=1 USD 扣款划不划算。
    usd_cny_fair = statistics.median(usd_cny_values)
    p2p_buy_cny = float(payloads.get("p2p_usdt_cny") or synthetic_usdt_cny)
    if payloads.get("p2p_usdt_cny"):
        real_usd_usdt = p2p_buy_cny / usd_cny_fair
    else:
        # P2P 抓取失败时回退多源折算，避免行情空缺
        real_usd_usdt = statistics.median((1.0 / tether_usd, 1.0 / cb_usdt_usd, synthetic_usdt_cny / usd_cny_fair))
    quotes.append(build_market_quote(
        "USD/USDT",
        {
            "Binance P2P": real_usd_usdt,
            "CoinGecko": 1.0 / tether_usd,
        },
        {},  # 真实折算价无 24h 变化可比，留空避免用锚定价污染
    ))

    # USDT 相对美元的溢价/倒挂：>0 表示 USDT 比美元贵（刷卡吃亏），<0 表示便宜（刷卡占便宜）。
    usdt_premium_pct = (real_usd_usdt - 1.0) * 100.0
    quotes.append(build_market_quote(
        "USDT买入溢价",
        {"Binance P2P": usdt_premium_pct},
        {},
    ))

    # 信用卡实刷口径（以标价 1000 USD 的商品为例，直白展示「刷卡要付 vs 换汇要付」）。
    # 场景：卡扣 USDT 但走 USD 结算汇率，且含手续费 CARD_FEE_RATE。
    #   刷卡实付 = 1000 * (1+fee) USDT，按币安 P2P 买入价折 CNY = 你实际掏的人民币
    #   换汇应付 = 1000 USD 按真实 USD/CNY 折 CNY = 理论上最低成本
    #   差值 = 刷卡比换汇多掏的钱（正数=刷卡亏）
    sample_usd = 1000.0
    card_spend_cny = sample_usd * (1.0 + CARD_FEE_RATE) * p2p_buy_cny       # 刷卡实付 CNY
    card_ideal_cny = sample_usd * usd_cny_fair                              # 换汇应付 CNY
    card_loss_cny = card_spend_cny - card_ideal_cny                          # 正=刷卡多花钱(亏)
    card_loss_pct = (card_loss_cny / card_ideal_cny) * 100.0
    quotes.append({
        "pair": "刷卡付CNY",
        "fair_price": round(card_spend_cny, 2),
        "change_24h_pct": None,
        "source_count": 1,
        "sources": ["derived"],
        "detail": {"口径": "买标价1000USD商品，刷卡含手续费实际付出的人民币"},
    })
    quotes.append({
        "pair": "换汇付CNY",
        "fair_price": round(card_ideal_cny, 2),
        "change_24h_pct": None,
        "source_count": 1,
        "sources": ["derived"],
        "detail": {"口径": "买同件商品，直接用美元换汇应付的人民币（理论最低成本）"},
    })
    quotes.append({
        "pair": "刷卡净损失",
        "fair_price": round(card_loss_pct, 2),
        "change_24h_pct": None,
        "source_count": 1,
        "sources": ["derived"],
        "detail": {
            "净损失CNY": round(card_loss_cny, 2),
            "净损失pct": round(card_loss_pct, 2),
            "手续费率": CARD_FEE_RATE,
            "P2P买入价": round(p2p_buy_cny, 4),
            "真实USD/CNY": round(usd_cny_fair, 4),
        },
    })
    return quotes


def is_relevant(entry: Entry) -> bool:
    text = f" {entry.title} {entry.summary} ".lower()
    return any(keyword in text for keyword in STRONG_KEYWORDS) or any(keyword in text for keyword in MACRO_KEYWORDS)


def title_fingerprint(title: str) -> str:
    stop = {"a", "an", "the", "to", "of", "for", "and", "is", "are", "on", "in", "as", "after", "with"}
    words = [word for word in WORD_RE.split(title.lower()) if len(word) > 1 and word not in stop]
    return " ".join(sorted(set(words)))


def event_terms(entry: Entry) -> set[str]:
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "into", "after",
        "says", "said", "new", "news", "report", "reports", "long", "awaited",
        "digital", "asset", "assets", "market", "markets", "crypto",
    }
    words = WORD_RE.split(f"{entry.title} {entry.summary}".lower())
    return {word for word in words if len(word) >= 3 and word not in stop}


def same_event(left: set[str], right: set[str]) -> bool:
    if not left or not right:
        return False
    shared = len(left & right)
    return shared >= 3 and shared / min(len(left), len(right)) >= 0.28


def event_signature(entry: Entry) -> str:
    text = f" {entry.title} {entry.summary} ".lower()
    signatures = (
        ("innovation exemption", "sec-innovation-exemption"),
        ("clarity act", "us-clarity-act"),
        ("revolut", "revolut-event"),
        ("deutsche bank", "deutsche-bank-event"),
        ("coinex", "coinex-event"),
    )
    for marker, signature in signatures:
        if marker in text:
            return signature
    if "fomc" in text or (("federal reserve" in text or "fed " in text) and any(
        marker in text for marker in ("rate hike", "rate cut", "rate increase", "interest rate", "quarter-point")
    )):
        return "fomc-rate-decision"
    return ""


def score_entry(entry: Entry, now: datetime) -> float:
    text = f" {entry.title} {entry.summary} ".lower()
    age_hours = max(0.0, (now - entry.published).total_seconds() / 3600)
    keyword_score = sum(7 for keyword in STRONG_KEYWORDS if keyword in text)
    impact_score = sum(10 for keyword in HIGH_IMPACT if keyword in text)
    macro_score = sum(6 for keyword in MACRO_KEYWORDS if keyword in text)
    freshness = max(0.0, 36.0 - age_hours)
    return entry.source_weight + keyword_score + impact_score + macro_score + freshness


def select_entries(entries: Iterable[Entry], seen: dict[str, int], now: datetime, limit: int) -> list[Entry]:
    cutoff = now - timedelta(hours=72)
    ranked = sorted(
        (entry for entry in entries if entry.published >= cutoff and is_relevant(entry)),
        key=lambda entry: (score_entry(entry, now), entry.published.timestamp()),
        reverse=True,
    )
    selected: list[Entry] = []
    fingerprints: set[str] = set()
    selected_terms: list[set[str]] = []
    signatures: set[str] = set()
    for entry in ranked:
        fingerprint = title_fingerprint(entry.title)
        terms = event_terms(entry)
        signature = event_signature(entry)
        if entry.url in seen:
            # Preserve event identity for seen URLs so syndicated rewrites do
            # not re-enter the same hourly digest.
            fingerprints.add(fingerprint)
            selected_terms.append(terms)
            if signature:
                signatures.add(signature)
            continue
        # The same wire story often appears with one auxiliary verb changed.
        duplicate = any(
            fingerprint == prior
            or (set(fingerprint.split()) and len(set(fingerprint.split()) ^ set(prior.split())) <= 1)
            for prior in fingerprints
        ) or any(same_event(terms, prior) for prior in selected_terms) or bool(signature and signature in signatures)
        if duplicate:
            continue
        selected.append(entry)
        fingerprints.add(fingerprint)
        selected_terms.append(terms)
        if signature:
            signatures.add(signature)
        if len(selected) == limit:
            break
    return selected


def classify(entry: Entry) -> tuple[str, str]:
    text = f" {entry.title} {entry.summary} ".lower()
    if any(word in text for word in ("hack", "breach", "exploit", "ransom", "security")):
        return "安全", "可能影响资产安全、平台运营或用户风险暴露。"
    if any(word in text for word in ("sec ", "cftc", "regulation", "legislation", "law", "enforcement", "court")):
        return "监管", "可能改变数字资产的合规边界、市场准入或机构行为。"
    if any(word in text for word in MACRO_KEYWORDS):
        return "宏观金融", "利率、流动性和监管环境会影响数字资产风险偏好。"
    if any(word in text for word in ("etf", "custody", "bank", "institution", "tokenized", "tokenization")):
        return "机构采用", "反映传统金融与链上资产之间的连接进展。"
    if any(word in text for word in ("launch", "mainnet", "upgrade", "wallet", "blockchain", "layer 2")):
        return "基础设施", "可能影响网络能力、钱包体验或链上应用生态。"
    return "市场动态", "属于本小时与数字资产市场相关的新增信息。"


def compact_summary(entry: Entry, limit: int = 260) -> str:
    text = entry.summary or entry.title
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_report(entries: list[Entry], now: datetime, market_snapshot: list[dict] | None = None) -> dict:
    local = now.astimezone()
    report_id = f"crypto-finance-{local:%Y-%m-%d-%H}"
    items = []
    for rank, entry in enumerate(entries, 1):
        tag, relevance = classify(entry)
        summary = compact_summary(entry)
        items.append(
            {
                "rank": rank,
                "tag": tag,
                "title": entry.title,
                "body": summary,
                "what": summary,
                "problem": "脚本仅基于原文标题和摘要筛选，不补写原文未明确说明的因果。",
                "mechanism": f"由 {entry.source} 的公开 Feed 发布，经关键词、来源权重和时效性规则匹配。",
                "who": entry.source,
                "maturity": f"公开发布；Feed 时间：{entry.published.astimezone():%Y-%m-%d %H:%M %Z}",
                "relevance": relevance,
                "url": entry.url,
                "source": entry.source,
            }
        )
    markets = list(market_snapshot or [])
    return {
        "report_id": report_id,
        "generated_at": local.isoformat(timespec="seconds"),
        "lark_title": f"数字资产与金融小时情报｜{local:%Y-%m-%d %H:00}",
        "doc_title": f"数字资产与金融小时情报｜{local:%Y-%m-%d %H:00}｜{len(items)}条",
        "title": "数字资产与金融权威来源自动匹配",
        "summary": f"固定行情 {len(markets)} 项，相关新闻 {len(items)} 条；均由无模型脚本处理，Token 消耗为 0。",
        "market_snapshot": markets,
        "original_urls": [entry.url for entry in entries],
        "archive_type": "数字资产与金融小时情报",
        "archive_tags": "数字货币, 区块链, Web3, 金融, 监管, 宏观",
        "items": items,
        "action": "仅供信息参考，不构成投资建议；重要事件请打开原文核验。",
        "footer": "行情：多源中位数公允价｜资讯：来源权重＋关键词＋时效性＋事件去重｜无模型、零 Token",
    }


def fetch_feed(feed: tuple[str, str, int, str]) -> tuple[str, list[Entry], str | None]:
    name, source, weight, url = feed
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    try:
        with urllib.request.urlopen(request, timeout=20, context=build_ssl_context()) as response:
            return name, parse_feed(response.read(), source, weight), None
    except Exception as exc:  # A failed source is reported, never treated as no news.
        return name, [], f"{type(exc).__name__}: {exc}"


def load_seen(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(url): int(timestamp) for url, timestamp in (data.get("seen") or {}).items()}
    except (OSError, ValueError, TypeError):
        return {}


def commit_seen(path: Path, entries: Iterable[dict], timestamp: int) -> None:
    seen = load_seen(path)
    expiry = timestamp - 7 * 24 * 3600
    seen = {url: value for url, value in seen.items() if value >= expiry}
    for entry in entries:
        seen[str(entry["url"])] = timestamp
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"seen": seen, "updated_at": timestamp}, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        temp = Path(handle.name)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp = Path(handle.name)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def report_path(now: datetime, dry_run: bool = False) -> Path:
    suffix = ".dry-run.json" if dry_run else ".json"
    return OUTPUT_DIR / f"{now.astimezone():%Y-%m-%d-%H}{suffix}"


def run_checked(command: list[str]) -> dict:
    completed = subprocess.run(command, text=True, capture_output=True, timeout=180, check=False)
    if completed.returncode != 0:
        safe_error = (completed.stderr or completed.stdout or "command failed").strip()[-500:]
        raise RuntimeError(f"{Path(command[1]).name}: {safe_error}")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="collect and write JSON without Lark writes or dedupe commit")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    if args.limit != 10:
        raise SystemExit("production delivery requires --limit 10")

    now = datetime.now(timezone.utc)
    all_entries: list[Entry] = []
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_feed, feed): feed[0] for feed in FEEDS}
        for future in as_completed(futures):
            name, entries, error = future.result()
            all_entries.extend(entries)
            if error:
                failures[name] = error

    selected = select_entries(all_entries, load_seen(STATE_PATH), now, args.limit)
    market_snapshot, market_failures = fetch_market_data()
    if not selected and not market_snapshot:
        print(json.dumps({"ok": True, "silent": True, "token_usage": 0, "item_count": 0, "source_failures": failures, "market_failures": market_failures}, ensure_ascii=False))
        return 0

    report = build_report(selected, now, market_snapshot)
    output_path = report_path(now, args.dry_run)
    write_json(output_path, report)
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "token_usage": 0, "item_count": len(selected), "market_count": len(market_snapshot), "report_path": str(output_path), "source_failures": failures, "market_failures": market_failures}, ensure_ascii=False))
        return 0

    translation = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "item_count": 0}
    if selected:
        translation = run_checked([str(PYTHON), str(TRANSLATOR), str(output_path)])
        if not (
            translation.get("translated") is True
            and translation.get("model") == "gpt-6-astra"
            and translation.get("reasoning") == "none"
            and translation.get("reasoning_tokens") == 0
            and translation.get("item_count") == len(selected)
        ):
            raise RuntimeError("translation verification failed")

    archive = run_checked([str(PYTHON), str(ARCHIVER), str(output_path)])
    if not all(archive.get(key) is True for key in ("archived", "document_verified", "record_verified")):
        raise RuntimeError("archive verification failed")
    delivery = run_checked([str(PYTHON), str(SENDER), str(output_path)])
    if not (delivery.get("sent") is True and delivery.get("verified") is True and delivery.get("msg_type") == "interactive" and delivery.get("item_count") == len(selected)):
        raise RuntimeError("Lark delivery verification failed")

    commit_seen(STATE_PATH, report["items"], int(now.timestamp()))
    token_usage = int(translation.get("input_tokens", 0)) + int(translation.get("output_tokens", 0))
    print(json.dumps({"ok": True, "token_usage": token_usage, "reasoning_tokens": int(translation.get("reasoning_tokens", 0)), "translation_model": translation.get("model") if selected else None, "item_count": len(selected), "market_count": len(market_snapshot), "report_path": str(output_path), "archive_verified": True, "lark_verified": True, "source_failures": failures, "market_failures": market_failures}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

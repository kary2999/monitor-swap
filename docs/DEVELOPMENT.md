# 后续开发指南

本文件面向"接手继续改"的场景。改之前先读这里。

## 1. 架构与数据流

```
crypto_finance_hourly.py（主入口）
   │
   ├─ 并发抓 FEEDS（10 个 RSS，ThreadPoolExecutor）
   │     └─ select_entries()：来源权重 + 关键词 + 时效 + 事件去重 → 最多 10 条
   │
   ├─ fetch_market_data()：抓行情
   │     ├─ Binance REST（BTC/ETH 现货）
   │     ├─ CoinGecko（BTC/ETH/USDT 多币种）
   │     ├─ Coinbase（法币交叉汇率）
   │     ├─ ECB + er-api（USD/JPY, JPY/CNY, USD/CNY）
   │     └─ Binance P2P（USDT/CNY 实价）★ 核心
   │
   ├─ build_report() → 写 <YYYY-MM-DD-HH>.json
   │
   ├─ 正式模式继续：
   │     ├─ translate_crypto_news.py（batch 中文化）
   │     ├─ archive_hourly_intel.py（飞书文档 + 多维表格）
   │     ├─ send_lark_intel_post.py（Lark 交互卡片）
   │     └─ commit_seen()（提交去重状态）
   │
   └─ 每步都有 verify：失败即 raise，绝不静默通过
```

**关键约束**：`--limit` 必须为 10（第 660 行硬校验），改动前想清楚。

## 2. 行情：改价格口径 / 加交易对

**入口**：`build_market_snapshot(payloads)`（约 307 行起）。

每个交易对调 `build_market_quote(pair, current_samples, change_samples)`：

```python
build_market_quote(
    "交易对名",
    {"源A": 价格A, "源B": 价格B},   # → fair_price 取中位数
    {"源A": 涨跌幅},                # 可空
)
```

### 加一个新交易对

1. 在 `fetch_market_data()` 里抓回原始数据，塞进 `payloads`。
2. 在 `build_market_snapshot()` 里取出并调用 `build_market_quote(...)`。
3. 如果它需要"实价优先"而非中位数（像 USDT/CNY 那样），**别用 `build_market_quote`**，直接手搓 dict（照抄 USDT/CNY 那段）。

### ⚠️ USDT/CNY 的口径坑（2026-10-04 修）

**症状**：Lark 显示 6.7，但币安 P2P 买家商户实价是 6.66-6.67。

**根因**：原代码把 `CoinGecko tether.cny` / `Coinbase USDT_CNY` / `FX synthetic` 三源取中位数——全是纸面价，**没读 P2P**。

**修法**（现行实现）：
```python
p2p_usdt_cny = payloads.get("p2p_usdt_cny")
if p2p_usdt_cny:
    # P2P 实价优先，sources=["Binance P2P"]，纸面价放 detail 对照
else:
    # 回退：三源中位数
```

**教训**：**行情口径 = 业务语义**。改任何"公允价"前，先问"用户实际成交看到的是哪个价"。`fetch_binance_p2p_buy_usdt()`（约 232 行）返回的是 P2P **买入 USDT 的最低要价**（= 你下单能拿到的最优价）。

### P2P 抓取失败怎么办

`fetch_binance_p2p_buy_usdt()` 抛异常时，`payloads["p2p_usdt_cny"]` 不设置，USDT/CNY 自动走中位数回退，`USD/USDT` 走多源折算回退。**不会行情空缺**。看 `market_failures` 字段定位失败源。

## 3. 资讯筛选：改关键词 / 换源 / 调去重

| 想改什么 | 改哪里 |
|---|---|
| 新增/删除 RSS 源 | `FEEDS` 元组（约 58 行），格式 `(名称, 域名, 来源权重, URL)` |
| 加密相关关键词 | `STRONG_KEYWORDS` |
| 宏观关键词 | `MACRO_KEYWORDS` |
| 高影响事件词（hack/ban/approval…） | `HIGH_IMPACT` |
| 事件去重规则 | `event_signature()` + `same_event()` |
| 打分权重 | `score_entry()` |

**去重机制**（别踩）：`select_entries()` 用三重去重——URL 去重（`seen` 状态）、标题指纹、事件词重叠。改了 `commit_seen` 的状态保留期（当前 7 天）要同步想清楚。

## 4. 调试与验证

```bash
# 干跑：不写 Lark、不提交状态，只出 JSON
python3 scripts/crypto_finance_hourly.py --dry-run

# 看行情结果（改完必做）
python3 -c "
import json,glob
f=sorted(glob.glob('$HERMES_HOME/hourly-intel/crypto-finance/*.dry-run.json'))[-1]
d=json.load(open(f))
for q in d['market_snapshot']:
    print(q['pair'],'=',q['fair_price'],'|',q['sources'])
"
```

**验收标准**：`market_failures` 为空 `{}`，且 USDT/CNY 的 `sources` 含 `"Binance P2P"`。

## 5. 部署

脚本设计为**从 `~/.hermes/scripts/` 直接运行**（Hermes cron 调用）：

- 环境探测：`HERMES_HOME`（默认 `~/.hermes`）
- 解释器：`$HERMES_HOME/hermes-agent/venv/bin/python`
- 代理：无代理时境外源不可达，脚本自注入 `HERMES_HTTP_PROXY`（默认 `http://127.0.0.1:7890`）

**改脚本后要同步回 `~/.hermes/scripts/`**，否则 cron 跑的还是旧版。建议做法：
```bash
# 本仓库改完 → 覆盖到运行目录
cp scripts/*.py ~/.hermes/scripts/
```

## 6. 已知问题 / TODO

- [ ] `notify_kimyx_security_audit.py` 是占位（1 行），未纳入本仓库
- [ ] `setup_feishu_hermes_kb.py` 是飞书知识库一次性初始化，与小时监控无关，未纳入
- [ ] 代理地址硬编码默认值 `127.0.0.1:7890`，换环境需注意
- [ ] `translate_crypto_news.py` 依赖外部模型接口，模型名 `gpt-6-astra` 与 `reasoning=none` 是硬校验（约 692 行），换模型要改

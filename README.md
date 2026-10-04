# monitor-swap · 数字资产与金融小时监控

> 每小时自动抓取数字货币 / 金融行情 + 权威媒体新闻，推送到 Lark（飞书），并归档到云文档与多维表格。
> 面向个人使用，**全脚本零 Token**（资讯中文化除外，走一次批量模型调用）。

## 这个项目做什么用的？

一句话：**替我盯盘 + 盯新闻，每小时往 Lark 推一份简报。**

它解决三件具体的事：

1. **行情监控** —— 固定 6+ 个交易对的公允价，其中 `USDT/CNY`、`USD/USDT` 以**币安 P2P 真实成交价**为准（与你在币安下单页看到的价格一致），而非纸面中间价。
2. **新闻筛选** —— 从 CoinDesk / The Block / Decrypt / SEC / CFTC / 美联储 / ECB / BIS 等权威源的 RSS 中，按来源权重 + 关键词 + 时效性 + 事件去重，每小时挑出最多 10 条，再批量中文化。
3. **归档** —— 每份简报写入飞书云文档 + 多维表格，Lark 群内发交互卡片，手动执行时报告 Token 消耗。

## 快速开始

```bash
# 1. 依赖：Python 3.10+，仅标准库（无第三方包）
python3 --version

# 2. 配置环境变量（见 .env.example）
cp .env.example .env && $EDITOR .env

# 3. 干跑（不写 Lark、不提交去重状态，只产出 JSON）
python3 scripts/crypto_finance_hourly.py --dry-run

# 4. 正式跑（写 Lark + 归档 + 提交去重）
python3 scripts/crypto_finance_hourly.py
```

产出落在 `$HERMES_HOME/hourly-intel/crypto-finance/<YYYY-MM-DD-HH>.json`。

## 目录结构

```
monitor-swap/
├── README.md                  # 本文件：项目做什么用
├── docs/
│   └── DEVELOPMENT.md         # 后续开发指南（行情源、加交易对、调试）
├── scripts/
│   ├── crypto_finance_hourly.py   # 主入口：行情 + 资讯采集、筛选、组装报告
│   ├── translate_crypto_news.py   # 资讯批量中文化（仅无新闻时跳过，零 Token）
│   ├── archive_hourly_intel.py    # 归档到飞书云文档 + 多维表格
│   ├── send_lark_intel_post.py    # 发送 Lark 交互卡片
│   ├── send_lark_image.py         # 图片发送（aux）
│   ├── render_hourly_intel.py     # 小时简报渲染
│   └── render_intel_long.py       # 长文简报渲染
├── .env.example               # 环境变量样例（不含真实密钥）
└── cron/                      # 定时任务配置样例
```

## 数据源一览

| 类别 | 来源 | 用途 |
|---|---|---|
| 行情-加密 | Binance REST (`/ticker/24hr`) | BTC/ETH 现货价 |
| 行情-加密 | CoinGecko simple price | BTC/ETH/USDT 多币种折算 |
| 行情-汇率 | Coinbase exchange-rates | 法币交叉汇率 |
| 行情-汇率 | ECB 90 天历史 XML | USD/JPY、JPY/CNY、USD/CNY 中间价 |
| 行情-汇率 | open.er-api.com | 汇率交叉校验 |
| **行情-实价** | **Binance P2P (C2C) search** | **USDT/CNY 买家实价（核心口径）** |
| 资讯 | CoinDesk/The Block/Decrypt/Blockworks | 加密媒体 |
| 资讯 | SEC/CFTC/Fed/ECB/BIS | 监管与央行原文 |

## 行情口径说明（重要）

**`USDT/CNY` 以币安 P2P 买家实价为准。** 这是本项目最核心的口径决定：

- ✅ **首选**：Binance P2P（C2C）`fiat=CNY, asset=USDT, tradeType=BUY` 的商家挂单最低要价 —— 与你在币安 App 下单页看到的价格一致。
- ⚠️ **回退**：P2P 抓取失败时，才退回 CoinGecko / Coinbase / FX 合成三源中位数。
- 📊 **对照**：纸面价保留在 `detail.纸面价对照`，便于随时比较。

> 为什么不用中间价？中间价（6.70+）与真实成交价（6.66-6.67）天生有差，用它折算刷卡成本会失真。

## 定时任务

由 Hermes cron 调度，每小时触发一次主入口。样例见 `cron/README.md`。

## 安全

- 所有密钥走 `.env` / 环境变量，**禁止硬编码**。
- 仓库仅含脚本与文档，不含任何 token、密钥、账号数据。
- 输出文件权限 `0600`。

## 许可

个人项目，仅供自用参考。

# 定时任务（cron）

本项目由 **Hermes cron** 调度，每小时触发一次主入口。

## 调度配置

任务注册在 Hermes 的 cron 里（见 `~/.hermes/cron/`），每小时整点后触发：

```
脚本：~/.hermes/scripts/crypto_finance_hourly.py
解释器：~/.hermes/hermes-agent/venv/bin/python
频率：每小时
```

## 手动触发

```bash
# 干跑（不写任何外部系统）
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/crypto_finance_hourly.py --dry-run

# 正式跑（写 Lark + 归档）
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/crypto_finance_hourly.py
```

## 输出

- 报告 JSON：`~/.hermes/hourly-intel/crypto-finance/<YYYY-MM-DD-HH>.json`
- 去重状态：`~/.hermes/hourly-intel/crypto-finance/feed-state.json`（保留 7 天）
- Lark：交互卡片 + 飞书云文档 + 多维表格记录

## 排障

| 现象 | 检查 |
|---|---|
| 没收到推送 | 看 cron 输出目录 `~/.hermes/cron/output/<job_id>/` 最新文件 |
| `market_failures` 非空 | 对应源抓取失败（常见：代理未开、P2P 限频） |
| 翻译校验失败 | `translate_crypto_news.py` 的 model/reasoning 硬校验 |
| 归档未验证 | 飞书 token 过期或权限变更 |

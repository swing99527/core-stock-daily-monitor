# 核心股票日报历史库设计

SQLite 路径：`financial-research/history/core_four_daily.sqlite3`

## 存储原则

只存可复盘、可查询、可重新生成结论的数据，不把 HTML 当作历史事实源。

## 表设计

### `report_runs`

记录每次自动化运行。用于回答“哪天跑过、是否成功、是否 fallback”。

核心字段：
- `report_date`：报告日期。
- `generated_at`：运行生成时间。
- `status`：`success` 或 `failed`。
- `valid_count`：拿到有效行情的股票数。
- `data_dates_json`：本次覆盖的数据交易日。
- `watchlist_json`：运行时 watchlist 快照。
- `run_dir`、`report_path`、`dashboard_path`：可追溯到原始数据和展示产物。
- `error_summary`：失败或 stale 时的摘要。

### `stock_daily_snapshots`

记录每只股票在每次运行中的核心监控快照。用于跨日比较趋势标签、资金配合、策略变化。

核心字段：
- 行情事实：`trade_date`、`close`、`change_rate`、`turnover`、`capital_flow`、`volume_ratio`。
- 估值事实：`pe`、`pb`。
- 监控标签：`trend_label`、`move_label`、`strategy_tag`。
- K线摘要：`kline_signal`、`kline_score`。
- JSON 详情：`metrics_json`、`labels_json`、`strategy_json`、`kline_analysis_json`、`news_json`、`filings_json`、`errors_json`。

### `kline_bars`

记录每只股票的日 K 原始 OHLCV。用于后续重新计算 MA、RSI、MACD、形态、支撑压力，而不依赖旧 Markdown。

核心字段：
- `symbol`、`period`、`adjust`、`timestamp`：唯一定位一根 K 线。
- `trade_date`、`open`、`high`、`low`、`close`、`volume`、`turnover`。
- `source_report_date`：这根 K 线由哪次日报写入。

### `kline_analysis_history`

记录每只股票每次运行的 K 线分析结论。用于查看分析随时间如何变化。

核心字段：
- `signal`、`score`：K 线综合信号和评分。
- `summary`、`trend`、`momentum`、`volume`、`pattern`、`risk`：可读分析文本。
- `support`、`resistance`：20日支撑/压力。
- `analysis_json`：完整结构化分析。

## 不存储的内容

- 不存整页 HTML：HTML 是展示层，可由报告脚本重新生成。
- 不存未结构化大段新闻正文：日报只需要标题和分类，避免数据库膨胀。
- 不存账户、交易、持仓信息：本系统只做研究监控。

# 核心股票日报 GitHub Actions / Pages 发布

这个工作流把 `financial-research/scripts/core_four_daily_report.py` 作为唯一生成入口，每个工作日下午 `17:40 Asia/Shanghai` 自动运行，并把 `latest_core_four_daily_dashboard.html` 通过 GitHub Actions artifact 发布为 GitHub Pages 首页。

workflow 也会把发布后的站点快照写入仓库根目录的 `docs/`，方便审计和本地查看。成功生成实时报表时，日报输出和 `docs/` 站点会放在同一次提交里推回 `main`，避免连续 push 触发多次 GitHub Pages deployment。

## GitHub 配置

1. 在 GitHub 仓库的 `Settings -> Secrets and variables -> Actions` 新增 secret：`LONGBRIDGE_CLI_AUTH_B64`。
2. 本机生成 secret 值：

```bash
base64 < ~/.longbridge/openapi/cli-auth
```

3. 在 `Settings -> Pages` 中选择 `Build and deployment -> Source -> GitHub Actions`。
4. 手动运行一次 `Core stock daily report` workflow，让它创建并部署 Pages 站点。
5. 确认 Pages URL 可访问。

## 失败回退

如果 Longbridge 或网络失败，脚本会写入 `financial-research/data/core-four/<YYYYMMDD>/failure.md` 并保留上一份有效日报。Pages 仍会发布上一份有效看板，同时在首页顶部显示失败提示，并附带 `failure.md` 与 `status.json`。

## 发布产物

- `index.html`：Pages 首页，来自最新有效 HTML 看板。
- `dashboard.html`：原始最新 HTML 看板副本。
- `latest_core_four_daily_report.md`：最新有效 Markdown 日报。
- `failure.md`：当日失败说明，仅在失败回退时存在。
- `status.json`：本次自动化状态。

#!/usr/bin/env python3
"""Prepare the GitHub Pages payload for the core-stock daily dashboard."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "financial-research" / "reports"
DATA_DIR = ROOT / "financial-research" / "data" / "core-four"
BJ = timezone(timedelta(hours=8))


def now_bj() -> str:
    return datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S CST")


def inject_status_banner(html: str, banner: str) -> str:
    if not banner:
        return html
    style = (
        "<style>"
        ".publish-status-banner{position:sticky;top:0;z-index:20;"
        "padding:10px 18px;background:#fff7ed;border-bottom:1px solid #fed7aa;"
        "color:#7c2d12;font:14px/1.45 -apple-system,BlinkMacSystemFont,"
        "'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif}"
        ".publish-status-banner a{color:#9a3412;font-weight:700}"
        "</style>"
    )
    markup = f'{style}<div class="publish-status-banner">{banner}</div>'
    if "<body>" in html:
        return html.replace("<body>", f"<body>\n  {markup}", 1)
    return markup + html


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare GitHub Pages files.")
    parser.add_argument("--date", required=True, help="Report run date in YYYYMMDD.")
    parser.add_argument("--report-exit-code", type=int, required=True)
    parser.add_argument("--site-dir", default="public")
    args = parser.parse_args()

    latest_html = REPORT_DIR / "latest_core_four_daily_dashboard.html"
    latest_report = REPORT_DIR / "latest_core_four_daily_report.md"
    if not latest_html.exists():
        raise SystemExit(f"missing latest dashboard: {latest_html}")
    if not latest_report.exists():
        raise SystemExit(f"missing latest report: {latest_report}")

    site_dir = Path(args.site_dir)
    site_dir.mkdir(parents=True, exist_ok=True)

    failure_path = DATA_DIR / args.date / "failure.md"
    fallback = args.report_exit_code != 0
    banner = ""
    if fallback:
        banner = (
            f"本次自动化未生成 {args.date} 实时报表，当前页面展示上一份有效看板。"
            ' 详情见 <a href="failure.md">failure.md</a>。'
        )

    dashboard_html = latest_html.read_text(encoding="utf-8")
    (site_dir / "index.html").write_text(inject_status_banner(dashboard_html, banner), encoding="utf-8")
    shutil.copyfile(latest_html, site_dir / "dashboard.html")
    shutil.copyfile(latest_report, site_dir / "latest_core_four_daily_report.md")

    if failure_path.exists():
        shutil.copyfile(failure_path, site_dir / "failure.md")
    elif fallback:
        (site_dir / "failure.md").write_text(
            f"# 核心股票日度监控报告生成失败 {args.date}\n\n"
            f"生成时间：{now_bj()}\n\n"
            f"脚本退出码：{args.report_exit_code}。未找到详细 failure 文件。\n",
            encoding="utf-8",
        )

    status = {
        "generated_at": now_bj(),
        "report_date": args.date,
        "report_exit_code": args.report_exit_code,
        "fallback_to_previous_report": fallback,
        "latest_dashboard": "dashboard.html",
        "latest_report": "latest_core_four_daily_report.md",
        "failure": "failure.md" if (site_dir / "failure.md").exists() else None,
    }
    (site_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

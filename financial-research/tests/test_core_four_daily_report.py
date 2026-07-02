import importlib.util
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "core_four_daily_report.py"
)
SPEC = importlib.util.spec_from_file_location("core_four_daily_report", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CoreFourDailyReportTests(unittest.TestCase):
    def test_collect_run_is_stale_when_any_symbol_is_not_report_date(self) -> None:
        collected = [
            {"config": {"symbol": "605499.SH"}, "metrics": {"trade_date": "2026-07-01"}, "errors": [], "used_cache": False},
            {"config": {"symbol": "000333.SZ"}, "metrics": {"trade_date": "2026-06-30"}, "errors": [], "used_cache": False},
        ]

        result = MODULE.validate_latest_trade_date(collected, "20260701")

        self.assertFalse(result["ok"])
        self.assertIn("000333.SZ", result["details"][0])

    def test_collect_run_is_invalid_when_any_symbol_uses_cache(self) -> None:
        collected = [
            {"config": {"symbol": "605499.SH"}, "metrics": {"trade_date": "2026-07-01"}, "errors": [], "used_cache": False},
            {"config": {"symbol": "000333.SZ"}, "metrics": {"trade_date": "2026-07-01"}, "errors": ["quote: timeout；使用缓存 2026-07-01 15:01:00 CST"], "used_cache": True},
        ]

        result = MODULE.validate_latest_trade_date(collected, "20260701")

        self.assertFalse(result["ok"])
        self.assertIn("使用缓存", result["details"][0])

    def test_main_can_render_from_summary_input(self) -> None:
        summary_item = {
            "config": {
                "symbol": "605499.SH",
                "name": "东鹏饮料",
                "short_name": "东鹏",
                "sector": "软饮料",
                "business_type": "品牌快消",
            },
            "errors": [],
            "used_cache": False,
            "metrics": {
                "trade_date": "2026-07-01",
                "close": 123.45,
                "prev_close": 122.0,
                "open": 122.8,
                "high": 124.0,
                "low": 121.2,
                "change_rate": 1.19,
                "turnover": 100000000.0,
                "volume_ratio": 1.2,
                "capital_flow": 1200000.0,
                "mktcap": 90000000000.0,
                "pe": 20.0,
                "pb": 4.5,
                "ma5": 120.0,
                "ma10": 119.0,
                "ma20": 118.0,
                "rsi14": 55.0,
            },
            "labels": {
                "move": "小幅波动",
                "trend": "趋势修复",
                "observe": "MA10 119.00",
            },
            "strategy": {
                "tag": "顺势观察",
                "plan": "看均线承接。",
                "breakout": "突破前高。",
                "pullback": "回踩 MA10。",
                "invalid": "跌破低点。",
            },
            "sparkline": [120.0, 121.0, 123.45],
            "news": {},
            "filings": [],
            "capital": {},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            summary_path = tmp / "summary.json"
            summary_path.write_text(json.dumps([summary_item], ensure_ascii=False), encoding="utf-8")
            data_dir = tmp / "data"
            report_dir = tmp / "reports"

            argv = [
                "core_four_daily_report.py",
                "--date",
                "20260701",
                "--summary-input",
                str(summary_path),
                "--data-dir",
                str(data_dir),
                "--report-dir",
                str(report_dir),
            ]
            with mock.patch.object(sys, "argv", argv):
                result = MODULE.main()

            self.assertEqual(result, 0)
            self.assertTrue((report_dir / "20260701_core_four_daily_report.md").exists())
            self.assertTrue((report_dir / "latest_core_four_daily_dashboard.html").exists())


if __name__ == "__main__":
    unittest.main()

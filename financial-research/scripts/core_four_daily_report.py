#!/usr/bin/env python3
"""Generate a daily monitoring report for the core-stock watchlist.

The script uses Longbridge CLI as the primary data source and writes both raw
JSON data and a Markdown report. It is a research workflow, not investment
advice or an order-execution tool.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WATCHLIST = ROOT / "financial-research" / "watchlists" / "core_four_watchlist.json"
DEFAULT_DATA_DIR = ROOT / "financial-research" / "data" / "core-four"
DEFAULT_CACHE_DIR = ROOT / "financial-research" / "data" / "core-four-cache"
DEFAULT_REPORT_DIR = ROOT / "financial-research" / "reports"
DEFAULT_HISTORY_DB = ROOT / "financial-research" / "history" / "core_four_daily.sqlite3"
BJ = timezone(timedelta(hours=8))


@dataclass
class CommandResult:
    ok: bool
    data: Any
    stderr: str
    command: list[str]


def now_bj() -> datetime:
    return datetime.now(BJ)


def to_float(value: Any) -> float | None:
    try:
        if value in ("", "-", None):
            return None
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def pct(value: float | None, digits: int = 2) -> str:
    if value is None or math.isnan(value):
        return "NA"
    return f"{value:+.{digits}f}%"


def num(value: float | None, digits: int = 2) -> str:
    if value is None or math.isnan(value):
        return "NA"
    return f"{value:.{digits}f}"


def money_cny(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "NA"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 100_000_000:
        return f"{sign}{value / 100_000_000:.2f}亿"
    if value >= 10_000:
        return f"{sign}{value / 10_000:.2f}万"
    return f"{sign}{value:.0f}"


def html_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def safe_filename(symbol: str) -> str:
    return symbol.replace(".", "_").replace("/", "_")


def run_longbridge(args: list[str], timeout: int = 45, attempts: int = 2) -> CommandResult:
    cmd = ["longbridge", *args]
    last_error = ""
    for _ in range(attempts):
        try:
            proc = subprocess.run(
                cmd,
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            return CommandResult(False, None, str(exc), cmd)
        except subprocess.TimeoutExpired as exc:
            last_error = f"timeout after {timeout}s: {exc}"
            continue

        if proc.returncode != 0:
            last_error = proc.stderr.strip()
            continue

        stdout = proc.stdout.strip()
        if not stdout:
            return CommandResult(True, None, proc.stderr.strip(), cmd)
        try:
            return CommandResult(True, json.loads(stdout), proc.stderr.strip(), cmd)
        except json.JSONDecodeError:
            last_error = f"invalid json: {stdout[:300]}"
            continue
    return CommandResult(False, None, last_error, cmd)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cache_path(cache_dir: Path, symbol: str, key: str) -> Path:
    return cache_dir / safe_filename(symbol) / f"{key}.json"


def save_cache(cache_dir: Path, symbol: str, key: str, data: Any) -> None:
    write_json(
        cache_path(cache_dir, symbol, key),
        {
            "cached_at": now_bj().strftime("%Y-%m-%d %H:%M:%S CST"),
            "symbol": symbol,
            "key": key,
            "data": data,
        },
    )


def load_cache(cache_dir: Path, symbol: str, key: str) -> tuple[Any, str] | None:
    path = cache_path(cache_dir, symbol, key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload.get("data"), payload.get("cached_at", "unknown")


def first_item(data: Any) -> dict[str, Any]:
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    return {}


def local_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(BJ)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return value[:10]


def report_trade_date(report_date: str) -> str:
    return datetime.strptime(report_date, "%Y%m%d").strftime("%Y-%m-%d")


def moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def rsi(values: list[float], window: int = 14) -> float | None:
    if len(values) <= window:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in zip(values[-window - 1 : -1], values[-window:]):
        delta = cur - prev
        gains.append(max(delta, 0))
        losses.append(abs(min(delta, 0)))
    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def rolling_std(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    sample = values[-window:]
    mean = sum(sample) / window
    return math.sqrt(sum((value - mean) ** 2 for value in sample) / window)


def average(values: list[float]) -> float | None:
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(values) / len(values)


def parse_kline_rows(rows: Any) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return parsed
    for row in rows:
        if not isinstance(row, dict):
            continue
        close = to_float(row.get("close"))
        open_price = to_float(row.get("open"))
        high = to_float(row.get("high"))
        low = to_float(row.get("low"))
        if close is None or open_price is None or high is None or low is None:
            continue
        parsed.append(
            {
                "time": row.get("time") or row.get("timestamp") or "",
                "trade_date": local_date(row.get("time")),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": to_float(row.get("volume")),
                "turnover": to_float(row.get("turnover")),
            }
        )
    return parsed


def latest_candle_label(row: dict[str, Any]) -> str:
    open_price = row.get("open")
    close = row.get("close")
    high = row.get("high")
    low = row.get("low")
    if None in (open_price, close, high, low):
        return "K线缺失"
    candle_range = max(high - low, 1e-9)
    body = abs(close - open_price)
    body_ratio = body / candle_range
    direction = "阳线" if close > open_price else "阴线" if close < open_price else "平盘线"
    if body_ratio <= 0.08:
        return "十字星"
    if body_ratio >= 0.7:
        return f"长实体{direction}"
    if body_ratio >= 0.35:
        return f"中实体{direction}"
    return f"小实体{direction}"


def detect_candlestick_patterns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    patterns: list[dict[str, Any]] = []
    if not rows:
        return patterns

    start = max(0, len(rows) - 5)
    for idx in range(start, len(rows)):
        row = rows[idx]
        open_price = row["open"]
        close = row["close"]
        high = row["high"]
        low = row["low"]
        candle_range = max(high - low, 1e-9)
        body = abs(close - open_price)
        upper = high - max(close, open_price)
        lower = min(close, open_price) - low
        bull = close > open_price
        date = row.get("trade_date") or local_date(row.get("time"))

        if body <= 0.05 * candle_range:
            patterns.append({"date": date, "name": "十字星", "score": 0, "note": "多空分歧放大，需看次日方向确认"})
        elif lower > 2 * body and upper < 0.25 * max(body, 1e-9):
            score = 1 if bull else 0
            patterns.append({"date": date, "name": "锤子线", "score": score, "note": "下影线较长，低位承接增强"})
        elif upper > 2 * body and lower < 0.25 * max(body, 1e-9):
            patterns.append({"date": date, "name": "倒锤/射击星", "score": -1, "note": "上影线较长，上方抛压需要消化"})
        elif body >= 0.85 * candle_range and bull:
            patterns.append({"date": date, "name": "强阳线", "score": 1, "note": "实体较强，需成交继续配合"})
        elif body >= 0.85 * candle_range and not bull:
            patterns.append({"date": date, "name": "强阴线", "score": -1, "note": "实体较弱，先看止跌信号"})

    for idx in range(max(1, len(rows) - 5), len(rows)):
        prev = rows[idx - 1]
        cur = rows[idx]
        prev_bull = prev["close"] > prev["open"]
        cur_bull = cur["close"] > cur["open"]
        date = cur.get("trade_date") or local_date(cur.get("time"))
        if (not prev_bull) and cur_bull and cur["open"] < prev["close"] and cur["close"] > prev["open"]:
            patterns.append({"date": date, "name": "看涨吞没", "score": 2, "note": "反包前一日实体，修复力度较强"})
        elif prev_bull and (not cur_bull) and cur["open"] > prev["close"] and cur["close"] < prev["open"]:
            patterns.append({"date": date, "name": "看跌吞没", "score": -2, "note": "反包前一日实体，短线压力上升"})

    for idx in range(max(2, len(rows) - 5), len(rows)):
        a, b, c = rows[idx - 2], rows[idx - 1], rows[idx]
        date = c.get("trade_date") or local_date(c.get("time"))
        if a["close"] < a["open"] and b["close"] > b["open"] and c["close"] > c["open"] and c["close"] > b["close"] > a["close"]:
            patterns.append({"date": date, "name": "三连阳", "score": 2, "note": "短线进攻连续，但需警惕加速后的回踩"})
        elif a["close"] > a["open"] and b["close"] < b["open"] and c["close"] < c["open"] and c["close"] < b["close"] < a["close"]:
            patterns.append({"date": date, "name": "三连阴", "score": -2, "note": "短线抛压连续，先看止跌"})

    return patterns[-5:]


def kline_detailed_analysis(rows: list[dict[str, Any]], metrics: dict[str, Any]) -> dict[str, Any]:
    if len(rows) < 20:
        return {
            "signal": "数据不足",
            "score": 0,
            "summary": "K线样本不足20根，只保留基础价格观察。",
            "trend": "无法确认",
            "momentum": "无法确认",
            "volume": "无法确认",
            "pattern": "无",
            "support": None,
            "resistance": None,
            "risk": "等待补齐K线历史后再判断。",
            "indicators": {},
            "patterns": [],
        }

    closes = [row["close"] for row in rows]
    highs = [row["high"] for row in rows]
    lows = [row["low"] for row in rows]
    volumes = [row["volume"] for row in rows if row.get("volume") is not None]
    latest = rows[-1]
    close = latest["close"]
    ma5 = moving_average(closes, 5)
    ma10 = moving_average(closes, 10)
    ma20 = moving_average(closes, 20)
    ma60 = moving_average(closes, 60)
    volume_ma5 = average(volumes[-5:]) if len(volumes) >= 5 else None
    volume_ma20 = average(volumes[-20:]) if len(volumes) >= 20 else None
    latest_volume = latest.get("volume")
    volume_ratio_20 = latest_volume / volume_ma20 if latest_volume and volume_ma20 else None

    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = [a - b for a, b in zip(ema12[-len(ema26):], ema26)]
    signal_line = ema(macd_line, 9)
    macd_hist = macd_line[-1] - signal_line[-1] if signal_line else None
    prev_macd_hist = macd_line[-2] - signal_line[-2] if len(signal_line) >= 2 else None

    rsi14 = metrics.get("rsi14") or rsi(closes, 14)
    low9 = min(lows[-9:])
    high9 = max(highs[-9:])
    kdj_j = None
    if high9 > low9:
        rsv = (close - low9) / (high9 - low9) * 100
        # Lightweight KDJ approximation for daily monitoring.
        kdj_j = max(0.0, min(100.0, rsv))

    boll_mid = moving_average(closes, 20)
    boll_std = rolling_std(closes, 20)
    boll_pos = None
    if boll_mid is not None and boll_std is not None and boll_std > 0:
        boll_upper = boll_mid + 2 * boll_std
        boll_lower = boll_mid - 2 * boll_std
        boll_pos = (close - boll_lower) / (boll_upper - boll_lower)
    else:
        boll_upper = None
        boll_lower = None

    tr_values = []
    for idx, row in enumerate(rows):
        if idx == 0:
            tr_values.append(row["high"] - row["low"])
            continue
        prev_close = rows[idx - 1]["close"]
        tr_values.append(max(row["high"] - row["low"], abs(row["high"] - prev_close), abs(row["low"] - prev_close)))
    atr14 = average(tr_values[-14:]) if len(tr_values) >= 14 else None
    atr_pct = atr14 / close * 100 if atr14 and close else None

    support = min(lows[-20:])
    resistance = max(highs[-20:])
    distance_to_support = (close / support - 1) * 100 if support else None
    distance_to_resistance = (resistance / close - 1) * 100 if resistance else None
    high20 = max(highs[-20:])
    low20 = min(lows[-20:])
    range_pos = (close - low20) / (high20 - low20) if high20 > low20 else None
    candle_label = latest_candle_label(latest)
    patterns = detect_candlestick_patterns(rows)
    pattern_score = sum(item["score"] for item in patterns[-3:])

    score = 0
    score += 1 if ma20 is not None and close >= ma20 else -1
    score += 1 if ma5 is not None and ma10 is not None and ma5 >= ma10 else -1
    if ma60 is not None:
        score += 1 if close >= ma60 else -1
    if macd_hist is not None and prev_macd_hist is not None:
        score += 1 if macd_hist > 0 and macd_hist >= prev_macd_hist else -1 if macd_hist < 0 and macd_hist <= prev_macd_hist else 0
    if rsi14 is not None:
        score += 1 if 45 <= rsi14 <= 68 else -1 if rsi14 > 75 or rsi14 < 35 else 0
    if volume_ratio_20 is not None and metrics.get("change_rate") is not None:
        score += 1 if volume_ratio_20 >= 1.1 and metrics["change_rate"] > 0 else -1 if volume_ratio_20 >= 1.2 and metrics["change_rate"] < 0 else 0
    score += 1 if pattern_score >= 2 else -1 if pattern_score <= -2 else 0

    if score >= 4:
        signal = "偏强"
    elif score <= -3:
        signal = "偏弱"
    else:
        signal = "震荡观察"

    if ma5 is not None and ma10 is not None and ma20 is not None:
        if close > ma5 > ma10 > ma20:
            trend = "均线多头排列，价格处在短中期均线上方。"
        elif close >= ma20 and ma5 >= ma10:
            trend = "价格站上MA20，短线修复成立但多头排列未完全展开。"
        elif close < ma20:
            trend = "价格仍在MA20下方，反弹更偏修复而非趋势反转。"
        else:
            trend = "均线结构混合，方向仍需量价确认。"
    else:
        trend = "均线样本不足，暂不做趋势定性。"

    if macd_hist is not None and prev_macd_hist is not None:
        if macd_hist > 0 and macd_hist >= prev_macd_hist:
            momentum = "MACD柱体在零轴上方并扩张，动能改善。"
        elif macd_hist < 0 and macd_hist <= prev_macd_hist:
            momentum = "MACD柱体在零轴下方并走弱，动能偏弱。"
        else:
            momentum = "MACD动能未单边扩张，短线仍偏震荡。"
    else:
        momentum = "MACD样本不足。"

    if volume_ratio_20 is not None:
        if metrics.get("change_rate", 0) > 0 and volume_ratio_20 >= 1.1:
            volume_text = f"成交量约为20日均量的 {volume_ratio_20:.2f} 倍，上涨有量能配合。"
        elif metrics.get("change_rate", 0) > 0:
            volume_text = f"成交量约为20日均量的 {volume_ratio_20:.2f} 倍，上涨量能一般。"
        elif metrics.get("change_rate", 0) < 0 and volume_ratio_20 >= 1.1:
            volume_text = f"成交量约为20日均量的 {volume_ratio_20:.2f} 倍，下跌放量需防守。"
        else:
            volume_text = f"成交量约为20日均量的 {volume_ratio_20:.2f} 倍，量能未明显放大。"
    else:
        volume_text = "成交量样本不足。"

    if patterns:
        latest_pattern = patterns[-1]
        pattern_text = f"{latest_pattern['date']} 出现 `{latest_pattern['name']}`：{latest_pattern['note']}。"
    else:
        pattern_text = f"近5根K线未识别出强反转形态，最新K线为 `{candle_label}`。"

    support_text = f"20日支撑 {support:.2f}，距离现价 {distance_to_support:.2f}%；20日压力 {resistance:.2f}，距离现价 {distance_to_resistance:.2f}%。"
    risk = "若跌破20日支撑且资金转负，应降低策略级别；若放量突破20日压力，突破确认更有效。"
    summary = f"{trend} {volume_text}"

    return {
        "signal": signal,
        "score": score,
        "summary": summary,
        "trend": trend,
        "momentum": momentum,
        "volume": volume_text,
        "pattern": pattern_text,
        "support": support,
        "resistance": resistance,
        "distance_to_support_pct": distance_to_support,
        "distance_to_resistance_pct": distance_to_resistance,
        "range_position_20d": range_pos,
        "risk": risk,
        "indicators": {
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "ma60": ma60,
            "macd_hist": macd_hist,
            "rsi14": rsi14,
            "kdj_j": kdj_j,
            "bollinger_position": boll_pos,
            "bollinger_upper": boll_upper,
            "bollinger_lower": boll_lower,
            "atr14": atr14,
            "atr14_pct": atr_pct,
            "volume_ma5": volume_ma5,
            "volume_ma20": volume_ma20,
            "volume_ratio_20": volume_ratio_20,
        },
        "patterns": patterns,
    }


def classify_news(title: str) -> str:
    lower = title.lower()
    rules = [
        ("业绩", ("业绩", "利润", "营收", "财报", "earnings", "revenue", "profit")),
        ("公告", ("公告", "披露", "filing", "disclosure")),
        ("资本动作", ("回购", "增持", "减持", "分红", "派息", "配售", "buyback", "dividend", "stake")),
        ("评级", ("评级", "目标价", "上调", "下调", "rating", "target price", "upgrade", "downgrade")),
        ("舆情", ("谣言", "辟谣", "刑拘", "ai-generated", "fake", "rumor")),
        ("行业", ("行业", "高温", "消费", "汽车", "苹果", "机器人", "出口", "需求", "market", "demand")),
    ]
    for label, words in rules:
        if any(word in lower or word in title for word in words):
            return label
    return "其他"


def trend_label(close: float | None, ma5: float | None, ma10: float | None, ma20: float | None) -> str:
    if close is None:
        return "数据缺失"
    if ma5 and ma10 and ma20 and close > ma5 > ma10 > ma20:
        return "强势延续"
    if ma10 and ma20 and close > ma10 and close < ma20:
        return "短线修复"
    if ma20 and close > ma20:
        return "趋势修复"
    if ma5 and close < ma5:
        return "继续走弱"
    return "等待验证"


def strategy_class(tag: str) -> str:
    return {
        "顺势观察": "strategy-up",
        "修复确认": "strategy-repair",
        "防守等待": "strategy-risk",
        "中性观察": "strategy-neutral",
        "等待数据": "strategy-missing",
    }.get(tag, "strategy-neutral")


def change_class(change_rate: float | None) -> str:
    if change_rate is None:
        return "flat"
    if change_rate > 0:
        return "up"
    if change_rate < 0:
        return "down"
    return "flat"


def move_label(change_rate: float | None, volume_ratio: float | None) -> str:
    if change_rate is None:
        return "数据缺失"
    abs_change = abs(change_rate)
    prefix = "大涨" if change_rate >= 5 else "大跌" if change_rate <= -5 else "小幅波动"
    if change_rate >= 9.8:
        prefix = "涨停或接近涨停"
    if change_rate <= -9.8:
        prefix = "跌停或接近跌停"
    if volume_ratio is not None and volume_ratio >= 1.5:
        return f"{prefix}，放量"
    if volume_ratio is not None and volume_ratio <= 0.8:
        return f"{prefix}，缩量"
    return prefix


def sparkline_svg(values: list[float], width: int = 220, height: int = 52) -> str:
    if len(values) < 2:
        return ""
    lo = min(values)
    hi = max(values)
    span = hi - lo or 1.0
    step = width / (len(values) - 1)
    points = []
    for idx, value in enumerate(values):
        x = idx * step
        y = height - ((value - lo) / span * (height - 8) + 4)
        points.append(f"{x:.1f},{y:.1f}")
    line_color = "#d43838" if values[-1] >= values[0] else "#16805a"
    return (
        f'<svg class="sparkline" viewBox="0 0 {width} {height}" role="img" aria-label="近30日走势">'
        f'<polyline points="{" ".join(points)}" fill="none" stroke="{line_color}" stroke-width="3" '
        'stroke-linecap="round" stroke-linejoin="round" />'
        "</svg>"
    )


def observe_points(close: float | None, ma10: float | None, ma20: float | None, low: float | None) -> str:
    if close is None:
        return "等待补齐行情数据"
    points: list[str] = []
    if ma10 is not None:
        points.append(f"MA10 {ma10:.2f}")
    if ma20 is not None:
        points.append(f"MA20 {ma20:.2f}")
    if low is not None:
        points.append(f"今日低点 {low:.2f}")
    if not points:
        return "观察成交量与收盘位置"
    return "；".join(points)


def tomorrow_strategy(metrics: dict[str, Any], labels: dict[str, str]) -> dict[str, str]:
    close = metrics.get("close")
    high = metrics.get("high")
    low = metrics.get("low")
    ma10 = metrics.get("ma10")
    ma20 = metrics.get("ma20")
    change_rate = metrics.get("change_rate")
    capital_flow = metrics.get("capital_flow")

    if close is None:
        return {
            "tag": "等待数据",
            "plan": "行情数据不完整，明日先补齐收盘价、均线、资金流后再判断。",
            "breakout": "NA",
            "pullback": "NA",
            "invalid": "NA",
        }

    flow_ok = capital_flow is not None and capital_flow > 0
    strong_day = change_rate is not None and change_rate >= 5
    weak_day = change_rate is not None and change_rate <= -3
    above_ma20 = ma20 is not None and close >= ma20
    above_ma10 = ma10 is not None and close >= ma10

    if labels.get("trend") in ("趋势修复", "强势延续") and flow_ok:
        tag = "顺势观察"
        plan = "不追高定性，优先看能否在均线上方缩量回踩并保持资金不大幅流出。"
    elif labels.get("trend") == "短线修复" or (strong_day and above_ma10):
        tag = "修复确认"
        plan = "核心看反弹能否从事件修复升级为趋势修复；先看MA20压力，回踩不破MA10才算健康。"
    elif labels.get("trend") == "继续走弱" or weak_day:
        tag = "防守等待"
        plan = "不急于判断见底，先看是否止跌并收回MA10；若继续放量下破低点，维持风险优先。"
    else:
        tag = "中性观察"
        plan = "等待方向选择，重点看收盘位置相对MA10/MA20和资金流是否同步。"

    if ma20 is not None and close < ma20:
        breakout = f"放量站上 MA20 {ma20:.2f}，才视为趋势修复增强。"
    elif high is not None:
        breakout = f"放量突破今日高点 {high:.2f}，强势延续概率提高。"
    else:
        breakout = "放量站稳MA20或前高。"

    if ma10 is not None:
        pullback = f"回踩 MA10 {ma10:.2f} 附近不破，且成交缩量，可视为健康回踩。"
    elif ma20 is not None:
        pullback = f"回踩 MA20 {ma20:.2f} 附近不破，观察承接。"
    else:
        pullback = "回踩时观察是否缩量和是否守住当日均价区域。"

    if low is not None:
        invalid = f"跌破今日低点 {low:.2f} 且资金继续流出，则明日策略失效，转为防守。"
    elif ma10 is not None:
        invalid = f"跌破 MA10 {ma10:.2f} 且放量，则策略失效。"
    else:
        invalid = "放量下跌且资金净流出，则策略失效。"

    return {"tag": tag, "plan": plan, "breakout": breakout, "pullback": pullback, "invalid": invalid}


def collect_symbol(symbol_cfg: dict[str, Any], run_dir: Path, cache_dir: Path) -> dict[str, Any]:
    symbol = symbol_cfg["symbol"]
    symbol_dir = run_dir / safe_filename(symbol)
    symbol_dir.mkdir(parents=True, exist_ok=True)

    calls = {
        "quote": ["quote", symbol, "--format", "json"],
        "calc_index": [
            "calc-index",
            symbol,
            "--fields",
            "last_done,change_value,change_rate,vol,turnover,ytd_change_rate,turnover_rate,mktcap,capital_flow,amplitude,volume_ratio,pe,pb,dps_rate,five_day_change_rate,ten_day_change_rate,half_year_change_rate",
            "--format",
            "json",
        ],
        "kline": ["kline", symbol, "--period", "day", "--count", "60", "--adjust", "forward", "--format", "json"],
        "capital": ["capital", symbol, "--format", "json"],
        "news": ["news", symbol, "--count", "10", "--format", "json", "--lang", "zh-CN"],
        "filing": ["filing", symbol, "--count", "10", "--format", "json", "--lang", "zh-CN"],
    }

    raw: dict[str, Any] = {}
    errors: list[str] = []
    used_cache = False
    for key, args in calls.items():
        result = run_longbridge(args)
        if result.ok:
            raw[key] = result.data
            write_json(symbol_dir / f"{key}.json", {"ok": True, "data": result.data, "stderr": result.stderr, "command": result.command})
            if result.data not in (None, [], {}):
                save_cache(cache_dir, symbol, key, result.data)
            continue

        cached = load_cache(cache_dir, symbol, key)
        if cached is not None:
            cached_data, cached_at = cached
            raw[key] = cached_data
            used_cache = True
            write_json(
                symbol_dir / f"{key}.json",
                {
                    "ok": False,
                    "data": cached_data,
                    "stderr": result.stderr,
                    "command": result.command,
                    "fallback": "cache",
                    "cached_at": cached_at,
                },
            )
            errors.append(f"{key}: {result.stderr}；使用缓存 {cached_at}")
            continue

        raw[key] = None
        write_json(symbol_dir / f"{key}.json", {"ok": False, "data": None, "stderr": result.stderr, "command": result.command})
        errors.append(f"{key}: {result.stderr}")

    quote = first_item(raw.get("quote"))
    calc = first_item(raw.get("calc_index"))
    capital = first_item(raw.get("capital"))
    kline_rows = raw.get("kline") if isinstance(raw.get("kline"), list) else []
    parsed_kline_rows = parse_kline_rows(kline_rows)
    news_rows = raw.get("news") if isinstance(raw.get("news"), list) else []
    filing_rows = raw.get("filing") if isinstance(raw.get("filing"), list) else []

    closes = [row["close"] for row in parsed_kline_rows]
    latest_k = first_item(kline_rows[-1:]) if kline_rows else {}

    close = to_float(quote.get("last")) or to_float(calc.get("last_done")) or to_float(latest_k.get("close"))
    prev_close = to_float(quote.get("prev_close"))
    open_price = to_float(quote.get("open")) or to_float(latest_k.get("open"))
    high = to_float(quote.get("high")) or to_float(latest_k.get("high"))
    low = to_float(quote.get("low")) or to_float(latest_k.get("low"))
    change_rate = to_float(calc.get("change_rate"))
    if change_rate is None and close is not None and prev_close:
        change_rate = (close / prev_close - 1) * 100

    ma5 = moving_average(closes, 5)
    ma10 = moving_average(closes, 10)
    ma20 = moving_average(closes, 20)
    rsi14 = rsi(closes, 14)
    volume_ratio = to_float(calc.get("volume_ratio"))
    capital_flow = to_float(calc.get("capital_flow"))
    turnover = to_float(calc.get("turnover")) or to_float(quote.get("turnover"))
    mktcap = to_float(calc.get("mktcap"))
    pe = to_float(calc.get("pe"))
    pb = to_float(calc.get("pb"))

    bucketed_news: dict[str, list[str]] = {}
    for item in news_rows[:8]:
        title = item.get("title", "") if isinstance(item, dict) else ""
        if not title:
            continue
        bucketed_news.setdefault(classify_news(title), []).append(title)

    filing_titles = []
    for item in filing_rows[:6]:
        if isinstance(item, dict):
            filing_titles.append(item.get("title") or item.get("file_name") or "")
    filing_titles = [title for title in filing_titles if title]

    labels = {
        "move": move_label(change_rate, volume_ratio),
        "trend": trend_label(close, ma5, ma10, ma20),
        "observe": observe_points(close, ma10, ma20, low),
    }
    metrics = {
        "trade_date": local_date(latest_k.get("time")),
        "close": close,
        "prev_close": prev_close,
        "open": open_price,
        "high": high,
        "low": low,
        "change_rate": change_rate,
        "turnover": turnover,
        "volume_ratio": volume_ratio,
        "capital_flow": capital_flow,
        "mktcap": mktcap,
        "pe": pe,
        "pb": pb,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "rsi14": rsi14,
    }
    kline_analysis = kline_detailed_analysis(parsed_kline_rows, metrics)

    return {
        "config": symbol_cfg,
        "errors": errors,
        "used_cache": used_cache,
        "metrics": metrics,
        "labels": labels,
        "strategy": tomorrow_strategy(metrics, labels),
        "sparkline": closes[-30:],
        "kline_rows": parsed_kline_rows,
        "kline_analysis": kline_analysis,
        "news": bucketed_news,
        "filings": filing_titles,
        "capital": capital,
    }


def validate_latest_trade_date(collected: list[dict[str, Any]], report_date: str) -> dict[str, Any]:
    expected_trade_date = report_trade_date(report_date)
    details: list[str] = []
    for item in collected:
        symbol = item["config"]["symbol"]
        trade_date = item["metrics"].get("trade_date")
        if trade_date != expected_trade_date:
            details.append(f"{symbol}: 数据日为 {trade_date or 'NA'}，不等于报告日 {expected_trade_date}")
            continue
        if item.get("used_cache"):
            details.append(f"{symbol}: 本次调用使用缓存，未拿到 {expected_trade_date} 实时数据")
    return {"ok": not details, "expected_trade_date": expected_trade_date, "details": details}


def load_summary_input(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"summary input must be a list: {path}")
    return payload


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def init_history_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=DELETE;
        CREATE TABLE IF NOT EXISTS report_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            valid_count INTEGER NOT NULL,
            expected_trade_date TEXT,
            data_dates_json TEXT NOT NULL,
            watchlist_json TEXT NOT NULL,
            run_dir TEXT NOT NULL,
            report_path TEXT,
            dashboard_path TEXT,
            error_summary TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_report_runs_date ON report_runs(report_date, generated_at);

        CREATE TABLE IF NOT EXISTS stock_daily_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES report_runs(id) ON DELETE CASCADE,
            report_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            trade_date TEXT,
            close REAL,
            change_rate REAL,
            turnover REAL,
            capital_flow REAL,
            volume_ratio REAL,
            pe REAL,
            pb REAL,
            trend_label TEXT,
            move_label TEXT,
            strategy_tag TEXT,
            kline_signal TEXT,
            kline_score REAL,
            used_cache INTEGER NOT NULL DEFAULT 0,
            metrics_json TEXT NOT NULL,
            labels_json TEXT NOT NULL,
            strategy_json TEXT NOT NULL,
            kline_analysis_json TEXT NOT NULL,
            news_json TEXT NOT NULL,
            filings_json TEXT NOT NULL,
            errors_json TEXT NOT NULL,
            UNIQUE(run_id, symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_date ON stock_daily_snapshots(symbol, trade_date);

        CREATE TABLE IF NOT EXISTS kline_bars (
            symbol TEXT NOT NULL,
            period TEXT NOT NULL,
            adjust TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL,
            turnover REAL,
            source_report_date TEXT NOT NULL,
            PRIMARY KEY(symbol, period, adjust, timestamp)
        );
        CREATE INDEX IF NOT EXISTS idx_kline_bars_symbol_trade_date ON kline_bars(symbol, trade_date);

        CREATE TABLE IF NOT EXISTS kline_analysis_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES report_runs(id) ON DELETE CASCADE,
            report_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            trade_date TEXT,
            signal TEXT,
            score REAL,
            summary TEXT,
            trend TEXT,
            momentum TEXT,
            volume TEXT,
            pattern TEXT,
            support REAL,
            resistance REAL,
            risk TEXT,
            analysis_json TEXT NOT NULL,
            UNIQUE(run_id, symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_kline_analysis_symbol_date ON kline_analysis_history(symbol, trade_date);
        """
    )


def persist_history(
    db_path: Path,
    watchlist: dict[str, Any],
    collected: list[dict[str, Any]],
    report_date: str,
    run_dir: Path,
    status: str,
    error_summary: str = "",
    report_path: Path | None = None,
    dashboard_path: Path | None = None,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = now_bj().strftime("%Y-%m-%d %H:%M:%S CST")
    data_dates = sorted({item["metrics"].get("trade_date") for item in collected if item["metrics"].get("trade_date")})
    valid_count = sum(1 for item in collected if item["metrics"].get("close") is not None)
    with sqlite3.connect(db_path) as conn:
        init_history_db(conn)
        cursor = conn.execute(
            """
            INSERT INTO report_runs (
                report_date, generated_at, status, valid_count, expected_trade_date,
                data_dates_json, watchlist_json, run_dir, report_path, dashboard_path, error_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_date,
                generated_at,
                status,
                valid_count,
                report_trade_date(report_date),
                json_text(data_dates),
                json_text(watchlist),
                str(run_dir),
                str(report_path) if report_path else None,
                str(dashboard_path) if dashboard_path else None,
                error_summary,
            ),
        )
        run_id = int(cursor.lastrowid)

        for item in collected:
            cfg = item["config"]
            metrics = item.get("metrics", {})
            labels = item.get("labels", {})
            strategy = item.get("strategy", {})
            analysis = item.get("kline_analysis", {})
            conn.execute(
                """
                INSERT INTO stock_daily_snapshots (
                    run_id, report_date, symbol, name, trade_date, close, change_rate, turnover,
                    capital_flow, volume_ratio, pe, pb, trend_label, move_label, strategy_tag,
                    kline_signal, kline_score, used_cache, metrics_json, labels_json, strategy_json,
                    kline_analysis_json, news_json, filings_json, errors_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    report_date,
                    cfg.get("symbol"),
                    cfg.get("name") or cfg.get("short_name") or cfg.get("symbol"),
                    metrics.get("trade_date"),
                    metrics.get("close"),
                    metrics.get("change_rate"),
                    metrics.get("turnover"),
                    metrics.get("capital_flow"),
                    metrics.get("volume_ratio"),
                    metrics.get("pe"),
                    metrics.get("pb"),
                    labels.get("trend"),
                    labels.get("move"),
                    strategy.get("tag"),
                    analysis.get("signal"),
                    analysis.get("score"),
                    1 if item.get("used_cache") else 0,
                    json_text(metrics),
                    json_text(labels),
                    json_text(strategy),
                    json_text(analysis),
                    json_text(item.get("news", {})),
                    json_text(item.get("filings", [])),
                    json_text(item.get("errors", [])),
                ),
            )
            conn.execute(
                """
                INSERT INTO kline_analysis_history (
                    run_id, report_date, symbol, trade_date, signal, score, summary,
                    trend, momentum, volume, pattern, support, resistance, risk, analysis_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    report_date,
                    cfg.get("symbol"),
                    metrics.get("trade_date"),
                    analysis.get("signal"),
                    analysis.get("score"),
                    analysis.get("summary"),
                    analysis.get("trend"),
                    analysis.get("momentum"),
                    analysis.get("volume"),
                    analysis.get("pattern"),
                    analysis.get("support"),
                    analysis.get("resistance"),
                    analysis.get("risk"),
                    json_text(analysis),
                ),
            )
            for row in item.get("kline_rows", []):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO kline_bars (
                        symbol, period, adjust, trade_date, timestamp, open, high, low,
                        close, volume, turnover, source_report_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cfg.get("symbol"),
                        "day",
                        "forward",
                        row.get("trade_date") or local_date(row.get("time")),
                        row.get("time") or row.get("trade_date"),
                        row.get("open"),
                        row.get("high"),
                        row.get("low"),
                        row.get("close"),
                        row.get("volume"),
                        row.get("turnover"),
                        report_date,
                    ),
                )


def render_report(watchlist: dict[str, Any], collected: list[dict[str, Any]], as_of: str, run_dir: Path) -> str:
    generated_at = now_bj().strftime("%Y-%m-%d %H:%M:%S CST")
    data_dates = sorted({item["metrics"].get("trade_date") for item in collected if item["metrics"].get("trade_date")})
    data_as_of = "、".join(data_dates) if data_dates else "NA"
    lines: list[str] = []
    lines.append(f"# 核心股票日度监控报告 {as_of}")
    lines.append("")
    lines.append(f"生成时间：{generated_at}")
    lines.append(f"数据截至交易日：{data_as_of}")
    lines.append("")
    lines.append("数据来源：Longbridge Securities。报告只做研究监控，不构成投资建议。")
    lines.append("")
    lines.append("## 明日行动面板")
    lines.append("")
    for item in collected:
        cfg = item["config"]
        s = item["strategy"]
        m = item["metrics"]
        lines.append(f"- **{cfg['short_name']}** `{cfg['symbol']}`：`{s['tag']}`，收盘 {num(m['close'])}，{pct(m['change_rate'])}。{s['plan']}")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("| 股票 | 数据日 | 收盘 | 涨跌幅 | 成交额 | 资金流 | PE | PB | 趋势 | 今日性质 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---|---|")
    for item in collected:
        cfg = item["config"]
        m = item["metrics"]
        lines.append(
            "| {name} `{symbol}` | {trade_date} | {close} | {chg} | {turnover} | {flow} | {pe} | {pb} | {trend} | {move} |".format(
                name=cfg["short_name"],
                symbol=cfg["symbol"],
                trade_date=m.get("trade_date") or "NA",
                close=num(m["close"]),
                chg=pct(m["change_rate"]),
                turnover=money_cny(m["turnover"]),
                flow=money_cny(m["capital_flow"]),
                pe=num(m["pe"]),
                pb=num(m["pb"]),
                trend=item["labels"]["trend"],
                move=item["labels"]["move"],
            )
        )
    lines.append("")

    movers = sorted(collected, key=lambda x: abs(x["metrics"].get("change_rate") or 0), reverse=True)
    lines.append("## 今日异动排序")
    lines.append("")
    for idx, item in enumerate(movers, 1):
        cfg = item["config"]
        m = item["metrics"]
        lines.append(f"{idx}. {cfg['name']} `{cfg['symbol']}`：{pct(m['change_rate'])}，{item['labels']['move']}，趋势标签 `{item['labels']['trend']}`。")
    lines.append("")

    lines.append("## 明日策略总表")
    lines.append("")
    lines.append("| 股票 | 策略标签 | 突破确认 | 回踩观察 | 失效条件 |")
    lines.append("|---|---|---|---|---|")
    for item in collected:
        cfg = item["config"]
        s = item["strategy"]
        lines.append(
            f"| {cfg['short_name']} `{cfg['symbol']}` | {s['tag']} | {s['breakout']} | {s['pullback']} | {s['invalid']} |"
        )
    lines.append("")

    lines.append("## 单股卡片")
    lines.append("")
    for item in collected:
        cfg = item["config"]
        m = item["metrics"]
        lines.append(f"### {cfg['name']} `{cfg['symbol']}`")
        lines.append("")
        lines.append(f"- 业务类型：{cfg['business_type']}")
        lines.append(f"- 数据日：{m.get('trade_date') or 'NA'}")
        lines.append(
            f"- 价格行为：收盘 {num(m['close'])}，涨跌 {pct(m['change_rate'])}；开盘 {num(m['open'])}，最高 {num(m['high'])}，最低 {num(m['low'])}。"
        )
        lines.append(
            f"- 趋势位置：MA5 {num(m['ma5'])}，MA10 {num(m['ma10'])}，MA20 {num(m['ma20'])}，RSI14 {num(m['rsi14'])}；结论 `{item['labels']['trend']}`。"
        )
        lines.append(
            f"- 资金与估值：成交额 {money_cny(m['turnover'])}，量比 {num(m['volume_ratio'])}，资金净流 {money_cny(m['capital_flow'])}，PE {num(m['pe'])}，PB {num(m['pb'])}，市值 {money_cny(m['mktcap'])}。"
        )
        if item["news"]:
            chunks = []
            for bucket, titles in item["news"].items():
                chunks.append(f"{bucket}: {titles[0]}")
            lines.append(f"- 今日催化：{'；'.join(chunks[:4])}")
        else:
            lines.append("- 今日催化：Longbridge 最近新闻未显示明确催化，需结合板块和公告继续验证。")
        if item["filings"]:
            lines.append(f"- 最新公告：{item['filings'][0]}")
        else:
            lines.append("- 最新公告：未抓到最新公告。")
        lines.append(f"- 明日观察：{item['labels']['observe']}")
        s = item["strategy"]
        lines.append(f"- 明日策略：`{s['tag']}`。{s['plan']} 突破看：{s['breakout']} 回踩看：{s['pullback']} 失效看：{s['invalid']}")
        k = item.get("kline_analysis", {})
        indicators = k.get("indicators", {}) if isinstance(k.get("indicators"), dict) else {}
        lines.append(
            f"- K线详析：综合 `{k.get('signal', 'NA')}`，评分 {k.get('score', 0):+}；"
            f"{k.get('summary', 'NA')}"
        )
        lines.append(
            f"- K线结构：{k.get('trend', 'NA')} {k.get('momentum', 'NA')} {k.get('volume', 'NA')}"
        )
        lines.append(
            f"- 形态/支撑压力：{k.get('pattern', 'NA')} 支撑 {num(k.get('support'))}，压力 {num(k.get('resistance'))}，"
            f"ATR14 {num(indicators.get('atr14'))}（{num(indicators.get('atr14_pct'))}%）。"
        )
        lines.append(f"- K线风险：{k.get('risk', 'NA')}")
        if item["errors"]:
            lines.append(f"- 数据缺口：{'；'.join(item['errors'][:2])}")
        lines.append("")

    lines.append("## 框架提醒")
    lines.append("")
    lines.append("- 大涨大跌先排除除权除息、停复牌、送转、指数调仓等机械因素。")
    lines.append("- 价格变化要和成交额、量比、资金流、新闻/公告一起看。")
    lines.append("- 日报只更新监控结论；是否买卖需要另行结合仓位、成本和风险承受能力。")
    lines.append("")
    lines.append("## 原始数据")
    lines.append("")
    lines.append(f"- 本次原始 JSON 目录：`{run_dir}`")
    lines.append(f"- Watchlist：`{DEFAULT_WATCHLIST}`")
    return "\n".join(lines) + "\n"


def render_html_dashboard(watchlist: dict[str, Any], collected: list[dict[str, Any]], as_of: str, run_dir: Path) -> str:
    generated_at = now_bj().strftime("%Y-%m-%d %H:%M:%S CST")
    data_dates = sorted({item["metrics"].get("trade_date") for item in collected if item["metrics"].get("trade_date")})
    data_as_of = "、".join(data_dates) if data_dates else "NA"
    movers = sorted(collected, key=lambda x: abs(x["metrics"].get("change_rate") or 0), reverse=True)
    leader = movers[0] if movers else None
    risk_items = [item for item in collected if item["strategy"]["tag"] == "防守等待" or (item["metrics"].get("capital_flow") or 0) < 0]

    def news_chips(item: dict[str, Any]) -> str:
        chips = []
        for bucket, titles in item["news"].items():
            if not titles:
                continue
            chips.append(f'<span class="chip">{html_escape(bucket)} · {html_escape(titles[0][:34])}</span>')
        if not chips:
            return '<span class="muted">无明确催化</span>'
        return "".join(chips[:3])

    cards = []
    for item in collected:
        cfg = item["config"]
        m = item["metrics"]
        s = item["strategy"]
        k = item.get("kline_analysis", {})
        chg_cls = change_class(m.get("change_rate"))
        cards.append(
            f"""
            <section class="stock-card {strategy_class(s['tag'])}">
              <div class="card-head">
                <div>
                  <div class="stock-name">{html_escape(cfg['short_name'])}</div>
                  <div class="symbol">{html_escape(cfg['symbol'])} · {html_escape(cfg['sector'])}</div>
                </div>
                <div class="price-block">
                  <div class="price">{num(m.get('close'))}</div>
                  <div class="change {chg_cls}">{pct(m.get('change_rate'))}</div>
                </div>
              </div>
              <div class="strategy-pill">{html_escape(s['tag'])}</div>
              {sparkline_svg(item.get('sparkline') or [])}
              <div class="metric-grid">
                <div><span>MA10</span><strong>{num(m.get('ma10'))}</strong></div>
                <div><span>MA20</span><strong>{num(m.get('ma20'))}</strong></div>
                <div><span>资金</span><strong>{money_cny(m.get('capital_flow'))}</strong></div>
                <div><span>成交额</span><strong>{money_cny(m.get('turnover'))}</strong></div>
                <div><span>K线信号</span><strong>{html_escape(k.get('signal', 'NA'))}</strong></div>
                <div><span>K线评分</span><strong>{html_escape(k.get('score', 'NA'))}</strong></div>
              </div>
              <div class="mini-title">明日主线</div>
              <p class="strategy-text">{html_escape(s['plan'])}</p>
              <div class="mini-title">K线判断</div>
              <p class="strategy-text">{html_escape(k.get('summary', 'NA'))}</p>
              <div class="news-row">{news_chips(item)}</div>
            </section>
            """
        )

    strategy_rows = []
    for item in collected:
        cfg = item["config"]
        s = item["strategy"]
        strategy_rows.append(
            f"""
            <tr>
              <td><strong>{html_escape(cfg['short_name'])}</strong><br><span>{html_escape(cfg['symbol'])}</span></td>
              <td><span class="tag {strategy_class(s['tag'])}">{html_escape(s['tag'])}</span></td>
              <td>{html_escape(s['breakout'])}</td>
              <td>{html_escape(s['pullback'])}</td>
              <td>{html_escape(s['invalid'])}</td>
            </tr>
            """
        )

    detail_sections = []
    for item in collected:
        cfg = item["config"]
        m = item["metrics"]
        s = item["strategy"]
        k = item.get("kline_analysis", {})
        indicators = k.get("indicators", {}) if isinstance(k.get("indicators"), dict) else {}
        detail_sections.append(
            f"""
            <section class="detail-row">
              <div>
                <h3>{html_escape(cfg['name'])} <span>{html_escape(cfg['symbol'])}</span></h3>
                <p>{html_escape(cfg['business_type'])}</p>
              </div>
              <ul>
                <li>价格：收盘 {num(m.get('close'))}，开 {num(m.get('open'))}，高 {num(m.get('high'))}，低 {num(m.get('low'))}</li>
                <li>趋势：MA5 {num(m.get('ma5'))}，MA10 {num(m.get('ma10'))}，MA20 {num(m.get('ma20'))}，RSI14 {num(m.get('rsi14'))}</li>
                <li>估值：PE {num(m.get('pe'))}，PB {num(m.get('pb'))}，市值 {money_cny(m.get('mktcap'))}</li>
                <li>策略：{html_escape(s['plan'])}</li>
                <li>K线：{html_escape(k.get('summary', 'NA'))}</li>
                <li>结构：{html_escape(k.get('trend', 'NA'))} {html_escape(k.get('momentum', 'NA'))}</li>
                <li>形态/风控：{html_escape(k.get('pattern', 'NA'))} 支撑 {num(k.get('support'))}，压力 {num(k.get('resistance'))}，ATR14 {num(indicators.get('atr14'))}</li>
              </ul>
            </section>
            """
        )

    leader_text = "NA"
    if leader:
        leader_text = f"{leader['config']['short_name']} {pct(leader['metrics'].get('change_rate'))}"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>核心股票日度监控看板 {html_escape(as_of)}</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #172033;
      --muted: #667085;
      --line: #d9dee8;
      --red: #d43838;
      --green: #16805a;
      --blue: #2563eb;
      --amber: #b7791f;
      --purple: #6d28d9;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      line-height: 1.5;
    }}
    .page {{ max-width: 1320px; margin: 0 auto; padding: 28px 28px 44px; }}
    header {{ display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; margin-bottom: 22px; }}
    h1 {{ margin: 0 0 8px; font-size: 30px; line-height: 1.2; }}
    .sub {{ color: var(--muted); font-size: 14px; }}
    .source {{ text-align: right; color: var(--muted); font-size: 13px; }}
    .kpi-row {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }}
    .kpi {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; }}
    .kpi span {{ display: block; color: var(--muted); font-size: 13px; }}
    .kpi strong {{ display: block; font-size: 22px; margin-top: 4px; }}
    .card-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 14px; margin: 18px 0 22px; }}
    .stock-card {{ background: var(--panel); border: 1px solid var(--line); border-top: 5px solid var(--muted); border-radius: 8px; padding: 16px; min-height: 330px; }}
    .stock-card.strategy-up {{ border-top-color: var(--red); }}
    .stock-card.strategy-repair {{ border-top-color: var(--amber); }}
    .stock-card.strategy-risk {{ border-top-color: var(--green); }}
    .stock-card.strategy-neutral {{ border-top-color: var(--blue); }}
    .card-head {{ display: flex; justify-content: space-between; gap: 12px; }}
    .stock-name {{ font-size: 21px; font-weight: 750; }}
    .symbol {{ color: var(--muted); font-size: 12px; margin-top: 2px; }}
    .price-block {{ text-align: right; }}
    .price {{ font-size: 24px; font-weight: 760; }}
    .change {{ font-size: 15px; font-weight: 700; }}
    .up {{ color: var(--red); }}
    .down {{ color: var(--green); }}
    .flat {{ color: var(--muted); }}
    .strategy-pill, .tag {{ display: inline-block; margin: 12px 0 8px; padding: 4px 9px; border-radius: 6px; font-size: 13px; font-weight: 700; background: #eef2ff; color: #334155; }}
    .tag {{ margin: 0; white-space: nowrap; }}
    .tag.strategy-up, .strategy-pill.strategy-up {{ background: #fee2e2; color: #991b1b; }}
    .tag.strategy-repair {{ background: #fef3c7; color: #92400e; }}
    .tag.strategy-risk {{ background: #dcfce7; color: #166534; }}
    .tag.strategy-neutral {{ background: #dbeafe; color: #1d4ed8; }}
    .sparkline {{ width: 100%; height: 54px; margin: 8px 0 12px; background: #f8fafc; border: 1px solid #edf1f7; border-radius: 6px; }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 12px 0; }}
    .metric-grid div {{ border: 1px solid #edf1f7; border-radius: 6px; padding: 8px; }}
    .metric-grid span {{ display: block; color: var(--muted); font-size: 12px; }}
    .metric-grid strong {{ font-size: 15px; }}
    .mini-title {{ font-size: 13px; color: var(--muted); margin-top: 10px; }}
    .strategy-text {{ margin: 4px 0 10px; font-size: 14px; }}
    .news-row {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .chip {{ display: inline-block; border: 1px solid #e5e7eb; background: #f8fafc; border-radius: 6px; padding: 3px 7px; color: #475569; font-size: 12px; max-width: 100%; }}
    section.panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; margin-top: 16px; }}
    h2 {{ margin: 0 0 14px; font-size: 20px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 11px 10px; text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 700; background: #f8fafc; }}
    td span {{ color: var(--muted); font-size: 12px; }}
    .detail-row {{ display: grid; grid-template-columns: 260px 1fr; gap: 18px; padding: 14px 0; border-top: 1px solid #e5e7eb; }}
    .detail-row:first-of-type {{ border-top: 0; }}
    .detail-row h3 {{ margin: 0 0 4px; font-size: 18px; }}
    .detail-row h3 span {{ color: var(--muted); font-size: 13px; }}
    .detail-row p {{ margin: 0; color: var(--muted); }}
    .detail-row ul {{ margin: 0; padding-left: 18px; }}
    .footer {{ color: var(--muted); font-size: 13px; margin-top: 18px; }}
    @media (max-width: 1100px) {{
      .card-grid, .kpi-row {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      header {{ flex-direction: column; }}
      .source {{ text-align: left; }}
    }}
    @media (max-width: 720px) {{
      .page {{ padding: 18px; }}
      .card-grid, .kpi-row, .detail-row {{ grid-template-columns: 1fr; }}
      table {{ font-size: 13px; }}
      th, td {{ padding: 9px 7px; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <header>
      <div>
        <h1>核心股票日度监控看板</h1>
        <div class="sub">报告日 {html_escape(as_of)} · 数据截至交易日 {html_escape(data_as_of)}</div>
      </div>
      <div class="source">生成时间 {html_escape(generated_at)}<br>数据来源：Longbridge Securities</div>
    </header>

    <div class="kpi-row">
      <div class="kpi"><span>今日最大异动</span><strong>{html_escape(leader_text)}</strong></div>
      <div class="kpi"><span>防守/资金负向</span><strong>{len(risk_items)} 只</strong></div>
      <div class="kpi"><span>监控股票</span><strong>{len(collected)} 只</strong></div>
      <div class="kpi"><span>报告定位</span><strong>明日策略</strong></div>
    </div>

    <div class="card-grid">
      {"".join(cards)}
    </div>

    <section class="panel">
      <h2>明日策略矩阵</h2>
      <table>
        <thead>
          <tr><th>股票</th><th>策略</th><th>突破确认</th><th>回踩观察</th><th>失效条件</th></tr>
        </thead>
        <tbody>
          {"".join(strategy_rows)}
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h2>单股证据摘要</h2>
      {"".join(detail_sections)}
    </section>

    <div class="footer">原始 JSON：{html_escape(run_dir)}。本报告只做研究监控，不构成投资建议。</div>
  </main>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate core-stock daily monitoring report.")
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    parser.add_argument("--date", default=now_bj().strftime("%Y%m%d"), help="Report date in YYYYMMDD.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--history-db", default=str(DEFAULT_HISTORY_DB))
    parser.add_argument("--summary-input", help="Use a prebuilt collected-summary JSON instead of fetching data.")
    args = parser.parse_args()

    watchlist_path = Path(args.watchlist)
    if not watchlist_path.exists():
        print(f"watchlist not found: {watchlist_path}", file=sys.stderr)
        return 2
    watchlist = json.loads(watchlist_path.read_text(encoding="utf-8"))

    run_dir = Path(args.data_dir) / args.date
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "watchlist.snapshot.json", watchlist)

    if args.summary_input:
        collected = load_summary_input(Path(args.summary_input))
    else:
        cache_dir = Path(args.cache_dir)
        collected = [collect_symbol(symbol_cfg, run_dir, cache_dir) for symbol_cfg in watchlist["symbols"]]
    write_json(run_dir / "summary.json", collected)

    valid_count = sum(1 for item in collected if item["metrics"].get("close") is not None)
    freshness = validate_latest_trade_date(collected, args.date)
    failure_path = run_dir / "failure.md"
    if valid_count == 0 or not freshness["ok"]:
        error_summary = "；".join(freshness["details"])
        failure_lines = [
            f"# 核心股票日度监控报告生成失败 {args.date}",
            "",
            f"生成时间：{now_bj().strftime('%Y-%m-%d %H:%M:%S CST')}",
            "",
            "未拿到报告日对应的最新实时数据，为避免覆盖上一份有效日报，本次不生成 Markdown/HTML 报告。",
            "",
            "## 错误摘要",
            "",
        ]
        if valid_count == 0:
            failure_lines.append("- Longbridge 核心行情数据全部获取失败。")
        for detail in freshness["details"]:
            failure_lines.append(f"- {detail}")
        for item in collected:
            cfg = item["config"]
            extra = "；".join(item["errors"][:3]) or "未知错误"
            failure_lines.append(f"- {cfg['name']} `{cfg['symbol']}`：{extra}")
        failure_lines.append("")
        failure_lines.append(f"原始错误数据目录：`{run_dir}`")
        failure_path.write_text("\n".join(failure_lines) + "\n", encoding="utf-8")
        persist_history(
            Path(args.history_db),
            watchlist,
            collected,
            args.date,
            run_dir,
            "failed",
            error_summary=error_summary,
        )
        print(f"failed: latest realtime data unavailable; see {failure_path}", file=sys.stderr)
        return 4

    if failure_path.exists():
        failure_path.unlink()

    report = render_report(watchlist, collected, args.date, run_dir)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{args.date}_core_four_daily_report.md"
    report_path.write_text(report, encoding="utf-8")
    latest_path = report_dir / "latest_core_four_daily_report.md"
    shutil.copyfile(report_path, latest_path)

    html = render_html_dashboard(watchlist, collected, args.date, run_dir)
    html_path = report_dir / f"{args.date}_core_four_daily_dashboard.html"
    html_path.write_text(html, encoding="utf-8")
    latest_html_path = report_dir / "latest_core_four_daily_dashboard.html"
    shutil.copyfile(html_path, latest_html_path)

    persist_history(
        Path(args.history_db),
        watchlist,
        collected,
        args.date,
        run_dir,
        "success",
        report_path=report_path,
        dashboard_path=html_path,
    )

    print(str(report_path))
    print(str(html_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

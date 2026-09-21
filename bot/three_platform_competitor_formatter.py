from __future__ import annotations

import math


PLATFORM_LABELS = {"TTL": "三平台", "TM": "天猫", "DY": "抖音", "JD": "京东"}


def _missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _fmt_gmv(value) -> str:
    if _missing(value):
        return "N/A"
    number = float(value)
    sign = "-" if number < 0 else ""
    absolute = abs(number)
    if absolute >= 1_000_000:
        return f"{sign}{absolute / 1_000_000:,.1f}M"
    if absolute >= 1_000:
        return f"{sign}{absolute / 1_000:,.1f}K"
    return f"{number:,.0f}"


def _fmt_evol(value, current=0, prior=0) -> str:
    if _missing(value):
        if float(current or 0) > 0 and float(prior or 0) == 0:
            return "新增长"
        return "N/A"
    return f"{float(value) * 100:+.1f}%"


def _fmt_pct(value) -> str:
    if _missing(value):
        return "N/A"
    return f"{float(value) * 100:.1f}%"


def _fmt_pp(value) -> str:
    if _missing(value):
        return "N/A"
    points = float(value) * 100
    if abs(points) < 0.05:
        return "0.0pp"
    return f"{points:+.1f}pp"


def _overall_table(rows: list[dict], *, pure_mass: bool) -> str:
    columns = ["指标", "三平台", "天猫", "抖音", "京东"]
    by_platform = {str(row.get("platform")): row for row in rows}
    matrix = [
        ("品牌整体GMV", lambda row: _fmt_gmv(row.get("gmv_current"))),
        (
            "品牌整体GMV同比",
            lambda row: _fmt_evol(row.get("evol"), row.get("gmv_current"), row.get("gmv_prior")),
        ),
        ("Beauty Market份额", lambda row: _fmt_pct(row.get("beauty_share"))),
        ("Beauty Market份额变化", lambda row: _fmt_pp(row.get("beauty_share_change"))),
    ]
    if pure_mass:
        matrix.extend([
            ("Pure Mass份额", lambda row: _fmt_pct(row.get("pure_mass_share"))),
            ("Pure Mass份额变化", lambda row: _fmt_pp(row.get("pure_mass_share_change"))),
        ])
    lines = ["| " + " | ".join(columns) + " |", "|---|---:|---:|---:|---:|"]
    for label, formatter in matrix:
        values = [formatter(by_platform.get(key, {})) for key in ("TTL", "TM", "DY", "JD")]
        lines.append("| " + " | ".join([label, *values]) + " |")
    return "\n".join(lines)


def _category_table(rows: list[dict]) -> str:
    lines = [
        "| 类目 | 本期GMV | 同比 | 本期品牌GMV占比 | 占比变化 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {str(row.get('category') or '未分类').replace('|', '／')} | "
            f"{_fmt_gmv(row.get('gmv_current'))} | "
            f"{_fmt_evol(row.get('evol'), row.get('gmv_current'), row.get('gmv_prior'))} | "
            f"{_fmt_pct(row.get('weight'))} | {_fmt_pp(row.get('weight_change'))} |"
        )
    if not rows:
        lines.append("| 暂无数据 | N/A | N/A | N/A | N/A |")
    return "\n".join(lines)


def _channel_table(rows: list[dict]) -> str:
    lines = [
        "| 渠道 | 本期GMV | 同比 | GMV增减额 | 本期品牌GMV占比 | 占比变化 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('channel')} | {_fmt_gmv(row.get('gmv_current'))} | "
            f"{_fmt_evol(row.get('evol'), row.get('gmv_current'), row.get('gmv_prior'))} | "
            f"{_fmt_gmv(row.get('gmv_change'))} | {_fmt_pct(row.get('weight'))} | "
            f"{_fmt_pp(row.get('weight_change'))} |"
        )
    return "\n".join(lines)


def _summary(result: dict) -> list[str]:
    overall = {str(row.get("platform")): row for row in result.get("overall") or []}
    ttl = overall.get("TTL", {})
    platform_rows = [overall.get(key, {}) for key in ("TM", "DY", "JD")]
    largest = max(platform_rows, key=lambda row: float(row.get("gmv_current") or 0))
    channels = result.get("channels") or []
    channel = max(channels, key=lambda row: float(row.get("gmv_current") or 0)) if channels else {}
    return [
        (
            f"三平台品牌GMV为{_fmt_gmv(ttl.get('gmv_current'))}，同比"
            f"{_fmt_evol(ttl.get('evol'), ttl.get('gmv_current'), ttl.get('gmv_prior'))}。"
        ),
        (
            f"本期品牌GMV规模最大的单平台是{PLATFORM_LABELS.get(largest.get('platform'), '—')}，"
            f"GMV为{_fmt_gmv(largest.get('gmv_current'))}。"
        ),
        (
            f"抖音本期贡献最大的渠道为{channel.get('channel') or '—'}，"
            f"GMV为{_fmt_gmv(channel.get('gmv_current'))}，占抖音品牌GMV"
            f"{_fmt_pct(channel.get('weight'))}。"
        ),
    ]


def format_three_platform_competitor_report(result: dict) -> str:
    brand = result.get("brand") or "品牌"
    period_meta = result.get("period_meta") or {}
    categories = result.get("categories") or {}
    sections = [
        f"# {brand}三平台生意分析",
        (
            f"分析期间：{period_meta.get('current_label') or result.get('period')}；"
            f"同比期间：{period_meta.get('prior_label') or '去年同期'}。金额单位为人民币元，"
            "表内GMV按M/K格式展示。"
        ),
        "## 核心结论",
        "\n".join(f"- {item}" for item in _summary(result)),
        "## 1. 品牌整体生意",
        _overall_table(result.get("overall") or [], pure_mass=bool(result.get("is_pure_mass"))),
        "## 2. 类目表现",
        "### 天猫平台｜类目GMV Top 5",
        _category_table(categories.get("TM") or []),
        "### 抖音平台｜类目GMV Top 5",
        _category_table(categories.get("DY") or []),
        "### 京东平台｜类目GMV Top 5",
        _category_table(categories.get("JD") or []),
        "## 3. 抖音渠道表现",
        _channel_table(result.get("channels") or []),
        "## 数据口径",
        (
            f"- 本期：{period_meta.get('current_start')}至{period_meta.get('current_end')}；"
            f"同期：{period_meta.get('prior_start')}至{period_meta.get('prior_end')}。"
        ),
        "- 完整自然月使用月表；非完整自然月使用对应平台日表，同一个月份只使用一个来源。",
        "- 三平台指天猫、抖音和京东自营；类目按本期品牌GMV降序展示Top 5。",
        "- Beauty Market份额以三平台对应Beauty Market大盘GMV为分母；Pure Mass品牌额外展示Pure Mass份额。",
        "- 抖音短视频及其他GMV＝品牌GMV－KOL直播GMV－品牌自营直播GMV。",
        "- 同期GMV为0且本期大于0时同比显示“新增长”；两期均为0时显示“N/A”。",
    ]
    return "\n\n".join(sections)

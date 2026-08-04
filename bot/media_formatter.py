from __future__ import annotations

import math

import pandas as pd

from bot.utils import safe_div, safe_evol

_MIX_NAMES = {
    "B": "品牌专区",
    "K": "达人",
    "F": "信息流投放",
    "S": "搜索投放",
    "T": "交易类投资",
}

def _missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _fmt_amount(value) -> str:
    if _missing(value) or float(value) == 0:
        return "-"
    value = float(value)
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    if absolute >= 1_000_000:
        return f"{sign}{absolute / 1_000_000:,.1f}M"
    return f"{sign}{absolute / 1_000:,.1f}K"


def _fmt_int(value) -> str:
    if _missing(value):
        return "—"
    return f"{int(round(float(value))):,}"


def _fmt_pct(value, digits: int = 1) -> str:
    if _missing(value):
        return "—"
    return f"{float(value) * 100:,.{digits}f}%"


def _fmt_evol(value, current=None, prior=None) -> str:
    if _missing(value):
        if not _missing(current) and float(current or 0) > 0 and float(prior or 0) == 0:
            return "新增"
        return "—"
    number = float(value) * 100
    return f"+{number:,.1f}%" if number >= 0 else f"{number:,.1f}%"


def _fmt_comparison(value, status: str | None, current=None, prior=None) -> str:
    labels = {
        "missing_current": "2026年无数据",
        "missing_prior": "2025年无数据",
        "base_zero": "2025年无数据",
    }
    if status in labels:
        return labels[status]
    return _fmt_evol(value, current, prior)


def _fmt_pp(value) -> str:
    if _missing(value):
        return "—"
    number = float(value) * 100
    return f"+{number:,.1f}pp" if number >= 0 else f"{number:,.1f}pp"


def _fmt_weight_change(value, status: str | None) -> str:
    labels = {
        "missing_current": "2026年无数据",
        "missing_prior": "2025年无数据",
        "base_zero": "2025年无数据",
    }
    return labels.get(status, _fmt_pp(value))


def _fmt_cpe(value) -> str:
    if _missing(value):
        return "—"
    return f"¥{float(value):,.2f}"


def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "_无可用数据_"
    return pd.DataFrame(rows, columns=columns).to_markdown(index=False)


def _error_note(result: dict, fallback: str) -> str:
    return f"• {result.get('message') or fallback}\n\n_本节无可用数据。_"


def _render_search(result: dict, number: str = "3") -> str:
    heading = f"# {number}. Social Search"
    source_note = (
        "> _数据来源：小红书；搜索量Evol%=(本期搜索量/上年同期搜索量)-1。_"
    )
    if result.get("error"):
        return "\n\n".join([
            heading,
            source_note,
            _error_note(result, "Social Search查询失败。"),
        ])

    monthly = result.get("monthly") or []
    brand_rows = [row for row in monthly if row.get("grain") == "Brand"]
    category_rows = result.get("categories") or []
    coverage = result.get("coverage") or {}
    bullets = []
    if brand_rows:
        actual = sum(float(row.get("actual") or 0) for row in brand_rows)
        prior = sum(float(row.get("previous_actual") or 0) for row in brand_rows)
        months = "、".join(f"{int(row['month'][-2:])}月" for row in brand_rows)
        qualifier = (
            f"有品牌粒度月份（{months}）"
            if len(brand_rows) != len(monthly)
            else f"2026年1–{int(monthly[-1]['month'][-2:])}月"
        )
        bullets.append(
            f"• {qualifier}搜索量累计{_fmt_int(actual)}，"
            f"同比{_fmt_evol(safe_evol(actual, prior), actual, prior)}。"
        )
        yoy_candidates = [row for row in brand_rows if row.get("evol") is not None]
        if yoy_candidates:
            best = max(yoy_candidates, key=lambda row: row["evol"])
            if len(yoy_candidates) > 1:
                lowest = min(yoy_candidates, key=lambda row: row["evol"])
                bullets.append(
                    f"• {int(best['month'][-2:])}月品牌搜索量同比"
                    f"{_fmt_evol(best['evol'])}最高；相比之下，"
                    f"{int(lowest['month'][-2:])}月同比"
                    f"{_fmt_evol(lowest['evol'])}。"
                )
            else:
                bullets.append(
                    f"• {int(best['month'][-2:])}月品牌搜索量同比"
                    f"{_fmt_evol(best['evol'])}。"
                )
    else:
        bullets.append("• 报告期内没有品牌粒度搜索数据，因此不计算品牌累计值。")

    if coverage.get("category_only_months"):
        month_text = "、".join(f"{int(m[-2:])}月" for m in coverage["category_only_months"])
        bullets.append(f"• {month_text}仅有Category粒度数据，未加总为品牌搜索量。")
    if category_rows:
        category_ordered = sorted(
            category_rows, key=lambda row: row.get("actual") or 0, reverse=True
        )
        category_leader = category_ordered[0]
        text = (
            f"• {int(category_leader['month'][-2:])}月"
            f"{category_leader.get('category') or '未分类'}搜索量"
            f"{_fmt_int(category_leader.get('actual'))}最高"
        )
        if len(category_ordered) > 1:
            runner = category_ordered[1]
            text += (
                f"；相比之下，{int(runner['month'][-2:])}月"
                f"{runner.get('category') or '未分类'}搜索量"
                f"{_fmt_int(runner.get('actual'))}"
            )
        bullets.append(text + "。")
    if coverage.get("missing_months"):
        month_text = "、".join(f"{int(m[-2:])}月" for m in coverage["missing_months"])
        bullets.append(f"• {month_text}无Social Search数据。")

    month_table = []
    for row in monthly:
        month_table.append({
            "月份": f"{int(row['month'][-2:])}月",
            "搜索量Actual": _fmt_int(row.get("actual")),
            "Evol%": _fmt_evol(row.get("evol"), row.get("actual"), row.get("previous_actual")),
            "数据粒度": row.get("grain") or "No data",
        })
    parts = [
        heading,
        "",
        source_note,
        "",
        "\n".join(bullets[:4]),
        "",
        _table(month_table, ["月份", "搜索量Actual", "Evol%", "数据粒度"]),
    ]
    if category_rows:
        category_table = [{
            "月份": f"{int(row['month'][-2:])}月",
            "Category": row.get("category"),
            "搜索量Actual": _fmt_int(row.get("actual")),
            "Evol%": _fmt_evol(row.get("evol"), row.get("actual"), row.get("previous_actual")),
        } for row in category_rows]
        parts.extend([
            "",
            f"## {number}.1 Category粒度明细（不加总为Brand）",
            "",
            _table(category_table, ["月份", "Category", "搜索量Actual", "Evol%"]),
        ])
    return "\n".join(parts)


def _mix_bullets(rows: list[dict], label: str) -> str:
    valid = [row for row in rows if (row.get("current_spend_million") or 0) > 0]
    if not valid:
        return f"• {label}在报告期内无已打标花费。"
    ordered = sorted(valid, key=lambda row: row.get("weight") or 0, reverse=True)
    leader = ordered[0]

    def type_fact(row: dict) -> str:
        name = _MIX_NAMES.get(row["label"], row["label"])
        status = row.get("weight_comparison_status")
        change = row.get("weight_change")
        if status in {"missing_prior", "base_zero"}:
            change_text = "2025年无数据"
        elif change is None:
            change_text = "同比变化不可比"
        elif change > 0:
            change_text = f"较去年{_fmt_pp(change)}，有所增强"
        elif change < 0:
            change_text = f"较去年{_fmt_pp(change)}，有所减弱"
        else:
            change_text = "较去年持平"
        return f"{name}占比{_fmt_pct(row.get('weight'))}，{change_text}"

    change_candidates = [
        row for row in rows
        if (row.get("current_spend_million") or 0) > 0
        or (row.get("prior_spend_million") or 0) > 0
    ]
    with_change = [
        row for row in change_candidates
        if row.get("weight_change") is not None
    ]
    mover = max(with_change, key=lambda row: abs(row["weight_change"])) if with_change else None
    if mover and mover["label"] != leader["label"]:
        text = (
            f"• {type_fact(leader)}，是{label}当前投入最集中的类型；"
            f"相比之下，{type_fact(mover)}，变化幅度最大。"
        )
        if (
            (leader.get("weight_change") or 0) > 0
            and (mover.get("weight_change") or 0) < 0
        ):
            text = text.rstrip("。") + (
                f"——花费结构正向{_MIX_NAMES.get(leader['label'], leader['label'])}集中。"
            )
        return text
    if mover is leader:
        text = (
            f"• {type_fact(leader)}，既是{label}投入占比最高、"
            "也是结构变化幅度最大的类型。"
        )
        if len(ordered) > 1:
            runner = ordered[1]
            text = text.rstrip("。") + (
                f"；相比之下，{type_fact(runner)}。"
            )
        return text
    if len(ordered) > 1:
        runner = ordered[1]
        return (
            f"• {type_fact(leader)}，为{label}投入占比最高的类型；"
            f"相比之下，{_MIX_NAMES.get(runner['label'], runner['label'])}"
            f"占比{_fmt_pct(runner.get('weight'))}。"
        )
    return f"• {type_fact(leader)}，为{label}唯一有花费的投资类型。"


def _mix_table(rows: list[dict], first_col: str) -> str:
    table_rows = [{
        first_col: row.get("label"),
        "Wgt%": _fmt_pct(row.get("weight")),
        "Wgt Change": _fmt_weight_change(
            row.get("weight_change"), row.get("weight_comparison_status")
        ),
    } for row in rows]
    return _table(table_rows, [first_col, "Wgt%", "Wgt Change"])


def _ratio_metrics(spend: dict, nso: dict) -> dict:
    if not spend or spend.get("actual_yuan") is None or nso.get("error"):
        return {"actual": None, "prior": None, "change": None, "status": "missing_current"}
    actual = safe_div(spend.get("actual_yuan"), nso.get("nso_actual"))
    prior = safe_div(spend.get("prior_yuan"), nso.get("nso_prior"))
    statuses = (spend.get("comparison_status"), nso.get("comparison_status"))
    status = next((value for value in statuses if value and value != "ok"), "ok")
    if prior == 0 and status == "ok":
        status = "base_zero"
    return {
        "actual": actual,
        "prior": prior,
        "change": (
            round(actual - prior, 6)
            if status == "ok" and actual is not None and prior is not None
            else None
        ),
        "status": status,
    }


def _investment_row(
    name: str,
    spend: dict,
    *,
    weight: float | None,
    weight_change: float | None,
    weight_status: str,
    nso: dict | None = None,
    show_nso: bool = False,
    show_ratio: bool = False,
) -> tuple[dict, dict]:
    nso = nso or {}
    ratio = _ratio_metrics(spend, nso) if show_ratio else {
        "actual": None,
        "prior": None,
        "change": None,
        "status": "missing_current",
    }
    return {
        "类型": name,
        "媒体花费Actual": _fmt_amount(spend.get("actual_yuan")),
        "花费Evol%": _fmt_comparison(
            spend.get("evol"),
            spend.get("comparison_status"),
            spend.get("actual_yuan"),
            spend.get("prior_yuan"),
        ),
        "媒体花费Wgt%": _fmt_pct(weight),
        "Wgt Change": _fmt_weight_change(weight_change, weight_status),
        "NSO Actual": (
            _fmt_amount(nso.get("nso_actual"))
            if show_nso and not nso.get("error") else "—"
        ),
        "NSO Evol%": (
            _fmt_comparison(
                nso.get("evol"),
                nso.get("comparison_status"),
                nso.get("nso_actual"),
                nso.get("nso_prior"),
            )
            if show_nso and not nso.get("error") else "—"
        ),
        "媒体费比": _fmt_pct(ratio.get("actual")) if show_ratio else "—",
        "费比变化": (
            _fmt_weight_change(ratio.get("change"), ratio.get("status"))
            if show_ratio and not nso.get("error") else "—"
        ),
    }, ratio


def _media_bullets(
    investment: dict,
    nso: dict,
    ttl_ratio: dict,
) -> list[str]:
    if investment.get("error"):
        return [f"• {investment.get('message') or 'Topline媒体投资无可用数据。'}"]
    ttl = investment.get("ttl") or {}
    ttl_text = (
        f"• TTL媒体花费{_fmt_amount(ttl.get('actual_yuan'))}，同比"
        f"{_fmt_comparison(ttl.get('evol'), ttl.get('comparison_status'), ttl.get('actual_yuan'), ttl.get('prior_yuan'))}"
    )
    if not nso.get("error"):
        ttl_text += (
            f"；NSO {_fmt_amount(nso.get('nso_actual'))}，同比"
            f"{_fmt_comparison(nso.get('evol'), nso.get('comparison_status'), nso.get('nso_actual'), nso.get('nso_prior'))}"
        )
        if ttl_ratio.get("actual") is not None:
            fee_change = _fmt_weight_change(
                ttl_ratio.get("change"), ttl_ratio.get("status")
            )
            ttl_text += f"；媒体费比{_fmt_pct(ttl_ratio.get('actual'))}"
            if fee_change != "—":
                ttl_text += f"，较同期{fee_change}"
    bullets = [ttl_text + "。"]

    ait_rows = [
        row for row in (investment.get("ait") or [])
        if (row.get("actual_yuan") or 0) > 0
    ]
    if ait_rows:
        leader = max(ait_rows, key=lambda row: row.get("weight") or 0)
        text = (
            f"• {leader.get('label')}花费占比{_fmt_pct(leader.get('weight'))}，"
            "是当前投入最大的AIT类型"
        )
        comparable = [row for row in ait_rows if row.get("weight_change") is not None]
        if comparable:
            mover = max(comparable, key=lambda row: abs(row.get("weight_change") or 0))
            if mover is leader:
                text += f"，占比较同期{_fmt_weight_change(mover.get('weight_change'), 'ok')}"
            else:
                text += (
                    f"；{mover.get('label')}占比变化最明显，较同期"
                    f"{_fmt_weight_change(mover.get('weight_change'), 'ok')}"
                )
        bullets.append(text + "。")

    platforms = [
        row for row in (investment.get("transaction_platforms") or [])
        if (row.get("actual_yuan") or 0) > 0
    ]
    if platforms:
        leader = max(platforms, key=lambda row: row.get("weight") or 0)
        text = (
            f"• Transaction中{leader.get('label')}花费"
            f"{_fmt_amount(leader.get('actual_yuan'))}，占比"
            f"{_fmt_pct(leader.get('weight'))}最高"
        )
        comparable = [row for row in platforms if row.get("evol") is not None]
        if comparable:
            mover = max(comparable, key=lambda row: abs(row.get("evol") or 0))
            text += (
                f"；{mover.get('label')}花费同比变化最明显，为"
                f"{_fmt_comparison(mover.get('evol'), mover.get('comparison_status'), mover.get('actual_yuan'), mover.get('prior_yuan'))}"
            )
        bullets.append(text + "。")
    return bullets[:3]


def _render_media(investment: dict, nso: dict) -> str:
    parts = [
        "# 1. Media Investment",
        "",
        "> _数据来源：媒体花费来自Topline Report，NSO来自EC Consolidation。_",
        "> _花费Evol%=(本期媒体花费/上年同期媒体花费)-1。_",
        (
            "> _AIT Wgt%=对应AIT类型花费/TTL媒体花费；"
            "交易平台Wgt%=对应平台交易花费/Transaction花费。_"
        ),
        (
            "> _Wgt Change=本期Wgt%-上年同期Wgt%；"
            "媒体费比（也称Take Rate、TR或BET%）=媒体花费（元）/TTL NSO；费比变化为百分点变化。_"
        ),
        "",
        "## 1.1 Overall",
        "",
        "### 1.1.1 TTL、AIT与交易平台",
        "",
    ]
    ttl = {} if investment.get("error") else (investment.get("ttl") or {})
    ttl_row, ttl_ratio = _investment_row(
        "TTL",
        ttl,
        weight=1.0 if not investment.get("error") else None,
        weight_change=0.0 if not investment.get("error") else None,
        weight_status="ok" if not investment.get("error") else "missing_current",
        nso=nso,
        show_nso=True,
        show_ratio=True,
    )
    rows = [ttl_row]
    ait_rows = investment.get("ait") or []
    for ait_row in ait_rows:
        row, _ = _investment_row(
            f"├─ {ait_row.get('label')}",
            ait_row,
            weight=ait_row.get("weight"),
            weight_change=ait_row.get("weight_change"),
            weight_status=ait_row.get("weight_comparison_status") or "missing_current",
            nso=nso,
            show_ratio=True,
        )
        rows.append(row)
    platform_rows = investment.get("transaction_platforms") or []
    for index, platform_row in enumerate(platform_rows):
        branch = "└─" if index == len(platform_rows) - 1 else "├─"
        row, _ = _investment_row(
            f"│　{branch} {platform_row.get('label')}",
            platform_row,
            weight=platform_row.get("weight"),
            weight_change=platform_row.get("weight_change"),
            weight_status=(
                platform_row.get("weight_comparison_status") or "missing_current"
            ),
        )
        rows.append(row)
    parts.extend([
        "\n".join(_media_bullets(investment, nso, ttl_ratio)),
        "",
        _table(
            rows,
            [
                "类型", "媒体花费Actual", "花费Evol%", "媒体花费Wgt%",
                "Wgt Change", "NSO Actual", "NSO Evol%", "媒体费比", "费比变化",
            ],
        ),
        "",
        (
            "> _AIT费比统一使用TTL NSO；交易平台Wgt%以Transaction花费为分母，"
            "未覆盖的其他交易媒体仍保留在Transaction总额中。_"
        ),
    ])
    if nso.get("error"):
        parts.extend([
            "",
            (
                f"> _{nso.get('message') or 'EC Consolidation无可用NSO数据'}；"
                "本表继续展示媒体花费和结构，NSO与媒体费比留空。_"
            ),
        ])

    mix = investment.get("mix") or {}
    if investment.get("error"):
        overall_section = red_section = douyin_section = (
            "_Topline数据源无对应品牌或无报告期数据。_"
        )
    else:
        overall = mix.get("overall") or []
        red = mix.get("red") or []
        douyin = mix.get("douyin") or []
        overall_section = "\n\n".join([
            _mix_bullets(overall, "整体媒体"),
            _mix_table(overall, "BKFST"),
        ])
        red_section = "\n\n".join([
            _mix_bullets(red, "小红书"),
            _mix_table(red, "BKFS"),
        ])
        douyin_section = "\n\n".join([
            _mix_bullets(douyin, "抖音"),
            _mix_table(douyin, "BKFST"),
        ])
    parts.extend([
        "",
        "### 1.1.2 Overall BKFST",
        "",
        overall_section,
        "",
        "## 1.2 RED BKFS",
        "",
        red_section,
        "",
        "## 1.3 DOUYIN BKFST",
        "",
        douyin_section,
    ])
    return "\n".join(parts)


def _dimension_name(row: dict, dimension_label: str) -> str:
    raw_name = str(row.get("name") or "未分类")
    if dimension_label == "Tier":
        tier_names = {
            "T1": "T1头部达人",
            "T2": "T2中腰部达人",
            "T3": "T3中腰部达人",
            "T4": "T4长尾达人",
            "T5": "T5长尾达人",
            "KOC": "KOC素人达人",
        }
        return tier_names.get(raw_name.upper(), f"{raw_name}层级达人")
    type_names = {
        "beauty": "美妆垂类达人",
        "美妆": "美妆垂类达人",
        "seeding": "种草类达人",
        "lifestyle": "生活方式达人",
        "life": "生活类达人",
        "生活": "生活方式达人",
        "fashion": "时尚达人",
        "sitcom": "情景剧类达人",
        "others": "其他类型达人",
        "gossip&entertainment": "八卦娱乐类达人",
        "母婴": "母婴达人",
    }
    return type_names.get(raw_name.casefold(), f"{raw_name}类型达人")


def _dimension_bullets(rows: list[dict], dimension_label: str) -> str:
    valid = [row for row in rows if (row.get("cost") or 0) > 0]
    if not valid:
        return f"• {dimension_label}维度无报告期花费。"
    ordered = sorted(valid, key=lambda row: row.get("weight") or 0, reverse=True)
    leader = ordered[0]
    dimension_word = "达人层级" if dimension_label == "Tier" else "达人类型"

    def type_fact(row: dict) -> str:
        status = row.get("weight_comparison_status")
        change = row.get("weight_change")
        if status in {"missing_prior", "base_zero"}:
            change_text = "2025年无数据"
        elif change is None:
            change_text = "同比变化不可比"
        elif change > 0:
            change_text = f"较去年{_fmt_pp(change)}，占比增强"
        elif change < 0:
            change_text = f"较去年{_fmt_pp(change)}，占比减弱"
        else:
            change_text = "较去年持平"
        cpe_text = (
            f"，CPE为{_fmt_cpe(row.get('cpe'))}"
            if row.get("cpe") is not None else ""
        )
        return (
            f"{_dimension_name(row, dimension_label)}花费占比"
            f"{_fmt_pct(row.get('weight'))}，{change_text}{cpe_text}"
        )

    growth_candidates = [
        row for row in valid
        if row.get("weight_change") is not None
    ]
    if growth_candidates:
        mover = max(growth_candidates, key=lambda row: abs(row["weight_change"]))
    else:
        mover = None

    if mover and mover is not leader:
        comparison = (
            f"• {type_fact(leader)}，是当前占比最高的{dimension_word}；"
            f"相比之下，{type_fact(mover)}，占比变化幅度最大"
        )
        if leader.get("cpe") is not None and mover.get("cpe") is not None:
            if leader["cpe"] < mover["cpe"]:
                comparison += (
                    f"，{_dimension_name(leader, dimension_label)}的互动成本更低"
                )
            elif mover["cpe"] < leader["cpe"]:
                comparison += (
                    f"，{_dimension_name(mover, dimension_label)}的互动成本更低"
                )
        comparison += "。"
    elif mover is leader:
        runner = ordered[1] if len(ordered) > 1 else None
        comparison = (
            f"• {type_fact(leader)}，既是占比最高、"
            f"也是变化幅度最大的{dimension_word}"
        )
        if runner:
            comparison += f"；{type_fact(runner)}"
            if leader.get("cpe") is not None and runner.get("cpe") is not None:
                lower = leader if leader["cpe"] < runner["cpe"] else runner
                comparison += (
                    f"，其中{_dimension_name(lower, dimension_label)}互动成本更低"
                )
        comparison += "。"
    elif len(ordered) > 1:
        comparison = (
            f"• {type_fact(leader)}，是当前占比最高的{dimension_word}；"
            f"相比之下，{type_fact(ordered[1])}。"
        )
    else:
        comparison = f"• {type_fact(leader)}，为唯一有花费的{dimension_word}。"

    bullets = [comparison]
    names = [_dimension_name(row, dimension_label) for row in ordered[:3]]
    if dimension_label == "Tier":
        change_rows = {
            str(row.get("name") or "").upper(): row.get("weight_change")
            for row in rows
        }
        head_change = sum(
            value for tier in ("T1", "T2", "T3")
            if (value := change_rows.get(tier)) is not None
        )
        tail_change = sum(
            value for tier in ("T4", "T5", "KOC")
            if (value := change_rows.get(tier)) is not None
        )
        if head_change < 0 < tail_change:
            bullets.append("• 达人结构正从头部及中腰部向长尾和素人达人转移。")
        elif head_change > 0 > tail_change:
            bullets.append("• 达人结构正从长尾和素人达人向头部及中腰部集中。")
    if len(bullets) == 1:
        if len(names) >= 3:
            summary = f"品牌主要侧重{names[0]}和{names[1]}，{names[2]}为辅"
        elif len(names) == 2:
            summary = f"品牌主要侧重{names[0]}，{names[1]}为辅"
        else:
            summary = f"品牌达人投资集中在{names[0]}"
        bullets.append(f"• {summary}。")
    return "\n".join(bullets[:2])


def _dimension_table(rows: list[dict], first_col: str) -> str:
    table_rows = [{
        first_col: row.get("name"),
        "花费Actual": _fmt_amount(row.get("cost")),
        "花费Evol%": _fmt_comparison(
            row.get("cost_evol"),
            row.get("comparison_status"),
            row.get("cost"),
            row.get("cost_prior"),
        ),
        "花费Wgt%": _fmt_pct(row.get("weight")),
        "Wgt Change": _fmt_weight_change(
            row.get("weight_change"), row.get("weight_comparison_status")
        ),
        "Engage": _fmt_int(row.get("engage")),
        "Engage Evol%": _fmt_comparison(
            row.get("engage_evol"),
            row.get("engage_comparison_status"),
            row.get("engage"),
            row.get("engage_prior"),
        ),
        "CPE": _fmt_cpe(row.get("cpe")),
    } for row in rows]
    return _table(
        table_rows,
        [first_col, "花费Actual", "花费Evol%", "花费Wgt%", "Wgt Change", "Engage", "Engage Evol%", "CPE"],
    )


def _render_kol_platform(result: dict, title: str, number: str) -> str:
    parts = [f"## {number} {title}"]
    if result.get("error"):
        parts.extend(["", _error_note(result, f"{title} KOL数据查询失败。")])
        return "\n".join(parts)
    by_tier = result.get("by_tier") or []
    by_type = result.get("by_kol_type") or []
    top = result.get("top_kol") or []
    if top:
        top_kol = top[0]
        top_bullet = (
            f"• {top_kol['nickname']}以Engage {_fmt_int(top_kol.get('engage'))}排名第一，"
            f"花费{_fmt_amount(top_kol.get('cost'))}，CPE为{_fmt_cpe(top_kol.get('cpe'))}"
        )
        tier_row = next(
            (
                row for row in by_tier
                if str(row.get("name") or "").casefold()
                == str(top_kol.get("tier") or "").casefold()
            ),
            None,
        )
        if (
            tier_row
            and top_kol.get("cpe") is not None
            and tier_row.get("cpe") is not None
            and top_kol["cpe"] < tier_row["cpe"]
        ):
            saving = safe_div(tier_row["cpe"] - top_kol["cpe"], tier_row["cpe"])
            top_bullet += (
                f"，比{top_kol.get('tier') or '同'}层级平均CPE"
                f"{_fmt_cpe(tier_row.get('cpe'))}低{_fmt_pct(saving)}"
            )
        top_bullet += "。"
    else:
        top_bullet = "• 报告期内无可用于Top 10排名的KOL数据。"
    top_rows = [{
        "排名": row.get("rank"),
        "KOL": row.get("nickname"),
        "Tier": row.get("tier"),
        "KOL Type": row.get("kol_type"),
        "花费Actual": _fmt_amount(row.get("cost")),
        "Engage": _fmt_int(row.get("engage")),
        "CPE": _fmt_cpe(row.get("cpe")),
    } for row in top]
    parts.extend([
        "",
        f"### {number}.1 By Tier",
        "",
        _dimension_bullets(by_tier, "Tier"),
        "",
        _dimension_table(by_tier, "Tier"),
        "",
        f"### {number}.2 By KOL Type",
        "",
        _dimension_bullets(by_type, "KOL Type"),
        "",
        _dimension_table(by_type, "KOL Type"),
        "",
        f"### {number}.3 Top 10 KOL by Engage",
        "",
        top_bullet,
        "",
        _table(top_rows, ["排名", "KOL", "Tier", "KOL Type", "花费Actual", "Engage", "CPE"]),
    ])
    return "\n".join(parts)


def format_media_report(
    display_brand: str,
    period: dict,
    search_result: dict,
    investment_result: dict,
    nso_result: dict,
    red_result: dict,
    douyin_result: dict,
    resolved_brands: dict,
    brand_match_methods: dict | None = None,
) -> str:
    return "\n".join([
        _render_media(investment_result, nso_result),
        "",
        "# 2. KOL Performance",
        "",
        (
            "> _数据来源：KSI Report；花费Wgt%=对应达人类型花费/平台达人总花费，"
            "Wgt Change为百分点变化，Engage为互动量，CPE=花费/Engage。_"
        ),
        "",
        _render_kol_platform(red_result, "RED", "2.1"),
        "",
        _render_kol_platform(douyin_result, "DOUYIN", "2.2"),
        "",
        _render_search(search_result, "3"),
    ])

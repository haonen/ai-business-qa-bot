"""
formatter.py — 固定结构报告生成（代码排版 + LLM只填结论bullets）
模块顺序：整体生意 → 品类分析 → 系列/链接下钻 → Key Driver
"""
import json
import os
import re

import pandas as pd

_FORBIDDEN_WORDS = [
    "大盘", "行业", "竞品", "对手", "市场平均", "同行",
    "跑赢", "跑输", "领先", "落后于", "排名第", "全网",
]
try:
    _CONFIG_PATH = os.path.join(os.path.dirname(__file__), "data", "narrative_config.json")
    if os.path.exists(_CONFIG_PATH):
        with open(_CONFIG_PATH, encoding="utf-8") as _f:
            _FORBIDDEN_WORDS += json.load(_f).get("wording_blacklist", [])
except Exception:
    pass

_SUMMARY_MODEL = "qwen-plus-latest"


# ── 工具函数 ──────────────────────────────────────────────────

def _llm_client():
    from openai import OpenAI
    return OpenAI(
        api_key=os.environ["DASHSCOPE_API_KEY"],
        base_url=os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )


def _short_category(name: str) -> str:
    return name.split("-")[-1] if "-" in name else name


def _fmt_gmv(value) -> str:
    """Format yuan-denominated GMV as M/K with thousands separators."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or value == 0:
        return "-"
    value = float(value)
    sign = "-" if value < 0 else ""
    abs_value = abs(value)
    if abs_value >= 1_000_000:
        return f"{sign}{abs_value / 1_000_000:,.1f}M"
    return f"{sign}{abs_value / 1_000:,.1f}K"


def _fmt_pct(value, digits: int = 0) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    return f"{value * 100:,.{digits}f}%"


def _fmt_evol(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "新增"
    p = value * 100
    return f"+{p:,.0f}%" if p >= 0 else f"{p:,.0f}%"


def _fmt_pp(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    p = value * 100
    return f"+{p:,.0f}pp" if p >= 0 else f"{p:,.0f}pp"


def _fmt_int(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    return f"{int(value):,}"


def _fmt_yuan(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    return f"¥{int(value):,}"


def _share_delta(gmv_26, total_26, gmv_25, total_25):
    if not total_26 or not total_25:
        return None
    return (gmv_26 / total_26) - (gmv_25 / total_25)


def _driver_sort_key(name: str) -> str:
    return str(name or "")


def df_to_markdown_table(
    df: pd.DataFrame,
    gmv_cols: list = None,
    pct_cols: list = None,
    evol_cols: list = None,
    pp_cols: list = None,
) -> str:
    df = df.copy()
    for col in (gmv_cols or []):
        if col in df.columns:
            df[col] = df[col].apply(_fmt_gmv)
    for col in (pct_cols or []):
        if col in df.columns:
            df[col] = df[col].apply(_fmt_pct)
    for col in (evol_cols or []):
        if col in df.columns:
            df[col] = df[col].apply(_fmt_evol)
    for col in (pp_cols or []):
        if col in df.columns:
            df[col] = df[col].apply(_fmt_pp)
    return df.to_markdown(index=False)


def select_bullet_facts(items: list[dict], total_gmv: float) -> list[dict]:
    """
    Deterministic: return 1-2 facts for bullet points.
    Fact A = item with highest weight (贡献最大).
    Fact B = item with highest positive evol, excluding noise
             (gmv_current < total_gmv * 2% → likely noise).
    Returns [fact_a] or [fact_a, fact_b].
    """
    if not items:
        return []

    fact_a = max(items, key=lambda x: x.get("weight") or 0)

    noise_floor = total_gmv * 0.02
    candidates = [i for i in items
                  if (i.get("gmv_current") or 0) >= noise_floor
                  and i.get("evol") is not None
                  and i.get("evol") > 0]
    if not candidates:
        candidates = [i for i in items if i.get("evol") is not None and i.get("evol") > 0]

    if candidates:
        fact_b = max(candidates, key=lambda x: x.get("evol") or 0)
    else:
        fact_b = None

    if fact_b is None or fact_b.get("name") == fact_a.get("name"):
        return [fact_a]
    return [fact_a, fact_b]


def validate_conclusion(text: str, table_data: dict) -> tuple[bool, str]:
    for word in _FORBIDDEN_WORDS:
        if word in text:
            return False, f'出现了违禁词"{word}"（该维度数据未提供）'
    nums = re.findall(r"\d+(?:\.\d+)?", text)
    if nums:
        all_vals = {float(v) for v in table_data.values() if isinstance(v, (int, float))}
        for n in nums:
            if not any(abs(float(n) - v) <= 0.5 for v in all_vals):
                return False, f"数字{n}在表格数据中找不到匹配值"
    return True, ""


def _align_drilldown_marker(bullets: str, selected_short: str, selected_obj: dict | None) -> str:
    """Keep “进入下钻分析” attached only to the actual selected drilldown category."""
    if not bullets or not selected_short:
        return bullets
    lines = [line.strip() for line in bullets.splitlines() if line.strip()]
    aligned = []
    selected_line_has_marker = False
    for line in lines:
        if "进入下钻分析" in line and selected_short not in line:
            line = line.replace("，进入下钻分析", "").replace("进入下钻分析", "").rstrip("，,。 ")
            if line and not line.endswith("。"):
                line += "。"
        if selected_short in line:
            if "进入下钻分析" not in line:
                line = line.rstrip("。") + "，进入下钻分析"
            selected_line_has_marker = True
        aligned.append(line)

    if not selected_line_has_marker and selected_obj:
        weight = selected_obj.get("weight")
        evol = selected_obj.get("evol")
        parts = [f"{selected_short}进入下钻分析"]
        if weight is not None:
            parts.append(f"占比{round(weight * 100)}%")
        if evol is not None:
            parts.append(f"同比{_fmt_evol(evol)}")
        aligned.append("• " + "，".join(parts))
    return "\n".join(aligned)


def _gen_bullets(prompt: str, table_data: dict, fallback: str) -> str:
    client = _llm_client()
    extra = ""
    last_reason = ""
    for attempt in range(3):
        if attempt > 0:
            extra = f"\n上次生成的结论存在以下问题：{last_reason}，请重新生成。你只能基于下方给定的数据陈述事实，不能引入大盘/行业/竞品等未提供的对比维度，也不能使用表格之外的数字。"
        try:
            resp = client.chat.completions.create(
                model=os.environ.get("DASHSCOPE_SUMMARY_MODEL", _SUMMARY_MODEL),
                messages=[{"role": "user", "content": prompt + extra}],
                max_tokens=150,
            )
            raw = resp.choices[0].message.content.strip()
            passed, reason = validate_conclusion(raw, table_data)
            if passed:
                lines = [l.strip() for l in raw.splitlines() if l.strip()]
                return "\n".join(
                    l if l.startswith("•") else f"• {l.lstrip('-').strip()}"
                    for l in lines
                )
            last_reason = reason
        except Exception:
            pass
    return f"• {fallback}"


def _gen_bullets_loose(
    prompt: str,
    fallback: str,
    max_tokens: int = 220,
    model: str = None,
    max_bullets: int = 3,
) -> str:
    """LLM bullets for richer qualitative synthesis; falls back deterministically."""
    try:
        resp = _llm_client().chat.completions.create(
            model=model or os.environ.get("DASHSCOPE_SUMMARY_MODEL", _SUMMARY_MODEL),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        raw = resp.choices[0].message.content.strip()
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        bullets = []
        for line in lines[:max_bullets]:
            text = line.lstrip("-•").strip()
            if text:
                bullets.append(f"• {text}")
        if bullets:
            return "\n".join(bullets)
    except Exception:
        pass
    return f"• {fallback}"


# ── 模块渲染 ──────────────────────────────────────────────────

def render_module_fraud(fraud_result: dict) -> str:
    """模块0（整体生意的第一部分）：刷单检测表格 + 说明 + 高风险警告"""
    if not fraud_result or fraud_result.get("error"):
        return ""

    breakdown = fraud_result.get("breakdown", [])
    total_gmv = fraud_result.get("total_gmv", 0)
    total_unit = fraud_result.get("total_unit", 0)
    fraud_pct = fraud_result.get("fraud_pct", 0)

    rows = []
    for group in breakdown:
        status = group["status"]
        rows.append({
            "状态": status,
            "渠道": "合计",
            "GMV": group["gmv_current"],
            "占比": group["weight"],
            "件数": group["unit"],
            "GMV/件(元)": group["atv"],
        })
        for d in group.get("drivers", []):
            rows.append({
                "状态": "",
                "渠道": d["key_driver"],
                "GMV": d["gmv_current"],
                "占比": d["weight"],
                "件数": d["unit"],
                "GMV/件(元)": d["atv"],
            })
    rows.append({
        "状态": "Grand Total",
        "渠道": "",
        "GMV": total_gmv,
        "占比": 1.0,
        "件数": total_unit,
        "GMV/件(元)": round(total_gmv / total_unit) if total_unit else 0,
    })

    df = pd.DataFrame(rows)
    df["GMV"] = df["GMV"].apply(_fmt_gmv)
    df["占比"] = df["占比"].apply(lambda x: f"{x * 100:.1f}%" if pd.notna(x) and x else "-")
    df["件数"] = df["件数"].apply(lambda x: f"{int(x):,}" if pd.notna(x) and x else "-")
    df["GMV/件(元)"] = df["GMV/件(元)"].apply(lambda x: f"¥{int(x):,}" if pd.notna(x) and x else "-")
    table_md = df.to_markdown(index=False)

    note = '_打标规则：GMV/件数 > ¥1,000 的链接标记为「疑似刷单」；后续所有分析不剔除此部分，仍按既有数据进行。_'

    parts = ["**生意质检**", "", table_md, "", note]

    if fraud_pct > 0.30:
        parts.append(
            f"\n⚠️ **疑似刷单占比达 {fraud_pct * 100:.1f}%，已超过30%警戒线。**"
            f" 该部分对整体GMV影响较大，建议核实后再解读生意数据。是否继续分析？"
        )

    return "\n".join(parts)


def render_module_overall(
    category_result: dict,
    fraud_result: dict = None,
    ttl_result: dict | None = None,
) -> str:
    """模块1：整体生意 — 总GMV + 同比 + 简版质检，无LLM"""
    total = (ttl_result or {}).get("total") or category_result.get("overall_total") or category_result.get("total", {})
    brand = category_result.get("brand", "")
    period_meta = category_result.get("period_meta") or {}
    current_label = period_meta.get("current_label") or category_result.get("period", "")
    prior_label = period_meta.get("prior_label") or "去年同期"
    gmv_26 = total.get("gmv_current", 0)
    gmv_25 = total.get("gmv_prior", 0)
    evol = total.get("evol")

    if evol is not None:
        evol_str = f"+{evol * 100:.0f}%" if evol >= 0 else f"{evol * 100:.0f}%"
    else:
        evol_str = "-"

    parts = [
        f"# 整体生意\n\n"
        f"{brand}在{current_label}的天猫总GMV为{_fmt_gmv(gmv_26)}，"
        f"同比{evol_str}（{prior_label}为{_fmt_gmv(gmv_25)}）"
    ]

    if fraud_result and not fraud_result.get("error"):
        fraud_pct = fraud_result.get("fraud_pct", 0) or 0
        normal_pct = max(0, 1 - fraud_pct)
        parts.append(
            f"生意质检：正常GMV占比{normal_pct * 100:.1f}%，"
            f"疑似刷单GMV占比{fraud_pct * 100:.1f}%。"
            f"打标规则：GMV/件数 > ¥1,000 的链接标记为「疑似刷单」；"
            f"后续分析不剔除此部分，仍按既有数据进行。"
        )

    return "\n\n".join(parts)


def render_module_category(category_result: dict, selected_category: str, sku_result: dict = None) -> str:
    """模块2：品类分析 — bullets（含进入下钻说明）+ 全品类表格"""
    categories = category_result.get("categories", [])
    total_26 = category_result.get("total", {}).get("gmv_current", 0)
    total_25 = category_result.get("total", {}).get("gmv_prior", 0)

    main_rows, other_26, other_25 = [], 0, 0
    table_data = {}

    for c in categories:
        w = c.get("weight") or 0
        if w >= 0.03:
            main_rows.append(c)
            sname = _short_category(c["category_cn"])
            table_data[f"{sname}_占比"] = round(w * 100)
            if c.get("evol") is not None:
                table_data[f"{sname}_同比"] = round(c["evol"] * 100)
        else:
            other_26 += c.get("gmv_current", 0)
            other_25 += c.get("gmv_prior", 0)

    rows = []
    for c in main_rows:
        rows.append({
            "品类": _short_category(c["category_cn"]),
            "本期GMV": c["gmv_current"],
            "同比": c.get("evol"),
            "占比": c.get("weight"),
            "占比变化": _share_delta(c.get("gmv_current", 0), total_26, c.get("gmv_prior", 0), total_25),
        })
    if other_26 > 0:
        other_w = other_26 / total_26 if total_26 else None
        other_ev = (other_26 - other_25) / other_25 if other_25 else None
        rows.append({
            "品类": "其他",
            "本期GMV": other_26,
            "同比": other_ev,
            "占比": other_w,
            "占比变化": _share_delta(other_26, total_26, other_25, total_25),
        })
        if other_w:
            table_data["其他_占比"] = round(other_w * 100)

    df = pd.DataFrame(rows)
    table_md = df_to_markdown_table(
        df,
        gmv_cols=["本期GMV"],
        pct_cols=["占比"],
        evol_cols=["同比"],
        pp_cols=["占比变化"],
    )

    sel_short = _short_category(selected_category) if selected_category else ""
    total_gmv_val = sum(c.get("gmv_current") or 0 for c in categories)

    # Deterministic fact selection
    fact_items = [{
        "name": _short_category(c["category_cn"]),
        "gmv_current": c.get("gmv_current") or 0,
        "weight": c.get("weight") or 0,
        "evol": c.get("evol"),
    } for c in categories]
    facts = select_bullet_facts(fact_items, total_gmv_val)

    fact_lines = []
    for f in facts:
        parts_f = []
        if f.get("weight") is not None:
            parts_f.append(f'占比{round((f["weight"] or 0)*100)}%')
        if f.get("evol") is not None:
            evol_p = round((f["evol"] or 0) * 100)
            parts_f.append(f'同比{"+" if evol_p >= 0 else ""}{evol_p}%')
        fact_lines.append(f'  - {f["name"]}：{", ".join(parts_f)}')
    facts_str = "\n".join(fact_lines)

    selected_obj = next((c for c in categories if c.get("category_cn") == selected_category), None)
    top = selected_obj or (main_rows[0] if main_rows else {})
    fallback = f"{_short_category(top.get('category_cn', ''))}占比{round((top.get('weight') or 0) * 100)}%，进入下钻分析"

    category_prompt_rows = []
    for row in rows[:8]:
        category_prompt_rows.append({
            "品类": row["品类"],
            "本期GMV": _fmt_gmv(row["本期GMV"]),
            "同比": _fmt_evol(row.get("同比")),
            "占比": _fmt_pct(row.get("占比")),
            "占比变化": _fmt_pp(row.get("占比变化")),
        })

    prompt = f"""请只基于以下品类表现表，写2条summary bullet。
品类表：{json.dumps(category_prompt_rows, ensure_ascii=False)}
重点事实：{facts_str}
选定下钻品类：{sel_short}

要求：
1. 每条以"•"开头，每行一条
2. 不要只堆数字，要先给业务判断，再用数字作证据
3. 必须覆盖：最大品类、增长最快或占比提升最明显的品类
4. 只有“选定下钻品类”对应的bullet可以写"进入下钻分析"，其他品类禁止写这句话
5. 禁止提及系列、链接、SKU、渠道；这些信息不在本表中
6. 只能使用给定品类表；不知道的信息不要写
7. 每条不超过45个字

只返回bullet列表，不要其他内容。"""
    bullets = _gen_bullets_loose(prompt, fallback, max_tokens=260)
    bullets = _align_drilldown_marker(bullets, sel_short, selected_obj)

    overall_evol = (
        category_result.get("overall_total") or category_result.get("total", {})
    ).get("evol")
    highlight_obj = selected_obj or (categories[0] if categories else {})

    top_sku_title = ""
    if sku_result:
        tskus = sku_result.get("top_skus", [])
        if tskus:
            top_sku_title = (tskus[0].get("product_title", "") or "")[:20]

    intro_parts = [
        f"整体生意同比{_fmt_evol(overall_evol)}，"
        f"重点品类为{_short_category(highlight_obj.get('category_cn', ''))}"
        f"（占{_fmt_pct(highlight_obj.get('weight'))}，{_fmt_evol(highlight_obj.get('evol'))}）"
    ]
    if top_sku_title:
        intro_parts.append(f"，Top链接为「{top_sku_title}」")
    intro = "".join(intro_parts) + "。"

    return "\n".join(["# 品类分析", "", intro, "", "## 品类表现", "", bullets, "", table_md])


def render_module_channel_product(sku_result: dict) -> str:
    """品类分析子模块：从渠道和Top链接看渠道x货品贡献。"""
    top_skus = sku_result.get("top_skus", [])
    if not top_skus:
        return ""

    parts = ["## 渠道x货品"]

    if top_skus:
        top_rows = []
        for s in top_skus[:5]:
            top_rows.append({
                "链接": s.get("product_title", ""),
                "渠道": s.get("key_driver") or "其他",
                "本期GMV": s.get("gmv_current", 0),
                "占比": s.get("weight"),
            })
        top_df = pd.DataFrame(top_rows)
        parts += [
            "",
            "## Top5链接",
            "",
            df_to_markdown_table(
                top_df,
                gmv_cols=["本期GMV"],
                pct_cols=["占比"],
            ),
        ]

    return "\n".join(parts)


def _render_series_distribution_bullets(
    series_rows: list[dict],
    selected_series_label: str,
    requested_series: str,
) -> str:
    """Deterministic bullets for series distribution; avoids LLM denominator drift."""
    if not series_rows:
        return ""

    bullets = []
    used = set()

    selected_row = None
    if selected_series_label:
        selected_row = next((r for r in series_rows if r.get("系列") == selected_series_label), None)
    if selected_row:
        bullets.append(
            f"• {selected_row['系列']}占比{_fmt_pct(selected_row.get('占比'))}，"
            f"同比{_fmt_evol(selected_row.get('同比'))}，"
            f"份额{_fmt_pp(selected_row.get('占比变化'))}，进入下一张Top链接表。"
        )
        used.add(selected_row["系列"])
    elif requested_series:
        bullets.append(f"• 上表保留当前品类全量系列分布；下一张Top链接表单独筛选「{requested_series}」。")

    top_row = series_rows[0]
    if top_row.get("系列") not in used:
        bullets.append(
            f"• {top_row['系列']}以{_fmt_pct(top_row.get('占比'))}占比领跑，"
            f"同比{_fmt_evol(top_row.get('同比'))}。"
        )
        used.add(top_row["系列"])

    gain_rows = [
        r for r in series_rows
        if r.get("系列") not in used
        and r.get("占比变化") is not None
        and r.get("占比变化") > 0
    ]
    if gain_rows:
        gain_row = max(gain_rows, key=lambda r: r.get("占比变化") or 0)
        bullets.append(
            f"• {gain_row['系列']}份额提升{_fmt_pp(gain_row.get('占比变化'))}，"
            f"同比{_fmt_evol(gain_row.get('同比'))}。"
        )
        used.add(gain_row["系列"])

    growth_rows = [
        r for r in series_rows
        if r.get("系列") not in used
        and r.get("同比") is not None
        and (r.get("占比") or 0) >= 0.03
    ]
    if growth_rows and len(bullets) < 4:
        growth_row = max(growth_rows, key=lambda r: r.get("同比") or -999)
        bullets.append(
            f"• {growth_row['系列']}同比{_fmt_evol(growth_row.get('同比'))}，"
            f"占比{_fmt_pct(growth_row.get('占比'))}。"
        )

    return "\n".join(bullets[:4])


def render_module_drilldown(
    sku_result: dict,
    selected_category: str,
    selected_series: str,
) -> str:
    """模块3：系列分布 + Top链接（纵向下钻，两个子标题）"""
    product_lines = sku_result.get("product_lines", [])
    top_skus = sku_result.get("top_skus", [])
    if not top_skus:
        return ""

    sel_cat_short = _short_category(selected_category) if selected_category else "品类"
    parts = []

    # ── 子标题A：系列分布 ─────────────────────────────────────
    parts.append(f"### {sel_cat_short}下钻：产品系列分布")
    parts.append("> _产品系列由AI根据产品链接归纳总结，存在误差。_")

    if isinstance(product_lines, list) and product_lines:
        total_series_gmv = sum(p.get("gmv_current", 0) for p in product_lines) or 1
        total_series_gmv_25 = sum(p.get("gmv_prior", 0) for p in product_lines) or 0
        series_table_data = {}
        series_rows = []
        for p in product_lines:
            gmv = p.get("gmv_current", 0)
            w = round(gmv / total_series_gmv, 4)
            series_rows.append({
                "系列": p["product_line"],
                "本期GMV": gmv,
                "同比": p.get("evol"),
                "占比": w,
                "占比变化": _share_delta(gmv, total_series_gmv, p.get("gmv_prior", 0), total_series_gmv_25),
            })
            series_table_data[f"{p['product_line']}_占比"] = round(w * 100)
            if p.get("evol") is not None:
                series_table_data[f"{p['product_line']}_同比"] = round(p["evol"] * 100)

        series_df = pd.DataFrame(series_rows)
        series_table_md = df_to_markdown_table(
            series_df,
            gmv_cols=["本期GMV"],
            pct_cols=["占比"],
            evol_cols=["同比"],
            pp_cols=["占比变化"],
        )

        sel_series = selected_series or (product_lines[0]["product_line"] if product_lines else "")
        if selected_series and product_lines:
            exact_series = next(
                (p.get("product_line", "") for p in product_lines if p.get("product_line") == selected_series),
                "",
            )
            fuzzy_series = next(
                (
                    p.get("product_line", "")
                    for p in product_lines
                    if selected_series in p.get("product_line", "")
                    or p.get("product_line", "").replace("系列", "") == selected_series
                ),
                "",
            )
            sel_series = exact_series or fuzzy_series or selected_series
        selected_in_rows = bool(
            sel_series and any(row.get("系列") == sel_series for row in series_rows)
        )
        bullets_a = _render_series_distribution_bullets(
            series_rows,
            sel_series if selected_in_rows else "",
            selected_series or "",
        )
        parts += ["", bullets_a, "", series_table_md]
    else:
        parts.append("\n（产品系列归纳暂无数据）")

    parts.append("")

    # ── 子标题B：Top链接及渠道 ───────────────────────────────
    selected_series_label = selected_series
    if selected_series and product_lines:
        selected_series_label = next(
            (
                p.get("product_line", "")
                for p in product_lines
                if selected_series in p.get("product_line", "")
                or p.get("product_line", "").replace("系列", "") == selected_series
            ),
            selected_series,
        )
    parts.append(f"### {selected_series_label or sel_cat_short}Top5链接及渠道")

    # 筛选属于选定系列的SKU
    filtered_skus = top_skus
    if (
        isinstance(product_lines, list)
        and selected_series
        and not sku_result.get("top_skus_filtered_to_selected_series")
    ):
        series_obj = next((p for p in product_lines if p["product_line"] == selected_series), None)
        if series_obj and series_obj.get("item_ids"):
            matched = [s for s in top_skus if s["item_id"] in series_obj["item_ids"]]
            if matched:
                filtered_skus = matched

    filtered_skus = filtered_skus[:5]
    sku_total = sum(s.get("gmv_current", 0) for s in filtered_skus) or 1
    link_table_data = {}
    link_rows = []
    driver_mix = {}
    for i, s in enumerate(filtered_skus, 1):
        gmv = s.get("gmv_current", 0)
        w = round(gmv / sku_total, 4)
        driver = s.get("key_driver") or "其他"
        link_rows.append({
            "链接": s["product_title"],
            "本期GMV": gmv,
            "占比": w,
            "渠道": driver,
        })
        link_table_data[f"Top{i}_占比"] = round(w * 100)
        if driver not in driver_mix:
            driver_mix[driver] = {"gmv_current": 0}
        driver_mix[driver]["gmv_current"] += gmv

    link_df = pd.DataFrame(link_rows)
    link_table_md = df_to_markdown_table(
        link_df,
        gmv_cols=["本期GMV"],
        pct_cols=["占比"],
    )

    top_link = link_rows[0] if link_rows else {}
    driver_rows = []
    for driver, vals in driver_mix.items():
        g26 = vals["gmv_current"]
        driver_rows.append({
            "渠道": driver,
            "GMV占比": round(g26 / sku_total * 100),
        })
    driver_rows = sorted(driver_rows, key=lambda x: x["GMV占比"], reverse=True)

    prompt_links = []
    for r in link_rows[:6]:
        prompt_links.append({
            "链接": r["链接"],
            "渠道": r["渠道"],
            "GMV占比": round((r.get("占比") or 0) * 100),
        })

    prompt_b = f"""以下是"{selected_series_label or sel_cat_short}"Top链接及渠道数据：
渠道汇总：{json.dumps(driver_rows, ensure_ascii=False)}
Top链接明细：{json.dumps(prompt_links, ensure_ascii=False)}

请生成1-2条结论bullet，要求：
1. 每条以"•"开头
2. 从链接标题判断货品打法和用户心智：主打什么卖点（如美白/礼赠/套装）、结合什么活动（如618/付尾款）
3. 要给出解读和判断，不要只罗列关键词（错误示例：标题含"美白""618"；正确示例：以美白提亮套装为核心卖点，围绕618大促集中引流）
4. 可以说渠道占比，但不要推断全品类或全渠道策略
5. 只能使用上面给出的事实，不引入行业/竞品/大盘
6. 每条不超过40个字

只返回bullet列表，不要其他内容。"""

    if driver_rows:
        main_driver = driver_rows[0]
        fallback_b = f"Top5中{main_driver['渠道']}占GMV{main_driver['GMV占比']}%"
    else:
        fallback_b = f"Top1链接GMV占{round((top_link.get('占比') or 0) * 100)}%，为{top_link.get('渠道', '')}"
    bullets_b = _gen_bullets_loose(prompt_b, fallback_b)
    parts += ["", bullets_b, "", link_table_md]

    return "\n".join(parts)


def render_module_driver(driver_result: dict) -> str:
    """模块4：Key Driver分析 — bullets + 渠道表格 + 情报通旁注"""
    ds = driver_result.get("driver_summary", {})
    drivers = ds.get("drivers", [])
    total_26 = ds.get("total_gmv_current", 0) or sum(d.get("gmv_current", 0) for d in drivers)
    total_25 = ds.get("total_gmv_prior", 0) or sum(d.get("gmv_prior", 0) for d in drivers)
    overall_evol = (total_26 - total_25) / total_25 if total_25 else None

    rows = []
    table_data = {}
    for d in drivers:
        name = d["key_driver"]
        w = d.get("weight")
        w25 = round(d["gmv_prior"] / total_25, 4) if total_25 else None
        ev = d.get("evol")
        rows.append({
            "渠道": name,
            "本期GMV": d["gmv_current"],
            "同比": ev,
            "占比": w,
            "占比变化": None if w is None or w25 is None else w - w25,
        })
        if w is not None:
            table_data[f"{name}_占比"] = round(w * 100)
        if w25 is not None:
            table_data[f"{name}_去年同期占比"] = round(w25 * 100)
        if ev is not None:
            table_data[f"{name}_同比"] = round(ev * 100)

    df = pd.DataFrame(rows)
    table_md = df_to_markdown_table(
        df,
        gmv_cols=["本期GMV"],
        pct_cols=["占比"],
        evol_cols=["同比"],
        pp_cols=["占比变化"],
    )

    top = max(rows, key=lambda r: r["占比"] or 0) if rows else {}
    growth_rows = [r for r in rows if r.get("同比") is not None]
    fastest = max(growth_rows, key=lambda r: r["同比"]) if growth_rows else None

    bullet_lines = []
    if top:
        bullet_lines.append(
            f"• {top['渠道']}占比{round((top.get('占比') or 0) * 100)}%，"
            f"仍是最大生意来源"
        )
    if fastest and fastest.get("渠道") != top.get("渠道"):
        bullet_lines.append(
            f"• {fastest['渠道']}同比{_fmt_evol(fastest.get('同比'))}，"
            f"增长最快"
        )
    bullets = "\n".join(bullet_lines)

    intro = ""
    if top:
        intro = (
            f"整体生意同比{_fmt_evol(overall_evol)}。"
            f"主要增长渠道为{fastest['渠道'] if fastest else top['渠道']}"
            f"（同比{_fmt_evol((fastest or top).get('同比'))}，"
            f"占比{_fmt_pct((fastest or top).get('占比'))}），"
            f"{top['渠道']}仍贡献最大。"
        )

    parts = ["# Key Driver分析", ""]
    if intro:
        parts += [intro, ""]
    parts += [bullets, "", table_md]
    return "\n".join(parts)


def render_module_driver_products(driver_result: dict) -> str:
    """Key Driver子模块：按每个driver拉Top链接，让LLM总结各driver主推货品。"""
    driver_top_skus = driver_result.get("driver_top_skus", [])
    if not driver_top_skus:
        return ""

    ds = driver_result.get("driver_summary", {})
    drivers = ds.get("drivers", [])
    driver_map = {d.get("key_driver"): d for d in drivers}

    def driver_order(block: dict) -> float:
        summary = driver_map.get(block.get("key_driver"), {})
        return -(summary.get("gmv_current") or 0)

    # ── 跨渠道综合叙事 ──────────────────────────────────────────
    synthesis_data = []
    for block in sorted(driver_top_skus, key=driver_order):
        drv = block.get("key_driver")
        if not drv:
            continue
        drv_summary = driver_map.get(drv, {})
        plines = block.get("product_lines", [])
        tskus = block.get("top_skus", [])
        synthesis_data.append({
            "渠道": drv,
            "本期GMV": _fmt_gmv(drv_summary.get("gmv_current")),
            "同比": _fmt_evol(drv_summary.get("evol")),
            "占比": _fmt_pct(drv_summary.get("weight")),
            "核心系列": plines[0].get("product_line", "") if plines else "",
            "Top链接": (tskus[0].get("product_title", "") or "")[:20] if tskus else "",
        })

    synthesis_bullets = ""
    if synthesis_data:
        synthesis_prompt = f"""请像业务总监一样，综合多个渠道（{', '.join(d['渠道'] for d in synthesis_data)}），写1-2条综合分析bullet，说明品牌整体的货品打法和各渠道分工。
各渠道数据：{json.dumps(synthesis_data, ensure_ascii=False)}

要求：
1. 每条以"•"开头
2. 要跨渠道综合，不能只说一个渠道
3. 可以说某渠道"奠定基础"、某渠道"拉动增长"，但必须基于GMV占比和同比数据
4. 可以基于Top链接名称判断货品打法（如礼赠心智、美白提亮等），但表述要保守
5. 禁止提及行业、竞品、大盘
6. 每条不超过50个字

只返回bullet列表，不要其他内容。"""
        biggest = max(synthesis_data, key=lambda d: driver_map.get(d.get("渠道"), {}).get("weight") or 0, default={})
        synthesis_fallback = f"{biggest.get('渠道', '')}主导生意，各渠道货品分工明确"
        synthesis_bullets = _gen_bullets_loose(synthesis_prompt, synthesis_fallback, max_tokens=200, max_bullets=2)

    parts = ["## 渠道x货品", "", synthesis_bullets] if synthesis_bullets else ["## 渠道x货品"]

    for block in sorted(driver_top_skus, key=driver_order):
        driver = block.get("key_driver")
        top_skus = block.get("top_skus", [])
        product_lines = block.get("product_lines", [])
        if not driver or (not top_skus and not product_lines):
            continue

        summary = driver_map.get(driver, {})
        w26 = summary.get("weight")
        ev = summary.get("evol")
        ev_text = "新增" if ev is None else f"{'+' if ev >= 0 else ''}{round(ev * 100)}%"
        w26_text = "-" if w26 is None else f"{round(w26 * 100)}%"

        series_rows = []
        for p in product_lines[:8]:
            series_rows.append({
                "产品系列": p.get("product_line", ""),
                "本期GMV": p.get("gmv_current", 0),
                "同比": p.get("evol"),
                "占比": p.get("weight"),
                "占比变化": p.get("share_delta"),
            })
        series_table_md = ""
        if series_rows:
            series_table_md = df_to_markdown_table(
                pd.DataFrame(series_rows),
                gmv_cols=["本期GMV"],
                pct_cols=["占比"],
                evol_cols=["同比"],
                pp_cols=["占比变化"],
            )

        rows = []
        for s in top_skus[:5]:
            gmv = s.get("gmv_current", 0) or 0
            rows.append({
                "链接": s.get("product_title", ""),
                "本期GMV": gmv,
                "占比": s.get("weight"),
                "渠道": s.get("key_driver") or driver,
            })

        table_md = df_to_markdown_table(
            pd.DataFrame(rows),
            gmv_cols=["本期GMV"],
            pct_cols=["占比"],
            )

        category_mix = {}
        atv_values = []
        top_share = 0
        for s in top_skus:
            category = _short_category(s.get("category_cn", "")) or "其他品类"
            category_mix[category] = category_mix.get(category, 0) + (s.get("gmv_current") or 0)
            if s.get("atv"):
                atv_values.append(s.get("atv"))
        for s in top_skus[:5]:
            top_share += s.get("weight") or 0
        top_category = max(category_mix, key=category_mix.get) if category_mix else "核心品类"
        category_total = sum(category_mix.values()) or 1
        category_rows = [
            {"品类": k, "占Top链接GMV": round(v / category_total * 100)}
            for k, v in sorted(category_mix.items(), key=lambda x: x[1], reverse=True)[:3]
        ]
        atv_hint = ""
        if atv_values:
            atv_hint = f"GMV/件区间{_fmt_yuan(min(atv_values))}-{_fmt_yuan(max(atv_values))}（不是商品售价）"

        prompt_series = []
        for r in series_rows[:5]:
            prompt_series.append({
                "产品系列": r["产品系列"],
                "本期GMV": _fmt_gmv(r["本期GMV"]),
                "同比": _fmt_evol(r.get("同比")),
                "占driver比": _fmt_pct(r.get("占比")),
                "占比变化": _fmt_pp(r.get("占比变化")),
            })

        prompt_rows = []
        for idx, r in enumerate(rows[:5]):
            source = top_skus[idx]
            prompt_rows.append({
                "排名": idx + 1,
                "链接": r["链接"],
                "品类": _short_category(source.get("category_cn", "")),
                "本期GMV": _fmt_gmv(r["本期GMV"]),
                "占driver比": round((r.get("占比") or 0) * 100),
            })

        # ── series_bullets（只看产品系列表）──
        if series_rows:
            prompt_series_only = f"""请只基于以下产品系列表，写1-2条关于 Key Driver「{driver}」系列打法的分析bullet。
渠道表现：本期GMV占比{w26_text}，同比{ev_text}。
主要产品系列：{json.dumps(prompt_series, ensure_ascii=False)}

要求：
1. 每条以"•"开头，每行一条
2. 总结该driver主要售卖的产品系列，说明占比和增长/下滑趋势
3. 禁止提及链接、SKU、活动词；这些信息不在本表中
4. 避免只复述"占比xx%"，必须给出货品打法判断
5. 只能使用上面给出的事实，不引入行业、竞品、大盘
6. 每条不超过45个字

只返回bullet列表，不要其他内容。"""
            top_s_name = series_rows[0].get("产品系列", "") if series_rows else ""
            fallback_series = f"{driver} 主力系列为{top_s_name}，占driver比{_fmt_pct(series_rows[0].get('占比'))}"
            series_bullets = _gen_bullets_loose(prompt_series_only, fallback_series, max_tokens=180, max_bullets=2)
        else:
            series_bullets = ""

        # ── link_bullets（只看 Top5 链接表）──
        if rows:
            prompt_links_only = f"""请只基于以下Top5链接表，写1-2条关于 Key Driver「{driver}」链接打法的分析bullet。
Top5链接合计占driver {round(top_share * 100)}%。
Top链接品类结构：{json.dumps(category_rows, ensure_ascii=False)}
Top链接数据：{json.dumps(prompt_rows, ensure_ascii=False)}

要求：
1. 每条以"•"开头，每行一条
2. 从链接标题判断货品打法和用户心智：主打什么卖点（如美白/礼赠/套装）、结合什么活动（如618/付尾款）
3. 要给出解读和判断，不要只罗列关键词（错误：标题含"套装""618"；正确：以套装礼赠为主力，结合618大促集中引流）
4. 不要提渠道归属（这些链接已知属于同一渠道，无需再说）
5. 不要分析GMV/件数；这些数据不在展示表格中
6. 禁止提及产品系列；这些信息不在本表中
7. 只能使用上面给出的事实，不引入行业、竞品、大盘
8. 每条不超过45个字

只返回bullet列表，不要其他内容。"""
            main_cat_row = max(category_rows, key=lambda x: x.get("占Top链接GMV", 0), default={})
            fallback_links = f"Top5中{top_category}占比{main_cat_row.get('占Top链接GMV', '')}%"
            link_bullets = _gen_bullets_loose(prompt_links_only, fallback_links, max_tokens=180, max_bullets=2)
        else:
            link_bullets = ""

        parts += [
            "",
            f"### {driver}",
        ]
        if series_table_md:
            parts += [
                "",
                "#### 主要产品系列",
                "",
                series_bullets,
                "",
                series_table_md,
            ]
        if table_md:
            parts += [
                "",
                "#### Top5链接",
                "",
                link_bullets,
                "",
                table_md,
            ]

    return "\n".join(parts)


# ── 主入口 ────────────────────────────────────────────────────

def format_report(
    category_result: dict,
    driver_result: dict,
    sku_result: dict,
    selected_category: str = "",
    selected_series: str = "",
    fraud_result: dict = None,
    ttl_result: dict | None = None,
) -> str:
    brand = category_result.get("brand", "")
    period = category_result.get("period", "")
    period_meta = category_result.get("period_meta") or {}
    coverage = ""
    if period_meta.get("source_max_date"):
        coverage = f"；当前品牌数据更新至{period_meta['source_max_date']}"
    header = "\n".join([
        "数据来源：",
        f"• 天猫品牌旗舰店链接：百库驾驶舱-天猫-商品-日表{coverage}。",
        "• TTL GMV：ECIP MASS Pure Mass Market Ranking (TTL Beauty)，月表优先，日表补充未覆盖日期。",
    ])

    category_parts = [
        render_module_category(category_result, selected_category, sku_result),
        render_module_drilldown(sku_result, selected_category, selected_series),
    ]
    category_section = "\n\n".join(m for m in category_parts if m.strip())

    driver_parts = [
        render_module_driver(driver_result),
        render_module_driver_products(driver_result),
    ]
    driver_section = "\n\n".join(m for m in driver_parts if m.strip())

    modules = [
        render_module_overall(category_result, fraud_result, ttl_result),
        category_section,
        driver_section,
    ]
    non_empty = [m for m in modules if m.strip()]
    return header + "\n\n---\n\n" + "\n\n---\n\n".join(non_empty)


def render_module_playbook(playbook_result: dict) -> str:
    """打法解读：场景标签 + 系列/功能线信号。"""
    if not playbook_result or playbook_result.get("error"):
        return playbook_result.get("message", "") if isinstance(playbook_result, dict) else ""

    scene_tags = playbook_result.get("scene_tags", [])
    series_rows = playbook_result.get("series", [])
    bullets = playbook_result.get("bullets", "")

    parts = ["# 打法解读"]
    if bullets:
        parts += ["", bullets]

    if scene_tags:
        df = pd.DataFrame(scene_tags[:10]).rename(columns={
            "tag": "场景标签",
            "gmv": "GMV",
            "unit": "件数",
            "atv": "GMV/件",
            "weight": "占比",
            "link_count": "链接数",
        })
        parts += [
            "",
            "## 场景标签",
            "",
            df_to_markdown_table(
                df,
                gmv_cols=["GMV"],
                pct_cols=["占比"],
            ),
        ]

    if series_rows:
        df = pd.DataFrame(series_rows[:10]).rename(columns={
            "product_line": "系列",
            "function_tag": "功能线",
            "gmv_current": "本期GMV",
            "unit_current": "件数",
            "atv_current": "GMV/件",
            "weight": "占比",
            "evol": "同比",
        })
        keep = [c for c in ["系列", "功能线", "本期GMV", "同比", "占比", "件数", "GMV/件"] if c in df.columns]
        parts += [
            "",
            "## 系列/功能线",
            "",
            df_to_markdown_table(
                df[keep],
                gmv_cols=["本期GMV"],
                evol_cols=["同比"],
                pct_cols=["占比"],
            ),
        ]

    return "\n".join(parts)

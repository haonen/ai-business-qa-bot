from __future__ import annotations

import json

from bot.formatter import render_module_category, render_module_driver, render_module_drilldown, render_module_playbook
from bot.session import SessionState
from bot.skills.loader import build_skill_prompt, load_narrative_config
from bot.tools import query_category, query_driver, query_scene_tag, query_series, query_sku_list
from bot.utils import llm_client, extract_json_object


def dispatch_skill(followup_text: str, state: SessionState) -> dict:
    ctx = state.drilldown_ctx
    prompt = f"""
你是生意分析助手的路由模块。当前上下文：
品牌={ctx.brand}，时间段={ctx.period}，已锁品类={ctx.category}，已锁渠道={ctx.kol_driver}

用户追问："{followup_text}"

判断使用哪个Skill，只返回JSON：
{{"skill":"category_drill"|"key_driver"|"sku_investigation"|"reverse_drill"|"playbook_read"|"attribution_lite"|"unsupported","confidence":"high"|"low"}}
"""
    try:
        resp = llm_client().chat.completions.create(
            model="qwen-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=80,
        )
        parsed = extract_json_object(resp.choices[0].message.content or "")
        if parsed:
            return parsed
    except Exception:
        pass
    if any(x in followup_text for x in ["打法", "推什么", "人群", "场景"]):
        return {"skill": "playbook_read", "confidence": "high"}
    if any(x in followup_text for x in ["为什么", "涨", "跌"]):
        return {"skill": "attribution_lite", "confidence": "high"}
    return {"skill": "category_drill", "confidence": "low"}


def _playbook_bullets(scene_result: dict, series_result: dict) -> str:
    config = load_narrative_config()
    threshold = config.get("scene_gmv_threshold", 0.10)
    top_scene = next((s for s in scene_result.get("scene_tags", []) if (s.get("weight") or 0) >= threshold), None)
    top_series = next((s for s in series_result.get("series", []) if (s.get("weight") or 0) >= 0.10), None)
    if top_scene and top_series:
        return (
            f"• 标题场景呈现{top_scene['tag']}聚焦：场景GMV占比{top_scene['weight'] * 100:.0f}%，"
            f"{top_series.get('product_line') or top_series.get('series')}占比{top_series.get('weight') * 100:.0f}%。"
        )
    if top_scene:
        return f"• 标题场景呈现{top_scene['tag']}集中：场景GMV占比{top_scene['weight'] * 100:.0f}%。"
    return "• 当前场景标签信号不够集中，暂不输出打法判断。"


def run_filter_update(state: SessionState, update: dict) -> dict:
    ctx = state.drilldown_ctx
    category = ctx.category
    view = update.get("view")
    if view == "driver" or update.get("kol_driver"):
        result = query_driver(brand=ctx.brand, period=ctx.period, category=category, series=ctx.series, function_tag=ctx.function_tag)
        return {"markdown": render_module_driver(result), "meta": {"last_analysis_view": "driver"}}
    if view == "playbook":
        scene = query_scene_tag(brand=ctx.brand, period=ctx.period, kol_driver=ctx.kol_driver, category=category)
        series = query_series(brand=ctx.brand, period=ctx.period, category=category or "")
        return {
            "markdown": render_module_playbook({
                "scene_tags": scene.get("scene_tags", []),
                "series": series.get("series", []),
                "bullets": _playbook_bullets(scene, series),
            }),
            "meta": {"last_analysis_view": "playbook"},
        }
    if view == "sku" or update.get("category"):
        series = query_series(brand=ctx.brand, period=ctx.period, category=category)
        sku = query_sku_list(brand=ctx.brand, period=ctx.period, category=category, top_n=20)
        sku["product_lines"] = series.get("series", [])
        return {
            "markdown": render_module_drilldown(sku, category, ""),
            "meta": {"last_analysis_view": "category_drilldown", "category": category},
        }
    result = query_category(brand=ctx.brand, period=ctx.period, kol_driver=ctx.kol_driver, link_type=ctx.link_type, series=ctx.series, function_tag=ctx.function_tag)
    return {"markdown": render_module_category(result, category or ""), "meta": {"last_analysis_view": "category"}}


def run_skill_chain(followup_text: str, state: SessionState) -> dict:
    ctx = state.drilldown_ctx
    if not ctx.brand or not ctx.period:
        return {"markdown": "我需要先知道品牌和时间段。可以先问：某品牌618怎么样。", "meta": {}}
    decision = dispatch_skill(followup_text, state)
    skill = decision.get("skill")
    if decision.get("confidence") == "low" and skill == "unsupported":
        return {"markdown": "这个追问当前数据不支持。", "meta": {}}

    # Load the Skill prompt to keep the method contract explicit even for deterministic paths.
    if skill != "attribution_lite" and skill != "unsupported":
        build_skill_prompt(skill, {"ctx": ctx.__dict__}, followup_text)

    category = ctx.category
    if not category and state.last_result_cache:
        category = state.last_result_cache.get("selected_category") or state.last_result_cache.get("category")

    if skill == "playbook_read":
        scene = query_scene_tag(brand=ctx.brand, period=ctx.period, kol_driver=ctx.kol_driver, category=category)
        series = query_series(brand=ctx.brand, period=ctx.period, category=category or "")
        return {
            "markdown": render_module_playbook({
                "scene_tags": scene.get("scene_tags", []),
                "series": series.get("series", []),
                "bullets": _playbook_bullets(scene, series),
            }),
            "meta": {"last_analysis_view": "playbook"},
        }

    if skill == "attribution_lite":
        if not category:
            cat = query_category(brand=ctx.brand, period=ctx.period)
            category = (cat.get("categories") or [{}])[0].get("category_cn")
        series = query_series(brand=ctx.brand, period=ctx.period, category=category)
        sku = query_sku_list(brand=ctx.brand, period=ctx.period, category=category, top_n=10)
        sku["product_lines"] = series.get("series", [])
        note = "\n\n_以上为系列级拆解。链接级的年度归因（具体哪条链接带来变化）当前数据暂不支持。_"
        return {"markdown": render_module_drilldown(sku, category, "") + note, "meta": {"last_analysis_view": "attribution_lite"}}

    if skill == "key_driver":
        result = query_driver(brand=ctx.brand, period=ctx.period, category=category)
        return {"markdown": render_module_driver(result), "meta": {"last_analysis_view": "driver"}}

    if skill == "sku_investigation":
        sku = query_sku_list(brand=ctx.brand, period=ctx.period, category=category, kol_driver=ctx.kol_driver, series=ctx.series, function_tag=ctx.function_tag, top_n=20)
        series = query_series(brand=ctx.brand, period=ctx.period, category=category or sku.get("category") or "")
        sku["product_lines"] = series.get("series", [])
        return {"markdown": render_module_drilldown(sku, category or "", ""), "meta": {"last_analysis_view": "sku"}}

    cat = query_category(brand=ctx.brand, period=ctx.period, kol_driver=ctx.kol_driver, link_type=ctx.link_type, series=ctx.series, function_tag=ctx.function_tag)
    return {"markdown": render_module_category(cat, category or ""), "meta": {"last_analysis_view": "category"}}

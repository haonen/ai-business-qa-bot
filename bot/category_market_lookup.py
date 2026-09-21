"""Atomic Newsletter 02 category-market and displayed-brand share entry."""
import json
import logging
import os
import re
from datetime import date
from decimal import Decimal

from bot.category_market_metrics import DISPLAY_BRANDS, query_category_metrics, query_maybelline_remover_gmv
from bot.llm_router import _message_day
from bot.session import _mutate_session
from bot.utils import extract_json_object, llm_client, llm_model

log = logging.getLogger(__name__)
TRIGGER = re.compile(r"护肤|洗护|男士|彩妆|skincare|hair|makeup|mex|品类大盘|(?:美宝莲|maybelline|mny).*(?:份额|市占|ms%|market share|gain share|lose share)", re.I)
SHARE_QUESTION = re.compile(r"份额|市占|ms\s*%|market\s*share|gain\s*share|lose\s*share", re.I)
INSTRUCTIONS = '''你是Newsletter 02品类市场原子查数入口，只输出JSON。
先判断用户是否在问品类市场指标，再判断平台是否受支持。品牌经营分析必须action=pass，即使出现彩妆/护肤和单个平台，也不能返回unsupported；包括02展示品牌自身的普通生意分析。已有02上下文不能覆盖用户明确的新品牌分析请求。
例如“请分析IORE在2026年9月6日至2026年9月12日、抖音平台的彩妆品类生意。”=>pass；“韩束天猫护肤生意如何”=>pass；“抖音彩妆大盘GMV多少”=>unsupported；02查询后仅追问“那天猫呢”=>unsupported。
只支持三平台合计（TTL）的02品类指标，不支持单个平台、两平台组合或分平台列表。
品类枚举：SKIN=护肤/Skincare（默认包含男士），FEMALE_SKIN=Female Skin/女士护肤/非男士护肤/护肤去掉男士，HAIR=洗护/Hair，MEX=男士/男士护肤，MAKEUP=彩妆/Makeup。
要根据整句语义和活动上下文区分三种护肤口径：用户要剔除、扣除、排除或不计男士时选FEMALE_SKIN；要单独看男士时选MEX；要护肤整体或将男士加回时选SKIN。不能只因为句子出现“男士”就选MEX。
示例：活动SKIN上下文中“去掉男士呢”=>modify+FEMALE_SKIN；“那单看男士呢”=>modify+MEX；活动FEMALE_SKIN上下文中“把男士也算进去”=>modify+SKIN。
支持每个品类的Total Beauty市场GMV/Evol%、Pure Mass GMV/Evol%，以及02展示品牌的GMV/Evol%、Pure Mass份额和同比份额变化（百分点）、gain/lose share。SKIN、FEMALE_SKIN与MAKEUP同时展示对应品类的CPD汇总（巴黎欧莱雅、3CE、美宝莲、Dr.G按该品类取数）。
展示品牌：SKIN/FEMALE_SKIN有L'OREAL PARIS（巴黎欧莱雅/巴欧）和MAYBELLINE（美宝莲/MNY）；HAIR/MEX只有L'OREAL PARIS；MAKEUP有L'OREAL PARIS、3CE、MAYBELLINE。
若只问“美宝莲market share/gain share”等份额问题且未指明品类，可选MAKEUP作为入口；程序会同时展示护肤、彩妆及（卸妆＋彩妆）÷彩妆Pure Mass三种份额。不要因此把普通品牌经营分析误路由到02。
品牌未指定填null，代码会返回该品类全部展示品牌。品牌与品类不匹配则clarify。
日期必须来自用户明确表达或紧接本入口的上下文。相对日期按北京时间。上周是上一完整周一至周日。
新问题action=new；同一02查询的“同比呢/巴黎欧莱雅呢/换彩妆/换上周”action=modify，未修改字段填null。
MEX填写用户请求的原始日期，不能自行缩成7天。历史数据完整时保持请求窗口；最新数据不齐时由工具检查并说明实际可用窗口。
普通单品牌经营分析、商品、渠道、BET、原因和建议action=pass交回原Bot。03榜单/Top Rising也pass。
01整体市场Total Beauty/Pure Mass或整体CPD请求action=pass，不能混用01和02。
无日期则start/end都填null。只给单日则start=end。取消/停止action=pass。
JSON字段：action(new|modify|pass|unsupported|clarify)、category(SKIN|FEMALE_SKIN|HAIR|MEX|MAKEUP|null)、brand(L'OREAL PARIS|3CE|MAYBELLINE|null)、start(YYYY-MM-DD|null)、end(YYYY-MM-DD|null)、reason(简短中文)。'''


def interpret(text, context, received_at):
    response = llm_client(max_retries=0).chat.completions.create(
        model=llm_model("router"),
        messages=[{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": json.dumps({
            "message_date": _message_day(received_at), "timezone": "Asia/Shanghai",
            "active_category_query": context, "text": text,
        }, ensure_ascii=False)}],
        response_format={"type": "json_object"}, extra_body={"enable_thinking": False},
        max_tokens=600, temperature=0, timeout=25,
    )
    parsed = extract_json_object(response.choices[0].message.content or "")
    log.info("[category_lookup] interpreted action=%s reason=%s", parsed.get("action") if isinstance(parsed, dict) else None, parsed.get("reason") if isinstance(parsed, dict) else None)
    display_alias = re.search(r"巴黎欧莱雅|巴欧|l['’]?oreal|3ce|美宝莲|maybelline|mny", text, re.I)
    if (isinstance(parsed, dict) and parsed.get("action") in ("new", "modify")
            and parsed.get("brand") is None and re.search(r"生意(?:如何|怎么样|表现)|经营(?:如何|怎么样|表现)", text)
            and not display_alias):
        return {"action": "pass", "reason": "非02展示品牌的经营问题交回原Bot"}
    if (context and isinstance(parsed, dict) and parsed.get("action") in ("new", "modify")
            and parsed.get("start") and parsed.get("end")
            and re.fullmatch(r"(?:那就|改成|换成|时间改成)?[\d\s年月日号至到~～—–./-]+", text.strip())):
        parsed["action"] = "modify"
        parsed["category"] = parsed["brand"] = None
    if (context and isinstance(parsed, dict) and parsed.get("action") == "new"
            and re.match(r"\s*(?:换成|改成|那看|再看)", text)):
        parsed["action"] = "modify"
    return parsed


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid request")
    action = payload.get("action")
    if action in ("pass", "unsupported", "clarify"):
        return {"action": action, "reason": str(payload.get("reason") or "")[:220]}
    if action not in ("new", "modify"):
        raise ValueError("invalid action")
    if payload.get("category") not in (None, *DISPLAY_BRANDS):
        raise ValueError("invalid category")
    if payload.get("brand") not in (None, "L'OREAL PARIS", "3CE", "MAYBELLINE"):
        raise ValueError("invalid brand")
    for key in ("start", "end"):
        if payload.get(key) is not None:
            date.fromisoformat(payload[key])
    if bool(payload.get("start")) != bool(payload.get("end")):
        raise ValueError("incomplete period")
    if payload.get("start") and payload["end"] < payload["start"]:
        raise ValueError("reversed period")
    return payload


def _save(open_id, value):
    _mutate_session(open_id, lambda state: setattr(state, "category_market_lookup_context", value))


def _amount(value):
    return "不可计算" if value is None else f"{Decimal(value)/Decimal(1000000):,.2f}M（{Decimal(value):,.0f}元）"


def _pct(value, signed=False):
    if value is None:
        return "不可计算"
    return f"{Decimal(value):+,.2f}%" if signed else f"{Decimal(value):,.2f}%"


def _maybelline_share_table(comparison):
    lines = [
        "\n**美宝莲 · 三种 MS 口径**",
        "| MS 口径 | GMV | GMV Evol% | MS% | MS%+/- |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for formula, row in (
        ("护肤 ÷ 护肤 Pure Mass", comparison["skin"]),
        ("彩妆 ÷ 彩妆 Pure Mass", comparison["makeup"]),
        ("（卸妆＋彩妆）÷ 彩妆 Pure Mass", comparison["remover_makeup"]),
    ):
        delta = row["share_change_pp"]
        delta_text = "不可计算" if delta is None else f"{Decimal(delta):+.2f}%"
        gmv_text = f"{Decimal(row['current_gmv_yuan']) / Decimal(1000000):,.2f}M"
        lines.append(f"| {formula} | {gmv_text} | {_pct(row['evol_pct'], True)} | {_pct(row['current_share_pct'])} | {delta_text} |")
    if comparison["skin_period"] != comparison["makeup_period"]:
        lines.append("\n护肤与彩妆实际数据周期不同，不宜直接比较。")
    return lines


def format_result(result, brand=None, comparison=None, comparison_warning=None):
    names = {
        "SKIN": "SKIN 护肤（含男士）",
        "FEMALE_SKIN": "FEMALE SKIN 护肤（剔除男士）",
        "HAIR": "HAIR 洗护", "MEX": "MEX 男士", "MAKEUP": "MAKEUP 彩妆",
    }
    requested, effective = result["requested_period"], result["effective_period"]
    lines = [f"范围：{effective['start']}至{effective['end']}｜三平台合计｜Newsletter 02｜{names[result['category']]}"]
    if requested != effective:
        lines.append(f"请求期间：{requested['start']}至{requested['end']}；男士按最新可用连续7天窗口展示。")
    lines.extend([
        "\n**品类市场**",
        "Total Beauty GMV：" + _amount(result["market_gmv_yuan"]),
        "Total Beauty Evol%（同比）：" + _pct(result["market_evol_pct"], True),
        "Pure Mass GMV：" + _amount(result["pure_mass_gmv_yuan"]),
        "Pure Mass Evol%（同比）：" + _pct(result["pure_mass_evol_pct"], True),
    ])
    if result.get("cpd"):
        cpd = result["cpd"]
        change = cpd["share_change_pp"]
        lines.extend([
            "\n**CPD（本品类；巴黎欧莱雅、3CE、美宝莲、Dr.G）**",
            "GMV：" + _amount(cpd["current_gmv_yuan"]),
            "Evol%（同比）：" + _pct(cpd["evol_pct"], True),
            "MS%：" + _pct(cpd["current_share_pct"]) + "；去年同期：" + _pct(cpd["prior_share_pct"]),
            "MS%+/-：" + ("不可计算" if change is None else f"{Decimal(change):+.2f}个百分点") + "。",
        ])
    selected = [r for r in result["brands"] if not brand or r["brand"].upper() == brand.upper()
                or (brand == "MAYBELLINE" and r["brand"] == "Maybelline")]
    for row in selected:
        label = {"L'OREAL PARIS": "巴黎欧莱雅", "Maybelline": "美宝莲"}.get(row["brand"], row["brand"])
        if row["brand"] == "Maybelline" and comparison:
            lines.extend(_maybelline_share_table(comparison))
            continue
        change = row["share_change_pp"]
        direction = "无法判断gain/lose share" if change is None else (
            "gain share" if Decimal(change) > 0 else "lose share" if Decimal(change) < 0 else "份额持平")
        lines.extend([
            f"\n**{label}**",
            "GMV：" + _amount(row["current_gmv_yuan"]),
            "Evol%（同比）：" + _pct(row["evol_pct"], True),
            "MS%：" + _pct(row["current_share_pct"]) + "；去年同期：" + _pct(row["prior_share_pct"]),
            "MS%+/-：" + ("不可计算" if change is None else f"{Decimal(change):+.2f}个百分点") + f"（{direction}）。",
        ])
    if comparison_warning:
        lines.append("\n美宝莲跨品类份额对照暂不可计算：" + comparison_warning)
    lines.extend("\nRemark：" + warning for warning in result.get("warnings", []))
    lines.append("\n口径：品牌及CPD的MS%=对应品类GMV/同品类Pure Mass；MS%+/-对比去年同期。Makeup去除香水；京东大盘只计自营；Pure Mass按四类店铺倒减。美宝莲的卸妆按店铺表二级类目makeup remover取数。")
    return "\n".join(lines)


def try_category_lookup(open_id, text, session, received_at=None, *, parsed_request=None):
    if os.environ.get("NEWSLETTER_CATEGORY_LOOKUP_ENABLED", "0") != "1":
        return None
    context = dict(session.category_market_lookup_context or {})
    if parsed_request is None and not context and not TRIGGER.search(text):
        return None
    try:
        parsed = validate(parsed_request if parsed_request is not None else interpret(text, context, received_at))
    except Exception:
        log.exception("category market interpretation failed")
        return {"route_type": "newsletter_category_lookup", "markdown": "暂时无法确认02品类范围，请明确品类和日期。", "meta": {"document_ready": False}}
    action = parsed["action"]
    if action == "pass":
        _save(open_id, {})
        return None
    if action == "unsupported":
        return {"route_type": "newsletter_category_lookup", "markdown": "Newsletter 02目前支持三平台合计的护肤（含男士）、Female Skin（剔除男士）、洗护、男士和彩妆；单平台和分平台列表尚未接入。", "meta": {"document_ready": False}}
    if action == "clarify":
        return {"route_type": "newsletter_category_lookup", "markdown": parsed.get("reason") or "请明确品类和日期。", "meta": {"document_ready": False, "awaiting": "category_scope"}}
    if action == "modify" and not context:
        return {"route_type": "newsletter_category_lookup", "markdown": "请先说明品类和日期。", "meta": {"document_ready": False, "awaiting": "category_scope"}}
    scope = context if action == "modify" else {}
    for key in ("category", "brand", "start", "end"):
        if parsed.get(key) is not None:
            scope[key] = parsed[key]
    if scope.get("brand") and scope.get("category") and scope["brand"] not in DISPLAY_BRANDS[scope["category"]]:
        return {"route_type": "newsletter_category_lookup", "markdown": "这个品牌不在该品类的Newsletter 02展示品牌中，请确认品牌或品类。", "meta": {"document_ready": False, "awaiting": "category_brand"}}
    _save(open_id, scope)
    from bot.session import CLEAR, SET, SlotUpdate, TaskContextPatch, apply_task_context_patch
    task_values = {
        "brand": scope.get("brand"),
        "period": (scope["start"] + "~" + scope["end"]) if scope.get("start") else None,
        "platform": "TTL",
        "category": scope.get("category"),
        # Newsletter 02 displays both market and Pure Mass, while its brand
        # shares and the Newsletter 03 brand ranking use Pure Mass as the
        # actionable brand universe.  Persist the executable segment instead
        # of the presentation label so a ranking follow-up does not ask the
        # user to clarify an internal UI name.
        "segment": "PURE MASS",
        "awaiting_slot": None if scope.get("start") and scope.get("category") else (
            "category" if not scope.get("category") else "period"
        ),
    }
    apply_task_context_patch(
        open_id,
        TaskContextPatch(
            relation="NEW_TASK" if action == "new" else "MODIFY_SCOPE",
            slots={
                key: SlotUpdate(SET if value is not None else CLEAR, value)
                for key, value in task_values.items()
            },
            add_intents=["MARKET"],
            add_goals=["MARKET_GMV"],
            current_turn=text,
            reason_codes=["ATOMIC_CATEGORY_MARKET_LOOKUP"],
        ),
    )

    def clear_stale_state(state):
        state.pending_request = None
        state.brand_lookup_context = {}
        if action == "new":
            from copy import deepcopy
            from bot.session import SessionState

            fresh = SessionState()
            state.market_lookup_context = {}
            for key in (
                "ec_context", "bet_context", "drilldown_ctx", "active_plan",
                "last_result_cache", "market_context",
            ):
                setattr(state, key, deepcopy(getattr(fresh, key)))

    _mutate_session(open_id, clear_stale_state)
    if not scope.get("category"):
        return {"route_type": "newsletter_category_lookup", "markdown": "请说明品类：护肤、洗护、男士或彩妆。", "meta": {"document_ready": False, "awaiting": "category"}}
    if not scope.get("start"):
        return {"route_type": "newsletter_category_lookup", "markdown": "请提供查询日期。已保留品类范围。", "meta": {"document_ready": False, "awaiting": "period"}}
    try:
        facts = query_category_metrics(scope["start"], scope["end"], scope["category"])
    except ValueError as exc:
        log.warning("category market scope unavailable: %s", exc)
        return {
            "route_type": "newsletter_category_lookup",
            "markdown": str(exc),
            "meta": {"document_ready": False, "failure_kind": "CATEGORY_MARKET_INVALID_SCOPE"},
        }
    except Exception:
        log.exception("category market query failed")
        return {"route_type": "newsletter_category_lookup", "markdown": "品类市场数据查询暂时失败，查询条件已保留，请稍后重试。", "meta": {"document_ready": False}}
    comparison = None
    comparison_warning = None
    if (scope.get("brand") == "MAYBELLINE" and SHARE_QUESTION.search(text)
            and scope["category"] in ("SKIN", "FEMALE_SKIN", "MAKEUP")):
        try:
            from bot.category_market_metrics import maybelline_share_comparison
            skin = facts if scope["category"] == "SKIN" else query_category_metrics(scope["start"], scope["end"], "SKIN")
            makeup = facts if scope["category"] == "MAKEUP" else query_category_metrics(scope["start"], scope["end"], "MAKEUP")
            remover = query_maybelline_remover_gmv(scope["start"], scope["end"])
            comparison = maybelline_share_comparison(skin, makeup, remover)
        except ValueError as exc:
            comparison_warning = str(exc)
        except Exception:
            log.exception("Maybelline cross-category share comparison failed")
            comparison_warning = "另一品类的数据查询失败，请稍后重试。"
    visible = {k: v for k, v in facts.items() if k != "audit"}
    if comparison:
        visible["maybelline_share_comparison"] = comparison
    return {"route_type": "newsletter_category_lookup", "markdown": format_result(facts, scope.get("brand"), comparison, comparison_warning),
            "meta": {"document_ready": False, "category_facts": visible}}

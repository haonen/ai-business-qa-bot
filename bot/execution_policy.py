"""Semantic execution selection; never infer a route from brand/category keywords."""
import os

STANDARD_ROUTES = {
    'default_chain': 'TM', 'douyin_business_analysis': 'DY',
    'jd_business_analysis': 'JD', 'three_platform_competitor_analysis': 'TTL',
    'media_analysis': None,
}


def enabled():
    return os.environ.get('SEMANTIC_EXECUTION_ENABLED', '0') == '1'


def use_standard_execution(route):
    if not enabled():
        return False
    decision = route.route_decision or {}
    if decision.get('missing_slots') or decision.get('unsupported_requests'):
        return False
    profile = decision.get('execution_profile')
    if profile == 'followup' and route.type == 'skill_dispatch':
        return True  # Existing evidence coverage/missing-data planner handles this.
    return bool(
        profile == 'standard' and route.type in STANDARD_ROUTES
        and route.brand and route.period
        and (route.type == 'media_analysis' or route.platform == STANDARD_ROUTES[route.type])
        and decision.get('question_mode') == 'report'
        and decision.get('response_strategy') == 'FULL_REPORT'
        and len(decision.get('intents') or []) == 1
        and not decision.get('comparison_periods')
    )


def validate_atomic(value):
    if not isinstance(value, dict):
        raise ValueError('invalid atomic request')
    kind = value.get('kind')
    if kind == 'category' and os.environ.get('NEWSLETTER_CATEGORY_LOOKUP_ENABLED') == '1':
        from bot.category_market_lookup import validate
    elif kind == 'market' and os.environ.get('NEWSLETTER_MARKET_LOOKUP_ENABLED') == '1':
        from bot.market_lookup import validate
    else:
        raise ValueError('atomic capability is disabled or unknown')
    payload = validate(value.get('request'))
    if payload['action'] == 'pass':
        raise ValueError('pass requires a general routing envelope instead')
    return {'kind': kind, 'request': payload}


def clear_atomic_contexts(open_id, session):
    from bot.session import _mutate_session
    def clear(state):
        state.market_lookup_context = {}
        state.category_market_lookup_context = {}
    _mutate_session(open_id, clear)
    clear(session)


def dispatch_atomic(value, open_id, text, session, received_at):
    value = validate_atomic(value)
    if value['kind'] == 'category':
        from bot.category_market_lookup import try_category_lookup as handler
    else:
        from bot.market_lookup import try_lookup as handler
    if value['request']['action'] in {'new', 'modify'}:
        from bot.session import _mutate_session
        opposite = 'market_lookup_context' if value['kind'] == 'category' else 'category_market_lookup_context'
        _mutate_session(open_id, lambda state: setattr(state, opposite, {}))
        setattr(session, opposite, {})
    answer = handler(open_id, text, session, received_at, parsed_request=value['request'])
    if answer is None:
        raise ValueError('selected atomic capability did not produce an answer')
    answer.setdefault('meta', {})['execution_profile'] = 'atomic'
    return answer


def routing_instructions(state=None):
    if not enabled():
        return ''
    rules = '''
执行方式：在普通路由JSON中增加execution_profile=standard|followup|dynamic。
standard仅用于五套模版能完整回答的常规完整报告：三平台、天猫、京东、抖音生意报告以及BET报告。
如“分析JF LABB 9月6至12日抖音护肤生意”是standard；常规品类/商品/渠道表现属于模版内容。
请求模版以外的问题、解释原因、建议、行动、竞品对比、自定义对比期间、多品牌或多目标联合分析必须dynamic，保留全部goals，不能为了提速遗漏额外诉求。
已有报告的定向追问可选followup，由已有追问工具检查证据，只补缺失数据；更换日期、品牌或平台不能沿用旧数。
不能确定是否完整覆盖时选dynamic。execution_profile不改变用户的日期/平台/品类/品牌和路由。

Newsletter原子查数同样由你在本次调用判断，不再调用第二个分类模型。
若是下列已启用的01或02查数，输出 {"atomic_request":{"kind":"market或category","request":{对应的JSON字段}}}，无需普通路由字段。
先判断整句：单品牌经营分析应输出普通路由JSON，不能因为提到护肤或抖音就进入Newsletter。
只有原子查数选择才适用下面各入口的限制。跨入口选择以用户当前意图为准：品类市场走category，整体市场/CPD走market；榜单/深挖/开放问题走普通路由，不能让旧入口的unsupported吞掉已有能力。
active_market_query和active_category_query分别是两个入口的活动上下文；原子modify只继承相应入口。显式新品牌请求优先于旧上下文。
'''
    if os.environ.get('NEWSLETTER_MARKET_LOOKUP_ENABLED') == '1':
        rules += """
market已启用：01整体市场Total Beauty/Beauty Market/TTL Beauty与Pure Mass的GMV、去年同期、Evol%同比，以及CPD整体GMV、同比、Pure Mass份额和gain/lose share。CPD是四品牌组合，不是单品牌。只有未限定品类的整体CPD才能走market；“Skin市场的CPD gain share”必须走category+SKIN。
request字段：action(new|modify|unsupported|clarify),platform(TM|JD|DY|TTL|null),segment(BEAUTY MARKET|PURE MASS|BOTH|null),metric(gmv|yoy|prior|all|null),start(YYYY-MM-DD|null),end(YYYY-MM-DD|null),include_cpd(bool|null),reason(中文)。
不指定平台或口径可填null，工具默认三平台和两种口径；没日期需填null，不能从旧品牌报告继承。无对象的份额问题需clarify；紧接市场查询问gain share则是CPD。两个平台合计、分平台列表和环比暂时unsupported。榜单、品类问题应转普通路由或category，不能unsupported。
有unsupported_request时模糊追问需要clarify；明确重回市场查询才恢复继承。
"""
    if os.environ.get('NEWSLETTER_CATEGORY_LOOKUP_ENABLED') == '1':
        rules += """
category已启用：02三平台合计各品类Total Beauty/Pure Mass金额与同比、展示品牌GMV/同比/份额/份额同比变化；SKIN、FEMALE_SKIN和MAKEUP还给出对应品类的CPD汇总（巴黎欧莱雅、3CE、美宝莲、Dr.G按品类取数）。
“护肤/Skincare/Skin市场的CPD份额、gain share或GMV”是category+SKIN；“彩妆/Makeup市场的CPD份额、gain share或GMV”是category+MAKEUP。这里CPD虽为四品牌组合，仍是品类内CPD，不能归入01整体market。
request字段：action(new|modify|unsupported|clarify),category(SKIN|FEMALE_SKIN|HAIR|MEX|MAKEUP|null),brand(L'OREAL PARIS|3CE|MAYBELLINE|null),start(YYYY-MM-DD|null),end(YYYY-MM-DD|null),reason(中文)。
SKIN护肤包含男士；FEMALE_SKIN是Female Skin/女士护肤/非男士护肤/剔除男士；MEX才是单独男士；HAIR洗护；MAKEUP彩妆不含香水。
“去掉男士呢”=modify+FEMALE_SKIN，“单看男士呢”=modify+MEX，“男士加回去”=modify+SKIN。历史男士日期保持用户原窗口，最新数据不足由工具判断，模型不能缩成7天。
SKIN和FEMALE_SKIN展示巴黎欧莱雅、美宝莲；MAKEUP展示巴黎欧莱雅、3CE、美宝莲；HAIR和MEX仅展示巴黎欧莱雅。品牌未提填null显示全部；品牌品类不匹配clarify。普通品牌经营分析仍走普通路由。
明确询问美宝莲market share、MS%、gain/lose share时选category原子入口；若未指明品类可用MAKEUP作为入口，工具会同时提供护肤、彩妆及（卸妆＋彩妆）÷彩妆Pure Mass三种份额。不要把普通美宝莲经营分析误归为02查数。
02单个平台/两个平台/分平台列表暂未接入，仅明确品类市场请求时unsupported；“抖音彩妆大盘多少”unsupported，但“IORE抖音彩妆生意如何”是普通品牌路由。
"""
    rules += """
最终优先级（高于任何入口限制）：榜单/Top5、品牌报告、商品渠道、原因建议、联合复杂诉求必须普通路由JSON，不输出atomic_request；尤其不能因原子工具不支持就拒绝已有榜单能力。纯查数才输出atomic_request。
每个原子request的日期来自当前句或对应活动上下文；modify未改字段填null，只给单日start=end；缺日期保留null，不编造。用户只改日期时保留原子对象。取消由控制层处理。
标准“品牌+期间+平台+生意如何/分析生意/BET如何”应为question_mode=report,response_strategy=FULL_REPORT,execution_profile=standard；开放建议、联合问题和自定义比较则dynamic。有不确定性选dynamic并保留全部目标。
"""
    if state is not None:
        import json
        rules += "\n当前原子入口的活动范围（这些是已经确认的槽位，即使task为空也有效）：\n" + json.dumps({
            'market': state.market_lookup_context,
            'category': state.category_market_lookup_context,
        }, ensure_ascii=False, default=str)
    rules += """
01/02取数范围不可互换：出现明确品类范围时，Total Beauty也指该品类的Total Beauty，不是整体美妆市场。护肤不是整体Total Beauty，洗护、男士、彩妆同理。
示例：“8月3日至8月20日护肤大盘如何”必须category+SKIN，不能market；“8月护肤Total Beauty GMV”仍是category+SKIN；“8月三平台Total Beauty market GMV”没有品类限定才是market。
只有整体美妆/整体CPD才可market；带品类的CPD请求不能偷偷变成整体CPD，支持的02展示品牌查数走category，不支持的组合需明确说明。
示例：“2026年9月6日至12日Skin市场的CPD有没有gain share?”应输出{"atomic_request":{"kind":"category","request":{"action":"new","category":"SKIN","brand":null,"start":"2026-09-06","end":"2026-09-12"}}}，绝不能输出kind=market。示例日期不得复制到其他请求。
连续追问必守：已有上述对应活动范围时，不得因为本句没重说日期而clarify。将未修改字段填null，让工具继承。
示例：category活动范围={category:SKIN,start:2026-08-03,end:2026-08-20}，用户“那单看男士呢”应输出{"atomic_request":{"kind":"category","request":{"action":"modify","category":"MEX"}}}。男士是品类，不能当品牌，也不能追问品牌。
示例：market活动范围={platform:TTL,segment:PURE MASS,start:2026-09-01,end:2026-09-06}，用户“evol%是多少？CPD gain share了吗”应输出{"atomic_request":{"kind":"market","request":{"action":"modify","metric":"all","include_cpd":true}}}，不需要用户重复日期。
以上示例仅说明继承方式，实际日期必须从当前用户或活动上下文读取，不得复制示例日期。
"""
    return rules


def publish_available_data(outputs, on_progress):
    """Publish a bounded extract of checked reports, without another model call."""
    if not enabled() or not on_progress:
        return
    from bot.inline_answer import _fallback_answer
    parts = []
    for report in outputs.values():
        if report.get('ok', True) is False or (report.get('meta') or {}).get('failure_kind'):
            continue
        markdown = report.get('markdown')
        if not markdown:
            continue
        meta = report.get('meta') or {}
        scope = '｜'.join(str(meta[k]) for k in ('brand', 'period', 'platform') if meta.get(k))
        excerpt = _fallback_answer('', markdown, 500)
        if excerpt:
            parts.append((scope + '\n' if scope else '') + excerpt)
    if parts:
        on_progress('已完成的数据（部分结果）：\n' + '\n\n'.join(parts)[:1800]
                    + '\n\n正在继续回答你的补充分析问题，以下最终回复会更新完整结果。')

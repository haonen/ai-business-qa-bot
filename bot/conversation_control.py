"""Atomic handoff of scoped reports and explicit conversation controls."""
from copy import deepcopy
import os
from bot import session as sessions

CANCEL_TEXTS = {'取消', '取消分析', '取消当前分析', '取消当前任务', '停止分析', '不用分析了'}
VIEWS = {'渠道呢':'channel', '渠道':'channel', '看渠道':'channel', '看看渠道':'channel',
         '商品呢':'product', '看商品':'product', '产品呢':'product',
         '品类呢':'category', '看品类':'category'}


def publish_scoped_result(open_id, result, context):
    """Replace old report identity and evidence together; never mix data sources."""
    def mutate(state):
        state.brand_lookup_context = deepcopy(context)
        if result.get('meta', {}).get('awaiting') or not context.get('context'):
            return
        brand, period, platform = (context.get(k) for k in ('input','period','platform'))
        category = context['context']['category']
        evidence = {'schema_version':'scoped-product-report.v1', 'brand':brand,
            'period':str(period), 'platform':platform, 'category':category,
            'source_kind':'product_sample', 'rows':deepcopy(result.get('evidence') or [])}
        state.ec_context = sessions.DomainContext(brand=brand, period=period, platform=platform,
            filters={'category':category}, recent_evidence=[evidence] if result.get('ok') else [])
        if hasattr(state.ec_context, 'latest_report_evidence'):
            state.ec_context.latest_report_evidence = evidence if result.get('ok') else None
        state.drilldown_ctx = sessions.DrilldownContext(brand=brand, period=period, category=category)
        state.last_result_cache = {}
        state.pending_request = None
        state.active_plan = sessions.ActivePlanState()
        task = state.task_context
        task.brand, task.period, task.platform, task.category = brand, period, platform, category
        task.awaiting_slot = None
        task.status = 'active' if result.get('ok') else 'failed'
    sessions._mutate_session(open_id, mutate)


def handle_control(open_id, text, session):
    value = text.strip().rstrip('。！？!?')
    if value in CANCEL_TEXTS:
        def cancel(state):
            fresh = sessions.SessionState()
            for name in ('pending_request','brand_lookup_context','task_context','ec_context',
                         'bet_context','drilldown_ctx','active_plan','last_result_cache','market_context',
                         'market_lookup_context','category_market_lookup_context'):
                setattr(state, name, deepcopy(getattr(fresh, name)))
        sessions._mutate_session(open_id, cancel)
        return {'route_type':'meta','markdown':'已取消当前分析。你可以直接提出新的分析问题。',
                'meta':{'document_ready':False,'cancelled':True}}
    return None

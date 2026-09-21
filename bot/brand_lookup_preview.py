"""Opt-in Bot route acceptance for product-source reports, not full business reports."""
import os
from dataclasses import asdict
from bot.brand_mapping import BrandContext, BrandMapping
from bot.brand_lookup_runtime import get_brand_lookup

CATEGORIES={'SKIN':'SKIN','护肤':'SKIN','女士护肤':'SKIN','MEX':'MEX','男士':'MEX','男士护肤':'MEX',
            'HAIR':'HAIR','洗护':'HAIR','MAKEUP':'MAKEUP','彩妆':'MAKEUP','TTL':'TTL','全部':'TTL','全部生意':'TTL'}

def pending_category_route(session, text):
    """Only exact slot answers may consume a saved clarification."""
    if os.environ.get('BRAND_LOOKUP_BOT_PREVIEW', '0') != '1':
        return None
    saved = dict(session.brand_lookup_context or {})
    category = CATEGORIES.get(text.strip())
    if not saved.get('awaiting_category') or not category:
        return None
    from bot.router import RouteResult
    return RouteResult(
        type='default_chain' if saved.get('platform') == 'TM' else 'douyin_business_analysis',
        brand=saved.get('input'), period=saved.get('period'),
        platform=saved.get('platform'), category=category,
    )


def preview_route(route,session,text):
    if os.environ.get('BRAND_LOOKUP_BOT_PREVIEW','0')!='1':return None
    saved=dict(session.brand_lookup_context or {})
    reply_category=CATEGORIES.get(text.strip())
    pending=saved.get('awaiting_category') and reply_category
    if route.type not in {'default_chain','douyin_business_analysis'} and not pending:return None
    lookup=get_brand_lookup()
    if lookup is None:
        return {'ok':False,'markdown':'统一品牌字典尚未启用，验收入口未执行。','meta':{'document_ready':False}},saved
    brand=saved.get('input') if pending else route.brand
    period=saved.get('period') if pending else route.period
    platform=saved.get('platform') if pending else ('TM' if route.type=='default_chain' else 'DY')
    category=reply_category if pending else CATEGORIES.get(route.category or '')
    context=BrandContext(**saved['context']) if pending and saved.get('context') else None
    resolved=BrandMapping(lookup.repository.mapping_rows()).resolve(brand,category,context)
    next_state={'input':brand,'period':period,'platform':platform}
    if resolved['status']=='clarify_category':
        next_state['awaiting_category']=True
        return {'ok':False,'markdown':f'你想看{brand}的哪个品类？护肤、男士、洗护、彩妆，还是全部生意？',
                'meta':{'document_ready':False,'awaiting':'business_category','brand_id':resolved['brand_id']}},next_state
    if resolved['status']!='resolved':
        return {'ok':False,'markdown':'品牌或品类映射需要核验，请明确品牌和品类。','meta':{'document_ready':False,'brand_mapping':resolved}},next_state
    context=resolved['context'];next_state['context']=asdict(context)
    if not period:
        return {'ok':False,'markdown':'请提供需要分析的时间段。','meta':{'document_ready':False}},next_state
    from bot.newsletter.scoped_evidence import run_scoped_report
    result=run_scoped_report(brand,period,platform,context.category,brand_lookup=lookup,brand_context=context)
    result['markdown']='【新字典链路验收：商品源报告】\n\n'+result.get('markdown','')
    result.setdefault('meta',{}).update(document_ready=False,brand_id=context.brand_id,
        business_category=context.category,brand_context=asdict(context),preview_only=True,
        brand=brand,period=period,platform=platform)
    if not result.get("ok", False):
        result["meta"].setdefault("failure_kind", "SCOPED_REPORT_UNAVAILABLE")
    return result,next_state

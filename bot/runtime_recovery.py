"""Readable coverage failures and scope retention, compatible with deployed sessions."""
from bot.coverage_options import safe_discover, describe
from bot.session import CLEAR, SET, SlotUpdate, TaskContextPatch, apply_task_context_patch, set_pending_request


def coverage_result(brand, period_meta, missing, error):
    start, end = period_meta['current_start'], period_meta['current_end']
    requested = start if start == end else f'{start}至{end}'
    labels = {'TM':'天猫','DY':'抖音','JD':'京东'}
    details = []
    for key, title in [('current','本期'),('prior','同比同期')]:
        platforms = [labels[p] for p in labels if any(row.get('period') == key and row.get('platform') == p for row in missing)]
        if platforms:
            details.append(title+'的'+'、'.join(platforms)+'数据不齐全')
    reason = '；'.join(details) or '所需的品牌数据不齐全'
    if error == 'incomplete_market': reason = '用于比较的大盘'+reason
    availability = safe_discover(brand)
    return {
        'coverage_options': availability,
        'error': error, 'failure_kind':'NO_DATA_IN_RANGE', 'retry_slot':'period',
        'message':f'暂时无法完成{brand}{requested}的三平台生意分析：{reason}。这不代表没有销售。' + "\n\n" + describe(availability),
        'coverage': {'requested_start': start, 'requested_end': end, 'missing': missing},
    }


def preserve_three_platform_scope(open_id, brand, period, meta, original_question):
    retry = meta.get('retry_slot') == 'period'
    apply_task_context_patch(open_id, TaskContextPatch(
        relation='RECOVERABLE_DATA_FAILURE' if retry else 'CONTINUE_TASK',
        slots={
            'brand':SlotUpdate(SET,brand), 'platform':SlotUpdate(SET,'TTL'),
            'period':SlotUpdate(CLEAR) if retry else SlotUpdate(SET,period),
            'original_question':SlotUpdate(SET,original_question),
            'awaiting_slot':SlotUpdate(SET,'period') if retry else SlotUpdate(CLEAR),
            'status':SlotUpdate(SET,'awaiting' if retry else 'active'),
        },
        set_intents=['EC_BUSINESS'], set_goals=['EC_BUSINESS'],
    ))
    if retry:
        set_pending_request(open_id, {'intent':'three_platform_competitor_analysis','brand':brand,'platform':'TTL','period':None,'original_text':original_question,'coverage_options':meta.get('coverage_options') or {}})

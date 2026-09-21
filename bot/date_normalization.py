"""Semantic date normalization at the route boundary; never generates SQL.

Only source spans may be replaced. Entity/task merging stays in task_context.
Campaign windows stay with the existing campaign confirmation flow.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo
import json
import logging
import os
import re
from bot.utils import extract_json_object, llm_client

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo('Asia/Shanghai')

@dataclass(frozen=True)
class NormalizedDates:
    text: str
    status: str = 'none'
    spans: tuple = ()
    message: str | None = None

def reference_time(value=None):
    if isinstance(value, datetime):
        return value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value.astimezone(SHANGHAI)
    if value:
        try:
            # Feishu create_time is epoch milliseconds; old jobs omit it.
            stamp=float(value)
            if stamp > 1e11: stamp/=1000
            if stamp > 0:return datetime.fromtimestamp(stamp,SHANGHAI)
        except (ValueError,TypeError,OverflowError,OSError):pass
    return datetime.now(SHANGHAI)

def _ask(text, frame, now):
    prompt='''你是时间标准化器，只输出JSON，不生成SQL、不改变品牌、平台或分析目的。
识别当前用户句子中明确表达的所有时间片段，将每段转换为起止日期（含首尾）。
相对日期以提供的消息时间、Asia/Shanghai计算；上周指上一个完整周一到周日。
未写年份时：延续任务的日期修改可使用任务年份；独立新问题使用消息年份。
保留比较、同比和多个分析窗口的不同片段，不把比较期当成本期，不为“同比”擅自添加片段。
大促名（如618、双11）不在此解释，保留原文，交给既有确认流程。
单纯平台/品牌回复或“确认”、询问最新数据日期，不生成日期。月份也给出实际起止日，granularity=month。
每个source必须是原句中连续、完整、仅时间表达的原文，不包含品牌/平台/业务要求。
有歧义（例如无法分辨月/日顺序）、无效日期、无法确定范围时status=ambiguous，给简短中文追问，禁止猜测。
输出格式：{"status":"none|resolved|ambiguous","spans":[{"source":"原文","start":"YYYY-MM-DD","end":"YYYY-MM-DD","granularity":"day|month"}],"question":null}
不要把没有时间表达的句子标为resolved。'''
    response=llm_client(max_retries=0).chat.completions.create(
        model=os.environ.get('DASHSCOPE_ROUTER_MODEL') or os.environ.get('DASHSCOPE_MODEL','qwen3.7-plus'),
        messages=[{'role':'system','content':prompt},{'role':'user','content':json.dumps({
            'message_time':now.isoformat(), 'active_task':frame, 'text':text,
        },ensure_ascii=False,default=str)}],temperature=0,max_tokens=900,
        timeout=float(os.environ.get('DATE_NORMALIZATION_TIMEOUT','12')),
        response_format={'type':'json_object'},extra_body={'enable_thinking':False})
    return extract_json_object(response.choices[0].message.content or '')

def validate_result(text, payload):
    if not isinstance(payload,dict):raise ValueError('date output is not an object')
    status=payload.get('status')
    if status=='none':
        if payload.get('spans'):raise ValueError('unexpected date spans')
        return NormalizedDates(text)
    if status=='ambiguous':
        question=payload.get('question')
        question=question.strip() if isinstance(question,str) else ''
        return NormalizedDates(text,'ambiguous',message=(question[:240] or '这次的日期范围还不能确定，请补充明确的起止日期。')+' 已确认的品牌和平台会保留。')
    spans=payload.get('spans')
    if status!='resolved' or not isinstance(spans,list) or not 1<=len(spans)<=8:raise ValueError('invalid date status/spans')
    edits=[]; used=set()
    for item in spans:
        source=item.get('source')
        if not isinstance(source,str) or not source.strip() or source in used or text.count(source)!=1:raise ValueError('date span is not unique source text')
        used.add(source)
        start,end=item.get('start'),item.get('end')
        if not all(isinstance(v,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',v) for v in (start,end)):raise ValueError('non ISO date')
        lo,hi=date.fromisoformat(start),date.fromisoformat(end)
        if hi<lo:raise ValueError('reversed dates')
        # Exact ISO input is authoritative, regardless of a model mistake.
        literals=re.findall(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)',source)
        if literals and (literals[0]!=start or literals[-1]!=end):raise ValueError('explicit ISO dates changed')
        granularity=item.get('granularity','day')
        if granularity not in {'day','month'}:raise ValueError('unknown granularity')
        replacement=f'{start}~{end}'
        if granularity=='month':
            import calendar
            if lo.day!=1 or hi.day!=calendar.monthrange(hi.year,hi.month)[1]:raise ValueError('partial month mislabeled')
            replacement=f'{start[:7]}~{end[:7]}' if start[:7]!=end[:7] else start[:7]
        at=text.index(source)
        edits.append((at,at+len(source),replacement,{'source':source,'start':start,'end':end,'granularity':granularity}))
    edits.sort()
    if any(a[1]>b[0] for a,b in zip(edits,edits[1:])):raise ValueError('overlapping spans')
    result=text
    for a,b,replacement,_ in reversed(edits):result=result[:a]+replacement+result[b:]
    return NormalizedDates(result,'resolved',tuple(e[3] for e in edits))

def normalize_dates(text, state, *, received_at=None):
    if os.environ.get('DATE_NORMALIZATION_ENABLED','1')!='1' or not os.environ.get('DASHSCOPE_API_KEY'):
        return NormalizedDates(text,'disabled')
    frame=state.task_context
    context={k:getattr(frame,k,None) for k in ('brand','platform','period','awaiting_slot','original_question')}
    try:
        result=validate_result(text,_ask(text,context,reference_time(received_at)))
    except Exception as exc:
        # Do not silently reinterpret a failed normalization through legacy
        # partial matches (8/31 could otherwise be treated as a month).
        log.warning('[date_normalization] failed type=%s',type(exc).__name__)
        return NormalizedDates(text,'unavailable',message='这次时间理解服务暂时没有完成响应，请重发刚才那句话。已确认的品牌、平台和日期均已保留。')
    log.info('[date_normalization] status=%s spans=%s',result.status,json.dumps(result.spans,ensure_ascii=False))
    return result

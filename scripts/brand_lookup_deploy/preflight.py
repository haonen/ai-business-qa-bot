import json,os,sys
from pathlib import Path
root=Path(sys.argv[1]);import bot
bot.__path__.insert(0,str(root/'bot'))
os.environ['BRAND_LOOKUP_ENABLED']='1';os.environ['BRAND_LOOKUP_BOT_PREVIEW']='1'
from bot.brand_lookup_runtime import get_brand_lookup
from bot.brand_lookup_preview import preview_route
from bot.session import SessionState,_state_payload,_state_from_payload
from bot.router import RouteResult
from bot.newsletter.mapping_coverage import prepare_pools
import bot.app
lookup=get_brand_lookup()
for brand,category in [('谷雨','SKIN'),('韩束','MEX'),('OIU','MAKEUP'),('MISTINE','SKIN')]:
 for table,field in [('ai_bot_tmall_product_link','brand_name'),('ai_bot_dy_product_link','商品品牌')]:
  plan=lookup.plan(brand,category,table,field)
  assert plan['status']=='ready',(brand,table,plan['status'])
  assert lookup.plan(brand,category,table,field)['cache_state']=='hit',(brand,table,'cache not working')
session=SessionState();route=RouteResult(type='default_chain',brand='谷雨',period='2026-07-26~2026-08-08')
result,ctx=preview_route(route,session,'分析谷雨生意')
assert result['meta']['awaiting']=='business_category'
session.brand_lookup_context=ctx
assert _state_from_payload(_state_payload(session)).brand_lookup_context==ctx
pools,audit=prepare_pools({'HAIR':[{'brand':'P&G'}]})
assert pools['HAIR'][0]['available_evidence_platforms']==['TM']
print(json.dumps({'imports':'passed','dictionary_and_cache':'passed','category_clarification':'passed','session_roundtrip':'passed','selection_platform_guard':'passed','messages_sent':0}))

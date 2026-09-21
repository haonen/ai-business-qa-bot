"""Preflight exact product-source mappings before model selection; keep metrics intact."""
from bot.brand_mapping import BrandMapping
from bot.brand_lookup_runtime import get_brand_lookup

def prepare_pools(pools):
    lookup=get_brand_lookup()
    if lookup is None:return pools,[]
    mapping=BrandMapping(lookup.repository.mapping_rows())
    result={};audit=[]
    for category,items in pools.items():
        result[category]=[]
        for item in items:
            identity=mapping.resolve(item['brand'],category);platforms=[];statuses={}
            for pl,table,field in [('TM','ai_bot_tmall_product_link','brand_name'),('DY','ai_bot_dy_product_link','商品品牌')]:
                plan=mapping.source_plan(identity['context'],table,field) if identity['status']=='resolved' else identity
                statuses[pl]=plan['status']
                if plan['status']=='ready':platforms.append(pl)
            audit.append({'brand':item['brand'],'category':category,'platforms':statuses})
            if platforms:result[category].append(dict(item,available_evidence_platforms=platforms))
        if not result[category]:raise ValueError('No verified product-source mappings for '+category)
    return result,audit

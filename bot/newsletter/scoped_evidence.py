"""Category-filtered product/channel evidence for the isolated newsletter trial."""
from collections import defaultdict
from pathlib import Path
import json
from bot.db.connection import fetch_df
from bot.media_brand import resolve_source_brand
from bot.brand_source_binding import binding
from bot.utils import parse_ec_period
from datetime import date

def category_case(platform):
    if platform=='TM':
        return """CASE WHEN category_level_1='美容护肤/美体/精油' AND category_level_2='男士面部护理' THEN 'MEX'
        WHEN category_level_1='美容护肤/美体/精油' THEN 'SKIN'
        WHEN category_level_1='美发护发/假发' THEN 'HAIR'
        WHEN category_level_1='彩妆/香水/美妆工具' THEN 'MAKEUP' ELSE 'UNKNOWN' END"""
    if platform=='DY':
        return """CASE WHEN `商品二级分类`='美容护肤' AND `商品三级分类`='男士护肤' THEN 'MEX'
        WHEN `商品二级分类`='美容护肤' THEN 'SKIN'
        WHEN `商品三级分类` IN ('洗发护发','染发烫发/头发造型','男士美发护发/假发') THEN 'HAIR'
        WHEN `商品二级分类`='彩妆/香水/美妆工具' THEN 'MAKEUP'
        WHEN `商品一级分类`='个护家清' AND `商品三级分类` IN ('手部护理','身体护理','奢品护肤') THEN 'SKIN'
        ELSE 'UNKNOWN' END"""
    raise ValueError('Product/channel source not supported for platform')

from bot.brand_request_cache import request_cached

@request_cached(fresh=False)
def run_scoped_report(brand,period,platform,business_category,*,brand_lookup=None,brand_context=None):
    if brand_lookup is None:
        from bot.brand_lookup_runtime import get_brand_lookup
        brand_lookup=get_brand_lookup()
    if business_category not in ('SKIN','MAKEUP','MEX','HAIR','TTL'):raise ValueError('Explicit business_category required')
    if platform not in ('TM','DY'):
        return {'ok':False,'markdown':'京东暂无已验证的商品/渠道品类接口。','meta':{'business_category':business_category}}
    if brand_lookup is not None:
        table='ai_bot_tmall_product_link' if platform=='TM' else 'ai_bot_dy_product_link'
        field='brand_name' if platform=='TM' else '商品品牌'
        mapped=brand_lookup.plan(brand,business_category,table,field,brand_context)
        if mapped['status']!='ready':
            return {'ok':False,'markdown':'统一品牌映射需要处理','meta':mapped}
        resolved={'brand':mapped['values'][0],**mapped}
        aliases={}
    else:
        aliases={'CHANDO':['自然堂'],'KANS':['韩束'],'PROYA':['珀莱雅'],'FLOWER KNOWS':['花知晓']}
        reference=json.loads(Path(__file__).with_name('brand_names.json').read_text())
        aliases[brand]=list(dict.fromkeys(aliases.get(brand,[])+reference.get(brand.upper(),[])))
        mapped=binding(brand,'ai_bot_tmall_product_link' if platform=='TM' else 'ai_bot_dy_product_link')
        if mapped is not None:
            if mapped.get('error') or len(mapped.get('values',[]))!=1:
                return {'ok':False,'markdown':'源表品牌映射尚未可用','meta':mapped}
            resolved={'brand':mapped['values'][0],**mapped}
        else:
            resolved=resolve_source_brand(brand,'tmall' if platform=='TM' else 'dy',brand_aliases=aliases.get(brand,[]))
    if resolved.get('error'):return {'ok':False,'markdown':'品牌映射失败','meta':resolved}
    source_brand=resolved['brand'];pm=parse_ec_period(period,date.today().year)
    if platform=='TM':
        table='ai_bot_tmall_product_link';dt='bus_date';bn='brand_name';item='item_id';title='product_title';leaf='category_CN';amount='gmv'
    else:
        table='ai_bot_dy_product_link';dt='业务日期';bn='商品品牌';item='商品ID';title='商品名称';leaf='商品四级分类';amount='销售额'
    case=category_case(platform)
    numeric=f"REPLACE(TRIM(CAST(`{amount}` AS CHAR)), ',', '')"
    valid=f"{numeric} REGEXP '^[+-]?[0-9]+([.][0-9]+)?$'"
    source_names=mapped['values'] if brand_lookup is not None else [source_brand]
    brand_filter='`'+bn+'` IN ('+','.join(':source_brand_'+str(i) for i in range(len(source_names)))+')' if brand_lookup is not None else '`'+bn+'`=:brand'
    leaf_filter=" AND NULLIF(TRIM(`商品四级分类`), '') IS NOT NULL" if platform=='DY' else ''
    sql=f"""SELECT {case} AS business_category,
      CASE WHEN `{dt}` BETWEEN :current_start AND :current_end THEN 'current' ELSE 'prior' END period_key,
      CAST(`{item}` AS CHAR) item_id, `{title}` product_title, `{leaf}` source_category, key_driver,
      SUM(CASE WHEN {valid} THEN CAST({numeric} AS DECIMAL(28,4)) ELSE 0 END) gmv,
      SUM(CASE WHEN {valid} THEN 0 ELSE 1 END) invalid_rows
      FROM `{table}` WHERE {brand_filter}{leaf_filter} AND
      ((`{dt}` BETWEEN :current_start AND :current_end) OR (`{dt}` BETWEEN :prior_start AND :prior_end))
      GROUP BY business_category,period_key,item_id,product_title,source_category,key_driver"""
    params={k:pm[k] for k in ('current_start','current_end','prior_start','prior_end')};params['brand']=source_brand
    if brand_lookup is not None:
        params.pop('brand')
        params.update({'source_brand_'+str(i):value for i,value in enumerate(source_names)})
    frame=fetch_df(sql,params)
    audit={'sql':sql,'params':params}
    if brand_lookup is not None:
        audit['brand_mapping']={k:v for k,v in mapped.items() if k!='context'}
    if frame.empty and mapped is not None:
        return {'ok':False,'markdown':'所选期间已核验品牌值无商品记录','meta':{'business_category':business_category,'brand_id':mapped['brand_id']},'query_audit':audit}
    if frame.empty:
        # A cached platform name may belong to a different table. Validate trusted
        # aliases against this exact product source and period before retrying.
        candidates=list(dict.fromkeys([brand]+aliases.get(brand,[])))
        probe_params=dict(params)
        for i,value in enumerate(candidates):probe_params[f'alias_{i}']=value
        slots=','.join(f':alias_{i}' for i in range(len(candidates)))
        probe=f"SELECT DISTINCT `{bn}` AS source_brand FROM `{table}` WHERE `{bn}` IN ({slots}) AND ((`{dt}` BETWEEN :current_start AND :current_end) OR (`{dt}` BETWEEN :prior_start AND :prior_end))"
        matches=fetch_df(probe,probe_params)
        names=matches.source_brand.tolist() if not matches.empty else []
        audit['brand_resolution']={'initial_brand':source_brand,'product_source_candidates':names,'sql':probe,'params':probe_params}
        if len(names)==1:
            source_brand=names[0]
            params=dict(params,brand=source_brand)
            audit['params']=params
            frame=fetch_df(sql,params)
        if frame.empty:return {'ok':False,'markdown':'商品源品牌匹配不唯一' if len(names)>1 else '商品源无记录','meta':{'business_category':business_category,'source_brand':source_brand,'error':'ambiguous_product_brand' if len(names)>1 else 'product_source_empty'},'query_audit':audit}
    available=sorted(set(frame.loc[(frame.gmv>0)&(frame.business_category!='UNKNOWN'),'business_category']))
    unknown=float(frame.loc[frame.business_category=='UNKNOWN','gmv'].sum())
    selected=frame[frame.business_category!='UNKNOWN'] if business_category=='TTL' else frame[frame.business_category==business_category]
    level_audit={}
    if platform=='DY':
        leaf_mask=selected['source_category'].fillna('').astype(str).str.strip().ne('')
        for period_key in ('current','prior'):
            period_mask=selected.period_key==period_key
            level_audit[period_key]={
                'leaf_gmv':float(selected.loc[period_mask & leaf_mask,'gmv'].sum()),
                'blank_leaf_gmv':float(selected.loc[period_mask & ~leaf_mask,'gmv'].sum())}
        # Confirmed for KANS/MEX by store reconciliation (208/55 yuan residual).
        # Other DY scopes are diagnostic leaf samples, not a certified brand total.
        selected=selected.loc[leaf_mask].copy()
    if selected.empty or int(selected.invalid_rows.sum()):return {'ok':False,'markdown':'所选品类无记录或金额格式异常','meta':{'business_category':business_category,'available_categories':available},'query_audit':audit}
    totals={p:float(selected.loc[selected.period_key==p,'gmv'].sum()) for p in ['current','prior']}
    evidence=[]
    for kind,cols in [('channel',['key_driver']),('category',['source_category']),('product',['item_id'])]:
        buckets=defaultdict(lambda:defaultdict(float))
        titles={}
        if kind=='product':
            # Same item ID may change title between periods and within a week.
            for row in selected.sort_values(['period_key','gmv']).to_dict('records'):
                item=str(row['item_id'])
                if row['period_key']=='current' or item not in titles:
                    titles[item]=str(row['product_title'])
        for row in selected.to_dict('records'):
            key=tuple('未分类' if row[c] is None else str(row[c]) for c in cols);buckets[key][row['period_key']]+=float(row['gmv'])
        ordered=sorted(buckets.items(),key=lambda x:x[1].get('current',0),reverse=True)
        for key,values in ordered[:(20 if kind=='product' else len(ordered))]:
            now=values.get('current');old=values.get('prior')
            evidence.append({'evidence_id':f'E{len(evidence)+1:03d}','kind':kind,'name':(' / '.join(key)+' / '+titles.get(key[0],'')) if kind=='product' else ' / '.join(key),'business_category':business_category,'platform':platform,'source':table,'period':period,'gmv_m':now/1e6 if now is not None else None,'prior_gmv_m':old/1e6 if old is not None else None,'wgt_pct':100*now/totals['current'] if now is not None and totals['current'] else None,'evol_pct':100*(now/old-1) if now is not None and old and old>0 else None,'denominator':'同平台同品牌所选品类商品源GMV（全部渠道）'})
    lines=[f'# {brand} / {platform} / {business_category} / {period}', '以下全部为筛选品类后的商品源样本，不能与店铺榜单金额跨源计算贡献。', '|ID|类型|名称|GMV(M)|Wgt%|Evol%|','|---|---|---|---:|---:|---:|']
    for r in evidence:
        fmt=lambda v:'—' if v is None else f'{v:.2f}'
        lines.append('|'+ '|'.join([r['evidence_id'],r['kind'],r['name'].replace('|','/'),fmt(r['gmv_m']),fmt(r['wgt_pct']),fmt(r['evol_pct'])])+'|')
    return {'ok':True,'markdown':'\n'.join(lines),'meta':{'brand':brand,'source_brand':source_brand,'business_category':business_category,'period':period,'available_categories':available,'unknown_gmv_both_periods':unknown,'level_audit':level_audit,'level_policy':'fourth_level_nonempty_sample' if platform=='DY' else 'source_detail','level_reconciliation':'confirmed_kans_mex' if platform=='DY' and brand=='KANS' and business_category=='MEX' else 'pending' if platform=='DY' else 'not_applicable'},'evidence':evidence,'query_audit':audit}

"""Fresh source queries and deterministic newsletter metrics."""
from collections import defaultdict
from datetime import date,timedelta
TABLES={'TM':'tmall_store_ranking_day_jiashicang','JD':'jd_store_ranking_selfrun_day_jiashicang','DY':'dy_store_ranking_BFSS_day_jiashicang'}
CATS=('SKIN','HAIR','MEX','MAKEUP')
OWNED={"L'OREAL PARIS",'3CE','MAYBELLINE','DR.G'}
def periods(start,end):
 a,b=date.fromisoformat(start),date.fromisoformat(end)
 if b<a:raise ValueError('End date must not precede start date')
 def prior(d):
  try:return d.replace(year=d.year-1)
  except ValueError:return d.replace(year=d.year-1,day=28)
 return dict(cs=str(a),ce=str(b),ps=str(prior(a)),pe=str(prior(b)))
class CoverageError(ValueError):
 def __init__(self,missing):
  self.missing=missing
  super().__init__('Incomplete source coverage; see coverage.json')
def date_set(start,end):
 a,b=date.fromisoformat(start),date.fromisoformat(end)
 return {str(a+timedelta(days=i)) for i in range((b-a).days+1)}
from bot.store_metric_scope import category_weights

def query_sources(params,run,include_mex=True,independent_mex=False,skin_mode="inclusive"):
 # run persists exact SQL, parameters and returned frame before computation.
 market=run('market',"""SELECT CAST(bus_date AS DATE) day,platform,global_segment,SUM(gmv) gmv FROM three_platforms_segmented_markets_daily
 WHERE ((bus_date BETWEEN :cs AND :ce) OR (bus_date BETWEEN :ps AND :pe))
 AND UPPER(TRIM(global_segment)) IN ('BEAUTY MARKET','PURE MASS')
 GROUP BY day,platform,global_segment""",params)
 stores={}
 for pl,table in TABLES.items():
  amount="REPLACE(TRIM(CAST(gmv AS CHAR)),',','')";valid=f"{amount} REGEXP '^[+-]?[0-9]+([.][0-9]+)?$'"
  stores[pl]=run('stores_'+pl,f"""SELECT CAST(bus_date AS DATE) day,brand_name,SELECTIVITY,category_EN_level_1,category_EN_level_2,
 SUM(CASE WHEN {valid} THEN CAST({amount} AS DECIMAL(28,4)) ELSE 0 END) gmv,
 SUM(CASE WHEN {valid} THEN 0 ELSE 1 END) invalid_rows
 FROM `{table}` WHERE ((bus_date BETWEEN :cs AND :ce) OR (bus_date BETWEEN :ps AND :pe))
 AND (LOWER(TRIM(category_EN_level_1)) IN ('skincare','hair','makeup','makeup + fragrance','makeup+fragrance','makeup (exclude fragrance)','fragrance') OR LOWER(TRIM(category_EN_level_2))='male skincare')
 GROUP BY day,brand_name,SELECTIVITY,category_EN_level_1,category_EN_level_2""",params)
 from .category_market import query
 stores["_section02"]=query(params,run,TABLES,include_mex=include_mex)
 if include_mex and independent_mex:
  from .mex_window import discover,query as query_mex
  stores['_mex']=query_mex(discover(params,run),run)
  stores['_skin_mode']=skin_mode
 return market,stores

def calculate(market,stores,params,include_mex=True):
 v=defaultdict(float);days=defaultdict(set);mv=defaultdict(float);md=defaultdict(set);cpd=defaultdict(float)
 def period(day):return 'current' if params['cs']<=str(day)[:10]<=params['ce'] else 'prior'
 for r in market:
  pl=str(r['platform']).upper();seg=str(r['global_segment']).upper();per=period(r['day'])
  if pl not in TABLES:raise ValueError('Unrecognized market platform')
  mv[pl,seg,per]+=float(r['gmv']);md[pl,seg,per].add(str(r['day'])[:10])
 for pl,rows in stores.items():
  if pl.startswith("_"):continue
  for r in rows:
   if int(r.get('invalid_rows') or 0):raise ValueError('Invalid source amount')
   b=str(r.get('brand_name') or '').strip().upper();per=period(r['day']);g=float(r['gmv']);weights=category_weights(r,pl)
   # CPD TTL uses level-one business rows, not extra MEX summary rows.
   if b in OWNED and any(c in ('SKINCARE_ALL','HAIR','MAKEUP') for c,_ in weights):cpd[pl,per]+=g
   scopes=['ALL']+(['PM','B:'+b] if not str(r.get('SELECTIVITY') or '').strip() else [])
   if b in OWNED:scopes+=['OWN:'+b]
   for cat,sign in weights:
    for scope in scopes:
     v[pl,per,scope,cat]+=g*sign;days[pl,per,scope,cat].add(str(r['day'])[:10])
 missing=[]
 for pl in TABLES:
  for per,start,end in [('current',params['cs'],params['ce']),('prior',params['ps'],params['pe'])]:
   expected=date_set(start,end)
   for seg in ('BEAUTY MARKET','PURE MASS'):
    absent=sorted(expected-md[pl,seg,per])
    if absent:missing.append({'source':'market','platform':pl,'segment':seg,'period':per,'missing_dates':absent,'latest_observed':max(md[pl,seg,per],default=None)})
   for cat in CATS:
    absent=sorted(expected-days[pl,per,'PM',cat])
    if absent:missing.append({'source':'store','platform':pl,'category':cat,'period':per,'missing_dates':absent,'latest_observed':max(days[pl,per,'PM',cat],default=None)})
 if missing:raise CoverageError(missing)
 for (pl,per,scope,cat),g in list(v.items()):v['TTL',per,scope,cat]+=g
 for (pl,seg,per),g in list(mv.items()):mv['TTL',seg,per]+=g
 for per in ('current','prior'):cpd['TTL',per]=sum(cpd[pl,per] for pl in TABLES)
 evol=lambda now,old:100*(now/old-1) if old>0 else None
 market_rows=[]
 for pl in ('TTL','TM','JD','DY'):
  r={'Platform':pl}
  for seg in ('BEAUTY MARKET','PURE MASS','CPD observed'):
   now,old=[cpd[pl,per] if seg=='CPD observed' else mv[pl,seg,per] for per in ('current','prior')]
   r.update({seg+' current GMV(M)':now/1e6,seg+' prior GMV(M)':old/1e6,seg+' Evol%':evol(now,old)})
  now=100*cpd[pl,'current']/mv[pl,'PURE MASS','current'];old=100*cpd[pl,'prior']/mv[pl,'PURE MASS','prior']
  r.update({'current CPD MS% (observed)':now,'prior CPD MS% (observed)':old,'MS%+/- (observed, pp)':now-old,'TTL MS change contribution (observed, pp)':100*(cpd[pl,'current']/mv['TTL','PURE MASS','current']-cpd[pl,'prior']/mv['TTL','PURE MASS','prior'])});market_rows.append(r)
 from .category_market import calculate as category_calculate
 if "_section02" not in stores:raise ValueError("Section02 market sources missing; rerun full query")
 independent=stores.get('_mex') if include_mex else None
 skin_mode=stores.get('_skin_mode','inclusive')
 main_cats=['HAIR','MAKEUP']+(['SKIN'] if skin_mode=='inclusive' else []) if independent else None
 denominators,category_missing=category_calculate(stores["_section02"],params,category_weights,date_set,include_mex=include_mex and not (independent and skin_mode=='inclusive'),categories=main_cats)
 independent_owned={}
 if independent:
  from .mex_window import metrics as mex_metrics
  extra,independent_owned=mex_metrics(independent,['MEX','SKIN'] if skin_mode=='aligned' else ['MEX'])
  denominators.update({k:val for k,val in extra.items() if k[0]=='TTL'})
 if category_missing:raise CoverageError(category_missing)
 # Override only 02 denominators. 01 and 03 retain their existing inputs.
 for key,value in denominators.items():
  if key[0]=="TTL":v[key]=value
 # Remove fragrance from 02 brand numerators, without changing pick pools or 01.
 for pl in TABLES:
  if pl=='JD':continue
  for row in stores[pl]:
   if str(row.get('category_EN_level_1') or '').strip().lower()=='fragrance':
    v['TTL',period(row['day']),'OWN:'+str(row.get('brand_name') or '').strip().upper(),'MAKEUP']-=float(row['gmv'])
 categories=[]
 for cat in (CATS if include_mex else tuple(c for c in CATS if c!="MEX")):
  rs=[];pm=v['TTL','current','PM',cat]/1e6;oldpm=v['TTL','prior','PM',cat]/1e6
  displayed = (["L'OREAL PARIS",'3CE','MAYBELLINE'] if cat=='MAKEUP'
               else ["L'OREAL PARIS",'MAYBELLINE'] if cat=='SKIN'
               else ["L'OREAL PARIS"])
  for b in displayed:
   am=[]
   for per in ('current','prior'):
    use_inclusive=cat=='SKIN' and (not include_mex or (independent and skin_mode=='inclusive'))
    g=(independent_owned[per,cat]
       if b=="L'OREAL PARIS" and independent and (cat=='MEX' or (cat=='SKIN' and skin_mode=='aligned'))
       else v['TTL',per,'OWN:'+b,'SKINCARE_ALL' if use_inclusive else cat])
    am.append(g/1e6)
   now,old=am;ms=100*now/pm;prior=100*old/oldpm
   rs.append(dict(brand='Maybelline' if b=='MAYBELLINE' else b,gmv_m=now,prior_gmv_m=old,evol_pct=evol(now,old),ms_pct=ms,prior_ms_pct=prior,ms_change_pp=ms-prior))
  categories.append(dict(category=cat,market_gmv_m=v['TTL','current','ALL',cat]/1e6,market_prior_gmv_m=v['TTL','prior','ALL',cat]/1e6,pure_mass_gmv_m=pm,pure_mass_prior_gmv_m=oldpm,brands=rs,displayed_brands_ms_change_pp=sum(r['ms_change_pp'] for r in rs)))
 for item in categories:
  independent_cat=bool(independent and (item['category']=='MEX' or (item['category']=='SKIN' and skin_mode=='aligned')))
  window=independent['window']['params'] if independent_cat else params
  item.update(period=window['cs']+'~'+window['ce'],comparison_period=window['ps']+'~'+window['pe'])
  if independent_cat:item['latest_data_date']=window['ce']
  if item['category']=='SKIN':item['includes_male']=not include_mex or bool(independent and skin_mode=='inclusive')
 pools={}
 for cat in CATS:
  rows=[]
  brands={scope[2:] for pl,per,scope,c in v if c==cat and scope.startswith('B:') and scope!='B:'}
  for b in brands:
   r={'category':cat,'brand':b}
   for pl in ('TTL','TM','JD','DY'):
    now=v[pl,'current','B:'+b,cat];old=v[pl,'prior','B:'+b,cat];total=v['TTL','current','B:'+b,cat]
    r.update({pl+'_gmv_m':now/1e6,pl+'_prior_m':old/1e6,pl+'_growth_m':(now-old)/1e6 if old>0 else None,pl+'_evol_pct':evol(now,old),pl+'_wgt_pct':100*now/total if total>0 else None,pl+'_current_days':len(days[pl,'current','B:'+b,cat]) if pl!='TTL' else len(set().union(*(days[p,'current','B:'+b,cat] for p in TABLES))),pl+'_prior_days':len(days[pl,'prior','B:'+b,cat]) if pl!='TTL' else len(set().union(*(days[p,'prior','B:'+b,cat] for p in TABLES)))})
   unavailable=[pl for pl in TABLES if r[pl+'_gmv_m']>0 and r[pl+'_prior_days']==0]
   if unavailable:
    r['TTL_evol_pct']=None;r['TTL_growth_m']=None
    r['comparison_warning']='上期缺少'+ '/'.join(unavailable)+'平台品牌记录，不能将缺失当零；不参与Top Rising排名。'
   elif r['TTL_prior_m']>0 and r['TTL_prior_m']<r['TTL_gmv_m']*0.1:
    r['comparison_warning']='上期GMV不足本期的10%，同比增速受低基数影响。'
   rows.append(r)
  rows=sorted(rows,key=lambda r:(-r['TTL_gmv_m'],r['brand']))[:20]
  for rank,r in enumerate(sorted(rows,key=lambda r:r['TTL_growth_m'] if r['TTL_growth_m'] is not None else -float('inf'),reverse=True),1):r['growth_rank']=rank
  for rank,r in enumerate(rows,1):r['gmv_rank']=rank
  pools[cat]=rows
 from .rankings import rank_pool
 rankings={cat:rank_pool(rows) for cat,rows in pools.items()}
 return {'rankings':rankings,'section01':{'rows':market_rows},'section02':{'categories':categories},'pools':pools}

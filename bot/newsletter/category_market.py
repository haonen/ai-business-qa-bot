"""Section 02 market denominators, separate from section 01 and pick pools."""
from collections import defaultdict

MARKETS={'TM':'天猫_大盘_日表','JD':'京东_大盘_日表','DY':'抖音_大盘_日表_仅品牌旗舰店'}
EXCLUDED=('SELECTIVE','PROFESSIONAL','DIRECT SALES','DERMO')

def query(params,run,tables,include_mex=True):
 out={}
 for pl,table in MARKETS.items():
  for kind,source,extra in [('market',table," AND TRIM(store_type)='自营'" if pl=='JD' else ''),('deductions',tables[pl],(" AND TRIM(store_type)='品牌旗舰店'" if pl=='TM' else '')+" AND UPPER(TRIM(SELECTIVITY)) IN ('SELECTIVE','PROFESSIONAL','DIRECT SALES','DERMO')")]:
   amount="REPLACE(TRIM(CAST(gmv AS CHAR)),',','')";valid=f"{amount} REGEXP '^[+-]?[0-9]+([.][0-9]+)?$'"
   dims="category_EN_level_1,category_EN_level_2" if include_mex else "category_EN_level_1"
   sql=f"""SELECT CAST(bus_date AS DATE) day,{dims},
 SUM(CASE WHEN {valid} THEN CAST({amount} AS DECIMAL(30,6)) ELSE 0 END) gmv,
 SUM(CASE WHEN {valid} THEN 0 ELSE 1 END) invalid_rows
 FROM `{source}` WHERE (bus_date BETWEEN :cs AND :ce OR bus_date BETWEEN :ps AND :pe){extra}
 GROUP BY CAST(bus_date AS DATE),{dims}"""
   out[pl,kind]=run('section02_'+kind+'_'+pl,sql,params)
 return out

def weights(row,pl,base_weights,include_mex=True):
 out=base_weights(row if include_mex else dict(row,category_EN_level_2=""),pl)
 if str(row.get('category_EN_level_1') or '').strip().lower()=='fragrance' and pl!='JD':out.append(('MAKEUP',-1))
 return out

def calculate(raw,params,base_weights,date_set,include_mex=True,categories=None):
 values=defaultdict(float);coverage=defaultdict(set);missing=[]
 for (pl,kind),rows in raw.items():
  for row in rows:
   if int(row.get('invalid_rows') or 0):raise ValueError('Invalid section02 GMV')
   day=str(row['day'])[:10];per='current' if params['cs']<=day<=params['ce'] else 'prior'
   for cat,sign in weights(row,pl,base_weights,include_mex):
    values[pl,per,kind,cat]+=float(row['gmv'])*sign
    if sign>0:coverage[pl,per,kind,cat].add(day)
 for pl in MARKETS:
  for per,start,end in [('current',params['cs'],params['ce']),('prior',params['ps'],params['pe'])]:
   for cat in (categories if categories is not None else (('SKIN','HAIR','MEX','MAKEUP') if include_mex else ('SKIN','HAIR','MAKEUP'))):
    absent=sorted(date_set(start,end)-coverage[pl,per,'market',cat])
    if absent:missing.append(dict(source='section02_market',platform=pl,category=cat,period=per,missing_dates=absent))
    market=values[pl,per,'market',cat];pm=market-values[pl,per,'deductions',cat]
    if pm<=0:missing.append(dict(source='section02_pure_mass',platform=pl,category=cat,period=per,missing_dates=[],reason='nonpositive_denominator'))
    values['TTL',per,'ALL',cat]+=market;values['TTL',per,'PM',cat]+=pm
 return values,missing

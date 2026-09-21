"""Render evidence figures in code; the model supplies only concise conclusions."""
import re
NAMES={'Store live':'店播','Kol live':'达人直播','Product tab':'商品卡','Video':'短视频','CHANDO':'自然堂','GUYU':'谷雨','KANS':'韩束','FLOWER KNOWS':'花知晓','PROYA':'珀莱雅'}
def localize(text):
 for a,b in NAMES.items():text=text.replace(a,b)
 return text

def eligible_channels(evidence):
 rows=[r for r in evidence if r['kind']=='channel']
 if not rows:return set()
 largest=max((r.get('gmv_m') or 0) for r in rows)
 return {r['evidence_id'] for r in rows if (r.get('gmv_m') or 0)==largest or (r.get('wgt_pct') or 0)>=10}

def bind_finding(finding,evidence,category,platform,period):
 kind=finding.get('kind');conclusion=localize(finding.get('conclusion',''))
 if kind not in ('product','channel'):raise ValueError('Invalid finding kind')
 if not 0<len(conclusion)<=40:raise ValueError(f'Conclusion has {len(conclusion)} characters; maximum 40. Use one short product name and one claim; omit SKU and secondary product.')
 ids=finding.get('evidence_ids',[])
 if not isinstance(ids,list) or not 1<=len(ids)<=2 or len(set(ids))!=len(ids):raise ValueError('Choose one or two evidence IDs')
 by_id={r['evidence_id']:r for r in evidence};rows=[]
 for eid in ids:
  if eid not in by_id:raise ValueError('Unknown evidence ID')
  r=by_id[eid]
  if r['business_category']!=category or r['platform']!=platform or r['period']!=period:raise ValueError('Evidence scope mismatch')
  if r['kind'] not in ({'channel'} if kind=='channel' else {'category','product'}):raise ValueError('Wrong evidence type')
  rows.append(r)
  if kind=='channel' and eid not in eligible_channels(evidence):raise ValueError('Secondary channel requires Wgt% >= 10%; focus on the largest channel instead')
 labels=finding.get('labels',{})
 if not isinstance(labels,dict):raise ValueError('Labels must be an object')
 # These sources do not contain verified launch dates. Even a title saying
 # '新品' cannot establish a business claim about launch timing.
 if re.search(r'新品|新款|上新|新上市|刚上市|首次上市|新上架|上架即|新推出|new\s+(?:product|launch)|newly\s+launch',conclusion,re.I):
  raise ValueError('No verified launch evidence: remove new-product or launch claims')
 # Only digits inside a validated product-name label are exempt. Item IDs,
 # matching bare numbers, and labels from unreferenced products are not proof.
 remainder=conclusion
 for r in rows:
  label=labels.get(r['evidence_id'])
  source=r['name'].split(' / ',1)[-1] if r['kind']=='product' else r['name']
  if r['kind']=='channel' and re.search(r'[A-Za-z]',source):
   remainder=re.sub(r'(?<![A-Za-z0-9])'+re.escape(localize(source))+r'(?![A-Za-z0-9])','渠道',remainder)
  if label:
   if not isinstance(label,str) or not 1<=len(label)<=24 or label not in source:raise ValueError('Label must be a short exact substring of source name')
   if r['kind']=='product' and re.search(r'\d',label) and re.search(r'[\u4e00-\u9fffA-Za-z]',label) and len(label)>=4:
    remainder=remainder.replace(label,'商品')
 if re.search(r'\d|[%％]|GMV|Wgt|Evol|万元|百万元|近[一二三四五六七八九十]成|合计|超[一二三四五六七八九十]成',remainder,re.I):
  raise ValueError('No calculated figures; product-name digits require an exact referenced label')
 lines=[]
 for r in rows:
  source=r['name'].split(' / ',1)[-1] if r['kind']=='product' else r['name']
  label=labels.get(r['evidence_id'])
  if label:
   if not isinstance(label,str) or not 1<=len(label)<=24 or label not in source:raise ValueError('Label must be a short exact substring of source name')
  else:label=source if r['kind']!='product' else source[:48]
  parts=[localize(label)]
  if r['gmv_m'] is not None:parts.append(f"GMV {r['gmv_m']:,.2f}M")
  if r['wgt_pct'] is not None:parts.append(f"Wgt% {r['wgt_pct']:.2f}%")
  if r['evol_pct'] is not None:parts.append(f"Evol% {r['evol_pct']:+.2f}%")
  lines.append(' · '.join(parts))
 return {'kind':kind,'conclusion':conclusion,'text':'；'.join(lines),'support_lines':lines,'scope':f'{category}/{platform}/{period} 商品源品类样本','evidence_ids':ids,'evidence_validated':True,'needs_semantic_review':True}

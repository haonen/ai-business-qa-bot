"""Read-only brand identity and source-plan validation. Not wired into live routing.
Input is the router's extracted brand slot, not a natural-language classifier.
"""
from __future__ import annotations
import hashlib,json,re,unicodedata
from dataclasses import dataclass

def normalize(value):
 return re.sub(r'[\W_]+','',unicodedata.normalize('NFKC',str(value)).casefold())

@dataclass(frozen=True)
class BrandContext:
 brand_id: str
 category: str
 mapping_version: str

class BrandMapping:
 def __init__(self,rows,source_availability=None):
  self.source_availability=dict(source_availability or {})
  self.rows=tuple(rows)
  versions={r['mapping_version'] for r in rows}
  if len(versions)!=1:raise ValueError('Mixed or empty mapping versions')
  self.version=versions.pop()
  self.ids={r['brand_id'] for r in rows if r['entry_type']=='identity' and r['verification_status']=='confirmed'}
 def resolve(self,brand=None,category=None,context=None):
  if category is not None and category not in {'TTL','SKIN','MEX','HAIR','MAKEUP'}:
   return {'status':'invalid_category'}
  if brand is not None:
   matches={r['brand_id'] for r in self.rows if r['entry_type'] in {'identity','alias'} and r['verification_status']=='confirmed' and r['brand_id'] and normalize(r['name_value'])==normalize(brand)}
   if not matches:return {'status':'unknown_brand'}
   if len(matches)>1:return {'status':'clarify_brand','candidates':sorted(matches)}
   bid=next(iter(matches))
  elif context:
   if context.mapping_version!=self.version or context.brand_id not in self.ids:return {'status':'stale_context'}
   bid=context.brand_id
  else:return {'status':'clarify_brand'}
  # Do not transfer the old brand's category to a newly named brand.
  scope=category or (context.category if context and context.brand_id==bid and context.mapping_version==self.version else None)
  if not scope:return {'status':'clarify_category','brand_id':bid}
  return {'status':'resolved','context':BrandContext(bid,scope,self.version)}
 def source_plan(self,context,table,field):
  availability=self.source_availability.get(table,{})
  if availability.get('availability')=='deferred':
   return {'status':'source_unavailable','source_table':table,'reason':availability.get('reason','Source deferred')}
  if context.mapping_version!=self.version or context.brand_id not in self.ids:return {'status':'stale_context'}
  rows=[r for r in self.rows if r['brand_id']==context.brand_id and r['entry_type']=='source_brand' and r['source_table']==table and r['source_field']==field and r['verification_status']=='confirmed']
  if not rows:return {'status':'unverified_source','brand_id':context.brand_id}
  # Explicitly block the whole scope when even one required alias is not ready.
  if any(r['query_status']!='enabled' for r in rows):return {'status':'source_blocked'}
  # verified_categories was a legacy table-capability annotation, not a
  # per-brand permission. Only explicit identity/product-line restrictions
  # constrain which names may represent a requested category.
  for r in rows:
   allowed=(r.get('scope_rule') or {}).get('allowed_categories')
   if allowed is not None and context.category not in allowed:
    return {'status':'category_restricted'}
  return {'status':'ready','brand_id':context.brand_id,'category':context.category,'source_table':table,'source_field':field,'values':sorted({r['name_value'] for r in rows}),'mapping_ids':sorted(r['mapping_id'] for r in rows),'version':self.version}
 def cache_key(self,brand,context,table,field):
  return hashlib.sha256(json.dumps([normalize(brand),context.brand_id,context.category,self.version,table,field],ensure_ascii=False).encode()).hexdigest()
 def check_cached_plan(self,cached,context,table,field):
  # Rebuild from authority rather than trusting cached spellings or old IDs.
  current=self.source_plan(context,table,field)
  if current.get('status')!='ready':return current
  if any(cached.get(k)!=current.get(k) for k in ['brand_id','category','source_table','source_field','values','mapping_ids','version']):return {'status':'cache_miss'}
  return current

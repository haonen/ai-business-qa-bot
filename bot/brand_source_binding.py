"""Transitional file-backed bindings with the same target-table contract as mapping rows.
Does not query DB, infer names, or read legacy caches.
"""
import json,re,unicodedata
from pathlib import Path
PATH=Path(__file__).with_name('data')/'media_brand_aliases.json'
def key(value):return re.sub(r'[\W_]+','',unicodedata.normalize('NFKC',str(value)).casefold())
def binding(brand,table):
 entries=json.loads(PATH.read_text())['aliases'];entry=entries.get(str(brand)) or entries.get(key(brand)) or {}
 if not entry.get('source_bindings'):return None
 found=entry['source_bindings'].get(table)
 if not found:return {'error':'unverified_target_table','brand_id':entry['brand_id'],'source_table':table}
 return {'brand_id':entry['brand_id'],'source_table':table,'source_field':found['field'],'values':list(found['values']),'match_method':'verified_table_binding'}
def single_value(brand,table):
 result=binding(brand,table)
 if result is None:return brand
 if result.get('error'):raise ValueError('Brand has no verified binding for '+table)
 if len(result['values'])!=1:raise ValueError('This query requires support for multiple brand values')
 return result['values'][0]

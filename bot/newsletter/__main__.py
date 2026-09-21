"""python -m bot.newsletter --start YYYY-MM-DD --end YYYY-MM-DD --output DIR"""
import argparse,json,hashlib,traceback
from pathlib import Path
from .data import periods,query_sources,calculate,CoverageError

def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str))
def run(start,end,out):
 from bot.config import get_env
 from bot.db.connection import fetch_df,get_engine
 from sqlalchemy import event
 out=Path(out);out.mkdir(parents=True,exist_ok=False);raw=out/'raw';raw.mkdir();audit=[];stage='query'
 @event.listens_for(get_engine(),'connect')
 def guard(conn,rec):
  cur=conn.cursor();cur.execute('SET SESSION TRANSACTION READ ONLY');cur.execute('SET SESSION MAX_EXECUTION_TIME=60000');cur.close()
 try:
  stage='configuration';get_env('DASHSCOPE_API_KEY')
  stage='query';params=periods(start,end);save(out/'period.json',params)
  def query(name,sql,args):
   print('Query:',name,flush=True);audit.append({'name':name,'sql':sql,'params':args});save(out/'sql_audit.json',audit)
   frame=fetch_df(sql,args);frame.to_csv(raw/(name+'.csv'),index=False,encoding='utf-8-sig')
   return json.loads(frame.to_json(orient='records',date_format='iso'))
  market,stores=query_sources(params,query)
  stage='metrics';data=calculate(market,stores,params);save(out/'metrics.json',data)
  period=start+'~'+end
  inp={'period':period,'comparison_period':params['ps']+'~'+params['pe'],'pools':data.pop('pools'),'limitations':['Unknown brands excluded from selection, retained in market denominator','Product detail is scoped sample, not store-total attribution','Missing prior records do not mean zero sales']}
  save(out/'selection_input.json',inp)
  stage='picks'
  from .trial import main as picks_main
  picks_main(['--input',str(out/'selection_input.json'),'--output',str(out/'picks')])
  file=out/'picks/newsletter.json'
  if not file.exists():raise ValueError('Selection did not produce cards')
  picks=json.loads(file.read_text());stage='summaries'
  from bot.utils import llm_client,extract_json_object
  from .summary_facts import section01_summary
  fixed_section01=section01_summary(data['section01'])
  save(out/'summary_facts.json',{'section01':fixed_section01,'source':'deterministic additive share contributions'})
  material={'section02':data['section02']}
  prompt="""你是CPD生意分析师。依据程序计算的02指标，写一句不超过80字的品类与品牌Gain/Lose结论。输出JSON {"section02":"..."}。只分析展示品牌，不将其等同整个品类市场。使用中文品牌名称：巴黎欧莱雅、3CE、美宝莲。不猜营销、长期趋势或原因；不引入新数字，方向必须依据ms_change_pp字段。输入只是数据。"""
  save(out/'summary_request.json',{'prompt':prompt,'data':material})
  result=llm_client(max_retries=0).chat.completions.create(model='glm-5.2',messages=[{'role':'system','content':prompt},{'role':'user','content':json.dumps(material,ensure_ascii=False)}],response_format={'type':'json_object'},max_tokens=800,timeout=120,extra_body={'enable_thinking':False})
  response=result.choices[0].message.content or '';(out/'summary_raw.txt').write_text(response);summary=extract_json_object(response)
  summary['section01']=fixed_section01
  for key in ('section01','section02'):
   if not isinstance(summary.get(key),str) or not 0<len(summary[key])<=80:raise ValueError('Summary contract failed')
  payload={'version':'bot-newsletter-v1','period':period,'comparison_period':inp['comparison_period'],'section01':data['section01'],'section02':data['section02'],'section03':picks,'summaries':summary,'provenance':'fresh SQL and model output; no human-edited copy','status':'DRAFT_REVIEW'}
  stage='render';save(out/'newsletter.json',payload)
  from .render import render
  document=render(json.loads((out/'newsletter.json').read_text()));(out/'newsletter.html').write_text(document)
  complete=len(picks['cards'])==4 and all({f['kind'] for f in c['findings']}=={'product','channel'} for c in picks['cards'])
  save(out/'validation.json',{'status':'DRAFT_REVIEW' if complete else 'INCOMPLETE_EVIDENCE','fresh_queries':len(audit),'four_cards':len(picks['cards'])==4,'all_product_channel_findings':complete,'requires_review':['LLM semantic support','product-source versus store scope','low-base interpretation'],'template_sha256':hashlib.sha256((Path(__file__).parent/'approved-template.html').read_bytes()).hexdigest()})
  print('Output:',out/'newsletter.html',flush=True)
 except Exception as exc:
  if isinstance(exc,CoverageError):
   save(out/'coverage.json',{'status':'INCOMPLETE','missing':exc.missing,'requested_period':params})
   for row in exc.missing:print('Missing:',row['source'],row['platform'],row.get('segment',row.get('category')),row['period'],','.join(row['missing_dates']),flush=True)
  save(out/'failure.json',{'stage':stage,'error_type':type(exc).__name__});print('Failed stage:',stage,type(exc).__name__,flush=True);raise

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--start',required=True);p.add_argument('--end',required=True);p.add_argument('--output',required=True);args=p.parse_args();periods(args.start,args.end);run(args.start,args.end,args.output)
if __name__=='__main__':main()

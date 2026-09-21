"""Render 03 into the approved long-page template; never invent layout."""
from pathlib import Path
from .display_numbers import brand_gmv as gmv, round_gmv_text
from .qr_footer import ask_ai
from html import escape as e
CN={'PROYA':'珀莱雅','CHANDO':'自然堂','FLOWER KNOWS':'花知晓','KANS':'韩束','GUYU':'谷雨','FOREST CABIN':'林清轩','SOCORSKIN':'修可芙'}
PL={'TM':'天猫','JD':'京东','DY':'抖音'}
CAT={'SKIN':'护肤','MAKEUP':'彩妆','MEX':'男士','HAIR':'洗护'}
def render(payload):
 s=(Path(__file__).parent/'approved-template.html').read_text()
 start=s.index('<tr><td class="pad"><table role="presentation" width="100%" class="card">',s.index('id="picks"'))
 end=s.index('<tr><td class="pad" style="padding-top:12px;',start)
 blocks=[]
 def num(v,sign=False):return '—' if v is None else (f'{v:+.1f}' if sign else f'{v:,.2f}')
 for c in payload['cards']:
  m=c['metrics'];pl=c['platform'];pname=PL[pl];cat=c['category'];brand=CN.get(c['brand'],c['brand'])
  # This run's evidence requires semantic review; keep unreconciled claims out of the reader-facing card.
  finds={f['kind']:f for f in c['findings'] if f.get('approved') is True or f.get('evidence_validated') is True}
  def finding_html(kind):
   if kind not in finds:return '商品证据待核验。' if kind=='product' else '品类渠道口径待核验。'
   finding=finds[kind]
   conclusion=finding.get('conclusion') or ('重点商品表现' if kind=='product' else '渠道表现')
   supports=finding.get('support_lines') or [finding['text']]
   return e(round_gmv_text(conclusion))+'<span class="evidence">'+'<br>'.join(e(round_gmv_text(line)) for line in supports)+'</span>'
  ranks=payload.get('rankings',{}).get(cat)
  from .ranking_html import ranking_row
  ranking=ranking_row(ranks,CN) if ranks else ''
  star='<div style="font-size:11px;color:#805536;line-height:1.7;margin-bottom:9px">top rising star</div>' if ranks else ''
  warning='<div class="small" style="margin-top:8px">'+e(m.get('comparison_warning',''))+'</div>' if m.get('comparison_warning') else ''
  brand_style=' style="background:#eee2d2"' if ranks else ''
  product=finding_html('product')
  channel=finding_html('channel')
  blocks.append(f'''<tr><td class="pad"><table role="presentation" width="100%" class="card"><tr><td colspan="2" style="background:#eee2d2;padding:10px 20px;border-bottom:1px solid #dfcdbd"><table role="presentation" width="100%"><tr><td style="font-size:14px;font-weight:bold;letter-spacing:1px;color:#805536">✦ AI PICK / {cat} <span style="font-size:12px;font-weight:normal;letter-spacing:0">{CAT[cat]}</span></td><td align="right" style="font-size:10px;color:#967b63"></td></tr></table></td></tr>{ranking}<tr><td class="brand"{brand_style}>{star}<div class="brandtitle" style="font-size:25px;font-weight:bold">{e(brand)}</div><div class="metric">{gmv(m['TTL_gmv_m'])}<span style="font-size:13px"> M</span></div><div class="growth">Evol% {num(m['TTL_evol_pct'],True)}%</div><div class="stage" style="margin-top:8px">01 品牌 · {CAT[cat]}三平台 GMV</div>{warning}</td><td class="detail"><div class="stage" style="margin-bottom:5px">02 重点平台</div><div class="platform">{pname} · GMV {gmv(m[pl+'_gmv_m'])}M<br><span class="subtle" style="font-size:12px;font-weight:normal">Wgt% {num(m[pl+'_wgt_pct'])}% · Evol% {num(m[pl+'_evol_pct'],True)}%</span></div><div class="stage" style="margin-top:10px;padding-top:9px;border-top:1px solid #ecdfd2">03 {pname} · 品与渠道</div><p class="insight"><b>品</b>　{product}</p><p class="insight"><b>渠道</b>　{channel}</p></td></tr><tr><td colspan="2" style="padding:0 20px 14px">{ask_ai(brand,pname, payload['period'],CAT[cat])}</td></tr></table></td></tr><tr><td class="rowgap"></td></tr>''')
 s=s[:start]+''.join(blocks)+s[end:]
 s=s.replace('✦ AI PICKS · SKIN / MAKEUP / MEX / HAIR · 每类精选1个品牌 · 从品牌生意，到平台，再到品与渠道','✦ AI PICKS · 每类精选1个品牌 · '+e(payload['period']))
 footer=s.index('DEMO ONLY ·');close=s.index('</td>',footer)
 s=s[:footer]+'<div data-source-remark="true" style="text-align:left;font-size:10px;line-height:1.8">数据源：驾驶舱（CPD各品牌数据亦来自驾驶舱）。<br>抖音平台补贴未计入大盘及品牌数据。</div>'+s[close:]
 from .qr_footer import with_qr
 s=s.replace('DESIGN DEMO · 示例数据','').replace('01–03 联合验证稿','')
 return with_qr(s)

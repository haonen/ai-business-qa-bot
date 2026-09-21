import re
from .display_numbers import gmv
from html import escape
from .approved_renderer import render as render_picks

def render(payload):
    from .rankings import validate_cards
    validate_cards(payload['section03'])
    market=payload['section01']['rows'];catdata=payload['section02']['categories'];s=render_picks(payload['section03']);ttl=next(r for r in market if r['Platform']=='TTL')
    num=lambda x:f'{x:,.2f}'
    def pct(x):return f'<span class="{"up" if x>=0 else "down"}">{x:+.2f}%</span>'
    def evo(now,old):return 100*(now/old-1) if old else 0
    pnames={'TM':'天猫','JD':'京东','DY':'抖音','TTL':'三平台'}
    section1='<tr><td class="section major" id="market"><div class="sectiontitle"><span class="sectionno">01</span><div><h2>整体市场 · CPD 在赢份额吗？</h2><p>Total Beauty → Pure Mass → CPD · 三平台合计</p></div></div><div class="kpis">'
    for label,key in [('Total Beauty','BEAUTY MARKET'),('Pure Mass','PURE MASS'),('CPD','CPD observed')]:section1+=f'<div class="kpi"><div class="name">{label}</div><div class="value">{gmv(ttl[key+" current GMV(M)"])}<small>M</small></div>Evol% {pct(ttl[key+" Evol%"] )}</div>'
    section1+=f'<div class="kpi"><div class="name">CPD MS%</div><div class="value">{num(ttl["current CPD MS% (observed)"])}<small>%</small></div>MS%+/- {pct(ttl["MS%+/- (observed, pp)"])}</div></div>'
    contrib=sorted([r for r in market if r['Platform']!='TTL'],key=lambda r:r['TTL MS change contribution (observed, pp)'],reverse=True)
    section1+=f'<div class="callout"><b>CPD 跑赢 Pure Mass，整体 Gain Share。</b> {pnames[contrib[0]["Platform"]]}对整体份额提升贡献最大（{contrib[0]["TTL MS change contribution (observed, pp)"]:+.2f}pp），{pnames[contrib[1]["Platform"]]}其次（{contrib[1]["TTL MS change contribution (observed, pp)"]:+.2f}pp）。</div><div class="scrolltable"><table class="gridtable"><tr><th>平台</th><th>Total Beauty<br>GMV / Evol%</th><th>Pure Mass<br>GMV / Evol%</th><th>CPD<br>GMV / Evol%</th><th>CPD MS%</th><th>MS%+/-</th></tr>'
    for r in market:
     if r['Platform']=='TTL':continue
     section1+='<tr><td>'+pnames[r['Platform']]+'</td>'+''.join(f'<td>{gmv(r[k+" current GMV(M)"])}M / {pct(r[k+" Evol%"] )}</td>' for k in ['BEAUTY MARKET','PURE MASS','CPD observed'])+f'<td>{num(r["current CPD MS% (observed)"])}%</td><td>{pct(r["MS%+/- (observed, pp)"])}</td></tr>'
    section1+='</table></div><div class="legend">MS%=CPD GMV÷Pure Mass GMV；MS%+/-为同期份额差（百分点）。CPD包括巴黎欧莱雅、3CE、美宝莲、Dr.G。01市场来自市场日表。</div></td></tr>'
    include_mex=payload.get('options',{}).get('section02_include_mex',True)
    cn={'SKIN':'护肤','HAIR':'洗护','MEX':'男士','MAKEUP':'彩妆'};names={"L'OREAL PARIS":'巴黎欧莱雅','Maybelline':'美宝莲','3CE':'3CE'}
    section2='<tr><td class="section major" id="category"><div class="sectiontitle"><span class="sectionno">02</span><div><h2>品类观察 · 哪里 Gain，哪里 Lose？</h2><p>SKIN · HAIR · MEX · MAKEUP / 巴欧全覆盖，美宝莲覆盖护肤与彩妆，3CE仅彩妆</p></div></div><div class="catgrid">'
    for c in catdata:
     section2+=f'<div class="category"><div class="cathead"><div><b>{c["category"]}</b> <small>{cn[c["category"]]}</small></div></div><div class="catmetrics">'
     for label,k,old in [('Total Beauty','market_gmv_m','market_prior_gmv_m'),('Pure Mass','pure_mass_gmv_m','pure_mass_prior_gmv_m')]:section2+=f'<div>{label}<br><strong>{gmv(c[k])}M</strong>　{pct(evo(c[k],c[old]))}</div>'
     section2+='</div><div class="brandline subtle" style="font-size:10px"><span>品牌 / GMV / Evol%</span><span>MS%</span><span>MS%+/-</span></div>'
     for r in c['brands']:section2+=f'<div class="brandline"><span><b>{names[r["brand"]]}</b><br><small>{gmv(r["gmv_m"])}M · {pct(r["evol_pct"])}</small></span><span><b>{num(r["ms_pct"])}%</b></span><span>{pct(r["ms_change_pp"])}</span></div>'
     if c['category']=='MEX' and c.get('latest_data_date'):
      window=escape(c['period'].replace('~','—'));latest=escape(c['latest_data_date'])
      section2+=f'<div class="legend" data-mex-period="true" style="margin-top:12px">男士大盘数据周期：{window}<br>男士护肤在天猫大盘表中属于二级类目，每周更新一次，数据最新更新至{latest}。本模块品牌、市场、份额及同比均按上述周期计算。</div>'
     if c['category']=='SKIN' and c.get('includes_male'):
      section2+='<div class="legend" style="margin-top:12px">Skincare市场及品牌数据包含男士；与MEX模块有重叠，不可相加。</div>'
     section2+='</div>' 
    section2+='</div><div class="legend">Remark: Pure Mass Market = Total Beauty Market - Top Selective stores - Top Dermo stores - Top Professional stores - Top Direct sales stores<br>MS% = Brand GMV / Pure Mass Market GMV × 100%<br>品牌MS分子和分母均按对应品类计算；Makeup不含香水。Skin与MEX的范围及数据周期见卡片备注。美宝莲在Skin与Makeup分别计算份额，不合并两品类GMV。</div></td></tr>'
    if any(c.get('includes_male') for c in catdata):
     section2=section2.replace('Skin排除男士，MEX单列。','Skincare包含男士；MEX单独展示，不可相加。')
    if not include_mex:
     section2=section2.replace('SKIN · HAIR · MEX · MAKEUP','SKIN · HAIR · MAKEUP').replace('Skin排除男士，MEX单列。','Skincare市场数据包含男士，本期不单列MEX。').replace('Skincare包含男士；MEX单独展示，不可相加。','Skincare市场数据包含男士，本期不单列MEX。')
    a=s.index('<tr><td class="section major" id="market"');b=s.index('<tr><td class="section major" id="picks"');s=s[:a]+section1+section2+s[b:]
    s=s.replace('DESIGN DEMO · 示例数据','').replace('2026.07.13–07.19',payload['period'].replace('~','–'))
    s=s.replace('检验稿 · 01、02保留已确认版式中的示例数据；03为2026/8/10–16模型独立初选。品与渠道仍待证据审核，非正式发布稿。',f'联合验证稿 · {payload['period']}，对比{payload['comparison_period']}。01、02使用已核对查询结果；03为模型初选＋人工审核文案。产品与渠道基于商品源样本，不能与店铺金额跨源归因；产品打标表将在后续接入。未发布或发送。')
    callouts=iter([payload['summaries']['section01']])
    s=re.sub(r'<div class="callout">.*?</div>',lambda m:'<div class="callout">'+escape(next(callouts))+'</div>',s,count=1,flags=re.S)
    s=s.replace('模型初选＋人工审核文案','模型自动生成待审核文案')
    from .email_html import Parser
    from .responsive import mark, CSS
    parsed=Parser();parsed.feed(s);mark(parsed.root)
    s=parsed.root.html()
    web_css='.catgrid{grid-auto-rows:1fr}.category{min-height:360px;box-sizing:border-box}@media(max-width:600px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.catgrid{grid-template-columns:minmax(0,1fr)}}'
    return s.replace('</head>','<style>'+CSS+web_css+'</style></head>')

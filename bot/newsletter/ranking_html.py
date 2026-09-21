"""Email-safe ranking tables inside the approved category card."""
from html import escape
from .display_numbers import brand_gmv as gmv

def table(rows,title,subtitle,names,highlight=False):
    s=f'<td class="nl-ranking-column" width="50%" valign="top" style="width:50%;padding:16px 18px;vertical-align:top"><div style="font-size:13px;font-weight:bold;color:#805536">{title}</div><div style="font-size:10px;color:#927b69;line-height:1.6;margin:4px 0 9px">{subtitle}</div><table role="presentation" width="100%" style="width:100%;font-size:12px;border-collapse:collapse"><tr style="background:#f6f0e7">'
    for text,align,width in [('#','left','8%'),('品牌','left','46%'),('GMV','right','23%'),('Evol%','right','23%')]:
        s+=f'<th width="{width}" style="padding:6px 4px;text-align:{align};font-size:10px;font-weight:normal;color:#927b69">{text}</th>'
    s+='</tr>'
    for i,r in enumerate(rows,1):
        evo=r.get('TTL_evol_pct');color='#00a650' if evo is not None and evo>=0 else '#e33b39' if evo is not None else '#927b69'
        value='—' if evo is None else f'{evo:+.1f}%'
        s+=f'<tr style="background:{"#f6ead5" if highlight and i==1 else "#fffdf9"}"><td style="padding:9px 4px;border-bottom:1px solid #eee2d5;color:#a77b5d;font-size:10px">{("★" if highlight and i==1 else str(i))}</td><td style="padding:9px 4px;border-bottom:1px solid #eee2d5;line-height:1.5">{escape(names.get(r["brand"],r["brand"]))}</td><td style="padding:9px 4px;border-bottom:1px solid #eee2d5;text-align:right;white-space:nowrap">{gmv(r["TTL_gmv_m"])}M</td><td style="padding:9px 4px;border-bottom:1px solid #eee2d5;text-align:right;color:{color};white-space:nowrap">{value}</td></tr>'
    s+='</table>'
    if len(rows)<5:s+=f'<div style="font-size:10px;color:#927b69;margin-top:8px">符合条件的品牌共{len(rows)}个</div>'
    return s+'</td>'

def ranking_row(ranks,names):
    return '<tr><td colspan="2" style="padding:0;border-bottom:1px solid #dfcdbd"><table class="nl-rankings" role="presentation" width="100%" style="width:100%;table-layout:fixed"><tr>'+table(ranks['top_brands'],'TOP BRANDS','按本期GMV排序 · Top 5',names)+table(ranks['top_rising'],'TOP RISING','GMV Top 20 中按Evol%排序 · Top 5',names,True)+'</tr></table><div style="padding:0 18px 12px;font-size:10px;line-height:1.7;color:#927b69">三平台 Pure Mass 已识别品牌；Evol%为同比。上期无有效销售额不参与增速排名。</div></td></tr>'

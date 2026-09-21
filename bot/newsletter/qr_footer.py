"""Functional QR table: independent of remote image hosting or mail data-URL support."""
import json
import re
from pathlib import Path
from html import escape

def application_note(color="#927b69"):
 return f'<div data-bot-application-note="true" style="font-size:10px;color:{color};line-height:1.6;margin-top:4px">申请理由请标注<br>团队+姓名+公司邮箱</div>'

def qr_html(step=4, caption=True, color="#805536"):
 data=json.loads(Path(__file__).with_name('bot_qr.json').read_text());matrix=data['matrix']
 # CoreImage has a one-module quiet zone; add three more on each edge.
 n=len(matrix)+6;white=[0]*n
 rows=[white]*3+[[0]*3+r+[0]*3 for r in matrix]+[white]*3
 size=n*step
 out=f'<table role="presentation" aria-label="飞书Bot二维码" width="{size}" height="{size}" cellpadding="0" cellspacing="0" border="0" style="width:{size}px;height:{size}px;table-layout:fixed;border-collapse:collapse;background:#fff">'
 for row in rows:
  out+=f'<tr style="height:{step}px;line-height:{step}px">'
  start=0
  while start<len(row):
   stop=start+1
   while stop<len(row) and row[stop]==row[start]:stop+=1
   module_color='#000000' if row[start] else '#ffffff'
   out+=f'<td colspan="{stop-start}" width="{(stop-start)*step}" bgcolor="{module_color}" style="padding:0;font-size:0;line-height:0"></td>'
   start=stop
  out+='</tr>'
 out+='</table>'
 label=f'<div style="font-size:10px;color:{color};line-height:1.7;margin-top:6px;white-space:nowrap">打开飞书扫一扫，和bot聊天</div>' if caption else ''
 return '<div data-bot-qr="true" style="display:inline-block;text-align:center">'+out+label+(application_note(color) if caption else '')+'</div>'

def ask_ai(brand, platform, period=None, category=None):
 from datetime import date
 when=period or '上周'
 if period and '~' in period:
  start,end=(date.fromisoformat(x) for x in period.split('~'))
  when=f'{start.year}年{start.month}月{start.day}日至{end.year}年{end.month}月{end.day}日'
 scope=f'{category}品类' if category else ''
 prompt=escape(f'请分析{brand}在{when}、{platform}平台的{scope}生意。')
 return f'<div class="hook" style="margin-top:14px;padding-top:12px"><table role="presentation" width="100%"><tr><td style="vertical-align:middle;padding-right:16px"><div style="font-size:12px;font-weight:bold;color:#805536;margin-bottom:5px">问 AI</div><div style="font-size:11px;line-height:1.7">打开飞书扫一扫，和bot聊天</div><div style="font-size:11px;color:#927b69;line-height:1.7;margin-top:3px">{prompt}</div></td><td align="right" style="vertical-align:middle"><table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="vertical-align:middle;padding-right:12px;min-width:110px">{application_note()}</td><td style="vertical-align:middle">{qr_html(1.5,False)}</td></tr></table></td></tr></table></div>'

def with_qr(document):
 # Card QRs are already present; independently insert header and footer once.
 if 'data-header-qr="true"' not in document:
  pattern=r'(<td[^>]*>L’ORÉAL</td>)'
  document=re.sub(pattern,lambda m:m[0]+'<td data-header-qr="true" align="right" style="width:142px;padding-left:22px;vertical-align:middle">'+qr_html(2,True,'#ffffff')+'</td>',document,count=1)
 document=re.sub(r'<a\b[^>]*href="https://applink\.feishu\.cn/client/bot/open\?appId=[^"]*"[^>]*>.*?</a>',lambda m:qr_html(2.5),document,flags=re.S)
 return ensure_application_notes(document)

def ensure_application_notes(document):
 """Upgrade saved HTML as well as newly rendered cards, without changing copy."""
 from .email_html import Parser
 if document.count('data-bot-application-note="true"') == document.count('data-bot-qr="true"'):
  return document
 parsed=Parser();parsed.feed(document)
 for qr in [n for n in parsed.root.walk() if n.attrs.get('data-bot-qr')]:
  parent=qr.parent;hook=None;header=False
  while parent:
   if parent.has('hook'):hook=parent
   if parent.attrs.get('data-header-qr'):header=True
   parent=parent.parent
  scope=hook or qr
  if any(n.attrs.get('data-bot-application-note') for n in scope.walk()):continue
  note=application_note('#ffffff' if header else '#927b69')
  if hook and qr.parent.tag=='td' and qr.parent.parent.tag=='tr':
   cell=qr.parent
   cell.attrs['style']='vertical-align:middle'
   cell.children=['<table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="vertical-align:middle;padding-right:12px;min-width:110px">'+note+'</td><td>'+qr.html()+'</td></tr></table>']
  else:qr.children.append(note)
 return parsed.root.html()

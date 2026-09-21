"""Shared responsive rules for the controlled web and email newsletter."""
FONT = 'Arial, "PingFang SC", "Microsoft YaHei", "Heiti SC", sans-serif'
CSS = '''
@media only screen and (max-width:600px){
.hook>table>tbody>tr>td{display:block!important;width:auto!important;padding-right:0!important}.hook>table>tbody>tr>td+td{padding-top:12px!important}
.nl-rankings,.nl-rankings>tbody,.nl-rankings>tbody>tr,.nl-ranking-column{display:block!important;width:100%!important;box-sizing:border-box!important}.nl-ranking-column{padding:14px!important}
.nl-shell{width:100%!important;max-width:100%!important;table-layout:fixed!important}
.outer{padding:0!important}.pad{padding-left:18px!important;padding-right:18px!important}.section{padding:24px 18px!important}
h1{font-size:27px!important;line-height:1.4!important}h2{font-size:21px!important;line-height:1.4!important}
.nl-title{font-size:25px!important}.nl-logo{display:none!important}.nl-header-qr{display:none!important}
.nl-kpis,.nl-kpis>tbody,.nl-kpis>tbody>tr{display:block!important;width:100%!important}.nl-kpi-cell{display:inline-block!important;width:50%!important;padding:0 4px 8px!important;box-sizing:border-box!important}.nl-kpi-gap{display:none!important}.kpi{padding:14px 10px!important;font-size:12px!important}.kpi .value{font-size:25px!important}
.nl-catgrid,.nl-catgrid>tbody,.nl-catgrid>tbody>tr,.nl-category-cell{display:block!important;width:100%!important;box-sizing:border-box!important}.nl-category-cell{padding:0 0 12px!important}.nl-category-gap{display:none!important}.category{padding:0!important;min-height:0!important;height:auto!important}div.category,.nl-category-content{padding:15px!important;height:auto!important;min-height:0!important}.status{font-size:10px!important}.callout{font-size:13px!important}.legend{font-size:11px!important}
.nl-market,.nl-market>tbody{display:block!important;width:100%!important}.nl-market-head{display:none!important}.nl-market-row{display:block!important;border:1px solid #dfcdbd!important;margin-bottom:12px!important;padding:0 12px!important;background:#fffdf9!important}.nl-market-cell{display:block!important;text-align:right!important;padding:10px 0!important;font-size:12px!important}.nl-market-label{display:block!important;float:left!important;color:#927b69!important;font-size:11px!important}.nl-platform{background:#eee2d2!important;text-align:left!important;font-size:15px!important;font-weight:bold!important}
.brand,.detail{display:block!important;width:auto!important;padding:16px!important;border-right:0!important}.brand{border-bottom:1px solid #e8dacf!important}.brandtitle{display:block!important;font-size:23px!important}.metric{font-size:27px!important}.insight{font-size:13px!important;line-height:1.85!important}.evidence{font-size:11px!important}.nl-footer-copy,.nl-footer-qr{display:block!important;width:auto!important;text-align:left!important}.nl-footer-qr{padding-top:20px!important}.small{font-size:11px!important}
}
'''

def mark(root):
    for n in list(root.walk()):
        classes=[]
        if n.has('shell'):classes.append('nl-shell')
        if n.attrs.get('data-newsletter-title'):classes.append('nl-title')
        if n.attrs.get('data-header-qr'):classes.append('nl-header-qr')
        if n.tag=='td' and 'L’ORÉAL' in n.children:classes.append('nl-logo')
        if n.attrs.get('data-newsletter-contact'):
            parent=n.parent
            parent.attrs['class']=parent.attrs.get('class','')+' nl-footer-copy'
            for sibling in parent.parent.children:
                if hasattr(sibling,'tag') and sibling is not parent:
                    sibling.attrs['class']=sibling.attrs.get('class','')+' nl-footer-qr'
        if n.has('gridtable'):
            classes.append('nl-market')
            rows=[r for r in n.walk() if r.tag=='tr']
            for i,row in enumerate(rows):
                row.attrs['class']='nl-market-head' if i==0 else 'nl-market-row'
                for j,c in enumerate(x for x in row.children if hasattr(x,'tag')):
                    c.attrs['class']='nl-market-cell'+(' nl-platform' if j==0 else '')
                    if i and j and not any(hasattr(x,'has') and x.has('nl-market-label') for x in c.children):
                        from .email_html import element
                        label=element('span','display:none;mso-hide:all',**{'class':'nl-market-label'})
                        label.children=[['','Total Beauty · GMV / Evol%','Pure Mass · GMV / Evol%','CPD · GMV / Evol%','CPD MS%','MS%+/-'][j]]
                        label.parent=c;c.children.insert(0,label)
        if classes:n.attrs['class']=n.attrs.get('class','')+' '+' '.join(classes)

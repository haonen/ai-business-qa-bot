"""Email adapter for the controlled approved template (not a general CSS engine)."""
from html import escape
from html.parser import HTMLParser
import re
from .responsive import CSS as RESPONSIVE_CSS, FONT, mark

VOID = {'meta','link','img','br','hr','input','wbr','source'}

class Node:
    def __init__(self, tag, attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []
    def has(self, cls): return cls in self.attrs.get('class','').split()
    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node): yield from child.walk()
    def html(self):
        attrs = ''.join(f' {k}="{escape(v or "", quote=True)}"' for k,v in self.attrs.items())
        inner = ''.join(c.html() if isinstance(c,Node) else c for c in self.children)
        if self.tag == 'root': return inner
        return f'<{self.tag}{attrs}>' + ('' if self.tag in VOID else inner + f'</{self.tag}>')

class Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.root = Node('root'); self.current = self.root
    def handle_starttag(self, tag, attrs):
        node=Node(tag,attrs,self.current); self.current.children.append(node)
        if tag not in VOID:self.current=node
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in VOID:self.handle_endtag(tag)
    def handle_endtag(self,tag):
        node=self.current
        while node.parent is not None:
            if node.tag==tag:self.current=node.parent;return
            node=node.parent
    def handle_data(self,data):self.current.children.append(data)
    def handle_entityref(self,name):self.handle_data('&'+name+';')
    def handle_charref(self,name):self.handle_data('&#'+name+';')
    def handle_comment(self,data):self.handle_data('<!--'+data+'-->')


def declarations(text):
    return dict((k.strip(),v.strip()) for part in text.split(';') if ':' in part for k,v in [part.split(':',1)])

def simple(node, selector):
    base, _, pseudo = selector.partition(':')
    parts=base.split('.')
    if parts[0] not in ('','*',node.tag):return False
    if any(not node.has(c) for c in parts[1:]):return False
    if pseudo:
        siblings=[x for x in node.parent.children if isinstance(x,Node)]
        if pseudo=='first-child':return siblings[0] is node
        if pseudo=='last-child':return siblings[-1] is node
        raise ValueError('Unsupported email CSS pseudo selector: '+pseudo)
    return True

def matches(node, selector):
    tokens=selector.split()
    if not simple(node,tokens[-1]):return False
    parent=node.parent
    for token in reversed(tokens[:-1]):
        while parent is not None and not simple(parent,token):parent=parent.parent
        if parent is None:return False
        parent=parent.parent
    return True


def base_css(css):
    # Base declarations are inlined; mobile overrides are emitted separately.
    while '@media' in css:
        start=css.index('@media');opening=css.index('{',start);depth=1;end=opening+1
        while depth:
            if css[end]=='{':depth+=1
            elif css[end]=='}':depth-=1
            end+=1
        css=css[:start]+css[end:]
    return css


def element(tag, style='', **attrs):
    if style:attrs['style']=style
    return Node(tag,attrs.items())

def attach(parent, child):
    parent.children.append(child)
    if isinstance(child,Node):child.parent=parent


def table_layout(node, columns, gap=0, widths=None):
    layout='kpi' if node.has('kpis') else 'category' if node.has('catgrid') else None
    if layout:node.attrs['class']=node.attrs.get('class','')+(' nl-kpis' if layout=='kpi' else ' nl-catgrid')
    children=[c for c in node.children if isinstance(c,Node) or str(c).strip()]
    node.tag='table';node.attrs.update(role='presentation',width='100%',cellpadding='0',cellspacing='0',border='0')
    node.children=[]
    node.attrs['style']=node.attrs.get('style','')+';table-layout:fixed'
    for offset in range(0,len(children),columns):
        row=element('tr');attach(node,row)
        for i,child in enumerate(children[offset:offset+columns]):
            if i and gap:
                spacer=element('td',f'width:{gap}px;font-size:0;line-height:0',width=str(gap));
                if layout:spacer.attrs['class']='nl-'+layout+'-gap'
                attach(row,spacer)
            width=widths[i] if widths else None
            cell=element('td','vertical-align:top'+(f';padding-bottom:{gap}px' if offset+columns<len(children) else ''),width=width,valign='top')
            if layout:cell.attrs['class']='nl-'+layout+'-cell'
            if width is None:cell.attrs.pop('width',None)
            attach(row,cell);attach(cell,child)


def for_email(document):
    parser=Parser();parser.feed(document);root=parser.root
    mark(root)
    # QR modules already use explicit email-safe table styles; avoid inflating each cell.
    for n in list(root.walk()):
        if n.attrs.get('data-bot-qr')=='true':
            n.parent.children[n.parent.children.index(n)]=n.html()
    styles=[n for n in root.walk() if n.tag=='style']
    css=base_css(''.join(''.join(n.children) for n in styles))
    rules=[]
    for selectors,body in re.findall(r'([^{}]+)\{([^{}]*)\}',css):
        for selector in selectors.split(','):
            selector=selector.strip()
            specificity=(selector.count('.')+selector.count(':'),len(re.findall(r'(?:^|\s)[a-z]',selector)))
            rules.append((specificity,len(rules),selector,declarations(body)))
    rules.sort(key=lambda r:(r[0],r[1]))
    for n in root.walk():
        values={}
        for _,__,selector,decl in rules:
            if matches(n,selector):values.update(decl)
        values.update(declarations(n.attrs.get('style','')))
        for k in list(values):
            if k.startswith(('grid','flex')) or k in ('gap','align-items','justify-content','place-items','box-sizing','overflow-x'):values.pop(k)
        if values.get('display') in ('grid','flex'):values.pop('display')
        if values:n.attrs['style']=';'.join(f'{k}:{v}' for k,v in values.items())
    # Convert bottom-up so child identities and computed styles are retained.
    for n in reversed(list(root.walk())):
        if n.has('kpis'):table_layout(n,4,10)
        elif n.has('category'):
            # Explicit table/cell height works in Outlook's Word renderer too.
            children=n.children;n.children=[]
            style=n.attrs.pop('style','')
            n.tag='table';n.attrs.update(role='presentation',width='100%',cellpadding='0',cellspacing='0',border='0')
            n.attrs['style']='width:100%;border-collapse:collapse;table-layout:fixed'
            row=element('tr');cell=element('td',style+';height:360px;vertical-align:top',height='360',valign='top')
            cell.attrs['class']='nl-category-content'
            attach(n,row);attach(row,cell)
            for child in children:attach(cell,child)
        elif n.has('catgrid'):table_layout(n,2,14)
        elif n.has('catmetrics'):table_layout(n,2,10)
        elif n.has('brandline'):table_layout(n,3,6,['40%','28%','32%'])
        elif n.has('sectiontitle'):table_layout(n,2,14,['48','auto'])
        elif n.has('cathead'):table_layout(n,2,8,['40%','60%'])
        if n.has('sectionno'):
            n.attrs['style'] += ';display:block;text-align:center;line-height:48px'
        if n.tag=='table':
            n.attrs.setdefault('cellspacing','0');n.attrs.setdefault('cellpadding','0');n.attrs.setdefault('border','0')
        if n.has('shell'):n.attrs['width']='100%'
        if n.attrs.get('data-newsletter-title'):
            n.attrs['style']=n.attrs.get('style','')+';color:#ffffff'
    # Body-level styles are often discarded when a flow embeds an HTML body.
    body=next(n for n in root.walk() if n.tag=='body')
    wrapper=element('table',body.attrs.get('style',''),width='100%',role='presentation',cellpadding='0',cellspacing='0',border='0')
    row=element('tr');cell=element('td');attach(wrapper,row);attach(row,cell)
    for child in body.children:attach(cell,child)
    # Tables in email clients may reset inherited text colors/fonts.
    for n in wrapper.walk():
        values=declarations(n.attrs.get('style',''))
        parent_values=declarations(n.parent.attrs.get('style','')) if n.parent else {}
        for prop in ('color','font-family'):
            if prop not in values and prop in parent_values:values[prop]=parent_values[prop]
        if 'font' not in values:values['font-family']=FONT
        if values:n.attrs['style']=';'.join(f'{k}:{v}' for k,v in values.items())
    # Emit a body fragment with all styling inline. No reliance on head/style/classes.
    return '<style type="text/css">'+RESPONSIVE_CSS+'</style>'+wrapper.html()

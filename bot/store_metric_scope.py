"""Source-aware store GMV weights shared by bulk calculation and SQL consumers.
A TM/DY male row is an independent subtotal; JD male rows sit inside L1.
No brand resolution, routing or product-table logic belongs here.
"""
MAKEUP_LEVELS=('makeup','makeup + fragrance','makeup+fragrance','makeup (exclude fragrance)')

def category_weights(row,platform):
    if platform not in ('TM','DY','JD'):raise ValueError('Unknown store platform')
    a=str(row.get('category_EN_level_1') or '').strip().lower()
    b=str(row.get('category_EN_level_2') or '').strip().lower()
    out=[]
    if a=='skincare':out=[('SKIN',1),('SKINCARE_ALL',1)]
    if b=='male skincare' and (a=='skincare' if platform=='JD' else not a):out.extend([('SKIN',-1),('MEX',1)])
    if a=='hair':out.append(('HAIR',1))
    if a in MAKEUP_LEVELS:out.append(('MAKEUP',1))
    return out

def weight_sql(category,platform,level1='category_EN_level_1',level2='category_EN_level_2'):
    """SUM(gmv * weight), not just a WHERE filter, for subtotal subtraction.
    Identifier arguments are internal SQL expressions from owned templates.
    SKIN excludes male; SKINCARE_ALL explicitly includes it.
    MAKEUP preserves existing store scope (fragrance deduction is a separate policy).
    """
    if platform not in ('TM','DY','JD'):raise ValueError('Unknown store platform')
    a=f"LOWER(TRIM(COALESCE({level1}, '')))";b=f"LOWER(TRIM(COALESCE({level2}, '')))"
    male=f"({b} = 'male skincare' AND {a} = '{'skincare' if platform=='JD' else ''}')"
    skin=f"CASE WHEN {a} = 'skincare' THEN 1 ELSE 0 END"
    mex=f"CASE WHEN {male} THEN 1 ELSE 0 END"
    makeup=f"CASE WHEN {a} IN ("+','.join("'"+v+"'" for v in MAKEUP_LEVELS)+") THEN 1 ELSE 0 END"
    hair=f"CASE WHEN {a} = 'hair' THEN 1 ELSE 0 END"
    values={'SKIN':f'({skin} - {mex})','SKINCARE_ALL':skin,'MEX':mex,'HAIR':hair,'MAKEUP':makeup,'TTL':f'({skin} + {hair} + {makeup})'}
    if category not in values:raise ValueError('Unknown store category')
    return values[category]

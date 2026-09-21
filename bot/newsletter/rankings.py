"""Deterministic rankings. Evidence availability never changes the ranked winner."""
import math

def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)

def rank_pool(rows):
    eligible=[r for r in rows if r.get('brand') and finite(r.get('TTL_gmv_m')) and r['TTL_gmv_m']>0]
    top20=sorted(eligible,key=lambda r:(-r['TTL_gmv_m'],r['brand']))[:20]
    comparable=[r for r in top20 if finite(r.get('TTL_evol_pct')) and r.get('TTL_prior_m',0)>0]
    rising=sorted(comparable,key=lambda r:(-r['TTL_evol_pct'],-r['TTL_gmv_m'],r['brand']))[:5]
    return dict(top_brands=top20[:5],top_rising=rising,
                scope='三平台 Pure Mass 已识别品牌；按当前品类汇总',
                remark='Top Rising：本期GMV前20名中按同比增速排序；上期无有效销售额的品牌不参与增速排名。',
                excluded_no_comparison=[r['brand'] for r in top20 if r not in comparable])

def choose_winners(rankings,prepared):
    picks=[]
    for cat,ranks in rankings.items():
        if not ranks['top_rising']:raise ValueError('No comparable Top Rising brand for '+cat)
        winner=ranks['top_rising'][0]
        mapped=next((r for r in prepared.get(cat,[]) if r['brand']==winner['brand']),None)
        platforms=mapped.get('available_evidence_platforms',[]) if mapped else []
        # Keep original platform priority: positive growth amount, then scale.
        usable=platforms or [p for p in ('TM','DY') if winner.get(p+'_gmv_m',0)>0]
        if not usable:raise ValueError('No current platform amount for '+cat)
        pl=max(usable,key=lambda p:(winner.get(p+'_growth_m') if finite(winner.get(p+'_growth_m')) else -math.inf,winner.get(p+'_gmv_m',0),p))
        picks.append(dict(category=cat,brand=winner['brand'],platform=pl,
                          reason='本期GMV前20名中同比增速排名第一',selection_rule='top_rising_first',
                          mapping_available=bool(mapped),needs_validation=[]))
    return picks

def validate_cards(section):
    """Block a replay/manual payload from detaching the feature from its ranking."""
    if section.get('selection_rule')!='top_rising_first':return
    if {c['category'] for c in section['cards']}!=set(section['rankings']):
        raise ValueError('Ranked category/card mismatch')
    for card in section['cards']:
        rising=section['rankings'][card['category']]['top_rising']
        if not rising or rising[0]['brand']!=card['brand']:
            raise ValueError('Featured brand must equal Top Rising #1')

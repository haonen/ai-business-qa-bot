"""Authoritative section01 wording derived from additive share contributions."""
import math
PLATFORMS={'TM':'天猫','JD':'京东','DY':'抖音'}
CONTRIBUTION='TTL MS change contribution (observed, pp)'
def section01_summary(section):
 rows=section['rows'];total=next(r for r in rows if r['Platform']=='TTL')
 platforms=[r for r in rows if r['Platform'] in PLATFORMS]
 if {r['Platform'] for r in platforms}!=set(PLATFORMS) or len(platforms)!=3:raise ValueError('Require three unique platforms')
 change=total['MS%+/- (observed, pp)']
 if not math.isclose(sum(r[CONTRIBUTION] for r in platforms),change,abs_tol=1e-7):raise ValueError('Non-additive share contributions')
 prefix=f"CPD整体MS% {total['current CPD MS% (observed)']:.2f}%，MS%+/- {change:+.2f}%。"
 if math.isclose(change,0,abs_tol=1e-9):return prefix+'整体份额持平。'
 ranked=sorted(platforms,key=lambda r:r[CONTRIBUTION],reverse=change>0)
 leading=ranked[0][CONTRIBUTION]
 names='、'.join(PLATFORMS[r['Platform']] for r in ranked if math.isclose(r[CONTRIBUTION],leading,abs_tol=1e-9))
 return prefix+names+('对整体份额提升贡献最大。' if change>0 else '对整体份额下降影响最大。')

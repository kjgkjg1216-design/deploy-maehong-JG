# -*- coding: utf-8 -*-
"""파우치 피처 엔지니어링: 재질→종류+두께+구조 분해 후 kNN. 엄밀 검정(짝지은 부트스트랩)."""
import sys, re, math, statistics as stat
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")
import app
d = app._get_material_data(); ALL = d['items']

# ── 재질 토큰 파서 ──
def base_type(tok):
    t = tok
    if 'PP튜브' in t or 'PP' in t and '튜브' in t: return 'PPtube'
    if 'PET' in t or '패트' in t or '페트' in t or '패증착' in t: return 'PET'
    if 'AL' in t or '은박' in t: return 'AL'
    if 'NY' in t: return 'NY'
    if 'LLD' in t: return 'LLDPE'
    if 'CPR' in t: return 'CPR'
    if 'CPP' in t: return 'CPP'
    if t.startswith('PE') or t == 'PE': return 'PE'
    if 'SPP' in t or 'SP' in t: return 'SPP'
    return None

def thickness(tok):
    # =..M, =..도, (..) 제거 후 베이스 뒤 첫 숫자
    s = re.sub(r'=.*$', '', tok)        # 길이/인쇄 제거
    s = re.sub(r'\*.*$', '', s)          # 폭 제거
    s = re.sub(r'\([^)]*\)', '', s)      # 괄호 제거
    nums = re.findall(r'\d+', s)
    return int(nums[0]) if nums else None

STRUCT = ['지퍼스탠드','지퍼삼방','스탠드','삼방','지퍼']
def features(mats):
    types = Counter(); total_t = 0; n_film = 0
    has_al = False; lam_dry = False; struct = set()
    for t in mats:
        for s in STRUCT:
            if s in t: struct.add('지퍼' if '지퍼' in s else s)
        if t in ('DR','DRY') or 'DRY' in t or t=='DR': lam_dry = True
        bt = base_type(t)
        if bt:
            types[bt]+=1; n_film+=1
            if bt=='AL': has_al=True
            th = thickness(t)
            if th and th < 500:  # 비정상 큰 수(길이) 방어
                total_t += th
    return {'types':types, 'total_t':total_t, 'n_film':n_film,
            'has_al':has_al, 'lam_dry':lam_dry, 'struct':struct}

# 사전계산
for r in ALL:
    if r['category']=='파우치':
        r['_feat'] = features(r['materials'])

def feat_sim(fa, fb):
    # 종류 멀티셋 코사인 유사도
    ta, tb = fa['types'], fb['types']
    keys = set(ta)|set(tb)
    dot = sum(ta[k]*tb[k] for k in keys)
    na = math.sqrt(sum(v*v for v in ta.values())); nb = math.sqrt(sum(v*v for v in tb.values()))
    type_sim = dot/(na*nb) if na and nb else 0
    return type_sim

def pred_feat(train, qf, area, *, beta, w_type, w_thick, w_struct, thick_scale):
    qa=max(area,1); lq=math.log(qa)
    tr=[r for r in train if r['category']=='파우치' and r.get('area',0)>0 and r.get('price',0)>0 and '_feat' in r]
    sc=[]
    for r in tr:
        rf=r['_feat']
        ts=feat_sim(qf,rf)
        if ts < 0.3: continue
        # 두께 유사
        dt=abs(qf['total_t']-rf['total_t'])
        thick_sim=math.exp(-dt/thick_scale) if thick_scale else 1.0
        # 구조/AL/라미 일치 보너스
        struct_m = 1.0
        if qf['has_al']!=rf['has_al']: struct_m*=0.5
        if qf['struct']!=rf['struct']: struct_m*=0.8
        ld=abs(lq-math.log(r['area']))
        area_sim=math.exp(-1.8*ld)
        w=(ts**w_type)*(thick_sim**w_thick)*(struct_m**w_struct)*area_sim
        if w>0.003:
            adj=r['price']*((qa/r['area'])**beta)
            sc.append((w,math.log(adj)))
    if not sc: return None
    sc.sort(key=lambda x:-x[0]); top=sc[:12]; tw=sum(s for s,_ in top)
    return math.exp(sum(s*v for s,v in top)/tw)

# 현행 토큰 kNN (비교 기준, β=0.65)
def pred_cur(train, sel, area):
    qa=max(area,1); lq=math.log(qa)
    tr=[r for r in train if r['category']=='파우치' and r.get('area',0)>0 and r.get('price',0)>0]
    exact=[r for r in tr if set(r['materials'])==sel]
    if exact:
        if len(exact)==1: return exact[0]['price']*((qa/exact[0]['area'])**0.65)
        la=[math.log(r['area']) for r in exact]; lp=[math.log(r['price']) for r in exact]
        n=len(la); mla=sum(la)/n; mlp=sum(lp)/n
        num=sum((x-mla)*(y-mlp) for x,y in zip(la,lp)); den=sum((x-mla)**2 for x in la)
        b=max(0.4,min(1.0,num/den)) if den>0 else 0.65
        return math.exp(mlp+b*(lq-mla))
    sc=[]
    for r in tr:
        rs=set(r['materials']); u=sel|rs; j=len(sel&rs)/len(u) if u else 0
        if j<0.15: continue
        ld=abs(lq-math.log(r['area'])); w=(j**1.2)*math.exp(-1.8*ld)
        if w>0.005: sc.append((w, math.log(r['price']*((qa/r['area'])**0.65))))
    if not sc: return None
    sc.sort(key=lambda x:-x[0]); top=sc[:12]; tw=sum(s for s,_ in top)
    return math.exp(sum(s*v for s,v in top)/tw)

idx=[i for i,r in enumerate(ALL) if r['category']=='파우치' and r.get('area',0)>0 and r.get('price',0)>0]
def loocv(predict, feat=False, **kw):
    ap=[]
    for i in idx:
        r=ALL[i]; train=[ALL[j] for j in idx if j!=i]
        if feat:
            est=predict(train, r['_feat'], r['area'], **kw)
        else:
            est=predict(train, set(r['materials']), r['area'])
        if est and est>0: ap.append(abs(est-r['price'])/r['price']*100)
    return ap
def summ(ap): return (sum(ap)/len(ap), stat.median(ap), sum(1 for a in ap if a<=20)/len(ap)*100)

print("=== 현행 토큰 kNN (β=0.65) ===")
ap_cur=loocv(pred_cur)
m=summ(ap_cur); print(f"  MAPE={m[0]:.2f} med={m[1]:.2f} ±20%={m[2]:.1f}")

print("\n=== 피처 kNN 그리드 (종류코사인+두께+구조) ===")
best=None
for w_type in (1.0,1.5,2.0):
    for w_thick in (0.5,1.0,1.5):
        for thick_scale in (50,100,200):
            for w_struct in (1.0,):
                ap=loocv(pred_feat, feat=True, beta=0.65, w_type=w_type, w_thick=w_thick, w_struct=w_struct, thick_scale=thick_scale)
                if len(ap)<len(idx)*0.8: continue
                mm=summ(ap)
                if best is None or mm[0]<best[0][0]: best=(mm,w_type,w_thick,thick_scale,ap)
print(f"  BEST w_type={best[1]} w_thick={best[2]} thick_scale={best[3]} → MAPE={best[0][0]:.2f} med={best[0][1]:.2f} ±20%={best[0][2]:.1f} (n={len(best[4])})")

# 짝지은 부트스트랩: 현행 vs 피처(best)
print("\n=== 짝지은 부트스트랩: 현행 vs 피처 ===")
A=[]; B=[]
for i in idx:
    r=ALL[i]; train=[ALL[j] for j in idx if j!=i]
    ea=pred_cur(train,set(r['materials']),r['area'])
    eb=pred_feat(train,r['_feat'],r['area'],beta=0.65,w_type=best[1],w_thick=best[2],w_struct=1.0,thick_scale=best[3])
    if ea and ea>0 and eb and eb>0:
        A.append(abs(ea-r['price'])/r['price']*100); B.append(abs(eb-r['price'])/r['price']*100)
n=len(A); s=999; diffs=[]
for _ in range(4000):
    sa=sb=0.0
    for _ in range(n):
        s=(1103515245*s+12345)&0x7FFFFFFF; k=s%n; sa+=A[k]; sb+=B[k]
    diffs.append((sb-sa)/n)
diffs.sort(); base=sum(B)/n-sum(A)/n
print(f"  ΔMAPE(피처-현행)={base:+.2f}%p [95%CI {diffs[100]:+.2f}~{diffs[3900]:+.2f}] P(피처개선)={sum(1 for x in diffs if x<0)/4000*100:.1f}% (n={n})")

# ── 앙상블: 현행 kNN + 피처 kNN 로그공간 결합 ──
print("\n=== 앙상블 (현행·피처 로그평균) vs 현행 ===")
def pred_ens(train, sel, feat, area, wt):
    a=pred_cur(train,sel,area)
    b=pred_feat(train,feat,area,beta=0.65,w_type=2.0,w_thick=0.5,w_struct=1.0,thick_scale=100)
    if a and a>0 and b and b>0: return math.exp((1-wt)*math.log(a)+wt*math.log(b))
    return a or b
for wt in (0.3,0.4,0.5,0.6):
    ap=[]
    for i in idx:
        r=ALL[i]; train=[ALL[j] for j in idx if j!=i]
        e=pred_ens(train,set(r['materials']),r['_feat'],r['area'],wt)
        if e and e>0: ap.append(abs(e-r['price'])/r['price']*100)
    mm=summ(ap); print(f"  wt={wt}: MAPE={mm[0]:.2f} med={mm[1]:.2f} ±20%={mm[2]:.1f}")
# 짝지은: 현행 vs 앙상블(wt=0.5)
A=[];B=[]
for i in idx:
    r=ALL[i]; train=[ALL[j] for j in idx if j!=i]
    ea=pred_cur(train,set(r['materials']),r['area'])
    eb=pred_ens(train,set(r['materials']),r['_feat'],r['area'],0.5)
    if ea and ea>0 and eb and eb>0:
        A.append(abs(ea-r['price'])/r['price']*100); B.append(abs(eb-r['price'])/r['price']*100)
n=len(A); s=2024; diffs=[]
for _ in range(4000):
    sa=sb=0.0
    for _ in range(n):
        s=(1103515245*s+12345)&0x7FFFFFFF; k=s%n; sa+=A[k]; sb+=B[k]
    diffs.append((sb-sa)/n)
diffs.sort(); base=sum(B)/n-sum(A)/n
print(f"  앙상블(wt=.5) vs 현행: ΔMAPE={base:+.2f}%p [CI {diffs[100]:+.2f}~{diffs[3900]:+.2f}] P(개선)={sum(1 for x in diffs if x<0)/4000*100:.1f}%")

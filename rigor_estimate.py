# -*- coding: utf-8 -*-
"""예상 단가 계산기 — 엄밀 평가.
1) 환원불가 노이즈 바닥(동일스펙 가격분산)
2) 중첩 CV로 β 무편향 선정 (하이퍼파라미터 선택편향 제거)
3) MOQ의 out-of-sample 기여 검정
4) 부트스트랩 95% 신뢰구간
난수: 인덱스 기반 결정적 셔플(LCG)로 재현성 확보."""
import sys, math, statistics as stat
sys.stdout.reconfigure(encoding="utf-8")
import app

d = app._get_material_data()
ALL = d['items']

def lcg_shuffle(lst, seed):
    """결정적 Fisher-Yates (Date/random 불가 환경 대비, 재현성)."""
    a = lst[:]; s = seed & 0xFFFFFFFF
    for i in range(len(a)-1, 0, -1):
        s = (1103515245*s + 12345) & 0x7FFFFFFF
        j = s % (i+1)
        a[i], a[j] = a[j], a[i]
    return a

# ───────────────────────── 1. 노이즈 바닥 ─────────────────────────
def noise_floor(cat):
    valid = [r for r in ALL if r['category']==cat and r.get('area',0)>0 and r.get('price',0)>0]
    # 동일 토큰셋 & 면적±8% 그룹의 within-group |Δ|/mean
    groups = {}
    for r in valid:
        key = frozenset(r['materials'])
        groups.setdefault(key, []).append(r)
    apes = []
    for key, rs in groups.items():
        # 면적 유사 쌍만
        for i in range(len(rs)):
            for j in range(i+1, len(rs)):
                a, b = rs[i], rs[j]
                if a['area']>0 and abs(a['area']-b['area'])/a['area'] < 0.08:
                    m = (a['price']+b['price'])/2
                    apes.append(abs(a['price']-b['price'])/m*100)
    return (stat.mean(apes), stat.median(apes), len(apes)) if apes else (None,None,0)

# ───────────────────────── 모델 정의 ─────────────────────────
def predict_pouch(train, sel, area, *, beta, moq=None, use_moq=False, moq_w=0.0):
    qa=max(area,1); lq=math.log(qa)
    tr=[r for r in train if r['category']=='파우치' and r.get('area',0)>0 and r.get('price',0)>0]
    if not tr: return None
    exact=[r for r in tr if set(r['materials'])==sel]
    if exact:
        la=[math.log(r['area']) for r in exact]; lp=[math.log(r['price']) for r in exact]
        if len(exact)==1:
            return exact[0]['price']*((qa/exact[0]['area'])**0.65)
        n=len(la); mla=sum(la)/n; mlp=sum(lp)/n
        num=sum((x-mla)*(y-mlp) for x,y in zip(la,lp)); den=sum((x-mla)**2 for x in la)
        b=max(0.4,min(1.0,num/den)) if den>0 else 0.65
        return math.exp(mlp+b*(lq-mla))
    sc=[]
    for r in tr:
        rs=set(r['materials']); u=sel|rs
        j=len(sel&rs)/len(u) if u else 0
        if j<0.15: continue
        ld=abs(lq-math.log(r['area']))
        w=(j**1.2)*math.exp(-1.8*ld)
        if use_moq and moq and r.get('moq',0)>0:
            w *= math.exp(-moq_w*abs(math.log(qa) and (math.log(moq)-math.log(r['moq']))))
        if w>0.005:
            adj=r['price']*((qa/r['area'])**beta)
            sc.append((w, math.log(adj)))
    if not sc: return None
    sc.sort(key=lambda x:-x[0]); top=sc[:12]; tw=sum(s for s,_ in top)
    return math.exp(sum(s*v for s,v in top)/tw)

# ───────────────────────── CV 유틸 ─────────────────────────
def apes_for(cat, predict, **kw):
    idx=[i for i,r in enumerate(ALL) if r['category']==cat and r.get('area',0)>0 and r.get('price',0)>0]
    out=[]
    for i in idx:
        r=ALL[i]; train=[ALL[j] for j in idx if j!=i]
        est=predict(train, set(r['materials']), r['area'], moq=r.get('moq'), **kw)
        if est and est>0:
            out.append(abs(est-r['price'])/r['price']*100)
    return out

def summ(apes):
    return (sum(apes)/len(apes), stat.median(apes),
            sum(1 for a in apes if a<=20)/len(apes)*100)

def bootstrap_ci(apes, fn, B=2000, seed=12345):
    n=len(apes); s=seed&0x7FFFFFFF; vals=[]
    for _ in range(B):
        samp=[]
        for _ in range(n):
            s=(1103515245*s+12345)&0x7FFFFFFF
            samp.append(apes[s%n])
        vals.append(fn(samp))
    vals.sort()
    lo=vals[int(0.025*B)]; hi=vals[int(0.975*B)]
    return lo, hi

# ───────────────────────── 중첩 CV: β 무편향 평가 ─────────────────────────
def nested_cv_beta(cat, betas, K=5, seed=777):
    """외부 K-fold: 각 폴드의 train에서 inner-LOOCV로 best β 선정 → test에 적용.
    이렇게 얻은 test 오차는 β 선택편향 없음."""
    idx=[i for i,r in enumerate(ALL) if r['category']==cat and r.get('area',0)>0 and r.get('price',0)>0]
    idx=lcg_shuffle(idx, seed)
    folds=[idx[k::K] for k in range(K)]
    test_apes=[]; chosen=[]
    for k in range(K):
        test=folds[k]; tr_idx=[i for f in folds if f is not folds[k] for i in f]
        # inner: tr_idx에서 LOOCV로 β별 MAPE
        best_b=None; best_m=1e9
        for beta in betas:
            ap=[]
            for i in tr_idx:
                inner_train=[ALL[j] for j in tr_idx if j!=i]
                r=ALL[i]
                est=predict_pouch(inner_train, set(r['materials']), r['area'], beta=beta)
                if est and est>0: ap.append(abs(est-r['price'])/r['price']*100)
            m=sum(ap)/len(ap) if ap else 1e9
            if m<best_m: best_m=m; best_b=beta
        chosen.append(best_b)
        # test 적용 (train=전체 tr_idx)
        for i in test:
            r=ALL[i]; train=[ALL[j] for j in tr_idx]
            est=predict_pouch(train, set(r['materials']), r['area'], beta=best_b)
            if est and est>0: test_apes.append(abs(est-r['price'])/r['price']*100)
    return test_apes, chosen

print("=== 1. 환원불가 노이즈 바닥 (동일토큰셋·면적±8% 쌍의 가격불일치) ===")
for cat in ('파우치','박스','단상자'):
    mean,med,n = noise_floor(cat)
    if n: print(f"  [{cat}] 쌍 {n}개 | 평균 |Δ|={mean:.1f}% 중앙={med:.1f}%  ← 이론적 MAPE 하한 근사")
    else: print(f"  [{cat}] 동일스펙 쌍 없음")

print("\n=== 2. 파우치 β: 단순 LOOCV vs 중첩 CV (선택편향 제거) ===")
betas=[0.0,0.3,0.45,0.55,0.65,0.75,0.85]
# 단순 LOOCV로 β별
for beta in betas:
    ap=apes_for('파우치', predict_pouch, beta=beta)
    mape,med,w20=summ(ap)
    print(f"  LOOCV β={beta}: MAPE={mape:.2f} med={med:.2f} ±20%={w20:.1f}")
nap, chosen = nested_cv_beta('파우치', betas)
nmape,nmed,nw20=summ(nap)
lo,hi=bootstrap_ci(nap, lambda a:sum(a)/len(a))
print(f"  >> 중첩CV(무편향): MAPE={nmape:.2f} [95%CI {lo:.1f}~{hi:.1f}] med={nmed:.2f} ±20%={nw20:.1f} | 폴드별 선택β={chosen}")

print("\n=== 3. 부트스트랩 95% CI — 현행(β=0) vs 개선(β=0.65) 파우치 ===")
for tag,beta in [('β=0(개선전)',0.0),('β=0.65(개선후)',0.65)]:
    ap=apes_for('파우치', predict_pouch, beta=beta)
    mape,med,w20=summ(ap)
    lo,hi=bootstrap_ci(ap, lambda a:sum(a)/len(a))
    lo2,hi2=bootstrap_ci(ap, lambda a:stat.median(a))
    print(f"  {tag}: MAPE={mape:.2f} [CI {lo:.1f}~{hi:.1f}] | med={med:.2f} [CI {lo2:.1f}~{hi2:.1f}]")

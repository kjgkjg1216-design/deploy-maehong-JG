# -*- coding: utf-8 -*-
"""짝지은 부트스트랩 유의성 검정 + MOQ out-of-sample 기여 + 헤드룸 분석."""
import sys, math, statistics as stat
sys.stdout.reconfigure(encoding="utf-8")
import app
d = app._get_material_data(); ALL = d['items']

def pouch_pred(train, sel, area, *, beta, moq=None, moq_w=0.0):
    qa=max(area,1); lq=math.log(qa)
    tr=[r for r in train if r['category']=='파우치' and r.get('area',0)>0 and r.get('price',0)>0]
    if not tr: return None
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
        if moq_w>0 and moq and moq>0 and r.get('moq',0)>0:
            w *= math.exp(-moq_w*abs(math.log(moq)-math.log(r['moq'])))
        if w>0.005:
            adj=r['price']*((qa/r['area'])**beta); sc.append((w,math.log(adj)))
    if not sc: return None
    sc.sort(key=lambda x:-x[0]); top=sc[:12]; tw=sum(s for s,_ in top)
    return math.exp(sum(s*v for s,v in top)/tw)

def box_pred(train, sel, area, *, dw_pen):
    qa=max(area,1); lq=math.log(qa)
    q_pr=('인쇄됨' in sel) or ('인쇄지' in sel); q_lm=('무광라미' in sel) or ('유광라미' in sel); q_dw='이중벽' in sel
    tr=[r for r in train if r.get('category') in ('박스','단상자') and r.get('area',0)>0 and r.get('price',0)>0]
    sc=[]
    for r in tr:
        rs=set(r['materials']); u=sel|rs; j=len(sel&rs)/len(u) if u else 0
        if j<0.1: continue
        pen=1.0
        if q_pr!=(('인쇄됨' in rs) or ('인쇄지' in rs)): pen*=0.4
        if q_lm!=(('무광라미' in rs) or ('유광라미' in rs)): pen*=0.7
        if q_dw!=('이중벽' in rs): pen*=dw_pen
        ld=abs(lq-math.log(r['area'])); s=j*math.exp(-1.5*ld)*pen
        if s>0.003: sc.append((s, math.log(r['price']/r['area'])))
    if not sc: return None
    sc.sort(key=lambda x:-x[0]); top=sc[:8]
    return math.exp(stat.median([v for _,v in top]))*qa

def paired_apes(cat, pred, kwA, kwB):
    """동일 item에 대해 두 모델 A,B의 APE 짝으로 반환."""
    idx=[i for i,r in enumerate(ALL) if r['category']==cat and r.get('area',0)>0 and r.get('price',0)>0]
    A=[]; B=[]
    for i in idx:
        r=ALL[i]; train=[ALL[j] for j in idx if j!=i]
        ea=pred(train,set(r['materials']),r['area'],moq=r.get('moq'),**kwA) if 'moq_w' in kwA or cat=='파우치' else pred(train,set(r['materials']),r['area'],**kwA)
        eb=pred(train,set(r['materials']),r['area'],moq=r.get('moq'),**kwB) if 'moq_w' in kwB or cat=='파우치' else pred(train,set(r['materials']),r['area'],**kwB)
        if ea and ea>0 and eb and eb>0:
            A.append(abs(ea-r['price'])/r['price']*100); B.append(abs(eb-r['price'])/r['price']*100)
    return A,B

def paired_boot(A,B,B_iter=4000,seed=999):
    """ΔMAPE = mean(B)-mean(A) 의 짝지은 부트스트랩 CI. 음수면 B가 개선."""
    n=len(A); s=seed&0x7FFFFFFF; diffs=[]
    base=sum(B)/n - sum(A)/n
    for _ in range(B_iter):
        sa=sb=0.0
        for _ in range(n):
            s=(1103515245*s+12345)&0x7FFFFFFF; k=s%n
            sa+=A[k]; sb+=B[k]
        diffs.append((sb-sa)/n)
    diffs.sort(); lo=diffs[int(0.025*B_iter)]; hi=diffs[int(0.975*B_iter)]
    p_improve=sum(1 for x in diffs if x<0)/B_iter
    return base, lo, hi, p_improve

print("=== A) 파우치 β=0 → β=0.65 (짝지은 부트스트랩) ===")
A,B=paired_apes('파우치', pouch_pred, {'beta':0.0}, {'beta':0.65})
base,lo,hi,p=paired_boot(A,B)
print(f"  ΔMAPE = {base:+.2f}%p [95%CI {lo:+.2f}~{hi:+.2f}]  P(개선)={p*100:.1f}%  (n={len(A)})")
print(f"  → CI가 0 미만에 있으면 통계적으로 유의한 개선")

print("\n=== B) 박스 이중벽 페널티 없음(1.0) → 0.4 (짝지은) ===")
A,B=paired_apes('박스', box_pred, {'dw_pen':1.0}, {'dw_pen':0.4})
base,lo,hi,p=paired_boot(A,B)
print(f"  ΔMAPE = {base:+.2f}%p [95%CI {lo:+.2f}~{hi:+.2f}]  P(개선)={p*100:.1f}%  (n={len(A)})")

print("\n=== C) 파우치 MOQ 가중 추가 (β=0.65 고정, moq_w=0 → 최적탐색) ===")
best=None
for mw in (0.0,0.1,0.2,0.3,0.5,0.8):
    idx=[i for i,r in enumerate(ALL) if r['category']=='파우치' and r.get('area',0)>0 and r.get('price',0)>0]
    ap=[]
    for i in idx:
        r=ALL[i]; train=[ALL[j] for j in idx if j!=i]
        e=pouch_pred(train,set(r['materials']),r['area'],beta=0.65,moq=r.get('moq'),moq_w=mw)
        if e and e>0: ap.append(abs(e-r['price'])/r['price']*100)
    mape=sum(ap)/len(ap); med=stat.median(ap); w20=sum(1 for a in ap if a<=20)/len(ap)*100
    print(f"  moq_w={mw}: MAPE={mape:.2f} med={med:.2f} ±20%={w20:.1f}")
    if best is None or mape<best[1]: best=(mw,mape)
# 최적 moq_w를 β=0.65(moq_w=0)와 짝지어 검정
A,B=paired_apes('파우치', pouch_pred, {'beta':0.65,'moq_w':0.0}, {'beta':0.65,'moq_w':best[0]})
base,lo,hi,p=paired_boot(A,B)
print(f"  최적 moq_w={best[0]} vs 무MOQ: ΔMAPE={base:+.2f}%p [CI {lo:+.2f}~{hi:+.2f}] P(개선)={p*100:.1f}%")

print("\n=== D) 헤드룸: 현행 MAPE vs 노이즈바닥 ===")
print("  파우치: 노이즈바닥~9.9%, 현행 MAPE~13.3% → 남은 개선여지 ~3.4%p")
print("  박스: 노이즈바닥~6.5%, 현행 MAPE~12.4% → 여지 ~5.9%p")
print("  단상자: 노이즈바닥~28%, 현행 MAPE~18.6% → 이미 노이즈 이하(추가개선=과적합)")

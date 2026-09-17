# -*- coding: utf-8 -*-
"""단가 시점(수령일) 효과 검정 — 오래된 단가가 체계적으로 낮은지, 보정하면 예측이 좋아지는지.
(1) LOO 잔차(log 예측 − log 실제) vs 수령일 회귀 → 연간 드리프트 추정
(2) 후보: 이웃 단가를 질의 시점으로 드리프트 보정 / 최근 이웃 가중 → 짝지은 부트스트랩 + 중첩CV"""
import sys, math, statistics as stat, random, datetime as dt
sys.stdout.reconfigure(encoding="utf-8")
import app
from pcalc_tune import pouch_pred, box_pred, POUCH_BASE, BOX_BASE, loo_apes, paired_boot, mape, idx_of, ALL, SPEC_RAW

# 수령일 조인 (CSV 직접 로드분)
dates = {str(r['품번']).strip(): str(r['수령일']).strip()[:10] for _, r in SPEC_RAW.iterrows()}
T0 = dt.date(2025, 1, 1)
for r in ALL:
    s = dates.get(r.get('code', ''), '')
    try:
        r['t'] = (dt.date.fromisoformat(s) - T0).days / 365.25
    except Exception:
        r['t'] = None

for cat, pred, BASE in (('파우치', pouch_pred, POUCH_BASE), ('박스', box_pred, BOX_BASE)):
    idx = [i for i in idx_of(cat) if ALL[i].get('t') is not None]
    print(f"\n{'=' * 70}\n[{cat}] n={len(idx)}  수령일 분포: " + str(sorted(stat.quantiles([ALL[i]['t'] for i in idx], n=4))))
    # (1) 잔차 vs 시점
    res = []
    for i in idx:
        train = [ALL[j] for j in range(len(ALL)) if j != i]
        e = pred(train, ALL[i], BASE)
        if e and e > 0:
            res.append((ALL[i]['t'], math.log(e) - math.log(ALL[i]['price'])))
    xs = [a for a, _ in res]; ys = [b for _, b in res]
    mx = sum(xs) / len(xs); my = sum(ys) / len(ys)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    r = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    print(f"  LOO 잔차(log예측−log실제) vs 수령일: 기울기 {b:+.3f}/년 (상관 {r:+.2f}) → "
          f"{'오래된 항목일수록 과소예측(=최근 단가↑)' if b < 0 else '오래된 항목일수록 과대예측(=최근 단가↓)'}")
    # 연도별 평균 잔차
    by = {}
    for t, e in res:
        y = 2025 + int(t) if t >= 0 else 2024
        by.setdefault(y, []).append(e)
    for y in sorted(by):
        print(f"    수령 {y}: n={len(by[y]):3d} 평균잔차 {sum(by[y]) / len(by[y]) * 100:+.1f}% (양수=과대예측)")

    # (2) 후보 모델: 이웃 단가 드리프트 보정 exp(g·(t_q − t_n)) / 최근성 가중 exp(−λ|Δt|)
    def make_pred(g=0.0, lam=0.0):
        def f(train, q, P):
            tq = q.get('t')
            tr2 = []
            for n in train:
                n2 = dict(n)
                if tq is not None and n.get('t') is not None:
                    n2['price'] = n['price'] * math.exp(g * (tq - n['t']))
                    n2['_w'] = math.exp(-lam * abs(tq - n['t']))
                tr2.append(n2)
            if lam > 0:
                # 최근성 가중은 kNN 점수에 곱해야 하므로 vendor 부스트 자리를 빌려 근사: price 보정만 적용 후 점수는 원본
                pass
            return pred(tr2, q, P)
        return f
    baseA = loo_apes(cat, pred, BASE, idx)
    print(f"  현행 LOO MAPE={mape(baseA):.2f}%")
    cands = []
    for g in (0.05, 0.10, 0.15, 0.20, 0.30):
        A = loo_apes(cat, make_pred(g=g), BASE, idx)
        base, lo, hi, p, n = paired_boot(baseA, A)
        cands.append((p, base, lo, hi, f"drift={g:.2f}/yr", mape(A)))
    cands.sort(key=lambda x: -x[0])
    for p, base, lo, hi, name, m in cands:
        print(f"    {name:14s} MAPE={m:.2f}% Δ={base:+.2f}%p [{lo:+.2f}~{hi:+.2f}] P={p * 100:5.1f}%{' ◀ 유의' if p >= 0.95 else ''}")
    # 중첩 5-fold: 내부 LOO로 g 선택
    folds = [idx[i::5] for i in range(5)]
    ob, os_ = {}, {}
    picks = []
    for f in folds:
        tr_idx = [i for i in idx if i not in f]; pool = [j for j in range(len(ALL)) if j not in f]
        best = (mape(loo_apes(cat, pred, BASE, tr_idx, pool)), 0.0)
        for g in (0.05, 0.10, 0.15, 0.20, 0.30):
            m = mape(loo_apes(cat, make_pred(g=g), BASE, tr_idx, pool))
            if m < best[0]:
                best = (m, g)
        picks.append(best[1])
        ob.update(loo_apes(cat, pred, BASE, f, pool + f))
        os_.update(loo_apes(cat, make_pred(g=best[1]), BASE, f, pool + f))
    base, lo, hi, p, n = paired_boot(ob, os_)
    print(f"  중첩CV: 현행 {mape(ob):.2f}% → 드리프트선택 {mape(os_):.2f}% (Δ={base:+.2f}%p [{lo:+.2f}~{hi:+.2f}] P={p * 100:.1f}%) 폴드별 g={picks}")

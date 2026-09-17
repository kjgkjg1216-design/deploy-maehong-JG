# -*- coding: utf-8 -*-
"""단가계산기 하이퍼파라미터 재조정 실험 (데이터 갱신 후).
- 파우치 T2 kNN / 박스 단위면적 kNN의 파라미터를 현행 기준에서 한 개씩 바꿔
  (1) LOO 짝지은 부트스트랩 P(개선)  (2) 중첩 5-fold CV(내부 LOO 선택)로 선택편향 없는 이득 확인.
- 채택 기준: 짝지은 부트스트랩 P(개선) ≥ 95% 이고 중첩CV에서도 개선.
실행: "<PY>" pcalc_tune.py [quick]"""
import sys, math, statistics as stat, itertools, time
sys.stdout.reconfigure(encoding="utf-8")
import app
d = app._get_material_data(); ALL = d['items']
QUICK = len(sys.argv) > 1 and sys.argv[1] == 'quick'

# '구분'(고객채널)·'수령일' 보강 — app.MONDAY_DF는 usecols 필터(_mon_keep)로 이 컬럼이 없음 → CSV 직접 로드
import glob as _glob, pandas as _pd
_mf = sorted(_glob.glob(f'{app.DATA_DIR}/*_monday.csv'))[-1]
SPEC_RAW = _pd.read_csv(_mf, encoding='utf-8-sig', dtype=str, low_memory=False,
                        usecols=lambda c: c in {'보드명', '품번', '구분', '수령일', '수정일', '생성일'}).fillna('')
SPEC_RAW = SPEC_RAW[SPEC_RAW['보드명'] == '부자재 규격']
_chan = {str(r['품번']).strip(): str(r['구분']).strip() for _, r in SPEC_RAW.iterrows()}
for r in ALL:
    r['chan'] = _chan.get(r.get('code', ''), '')

POUCH_BASE = dict(beta=0.65, jmin=0.15, decay=1.8, mexp=1.2, k=12, vb=1.2, cb=1.0, t1beta=0.65)
BOX_BASE = dict(jmin=0.1, decay=1.5, k=8, ppen=0.4, lpen=0.7, agg='median', vb=1.0)


def pouch_pred(train, q, P):
    qa = max(q['area'], 1); lq = math.log(qa); sel = set(q['materials'])
    tr = [r for r in train if r['category'] == '파우치' and r.get('area', 0) > 0 and r.get('price', 0) > 0]
    exact = [r for r in tr if set(r['materials']) == sel]
    if exact:
        if len(exact) == 1:
            return exact[0]['price'] * ((qa / exact[0]['area']) ** P['t1beta'])
        la = [math.log(r['area']) for r in exact]; lp = [math.log(r['price']) for r in exact]
        n = len(la); mla = sum(la) / n; mlp = sum(lp) / n
        num = sum((x - mla) * (y - mlp) for x, y in zip(la, lp)); den = sum((x - mla) ** 2 for x in la)
        b = max(0.4, min(1.0, num / den)) if den > 0 else 0.65
        return math.exp(mlp + b * (lq - mla))
    sc = []
    for r in tr:
        rs = set(r['materials']); u = sel | rs; j = len(sel & rs) / len(u) if u else 0
        if j < P['jmin']:
            continue
        ld = abs(lq - math.log(r['area']))
        w = (j ** P['mexp']) * math.exp(-P['decay'] * ld)
        if q.get('vendor') and r.get('vendor') == q.get('vendor'):
            w *= P['vb']
        if q.get('chan') and r.get('chan') == q.get('chan'):
            w *= P['cb']
        if w > 0.005:
            sc.append((w, math.log(r['price'] * ((qa / r['area']) ** P['beta']))))
    if sc:
        sc.sort(key=lambda x: -x[0]); top = sc[:P['k']]; tw = sum(s for s, _ in top)
        return math.exp(sum(s * v for s, v in top) / tw)
    sims = []
    for r in tr:
        ld = abs(lq - math.log(r['area'])); s = math.exp(-2.0 * ld)
        if s > 0.005:
            sims.append((s, math.log(r['price'] * ((qa / r['area']) ** P['beta']))))
    if not sims:
        return None
    sims.sort(key=lambda x: -x[0]); top = sims[:8]; tw = sum(s for s, _ in top)
    return math.exp(sum(s * v for s, v in top) / tw)


def box_pred(train, q, P):
    qa = max(q['area'], 1); lq = math.log(qa); sel = set(q['materials'])
    q_pr = ('인쇄됨' in sel) or ('인쇄지' in sel); q_lm = ('무광라미' in sel) or ('유광라미' in sel)
    tr = [r for r in train if r.get('category') in ('박스', '단상자') and r.get('area', 0) > 0 and r.get('price', 0) > 0]
    sc = []
    for r in tr:
        rs = set(r['materials']); u = sel | rs; j = len(sel & rs) / len(u) if u else 0
        if j < P['jmin']:
            continue
        pen = 1.0
        if q_pr != (('인쇄됨' in rs) or ('인쇄지' in rs)): pen *= P['ppen']
        if q_lm != (('무광라미' in rs) or ('유광라미' in rs)): pen *= P['lpen']
        if q.get('vendor') and r.get('vendor') == q.get('vendor'):
            pen *= P['vb']
        ld = abs(lq - math.log(r['area'])); s = j * math.exp(-P['decay'] * ld) * pen
        if s > 0.003:
            sc.append((s, math.log(r['price'] / r['area'])))
    if not sc:
        return None
    sc.sort(key=lambda x: -x[0]); top = sc[:P['k']]
    if P['agg'] == 'median':
        lu = stat.median([v for _, v in top])
    else:
        tw = sum(s for s, _ in top); lu = sum(s * v for s, v in top) / tw
    return math.exp(lu) * qa


def idx_of(cat):
    cats = ('박스',) if cat == '박스' else (cat,)
    return [i for i, r in enumerate(ALL) if r['category'] in cats and r.get('area', 0) > 0 and r.get('price', 0) > 0]


def loo_apes(cat, pred, P, idx=None, pool=None):
    """idx: 평가 대상, pool: train 후보(기본 전체). 반환 {i: ape}"""
    idx = idx if idx is not None else idx_of(cat)
    pool = pool if pool is not None else list(range(len(ALL)))
    out = {}
    for i in idx:
        train = [ALL[j] for j in pool if j != i]
        e = pred(train, ALL[i], P)
        if e and e > 0:
            out[i] = abs(e - ALL[i]['price']) / ALL[i]['price'] * 100
    return out


def paired_boot(A, B, n_iter=3000, seed=999):
    import random
    keys = sorted(set(A) & set(B)); a = [A[k] for k in keys]; b = [B[k] for k in keys]
    n = len(a); rng = random.Random(seed); diffs = []
    base = sum(b) / n - sum(a) / n
    dd = [bi - ai for ai, bi in zip(a, b)]
    for _ in range(n_iter):
        diffs.append(sum(rng.choice(dd) for _ in range(n)) / n)
    diffs.sort()
    return base, diffs[int(0.025 * n_iter)], diffs[int(0.975 * n_iter)], sum(1 for x in diffs if x < 0) / n_iter, n


def mape(A):
    v = list(A.values()); return sum(v) / len(v) if v else float('nan')


GRID = {
    '파우치': (pouch_pred, POUCH_BASE, {
        'beta': [0.55, 0.75], 'jmin': [0.10, 0.20, 0.25], 'decay': [1.2, 2.4, 3.0], 'mexp': [1.0, 1.6, 2.0],
        'k': [6, 8, 20], 'vb': [1.0, 1.5, 2.0], 'cb': [1.2, 1.5], 't1beta': [0.55, 0.75],
    }),
    '박스': (box_pred, BOX_BASE, {
        'jmin': [0.05, 0.2], 'decay': [1.0, 2.0, 2.5], 'k': [5, 12], 'ppen': [0.3, 0.6], 'lpen': [0.5, 0.9],
        'agg': ['wmean'], 'vb': [1.3, 1.6],
    }),
}

for cat, (pred, BASE, grid) in (GRID.items() if __name__ == '__main__' else ()):
    t0 = time.time()
    print(f"\n{'=' * 70}\n[{cat}] 현행 {BASE}")
    baseA = loo_apes(cat, pred, BASE)
    print(f"  현행 LOO MAPE={mape(baseA):.2f}% (n={len(baseA)})")
    cands = []
    for key, vals in grid.items():
        for v in vals:
            P = dict(BASE); P[key] = v
            A = loo_apes(cat, pred, P)
            base, lo, hi, p, n = paired_boot(baseA, A)
            cands.append((p, base, lo, hi, key, v, n))
    cands.sort(key=lambda x: (-x[0], x[1]))
    print("  단일 파라미터 변경 — ΔMAPE(음수=개선) [95%CI] P(개선)")
    for p, base, lo, hi, key, v, n in cands:
        mark = ' ◀ 유의' if p >= 0.95 else ''
        print(f"    {key}={v!s:6s} Δ={base:+.2f}%p [{lo:+.2f}~{hi:+.2f}] P={p * 100:5.1f}% n={n}{mark}")

    # 중첩 5-fold CV: 내부(LOO on train fold)에서 단일변경 후보 중 최적 선택 → 외부 fold 평가
    if not QUICK:
        idx = idx_of(cat); folds = [idx[i::5] for i in range(5)]
        outer_base, outer_sel, picks = {}, {}, []
        for f in folds:
            tr_idx = [i for i in idx if i not in f]
            pool = [j for j in range(len(ALL)) if j not in f]
            best = (mape(loo_apes(cat, pred, BASE, tr_idx, pool)), None, None)
            for key, vals in grid.items():
                for v in vals:
                    P = dict(BASE); P[key] = v
                    m = mape(loo_apes(cat, pred, P, tr_idx, pool))
                    if m < best[0]:
                        best = (m, key, v)
            P = dict(BASE)
            if best[1]:
                P[best[1]] = best[2]
            picks.append((best[1], best[2]))
            outer_base.update(loo_apes(cat, pred, BASE, f, pool + f))
            outer_sel.update(loo_apes(cat, pred, P, f, pool + f))
        base, lo, hi, p, n = paired_boot(outer_base, outer_sel)
        print(f"  중첩CV: 현행 {mape(outer_base):.2f}% → 내부선택 {mape(outer_sel):.2f}% (Δ={base:+.2f}%p [{lo:+.2f}~{hi:+.2f}] P={p * 100:.1f}%)")
        print(f"    폴드별 선택: {picks}")
    print(f"  ({time.time() - t0:.0f}s)")

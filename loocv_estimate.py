# -*- coding: utf-8 -*-
"""예상 단가 계산기 정확도 측정 — 진짜 Leave-One-Out CV.
production 함수(_predict_box_price/_predict_material_price)를 그대로 사용하되
평가 대상 샘플을 train에서 제외하여 데이터 누수 없이 정확도 측정."""
import sys, statistics as stat
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8")

import app  # MONDAY_DF 로드(모듈 수준), 서버는 __main__ 아래라 미기동

d = app._get_material_data()
items = d['items']
cat_unit = d.get('cat_unit_price', {})

# 평가 대상: 가격/면적 유효
idxs = [i for i, r in enumerate(items) if r.get('price', 0) > 0 and r.get('area', 0) > 0]

def predict_loo(target_i):
    r = items[target_i]
    cat = r['category']
    sel_set = set(r['materials'])
    area = r['area']
    vendor = r.get('vendor', '')
    train = [items[j] for j in range(len(items)) if j != target_i]
    if cat in ('단상자', '박스'):
        est, method = app._predict_box_price(train, sel_set, area, is_carton=(cat == '단상자'))
        if est is None or est <= 0:
            est, method = app._predict_material_price(train, sel_set, '박스', area, vendor)
    else:
        est, method = app._predict_material_price(train, sel_set, cat, area, vendor)
    if est is None or est <= 0:
        unit = cat_unit.get(cat, 0) or 0
        if unit > 0:
            est, method = unit * area, 'fallback_cat_avg'
    return est, method

results = []
for i in idxs:
    est, method = predict_loo(i)
    r = items[i]
    if not est or est <= 0:
        results.append({'code': r['code'], 'cat': r['category'], 'actual': r['price'],
                        'pred': 0, 'ape': None, 'method': method or 'no_match',
                        'mat': r['materials'], 'size': r['size']})
        continue
    ape = abs(est - r['price']) / r['price'] * 100
    results.append({'code': r['code'], 'cat': r['category'], 'actual': r['price'],
                    'pred': est, 'ape': ape, 'method': method,
                    'mat': r['materials'], 'size': r['size']})

ok = [x for x in results if x['ape'] is not None]
nopred = [x for x in results if x['ape'] is None]

def report(rows, label):
    if not rows:
        print(f"  {label}: 샘플 없음"); return
    apes = [x['ape'] for x in rows]
    mape = sum(apes)/len(apes)
    med = stat.median(apes)
    w20 = sum(1 for a in apes if a <= 20)/len(apes)*100
    w30 = sum(1 for a in apes if a <= 30)/len(apes)*100
    print(f"  {label:8s} n={len(rows):3d} | MAPE={mape:5.1f}% | 중앙값={med:5.1f}% | ±20%={w20:4.1f}% | ±30%={w30:4.1f}%")

print(f"=== LOOCV 결과 (예측성공 {len(ok)} / 무예측 {len(nopred)} / 전체 {len(results)}) ===")
report(ok, "전체")
by = defaultdict(list)
for x in ok:
    by[x['cat']].append(x)
for c in sorted(by):
    report(by[c], c)

print("\n=== 오차 TOP 12 ===")
for x in sorted(ok, key=lambda v: -v['ape'])[:12]:
    print(f"  [{x['code']}] {x['cat']} 실제={x['actual']:>9,.0f} 예측={x['pred']:>10,.0f} 오차={x['ape']:6.1f}% ({x['method']})")
    print(f"      {'/'.join(x['mat'])[:55]} | {x['size'][:28]}")

if nopred:
    print(f"\n=== 무예측 {len(nopred)}건 ===")
    for x in nopred[:10]:
        print(f"  [{x['code']}] {x['cat']} {'/'.join(x['mat'])[:50]} | {x['size'][:25]}")

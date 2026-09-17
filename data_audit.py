# -*- coding: utf-8 -*-
"""데이터 품질 감사 — 정확도의 진짜 상한을 만드는 결함 추적.
1) 동일 스펙 가격 불일치 (모델이 구분 불가 → 노이즈 바닥)
2) 단위 의심(단가/면적 이상치)
3) 파싱 실패(면적/두께 추출 안 됨)
출력: 사람이 Monday 부자재 규격에서 검증·정정할 수 있는 목록."""
import sys, math, statistics as stat
sys.stdout.reconfigure(encoding="utf-8")
import app
d = app._get_material_data(); ALL = d['items']

print("=== 1. 동일 재질·유사 면적인데 단가 30%+ 불일치 (정정 대상 후보) ===")
print("    같은 스펙이면 가격도 같아야 정상. 불일치 = 입력오류/누락스펙/협상가 → 정정 시 정확도↑\n")
for cat in ('파우치','박스','단상자'):
    valid=[r for r in ALL if r['category']==cat and r.get('area',0)>0 and r.get('price',0)>0]
    seen=set(); pairs=[]
    for i in range(len(valid)):
        for j in range(i+1,len(valid)):
            a,b=valid[i],valid[j]
            if set(a['materials'])==set(b['materials']) and a['area']>0 and abs(a['area']-b['area'])/a['area']<0.08:
                lo,hi=sorted([a['price'],b['price']])
                diff=(hi-lo)/lo*100
                if diff>30:
                    pairs.append((diff,a,b))
    pairs.sort(key=lambda x:-x[0])
    if pairs:
        print(f"  [{cat}] {len(pairs)}쌍:")
        for diff,a,b in pairs:
            print(f"    {diff:3.0f}% | {a['code']}({a['name'][:14]}) {a['price']:.0f}원  vs  {b['code']}({b['name'][:14]}) {b['price']:.0f}원")
            print(f"         재질: {'/'.join(a['materials'])[:50]} | 사이즈 {a['size'][:20]} vs {b['size'][:20]}")

print("\n=== 2. 단위면적 단가 이상치 (카테고리 중앙값 대비 3배↑ 또는 1/3↓) ===")
print("    단가 입력 단위오류(원/개 vs 원/1000개 등) 의심\n")
for cat in ('파우치','박스','단상자'):
    valid=[r for r in ALL if r['category']==cat and r.get('area',0)>0 and r.get('price',0)>0]
    units=[r['price']/r['area'] for r in valid]
    med=stat.median(units)
    out=[r for r in valid if (r['price']/r['area'])>med*3 or (r['price']/r['area'])<med/3]
    if out:
        print(f"  [{cat}] 중앙 단위단가={med:.4f}, 이상치 {len(out)}건:")
        for r in sorted(out,key=lambda x:-(x['price']/x['area'])):
            u=r['price']/r['area']
            print(f"    {r['code']}({r['name'][:14]}) {r['price']:.0f}원 area={r['area']:.0f} 단위={u:.4f}({u/med:.1f}x중앙) | {r['size'][:20]}")

print("\n=== 3. 면적 파싱 실패 (price>0 인데 area=0 → 예측 불가) ===")
for cat in ('파우치','박스','단상자'):
    bad=[r for r in ALL if r['category']==cat and r.get('price',0)>0 and r.get('area',0)<=0]
    if bad:
        print(f"  [{cat}] {len(bad)}건:")
        for r in bad:
            print(f"    {r['code']}({r['name'][:16]}) 사이즈='{r['size']}' ← 사이즈 형식 비표준")

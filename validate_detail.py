# -*- coding: utf-8 -*-
import sys, json, re, urllib.request, urllib.error
sys.stdout.reconfigure(encoding='utf-8')

BASE = "http://localhost:5000"

def api_get(path):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def api_post(path, body):
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}"
    except Exception as ex:
        return None, str(ex)

def parse_size(size_str):
    if not size_str:
        return None, None, None
    m = re.search(r'(\d+)[*x×](\d+)', size_str)
    if not m:
        return None, None, None
    w, h = int(m.group(1)), int(m.group(2))
    dm = re.search(r'[+＋바닥]+\s*(\d+)', size_str)
    d = int(dm.group(1)) if dm else 0
    return w, h, d

def parse_materials(mat_str):
    if not mat_str:
        return []
    parts = re.split(r'[/,]', mat_str)
    return [p.strip() for p in parts if p.strip()]

def guess_category(cat_str):
    if not cat_str: return "파우치"
    c = cat_str.strip()
    if "박스" in c or "box" in c.lower(): return "박스"
    if "단상자" in c or "단상" in c: return "단상자"
    return "파우치"

data = api_get("/api/spec_list")
items = data.get("items", [])
valid = [x for x in items if isinstance(x, dict) and x.get("price", 0) > 0]

# 오차 큰 TOP 항목 B0404 분석
target_codes = ["B0404", "B0135", "B0384", "B0315", "B0054"]
targets = [x for x in valid if x.get("code") in target_codes]

print("=== 오차 큰 항목 상세 분석 ===\n")
for item in targets:
    code = item.get("code", "")
    name = item.get("name", "")
    actual = item.get("price", 0)
    material = item.get("material", "")
    size = item.get("size", "")
    category = guess_category(item.get("category", ""))
    w, h, d = parse_size(size)
    materials = parse_materials(material)

    print(f"[{code}] {name}")
    print(f"  실제단가: {actual}원  |  size: {size}  |  category: {category}")
    print(f"  material raw: {material}")
    print(f"  materials parsed: {materials}")
    print(f"  w={w}, h={h}, d={d}")

    if w:
        body = {"materials": materials, "category": category, "w": w, "h": h, "d": d}
        resp, err = api_post("/api/material_estimate", body)
        if resp:
            estimate = resp.get("estimate")
            method = resp.get("method", "")
            similar = resp.get("similar", [])
            print(f"  예측: {estimate}원  (method={method})")
            print(f"  유사항목 상위3:")
            for s in similar[:3]:
                print(f"    {s.get('code')} {s.get('name','')[:25]} price={s.get('price')} overlap={s.get('overlap')}/{s.get('total')}")
        else:
            print(f"  오류: {err}")
    print()

# 정확도 높은 항목도 확인
print("\n=== 정확도 높은 항목 (오차 5% 이내) ===")
results = []
for item in valid[:50]:  # 처음 50개만 빠르게
    code = item.get("code", "")
    actual = item.get("price", 0)
    material = item.get("material", "")
    size = item.get("size", "")
    category = guess_category(item.get("category", ""))
    w, h, d = parse_size(size)
    materials = parse_materials(material)
    if not w or not materials:
        continue
    body = {"materials": materials, "category": category, "w": w, "h": h, "d": d}
    resp, err = api_post("/api/material_estimate", body)
    if resp:
        estimate = resp.get("estimate", 0)
        if actual > 0:
            err_pct = abs(estimate - actual) / actual * 100
            results.append({"code": code, "actual": actual, "estimate": estimate, "err": err_pct, "method": resp.get("method","")})

good = [r for r in results if r["err"] <= 5]
print(f"처음 50개 중 오차 5%이내: {len(good)}개")
for r in sorted(good, key=lambda x: x["err"])[:5]:
    print(f"  {r['code']}: 실제={r['actual']} 예측={r['estimate']:.0f} 오차={r['err']:.1f}% method={r['method']}")

# method별 분포
print("\n=== method별 예측 분포 (처음 50개) ===")
from collections import Counter
mc = Counter(r["method"] for r in results)
for m, cnt in mc.most_common():
    errs = [r["err"] for r in results if r["method"] == m]
    avg = sum(errs)/len(errs) if errs else 0
    print(f"  {m}: {cnt}개, 평균오차 {avg:.1f}%")

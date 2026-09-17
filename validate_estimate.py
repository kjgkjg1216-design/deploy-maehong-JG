# -*- coding: utf-8 -*-
import requests
import json
import re
import sys
from collections import defaultdict

BASE = "http://localhost:5000"

resp = requests.get(f"{BASE}/api/spec_list")
raw = resp.json()

items = raw.get("items", raw) if isinstance(raw, dict) else raw
priced = [s for s in items if s.get("price") and float(s.get("price", 0)) > 0]

def parse_materials(mat_str):
    if not mat_str:
        return []
    tokens = re.split(r"[/+]", str(mat_str))
    result = []
    for t in tokens:
        t = t.strip()
        t = re.sub(r"㎛", "", t)
        t = re.sub(r"um", "", t, flags=re.IGNORECASE)
        t = t.strip().upper()
        if t:
            result.append(t)
    return result

def map_category(cat):
    if not cat:
        return cat
    if "파우치" in cat:
        return "파우치"
    if "단상자" in cat:
        return "단상자"
    if "RRP" in cat or "물류박스" in cat or "박스" in cat:
        return "박스"
    return cat

def parse_size(size_str):
    w, h, d = 0, 0, 0
    if not size_str:
        return w, h, d
    milji = re.search(r"[\+밑]?\s*밑지\s*(\d+)", size_str)
    if milji:
        d = int(milji.group(1))
    wh_match = re.search(r"[Ww][A-Za-z]?(\d+)\*[Hh][A-Za-z]?(\d+)", size_str)
    if wh_match:
        w = int(wh_match.group(1))
        h = int(wh_match.group(2))
        return w, h, d
    triple = re.search(r"(\d+)\*(\d+)\*(\d+)", size_str)
    if triple:
        w = int(triple.group(1))
        h = int(triple.group(2))
        if d == 0:
            d = int(triple.group(3))
        return w, h, d
    pair = re.findall(r"[A-Za-z]?(\d+)\*[A-Za-z]?(\d+)", size_str)
    if pair:
        w = int(pair[0][0])
        h = int(pair[0][1])
    return w, h, d

target_ids = {"B0077", "B0289", "B0290", "B0396", "B0384", "B0404"}
results = []
errors = []

for item in priced:
    spec_id = item.get("code", "")
    mat_str = item.get("material", "")
    cat_raw = item.get("category", "")
    size_str = item.get("size", "")
    actual_price = float(item.get("price", 0))

    materials = parse_materials(mat_str)
    category = map_category(cat_raw)
    w, h, d = parse_size(size_str)

    if not materials:
        errors.append({"id": spec_id, "reason": "no materials"})
        continue

    body = {"materials": materials, "category": category, "w": w, "h": h, "d": d}

    try:
        r = requests.post(f"{BASE}/api/material_estimate", json=body, timeout=10)
        if r.status_code == 200:
            data = r.json()
            pred_val = data.get("estimate") or data.get("predicted_price") or data.get("price") or 0
            if pred_val and float(pred_val) > 0:
                pred = float(pred_val)
                err_pct = abs(pred - actual_price) / actual_price * 100
                results.append({
                    "id": spec_id, "category": cat_raw, "material": mat_str,
                    "size": size_str, "actual": actual_price, "predicted": pred,
                    "error_pct": err_pct, "within_20": err_pct <= 20,
                    "method": data.get("method", "")
                })
            else:
                errors.append({"id": spec_id, "reason": "no prediction", "resp": str(data)[:100]})
        else:
            errors.append({"id": spec_id, "reason": f"status {r.status_code}", "text": r.text[:100]})
    except Exception as e:
        errors.append({"id": spec_id, "reason": str(e)})

sys.stdout.reconfigure(encoding="utf-8")

print(f"분석완료={len(results)}, 실패={len(errors)}")

if results:
    within_20 = [r for r in results if r["within_20"]]
    accuracy = len(within_20) / len(results) * 100
    mape = sum(r["error_pct"] for r in results) / len(results)

    print(f"")
    print(f"1. 전체 ±20% 이내 정확도: {accuracy:.1f}% ({len(within_20)}/{len(results)})")
    print(f"2. 전체 MAPE: {mape:.1f}%")

    cats = defaultdict(list)
    for r in results:
        cats[r["category"]].append(r)

    print(f"")
    print(f"3. 카테고리별:")
    for cat in sorted(cats.keys()):
        citems = cats[cat]
        cmape = sum(i["error_pct"] for i in citems) / len(citems)
        cacc = sum(1 for i in citems if i["within_20"]) / len(citems) * 100
        cnt = sum(1 for i in citems if i["within_20"])
        print(f"   {cat}: MAPE={cmape:.1f}%, 정확도={cacc:.1f}% ({cnt}/{len(citems)})")

    top10 = sorted(results, key=lambda x: x["error_pct"], reverse=True)[:10]
    print(f"")
    print(f"4. 오차 TOP 10:")
    for i, r in enumerate(top10, 1):
        print(f"   {i}. [{r['id']}] {r['category']} | 실제={r['actual']:,.0f} 예측={r['predicted']:,.2f} | 오차={r['error_pct']:.1f}%")
        print(f"      재질: {r['material'][:60]} | 규격: {r['size'][:30]}")

    print(f"")
    print(f"5. 이전 문제 항목:")
    found = set()
    for r in results:
        if r["id"] in target_ids:
            found.add(r["id"])
            s = "OK" if r["within_20"] else "FAIL"
            print(f"   [{r['id']}] {s} | 실제={r['actual']:,.0f} 예측={r['predicted']:,.2f} | 오차={r['error_pct']:.1f}%")
    missing = target_ids - found
    if missing:
        print(f"   미발견: {missing}")

    print(f"")
    print(f"6. 이전 결과 비교:")
    print(f"   2차: 정확도 47.6%, MAPE 59.4%")
    print(f"   현재: 정확도 {accuracy:.1f}%, MAPE {mape:.1f}%")
    diff_acc = accuracy - 47.6
    diff_mape = mape - 59.4
    print(f"   변화: 정확도 {diff_acc:+.1f}%p, MAPE {diff_mape:+.1f}%p")

if errors:
    print(f"")
    print(f"실패 샘플 (최대 5건):")
    for e in errors[:5]:
        print(f"  {e}")

"""판매단가(영업 기준단가) 수집 → data/YYYYMMDD_판매단가.csv
아마란스 품목단가정보 API(api20A00S01401)의 standardUm(기준단가)을 판매단가로 사용.
- 완제품(G/H/I) 판매단가가 saleUm엔 비어있고 standardUm에 채워져 있음 (전 품목 커버).
- 매출액 계산(월 판매기반 자료 패널)에서 사용.
"""
import os, sys
import pandas as pd

BASE_DIR = os.environ.get('APP_BASE_DIR', 'C:/Users/jgkim/maehong-JG')
DATA_DIR = os.environ.get('DATA_DIR', BASE_DIR + '/data')
sys.path.insert(0, BASE_DIR)
import amaranth_api as a


def fetch():
    r = a.call_api('/apiproxy/api20A00S01401', {'coCd': a.CO_CD}, verbose=False)
    if not r or r.get('resultCode') != 0:
        print('[판매단가] 조회 실패:', (r or {}).get('resultMsg'))
        return None
    rows = []
    for it in r.get('resultData', []):
        code = str(it.get('itemCd', '')).strip()
        if not code:
            continue

        def _f(v):
            try:
                return float(v or 0)
            except Exception:
                return 0.0
        # 판매단가 우선순위: 기준단가 → 판매단가 → 매입단가
        price = _f(it.get('standardUm')) or _f(it.get('saleUm')) or _f(it.get('purchUm'))
        # 매입단가(purchUm)·기준단가(standardUm)도 함께 저장 (2026-09-11: 대시보드 단가를 발주단가>아마란스 매입단가>엑셀 순으로 자동화)
        rows.append({'품번': code, '품명': str(it.get('itemNm', '')).strip(),
                     '판매단가': int(round(price)),
                     '매입단가': round(_f(it.get('purchUm')), 2),
                     '기준단가': round(_f(it.get('standardUm')), 2)})
    return pd.DataFrame(rows)


def main():
    df = fetch()
    if df is None or df.empty:
        print('[판매단가] 데이터 없음 — 저장 건너뜀')
        return
    from datetime import datetime
    today = datetime.now().strftime('%Y%m%d')
    path = f'{DATA_DIR}/{today}_판매단가.csv'
    df.to_csv(path, index=False, encoding='utf-8-sig')
    nz = (df['판매단가'] > 0).sum()
    print(f'[판매단가] 저장 완료: {path} ({len(df)}건, 단가>0 {nz}건)')


if __name__ == '__main__':
    main()

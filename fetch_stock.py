"""아마란스 현재고 조회(품목별) - api20A02S01501.
G/H/I/E 전 품번의 자사 창고 현재고 수집 → 20YYMMDD_현재고.csv 저장.
E품번 재고 대시보드 표시용 (BOM 자사재고 lookup이 JASA_DF에서 못 찾던 문제 해결)."""
import sys
from datetime import datetime
import pandas as pd
sys.path.insert(0, 'c:/Users/jgkim/maehong-JG')
from amaranth_api import call_api, CO_CD
from _safe_csv import safe_to_csv


COL_RENAME = {
    'itemCd':       '품번',
    'itemNm':       '품명',
    'itemDc':       '품목구분',
    'unitDc':       '단위',
    'whCd':         '창고코드',
    'whNm':         '창고명',
    'lcCd':         '위치코드',
    'lcNm':         '위치명',
    'divCd':        '사업장코드',
    'divNm':        '사업장명',
    'iopenQt':      '기초재고',
    'ircvQt':       '입고량',
    'iisuQt':       '출고량',
    'invQt1':       '현재고',
    'safestockQt':  '안전재고',
    'gayongQt':     '가용수량',
    'invmangQt':    '관리재고',
    'unitmangDc':   '관리단위',
}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    year = datetime.now().strftime('%Y')
    today = datetime.now().strftime('%Y%m%d')

    print(f"[현재고 수집] year={year}, totalFg=0")
    r = call_api('/apiproxy/api20A02S01501',
                 {'coCd': CO_CD, 'year': year, 'totalFg': '0'},
                 verbose=False)
    if not r or r.get('resultCode') != 0:
        print(f"  실패: {r}")
        sys.exit(1)

    data = r.get('resultData', [])
    print(f"  수신 {len(data)}건")

    if not data:
        print("  데이터 없음 - 종료")
        return

    df = pd.DataFrame(data)
    rename_map = {k: v for k, v in COL_RENAME.items() if k in df.columns}
    df = df.rename(columns=rename_map)

    # 주요 컬럼 먼저
    priority = [v for v in COL_RENAME.values() if v in df.columns]
    others = [c for c in df.columns if c not in priority]
    df = df[priority + others]

    # 코드 prefix별 카운트
    if '품번' in df.columns:
        from collections import Counter
        pref = Counter(str(c)[:1] for c in df['품번'])
        print(f"  prefix: {dict(pref.most_common())}")

    out = f'C:/Users/jgkim/maehong-JG/data/{today}_현재고.csv'
    safe_to_csv(df, out, label='현재고')
    print(f"[저장 시도] {out} ({len(df)}건, {len(df.columns)}열)")


if __name__ == '__main__':
    main()

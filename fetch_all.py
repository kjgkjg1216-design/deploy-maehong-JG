"""
아마란스10 전체 데이터 통합 수집 (25년6월 ~ 현재)
구매발주, 외주발주, 생산실적, 출하, 출고, BOM
"""
import os, sys, json, time, hmac, hashlib, base64, secrets, urllib3, requests
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv('C:/Users/jgkim/maehong-JG/.env')

ACCESS_TOKEN = os.getenv('AMARANTH_ACCESS_TOKEN', '').strip()
HASH_KEY     = os.getenv('AMARANTH_HASH_KEY', '').strip()
GROUP_SEQ    = os.getenv('AMARANTH_GROUP_SEQ', '').strip()
CALLER_NAME  = os.getenv('AMARANTH_CALLER_NAME', '').strip()
BASE_URL = 'https://gwa.maehong.kr'
CO_CD    = '1000'
DATE_FROM = '20250601'  # 수집 시작일

def make_sign(at, tid, ts, path):
    return base64.b64encode(hmac.new(HASH_KEY.encode(), (at+tid+ts+path).encode(), hashlib.sha256).digest()).decode()

def call_api(api_path, body=None):
    tid = secrets.token_hex(16)
    ts = str(int(time.time()))
    headers = {
        'Content-Type': 'application/json', 'Authorization': f'Bearer {ACCESS_TOKEN}',
        'transaction-id': tid, 'timestamp': ts, 'CallerName': CALLER_NAME,
        'groupSeq': GROUP_SEQ, 'wehago-sign': make_sign(ACCESS_TOKEN, tid, ts, api_path),
    }
    try:
        return requests.post(f"{BASE_URL}{api_path}", json=body or {}, headers=headers, timeout=30, verify=False).json()
    except:
        return None

def save_csv(items, name, col_rename=None):
    if not items:
        print(f"  [{name}] 데이터 없음")
        return
    df = pd.DataFrame(items)
    if col_rename:
        df = df.rename(columns={k:v for k,v in col_rename.items() if k in df.columns})
    today = datetime.now().strftime('%Y%m%d')
    path = f'C:/Users/jgkim/maehong-JG/data/{today}_{name}.csv'
    df.to_csv(path, index=False, encoding='utf-8-sig')
    print(f"  [{name}] 저장: {path} ({len(df)}건)")

# ── 월별 분할 수집 (API 부하 방지) ─────────────────────
def month_ranges(start='20250601'):
    """시작일부터 현재까지 월별 (from, to) 리스트"""
    from datetime import date
    ranges = []
    y, m = int(start[:4]), int(start[4:6])
    today = date.today()
    while True:
        dt_from = f"{y}{m:02d}01"
        if m == 12:
            ny, nm = y+1, 1
        else:
            ny, nm = y, m+1
        # 월말
        from calendar import monthrange
        _, last_day = monthrange(y, m)
        dt_to = f"{y}{m:02d}{last_day}"
        if int(dt_from) > int(today.strftime('%Y%m%d')):
            break
        ranges.append((dt_from, min(dt_to, today.strftime('%Y%m%d'))))
        y, m = ny, nm
    return ranges


def fetch_header_detail(header_api, detail_api, date_param_from, date_param_to,
                        detail_key='poNb', header_merge_fields=None, label=''):
    """헤더+디테일 패턴의 API를 월별로 수집"""
    all_details = []
    months = month_ranges(DATE_FROM)

    for dt_from, dt_to in months:
        body = {'coCd': CO_CD, date_param_from: dt_from, date_param_to: dt_to}
        r = call_api(header_api, body)
        if not r or r.get('resultCode') != 0:
            continue
        headers_data = r.get('resultData', [])
        if not headers_data:
            continue

        sys.stdout.write(f"\r  {label} {dt_from[:6]}: 헤더 {len(headers_data)}건...")
        sys.stdout.flush()

        for h in headers_data:
            key_val = h.get(detail_key, '')
            if not key_val:
                continue
            dr = call_api(detail_api, {'coCd': CO_CD, detail_key: key_val})
            if dr and dr.get('resultCode') == 0 and dr.get('resultData'):
                details = dr['resultData']
                if header_merge_fields:
                    for d in details:
                        for field, src in header_merge_fields.items():
                            d[field] = h.get(src, '')
                all_details.extend(details)
            time.sleep(0.2)

        sys.stdout.write(f"\r  {label} {dt_from[:6]}: 헤더 {len(headers_data)}건 → 누적 {len(all_details)}건\n")
        sys.stdout.flush()
        time.sleep(0.5)

    print(f"  [{label} 완료] {len(all_details)}건")
    return all_details


def fetch_simple(api_path, date_param_from, date_param_to, label=''):
    """단순 날짜 범위 조회 API를 월별로 수집"""
    all_items = []
    months = month_ranges(DATE_FROM)

    for dt_from, dt_to in months:
        body = {'coCd': CO_CD, date_param_from: dt_from, date_param_to: dt_to}
        r = call_api(api_path, body)
        if r and r.get('resultCode') == 0 and r.get('resultData'):
            items = r['resultData']
            all_items.extend(items)
            sys.stdout.write(f"\r  {label} {dt_from[:6]}: {len(items)}건 (누적 {len(all_items)}건)")
            sys.stdout.flush()
        time.sleep(0.3)

    print(f"\n  [{label} 완료] {len(all_items)}건")
    return all_items


# ── 컬럼 매핑 ───────────────────────────────
PO_RENAME = {
    'poNb':'발주번호','poSq':'순번','itemCd':'품번','itemNm':'품명',
    'itemDc':'품목구분','unitDc':'단위','dueDt':'납기일자','shipreqDt':'입고예정일',
    'poQt':'발주수량','poUm':'단가','pogAm':'공급가액','pogvAm1':'부가세',
    'poghAm1':'합계금액','rcvQt':'입고수량','remarkDc':'비고','insertDt':'등록일시',
    '거래처명':'거래처명','거래처코드':'거래처코드','발주일자':'발주일자',
}
WP_RENAME = {
    'poNb':'외주발주번호','poSq':'순번','itemCd':'품번','itemNm':'품명',
    'unitNm':'단위','dueDt':'납기일자','poQt':'발주수량','poUm':'단가',
    'pogAm':'공급가액','pogvAm1':'부가세','poghAm1':'합계금액',
    'rcvQt':'입고수량','remarkDc':'비고','insertDt':'등록일시',
    '거래처명':'거래처명','발주일자':'발주일자',
}
SHIP_RENAME = {
    'isuNb':'출하번호','isuSq':'순번','itemCd':'품번','itemNm':'품명',
    'itemDc':'품목구분','unitDc':'단위','poQt':'발주수량','isuQt':'출하수량',
    'whNm':'창고','lcNm':'로케이션','poNb':'발주번호',
    'itemparentCd':'모품번','itemparentNm':'모품명','remarkDc':'비고',
    '거래처명':'거래처명','출하일자':'출하일자',
}
ISU_RENAME = {
    'isuNb':'출고번호','isuSq':'순번','itemCd':'품번','itemNm':'품명',
    'itemDc':'품목구분','unitDc':'단위','isuQt':'출고수량',
    'fwhNm':'출고창고','twhNm':'입고창고',
    'itemparentCd':'모품번','itemparentNm':'모품명','remarkDc':'비고',
    '출고일자':'출고일자',
}
RCV_RENAME = {
    'rcvNb':'입고번호','rcvSq':'순번','itemCd':'품번','itemNm':'품명',
    'itemDc':'품목구분','unitDc':'단위','rcvQt':'입고수량','poQt':'발주수량',
    'rcvUm':'단가','rcvgAm':'공급가액','rcvvAm':'부가세','rcvhAm':'합계금액',
    'lcNm':'입고장소','whNm':'입고창고','lotNb':'LOT번호',
    'poNb':'발주번호','remarkDc':'비고',
    '거래처명':'거래처명','거래처코드':'거래처코드','입고일자':'입고일자',
}
WR_RENAME = {
    'wrCd':'실적번호','woCd':'생산지시번호','wrDt':'실적일자',
    'workQt':'작업수량','goodQt':'양품수량','badQt':'불량수량','moveQt':'이동수량',
    'movebaselocNm':'이동창고','movelocNm':'이동위치','remarkDc':'비고',
}


if __name__ == '__main__':
    print("=" * 60)
    print(f"  아마란스10 전체 데이터 수집 ({DATE_FROM} ~ 현재)")
    print(f"  회사: {CO_CD} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # 1. 구매발주 (헤더+디테일)
    print("\n[1/7] 구매발주")
    po_items = fetch_header_detail(
        '/apiproxy/api20A02S00101', '/apiproxy/api20A02S00102',
        'poDtFrom', 'poDtTo', detail_key='poNb',
        header_merge_fields={'거래처명':'attrNm','거래처코드':'trCd','발주일자':'poDt'},
        label='구매발주'
    )
    save_csv(po_items, '발주정보', PO_RENAME)

    # 2. 외주발주 (헤더+디테일)
    print("\n[2/7] 외주발주")
    wp_items = fetch_header_detail(
        '/apiproxy/api20A03S01201', '/apiproxy/api20A03S01202',
        'poDtFrom', 'poDtTo', detail_key='poNb',
        header_merge_fields={'거래처명':'attrNm','거래처코드':'trCd','발주일자':'poDt','부서명':'deptNm','담당자':'korNm'},
        label='외주발주'
    )
    save_csv(wp_items, '외주발주정보', WP_RENAME)

    # 3. 생산지시
    print("\n[3/7] 생산지시+실적")
    wo_items = fetch_simple('/apiproxy/api20A03S00801', 'woDtFrom', 'woDtTo', '생산지시')
    wo_map = {i.get('woCd',''): {'품번':i.get('itemCd',''),'품명':i.get('itemNm',''),'품목구분':i.get('itemDc',''),'지시수량':i.get('itemQt','')} for i in wo_items}

    # 4. 생산실적
    wr_items = fetch_simple('/apiproxy/api20A03S00901', 'wrDtFrom', 'wrDtTo', '생산실적')
    for item in wr_items:
        wo = wo_map.get(item.get('woCd',''), {})
        item['품번'] = wo.get('품번','')
        item['품명'] = wo.get('품명','')
        item['품목구분'] = wo.get('품목구분','')
        item['지시수량'] = wo.get('지시수량','')
    save_csv(wr_items, '생산실적', WR_RENAME)

    # 5. 출하 (헤더+디테일)
    print("\n[5/7] 출하(매출)")
    ship_items = fetch_header_detail(
        '/apiproxy/api20A02S01001', '/apiproxy/api20A02S01002',
        'fromDt', 'toDt', detail_key='isuNb',
        header_merge_fields={'거래처명':'trNm','거래처코드':'trCd','출하일자':'isuDt'},
        label='출하'
    )
    save_csv(ship_items, '출하정보', SHIP_RENAME)

    # 6. 입고 (헤더+디테일)
    print("\n[6/7] 입고")
    rcv_items = fetch_header_detail(
        '/apiproxy/api20A02S00201', '/apiproxy/api20A02S00202',
        'rcvDtFrom', 'rcvDtTo', detail_key='rcvNb',
        header_merge_fields={'거래처명':'attrNm','거래처코드':'trCd','입고일자':'rcvDt','입고창고':'whNm'},
        label='입고'
    )
    save_csv(rcv_items, '입고정보', RCV_RENAME)

    # 7. 출고 (헤더+디테일)
    print("\n[7/7] 출고(자재이동)")
    isu_items = fetch_header_detail(
        '/apiproxy/api20A02S00801', '/apiproxy/api20A02S00802',
        'isuDtFrom', 'isuDtTo', detail_key='isuNb',
        header_merge_fields={'출고일자':'isuDt','부서명':'deptNm','담당자':'korNm'},
        label='출고'
    )
    save_csv(isu_items, '출고정보', ISU_RENAME)

    # 요약
    print("\n" + "=" * 60)
    print(f"  구매발주:   {len(po_items):,}건")
    print(f"  외주발주:   {len(wp_items):,}건")
    print(f"  생산실적:   {len(wr_items):,}건")
    print(f"  출하(매출): {len(ship_items):,}건")
    print(f"  입고:       {len(rcv_items):,}건")
    print(f"  출고(자재): {len(isu_items):,}건")
    print(f"  기간: {DATE_FROM} ~ {datetime.now().strftime('%Y%m%d')}")
    print("  챗봇 서버 재시작 시 자동 로드됩니다.")
    print("=" * 60)

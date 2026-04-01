"""
아마란스10 API에서 발주정보 + 외주발주정보를 가져와 CSV로 저장
실행: python fetch_orders.py
"""
import os
import sys
import json
import time
import hmac
import hashlib
import base64
import secrets
import urllib3
import requests
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


def make_wehago_sign(access_token, transaction_id, timestamp, url_path):
    value = access_token + transaction_id + timestamp + url_path
    signature = hmac.new(
        HASH_KEY.encode('utf-8'),
        value.encode('utf-8'),
        hashlib.sha256
    ).digest()
    return base64.b64encode(signature).decode('utf-8')


def call_api(api_path, body=None, verbose=False):
    url = f"{BASE_URL}{api_path}"
    transaction_id = secrets.token_hex(16)
    timestamp = str(int(time.time()))
    wehago_sign = make_wehago_sign(ACCESS_TOKEN, transaction_id, timestamp, api_path)

    headers = {
        'Content-Type':   'application/json',
        'Authorization':  f'Bearer {ACCESS_TOKEN}',
        'transaction-id': transaction_id,
        'timestamp':      timestamp,
        'CallerName':     CALLER_NAME,
        'groupSeq':       GROUP_SEQ,
        'wehago-sign':    wehago_sign,
    }

    try:
        resp = requests.post(url, json=body or {}, headers=headers, timeout=15, verify=False)
        return resp.json()
    except Exception:
        return None


# ────────────────────────────────────────────
# 1. 구매발주 수집 (PO번호 스캔)
# ────────────────────────────────────────────
def fetch_purchase_orders(date_from='20260301', date_to=None):
    """구매 발주정보 수집: 헤더(거래처) + 디테일 병합"""
    if date_to is None:
        date_to = datetime.now().strftime('%Y%m%d')

    # 1단계: 헤더 조회 (거래처 정보 포함)
    print(f"[구매발주] {date_from} ~ {date_to} 헤더 조회 (api20A02S00101)")
    header_result = call_api('/apiproxy/api20A02S00101', {
        'coCd': CO_CD,
        'poDtFrom': date_from,
        'poDtTo': date_to,
    })

    if not header_result or header_result.get('resultCode') != 0:
        print("  헤더 조회 실패")
        return []

    headers_data = header_result.get('resultData', [])
    print(f"  헤더 {len(headers_data)}건 발견")

    # 2단계: 각 발주번호별 디테일 조회 + 거래처 병합
    all_items = []
    for i, h in enumerate(headers_data):
        po_nb = h.get('poNb', '')
        if not po_nb:
            continue

        result = call_api('/apiproxy/api20A02S00102', {'coCd': CO_CD, 'poNb': po_nb})

        if result and result.get('resultCode') == 0 and result.get('resultData'):
            details = result['resultData']
            # 헤더의 거래처 정보를 디테일에 병합
            for d in details:
                d['거래처명'] = h.get('attrNm', '')
                d['거래처코드'] = h.get('trCd', '')
                d['발주일자'] = h.get('poDt', '')
                d['부서명'] = h.get('deptNm', '')
                d['담당자'] = h.get('korNm', '')
            all_items.extend(details)
            sys.stdout.write(f"\r  [{i+1}/{len(headers_data)}] {po_nb}: +{len(details)}건 (누적 {len(all_items)}건)")
            sys.stdout.flush()

        time.sleep(0.3)

    print(f"\n[구매발주 완료] 헤더 {len(headers_data)}건, 디테일 {len(all_items)}건")
    return all_items


# ────────────────────────────────────────────
# 2. 외주발주 수집 (날짜 범위 헤더 조회 + 디테일)
# ────────────────────────────────────────────
def fetch_outsource_orders(date_from='20260301', date_to=None):
    """외주발주 헤더(api20A03S01201) + 디테일(api20A03S01202) 수집"""
    if date_to is None:
        date_to = datetime.now().strftime('%Y%m%d')

    print(f"[외주발주] {date_from} ~ {date_to} 헤더 조회")

    # 헤더 목록 조회
    header_result = call_api('/apiproxy/api20A03S01201', {
        'coCd': CO_CD,
        'poDtFrom': date_from,
        'poDtTo': date_to,
    })

    if not header_result or header_result.get('resultCode') != 0:
        print("  헤더 조회 실패")
        return [], []

    headers_data = header_result.get('resultData', [])
    print(f"  헤더 {len(headers_data)}건 발견")

    # 각 헤더의 디테일 조회
    all_details = []
    for i, h in enumerate(headers_data):
        po_nb = h.get('poNb', '')
        if not po_nb:
            continue

        detail_result = call_api('/apiproxy/api20A03S01202', {
            'coCd': CO_CD,
            'poNb': po_nb,
        })

        if detail_result and detail_result.get('resultCode') == 0 and detail_result.get('resultData'):
            details = detail_result['resultData']
            # 헤더 정보를 디테일에 병합
            for d in details:
                d['거래처명'] = h.get('attrNm', '')
                d['거래처코드'] = h.get('trCd', '')
                d['발주일자'] = h.get('poDt', '')
                d['부서명'] = h.get('deptNm', '')
                d['담당자'] = h.get('korNm', '')
            all_details.extend(details)
            sys.stdout.write(f"\r  [{i+1}/{len(headers_data)}] {po_nb}: +{len(details)}건 (누적 {len(all_details)}건)")
            sys.stdout.flush()

        time.sleep(0.3)

    print(f"\n[외주발주 완료] 헤더 {len(headers_data)}건, 디테일 {len(all_details)}건")
    return headers_data, all_details


# ────────────────────────────────────────────
# CSV 저장
# ────────────────────────────────────────────
def save_csv(items, name, col_rename=None):
    if not items:
        print(f"[{name}] 데이터 없음, 저장 건너뜀")
        return None

    df = pd.DataFrame(items)
    if col_rename:
        rename_map = {k: v for k, v in col_rename.items() if k in df.columns}
        df = df.rename(columns=rename_map)

    today = datetime.now().strftime('%Y%m%d')
    output = f'C:/Users/jgkim/maehong-JG/data/{today}_{name}.csv'
    df.to_csv(output, index=False, encoding='utf-8-sig')
    print(f"[저장] {output} ({len(df)}건)")
    return output


# 컬럼명 매핑
PO_COL_RENAME = {
    'poNb': '발주번호', 'poSq': '순번', 'itemCd': '품번', 'itemNm': '품명',
    'itemDc': '품목구분', 'unitDc': '단위', 'dueDt': '납기일자',
    'shipreqDt': '입고예정일', 'poQt': '발주수량', 'poUm': '단가',
    'pogAm': '공급가액', 'pogvAm1': '부가세', 'poghAm1': '합계금액',
    'exchCd': '환종코드', 'rcvQt': '입고수량', 'remarkDc': '비고',
    'insertDt': '등록일시',
    '거래처명': '거래처명', '거래처코드': '거래처코드', '발주일자': '발주일자',
    '부서명': '부서명', '담당자': '담당자',
}

WP_COL_RENAME = {
    'poNb': '외주발주번호', 'poSq': '순번', 'itemCd': '품번', 'itemNm': '품명',
    'unitNm': '단위', 'dueDt': '납기일자', 'poQt': '발주수량', 'poUm': '단가',
    'pogAm': '공급가액', 'pogvAm1': '부가세', 'poghAm1': '합계금액',
    'rcvQt': '입고수량', 'remarkDc': '비고', 'insertDt': '등록일시',
    '거래처명': '거래처명', '거래처코드': '거래처코드', '발주일자': '발주일자',
    '부서명': '부서명', '담당자': '담당자',
}


if __name__ == '__main__':
    print("=" * 50)
    print("  아마란스10 발주정보 통합 수집")
    print(f"  회사: {CO_CD} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    # 1. 구매발주 (헤더+디테일 병합으로 거래처 포함)
    print("\n[1/2] 구매발주 수집")
    po_items = fetch_purchase_orders(date_from='20260301')
    save_csv(po_items, '발주정보', PO_COL_RENAME)

    # 2. 외주발주
    print("\n[2/2] 외주발주 수집")
    wp_headers, wp_details = fetch_outsource_orders(date_from='20260301')
    save_csv(wp_details, '외주발주정보', WP_COL_RENAME)

    print("\n" + "=" * 50)
    print(f"  구매발주: {len(po_items)}건")
    print(f"  외주발주: {len(wp_details)}건")
    print("  챗봇 서버 재시작 시 자동 로드됩니다.")
    print("=" * 50)

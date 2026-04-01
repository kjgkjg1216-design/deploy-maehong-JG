"""
아마란스10 API에서 출하(매출) + 출고 데이터를 수집하여 CSV로 저장
- 출하(SI): 거래처에 납품하는 매출 데이터
- 출고(MW): 내부 자재 이동 데이터
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


def call_api(api_path, body=None):
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


def fetch_shipments(date_from='20260301', date_to=None):
    """출하(매출) 헤더(api20A02S01001) + 디테일(api20A02S01002) 수집"""
    if date_to is None:
        date_to = datetime.now().strftime('%Y%m%d')

    print(f"[출하/매출] {date_from} ~ {date_to} 헤더 조회")

    header_result = call_api('/apiproxy/api20A02S01001', {
        'coCd': CO_CD,
        'fromDt': date_from,
        'toDt': date_to,
    })

    if not header_result or header_result.get('resultCode') != 0:
        print("  헤더 조회 실패")
        return []

    headers_data = header_result.get('resultData', [])
    print(f"  헤더 {len(headers_data)}건 발견")

    all_details = []
    for i, h in enumerate(headers_data):
        isu_nb = h.get('isuNb', '')
        if not isu_nb:
            continue

        detail_result = call_api('/apiproxy/api20A02S01002', {
            'coCd': CO_CD,
            'isuNb': isu_nb,
        })

        if detail_result and detail_result.get('resultCode') == 0 and detail_result.get('resultData'):
            details = detail_result['resultData']
            for d in details:
                d['거래처명'] = h.get('trNm', '')
                d['거래처코드'] = h.get('trCd', '')
                d['출하일자'] = h.get('isuDt', '')
                d['부서명'] = h.get('deptNm', '')
                d['담당자'] = h.get('korNm', '')
            all_details.extend(details)
            sys.stdout.write(f"\r  [{i+1}/{len(headers_data)}] {isu_nb}: +{len(details)}건 (누적 {len(all_details)}건)")
            sys.stdout.flush()

        time.sleep(0.3)

    print(f"\n[출하/매출 완료] 헤더 {len(headers_data)}건, 디테일 {len(all_details)}건")
    return all_details


def fetch_issues(date_from='20260301', date_to=None):
    """출고(자재이동) 헤더(api20A02S00801) + 디테일(api20A02S00802) 수집"""
    if date_to is None:
        date_to = datetime.now().strftime('%Y%m%d')

    print(f"\n[출고/자재이동] {date_from} ~ {date_to} 헤더 조회")

    header_result = call_api('/apiproxy/api20A02S00801', {
        'coCd': CO_CD,
        'isuDtFrom': date_from,
        'isuDtTo': date_to,
    })

    if not header_result or header_result.get('resultCode') != 0:
        print("  헤더 조회 실패")
        return []

    headers_data = header_result.get('resultData', [])
    print(f"  헤더 {len(headers_data)}건 발견")

    all_details = []
    for i, h in enumerate(headers_data):
        isu_nb = h.get('isuNb', '')
        if not isu_nb:
            continue

        detail_result = call_api('/apiproxy/api20A02S00802', {
            'coCd': CO_CD,
            'isuNb': isu_nb,
        })

        if detail_result and detail_result.get('resultCode') == 0 and detail_result.get('resultData'):
            details = detail_result['resultData']
            for d in details:
                d['출고일자'] = h.get('isuDt', '')
                d['부서명'] = h.get('deptNm', '')
                d['담당자'] = h.get('korNm', '')
            all_details.extend(details)
            sys.stdout.write(f"\r  [{i+1}/{len(headers_data)}] {isu_nb}: +{len(details)}건 (누적 {len(all_details)}건)")
            sys.stdout.flush()

        time.sleep(0.3)

    print(f"\n[출고 완료] 헤더 {len(headers_data)}건, 디테일 {len(all_details)}건")
    return all_details


def save_csv(items, name, col_rename=None):
    if not items:
        print(f"[{name}] 데이터 없음")
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


SHIP_COL_RENAME = {
    'isuNb': '출하번호', 'isuSq': '순번', 'itemCd': '품번', 'itemNm': '품명',
    'itemDc': '품목구분', 'unitDc': '단위', 'poQt': '발주수량', 'isuQt': '출하수량',
    'whNm': '창고', 'lcNm': '로케이션', 'poNb': '발주번호',
    'itemparentCd': '모품번', 'itemparentNm': '모품명',
    'remarkDc': '비고', 'insertDt': '등록일시',
    '거래처명': '거래처명', '거래처코드': '거래처코드', '출하일자': '출하일자',
    '부서명': '부서명', '담당자': '담당자',
}

ISU_COL_RENAME = {
    'isuNb': '출고번호', 'isuSq': '순번', 'itemCd': '품번', 'itemNm': '품명',
    'itemDc': '품목구분', 'unitDc': '단위', 'isuQt': '출고수량',
    'fwhNm': '출고창고', 'flcNm': '출고위치', 'twhNm': '입고창고', 'tlcNm': '입고위치',
    'itemparentCd': '모품번', 'itemparentNm': '모품명',
    'remarkDc': '비고', 'insertDt': '등록일시',
    '출고일자': '출고일자', '부서명': '부서명', '담당자': '담당자',
}


if __name__ == '__main__':
    print("=" * 50)
    print("  아마란스10 출하/출고 데이터 수집")
    print(f"  회사: {CO_CD} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    # 1. 출하(매출)
    print("\n[1/2] 출하(매출) 수집")
    ship_items = fetch_shipments(date_from='20260301')
    save_csv(ship_items, '출하정보', SHIP_COL_RENAME)

    # 2. 출고(자재이동)
    print("\n[2/2] 출고(자재이동) 수집")
    isu_items = fetch_issues(date_from='20260301')
    save_csv(isu_items, '출고정보', ISU_COL_RENAME)

    print("\n" + "=" * 50)
    print(f"  출하(매출): {len(ship_items)}건")
    print(f"  출고(자재): {len(isu_items)}건")
    print("  챗봇 서버 재시작 시 자동 로드됩니다.")
    print("=" * 50)

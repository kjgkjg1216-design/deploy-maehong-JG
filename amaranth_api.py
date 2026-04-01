"""
더존 아마란스10 API 통합 클라이언트
- 어떤 API 경로든 call_api(path, body)로 호출 가능
- 인증(wehago-sign) 자동 처리
"""
import os
import json
import time
import hmac
import hashlib
import base64
import secrets
import urllib3
import requests
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv('C:/Users/jgkim/maehong-JG/.env')

ACCESS_TOKEN = os.getenv('AMARANTH_ACCESS_TOKEN', '').strip()
HASH_KEY     = os.getenv('AMARANTH_HASH_KEY', '').strip()
GROUP_SEQ    = os.getenv('AMARANTH_GROUP_SEQ', '').strip()
CALLER_NAME  = os.getenv('AMARANTH_CALLER_NAME', '').strip()

BASE_URL = 'https://gwa.maehong.kr'
CO_CD    = '1000'  # (주)매홍엘앤에프


# ────────────────────────────────────────────
# 핵심 함수 — 모든 API 공통
# ────────────────────────────────────────────
def make_wehago_sign(access_token, transaction_id, timestamp, url_path):
    """wehago-sign 생성: Base64(HMAC-SHA256(HASH_KEY, AT+tid+ts+path))"""
    value = access_token + transaction_id + timestamp + url_path
    signature = hmac.new(
        HASH_KEY.encode('utf-8'),
        value.encode('utf-8'),
        hashlib.sha256
    ).digest()
    return base64.b64encode(signature).decode('utf-8')


def call_api(api_path, body=None, method='POST', verbose=True):
    """아마란스10 API 범용 호출 — 경로만 넣으면 인증 자동 처리

    사용법:
        call_api('/apiproxy/api20A02S00102', {'coCd': '1000', 'poNb': 'PO2603000067'})
        call_api('/apiproxy/api20A03I01201', {'coCd': '1000', ...})
    """
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

    if verbose:
        print(f"[API] {method} {api_path}")
        if body:
            print(f"  Body: {json.dumps(body, ensure_ascii=False)[:200]}")

    try:
        if method.upper() == 'GET':
            resp = requests.get(url, headers=headers, timeout=30, verify=False)
        else:
            resp = requests.post(url, json=body or {}, headers=headers, timeout=30, verify=False)

        data = resp.json()

        if verbose:
            rc = data.get('resultCode', '')
            rm = data.get('resultMsg', '')
            rd = data.get('resultData')
            count = len(rd) if isinstance(rd, list) else ('있음' if rd else '없음')
            print(f"  → code={rc} msg={rm} data={count}")

        return data

    except Exception as e:
        if verbose:
            print(f"  → 오류: {e}")
        return None


# ────────────────────────────────────────────
# 편의 함수 (자주 쓰는 API)
# ────────────────────────────────────────────
def 발주정보_조회(po_nb, co_cd=CO_CD, verbose=True):
    """발주정보 디테일 내역 조회"""
    return call_api('/apiproxy/api20A02S00102',
                    {'coCd': co_cd, 'poNb': po_nb}, verbose=verbose)

def 발주정보_등록(data, co_cd=CO_CD):
    """발주정보 등록"""
    return call_api('/apiproxy/api20A02I00101', data)

def 발주정보_디테일_등록(data, co_cd=CO_CD):
    """발주정보 디테일 추가 등록"""
    return call_api('/apiproxy/api20A02I00102', data)

def 발주정보_삭제(data, co_cd=CO_CD):
    """발주정보 삭제"""
    return call_api('/apiproxy/api20A02D00101', data)

def 외주발주_등록(data, co_cd=CO_CD):
    """외주발주정보 등록"""
    return call_api('/apiproxy/api20A03I01201', data)

def 외주발주_디테일_등록(data, co_cd=CO_CD):
    """외주발주정보 디테일 추가 등록"""
    return call_api('/apiproxy/api20A03I01202', data)

def 청구요청_조회(data, co_cd=CO_CD):
    """청구요청등록 디테일 내역 조회"""
    return call_api('/apiproxy/api20A02S02402', data)

def 청구요청_등록(data, co_cd=CO_CD):
    """청구요청 정보 등록"""
    return call_api('/apiproxy/api20A02I02401', data)


# ────────────────────────────────────────────
# 테스트 / 사용 예시
# ────────────────────────────────────────────
if __name__ == '__main__':
    print("=" * 50)
    print("  아마란스10 API 통합 클라이언트")
    print(f"  회사: {CO_CD} / 그룹: {GROUP_SEQ}")
    print("=" * 50)

    # 예시 1: 발주정보 조회 (편의 함수)
    print("\n[예시1] 발주정보 조회")
    r = 발주정보_조회('PO2603000067')
    if r and r.get('resultData'):
        for item in r['resultData'][:3]:
            print(f"  품번:{item['itemCd']} | 품명:{item['itemNm']} | "
                  f"수량:{item['poQt']} | 금액:{item.get('poghAm1','')}")

    # 예시 2: 어떤 API든 직접 호출 (경로만 넣으면 됨)
    print("\n[예시2] 범용 호출 - 아무 API 경로")
    call_api('/apiproxy/api20A02S02402', {'coCd': CO_CD})

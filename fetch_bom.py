"""
아마란스10 API에서 BOM 데이터를 수집하여 CSV로 저장
- G코드: 자사제품 BOM정전개
- H코드: 외주생산제품 BOM
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


def fetch_item_master():
    """품목 마스터에서 G/H/I 코드 목록 가져오기"""
    print("[1/3] 품목 마스터 조회 (api20A03S00301)")
    r = call_api('/apiproxy/api20A03S00301', {'coCd': CO_CD})
    if not r or r.get('resultCode') != 0:
        print("  품목 마스터 조회 실패")
        return []

    items = r.get('resultData', [])
    print(f"  전체 품목: {len(items)}건")

    # G/H/I 코드만 추출
    target = [i for i in items if str(i.get('itemCd', '')).startswith(('G', 'H', 'I'))]
    print(f"  G/H/I 코드: {len(target)}건")
    return target


def fetch_bom_data(item_codes):
    """BOM 조회 API (api20A00S01001)로 전체 BOM 수집"""
    print(f"\n[2/3] BOM 수집 ({len(item_codes)}개 품번 조회)")
    all_bom = []
    hit_count = 0

    for i, code in enumerate(item_codes):
        r = call_api('/apiproxy/api20A00S01001', {
            'coCd': CO_CD,
            'itemparentCd': code,
        })

        if r and r.get('resultCode') == 0 and r.get('resultData'):
            bom_items = r['resultData']
            all_bom.extend(bom_items)
            hit_count += 1
            sys.stdout.write(f"\r  [{i+1}/{len(item_codes)}] {code}: +{len(bom_items)}건 (누적 {len(all_bom)}건, BOM있는 품번 {hit_count}개)")
            sys.stdout.flush()
        else:
            if (i + 1) % 50 == 0:
                sys.stdout.write(f"\r  [{i+1}/{len(item_codes)}] 스캔중... (누적 {len(all_bom)}건, BOM있는 품번 {hit_count}개)")
                sys.stdout.flush()

        time.sleep(0.2)

    print(f"\n  BOM 수집 완료: {len(all_bom)}건 (BOM 등록 품번 {hit_count}개)")
    return all_bom


def fetch_item_prices():
    """품목단가정보 조회 (api20A00S01401) → {품번: 매입단가} 딕셔너리"""
    print("\n[단가] 품목단가정보 조회 (api20A00S01401)")
    r = call_api('/apiproxy/api20A00S01401', {'coCd': CO_CD})
    if not r or r.get('resultCode') != 0:
        print("  단가 조회 실패")
        return {}

    items = r.get('resultData', [])
    price_map = {}
    for item in items:
        code = str(item.get('itemCd', '')).strip()
        purch = item.get('purchUm', 0) or 0
        sale = item.get('saleUm', 0) or 0
        std = item.get('standardUm', 0) or 0
        # 매입단가 우선, 없으면 기준단가, 없으면 판매단가
        price = float(purch) if purch else (float(std) if std else float(sale))
        if code:
            price_map[code] = price

    print(f"  단가 {len(price_map)}건 로드")
    return price_map


def save_bom_csv(bom_data, price_map=None):
    """BOM 데이터를 CSV로 저장"""
    if not bom_data:
        print("[저장] BOM 데이터 없음")
        return None

    df = pd.DataFrame(bom_data)

    col_rename = {
        'itemparentCd': '모품번',
        'itemparentNm': '모품명',
        'itemparentDc': '모품목구분',
        'itemparentUnitDc': '모품단위',
        'bomSq': 'BOM순번',
        'sortSq': '정렬순번',
        'itemchildCd': '자품번',
        'itemchildNm': '자품명',
        'itemchildDc': '자품목구분',
        'itemchildUnitDc': '자품단위',
        'justQt': '정미수량',
        'lossRt': '로스율',
        'realQt': '실소요량',
        'acctFgNm': '계정구분',
        'useYnNm': '사용여부',
        'outFgNm': '유무상구분',
        'startDt': '시작일',
        'endDt': '종료일',
    }

    rename_map = {k: v for k, v in col_rename.items() if k in df.columns}
    df = df.rename(columns=rename_map)

    # 단가 매칭 (자품번 기준)
    if price_map and '자품번' in df.columns:
        df['자재단가'] = df['자품번'].map(lambda x: price_map.get(str(x).strip(), 0))
        # 소요비용 = 실소요량 × 자재단가
        if '실소요량' in df.columns:
            df['소요비용'] = df.apply(
                lambda r: round(float(r.get('실소요량', 0) or 0) * float(r.get('자재단가', 0) or 0), 2),
                axis=1
            )
        print(f"  단가 매칭: {(df['자재단가'] > 0).sum()}/{len(df)}건")

    # 주요 컬럼 순서
    priority = [v for v in col_rename.values() if v in df.columns]
    extra = ['자재단가', '소요비용']
    extra = [c for c in extra if c in df.columns]
    others = [c for c in df.columns if c not in priority and c not in extra]
    df = df[priority + extra + others]

    today = datetime.now().strftime('%Y%m%d')
    output = f'C:/Users/jgkim/maehong-JG/data/{today}_BOM.csv'
    df.to_csv(output, index=False, encoding='utf-8-sig')
    print(f"\n[3/3] 저장 완료: {output} ({len(df)}건)")
    return output


if __name__ == '__main__':
    print("=" * 50)
    print("  아마란스10 BOM 데이터 수집")
    print(f"  회사: {CO_CD} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    # 품목 마스터에서 G/H/I 코드 추출
    items = fetch_item_master()
    if not items:
        print("품목 마스터 조회 실패, 종료")
        sys.exit(1)

    codes = sorted(set(str(i.get('itemCd', '')) for i in items if i.get('itemCd')))
    g_cnt = len([c for c in codes if c.startswith('G')])
    h_cnt = len([c for c in codes if c.startswith('H')])
    i_cnt = len([c for c in codes if c.startswith('I')])
    print(f"  G코드: {g_cnt}개 | H코드: {h_cnt}개 | I코드: {i_cnt}개")

    # BOM 수집
    bom_data = fetch_bom_data(codes)

    # 단가 수집
    price_map = fetch_item_prices()

    # CSV 저장 (단가 포함)
    save_bom_csv(bom_data, price_map)

    # 요약
    if bom_data:
        df = pd.DataFrame(bom_data)
        g_bom = len(df[df['itemparentCd'].str.startswith('G')])
        h_bom = len(df[df['itemparentCd'].str.startswith('H')])
        i_bom = len(df[df['itemparentCd'].str.startswith('I')])
        parents = df['itemparentCd'].nunique()
        print(f"\n  BOM 등록 품번: {parents}개")
        print(f"  G코드 BOM: {g_bom}건 | H코드 BOM: {h_bom}건 | I코드 BOM: {i_bom}건")
        print("  챗봇 서버 재시작 시 자동 로드됩니다.")

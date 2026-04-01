"""
아마란스10 API에서 생산지시 + 생산실적 데이터를 수집하여 CSV로 저장
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

def make_wehago_sign(at, tid, ts, path):
    return base64.b64encode(hmac.new(HASH_KEY.encode(), (at+tid+ts+path).encode(), hashlib.sha256).digest()).decode()

def call_api(api_path, body=None):
    tid = secrets.token_hex(16)
    ts = str(int(time.time()))
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {ACCESS_TOKEN}',
        'transaction-id': tid, 'timestamp': ts,
        'CallerName': CALLER_NAME, 'groupSeq': GROUP_SEQ,
        'wehago-sign': make_wehago_sign(ACCESS_TOKEN, tid, ts, api_path),
    }
    try:
        return requests.post(f"{BASE_URL}{api_path}", json=body or {}, headers=headers, timeout=15, verify=False).json()
    except:
        return None

def save_csv(items, name, col_rename=None):
    if not items:
        print(f"[{name}] 데이터 없음")
        return None
    df = pd.DataFrame(items)
    if col_rename:
        df = df.rename(columns={k:v for k,v in col_rename.items() if k in df.columns})
    today = datetime.now().strftime('%Y%m%d')
    output = f'C:/Users/jgkim/maehong-JG/data/{today}_{name}.csv'
    df.to_csv(output, index=False, encoding='utf-8-sig')
    print(f"[저장] {output} ({len(df)}건)")
    return output

WO_RENAME = {
    'woCd': '생산지시번호', 'ordDt': '지시일자', 'compDt': '완료예정일',
    'itemCd': '품번', 'itemNm': '품명', 'itemDc': '품목구분', 'unitDc': '단위',
    'itemQt': '지시수량', 'routingNm': '공정명',
    'trNm': '거래처명', 'plnNm': '담당자',
    'remarkDc': '비고', 'insertDt': '등록일시',
}

WR_RENAME = {
    'wrCd': '실적번호', 'woCd': '생산지시번호', 'wrDt': '실적일자',
    'workQt': '작업수량', 'goodQt': '양품수량', 'badQt': '불량수량', 'moveQt': '이동수량',
    'movebaselocNm': '이동창고', 'movelocNm': '이동위치',
    'plnNm': '담당자', 'equipNm': '설비',
    'remarkDc': '비고', 'insertDt': '등록일시',
}

if __name__ == '__main__':
    print("=" * 50)
    print("  아마란스10 생산실적 수집")
    print(f"  회사: {CO_CD} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    # 1. 생산지시
    print("\n[1/2] 생산지시 수집 (api20A03S00801)")
    r1 = call_api('/apiproxy/api20A03S00801', {
        'coCd': CO_CD, 'woDtFrom': '20260301', 'woDtTo': datetime.now().strftime('%Y%m%d'),
    })
    wo_items = r1.get('resultData', []) if r1 and r1.get('resultCode') == 0 else []
    print(f"  생산지시: {len(wo_items)}건")
    save_csv(wo_items, '생산지시', WO_RENAME)

    # 2. 생산실적
    print("\n[2/2] 생산실적 수집 (api20A03S00901)")
    r2 = call_api('/apiproxy/api20A03S00901', {
        'coCd': CO_CD, 'wrDtFrom': '20260301', 'wrDtTo': datetime.now().strftime('%Y%m%d'),
    })
    wr_items = r2.get('resultData', []) if r2 and r2.get('resultCode') == 0 else []
    print(f"  생산실적: {len(wr_items)}건")

    # 생산지시의 품번/품명을 실적에 병합 (실적에는 품번이 없음)
    wo_map = {}
    for item in wo_items:
        wo_map[item.get('woCd', '')] = {
            '품번': item.get('itemCd', ''),
            '품명': item.get('itemNm', ''),
            '품목구분': item.get('itemDc', ''),
            '지시수량': item.get('itemQt', ''),
        }
    for item in wr_items:
        wo_info = wo_map.get(item.get('woCd', ''), {})
        item['품번'] = wo_info.get('품번', '')
        item['품명'] = wo_info.get('품명', '')
        item['품목구분'] = wo_info.get('품목구분', '')
        item['지시수량'] = wo_info.get('지시수량', '')

    save_csv(wr_items, '생산실적', WR_RENAME)

    print(f"\n  생산지시: {len(wo_items)}건 | 생산실적: {len(wr_items)}건")
    print("  챗봇 서버 재시작 시 자동 로드됩니다.")

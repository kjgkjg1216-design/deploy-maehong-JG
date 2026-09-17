"""
아마란스10 전체 데이터 통합 수집 (25년6월 ~ 현재)
구매발주, 외주발주, 생산실적, 출하, 출고, BOM
"""
import os, sys, json, time, hmac, hashlib, base64, secrets, urllib3, requests
import glob as glob_mod
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
BASE_DIR = os.environ.get('APP_BASE_DIR', 'C:/Users/jgkim/maehong-JG')
DATA_DIR = os.environ.get('DATA_DIR', BASE_DIR + '/data')
load_dotenv(os.path.join(BASE_DIR, '.env'))

ACCESS_TOKEN = os.getenv('AMARANTH_ACCESS_TOKEN', '').strip()
HASH_KEY     = os.getenv('AMARANTH_HASH_KEY', '').strip()
GROUP_SEQ    = os.getenv('AMARANTH_GROUP_SEQ', '').strip()
CALLER_NAME  = os.getenv('AMARANTH_CALLER_NAME', '').strip()
BASE_URL = 'https://gwa.maehong.kr'
CO_CD    = '1000'
DATE_FROM = '20250601'  # 수집 시작일

def make_sign(at, tid, ts, path):
    return base64.b64encode(hmac.new(HASH_KEY.encode(), (at+tid+ts+path).encode(), hashlib.sha256).digest()).decode()

def call_api(api_path, body=None, retries=3, backoff=0.6):
    """아마란스 API 호출 — 일시적 실패는 재시도.

    ⚠️ 재시도가 없으면 헤더+디테일 수집에서 디테일 호출이 한 번 타임아웃할 때
    해당 레코드가 조용히 누락된다. 매 수집마다 다른 행이 빠져 CSV가 출렁이고
    (→ 클라우드 재업로드·화면 새로고침 반복), 데이터 완전성도 훼손된다.
    성공(resultCode 0) 시 즉시 반환, 실패 시 지수 백오프로 재시도.
    'resultData 없음'인 정상 빈 응답(resultCode 0)은 재시도하지 않는다.
    """
    last = None
    for attempt in range(retries):
        tid = secrets.token_hex(16)
        ts = str(int(time.time()))
        headers = {
            'Content-Type': 'application/json', 'Authorization': f'Bearer {ACCESS_TOKEN}',
            'transaction-id': tid, 'timestamp': ts, 'CallerName': CALLER_NAME,
            'groupSeq': GROUP_SEQ, 'wehago-sign': make_sign(ACCESS_TOKEN, tid, ts, api_path),
        }
        try:
            j = requests.post(f"{BASE_URL}{api_path}", json=body or {},
                              headers=headers, timeout=30, verify=False).json()
            if j is not None and j.get('resultCode') == 0:
                return j            # 정상(빈 결과 포함) — 재시도 불필요
            last = j                # resultCode != 0 → 재시도 대상
        except Exception:
            last = None             # 타임아웃/네트워크 오류 → 재시도 대상
        if attempt < retries - 1:
            time.sleep(backoff * (2 ** attempt))   # 0.6s, 1.2s
    return last                     # 끝내 실패 시 마지막 응답(None/오류) 반환 — 호출부가 스킵

def save_csv(items, name, col_rename=None):
    if not items:
        # 빈 결과 = 수집 실패(타임아웃 kill 등) 가능성. 조용히 넘기면 옛 파일이
        # 남아 누락이 은폐됨 → stderr로 눈에 띄게 경고.
        print(f"  [경고] [{name}] 데이터 없음 — 저장 건너뜀 (직전 파일 유지). 수집 실패 의심!",
              file=sys.stderr, flush=True)
        print(f"  [{name}] 데이터 없음 — 저장 건너뜀 (직전 파일 유지)")
        return
    df = pd.DataFrame(items)
    if col_rename:
        df = df.rename(columns={k:v for k,v in col_rename.items() if k in df.columns})
    today = datetime.now().strftime('%Y%m%d')
    path = f'{DATA_DIR}/{today}_{name}.csv'
    from _safe_csv import safe_to_csv
    safe_to_csv(df, path, label=name)


# ── 증분 수집 (헤더+디테일 대용량 타입) ─────────────────
# 전체기간(15개월+) 재수집은 회당 75분을 넘겨 마지막 단계(출고)가 매번
# 타임아웃 kill되던 근본 원인. ERP 마감된 옛 달은 사실상 불변이므로
# 최근 INCR_MONTHS개월만 재수집하고 그 이전 행은 직전 CSV에서 승계한다.
# 전체 재수집이 필요하면 env FETCH_FULL=1 로 실행.
INCR_MONTHS = 3   # 이번달 포함 최근 3개월 재수집 (역기재 수정 여유 포함)


def incr_cutoff():
    """증분 재수집 시작일(YYYYMM01). FETCH_FULL=1이면 None(전체)."""
    if os.environ.get('FETCH_FULL', '') == '1':
        return None
    from datetime import date
    t = date.today()
    y, m = t.year, t.month - (INCR_MONTHS - 1)
    while m <= 0:
        m += 12
        y -= 1
    return f'{y}{m:02d}01'


def save_csv_incr(items, name, col_rename, date_col, cutoff):
    """cutoff 이전 행은 직전 CSV에서 승계 + 신규 수집분(≥cutoff) 결합 저장.
    cutoff가 None이면 전체 수집분 그대로 저장(save_csv와 동일)."""
    if cutoff is None or not items:
        save_csv(items, name, col_rename)
        return
    df_new = pd.DataFrame(items)
    if col_rename:
        df_new = df_new.rename(columns={k: v for k, v in col_rename.items() if k in df_new.columns})
    prev_files = sorted(glob_mod.glob(f'{DATA_DIR}/*_{name}.csv'), reverse=True)
    kept = None
    if prev_files:
        try:
            prev = pd.read_csv(prev_files[0], dtype=str, keep_default_na=False,
                               encoding='utf-8-sig')
            if date_col in prev.columns and list(prev.columns) == [str(c) for c in df_new.columns]:
                d = prev[date_col].astype(str).str.replace('-', '', regex=False).str[:8]
                kept = prev[d < cutoff]
            else:
                print(f"  [경고] [{name}] 직전 CSV 스키마 불일치 → 신규 수집분만 저장"
                      f" (옛 데이터 승계 불가)", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"  [경고] [{name}] 직전 CSV 읽기 실패({e!r}) → 신규 수집분만 저장",
                  file=sys.stderr, flush=True)
    if kept is not None and len(kept):
        df_new = df_new.astype(str)
        combined = pd.concat([kept, df_new], ignore_index=True)
        print(f"  [{name}] 증분 결합: 승계 {len(kept):,}행(<{cutoff}) + 신규 {len(df_new):,}행"
              f" = {len(combined):,}행")
    else:
        combined = df_new
    today = datetime.now().strftime('%Y%m%d')
    from _safe_csv import safe_to_csv
    safe_to_csv(combined, f'{DATA_DIR}/{today}_{name}.csv', label=name)

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
                        detail_key='poNb', header_merge_fields=None, label='',
                        start_from=None):
    """헤더+디테일 패턴의 API를 월별로 수집. start_from 지정 시 그날부터(증분)."""
    all_details = []
    failed_keys = []   # 재시도 후에도 디테일 수집 실패한 헤더 (행 누락 → 경고)
    months = month_ranges(start_from or DATE_FROM)

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
            if dr and dr.get('resultCode') == 0:
                details = dr.get('resultData') or []   # resultCode 0 = 성공(빈 디테일 허용)
                if header_merge_fields:
                    for d in details:
                        for field, src in header_merge_fields.items():
                            d[field] = h.get(src, '')
                all_details.extend(details)
            else:
                # 재시도(call_api 내부 3회)까지 실패 → 이 헤더 행이 누락됨. 조용히 넘기면
                # 수집마다 다른 행이 빠져 CSV가 출렁이므로 눈에 띄게 남긴다.
                failed_keys.append(key_val)
            time.sleep(0.2)

        sys.stdout.write(f"\r  {label} {dt_from[:6]}: 헤더 {len(headers_data)}건 → 누적 {len(all_details)}건\n")
        sys.stdout.flush()
        time.sleep(0.5)

    if failed_keys:
        sample = ', '.join(failed_keys[:10]) + ('...' if len(failed_keys) > 10 else '')
        print(f"  [경고] [{label}] 디테일 수집 실패 {len(failed_keys)}건 → 해당 행 누락"
              f" (재시도 후에도 실패): {sample}", file=sys.stderr, flush=True)
    print(f"  [{label} 완료] {len(all_details)}건"
          + (f"  (누락 {len(failed_keys)}건)" if failed_keys else ""))
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

# 생산지시 저장용 — fetch_production.py의 WO_RENAME과 동일 스키마 유지(파일 호환).
WO_RENAME = {
    'woCd': '생산지시번호', 'ordDt': '지시일자', 'compDt': '완료예정일',
    'itemCd': '품번', 'itemNm': '품명', 'itemDc': '품목구분', 'unitDc': '단위',
    'itemQt': '지시수량', 'routingNm': '공정명',
    'trNm': '거래처명', 'plnNm': '담당자',
    'remarkDc': '비고', 'insertDt': '등록일시',
}


if __name__ == '__main__':
    print("=" * 60)
    print(f"  아마란스10 전체 데이터 수집 ({DATE_FROM} ~ 현재)")
    print(f"  회사: {CO_CD} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    cutoff = incr_cutoff()
    print(f"  수집 모드: {'전체(' + DATE_FROM + '~)' if cutoff is None else '증분(' + cutoff + '~, 이전은 직전 CSV 승계)'}")

    # 1. 구매발주 (헤더+디테일)
    print("\n[1/7] 구매발주")
    po_items = fetch_header_detail(
        '/apiproxy/api20A02S00101', '/apiproxy/api20A02S00102',
        'poDtFrom', 'poDtTo', detail_key='poNb',
        header_merge_fields={'거래처명':'attrNm','거래처코드':'trCd','발주일자':'poDt'},
        label='구매발주', start_from=cutoff
    )
    save_csv_incr(po_items, '발주정보', PO_RENAME, '발주일자', cutoff)

    # 2. 외주발주 (헤더+디테일)
    print("\n[2/7] 외주발주")
    wp_items = fetch_header_detail(
        '/apiproxy/api20A03S01201', '/apiproxy/api20A03S01202',
        'poDtFrom', 'poDtTo', detail_key='poNb',
        header_merge_fields={'거래처명':'attrNm','거래처코드':'trCd','발주일자':'poDt','부서명':'deptNm','담당자':'korNm'},
        label='외주발주', start_from=cutoff
    )
    save_csv_incr(wp_items, '외주발주정보', WP_RENAME, '발주일자', cutoff)

    # 3. 생산지시
    print("\n[3/7] 생산지시+실적")
    wo_items = fetch_simple('/apiproxy/api20A03S00801', 'woDtFrom', 'woDtTo', '생산지시')
    wo_map = {i.get('woCd',''): {'품번':i.get('itemCd',''),'품명':i.get('itemNm',''),'품목구분':i.get('itemDc',''),'지시수량':i.get('itemQt','')} for i in wo_items}
    # 생산지시 CSV 저장 — 자동 파이프라인에 포함(이전엔 실적 보강용으로만 쓰고 저장 안 해
    # 생산지시가 fetch_production 수동실행 때만 갱신돼 고착됐음).
    save_csv(wo_items, '생산지시', WO_RENAME)

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
        label='출하', start_from=cutoff
    )
    save_csv_incr(ship_items, '출하정보', SHIP_RENAME, '출하일자', cutoff)

    # 6. 입고 (헤더+디테일)
    print("\n[6/7] 입고")
    rcv_items = fetch_header_detail(
        '/apiproxy/api20A02S00201', '/apiproxy/api20A02S00202',
        'rcvDtFrom', 'rcvDtTo', detail_key='rcvNb',
        header_merge_fields={'거래처명':'attrNm','거래처코드':'trCd','입고일자':'rcvDt','입고창고':'whNm'},
        label='입고', start_from=cutoff
    )
    save_csv_incr(rcv_items, '입고정보', RCV_RENAME, '입고일자', cutoff)

    # 7. 출고 (헤더+디테일)
    print("\n[7/7] 출고(자재이동)")
    isu_items = fetch_header_detail(
        '/apiproxy/api20A02S00801', '/apiproxy/api20A02S00802',
        'isuDtFrom', 'isuDtTo', detail_key='isuNb',
        header_merge_fields={'출고일자':'isuDt','부서명':'deptNm','담당자':'korNm'},
        label='출고', start_from=cutoff
    )
    save_csv_incr(isu_items, '출고정보', ISU_RENAME, '출고일자', cutoff)

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

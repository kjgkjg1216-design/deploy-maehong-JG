"""
완제품 재고 챗봇 - RAG 기반 Flask 앱
데이터 출처: 원자재부자재 재고파악(3월) - 최종본.xlsx
시트: daily _ 완제품 재고일지
"""
import os
import glob
import httpx
import pandas as pd
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from openai import OpenAI
import firebase_admin
from firebase_admin import credentials, firestore as fs_admin, auth as fb_auth
from dotenv import load_dotenv

# .env 로드
load_dotenv('C:/Users/jgkim/maehong-JG/.env')

app = Flask(__name__)
CORS(app)

# Firebase Admin SDK 초기화
if not firebase_admin._apps:
    _fb_key = os.path.join(os.path.dirname(__file__), 'maehong-scm-firebase-adminsdk-fbsvc-8cae1845b3.json')
    if os.path.exists(_fb_key):
        _cred = credentials.Certificate(_fb_key)
        firebase_admin.initialize_app(_cred)
        print("[Firebase] 서비스 계정 키로 초기화 완료")
    else:
        firebase_admin.initialize_app()
        print("[Firebase] 기본 인증으로 초기화")
FIRESTORE_DB = fs_admin.client()

# 프록시/타임아웃 설정 - 연결 오류 방지
_http_client = httpx.Client(
    timeout=httpx.Timeout(60.0, connect=10.0),
    trust_env=False,   # 시스템 프록시 무시 (기업망 충돌 방지)
)
client = OpenAI(
    api_key=os.getenv('OPENAI_API_KEY'),
    http_client=_http_client,
)

# ────────────────────────────────────────────
# 데이터 로드 (CSV)
# ────────────────────────────────────────────
def load_inventory_data():
    inv_files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_재고일지.csv'), reverse=True)
    if not inv_files:
        raise FileNotFoundError("재고 CSV 파일을 찾을 수 없습니다. convert_excel.py를 먼저 실행하세요.")
    latest_csv = inv_files[0]
    print(f"[재고 로드] {latest_csv}")
    df = pd.read_csv(latest_csv, encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[재고 로드 완료] {len(df)}행 × {len(df.columns)}열")
    return df, latest_csv

def load_price_data():
    price_files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_단가.csv'), reverse=True)
    if not price_files:
        print("[단가] CSV 없음 → convert_price.py 실행 필요")
        return None
    print(f"[단가 로드] {price_files[0]}")
    df = pd.read_csv(price_files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[단가 로드 완료] {len(df)}개 품번")
    return df

def load_spec_data():
    spec_files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_부자재규격.csv'), reverse=True)
    if not spec_files:
        print("[부자재규격] CSV 없음 → convert_spec.py 실행 필요")
        return None
    print(f"[부자재규격 로드] {spec_files[0]}")
    df = pd.read_csv(spec_files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[부자재규격 로드 완료] {len(df)}개 항목")
    return df

def load_jasa_data():
    files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_자사재고.csv'), reverse=True)
    if not files:
        print("[자사재고] CSV 없음 → convert_jasa.py 실행 필요")
        return None
    print(f"[자사재고 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[자사재고 로드 완료] {len(df)}행")
    return df

def load_order_data():
    files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_발주정보.csv'), reverse=True)
    if not files:
        print("[발주정보] CSV 없음")
        return None
    print(f"[발주정보 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[발주정보 로드 완료] {len(df)}건")
    return df

def load_wp_order_data():
    files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_외주발주정보.csv'), reverse=True)
    if not files:
        print("[외주발주정보] CSV 없음")
        return None
    print(f"[외주발주정보 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[외주발주정보 로드 완료] {len(df)}건")
    return df

def load_production_data():
    files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_생산실적.csv'), reverse=True)
    if not files:
        print("[생산실적] CSV 없음")
        return None
    print(f"[생산실적 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[생산실적 로드 완료] {len(df)}건")
    return df

def load_shipment_data():
    files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_출하정보.csv'), reverse=True)
    if not files:
        print("[출하정보] CSV 없음")
        return None
    print(f"[출하정보 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[출하정보 로드 완료] {len(df)}건")
    return df

def load_issue_data():
    files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_출고정보.csv'), reverse=True)
    if not files:
        print("[출고정보] CSV 없음")
        return None
    print(f"[출고정보 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[출고정보 로드 완료] {len(df)}건")
    return df

def load_bom_data():
    files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_BOM.csv'), reverse=True)
    if not files:
        print("[BOM] CSV 없음")
        return None
    print(f"[BOM 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[BOM 로드 완료] {len(df)}건 (모품번 {df['모품번'].nunique()}개)")
    return df

def load_rcv_data():
    files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_입고정보.csv'), reverse=True)
    if not files:
        print("[입고정보] CSV 없음")
        return None
    print(f"[입고정보 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[입고정보 로드 완료] {len(df)}건")
    return df

DF, CSV_PATH = load_inventory_data()
PRICE_DF = load_price_data()
SPEC_DF = load_spec_data()
JASA_DF = load_jasa_data()
ORDER_DF = load_order_data()
WP_ORDER_DF = load_wp_order_data()
PROD_DF = load_production_data()

def load_work_order_data():
    files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_생산지시.csv'), reverse=True)
    if not files:
        print("[생산지시] CSV 없음")
        return None
    print(f"[생산지시 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[생산지시 로드 완료] {len(df)}건")
    return df

WO_DF = load_work_order_data()
SHIP_DF = load_shipment_data()
ISSUE_DF = load_issue_data()
BOM_DF = load_bom_data()
RCV_DF = load_rcv_data()

# Monday.com 데이터 로드
def load_monday_data():
    mon_files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_monday.csv'), reverse=True)
    if not mon_files:
        print("[Monday] CSV 없음")
        return None
    print(f"[Monday 로드] {mon_files[0]}")
    df = pd.read_csv(mon_files[0], encoding='utf-8-sig', dtype=str,
                     usecols=lambda c: c in ['보드ID','보드명','아이템ID','아이템명','그룹','생성일','수정일'])
    df = df.fillna('')
    print(f"[Monday 로드 완료] {len(df)}건, 보드 {df['보드명'].nunique()}개")
    return df

MONDAY_DF = load_monday_data()

# 컬럼 인덱스 기반 주요 컬럼 매핑 (터미널 인코딩 문제 회피)
COL = {name: idx for idx, name in enumerate(DF.columns)}
# 주요 컬럼명 (실제 데이터 컬럼 사용)
COL_납품처 = DF.columns[0]   # 납품처
COL_품목   = DF.columns[1]   # 품목코드
COL_규격   = DF.columns[2]   # 규격
COL_품명   = DF.columns[3]   # 품명
COL_원산지 = DF.columns[4]   # 원산지여부
COL_입수량 = DF.columns[5]   # 입수량
COL_pallet = DF.columns[6]   # pallet적재량
COL_창고재고 = DF.columns[7] # 창고재고량
COL_원가재고 = DF.columns[8] # 원가재고량
COL_재고량  = DF.columns[9]  # 재고량
COL_합계       = DF.columns[41]  # 합계
COL_생산부자재   = DF.columns[42] # 생산&부자재 사용 합계
COL_생산일수     = DF.columns[43] # 생산일수
COL_전월일평균   = DF.columns[44] # 전월 일평균필요량
COL_일평균필요량 = DF.columns[45] # 일평균필요량(전월기준)

# 요약용 핵심 컬럼
KEY_COLS = [COL_납품처, COL_품목, COL_규격, COL_품명, COL_원산지,
            COL_입수량, COL_창고재고, COL_원가재고, COL_재고량,
            COL_합계, COL_생산부자재, COL_생산일수, COL_전월일평균, COL_일평균필요량]

# 일별 컬럼 (3월01일 ~ 3월31일)
DAILY_COLS = [DF.columns[i] for i in range(10, 41)]

print(f"[컬럼 매핑 완료] 납품처={COL_납품처}, 품명={COL_품명}, 현재고량={COL_재고량}")

# ────────────────────────────────────────────
# 규격 표시 변환 (부재료 카테고리 통합)
# ────────────────────────────────────────────
# 부재료로 통칭할 규격값 목록
BUJAMYO_TYPES = {'부재료', '단상자', '물류박스'}

def display_규격(raw_val: str) -> str:
    """규격 원본값 → 표시용 문자열 변환
    부재료/단상자/물류박스 → 부재료(원본값)
    나머지는 그대로 반환
    """
    v = str(raw_val).strip()
    if v in BUJAMYO_TYPES:
        return f"부재료({v})"
    return v

def is_부재료(raw_val: str) -> bool:
    return str(raw_val).strip() in BUJAMYO_TYPES

# ────────────────────────────────────────────
# 단가 조회 함수 (품번 → 단가, 없으면 품명 유사도 매칭)
# ────────────────────────────────────────────
def _build_price_lookup():
    """품번 → 단가 딕셔너리 + 품명 역색인 구축"""
    if PRICE_DF is None:
        return {}, {}
    by_code = {}
    by_name = {}
    for _, row in PRICE_DF.iterrows():
        code = str(row.get('품번', '')).strip()
        name = str(row.get('품명', '')).strip()
        sheet = str(row.get('시트종류', '')).strip()
        trader = str(row.get('거래처', '')).strip()
        try:
            price = float(row.get('최신단가', '')) if str(row.get('최신단가', '')) not in ('', 'nan') else None
        except ValueError:
            price = None
        basis = str(row.get('기준년월', '')).strip()
        info = {'단가': price, '거래처': trader, '품명': name, '시트종류': sheet, '기준년월': basis}
        if code:
            by_code[code.upper()] = info
        if name:
            by_name[name.lower()] = {'code': code.upper(), **info}
    return by_code, by_name

PRICE_BY_CODE, PRICE_BY_NAME = _build_price_lookup()

def get_price_info(품번: str, 품명: str) -> dict | None:
    """품번 우선 매칭(대소문자 무관), 없으면 품명 부분 매칭"""
    code = str(품번).strip().upper()
    # ① 품번 정확 매칭 (대소문자 무관)
    if code and code in PRICE_BY_CODE:
        return PRICE_BY_CODE[code]
    # ② 품명 부분 매칭 (포함 여부)
    name_lower = str(품명).strip().lower()
    if name_lower:
        for key, info in PRICE_BY_NAME.items():
            if name_lower in key or key in name_lower:
                return info
    return None

print(f"[단가 조회] 품번 {len(PRICE_BY_CODE)}개, 품명 역색인 {len(PRICE_BY_NAME)}개")

# ────────────────────────────────────────────
# 부자재 규격 조회 함수
# ────────────────────────────────────────────
def _build_spec_lookup():
    """품번 → 규격정보 딕셔너리 + 업체명/납품처/품명 역색인 구축"""
    if SPEC_DF is None:
        return {}, {}, {}, {}
    by_code = {}    # 품번 → row dict
    by_name = {}    # 품명(lower) → row dict
    by_vendor = {}  # 외주업체명(lower) → list of row dicts
    by_dest = {}    # 납품처(lower) → list of row dicts
    for _, row in SPEC_DF.iterrows():
        code   = str(row.get('품번', '')).strip()
        name   = str(row.get('품명', '')).strip()
        vendor = str(row.get('외주업체명', '')).strip()
        dest   = str(row.get('납품처', '')).strip()
        info = {
            '외주업체명':   vendor,
            '품번':         code,
            '납품처':       dest,
            '품명':         name,
            '규격(사이즈)': str(row.get('규격(사이즈)', '')).strip(),
            '재질':         str(row.get('재질', '')).strip(),
            'MOQ':          str(row.get('MOQ', '')).strip(),
            '단가(원)':     str(row.get('단가(원)', '')).strip(),
            '중량(g)':      str(row.get('중량(g)', '')).strip(),
        }
        if code:
            by_code[code.upper()] = info
        if name:
            by_name[name.lower()] = info
        if vendor:
            vk = vendor.lower()
            if vk not in by_vendor:
                by_vendor[vk] = []
            by_vendor[vk].append(info)
        if dest:
            dk = dest.lower()
            if dk not in by_dest:
                by_dest[dk] = []
            by_dest[dk].append(info)
    return by_code, by_name, by_vendor, by_dest

SPEC_BY_CODE, SPEC_BY_NAME, SPEC_BY_VENDOR, SPEC_BY_DEST = _build_spec_lookup()

def get_spec_info(품번: str, 품명: str = '') -> dict | None:
    """품번 우선 매칭(대소문자 무관) → 품명 부분 매칭"""
    code = str(품번).strip().upper()
    if code and code in SPEC_BY_CODE:
        return SPEC_BY_CODE[code]
    name_lower = str(품명).strip().lower()
    if name_lower:
        for key, info in SPEC_BY_NAME.items():
            if name_lower in key or key in name_lower:
                return info
    return None

def search_spec_by_query(query_lower: str) -> list:
    """쿼리에서 납품처/업체명/품번/품명으로 규격 행 검색 (행 제한 없음)"""
    if SPEC_DF is None:
        return []
    results = []
    seen = set()

    def _add(info):
        key = (info.get('품번',''), info.get('품명',''))
        if key not in seen:
            seen.add(key)
            results.append(info)

    # 납품처 매칭 (SPEC_DF의 납품처 컬럼)
    for dk, rows in SPEC_BY_DEST.items():
        if dk in query_lower:
            for r in rows:
                _add(r)
    # 업체명 매칭 (정방향 + 역방향 부분 매칭)
    q_tokens = [k.strip('.,;:!?()[]') for k in query_lower.split() if len(k) >= 2]
    for vk, rows in SPEC_BY_VENDOR.items():
        matched = vk in query_lower  # 정방향: '동원시스템즈' in query
        if not matched:
            # 역방향: 쿼리 토큰이 업체명에 포함 (e.g., '동원' in '동원시스템즈')
            for tok in q_tokens:
                # 조사 제거
                t = tok
                for p in ['에서', '으로', '한테', '로', '에', '의', '은', '는', '이', '가', '을', '를', '도']:
                    if t.endswith(p) and len(t) > len(p) + 1:
                        t = t[:-len(p)]
                        break
                if len(t) >= 2 and t in vk:
                    matched = True
                    break
        if matched:
            for r in rows:
                _add(r)
    # 품번 코드 매칭
    import re as _re2
    for code_match in _re2.findall(r'[A-Za-z]\d{3,}', query_lower):
        cu = code_match.upper()
        if cu in SPEC_BY_CODE:
            _add(SPEC_BY_CODE[cu])
    # 품명 키워드 매칭 (앞의 매칭 결과 없을 때)
    if not results:
        for key, info in SPEC_BY_NAME.items():
            for word in query_lower.split():
                if len(word) >= 2 and word in key:
                    _add(info)
    return results

def format_spec_row(info: dict) -> str:
    parts = []
    for k in ['외주업체명', '품번', '납품처', '품명', '규격(사이즈)', '재질', 'MOQ', '단가(원)', '중량(g)']:
        v = info.get(k, '')
        if v and v not in ('', 'nan'):
            parts.append(f"{k}: {v}")
    return ' | '.join(parts)

_spec_count = len(SPEC_BY_CODE) if SPEC_BY_CODE else 0
_spec_vendors = sorted(set(v.get('외주업체명','') for v in SPEC_BY_CODE.values() if v.get('외주업체명'))) if SPEC_BY_CODE else []
print(f"[부자재규격 조회] 품번 {_spec_count}개, 업체 {len(_spec_vendors)}개")

# ────────────────────────────────────────────
# 정적 메타데이터 (서버 시작 시 1회 계산 → 항상 시스템 메시지에 포함)
# ────────────────────────────────────────────
def _unique_vals(col):
    return sorted(DF[col].replace('', pd.NA).dropna().unique().tolist())

def _num(v):
    try:
        return float(v)
    except Exception:
        return 0.0

# 외주업체별 현재고량 + 재고비용 합계 (pre-computed)
def _vendor_stock_summary():
    lines = []
    for vendor in _unique_vals(COL_원산지):
        rows = DF[DF[COL_원산지] == vendor]
        stock_total = sum(_num(v) for v in rows[COL_재고량] if v not in ('', 'nan'))
        cost_total  = sum(
            _num(r[COL_재고량]) * (get_price_info(r[COL_품목], r[COL_품명]) or {}).get('단가', 0) or 0
            for _, r in rows.iterrows()
        )
        count = len(rows)
        cost_str = f"{cost_total:,.0f}원" if cost_total else "단가정보없음"
        lines.append(f"- {vendor}: 현재고량 {stock_total:,.0f} | 재고비용 {cost_str} (항목 {count}개)")
    return '\n'.join(lines)

# 납품처별 현재고량 + 재고비용 합계 (pre-computed)
def _dest_stock_summary():
    lines = []
    for dest in _unique_vals(COL_납품처):
        rows = DF[DF[COL_납품처] == dest]
        stock_total = sum(_num(v) for v in rows[COL_재고량] if v not in ('', 'nan'))
        cost_total  = sum(
            _num(r[COL_재고량]) * (get_price_info(r[COL_품목], r[COL_품명]) or {}).get('단가', 0) or 0
            for _, r in rows.iterrows()
        )
        count = len(rows)
        cost_str = f"{cost_total:,.0f}원" if cost_total else "단가정보없음"
        lines.append(f"- {dest}: 현재고량 {stock_total:,.0f} | 재고비용 {cost_str} (항목 {count}개)")
    return '\n'.join(lines)

VENDOR_STOCK_TEXT = _vendor_stock_summary()
DEST_STOCK_TEXT   = _dest_stock_summary()

# 외주업체별 부재료 품목별 재고 (pre-computed) - 125개 전체 포함
def _vendor_bujamyo_detail():
    """외주업체별로 부재료(부재료/단상자/물류박스) 품목 전체 목록 + 재고량"""
    bj_df = DF[DF[COL_규격].isin(BUJAMYO_TYPES)]
    sections = []
    for vendor in _unique_vals(COL_원산지):
        rows = bj_df[bj_df[COL_원산지] == vendor]
        if rows.empty:
            continue
        stock_total = sum(_num(v) for v in rows[COL_재고량] if v not in ('', 'nan'))
        cost_total = sum(
            _num(r[COL_재고량]) * (get_price_info(r[COL_품목], r[COL_품명]) or {}).get('단가', 0) or 0
            for _, r in rows.iterrows()
        )
        cost_str = f"{cost_total:,.0f}원" if cost_total else "단가정보없음"
        header = (f"#### {vendor} (부재료 {len(rows)}개 품목 | "
                  f"현재고 합계: {stock_total:,.0f} | 재고비용: {cost_str})")
        item_lines = []
        for _, r in rows.iterrows():
            품번 = r.get(COL_품목, '-')
            품명 = r.get(COL_품명, '-')
            규격 = display_규격(r.get(COL_규격, ''))
            재고 = r.get(COL_재고량, '0')
            pi = get_price_info(품번, 품명)
            단가_str = f"{pi['단가']:,.0f}원" if pi and pi.get('단가') else "단가없음"
            spec = get_spec_info(품번, 품명)
            mfg = spec.get('외주업체명', '') if spec else ''
            mfg_str = f" | 부재료제조업체: {mfg}" if mfg else ''
            item_lines.append(
                f"  - 품번: {품번} | {품명} | {규격} | 현재고량: {재고} | 단가: {단가_str}{mfg_str}"
            )
        sections.append(header + '\n' + '\n'.join(item_lines))
    return '\n\n'.join(sections)

VENDOR_BUJAMYO_TEXT = _vendor_bujamyo_detail()

_price_count = len(PRICE_BY_CODE) if PRICE_BY_CODE else 0
_price_basis = list(set(v.get('기준년월','') for v in PRICE_BY_CODE.values()))[:2] if PRICE_BY_CODE else []

_spec_mfg_summary = ''
if _spec_vendors:
    _spec_mfg_summary = f"\n### 부재료 제조업체 전체 목록 ({len(_spec_vendors)}개) — 부자재 규격.xlsx 기준\n"
    _spec_mfg_summary += ', '.join(_spec_vendors)

STATIC_META_TEXT = f"""## 데이터 고유값 및 집계 요약

### 납품처 전체 목록 ({len(_unique_vals(COL_납품처))}개)
{', '.join(_unique_vals(COL_납품처))}

### 외주소분업체 전체 목록 ({len(_unique_vals(COL_원산지))}개) — 완제품 생산/소분 담당
※ 재고일지의 "외주업체" 컬럼 = 외주소분업체 (완제품을 실제 생산·소분하는 업체)
{', '.join(_unique_vals(COL_원산지))}
{_spec_mfg_summary}

### 규격 분류 체계
- 부재료(부재료): 원본값 "부재료" 항목 ({len(DF[DF[COL_규격]=='부재료'])}건)
- 부재료(단상자): 원본값 "단상자" 항목 ({len(DF[DF[COL_규격]=='단상자'])}건)
- 부재료(물류박스): 원본값 "물류박스" 항목 ({len(DF[DF[COL_규격]=='물류박스'])}건)
- 위 3가지를 통칭할 때 "부재료"라고 부름
- 그 외 규격: {', '.join(v for v in _unique_vals(COL_규격) if v not in BUJAMYO_TYPES)}

### 단가 데이터: {_price_count}개 품번 (기준년월: {', '.join(_price_basis)})

### 외주소분업체별 현재고량 및 재고비용 (완제품+부재료 전체)
{VENDOR_STOCK_TEXT}

### 외주소분업체별 부재료 재고 항목 수
{chr(10).join(
    f"- {v}: 부재료 {len(DF[(DF[COL_원산지]==v) & DF[COL_규격].isin(BUJAMYO_TYPES)])}개 품목"
    for v in _unique_vals(COL_원산지)
)}

### 납품처별 현재고량 및 재고비용
{DEST_STOCK_TEXT}

### 총 재고 항목 수: {len(DF)}개 (완제품 재고일지 기준)
"""

print(f"[정적 메타데이터 완료] 납품처 {len(_unique_vals(COL_납품처))}개, 외주업체 {len(_unique_vals(COL_원산지))}개, 단가 {_price_count}개")


# ────────────────────────────────────────────
# 자사 부자재 재고 검색 함수
# ────────────────────────────────────────────
# 자사재고 컬럼 인덱스 고정 (Excel 원본 기준)
# C열=품번(1), F열=구분2(4), G열=제품명(5), I열=총재고(7)
if JASA_DF is not None:
    JASA_COL_품번    = JASA_DF.columns[1]   # C열
    JASA_COL_업체    = JASA_DF.columns[2]   # D열
    JASA_COL_구분1   = JASA_DF.columns[3]   # E열
    JASA_COL_구분2   = JASA_DF.columns[4]   # F열
    JASA_COL_제품명  = JASA_DF.columns[5]   # G열
    JASA_COL_총재고  = JASA_DF.columns[7]   # I열
else:
    JASA_COL_품번 = JASA_COL_업체 = JASA_COL_구분1 = '품번'
    JASA_COL_구분2 = JASA_COL_제품명 = JASA_COL_총재고 = '총재고'

# 자사재고 키워드 집합 (구분1, 업체)  ※구분2는 원물/부재료 판단에 사용
JASA_KW_구분1 = set(JASA_DF[JASA_COL_구분1].unique()) if JASA_DF is not None else set()
JASA_KW_업체  = set(JASA_DF[JASA_COL_업체].unique())  if JASA_DF is not None else set()

# 구분2 중 원물 제외 = 부재료 타입
JASA_구분2_부재료 = set(
    v for v in (JASA_DF[JASA_COL_구분2].unique() if JASA_DF is not None else [])
    if v and v != '원물'
)  # PP, RRP, 파우치, 단상자, 용기, 롤파우치, 핸들캡, 게또바시, 공용

def _jasa_summary():
    """자사재고 정적 요약 (구분1/업체별 총재고+재고금액 집계)"""
    if JASA_DF is None:
        return ''
    lines = []
    lines.append('### 자사 부자재 재고 - 구분1(품목군)별 총재고 및 재고금액')
    for g, sub in JASA_DF.groupby(JASA_COL_구분1):
        total = sum(_num(v) for v in sub[JASA_COL_총재고] if v not in ('', 'nan'))
        cost = sum(
            _num(r[JASA_COL_총재고]) * (get_price_info(r[JASA_COL_품번], r[JASA_COL_제품명]) or {}).get('단가', 0)
            for _, r in sub.iterrows()
        )
        cost_str = f"{cost:,.0f}원" if cost else '단가정보없음'
        lines.append(f"- {g}: 총재고 {total:,.0f} | 재고금액 {cost_str} ({len(sub)}개 품목)")
    lines.append('\n### 자사 부자재 재고 - 업체별 총재고 및 재고금액')
    for g, sub in JASA_DF.groupby(JASA_COL_업체):
        total = sum(_num(v) for v in sub[JASA_COL_총재고] if v not in ('', 'nan'))
        cost = sum(
            _num(r[JASA_COL_총재고]) * (get_price_info(r[JASA_COL_품번], r[JASA_COL_제품명]) or {}).get('단가', 0)
            for _, r in sub.iterrows()
        )
        cost_str = f"{cost:,.0f}원" if cost else '단가정보없음'
        lines.append(f"- {g}: 총재고 {total:,.0f} | 재고금액 {cost_str} ({len(sub)}개 품목)")
    return '\n'.join(lines)

JASA_META = _jasa_summary()
if JASA_META:
    print(f"[자사재고 요약] {len(JASA_DF)}개 항목 집계 완료")
    STATIC_META_TEXT += '\n' + JASA_META

def _jasa_format_row(r) -> str:
    """자사재고 행 포맷 — 품번(C열)+품명(G열) 고정, 총재고(I열)+단가+재고금액
    F열(구분2): 원물=원재료, 그 외=부재료
    """
    품번   = str(r.get(JASA_COL_품번,   '')).strip()   # C열
    품명   = str(r.get(JASA_COL_제품명, '')).strip()   # G열
    구분2  = str(r.get(JASA_COL_구분2,  '')).strip()   # F열
    업체   = str(r.get(JASA_COL_업체,   '')).strip()   # D열
    총재고_raw = str(r.get(JASA_COL_총재고, '0')).strip()  # I열
    총재고_num = _num(총재고_raw)
    분류   = '원재료' if 구분2 == '원물' else f'부재료({구분2})' if 구분2 else '부재료'

    # 단가 조회 (품번 → 품명 순서로 매칭)
    pi = get_price_info(품번, 품명)
    단가 = pi.get('단가', 0) if pi else 0
    재고금액 = 총재고_num * 단가

    단가_str = f"{단가:,.0f}원" if 단가 else '단가없음'
    금액_str = f"{재고금액:,.0f}원" if 재고금액 else '-'

    return (
        f"품번: {품번 or '-'} | "
        f"품명: {품명 or '-'} | "
        f"분류: {분류} | "
        f"업체: {업체 or '-'} | "
        f"총재고: {총재고_raw if 총재고_raw and 총재고_raw != 'nan' else '0'} | "
        f"단가: {단가_str} | "
        f"재고금액: {금액_str}"
    )

def search_jasa(query_lower: str) -> str:
    """자사 부자재 재고 검색 — G열(품명) 기준, 행 제한 없음"""
    if JASA_DF is None:
        return ''

    q  = query_lower
    df = JASA_DF

    # ── 집계 모드 판단 (업체별/구분별만 집계, 품명별은 개별 나열) ────
    # '업체별', '구분별', '카테고리별' → 집계
    # '품명별', '품목별' → 개별 나열 (집계 아님)
    is_aggregate = any(k in q for k in ['업체별', '구분별', '카테고리별', '분류별'])
    # '별' 단독이 있어도 품명/품목 관련이면 나열 처리
    if '별' in q and not is_aggregate:
        is_aggregate = not any(k in q for k in ['품명별', '품목별', '품번별'])

    # ── 부재료 / 원재료 필터 (F열 기준) ─────────────────────────────
    want_bujamyo = any(k in q for k in ['부재료', '자사부재료'])
    want_원물    = ('원물' in q or '원재료' in q) and '제외' not in q

    if want_bujamyo and not want_원물:
        base_df = df[df[JASA_COL_구분2] != '원물']   # 125건
    elif want_원물 and not want_bujamyo:
        base_df = df[df[JASA_COL_구분2] == '원물']   # 17건
    else:
        base_df = df  # 전체 142건

    # ── 한국어 조사 제거 헬퍼 ──────────────────────────────────────
    def _strip_ko_particles(token: str) -> str:
        """'롯데로'→'롯데', '자사에'→'자사', '제품명을'→'제품명'"""
        t = token.strip('.,;:!?()[]')
        # 긴 조사부터 체크 (순서 중요)
        for p in ['에서는', '에서', '으로', '한테', '에게', '별로',
                  '로', '에', '의', '은', '는', '이', '가', '을', '를', '도']:
            if t.endswith(p) and len(t) > len(p) + 1:
                return t[:-len(p)]
        return t

    # ── 카테고리 필터 (구분1 / 업체) ─────────────────────────────────
    # 쿼리 토큰에서 조사 제거 후 매칭 (롯데→롯데마트, 롯데슈퍼)
    q_tokens = [_strip_ko_particles(k) for k in q.split() if len(k) >= 2]

    구분1_hit = [v for v in JASA_KW_구분1 if v.lower() in q]
    업체_hit  = [v for v in JASA_KW_업체 if v and v.lower() in q]

    # 역방향 부분 매칭: 쿼리 토큰이 업체명에 포함됨 (e.g., '롯데' → 롯데마트, 롯데슈퍼)
    _matched_tokens = set()  # 업체 매칭에 사용된 토큰 (JASA_SKIP에 추가용)
    if not 업체_hit:
        for v in JASA_KW_업체:
            if not v:
                continue
            vl = v.lower()
            for tok in q_tokens:
                if len(tok) >= 2 and tok in vl and tok not in JASA_KW_구분1:
                    if v not in 업체_hit:
                        업체_hit.append(v)
                        _matched_tokens.add(tok)

    cat_mask = pd.Series([True] * len(base_df), index=base_df.index)
    if 구분1_hit:
        cat_mask = cat_mask & base_df[JASA_COL_구분1].isin(구분1_hit)
    if 업체_hit:
        cat_mask = cat_mask & base_df[JASA_COL_업체].isin(업체_hit)

    # ── G열(품명) 검색 — 1순위 / C열(품번 코드) 보조 ────────────────
    JASA_SKIP = set(v.lower() for v in (구분1_hit + 업체_hit))
    JASA_SKIP |= _matched_tokens  # 부분 매칭된 토큰도 제외 (e.g., '롯데')
    JASA_SKIP |= {
        # 기능어 / 데이터 소스 구분
        '자사', '자사재고', '자사부재료', '총재고', '재고', '부재료', '원물', '원재료',
        '재고수량', '재고량', '재고금액', '재고비용', '수량', '금액', '비용', '단가',
        '현황', '조회', '목록', '전체', '모든', '전부',
        # 외주 관련 (자사재고 검색에선 제외)
        '외주', '외주업체', '외주처', '외주별', '외주소분', '소분업체',
        # 집계/나열 관련
        '품명별', '품목별', '품번별', '구분별', '업체별', '카테고리별', '분류별',
        '별로', '별', '나열', '정리', '요약',
        # 질문 표현
        '알려줘', '알려', '보여줘', '보여', '얼마', '몇', '있어', '있는',
        '알고싶어', '알고', '싶어', '궁금해', '궁금', '문의', '대해',
        '들어가는', '들어가', '제품', '제품명', '품명',
        # 조사 / 접속사
        '의', '은', '는', '이', '가', '을', '를', '에', '도', '로', '으로',
        '에서', '에게', '한테', '과', '와', '다', '좀', '한', '해줘', '해',
        '및', '그리고', '대한',
    }
    # 조사 제거 + 특수문자 정리 후 키워드 추출
    clean_tokens = [_strip_ko_particles(k) for k in q.split()]
    name_kws = [k for k in clean_tokens if len(k) >= 2 and k not in JASA_SKIP]

    txt_mask = pd.Series([False] * len(base_df), index=base_df.index)
    if name_kws:
        # 1순위: G열(품명) 포함 검색
        col_g = base_df[JASA_COL_제품명].str.lower()
        for k in name_kws:
            txt_mask = txt_mask | col_g.str.contains(k, na=False, regex=False)
        # 2순위: C열(품번) — 알파벳+숫자 코드 패턴만
        code_kws = [k for k in name_kws if k and k[0].isalpha() and any(c.isdigit() for c in k)]
        if code_kws:
            col_c = base_df[JASA_COL_품번].str.lower()
            for k in code_kws:
                txt_mask = txt_mask | col_c.str.contains(k, na=False, regex=False)

    # ── 최종 마스크 결합 ────────────────────────────────────────────
    if (구분1_hit or 업체_hit) and name_kws:
        mask = cat_mask & txt_mask
        if not base_df[mask].any(axis=None):
            mask = cat_mask   # 교집합 없으면 카테고리 필터만
    elif 구분1_hit or 업체_hit:
        mask = cat_mask
    elif name_kws:
        mask = txt_mask
    else:
        mask = pd.Series([True] * len(base_df), index=base_df.index)

    matched = base_df[mask]   # 행 제한 없음 — 전체 매칭 반환
    if matched.empty:
        return ''

    # ── 집계 반환 (업체별/구분별) ────────────────────────────────────
    if is_aggregate:
        group_col = JASA_COL_업체 if (업체_hit or '업체' in q) else JASA_COL_구분1
        lines = [f'[자사 재고 - {group_col}별 총재고 및 재고금액 집계 ({len(matched)}건)]']
        for g, sub in matched.groupby(group_col):
            total = sum(_num(v) for v in sub[JASA_COL_총재고] if v not in ('', 'nan'))
            cost = sum(
                _num(r[JASA_COL_총재고]) * (get_price_info(r[JASA_COL_품번], r[JASA_COL_제품명]) or {}).get('단가', 0)
                for _, r in sub.iterrows()
            )
            cost_str = f"{cost:,.0f}원" if cost else '단가정보없음'
            lines.append(f"- {g}: 총재고 {total:,.0f} | 재고금액 {cost_str} ({len(sub)}품목)")
        return '\n'.join(lines)

    # ── 행별 반환 — 품번·품명(G열) 고정, 총재고(I열)만 공개 ──────────
    label = '부재료' if want_bujamyo else ('원물' if want_원물 else '전체')
    lines = [f'[자사 부자재 재고({label}) - 총 {len(matched)}건]', '']
    for _, r in matched.iterrows():
        lines.append(_jasa_format_row(r))
    return '\n'.join(lines)


# ────────────────────────────────────────────
# 발주정보 검색 함수
# ────────────────────────────────────────────
# ────────────────────────────────────────────
# 출하/출고(매출) 검색 함수
# ────────────────────────────────────────────
# ────────────────────────────────────────────
# 입고정보 검색 함수
# ────────────────────────────────────────────
RCV_TRIGGER_KW = ['입고내역', '입고현황', '입고정보', '입고', '입고량', '입고수량']

def search_receiving(query_lower: str) -> str:
    """입고정보 데이터 검색 — 입고장소 포함"""
    if RCV_DF is None or RCV_DF.empty:
        return ''

    q = query_lower
    df = RCV_DF
    date_filter, month_filter, date_tokens = _parse_date_filter(q)

    # 조사 제거
    _particles = ['에서', '으로', '한테', '에게', '별로', '로', '에', '의', '은', '는',
                  '이', '가', '을', '를', '과', '와', '도', '한', '해줘', '해']
    def _clean_rcv(tok):
        for p in sorted(_particles, key=len, reverse=True):
            if tok.endswith(p) and len(tok) > len(p) + 1:
                return tok[:-len(p)]
        return tok
    q_tokens = [_clean_rcv(t) for t in q.split() if len(t) >= 2]

    skip = set(RCV_TRIGGER_KW) | {'알려줘', '알려', '보여줘', '보여', '현황', '조회',
            '전체', '목록', '내역', '정보', '품목별', '품명별', '날짜별',
            '집계', '요약', '별로', '별', '수량', '오늘', '어제', '월', '일'} | date_tokens
    name_kws = [t for t in q_tokens if t not in skip and len(t) >= 2]

    mask = pd.Series([True] * len(df), index=df.index)

    # 거래처 필터
    if '거래처명' in df.columns and name_kws:
        v_mask = pd.Series([False] * len(df), index=df.index)
        for kw in name_kws:
            v_mask = v_mask | df['거래처명'].str.lower().str.contains(kw, na=False, regex=False)
        if v_mask.any():
            mask = mask & v_mask
            name_kws = []

    # 품번/품명/입고장소 필터
    if name_kws:
        txt_mask = pd.Series([False] * len(df), index=df.index)
        for col in ['품번', '품명', '입고장소', '거래처명']:
            if col in df.columns:
                col_lower = df[col].str.lower()
                for kw in name_kws:
                    txt_mask = txt_mask | col_lower.str.contains(kw, na=False, regex=False)
        mask = mask & txt_mask

    # 날짜 필터
    if date_filter and '입고일자' in df.columns:
        mask = mask & (df['입고일자'] == date_filter)
    elif month_filter and '입고일자' in df.columns:
        mask = mask & df['입고일자'].str.startswith(month_filter)

    matched = df[mask]
    date_label = f" ({date_filter})" if date_filter else (f" ({month_filter[:4]}.{month_filter[4:]}월)" if month_filter else "")

    if matched.empty:
        if date_filter or month_filter or name_kws:
            return f'[입고정보 조회{date_label} - 0건]\n해당 조건에 맞는 입고 데이터가 없습니다.'
        return ''

    # 최신순 정렬, 최대 10건
    if '입고일자' in matched.columns:
        matched = matched.sort_values('입고일자', ascending=False)
    total = len(matched)
    truncated = total > 10
    matched_show = matched.head(10)
    header = f'[입고정보 조회{date_label} - {total}건'
    if truncated:
        header += f', 최신 10건 표시'
    header += ']'

    lines = [header, '']
    show_cols = ['입고번호', '입고일자', '거래처명', '품번', '품명', '입고수량',
                 '단가', '합계금액', '입고장소', '입고창고', '비고']
    show_cols = [c for c in show_cols if c in matched_show.columns]
    for _, r in matched_show.iterrows():
        parts = []
        for col in show_cols:
            val = r.get(col, '')
            if val and str(val) not in ('nan', ''):
                if col in ('입고수량', '발주수량', '단가', '합계금액', '공급가액', '부가세'):
                    try:
                        val = f"{float(val):,.0f}"
                    except Exception:
                        pass
                parts.append(f"{col}: {val}")
        lines.append(' | '.join(parts))
    return '\n'.join(lines)


# ────────────────────────────────────────────
# 생산실적 검색 함수
# ────────────────────────────────────────────
PROD_TRIGGER_KW = ['생산실적', '생산현황', '생산량', '생산내역', '생산정보',
                   '양품', '불량', '작업수량']

# 생산계획(생산지시) 트리거 키워드
WO_TRIGGER_KW = ['생산계획', '생산지시', '작업지시', '지시현황', '지시내역',
                 '생산일정', '생산스케줄', '계획수량']

def search_work_order(query_lower: str) -> str:
    """생산지시(생산계획) 데이터 검색 — 아마란스 기준"""
    if WO_DF is None or WO_DF.empty:
        return ''
    q = query_lower
    df = WO_DF

    date_filter, month_filter, date_tokens = _parse_date_filter(q)

    # 날짜 필터 적용
    if date_filter:
        df = df[df['지시일자'] == date_filter]
    elif month_filter:
        df = df[df['지시일자'].str.startswith(month_filter)]

    # 품번/품명/거래처 키워드 검색
    import re as _re_wo
    code_match = _re_wo.findall(r'[A-Za-z]\d{3,}', q)
    skip = set(WO_TRIGGER_KW) | {'알려줘', '알려', '보여줘', '보여', '현황', '조회',
            '전체', '목록', '내역', '정보', '오늘', '월', '일'} | date_tokens
    name_kws = [k for k in q.split() if len(k) >= 2 and k not in skip]

    if code_match:
        codes_upper = [c.upper() for c in code_match]
        mask = df['품번'].str.upper().isin(codes_upper)
        df = df[mask] if mask.any() else df

    if not code_match and name_kws:
        txt_mask = pd.Series([False] * len(df), index=df.index)
        for col in ['품명', '품번', '거래처명']:
            if col in df.columns:
                col_lower = df[col].str.lower()
                for k in name_kws:
                    txt_mask = txt_mask | col_lower.str.contains(k, na=False, regex=False)
        if txt_mask.any():
            df = df[txt_mask]

    if df.empty:
        if date_filter:
            return f'[생산계획(생산지시) 조회 ({date_filter})] 해당 날짜에 등록된 생산지시가 없습니다.'
        elif month_filter:
            return f'[생산계획(생산지시) 조회 ({month_filter[:4]}.{month_filter[4:]}월)] 해당 월에 등록된 생산지시가 없습니다.'
        return ''

    # 최신순 정렬, 최대 15건
    total_count = len(df)
    df = df.sort_values('지시일자', ascending=False).head(15)

    date_label = ''
    if date_filter:
        date_label = f' ({date_filter[:4]}.{date_filter[4:6]}.{date_filter[6:]})'
    elif month_filter:
        date_label = f' ({month_filter[:4]}.{month_filter[4:]}월)'

    # 실적 대비 여부 감지
    want_compare = any(k in q for k in ['대비', '비교', '실적', '달성', '달성률', '진행률', '진척', '현황'])

    lines = [f'[생산계획(생산지시) 조회{date_label} - {total_count}건 중 최신 {len(df)}건]', '']

    for _, r in df.iterrows():
        wo_cd = str(r.get('생산지시번호', '')).strip()
        지시수량 = float(r.get('지시수량', 0) or 0)
        거래처 = str(r.get('거래처명', '')).strip()
        거래처_str = f' | 외주처: {거래처}' if 거래처 and 거래처 != 'None' else ''

        # 생산실적 매칭
        실적_str = ''
        if PROD_DF is not None and want_compare:
            pr_match = PROD_DF[PROD_DF['생산지시번호'] == wo_cd]
            if not pr_match.empty:
                양품 = sum(float(v or 0) for v in pr_match['양품수량'])
                불량 = sum(float(v or 0) for v in pr_match['불량수량'])
                달성률 = (양품 / 지시수량 * 100) if 지시수량 > 0 else 0
                상태 = '완료' if 달성률 >= 100 else '진행중' if 달성률 > 0 else '미착수'
                실적_str = f' | 양품: {양품:,.0f} | 불량: {불량:,.0f} | 달성률: {달성률:.1f}% ({상태})'
            else:
                실적_str = ' | 실적: 미착수'
        elif PROD_DF is not None:
            # 대비 키워드 없어도 기본 달성률 표시
            pr_match = PROD_DF[PROD_DF['생산지시번호'] == wo_cd]
            if not pr_match.empty:
                양품 = sum(float(v or 0) for v in pr_match['양품수량'])
                달성률 = (양품 / 지시수량 * 100) if 지시수량 > 0 else 0
                실적_str = f' | 양품: {양품:,.0f} ({달성률:.0f}%)'

        lines.append(
            f"지시번호: {wo_cd} | 지시일: {r.get('지시일자','')} | "
            f"품번: {r.get('품번','')} | 품명: {str(r.get('품명',''))[:30]} | "
            f"지시수량: {지시수량:,.0f}{실적_str}{거래처_str}"
        )
    return '\n'.join(lines)

def search_production(query_lower: str) -> str:
    """생산실적 데이터 검색"""
    if PROD_DF is None or PROD_DF.empty:
        return ''

    q = query_lower
    df = PROD_DF

    _particles = ['에서', '으로', '한테', '에게', '별로', '로', '에', '의', '은', '는',
                  '이', '가', '을', '를', '과', '와', '도', '한', '해줘', '해']
    def _clean_p(tok):
        for p in sorted(_particles, key=len, reverse=True):
            if tok.endswith(p) and len(tok) > len(p) + 1:
                return tok[:-len(p)]
        return tok
    q_tokens = [_clean_p(t) for t in q.split() if len(t) >= 2]

    # 날짜 필터 (공통 함수 사용)
    date_filter, month_filter, date_tokens = _parse_date_filter(q)

    skip = set(PROD_TRIGGER_KW) | {'알려줘', '알려', '보여줘', '보여', '현황', '조회',
            '전체', '목록', '내역', '정보', '품목별', '품명별', '날짜별',
            '집계', '요약', '별로', '별', '수량', '오늘', '월', '일'} | date_tokens
    name_kws = [t for t in q_tokens if t not in skip and len(t) >= 2]

    mask = pd.Series([True] * len(df), index=df.index)

    # 날짜 필터
    date_col = '실적일자' if '실적일자' in df.columns else None
    if date_filter and date_col:
        mask = mask & (df[date_col] == date_filter)
    elif month_filter and date_col:
        mask = mask & df[date_col].str.startswith(month_filter)

    # 품번/품명 필터
    if name_kws:
        txt_mask = pd.Series([False] * len(df), index=df.index)
        for col in ['품번', '품명', '품목구분', '생산지시번호']:
            if col in df.columns:
                col_lower = df[col].str.lower()
                for kw in name_kws:
                    txt_mask = txt_mask | col_lower.str.contains(kw, na=False, regex=False)
        mask = mask & txt_mask

    matched = df[mask]
    if matched.empty:
        if date_filter:
            return f'[생산실적 조회 ({date_filter})] 해당 날짜에 등록된 생산실적이 없습니다.'
        elif month_filter:
            return f'[생산실적 조회 ({month_filter[:4]}.{month_filter[4:]}월)] 해당 월에 등록된 생산실적이 없습니다.'
        return ''

    # 집계
    is_group = any(k in q for k in ['별', '별로', '집계', '요약'])
    if is_group and '품명' in df.columns:
        lines = [f'[생산실적 품목별 집계 - {len(matched)}건]']
        for g, sub in matched.groupby('품명'):
            작업 = sum(_num(v) for v in sub.get('작업수량', []))
            양품 = sum(_num(v) for v in sub.get('양품수량', []))
            불량 = sum(_num(v) for v in sub.get('불량수량', []))
            품번 = sub['품번'].iloc[0] if '품번' in sub.columns else ''
            lines.append(f"- 품번: {품번} | {g} | 작업: {작업:,.0f} | 양품: {양품:,.0f} | 불량: {불량:,.0f}")
        return '\n'.join(lines)

    # 개별 나열
    date_info = f" ({date_filter})" if date_filter else ""
    lines = [f'[생산실적 조회{date_info} - {len(matched)}건]', '']
    show_cols = ['실적번호', '실적일자', '품번', '품명', '품목구분', '지시수량',
                 '작업수량', '양품수량', '불량수량', '이동수량', '이동창고', '비고']
    show_cols = [c for c in show_cols if c in matched.columns]
    for _, r in matched.iterrows():
        parts = []
        for col in show_cols:
            val = r.get(col, '')
            if val and str(val) not in ('nan', ''):
                if '수량' in col:
                    try:
                        val = f"{float(val):,.0f}"
                    except Exception:
                        pass
                parts.append(f"{col}: {val}")
        lines.append(' | '.join(parts))
    return '\n'.join(lines)


SALES_TRIGGER_KW = ['매출', '매출현황', '매출정보', '매출내역',
                    '판매', '판매량', '판매현황', '판매내역', '판매정보',
                    '출하', '출하정보', '출하현황', '출하내역',
                    '출고', '출고정보', '출고현황', '출고내역', '자재이동']

def search_sales(query_lower: str) -> str:
    """출하(매출)/출고 데이터 검색"""
    q = query_lower
    _s_date_f, _s_month_f, _s_date_tokens = _parse_date_filter(q)
    is_issue = any(k in q for k in ['출고', '자재이동']) and '출하' not in q
    is_ship = any(k in q for k in ['출하', '매출']) or not is_issue

    df = None
    label = ''
    if is_ship and SHIP_DF is not None and not SHIP_DF.empty:
        df = SHIP_DF
        label = '출하(매출)'
    elif is_issue and ISSUE_DF is not None and not ISSUE_DF.empty:
        df = ISSUE_DF
        label = '출고(자재이동)'

    if df is None or df.empty:
        return ''

    # 조사 제거
    _particles = ['에서', '으로', '한테', '에게', '별로', '로', '에', '의', '은', '는',
                  '이', '가', '을', '를', '과', '와', '도', '한', '해줘', '해']
    def _clean_s(tok):
        for p in sorted(_particles, key=len, reverse=True):
            if tok.endswith(p) and len(tok) > len(p) + 1:
                return tok[:-len(p)]
        return tok
    q_tokens = [_clean_s(t) for t in q.split() if len(t) >= 2]

    # 거래처 필터
    vendor_col = '거래처명' if '거래처명' in df.columns else None
    vendor_hit = []
    if vendor_col:
        for v in df[vendor_col].unique():
            vl = str(v).lower()
            if not vl:
                continue
            if vl in q:
                vendor_hit.append(v)
            else:
                for tok in q_tokens:
                    if len(tok) >= 2 and tok in vl:
                        if v not in vendor_hit:
                            vendor_hit.append(v)

    # 키워드 필터
    skip = set(SALES_TRIGGER_KW) | {'알려줘', '알려', '보여줘', '보여', '현황', '조회',
            '전체', '목록', '내역', '정보', '거래처별', '거래처', '품목별', '품명별',
            '집계', '요약', '별로', '별', '수량', '금액', '합계', '오늘'} | _s_date_tokens
    if vendor_hit:
        skip |= set(v.lower() for v in vendor_hit)
        if vendor_col:
            for v in vendor_hit:
                for tok in q_tokens:
                    if tok in v.lower():
                        skip.add(tok)
    name_kws = [t for t in q_tokens if t not in skip and len(t) >= 2]

    mask = pd.Series([True] * len(df), index=df.index)
    if vendor_hit and vendor_col:
        mask = mask & df[vendor_col].isin(vendor_hit)
    if name_kws:
        txt_mask = pd.Series([False] * len(df), index=df.index)
        for col in ['품번', '품명', '거래처명', '모품번', '모품명']:
            if col in df.columns:
                col_lower = df[col].str.lower()
                for kw in name_kws:
                    txt_mask = txt_mask | col_lower.str.contains(kw, na=False, regex=False)
        mask = mask & txt_mask

    # 날짜 필터 적용
    date_cols_s = ['출하일자'] if label == '출하(매출)' else ['출고일자']
    mask = _apply_date_mask(df, mask, _s_date_f, _s_month_f, date_cols_s)

    matched = df[mask]
    if matched.empty:
        return ''

    date_label = f" ({_s_date_f})" if _s_date_f else (f" ({_s_month_f[:4]}.{_s_month_f[4:]}월)" if _s_month_f else "")

    # 집계
    is_group = any(k in q for k in ['별', '별로', '집계', '요약'])
    if is_group and vendor_col and vendor_col in df.columns:
        lines = [f'[{label} 거래처별 집계{date_label} - {len(matched)}건]']
        for g, sub in matched.groupby(vendor_col):
            qty_col = '출하수량' if '출하수량' in sub.columns else ('출고수량' if '출고수량' in sub.columns else None)
            total_qty = sum(_num(v) for v in sub[qty_col]) if qty_col else 0
            lines.append(f"- {g}: {len(sub)}건 | 수량합계: {total_qty:,.0f}")
        return '\n'.join(lines)

    # 개별 나열
    lines = [f'[{label} 조회{date_label} - {len(matched)}건]', '']
    if label == '출하(매출)':
        show_cols = ['출하번호', '출하일자', '거래처명', '품번', '품명', '출하수량', '모품번', '모품명', '창고', '비고']
    else:
        show_cols = ['출고번호', '출고일자', '품번', '품명', '출고수량', '출고창고', '입고창고', '모품번', '모품명', '비고']
    show_cols = [c for c in show_cols if c in matched.columns]

    for _, r in matched.iterrows():
        parts = []
        for col in show_cols:
            val = r.get(col, '')
            if val and str(val) not in ('nan', ''):
                if '수량' in col:
                    try:
                        val = f"{float(val):,.0f}"
                    except Exception:
                        pass
                parts.append(f"{col}: {val}")
        lines.append(' | '.join(parts))
    return '\n'.join(lines)


ORDER_TRIGGER_KW = ['발주', '발주정보', '발주내역', '발주현황', '발주확정', '발주대기',
                    '납기', '납기일', 'po-', 'po2026', 'wp2026',
                    '외주발주', '외주발주현황', '외주발주내역']

# ────────────────────────────────────────────
# BOM 검색 함수
# ────────────────────────────────────────────
BOM_TRIGGER_KW = ['bom', 'BOM', '정전개', '역전개', '소요량', '투입량', '자품', '모품',
                  '부족수량', '부족량', '부족']

def _lookup_bom_stock(자품번: str, 모품번: str):
    """BOM 자재의 현재고 조회
    모품번 G → 자사재고(JASA_DF), 모품번 H/I → 외주재고(DF)
    """
    code = str(자품번).strip().upper()
    if not code:
        return None

    if 모품번.startswith('G'):
        # 자사재고에서 검색
        if JASA_DF is not None:
            col_품번 = JASA_DF.columns[1]
            col_총재고 = JASA_DF.columns[7]
            match = JASA_DF[JASA_DF[col_품번].str.upper() == code]
            if not match.empty:
                return _num(match.iloc[0][col_총재고])
    else:
        # 외주재고(재고일지)에서 검색
        match = DF[DF[COL_품목].str.upper() == code]
        if not match.empty:
            # 현재고량 합계 (같은 품번 여러 행 가능)
            return sum(_num(r[COL_재고량]) for _, r in match.iterrows())

    return None


# ────────────────────────────────────────────
# Monday.com 검색 함수
# ────────────────────────────────────────────
MONDAY_TRIGGER_KW = ['먼데이', 'monday', '공지사항', '업무일지', '회의', '주간보고',
                     '주간업무', '업무관리', '보드', '구매요청', '인감']

def search_monday(query_lower: str) -> str:
    """Monday.com 보드/아이템 검색"""
    if MONDAY_DF is None or MONDAY_DF.empty:
        return ''

    q = query_lower
    df = MONDAY_DF

    # 날짜 필터
    date_filter, month_filter, date_tokens = _parse_date_filter(q)

    # 검색 키워드 추출 (날짜 토큰도 검색에 포함 — 보드 제목에 날짜가 있을 수 있음)
    skip = set(MONDAY_TRIGGER_KW) | {'알려줘', '알려', '보여줘', '보여', '조회', '검색',
            '내용', '전체', '목록', '뭐야', '있어', '찾아줘', '먼데이', 'monday',
            '자료', '내역', '해줘', '해', '좀', '줘'}
    tokens = [t for t in q.split() if len(t) >= 2 and t not in skip]

    # 보드명 + 아이템명에서 키워드 검색
    mask = pd.Series([False] * len(df), index=df.index)
    if tokens:
        for col in ['보드명', '아이템명', '그룹']:
            col_lower = df[col].str.lower()
            for kw in tokens:
                mask = mask | col_lower.str.contains(kw, na=False, regex=False)
    else:
        mask = pd.Series([True] * len(df), index=df.index)

    # 날짜 필터는 텍스트 검색 결과가 없을 때만 생성일 기준으로 적용
    if not mask.any():
        if date_filter:
            date_col = df['생성일'].str.replace('-', '')
            mask = date_col == date_filter
        elif month_filter:
            date_col = df['생성일'].str.replace('-', '')
            mask = date_col.str.startswith(month_filter)

    matched = df[mask]
    if matched.empty:
        return ''

    # 보드별 그룹핑
    lines = [f'[Monday.com 검색 - {len(matched)}건, {matched["보드명"].nunique()}개 보드]', '']

    for board, group in matched.groupby('보드명'):
        items = group.head(15)
        lines.append(f'### 보드: {board} ({len(group)}건)')
        for _, r in items.iterrows():
            item_id = r.get('아이템ID', '')
            lines.append(f'  - [{r["아이템명"]}](monday:{item_id}) (그룹: {r["그룹"]}, 생성: {r["생성일"]})')
        if len(group) > 15:
            lines.append(f'  ... 외 {len(group) - 15}건')
        lines.append('')

    return '\n'.join(lines[:200])  # 컨텍스트 크기 제한


def search_bom(query_lower: str) -> str:
    """BOM 데이터 검색 - 모품번/모품명으로 자재 구성 조회"""
    if BOM_DF is None or BOM_DF.empty:
        return ''

    q = query_lower
    df = BOM_DF

    # 품번 코드 추출 (G0010, H0226 등)
    import re
    code_match = re.findall(r'[ghiGHI]\d{3,}', q)
    code_match = [c.upper() for c in code_match]

    # 품번 매칭
    if code_match:
        mask = df['모품번'].str.upper().isin(code_match)
        matched = df[mask]
    else:
        # 텍스트 키워드 검색
        _skip = set(BOM_TRIGGER_KW) | {'알려줘', '알려', '보여줘', '보여', '조회', '정보',
                '구성', '뭐야', '뭐가', '들어가', '어떤', '전체', '외주', '자사'}
        tokens = [t for t in q.split() if len(t) >= 2 and t not in _skip]

        if not tokens:
            # 전체 BOM 요약
            lines = [f'[BOM 전체 요약 - 모품번 {df["모품번"].nunique()}개, 총 {len(df)}건]', '']
            for prefix, label in [('G', '자사제품'), ('H', '외주제품'), ('I', '외주제품')]:
                sub = df[df['모품번'].str.startswith(prefix)]
                if not sub.empty:
                    parents = sub['모품번'].nunique()
                    lines.append(f"  {prefix}코드({label}): 모품번 {parents}개, BOM {len(sub)}건")
            return '\n'.join(lines)

        mask = pd.Series([False] * len(df), index=df.index)
        for col in ['모품번', '모품명', '자품번', '자품명']:
            if col in df.columns:
                col_lower = df[col].str.lower()
                for kw in tokens:
                    mask = mask | col_lower.str.contains(kw, na=False, regex=False)
        matched = df[mask]

    if matched.empty:
        return ''

    # 생산계획 수량 파싱 (예: "10000ea", "10,000개", "5000")
    # ※ 품번코드(G0010) 내 숫자는 제외 — 앞에 알파벳이 없는 숫자만 추출
    import re as _re2
    plan_qty = 0
    # 품번코드를 먼저 제거한 텍스트에서 수량 추출
    _q_no_code = _re2.sub(r'[a-zA-Z]\d{3,}', '', q)
    qty_match = _re2.findall(r'(\d[\d,]+)\s*(?:ea|EA|개|수량)?', _q_no_code)
    if qty_match:
        for m in qty_match:
            val = int(m.replace(',', ''))
            if val >= 10:
                plan_qty = val
                break

    _want_stock = any(k in q for k in ['부족', '비교', '검토', '계획', '확인', '충분',
                                        '가능', '필요', '생산할', '생산계획', '생산예정',
                                        '생산하려', '생산해야', '재고'])

    # 모품번별로 그룹핑
    header = f'[BOM 조회 - {matched["모품번"].nunique()}개 제품, {len(matched)}건]'
    if plan_qty:
        header += f' (생산계획: {plan_qty:,}EA)'
    lines = [header, '']

    for parent, group in matched.groupby('모품번'):
        parent_nm = group['모품명'].iloc[0] if '모품명' in group.columns else ''
        parent_dc = group.get('모품목구분', pd.Series([''])).iloc[0]
        parent_unit = group.get('모품단위', pd.Series([''])).iloc[0]

        lines.append(f"### 제품정보")
        lines.append(f"  품번: {parent}")
        lines.append(f"  품명: {parent_nm}")
        lines.append(f"  품목구분: {parent_dc}")
        if parent_unit:
            lines.append(f"  단위: {parent_unit}")
        if plan_qty:
            lines.append(f"  생산계획수량: {plan_qty:,}EA")
        lines.append(f"  BOM 구성자재: {len(group)}건")
        lines.append('')
        lines.append(f"### BOM 구성 (자재 목록) — 재고 비교")

        for _, r in group.iterrows():
            자품번 = r.get('자품번', '-')
            자품명 = r.get('자품명', '-')
            자품구분 = r.get('자품목구분', '')
            정미 = _num(r.get('정미수량', 0))
            실소요 = _num(r.get('실소요량', 0))
            단가 = r.get('자재단가', '')
            소요비용 = r.get('소요비용', '')
            단가_str = f" | 단가: {float(단가):,.0f}원" if 단가 and str(단가) not in ('', '0', '0.0', 'nan') else ''
            비용_str = f" | 소요비용: {float(소요비용):,.0f}원" if 소요비용 and str(소요비용) not in ('', '0', '0.0', 'nan') else ''

            # 재고 조회 + 부족수량 계산
            stock_str = ''
            shortage_str = ''
            if _want_stock and 자품번 != '-':
                stock_val = _lookup_bom_stock(자품번, parent)
                if stock_val is not None:
                    stock_str = f" | 현재고: {stock_val:,.0f}"
                    if plan_qty and 실소요 > 0:
                        필요수량 = plan_qty * 실소요
                        부족 = 필요수량 - stock_val
                        if 부족 > 0:
                            shortage_str = f" | ★필요수량: {필요수량:,.0f} → 부족: {부족:,.0f}"
                        else:
                            shortage_str = f" | 필요수량: {필요수량:,.0f} → 충분(여유: {-부족:,.0f})"
                else:
                    stock_str = " | 현재고: 정보없음"

            lines.append(f"  - 자품번: {자품번} | 자품명: {자품명} | 구분: {자품구분} | 실소요량: {실소요}{단가_str}{stock_str}{shortage_str}")

        lines.append('')

        if _want_stock:
            src = '자사재고 (자사사용 부자재)' if parent.startswith('G') else '외주재고 (완제품 재고일지)'
            lines.append(f"※ 재고 출처: {src}")
            if plan_qty:
                lines.append(f"※ 부족수량 = (생산계획 {plan_qty:,} × 실소요량) - 현재고")
            lines.append('')

    return '\n'.join(lines)

def _parse_date_filter(q):
    """쿼리에서 날짜 필터 추출 → (date_filter, month_filter, date_tokens)
    지원 형식: 25년7월15일, 7월15일, 7월, 3/5, 3.5, 0305, 20260305, 오늘, 어제
    """
    import re
    from datetime import datetime as dt, timedelta
    date_filter = None
    month_filter = None

    # 연도 감지 (25년, 26년, 2025년, 2026년)
    year_match = re.search(r'(\d{2,4})년', q)
    year = None
    if year_match:
        y = year_match.group(1)
        year = int(y) if len(y) == 4 else 2000 + int(y)

    default_year = dt.now().year

    # 패턴1: XX년XX월XX일 또는 XX월XX일
    full_match = re.search(r'(\d{1,2})월\s*(\d{1,2})일', q)
    if full_match:
        m, d = int(full_match.group(1)), int(full_match.group(2))
        y = year if year else default_year
        date_filter = f'{y}{m:02d}{d:02d}'

    # 패턴2: 슬래시/점 형식 (3/5, 3.5, 3-5, 03/05)
    elif re.search(r'(\d{1,2})[/.\-](\d{1,2})', q):
        slash = re.search(r'(\d{1,2})[/.\-](\d{1,2})', q)
        m, d = int(slash.group(1)), int(slash.group(2))
        if 1 <= m <= 12 and 1 <= d <= 31:
            y = year if year else default_year
            date_filter = f'{y}{m:02d}{d:02d}'

    # 패턴3: 8자리 숫자 (20260305)
    elif re.search(r'(?<!\d)(20\d{6})(?!\d)', q):
        eight = re.search(r'(?<!\d)(20\d{6})(?!\d)', q)
        date_filter = eight.group(1)

    # 패턴4: 4자리 숫자 MMDD (0305, 1215)
    elif re.search(r'(?<!\d)(\d{4})(?!\d)', q):
        four = re.search(r'(?<!\d)(\d{4})(?!\d)', q)
        val = four.group(1)
        m, d = int(val[:2]), int(val[2:])
        if 1 <= m <= 12 and 1 <= d <= 31:
            y = year if year else default_year
            date_filter = f'{y}{m:02d}{d:02d}'

    # 패턴5: 오늘/어제
    elif '오늘' in q:
        date_filter = dt.now().strftime('%Y%m%d')
    elif '어제' in q:
        date_filter = (dt.now() - timedelta(days=1)).strftime('%Y%m%d')
    else:
        # 패턴6: XX년XX월 또는 XX월 (월 단위)
        month_match = re.search(r'(\d{1,2})월', q)
        if month_match:
            m = int(month_match.group(1))
            y = year if year else default_year
            month_filter = f'{y}{m:02d}'

    # 날짜 관련 토큰을 skip에 추가
    date_tokens = set()
    for tok in q.split():
        if re.match(r'\d{2,4}년', tok):
            date_tokens.add(tok)
        if re.match(r'\d{1,2}월', tok):
            date_tokens.add(tok)
        if re.match(r'\d{1,2}일', tok):
            date_tokens.add(tok)
        if re.match(r'\d{1,2}[/.\-]\d{1,2}$', tok):
            date_tokens.add(tok)
        if re.match(r'20\d{6}$', tok):
            date_tokens.add(tok)
        if re.match(r'\d{4}$', tok) and date_filter:
            date_tokens.add(tok)
    combined = re.findall(r'\d{2,4}년\d{1,2}월(?:\d{1,2}일)?', q)
    for c in combined:
        date_tokens.add(c)
    if '오늘' in q:
        date_tokens.add('오늘')
    if '어제' in q:
        date_tokens.add('어제')

    return date_filter, month_filter, date_tokens


def _apply_date_mask(df, mask, date_filter, month_filter, date_cols):
    """날짜 필터를 mask에 적용"""
    for col in date_cols:
        if col in df.columns:
            if date_filter:
                return mask & (df[col] == date_filter)
            elif month_filter:
                return mask & df[col].str.startswith(month_filter, na=False)
    return mask


def search_order(query_lower: str) -> str:
    """발주정보 + 외주발주정보 검색"""
    _is_wp = any(k in query_lower for k in ['외주발주', '외주 발주', 'wp'])

    # 날짜 필터 공통
    _date_f, _month_f, _date_tokens = _parse_date_filter(query_lower)

    # 외주발주 전용 질문
    if _is_wp and WP_ORDER_DF is not None and not WP_ORDER_DF.empty:
        q = query_lower
        df = WP_ORDER_DF

        _particles = ['에서', '으로', '한테', '에게', '별로', '로', '에', '의', '은', '는',
                      '이', '가', '을', '를', '과', '와', '도', '한', '해줘', '해']
        def _clean_wp(tok):
            for p in sorted(_particles, key=len, reverse=True):
                if tok.endswith(p) and len(tok) > len(p) + 1:
                    return tok[:-len(p)]
            return tok
        q_tokens = [_clean_wp(t) for t in q.split() if len(t) >= 2]

        skip_wp = set(ORDER_TRIGGER_KW) | {'알려줘', '알려', '보여줘', '보여', '현황', '조회',
                '전체', '목록', '얼마', '몇', '있어', '있는', '내역', '정보',
                '거래처별', '거래처', '품목별', '품명별', '집계', '요약', '별로',
                '별', '수량', '금액', '합계', '외주', '외주발주'} | _date_tokens
        name_kws = [t for t in q_tokens if t not in skip_wp and len(t) >= 2]

        mask = pd.Series([True] * len(df), index=df.index)

        # 거래처명 필터
        if '거래처명' in df.columns and name_kws:
            v_mask = pd.Series([False] * len(df), index=df.index)
            for kw in name_kws:
                v_mask = v_mask | df['거래처명'].str.lower().str.contains(kw, na=False, regex=False)
            if v_mask.any():
                mask = mask & v_mask
                name_kws = []  # 거래처로 매칭됨

        # 품명/품번 필터
        if name_kws:
            txt_mask = pd.Series([False] * len(df), index=df.index)
            for col in df.columns:
                if '품' in col or 'item' in col.lower():
                    col_lower = df[col].str.lower()
                    for kw in name_kws:
                        txt_mask = txt_mask | col_lower.str.contains(kw, na=False, regex=False)
            mask = mask & txt_mask

        # 날짜 필터 적용
        mask = _apply_date_mask(df, mask, _date_f, _month_f, ['발주일자', '납기일자'])

        matched = df[mask]
        date_label = f" ({_date_f})" if _date_f else (f" ({_month_f[:4]}.{_month_f[4:]}월)" if _month_f else "")
        if not matched.empty:
            # 집계
            is_group = any(k in q for k in ['별', '별로', '집계', '요약'])
            if is_group and '거래처명' in df.columns:
                lines = [f'[외주발주 거래처별 집계{date_label} - {len(matched)}건]']
                for g, sub in matched.groupby('거래처명'):
                    total = sum(_num(v) for v in sub.get('합계금액', sub.get('poghAm1', [])))
                    lines.append(f"- {g}: {len(sub)}건 | 합계금액: {total:,.0f}원")
                return '\n'.join(lines)

            # 개별 나열 (최신순 정렬, 최대 10건)
            _wp_sort = '발주일자' if '발주일자' in matched.columns else None
            if _wp_sort:
                matched = matched.sort_values(_wp_sort, ascending=False)
            _wp_total = len(matched)
            _wp_trunc = _wp_total > 10
            matched_show = matched.head(10)
            _wp_header = f'[외주발주정보 조회 - {_wp_total}건'
            if _wp_trunc:
                _wp_header += f', 최신 10건 표시'
            _wp_header += ']'
            lines = [_wp_header, '']
            show_cols = ['외주발주번호', '발주일자', '납기일자', '거래처명', '품번', '품명',
                         '발주수량', '단가', '합계금액', '담당자', '비고']
            show_cols = [c for c in show_cols if c in matched.columns]
            for _, r in matched_show.iterrows():
                parts = []
                for col in show_cols:
                    val = r.get(col, '')
                    if val and str(val) not in ('nan', ''):
                        if col in ('발주수량', '단가', '합계금액', '공급가액', '부가세'):
                            try:
                                val = f"{float(val):,.0f}"
                            except Exception:
                                pass
                        parts.append(f"{col}: {val}")
                lines.append(' | '.join(parts))
            return '\n'.join(lines)
        # 외주발주에서 못 찾으면 구매발주로 fallthrough

    if ORDER_DF is None or ORDER_DF.empty:
        return ''

    q = query_lower
    df = ORDER_DF

    # 조사 제거용 클린 토큰
    _particles = ['에서', '으로', '한테', '에게', '별로', '로', '에', '의', '은', '는',
                  '이', '가', '을', '를', '과', '와', '도', '한', '해줘', '해']
    def _clean(tok):
        for p in sorted(_particles, key=len, reverse=True):
            if tok.endswith(p) and len(tok) > len(p) + 1:
                return tok[:-len(p)]
        return tok

    q_tokens = [_clean(t) for t in q.split() if len(t) >= 2]

    # 상태 필터 (발주확정, 입고완료, 발주대기)
    status_filter = None
    for s in ['발주확정', '입고완료', '발주대기']:
        if s in q:
            status_filter = s
            break

    # 거래처 필터 (거래처 또는 거래처명 컬럼)
    vendor_col = '거래처명' if '거래처명' in df.columns else ('거래처' if '거래처' in df.columns else None)
    vendor_hit = []
    if vendor_col:
        for v in df[vendor_col].unique():
            vl = str(v).lower()
            if not vl:
                continue
            if vl in q:
                vendor_hit.append(v)
            else:
                for tok in q_tokens:
                    if len(tok) >= 2 and tok in vl:
                        if v not in vendor_hit:
                            vendor_hit.append(v)

    # 품번/품명 필터 (날짜 토큰도 skip)
    skip = set(ORDER_TRIGGER_KW) | {'알려줘', '알려', '보여줘', '보여', '현황', '조회',
            '전체', '목록', '얼마', '몇', '있어', '있는', '내역', '정보',
            '거래처별', '거래처', '품목별', '품명별', '집계', '요약', '별로',
            '별', '기간', '월', '건', '수량', '금액', '합계', '오늘', '어제'} | _date_tokens
    if vendor_hit:
        skip |= set(v.lower() for v in vendor_hit)
        # 거래처 부분 매칭된 토큰도 skip에 추가
        if vendor_col:
            for v in vendor_hit:
                for tok in q_tokens:
                    if tok in v.lower():
                        skip.add(tok)
    if status_filter:
        skip.add(status_filter)
    name_kws = [t for t in q_tokens if t not in skip and len(t) >= 2]

    # 필터 적용
    mask = pd.Series([True] * len(df), index=df.index)

    if status_filter and '상태' in df.columns:
        mask = mask & (df['상태'] == status_filter)
    if vendor_hit and vendor_col:
        mask = mask & df[vendor_col].isin(vendor_hit)
    if name_kws:
        txt_mask = pd.Series([False] * len(df), index=df.index)
        for col in ['품번', '품명', '거래처', '거래처명']:
            if col in df.columns:
                col_lower = df[col].str.lower()
                for kw in name_kws:
                    txt_mask = txt_mask | col_lower.str.contains(kw, na=False, regex=False)
        mask = mask & txt_mask

    # 날짜 필터 적용
    mask = _apply_date_mask(df, mask, _date_f, _month_f, ['발주일자', '납기일자'])

    matched = df[mask]
    date_label = f" ({_date_f})" if _date_f else (f" ({_month_f[:4]}.{_month_f[4:]}월)" if _month_f else "")

    if matched.empty:
        if _date_f or _month_f:
            return f'[발주정보 조회{date_label} - 0건]\n해당 날짜에 발주 데이터가 없습니다. (데이터 범위: {df["발주일자"].min()} ~ {df["발주일자"].max()})'
        return ''

    # 별 키워드 → 집계
    is_group = any(k in q for k in ['별', '별로', '집계', '요약'])
    if is_group and '거래처' in df.columns:
        lines = [f'[발주정보 집계{date_label} - {len(matched)}건]']
        for g, sub in matched.groupby('거래처'):
            total_amt = sum(_num(v) for v in sub.get('합계금액', []))
            lines.append(f"- {g}: {len(sub)}건 | 합계금액: {total_amt:,.0f}원")
        return '\n'.join(lines)

    # 개별 나열 (최신순 정렬, 최대 10건)
    sort_col = '발주일자' if '발주일자' in matched.columns else None
    if sort_col:
        matched = matched.sort_values(sort_col, ascending=False)
    total_count = len(matched)
    show_limit = 10
    truncated = total_count > show_limit
    matched_show = matched.head(show_limit)
    header = f'[발주정보 조회{date_label} - {total_count}건'
    if truncated:
        header += f', 최신 {show_limit}건 표시'
    header += ']'
    lines = [header, '']
    for _, r in matched_show.iterrows():
        parts = []
        for col in ['발주번호', '발주일자', '납기일자', '거래처명', '거래처', '품번', '품명',
                     '발주수량', '단가', '합계금액', '상태']:
            if col in r and r[col] and str(r[col]) != 'nan':
                val = r[col]
                if col in ('발주수량', '단가', '합계금액', '공급가액', '부가세'):
                    try:
                        val = f"{float(val):,.0f}"
                    except Exception:
                        pass
                parts.append(f"{col}: {val}")
        lines.append(' | '.join(parts))
    return '\n'.join(lines)


# 발주정보 정적 요약
def _order_summary():
    if ORDER_DF is None or ORDER_DF.empty:
        return ''
    lines = [f'\n### 발주정보 현황 ({len(ORDER_DF)}건)']
    if '상태' in ORDER_DF.columns:
        for s, cnt in ORDER_DF['상태'].value_counts().items():
            lines.append(f"- {s}: {cnt}건")
    if '거래처' in ORDER_DF.columns:
        vendors = sorted(ORDER_DF['거래처'].unique())
        lines.append(f"- 거래처: {', '.join(vendors)}")
    return '\n'.join(lines)

ORDER_META = _order_summary()
if ORDER_META:
    print(f"[발주정보 요약] {len(ORDER_DF)}건 로드 완료")
    STATIC_META_TEXT += '\n' + ORDER_META

# 자사재고 관련 키워드
JASA_TRIGGER_KW = ['자사', '자사재고', '자사부자재', '생산러닝', '총재고', '원물']

# ────────────────────────────────────────────
# 쿼리 키워드 → 컬럼 매핑 (사용자 자연어 → 실제 컬럼)
# ────────────────────────────────────────────
# 컬럼 이름 동의어 매핑: 사용자가 쓸 만한 단어 → 실제 컬럼 객체
QUERY_COL_MAP = {
    '외주업체': COL_원산지,  '외주처': COL_원산지,  '외주별': COL_원산지,
    '외주': COL_원산지,    '업체': COL_원산지,
    '납품처': COL_납품처,    '납품': COL_납품처,   '채널': COL_납품처,
    '현재고량': COL_재고량,  '현재고': COL_재고량, '재고량': COL_재고량,
    '재고': COL_재고량,
    '기초재고': COL_창고재고, '기초재고량': COL_창고재고,
    '입고': COL_원가재고,
    '합계': COL_합계,
    '입수량': COL_입수량,    '입수': COL_입수량,
    '품명': COL_품명,
    '품번': COL_품목,        '품목': COL_품목,
    '규격': COL_규격,
    '단가': None,   # 단가는 PRICE_DF에서 조회
    '재고비용': None,
    '재고금액': None,
    '비용': None,
}

# ────────────────────────────────────────────
# ────────────────────────────────────────────
# 제품재고 사전 계산: 제품(생산) + 제품(출고) = 제품재고
# ────────────────────────────────────────────
PRODUCT_STOCK = {}  # (품번, 납품처) → {'생산': N, '출고': N, '제품재고': N}
_prod_rows = DF[DF[COL_규격] == '제품(생산)']
_ship_rows = DF[DF[COL_규격] == '제품(출고)']
for _, _r in _prod_rows.iterrows():
    _key = (str(_r[COL_품목]).strip(), str(_r[COL_납품처]).strip())
    _prod = _num(_r[COL_재고량])
    _ship_match = _ship_rows[
        (_ship_rows[COL_품목] == _r[COL_품목]) & (_ship_rows[COL_납품처] == _r[COL_납품처])
    ]
    _ship = _num(_ship_match.iloc[0][COL_재고량]) if not _ship_match.empty else 0
    PRODUCT_STOCK[_key] = {'생산': _prod, '출고': _ship, '제품재고': _prod + _ship}
print(f"[제품재고 계산] 제품(생산) {len(_prod_rows)}건 + 제품(출고) {len(_ship_rows)}건 → {len(PRODUCT_STOCK)}개 제품재고")

# ────────────────────────────────────────────
# 반제품재고 사전 계산: 반제품(생산) + 반제품(출고) + 반제품(풀고) = 반제품재고
# ※ 품번 컬럼이 '반제품(생산)' 리터럴이므로 품명+외주업체 기준 매칭
# ────────────────────────────────────────────
SEMI_PRODUCT_STOCK = {}  # (품명, 외주업체) → {'생산': N, '출고': N, '반제품재고': N, '납품처': str}
_semi_prod = DF[DF[COL_규격] == '반제품(생산)']
_semi_ship = DF[DF[COL_규격].isin(['반제품(출고)', '반제품(풀고)'])]
for _, _r in _semi_prod.iterrows():
    _품명 = str(_r[COL_품명]).strip()
    _외주 = str(_r[COL_원산지]).strip()
    _납품처 = str(_r[COL_납품처]).strip()
    _key = (_품명, _외주)
    _prod = _num(_r[COL_재고량])
    # 같은 품명+외주업체의 출고 합산 (반제품(출고) + 반제품(풀고))
    _ship_match = _semi_ship[
        (_semi_ship[COL_품명] == _r[COL_품명]) & (_semi_ship[COL_원산지] == _r[COL_원산지])
    ]
    _ship = sum(_num(v) for v in _ship_match[COL_재고량])
    SEMI_PRODUCT_STOCK[_key] = {
        '생산': _prod, '출고': _ship, '반제품재고': _prod + _ship, '납품처': _납품처
    }
print(f"[반제품재고 계산] 반제품(생산) {len(_semi_prod)}건 + 반제품(출고/풀고) {len(_semi_ship)}건 → {len(SEMI_PRODUCT_STOCK)}개 반제품재고")


# RAG: 관련 데이터 검색
# ────────────────────────────────────────────
def format_row_for_context(row: pd.Series) -> str:
    규격_val = str(row.get(COL_규격, '')).strip()

    # 제품(출고) 행은 건너뛰기 (제품(생산)에서 합산 표시)
    if 규격_val == '제품(출고)':
        return ''  # 빈 문자열 → 호출부에서 필터링

    parts = []
    # 품번은 값 유무와 관계없이 항상 첫 번째로 출력
    품번_val = row.get(COL_품목, '')
    parts.append(f"{COL_품목}: {품번_val if 품번_val and str(품번_val) not in ('nan', '') else '-'}")

    for col in KEY_COLS:
        if col == COL_품목:
            continue  # 이미 위에서 출력
        val = row.get(col, '')
        if val and str(val) not in ('nan', ''):
            # 규격 컬럼은 부재료 표시 변환 적용
            if col == COL_규격:
                parts.append(f"{col}: {display_규격(val)}")
            else:
                parts.append(f"{col}: {val}")

    # 제품(생산) 행: 제품재고(생산+출고) 합산 표시
    if 규격_val == '제품(생산)':
        품번_str = str(품번_val).strip()
        납품처_str = str(row.get(COL_납품처, '')).strip()
        ps = PRODUCT_STOCK.get((품번_str, 납품처_str))
        if ps:
            parts.append(f"제품생산: {ps['생산']:,.0f}")
            parts.append(f"제품출고: {ps['출고']:,.0f}")
            parts.append(f"제품재고: {ps['제품재고']:,.0f}")

    # 반제품(생산) 행: 반제품재고(생산+출고/풀고) 합산 표시
    if 규격_val == '반제품(생산)':
        품명_str = str(row.get(COL_품명, '')).strip()
        외주_str = str(row.get(COL_원산지, '')).strip()
        sp = SEMI_PRODUCT_STOCK.get((품명_str, 외주_str))
        if sp:
            parts.append(f"반제품생산: {sp['생산']:,.0f}")
            parts.append(f"반제품출고: {sp['출고']:,.0f}")
            parts.append(f"반제품재고: {sp['반제품재고']:,.0f}")

    # 단가 + 재고비용 추가
    price_info = get_price_info(row.get(COL_품목, ''), row.get(COL_품명, ''))
    if price_info and price_info.get('단가') is not None:
        단가 = price_info['단가']
        현재고 = _num(row.get(COL_재고량, 0))
        재고비용 = 현재고 * 단가
        basis = price_info.get('기준년월', '')
        parts.append(f"단가: {단가:,.0f}원({basis})")
        parts.append(f"재고비용: {재고비용:,.0f}원")
    else:
        parts.append("단가: 정보없음")
    # 부자재 규격 정보 추가 (부재료 제조업체/재질/사이즈)
    spec_info = get_spec_info(row.get(COL_품목, ''), row.get(COL_품명, ''))
    if spec_info:
        if spec_info.get('외주업체명') and spec_info['외주업체명'] not in ('', 'nan'):
            parts.append(f"부재료제조업체: {spec_info['외주업체명']}")
        if spec_info.get('규격(사이즈)') and spec_info['규격(사이즈)'] not in ('', 'nan'):
            parts.append(f"사이즈: {spec_info['규격(사이즈)']}")
        if spec_info.get('재질') and spec_info['재질'] not in ('', 'nan'):
            parts.append(f"재질: {spec_info['재질']}")
    daily_parts = []
    for col in DAILY_COLS:
        val = row.get(col, '')
        if val and str(val) not in ('nan', '', '0.0', '0'):
            daily_parts.append(f"{col}={val}")
    if daily_parts:
        parts.append(f"일별출고: {', '.join(daily_parts)}")
    return ' | '.join(parts)


def search_relevant_rows(query: str, max_rows: int = 30) -> str:
    query_lower = query.lower().strip()

    # ⓪-r 입고정보 검색
    _is_rcv_query = RCV_DF is not None and any(k in query_lower for k in RCV_TRIGGER_KW)
    if _is_rcv_query:
        rcv_result = search_receiving(query_lower)
        if rcv_result:
            return rcv_result

    # ⓪-w 생산계획(생산지시) 검색 — 생산실적보다 먼저 체크
    _is_wo_query = WO_DF is not None and any(k in query_lower for k in WO_TRIGGER_KW)
    if _is_wo_query:
        wo_result = search_work_order(query_lower)
        if wo_result:
            return wo_result

    # ⓪-p 생산실적 검색
    _is_prod_query = PROD_DF is not None and any(k in query_lower for k in PROD_TRIGGER_KW)
    if _is_prod_query:
        prod_result = search_production(query_lower)
        if prod_result:
            return prod_result

    # ⓪-0 출하/출고(매출) 검색
    _is_sales_query = (SHIP_DF is not None or ISSUE_DF is not None) and any(k in query_lower for k in SALES_TRIGGER_KW)
    if _is_sales_query:
        sales_result = search_sales(query_lower)
        if sales_result:
            return sales_result

    # ⓪-m Monday.com 검색
    _is_monday_query = MONDAY_DF is not None and any(k in query_lower for k in MONDAY_TRIGGER_KW)
    if _is_monday_query:
        monday_result = search_monday(query_lower)
        if monday_result:
            return monday_result

    # ⓪-a BOM 검색 (최우선)
    # BOM 키워드 직접 매칭 또는 (품번코드 + 생산/부족 키워드) 조합
    import re as _re_mod
    _has_code = bool(_re_mod.search(r'[a-zA-Z]\d{3,}', query))
    _has_prod_plan = any(k in query_lower for k in ['생산할', '생산계획', '생산예정', '생산하려', '생산해야'])
    _is_bom_query = BOM_DF is not None and (
        any(k in query_lower for k in BOM_TRIGGER_KW) or
        (_has_code and _has_prod_plan)
    )
    if _is_bom_query:
        bom_result = search_bom(query_lower)
        if bom_result:
            return bom_result

    # ⓪-b 발주정보 검색 ('발주' 키워드 포함 시)
    _is_order_query = ORDER_DF is not None and any(k in query_lower for k in ORDER_TRIGGER_KW)
    if _is_order_query:
        order_result = search_order(query_lower)
        if order_result:
            return order_result

    # ① 쿼리에서 언급된 컬럼들 감지
    mentioned_cols = set()
    for kw, col in QUERY_COL_MAP.items():
        if kw in query_lower:
            mentioned_cols.add(col)

    # ② 쿼리에서 언급된 실제 값 감지 (납품처명, 외주업체명 직접 언급)
    #    부분 매칭 지원: '롯데' → '롯데마트' 매칭
    def _strip_particles(token):
        t = token.strip('.,;:!?()[]')
        for p in ['에서는', '에서', '으로', '한테', '에게', '별로',
                  '로', '에', '의', '은', '는', '이', '가', '을', '를', '도']:
            if t.endswith(p) and len(t) > len(p) + 1:
                return t[:-len(p)]
        return t

    q_tokens_clean = [_strip_particles(k) for k in query_lower.split() if len(k) >= 2]

    filter_masks = {}  # col → mask
    for col in [COL_납품처, COL_원산지]:
        for val in _unique_vals(col):
            vl = val.lower()
            matched = False
            # 정방향: 값 전체가 쿼리에 포함 ('롯데마트' in query)
            if vl in query_lower:
                matched = True
            else:
                # 역방향: 쿼리 토큰이 값에 포함 ('롯데' in '롯데마트')
                for tok in q_tokens_clean:
                    if len(tok) >= 2 and tok in vl:
                        matched = True
                        break
            if matched:
                if col not in filter_masks:
                    filter_masks[col] = pd.Series([False] * len(DF), index=DF.index)
                filter_masks[col] = filter_masks[col] | (DF[col] == val)

    # ①-a 부재료 제조업체 직접 질문 (부자재 규격.xlsx 기준)
    #      제일산업, 동원시스템즈, 어기여차 등 → SPEC 데이터 우선 반환
    _spec_vendor_hit = []
    if SPEC_BY_VENDOR:
        for vk in SPEC_BY_VENDOR:
            if vk in query_lower:
                _spec_vendor_hit.append(vk)
        # 역방향 부분 매칭
        if not _spec_vendor_hit:
            for vk in SPEC_BY_VENDOR:
                for tok in q_tokens_clean:
                    if len(tok) >= 2 and tok in vk:
                        if vk not in _spec_vendor_hit:
                            _spec_vendor_hit.append(vk)

    if _spec_vendor_hit and '부재료' in query_lower:
        spec_results = []
        for vk in _spec_vendor_hit:
            spec_results.extend(SPEC_BY_VENDOR[vk])
        if spec_results:
            vendor_names = ', '.join(_spec_vendor_hit)
            lines = [f"[부재료 제조업체 '{vendor_names}' 부자재 규격 - {len(spec_results)}건]", ""]
            for info in spec_results:
                lines.append(format_spec_row(info))
            return '\n'.join(lines)

    # ①-b 자사+외주 통합 원재료/부재료 재고금액 질문 처리
    OEM_KW = ['외주업체', '외주처', '외주별', '외주소분', '소분업체', '외주', '업체']
    _has_jasa = '자사' in query_lower
    _has_oem  = any(k in query_lower for k in OEM_KW) and '제조' not in query_lower
    _has_원재료 = '원재료' in query_lower
    _has_부재료 = '부재료' in query_lower
    if (_has_jasa or _has_oem) and (_has_원재료 or _has_부재료):
        parts = []

        # 외주 데이터 (완제품 재고일지 기준) — 외주 언급 시에만
        if _has_oem:
            # 납품처/외주업체 필터가 있으면 적용 (롯데→롯데마트 등)
            base_mask = pd.Series([True] * len(DF), index=DF.index)
            if filter_masks:
                combined_filter = pd.Series([False] * len(DF), index=DF.index)
                for m in filter_masks.values():
                    combined_filter = combined_filter | m
                base_mask = combined_filter

            if _has_원재료:
                type_mask = base_mask & ~DF[COL_규격].isin(BUJAMYO_TYPES)
                matched = DF[type_mask]
                lines = [f'[외주 원재료 재고 - {len(matched)}건]',
                         f"컬럼: {', '.join(KEY_COLS)}", ""]
                for _, row in matched.iterrows():
                    _frc = format_row_for_context(row)
                    if _frc:
                        lines.append(_frc)
                if matched.empty:
                    lines = [f'[외주 원재료 - 해당 항목 없음]']
                parts.append('\n'.join(lines))
            elif _has_부재료:
                type_mask = base_mask & DF[COL_규격].isin(BUJAMYO_TYPES)
                matched = DF[type_mask]
                lines = [f'[외주 부재료 재고 - {len(matched)}건]',
                         f"컬럼: {', '.join(KEY_COLS)}", ""]
                for _, row in matched.iterrows():
                    _frc = format_row_for_context(row)
                    if _frc:
                        lines.append(_frc)
                if matched.empty:
                    lines = [f'[외주 부재료 - 해당 항목 없음]']
                parts.append('\n'.join(lines))

        # 자사 데이터
        if _has_jasa:
            jasa_r = search_jasa(query_lower)
            if jasa_r:
                parts.append(jasa_r)

        if parts:
            return '\n\n---\n\n'.join(parts)

    # ①-c BOM+생산계획 우선 처리 — 품번 + (생산/부족/소요/계획/확인) 조합
    #     "H0246 5000ea 생산 부재료 재고 확인" 같은 질문이 ②에 잡히지 않도록
    import re as _re_bom
    _bom_code_match = _re_bom.findall(r'[A-Za-z]\d{3,}', query)
    _bom_plan_kw = any(k in query_lower for k in [
        '생산계획', '생산할', '생산예정', '생산하', '부족수량', '부족량', '부족',
        '소요량', '필요량', '검토', '충분', '가능',
    ])
    # 수량(ea/개) + 생산/부재료 조합 → BOM 우선
    _has_qty = bool(_re_bom.search(r'\d+\s*(?:ea|EA|개|수량)', query))
    _bom_stock_kw = _has_qty and any(k in query_lower for k in ['생산', '부재료', '부족', '확인'])
    if _bom_code_match and (_bom_plan_kw or _bom_stock_kw) and BOM_DF is not None:
        bom_result = search_bom(query_lower)
        if bom_result:
            return bom_result
        # BOM이 없는 품번이지만 생산계획 질문 → BOM 미등록 안내
        missing_codes = [c.upper() for c in _bom_code_match if c.upper() not in set(BOM_DF['모품번'].unique())]
        if missing_codes:
            return f"[BOM 미등록 안내]\n품번 {', '.join(missing_codes)}에 대한 BOM(자재명세서)이 등록되어 있지 않습니다.\n아마란스 ERP에서 BOM 등록 후 다시 조회해 주세요."

    # ② 부재료 키워드 처리 — 외주(완제품재고일지) + 자사 양쪽 검색
    if '부재료' in query_lower:
        bujamyo_mask = DF[COL_규격].isin(BUJAMYO_TYPES)

        # ②-a 외주업체별 부재료 전체 집계 (그룹 집계 요청)
        is_vendor_group = (
            '별' in query_lower or '전체' in query_lower or '모든' in query_lower or
            any(k in query_lower for k in ['외주업체', '외주처', '외주별', '외주', '소분업체', '업체별'])
        ) and not filter_masks
        if is_vendor_group:
            oem_part = f"[외주업체별 부재료 재고 현황 - 전체 {len(DF[bujamyo_mask])}개 품목]\n\n{VENDOR_BUJAMYO_TEXT}"
            jasa_part = search_jasa(query_lower) if JASA_DF is not None else ''
            parts = [oem_part]
            if jasa_part:
                parts.append(jasa_part)
            return '\n\n---\n\n'.join(parts)

        # ②-b 특정 외주업체의 부재료만 조회
        oem_result = ''
        if COL_원산지 in filter_masks:
            vendor_bj_mask = bujamyo_mask & filter_masks[COL_원산지]
            matched_bj = DF[vendor_bj_mask]
            if not matched_bj.empty:
                vendor_names = DF[filter_masks[COL_원산지]][COL_원산지].unique().tolist()
                lines = [f"[외주 {', '.join(vendor_names)} 부재료 항목 - {len(matched_bj)}건]",
                         f"컬럼: {', '.join(KEY_COLS)}", ""]
                for _, row in matched_bj.iterrows():
                    _frc = format_row_for_context(row)
                    if _frc:
                        lines.append(_frc)
                oem_result = '\n'.join(lines)

        # ②-c 특정 납품처의 부재료 조회
        if not oem_result and COL_납품처 in filter_masks:
            dest_bj_mask = bujamyo_mask & filter_masks[COL_납품처]
            matched_bj = DF[dest_bj_mask]
            if not matched_bj.empty:
                matched_dests = DF[filter_masks[COL_납품처]][COL_납품처].unique().tolist()
                lines = [f"[외주 부재료 항목 ({', '.join(matched_dests)}) - {len(matched_bj)}건]",
                         f"컬럼: {', '.join(KEY_COLS)}", ""]
                for _, row in matched_bj.iterrows():
                    _frc = format_row_for_context(row)
                    if _frc:
                        lines.append(_frc)
                oem_result = '\n'.join(lines)

        # ②-d 필터 없는 일반 부재료 → 전체
        if not oem_result:
            matched_bj = DF[bujamyo_mask]
            if not matched_bj.empty:
                lines = [f"[외주 부재료 전체 항목 - {len(matched_bj)}건]",
                         f"컬럼: {', '.join(KEY_COLS)}", ""]
                for _, row in matched_bj.iterrows():
                    _frc = format_row_for_context(row)
                    if _frc:
                        lines.append(_frc)
                oem_result = '\n'.join(lines)

        # ②-e 자사 부재료도 항상 검색하여 결합
        jasa_part = search_jasa(query_lower) if JASA_DF is not None else ''

        parts = []
        if oem_result:
            parts.append(oem_result)
        if jasa_part:
            parts.append(jasa_part)
        if parts:
            return '\n\n---\n\n'.join(parts)

    # ③ "별" 키워드 → 그룹 집계 응답
    is_group_query = '별' in query_lower or '별로' in query_lower

    if is_group_query:
        has_jasa = '자사' in query_lower
        has_oem = any(k in query_lower for k in OEM_KW) and '제조' not in query_lower

        # 자사 + 외주 모두 언급 → 양쪽 결합
        if has_jasa and has_oem:
            parts = [f"[외주소분업체별 현재고량 및 재고비용 집계]\n{VENDOR_STOCK_TEXT}"]
            jasa_r = search_jasa(query_lower)
            if jasa_r:
                parts.append(jasa_r)
            else:
                parts.append(JASA_META)
            return '\n\n---\n\n'.join(parts)

        # 자사만 언급 → 자사재고 집계
        if has_jasa and not has_oem:
            jasa_r = search_jasa(query_lower)
            if jasa_r:
                return jasa_r
            return JASA_META

        # 외주만 언급
        if has_oem:
            return f"[외주소분업체별 현재고량 및 재고비용 집계]\n{VENDOR_STOCK_TEXT}"

        # 납품처별 집계
        if any(k in query_lower for k in ['납품처', '채널']):
            return f"[납품처별 현재고량 및 재고비용 집계]\n{DEST_STOCK_TEXT}"

    # ④ 목록/전체 조회 (특정 컬럼의 유니크값)
    LIST_TRIGGERS = ['목록', '종류', '어떤', '전체', '모든', '몇 개', '몇개', '어디어디', '어디 어디', '알려줘']
    if any(t in query_lower for t in LIST_TRIGGERS):
        # 부재료 제조업체 목록
        if any(k in query_lower for k in ['제조업체', '부재료업체', '부재료 업체', '부재료제조']):
            return (
                f"[부재료 제조업체 전체 목록 - {len(_spec_vendors)}개]\n" +
                '\n'.join(f"- {v}" for v in _spec_vendors)
            )
        # 외주소분업체 목록 (외주업체 단독 언급 → 소분업체)
        if any(k in query_lower for k in ['외주소분', '소분업체']):
            vals = _unique_vals(COL_원산지)
            return f"[외주소분업체 전체 목록 - {len(vals)}개]\n" + '\n'.join(f"- {v}" for v in vals)
        if any(k in query_lower for k in OEM_KW) and '제조' not in query_lower:
            vals = _unique_vals(COL_원산지)
            mfg_list = '\n'.join(f"- {v}" for v in _spec_vendors)
            return (
                f"[외주소분업체 전체 목록 - {len(vals)}개] (완제품 생산/소분 담당)\n"
                + '\n'.join(f"- {v}" for v in vals)
                + f"\n\n[부재료 제조업체 전체 목록 - {len(_spec_vendors)}개] (포장재 등 부재료 제조)\n"
                + mfg_list
            )
        if any(k in query_lower for k in ['납품처', '납품', '채널']):
            vals = _unique_vals(COL_납품처)
            return f"[납품처 전체 목록 - {len(vals)}개]\n" + '\n'.join(f"- {v}" for v in vals)
        if '부재료' in query_lower:
            return (
                "[부재료 규격 분류]\n"
                "- 부재료(부재료): 원본값 '부재료'\n"
                "- 부재료(단상자): 원본값 '단상자'\n"
                "- 부재료(물류박스): 원본값 '물류박스'\n"
                f"세 종류를 합쳐 '부재료'로 통칭합니다. "
                f"전체 {len(DF[DF[COL_규격].isin(BUJAMYO_TYPES)])}개 항목."
            )

    # ④-b 품번 코드 패턴 감지 후 MOQ / 단가 / 재고 조회
    import re as _re
    code_match = _re.findall(r'[A-Za-z]\d{3,}', query)
    moq_kw   = 'moq' in query_lower
    price_kw = any(k in query_lower for k in ['단가', '재고비용', '재고금액', '비용', '금액', '얼마'])
    stock_kw = any(k in query_lower for k in ['재고수량', '재고량', '재고', '수량', '몇개', '얼마나'])

    # 품번 + 재고 키워드 → 자사/외주 양쪽 재고 통합 조회
    if code_match and stock_kw:
        stock_lines = []
        for code in code_match:
            cu = code.upper()
            pi = get_price_info(cu, '')
            품명 = pi.get('품명', '') if pi else ''

            # 외주재고 (외주처별 중복 제거)
            inv_rows = DF[DF[COL_품목].str.upper() == cu]
            if not inv_rows.empty and not 품명:
                품명 = str(inv_rows.iloc[0][COL_품명])
            seen_vendors = set()
            oem_parts = []
            for _, r in inv_rows.iterrows():
                vendor = str(r.get(COL_원산지, '')).strip()
                if vendor in seen_vendors:
                    continue
                seen_vendors.add(vendor)
                재고 = _num(r.get(COL_재고량, 0))
                oem_parts.append(f"  - 외주 {vendor}: {재고:,.0f}")
            oem_total = sum(_num(r[COL_재고량]) for _, r in inv_rows.drop_duplicates(subset=[COL_원산지]).iterrows())

            # 자사재고
            jasa_stock = 0
            if JASA_DF is not None:
                j_match = JASA_DF[JASA_DF[JASA_COL_품번].str.upper() == cu]
                if not j_match.empty:
                    jasa_stock = _num(j_match.iloc[0][JASA_COL_총재고])
                    if not 품명:
                        품명 = str(j_match.iloc[0][JASA_COL_제품명])

            단가 = pi.get('단가', 0) if pi else 0
            단가_str = f" | 단가: {단가:,.0f}원" if 단가 else ""

            stock_lines.append(f"### 품번: {cu} | 품명: {품명}{단가_str}")
            if jasa_stock:
                stock_lines.append(f"  - 자사 재고: {jasa_stock:,.0f}")
            if oem_parts:
                stock_lines.extend(oem_parts)
                stock_lines.append(f"  - 외주 합계: {oem_total:,.0f}")
            total = jasa_stock + oem_total
            stock_lines.append(f"  ▶ 총 재고: {total:,.0f}")
            stock_lines.append('')

        if stock_lines:
            return '[품번 재고 조회 (자사+외주 통합)]\n\n' + '\n'.join(stock_lines)

    # MOQ 조회 (대소문자 무관, 부자재 규격 데이터에서 반환)
    if code_match and moq_kw:
        moq_lines = []
        for code in code_match:
            code_upper = code.upper()
            spec_info = SPEC_BY_CODE.get(code_upper)
            if spec_info:
                moq_val = spec_info.get('MOQ', '').strip()
                moq_lines.append(
                    f"품번: {code_upper} | 품명: {spec_info.get('품명','')} | "
                    f"부재료제조업체: {spec_info.get('외주업체명','')} | "
                    f"MOQ: {moq_val if moq_val and moq_val not in ('', 'nan') else '정보없음'}"
                )
            else:
                moq_lines.append(f"품번: {code_upper} | MOQ 정보 없음 (부자재 규격 데이터에 미등록)")
        if moq_lines:
            return '[MOQ 조회 - 부자재 규격 기준]\n' + '\n'.join(moq_lines)

    if code_match or price_kw:
        price_lines = []
        for code in code_match:
            code_upper = code.upper()

            # 부자재 규격 정보 (사이즈, 재질, MOQ)
            spec = get_spec_info(code_upper, '') if SPEC_BY_CODE else None
            spec_parts = []
            if spec:
                if spec.get('규격(사이즈)') and spec['규격(사이즈)'] not in ('', 'nan'):
                    spec_parts.append(f"사이즈: {spec['규격(사이즈)']}")
                if spec.get('재질') and spec['재질'] not in ('', 'nan'):
                    spec_parts.append(f"재질: {spec['재질']}")
                if spec.get('MOQ') and spec['MOQ'] not in ('', 'nan', '0'):
                    spec_parts.append(f"MOQ: {spec['MOQ']}")
                if spec.get('외주업체명') and spec['외주업체명'] not in ('', 'nan'):
                    spec_parts.append(f"부재료제조업체: {spec['외주업체명']}")
            spec_str = ' | '.join(spec_parts)

            # 제품재고 조회 (제품(생산)+제품(출고) 합산)
            ps_entries = [(k, v) for k, v in PRODUCT_STOCK.items() if k[0] == code_upper]
            ps_str = ''
            if ps_entries:
                ps_parts = []
                for (_, dest), ps in ps_entries:
                    ps_parts.append(f"{dest}: 제품생산 {ps['생산']:,.0f} / 제품출고 {ps['출고']:,.0f} / 제품재고 {ps['제품재고']:,.0f}")
                ps_str = ' | '.join(ps_parts)

            if code_upper in PRICE_BY_CODE:
                info = PRICE_BY_CODE[code_upper]
                단가 = info.get('단가')
                basis = info.get('기준년월', '')
                base_info = (
                    f"품번: {code_upper} | 품명: {info.get('품명','')} | "
                    f"거래처: {info.get('거래처','')} | 단가: {단가:,.0f}원 ({basis})"
                    if 단가 else f"품번: {code_upper} | 품명: {info.get('품명','')} | 단가: 정보없음"
                )
                if spec_str:
                    base_info += f" | {spec_str}"
                if ps_str:
                    base_info += f" | {ps_str}"
                price_lines.append(base_info)
            else:
                inv_rows = DF[DF[COL_품목] == code_upper]
                if not inv_rows.empty:
                    r = inv_rows.iloc[0]
                    pi = get_price_info(code_upper, r[COL_품명])
                    if not spec:
                        spec = get_spec_info(code_upper, r[COL_품명])
                        if spec:
                            spec_parts = []
                            if spec.get('규격(사이즈)') and spec['규격(사이즈)'] not in ('', 'nan'):
                                spec_parts.append(f"사이즈: {spec['규격(사이즈)']}")
                            if spec.get('재질') and spec['재질'] not in ('', 'nan'):
                                spec_parts.append(f"재질: {spec['재질']}")
                            if spec.get('MOQ') and spec['MOQ'] not in ('', 'nan', '0'):
                                spec_parts.append(f"MOQ: {spec['MOQ']}")
                            if spec.get('외주업체명') and spec['외주업체명'] not in ('', 'nan'):
                                spec_parts.append(f"부재료제조업체: {spec['외주업체명']}")
                            spec_str = ' | '.join(spec_parts)
                    if pi and pi.get('단가'):
                        현재고 = _num(r[COL_재고량])
                        line = (
                            f"품번: {code_upper} | 품명: {r[COL_품명]} | "
                            f"단가: {pi['단가']:,.0f}원({pi.get('기준년월','')}) | "
                            f"현재고량: {현재고:,.0f} | 재고비용: {현재고*pi['단가']:,.0f}원"
                        )
                    else:
                        line = f"품번: {code_upper} | 품명: {r[COL_품명]} | 단가 정보 없음"
                    if spec_str:
                        line += f" | {spec_str}"
                    if ps_str:
                        line += f" | {ps_str}"
                    price_lines.append(line)
                else:
                    line = f"품번: {code_upper} | 재고 및 단가 데이터에서 찾을 수 없음"
                    if spec_str:
                        line += f" | {spec_str}"
                    if ps_str:
                        line += f" | {ps_str}"
                    price_lines.append(line)
        if price_lines:
            return '[품번 조회 (단가·규격 통합)]\n' + '\n'.join(price_lines)

    # ④-c 제품재고 전용 검색 (제품(생산)+제품(출고) 합산)
    # ※ '반제품'은 ④-d에서 먼저 처리하므로 여기서 제외
    _is_product_query = (
        '반제품' not in query_lower and (
            any(k in query_lower for k in ['제품재고', '제품 재고', '제품생산', '제품출고', '완제품재고', '완제품 재고']) or
            ('제품' in query_lower and any(k in query_lower for k in ['재고', '수량', '현황', '얼마'])) or
            ('완제품' in query_lower and any(k in query_lower for k in ['재고', '수량', '현황', '얼마']))
        )
    )
    if _is_product_query:
        prod_mask = DF[COL_규격] == '제품(생산)'
        if filter_masks:
            fm_combined = pd.Series([False] * len(DF), index=DF.index)
            for m in filter_masks.values():
                fm_combined = fm_combined | m
            prod_mask = prod_mask & fm_combined

        prod_rows = DF[prod_mask]
        if not prod_rows.empty:
            lines = [f'[제품재고 조회 - {len(prod_rows)}건 (제품생산+제품출고 합산)]', '']
            for _, row in prod_rows.iterrows():
                품번 = str(row.get(COL_품목, '')).strip()
                품명 = str(row.get(COL_품명, '')).strip()
                납품처 = str(row.get(COL_납품처, '')).strip()
                외주업체 = str(row.get(COL_원산지, '')).strip()
                ps = PRODUCT_STOCK.get((품번, 납품처))
                if ps:
                    pi = get_price_info(품번, 품명)
                    단가 = pi.get('단가', 0) if pi else 0
                    비용 = ps['제품재고'] * 단가 if 단가 else 0
                    단가_str = f"단가: {단가:,.0f}원" if 단가 else "단가: 정보없음"
                    비용_str = f"재고비용: {비용:,.0f}원" if 비용 else ""
                    lines.append(
                        f"품번: {품번} | 품명: {품명} | 납품처: {납품처} | 외주업체: {외주업체} | "
                        f"제품생산: {ps['생산']:,.0f} | 제품출고: {ps['출고']:,.0f} | "
                        f"제품재고: {ps['제품재고']:,.0f} | {단가_str}"
                        + (f" | {비용_str}" if 비용_str else "")
                    )
            return '\n'.join(lines)

    # ④-d 반제품재고 전용 검색 (반제품(생산)+반제품(출고/풀고) 합산)
    _is_semi_query = (
        any(k in query_lower for k in ['반제품재고', '반제품 재고', '반제품생산', '반제품출고', '반제품']) or
        ('반제품' in query_lower and any(k in query_lower for k in ['재고', '수량', '현황', '얼마']))
    )
    if _is_semi_query:
        semi_mask = DF[COL_규격] == '반제품(생산)'
        if filter_masks:
            fm_combined = pd.Series([False] * len(DF), index=DF.index)
            for m in filter_masks.values():
                fm_combined = fm_combined | m
            semi_mask = semi_mask & fm_combined

        semi_rows = DF[semi_mask]
        if not semi_rows.empty:
            lines = [f'[반제품재고 조회 - {len(semi_rows)}건 (반제품생산+반제품출고 합산)]', '']
            for _, row in semi_rows.iterrows():
                품명 = str(row.get(COL_품명, '')).strip()
                납품처 = str(row.get(COL_납품처, '')).strip()
                외주업체 = str(row.get(COL_원산지, '')).strip()
                sp = SEMI_PRODUCT_STOCK.get((품명, 외주업체))
                if sp:
                    lines.append(
                        f"품명: {품명} | 납품처: {납품처} | 외주업체: {외주업체} | "
                        f"반제품생산: {sp['생산']:,.0f} | 반제품출고: {sp['출고']:,.0f} | "
                        f"반제품재고: {sp['반제품재고']:,.0f}"
                    )
            return '\n'.join(lines)

    # ④-e 원재료 전용 검색
    _is_raw_query = (
        '원재료' in query_lower and
        any(k in query_lower for k in ['재고', '수량', '현황', '얼마', '목록', '알려', '단가', '종류', '가격'])
    )
    if _is_raw_query and '자사' not in query_lower:
        _want_price = any(k in query_lower for k in ['단가', '가격', '종류', '목록'])

        # (1) 단가/종류/목록 질문 → 단가 CSV에서 원재료(A-prefix) 전체 조회
        if _want_price and PRICE_DF is not None:
            raw_price = PRICE_DF[PRICE_DF['품번'].str.strip().str.upper().str.startswith('A')]
            # 외주업체/납품처 필터 적용 (쿼리에 업체명 있으면)
            if q_tokens_clean:
                txt_mask = pd.Series([False] * len(raw_price), index=raw_price.index)
                for tok in q_tokens_clean:
                    if len(tok) >= 2 and tok not in {'원재료', '단가', '가격', '종류', '목록', '알려줘', '알려', '전체'}:
                        for col in ['품명', '거래처']:
                            txt_mask = txt_mask | raw_price[col].str.lower().str.contains(tok, na=False, regex=False)
                if txt_mask.any():
                    raw_price = raw_price[txt_mask]

            if not raw_price.empty:
                lines = [f'[원재료 단가 조회 (단가파일 기준) - {len(raw_price)}건]', '']
                for _, r in raw_price.iterrows():
                    품번 = str(r.get('품번', '')).strip()
                    품명 = str(r.get('품명', '')).strip()
                    거래처 = str(r.get('거래처', '')).strip()
                    단가 = r.get('최신단가', '')
                    기준 = str(r.get('기준년월', '')).strip()
                    lines.append(
                        f"품번: {품번} | 품명: {품명} | 거래처: {거래처} | "
                        f"단가: {단가}원 | 기준: {기준}"
                    )
                return '\n'.join(lines)

        # (2) 재고 질문 → 재고일지에서 원재료 조회 (기존 로직)
        raw_mask = DF[COL_규격] == '원재료'
        if filter_masks:
            fm_combined = pd.Series([False] * len(DF), index=DF.index)
            for m in filter_masks.values():
                fm_combined = fm_combined | m
            raw_mask = raw_mask & fm_combined

        raw_rows = DF[raw_mask]
        if not raw_rows.empty:
            lines = [f'[원재료 재고 조회 (재고일지 기준) - {len(raw_rows)}건]', '']
            for _, row in raw_rows.iterrows():
                품번 = str(row.get(COL_품목, '')).strip()
                품명 = str(row.get(COL_품명, '')).strip()
                납품처 = str(row.get(COL_납품처, '')).strip()
                외주업체 = str(row.get(COL_원산지, '')).strip()
                재고 = _num(row.get(COL_재고량, 0))
                pi = get_price_info(품번, 품명)
                단가 = pi.get('단가', 0) if pi else 0
                비용 = 재고 * 단가 if 단가 else 0
                단가_str = f"단가: {단가:,.0f}원" if 단가 else "단가: 정보없음"
                비용_str = f" | 재고비용: {비용:,.0f}원" if 비용 else ""
                lines.append(
                    f"품번: {품번} | 품명: {품명} | 납품처: {납품처} | 외주업체: {외주업체} | "
                    f"현재고량: {재고:,.0f} | {단가_str}{비용_str}"
                )
            return '\n'.join(lines)

    # ⑤ 특정 값 필터 검색 (e.g. "더고은 재고", "홈플러스 제품")
    if filter_masks:
        combined = pd.Series([False] * len(DF), index=DF.index)
        for m in filter_masks.values():
            combined = combined | m
        matched = DF[combined].head(max_rows)
        if not matched.empty:
            extra_info = ''
            # 외주업체 필터된 경우 전체 목록도 덧붙임
            if COL_원산지 in filter_masks:
                all_v = _unique_vals(COL_원산지)
                extra_info += f"\n[참고 - 전체 외주업체 목록]: {', '.join(all_v)}\n"
            lines = [f"[검색 결과: '{query}' 관련 {len(matched)}건]{extra_info}",
                     f"컬럼: {', '.join(KEY_COLS)}", ""]
            for _, row in matched.iterrows():
                lines.append(format_row_for_context(row))
            return '\n'.join(lines)

    # ⑥ 일반 키워드 검색 (제품명, 품번 등)
    keywords = [kw for kw in query_lower.split() if len(kw) >= 1]
    # 컬럼 이름 키워드 제외하고 제품/품번 키워드만 검색
    skip_kw = set(QUERY_COL_MAP.keys())
    value_kw = [kw for kw in keywords if kw not in skip_kw and len(kw) >= 2]

    # ⑥ 일반 키워드 검색 + 자사재고 동시 검색
    #    두 데이터 소스를 모두 탐색하여 누락 방지
    main_result = ''
    if value_kw:
        mask = pd.Series([False] * len(DF), index=DF.index)
        for col in [COL_품명, COL_품목, COL_납품처, COL_원산지, COL_규격]:
            col_lower = DF[col].str.lower()
            for kw in value_kw:
                mask = mask | col_lower.str.contains(kw, na=False, regex=False)
        matched = DF[mask].head(max_rows)
        if not matched.empty:
            lines = [f"[완제품 재고일지 검색: '{query}' 관련 {len(matched)}건]",
                     f"컬럼: {', '.join(KEY_COLS)}", ""]
            for _, row in matched.iterrows():
                lines.append(format_row_for_context(row))
            main_result = '\n'.join(lines)

    # ⑥-b 부자재 규격 검색
    SPEC_KW = ['재질', '사이즈', '규격', '크기', '소재', '성분', 'moq', '중량', '무게', '링크']
    is_spec_query_b = any(k in query_lower for k in SPEC_KW)
    spec_results = search_spec_by_query(query_lower)
    spec_result = ''
    if spec_results and (is_spec_query_b or len(spec_results) <= 10):
        lines = [f"[부자재 규격 정보 - {len(spec_results)}건]", ""]
        for info in spec_results:
            lines.append(format_spec_row(info))
        spec_result = '\n'.join(lines)

    # ⑥-c 자사 부자재 재고 검색 (항상 시도)
    jasa_result = ''
    if JASA_DF is not None:
        is_jasa_query = any(k in query_lower for k in JASA_TRIGGER_KW)
        # 부분 매칭 포함 (롯데→롯데마트, 홈플→홈플러스 등)
        def _partial_match(val_set, q_str):
            for v in val_set:
                if not v:
                    continue
                vl = v.lower()
                if vl in q_str:
                    return True
                for tok in q_str.split():
                    t = tok.strip('.,;:!?()[]')
                    for p in ['에서', '으로', '한테', '로', '에', '의', '은', '는', '이', '가', '을', '를', '도']:
                        if t.endswith(p) and len(t) > len(p) + 1:
                            t = t[:-len(p)]
                            break
                    if len(t) >= 2 and t in vl:
                        return True
            return False
        jasa_kw_hit = (
            _partial_match(JASA_KW_구분1, query_lower) or
            _partial_match(JASA_KW_업체, query_lower)
        )
        # 자사 키워드 OR 일반 검색 키워드 존재 시 자사재고도 검색
        if is_jasa_query or jasa_kw_hit or value_kw:
            jasa_result = search_jasa(query_lower)

    # ⑥-d 결과 결합 — 여러 소스에서 데이터가 있으면 모두 포함
    combined_parts = []
    if main_result:
        combined_parts.append(main_result)
    if jasa_result:
        combined_parts.append(jasa_result)
    if spec_result and not main_result:
        combined_parts.append(spec_result)
    if combined_parts:
        return '\n\n---\n\n'.join(combined_parts)

    # ⑦ Monday.com 검색 (마지막 시도 — 다른 핸들러에서 못 찾은 경우)
    if MONDAY_DF is not None:
        monday_fallback = search_monday(query_lower)
        if monday_fallback:
            return monday_fallback

    # ⑧ 아무것도 안 걸리면 전체 요약 반환
    return get_data_summary()


def get_data_summary() -> str:
    return (
        f"[전체 데이터 요약 - 3월 완제품 재고일지]\n"
        f"총 {len(DF)}개 항목\n\n"
        f"외주업체별 현재고량:\n{VENDOR_STOCK_TEXT}\n\n"
        f"납품처별 현재고량:\n{DEST_STOCK_TEXT}\n\n"
        f"컬럼 목록: {', '.join(KEY_COLS)}"
    )


# ────────────────────────────────────────────
# 시스템 프롬프트
# ────────────────────────────────────────────
SYSTEM_PROMPT = """당신은 매홍(maehong-JG) 회사의 완제품 재고 조회 전용 챗봇입니다.

## 핵심 규칙 (반드시 준수)
1. **데이터 전용 응답**: 오직 제공된 재고 데이터(CSV)에 있는 정보만 답변합니다.
2. **숫자/제품명 임의 생성 금지**: 데이터에 없는 수치나 제품명을 절대 만들어내지 마세요.
3. **출처 명시**: 답변 시 어떤 데이터를 참조했는지 명확히 알려주세요.
4. **데이터 없을 때**: 해당 정보가 데이터에 없으면 "해당 정보를 찾을 수 없습니다"라고 솔직하게 답하세요.
5. **한국어 응답**: 항상 한국어로 답변하세요.
6. **숫자 원본 유지**: 반올림, 단위 변환, 추산 없이 데이터 원본 값을 그대로 사용하세요.

## 데이터 구조 안내 (daily _ 완제품 재고일지 - 3월)
| 컬럼명 | 설명 |
|--------|------|
| 납품처 | 납품 채널 (홈플러스, 로켓배송, HBAF, 롯데마트, 올가니카, 마켓컬리, 이마트, 스낵24, 로켓프레시, 3P) |
| 품번 | 제품 품번 코드 |
| 규격 | 항목 분류. 부재료/단상자/물류박스는 모두 "부재료"로 통칭하며 부재료(부재료)·부재료(단상자)·부재료(물류박스) 형태로 표시 |
| 품명 | 제품명 |
| 외주업체(외주소분업체) | 완제품을 실제 생산·소분하는 업체. 재고일지의 "외주업체" 컬럼 (더고은, 데이웰즈, 마뤄아, 아리랑식품, 엔디에프팩킹, 정성, 청통본가, 한올담(해오름), 한조(경북친환경)) |
| 부재료 제조업체 | 부재료(포장재, 파우치 등)를 제조·공급하는 업체. 부자재 규격.xlsx의 "Name" 컬럼 (대성스텐실러, 대성인쇄 등) |
| 입수량 | 박스당 입수량 |
| pallet적재량 | 팔레트 적재량 |
| 기초재고량 | 기초(시작) 재고량 |
| 입고일지 | 입고 이력 |
| 현재고량 | 현재 재고량 ("재고", "재고량" 관련 질문 시 이 컬럼 참조) |
| 단가 | 26년 원부자재 단가 파일에서 품번 매칭(없으면 품명 유사 매칭)으로 조회 |
| 재고비용 | 현재고량 × 단가 (품번 매칭된 경우에만 계산 가능) |
| 사이즈 | 부자재 규격 파일의 규격(사이즈) 컬럼 (예: 70*175, 130*155+40) |
| 재질 | 부자재 규격 파일의 재질 컬럼 (예: 공판 PET12/AL7/NY15/CPR1 50) |
| MOQ | 최소주문수량 (부자재 규격 파일 기준, 대소문자 구분 없이 조회 가능) |
| 3월01일~3월31일 | 일별 출고/사용량 |
| 합계 | 월 합계 |
| 생산&부자재 사용 합계 | 생산 및 부자재 사용 합계 |
| 생산일수 | 생산 일수 |
| 전월 일평균필요량 | 전월 기준 일 평균 필요량 |
| 일평균필요량(전월기준) | 전월 기준 일 평균 필요량 계산값 |

## 응답 형식
- 마크다운 형식으로 정리된 답변을 제공하세요
- **품번은 모든 제품 응답에 반드시 포함**하세요. 품번이 없는 경우 "-"로 표시하세요
- 표(table) 형식 사용 시 첫 번째 컬럼은 반드시 품번이어야 합니다
- 숫자는 원본 데이터 그대로 표시하세요 (반올림 등 임의 수정 금지)
- 여러 제품 비교 시 표(table) 형식을 적극 활용하세요
- 재고량이 0이거나 음수인 경우 명확히 표시하세요

## BOM 응답 형식 (반드시 준수)
BOM 관련 질문에 답변할 때 반드시 아래 순서를 따르세요:
1. **제품정보를 먼저 표시** (품번, 품명, 품목구분, 단위)
2. 그 아래에 **BOM 구성 자재 목록**을 표 형식으로 표시
예시:
```
**제품정보**
- 품번: G0010
- 품명: [F] 매홍 무농약 고구마로 만든 군고구마말랭이 80g_5개
- 품목구분: 자사제품
- 단위: EA

**BOM 구성 (3건)**
| 자품번 | 자품명 | 구분 | 정미수량 | 실소요량 | 계정 |
...
```
- 데이터에 "제품정보" 섹션이 포함되어 있으면 **절대 생략하지 마세요**
- 모품번의 품명은 반드시 상단에 표시되어야 합니다

## 데이터 출처 구분 (매우 중요)
두 개의 재고 데이터가 있습니다. 질문 맥락에 따라 적절한 데이터를 사용하세요.

| 데이터 | 파일 | 주요 내용 |
|--------|------|-----------|
| **완제품 재고일지** | daily_완제품 재고일지 | 외주소분업체별 완제품/부재료 현재고량. 납품처(홈플러스, 쿠팡 등)별 관리 |
| **자사 부자재 재고** | 자사사용 부자재_REV | 자사가 직접 보유한 원물·부자재(파우치, PP, 단상자 등). 구분1(고구마/오트밀 등), 업체(납품채널), 총재고·창고재고·생산현장재고 포함 |
| **발주정보** | 아마란스10 API / 발주정보.csv | 거래처별 발주내역(발주번호, 발주일자, 납기일자, 품번, 품명, 발주수량, 단가, 합계금액, 상태) |

## 업체 구분 (매우 중요)
- **외주소분업체**: 재고일지의 "외주업체" 컬럼에 기재된 업체. 완제품을 생산·소분하는 업체.
- **부재료 제조업체**: 부자재 규격.xlsx의 "Name" 컬럼에 기재된 업체. 포장재(파우치, 라벨, 박스 등) 부재료를 제조하는 업체.
- 두 업체는 역할이 다릅니다. 사용자가 "외주업체"라고 하면 맥락에 따라 두 종류 모두 안내하세요.

## 자사 부자재 재고 컬럼
| 컬럼 | 설명 |
|------|------|
| 품번 | 부자재 품번 (A/B/C 코드) — 항상 첫 번째로 표시 |
| 제품명 | 부자재명 |
| 구분1 | 품목 대분류 (고구마, 오트밀, 공용, 카사바, 누룽지, 김맛카사바, 바나나칩) |
| 구분2 | 부자재 유형 (파우치, PP, RRP, 단상자, 용기, 원물, 롤파우치, 핸들캡, 게또바시) |
| 업체 | 납품채널 (쿠팡, 홈플러스, 스낵24, 이마트 등) |
| 총재고 | 공개 재고 수량 — 자사재고 질문 시 이 수치만 제공 |

※ 자사 부자재 재고는 **품번과 총재고만** 공개합니다. 창고재고·생산현장재고·기초재고·생산사용량 등 세부 내역은 제공하지 않습니다.

## 부자재 규격 데이터 (부자재 규격.xlsx)
- Name 컬럼 = **부재료 제조업체명** (예: 대성스텐실러, 대성인쇄)
- 품번으로 재고 데이터와 연결 가능
- 규격(사이즈): 포장재 크기 (예: 70*175mm, 130*155+40mm)
- 재질: 포장재 소재 구성 (예: 공판 PET12/AL7/NY15/CPR1 50)
- MOQ: 최소주문수량, 단가(원): 부자재 단가, 중량(g): 무게
- "재질 알려줘", "사이즈는?", "규격 정보" 등 질문 시 이 데이터 참조
- 외주업체명으로 해당 업체의 모든 부자재 규격 조회 가능"""


# ────────────────────────────────────────────
# ────────────────────────────────────────────
# 관리자 집계 데이터 (pre-computed)
# ────────────────────────────────────────────
def _build_admin_data():
    """외주업체별 부재료/원재료 재고금액 집계
    분류 기준 (품번 prefix):
      원재료 = A코드 | 부재료 = B, C, D코드 | 기타 = H, I, E, 반제품 등
    """
    vendors = _unique_vals(COL_원산지)
    result = []
    grand_bj_cost = 0
    grand_wj_cost = 0

    _bj_prefixes = ('B', 'C', 'D')

    for vendor in vendors:
        vendor_rows = DF[DF[COL_원산지] == vendor]
        # 품번 prefix 기준 분류
        bj_mask = vendor_rows[COL_품목].str.strip().str.upper().str[:1].isin(_bj_prefixes)
        wj_mask = vendor_rows[COL_품목].str.strip().str.upper().str.startswith('A')
        bj_rows = vendor_rows[bj_mask]
        wj_rows = vendor_rows[wj_mask]

        def _rows_to_items(rows, category):
            items = []
            seen_codes = set()
            for _, r in rows.iterrows():
                품번 = str(r.get(COL_품목, '')).strip()
                if 품번 in seen_codes:
                    continue
                seen_codes.add(품번)
                품명 = str(r.get(COL_품명, '')).strip()
                규격 = display_규격(r.get(COL_규격, ''))
                재고 = _num(r.get(COL_재고량, '0'))
                pi = get_price_info(품번, 품명)
                단가 = pi.get('단가', 0) if pi else 0
                비용 = 재고 * 단가
                items.append({
                    '품번': 품번,
                    '품명': 품명,
                    '규격': 규격,
                    '재고량': int(재고) if 재고 == int(재고) else 재고,
                    '단가': int(단가) if 단가 else 0,
                    '재고금액': int(비용) if 비용 else 0,
                    '단가유무': bool(단가),
                    '분류': category,
                })
            return items

        bj_items = _rows_to_items(bj_rows, '부재료')
        wj_items = _rows_to_items(wj_rows, '원재료')
        bj_cost = sum(i['재고금액'] for i in bj_items)
        wj_cost = sum(i['재고금액'] for i in wj_items)
        grand_bj_cost += bj_cost
        grand_wj_cost += wj_cost

        result.append({
            'vendor': vendor,
            'bj_items': bj_items,
            'wj_items': wj_items,
            'bj_cost': bj_cost,
            'wj_cost': wj_cost,
            'total_cost': bj_cost + wj_cost,
            'bj_count': len(bj_items),
            'wj_count': len(wj_items),
        })

    result.sort(key=lambda x: -x['total_cost'])

    # ── 자사재고 집계 ────────────────────────────────────────────────
    jasa_groups = []
    jasa_grand_bj = 0
    jasa_grand_wj = 0
    if JASA_DF is not None:
        for g1, sub in JASA_DF.groupby(JASA_COL_구분1):
            bj_sub = sub[sub[JASA_COL_구분2] != '원물']
            wj_sub = sub[sub[JASA_COL_구분2] == '원물']

            def _jasa_items(rows, cat):
                items = []
                for _, r in rows.iterrows():
                    품번 = str(r.get(JASA_COL_품번, '')).strip()
                    품명 = str(r.get(JASA_COL_제품명, '')).strip()
                    구분2 = str(r.get(JASA_COL_구분2, '')).strip()
                    업체 = str(r.get(JASA_COL_업체, '')).strip()
                    재고 = _num(r.get(JASA_COL_총재고, '0'))
                    pi = get_price_info(품번, 품명)
                    단가 = pi.get('단가', 0) if pi else 0
                    비용 = 재고 * 단가
                    items.append({
                        '품번': 품번, '품명': 품명, '업체': 업체,
                        '구분2': f'부재료({구분2})' if 구분2 != '원물' else '원재료',
                        '재고량': int(재고) if 재고 == int(재고) else 재고,
                        '단가': int(단가) if 단가 else 0,
                        '재고금액': int(비용) if 비용 else 0,
                        '단가유무': bool(단가),
                        '분류': cat,
                    })
                return items

            bj_items = _jasa_items(bj_sub, '부재료')
            wj_items = _jasa_items(wj_sub, '원재료')
            bj_cost = sum(i['재고금액'] for i in bj_items)
            wj_cost = sum(i['재고금액'] for i in wj_items)
            jasa_grand_bj += bj_cost
            jasa_grand_wj += wj_cost
            jasa_groups.append({
                'group': g1,
                'bj_items': bj_items, 'wj_items': wj_items,
                'bj_cost': bj_cost, 'wj_cost': wj_cost,
                'total_cost': bj_cost + wj_cost,
                'bj_count': len(bj_items), 'wj_count': len(wj_items),
            })
        jasa_groups.sort(key=lambda x: -x['total_cost'])

    return {
        'vendors': result,
        'grand_bj_cost': grand_bj_cost,
        'grand_wj_cost': grand_wj_cost,
        'grand_total': grand_bj_cost + grand_wj_cost,
        'csv_file': os.path.basename(CSV_PATH),
        'vendor_count': len(vendors),
        'total_rows': len(DF),
        # 자사재고
        'jasa_groups': jasa_groups,
        'jasa_grand_bj': jasa_grand_bj,
        'jasa_grand_wj': jasa_grand_wj,
        'jasa_grand_total': jasa_grand_bj + jasa_grand_wj,
        'jasa_total_rows': len(JASA_DF) if JASA_DF is not None else 0,
    }

ADMIN_DATA = _build_admin_data()
print(f"[관리자 집계] 외주업체 {ADMIN_DATA['vendor_count']}개, 총 재고금액 {ADMIN_DATA['grand_total']:,.0f}원")
if ADMIN_DATA.get('jasa_total_rows'):
    print(f"[관리자 집계] 자사재고 {ADMIN_DATA['jasa_total_rows']}개, 재고금액 {ADMIN_DATA['jasa_grand_total']:,.0f}원")


# ────────────────────────────────────────────
# API 엔드포인트
# ────────────────────────────────────────────
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/admin')
def admin():
    return render_template_string(ADMIN_TEMPLATE)


@app.route('/api/admin-data', methods=['GET'])
def admin_data():
    return jsonify(ADMIN_DATA)


# ────────────────────────────────────────────
# 외주처별 부재료 재고 상세 페이지
# ────────────────────────────────────────────
@app.route('/vendor/<vendor_name>')
def vendor_page(vendor_name):
    return render_template_string(VENDOR_TEMPLATE, vendor_name=vendor_name)


@app.route('/api/vendor-data/<vendor_name>', methods=['GET'])
def vendor_data(vendor_name):
    """외주처별 부재료+원재료 재고 상세 데이터 (A=원재료, B/C/D=부재료)"""
    _bj_prefixes = ('B', 'C', 'D')
    _wj_prefixes = ('A',)

    rows = DF[DF[COL_원산지] == vendor_name]
    if rows.empty:
        return jsonify({'vendor': vendor_name, 'bj_items': [], 'wj_items': [], 'summary': {}})

    bj_items = []
    wj_items = []
    seen_bj = set()
    seen_wj = set()

    for _, r in rows.iterrows():
        품번 = str(r.get(COL_품목, '')).strip()
        prefix = 품번.upper()[:1] if 품번 else ''

        if prefix in _bj_prefixes:
            if 품번 in seen_bj:
                continue
            seen_bj.add(품번)
            target = bj_items
        elif prefix in _wj_prefixes:
            if 품번 in seen_wj:
                continue
            seen_wj.add(품번)
            target = wj_items
        else:
            continue

        품명 = str(r.get(COL_품명, '')).strip()
        규격_raw = r.get(COL_규격, '')
        규격 = display_규격(규격_raw) if prefix in _bj_prefixes else str(규격_raw)
        재고 = _num(r.get(COL_재고량, '0'))
        pi = get_price_info(품번, 품명)
        단가 = pi.get('단가', 0) if pi else 0
        비용 = 재고 * 단가
        spec = get_spec_info(품번, 품명)
        제조업체 = spec.get('외주업체명', '') if spec else ''
        사이즈 = spec.get('규격(사이즈)', '') if spec else ''

        target.append({
            '품번': 품번, '품명': 품명, '규격': 규격,
            '분류': '원재료' if prefix in _wj_prefixes else '부재료',
            '재고량': int(재고) if 재고 == int(재고) else 재고,
            '단가': int(단가) if 단가 else 0,
            '재고금액': int(비용) if 비용 else 0,
            '단가유무': bool(단가),
            '제조업체': 제조업체,
            '사이즈': 사이즈,
        })

    bj_items.sort(key=lambda x: -x['재고금액'])
    wj_items.sort(key=lambda x: -x['재고금액'])

    bj_stock = sum(i['재고량'] for i in bj_items)
    bj_cost = sum(i['재고금액'] for i in bj_items)
    wj_stock = sum(i['재고량'] for i in wj_items)
    wj_cost = sum(i['재고금액'] for i in wj_items)

    return jsonify({
        'vendor': vendor_name,
        'bj_items': bj_items,
        'wj_items': wj_items,
        'summary': {
            'bj_count': len(bj_items), 'bj_stock': int(bj_stock), 'bj_cost': int(bj_cost),
            'wj_count': len(wj_items), 'wj_stock': int(wj_stock), 'wj_cost': int(wj_cost),
            'total_count': len(bj_items) + len(wj_items),
            'total_stock': int(bj_stock + wj_stock),
            'total_cost': int(bj_cost + wj_cost),
        }
    })


@app.route('/api/monday-item/<item_id>', methods=['GET'])
def monday_item_detail(item_id):
    """Monday.com 아이템 상세 조회 (실시간 API 호출)"""
    import requests as _req
    api_key = os.getenv('MONDAY_API_KEy')
    if not api_key:
        return jsonify({'error': 'Monday API 키 없음'}), 500

    query = f'''{{
        items(ids: [{item_id}]) {{
            id name created_at updated_at
            board {{ name }}
            group {{ title }}
            column_values {{
                column {{ title }}
                text
            }}
            subitems {{
                id name
                column_values {{
                    column {{ title }}
                    text
                }}
            }}
            updates(limit: 5) {{
                text_body
                created_at
                creator {{ name }}
            }}
        }}
    }}'''

    try:
        r = _req.post('https://api.monday.com/v2',
                      json={'query': query},
                      headers={'Authorization': api_key, 'Content-Type': 'application/json', 'API-Version': '2024-10'},
                      timeout=15)
        data = r.json()
        items = data.get('data', {}).get('items', [])
        if not items:
            return jsonify({'error': '아이템을 찾을 수 없습니다'}), 404

        item = items[0]
        result = {
            'id': item['id'],
            'name': item['name'],
            'board': item.get('board', {}).get('name', ''),
            'group': item.get('group', {}).get('title', ''),
            'created': item.get('created_at', '')[:10],
            'updated': item.get('updated_at', '')[:10],
            'columns': [],
            'subitems': [],
            'updates': [],
        }
        for cv in item.get('column_values', []):
            text = cv.get('text', '')
            if text:
                result['columns'].append({
                    'title': cv.get('column', {}).get('title', ''),
                    'value': text,
                })
        # 하위 아이템
        for si in item.get('subitems', []):
            sub = {'name': si['name'], 'columns': []}
            for scv in si.get('column_values', []):
                text = scv.get('text', '')
                if text:
                    sub['columns'].append({
                        'title': scv.get('column', {}).get('title', ''),
                        'value': text,
                    })
            result['subitems'].append(sub)

        for upd in item.get('updates', []):
            result['updates'].append({
                'text': upd.get('text_body', '')[:300],
                'date': upd.get('created_at', '')[:10],
                'author': upd.get('creator', {}).get('name', ''),
            })

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/jasa')
def jasa_page():
    return render_template_string(JASA_PAGE_TEMPLATE)


@app.route('/api/jasa-stock', methods=['GET'])
def jasa_stock():
    """자사 부자재 재고 상세 (중복 품번 제거)"""
    if JASA_DF is None:
        return jsonify({'items': [], 'summary': {}})

    seen = set()
    items = []
    total_stock = 0
    total_cost = 0

    for _, r in JASA_DF.iterrows():
        품번 = str(r.get(JASA_COL_품번, '')).strip()
        if not 품번 or 품번 in seen:
            continue
        seen.add(품번)

        품명 = str(r.get(JASA_COL_제품명, '')).strip()
        구분1 = str(r.get(JASA_COL_구분1, '')).strip()
        구분2 = str(r.get(JASA_COL_구분2, '')).strip()
        업체 = str(r.get(JASA_COL_업체, '')).strip()
        재고 = _num(r.get(JASA_COL_총재고, '0'))
        pi = get_price_info(품번, 품명)
        단가 = pi.get('단가', 0) if pi else 0
        비용 = 재고 * 단가
        분류 = '원재료' if 구분2 == '원물' else f'부재료({구분2})'

        total_stock += 재고
        total_cost += 비용
        items.append({
            '품번': 품번, '품명': 품명, '구분1': 구분1, '분류': 분류,
            '업체': 업체,
            '재고량': int(재고) if 재고 == int(재고) else 재고,
            '단가': int(단가) if 단가 else 0,
            '재고금액': int(비용) if 비용 else 0,
            '단가유무': bool(단가),
        })

    items.sort(key=lambda x: -x['재고금액'])
    return jsonify({
        'items': items,
        'summary': {
            'count': len(items),
            'totalStock': int(total_stock),
            'totalCost': int(total_cost),
        }
    })


JASA_PAGE_TEMPLATE = '''<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>자사 부자재 재고</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Pretendard','Noto Sans KR',sans-serif; background: #f8f9fa; color: #1a1a2e; }
    header { background: #1a1a2e; color: white; padding: 14px 28px; display: flex; align-items: center; gap: 14px; }
    header h1 { font-size: 18px; font-weight: 700; }
    .nav { margin-left: auto; display: flex; gap: 6px; }
    .nav a { color: white; text-decoration: none; background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.2); border-radius: 16px; padding: 4px 12px; font-size: 11px; font-weight: 600; }
    .nav a:hover { background: rgba(255,255,255,0.25); }
    .nav a.active { background: rgba(255,255,255,0.3); }
    .content { max-width: 1200px; margin: 24px auto; padding: 0 20px; }
    .sum-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 24px; }
    .sum-card { background: white; border-radius: 12px; padding: 18px 22px; border: 1px solid #e5e7eb; }
    .sum-card .label { font-size: 11px; color: #888; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
    .sum-card .val { font-size: 24px; font-weight: 800; color: #1a1a2e; margin-top: 4px; }
    .sum-card .sub { font-size: 11px; color: #aaa; margin-top: 2px; }
    .filter-row { display: flex; gap: 6px; margin-bottom: 14px; flex-wrap: wrap; }
    .filter-btn { padding: 5px 14px; border: 1px solid #e5e7eb; border-radius: 16px; background: white; font-size: 12px; cursor: pointer; font-weight: 600; color: #666; }
    .filter-btn:hover { border-color: #166534; color: #166534; }
    .filter-btn.active { background: #166534; color: white; border-color: #166534; }
    table { width: 100%; border-collapse: collapse; background: white; border-radius: 12px; overflow: hidden; border: 1px solid #e5e7eb; }
    thead th { background: #f8f9fa; padding: 10px 14px; font-size: 11px; font-weight: 700; color: #555; text-align: left; border-bottom: 2px solid #e5e7eb; }
    tbody td { padding: 9px 14px; font-size: 13px; border-bottom: 1px solid #f3f4f6; }
    tbody tr:hover td { background: #f8f9fa; }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    .cost { font-weight: 600; }
    .no-price { color: #ccc; font-style: italic; font-size: 12px; }
    .chip { display: inline-block; padding: 2px 8px; border-radius: 8px; font-size: 10px; font-weight: 600; }
    .chip-raw { background: #dbeafe; color: #2563eb; }
    .chip-bj { background: #fee2e2; color: #dc2626; }
    .loading { text-align: center; padding: 60px; color: #999; }
  </style>
</head>
<body>
<header>
  <h1>자사 — 부자재 재고 현황</h1>
  <div class="nav">
    <a href="/">챗봇</a>
    <a href="/admin">관리자</a>
    <a href="/vendor/데이웰즈">데이웰즈</a>
    <a href="/vendor/더고은">더고은</a>
    <a href="/vendor/정성">정성</a>
    <a href="/vendor/청통본가">청통본가</a>
    <a href="/jasa" class="active">자사</a>
  </div>
</header>
<div class="content">
  <div class="sum-grid" id="summary"></div>
  <div class="filter-row" id="filters"></div>
  <div id="table-area"><div class="loading">로딩 중...</div></div>
</div>
<script>
let ALL_ITEMS = [];
let currentFilter = 'all';

function fmt(n) { return n ? Number(n).toLocaleString('ko-KR') : '0'; }

function chipFor(분류) {
  if (분류 === '원재료') return '<span class="chip chip-raw">원재료</span>';
  return '<span class="chip chip-bj">' + 분류 + '</span>';
}

async function load() {
  const res = await fetch('/api/jasa-stock');
  const d = await res.json();
  ALL_ITEMS = d.items;
  const s = d.summary;

  document.getElementById('summary').innerHTML = `
    <div class="sum-card">
      <div class="label">총 품목 수</div>
      <div class="val">${fmt(s.count)}개</div>
      <div class="sub">중복 제거 기준</div>
    </div>
    <div class="sum-card">
      <div class="label">총 재고수량</div>
      <div class="val">${fmt(s.totalStock)}</div>
      <div class="sub">총재고 합계</div>
    </div>
    <div class="sum-card">
      <div class="label">총 재고금액</div>
      <div class="val">${fmt(s.totalCost)}원</div>
      <div class="sub">총재고 × 단가</div>
    </div>
  `;

  // 구분1 필터 버튼 생성
  const cats = ['all', ...new Set(ALL_ITEMS.map(i => i.구분1).filter(Boolean))];
  const labels = { all: '전체' };
  document.getElementById('filters').innerHTML = cats.map(c =>
    `<button class="filter-btn ${c === 'all' ? 'active' : ''}" onclick="applyFilter('${c}')">${labels[c] || c}</button>`
  ).join('');

  renderTable(ALL_ITEMS);
}

function applyFilter(cat) {
  currentFilter = cat;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.textContent === (cat === 'all' ? '전체' : cat)));
  const filtered = cat === 'all' ? ALL_ITEMS : ALL_ITEMS.filter(i => i.구분1 === cat);
  renderTable(filtered);
}

function renderTable(items) {
  if (!items.length) {
    document.getElementById('table-area').innerHTML = '<div class="loading">데이터 없음</div>';
    return;
  }
  const rows = items.map((it, i) => `
    <tr>
      <td>${i+1}</td>
      <td><strong>${it.품번}</strong></td>
      <td>${it.품명}</td>
      <td>${chipFor(it.분류)}</td>
      <td>${it.구분1}</td>
      <td>${it.업체}</td>
      <td class="num">${fmt(it.재고량)}</td>
      <td class="num">${it.단가유무 ? fmt(it.단가) + '원' : '<span class="no-price">-</span>'}</td>
      <td class="num cost">${it.단가유무 ? fmt(it.재고금액) + '원' : '<span class="no-price">-</span>'}</td>
    </tr>
  `).join('');

  document.getElementById('table-area').innerHTML = `
    <table>
      <thead><tr>
        <th>#</th><th>품번</th><th>품명</th><th>분류</th><th>구분</th><th>업체</th>
        <th class="num">총재고</th><th class="num">단가</th><th class="num">재고금액</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

load();
</script>
</body>
</html>'''


VENDOR_TEMPLATE = '''<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ vendor_name }} 부재료 재고</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Pretendard','Noto Sans KR',sans-serif; background: #f8f9fa; color: #1a1a2e; }
    header { background: #1a1a2e; color: white; padding: 14px 28px; display: flex; align-items: center; gap: 14px; }
    header h1 { font-size: 18px; font-weight: 700; }
    .nav { margin-left: auto; display: flex; gap: 6px; }
    .nav a { color: white; text-decoration: none; background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.2); border-radius: 16px; padding: 4px 12px; font-size: 11px; font-weight: 600; }
    .nav a:hover { background: rgba(255,255,255,0.25); }
    .nav a.active { background: rgba(255,255,255,0.3); }
    .content { max-width: 1200px; margin: 24px auto; padding: 0 20px; }
    .sum-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 24px; }
    .sum-card { background: white; border-radius: 12px; padding: 18px 22px; border: 1px solid #e5e7eb; }
    .sum-card .label { font-size: 11px; color: #888; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
    .sum-card .val { font-size: 24px; font-weight: 800; color: #1a1a2e; margin-top: 4px; }
    .sum-card .sub { font-size: 11px; color: #aaa; margin-top: 2px; }
    table { width: 100%; border-collapse: collapse; background: white; border-radius: 12px; overflow: hidden; border: 1px solid #e5e7eb; }
    thead th { background: #f8f9fa; padding: 10px 14px; font-size: 11px; font-weight: 700; color: #555; text-align: left; border-bottom: 2px solid #e5e7eb; text-transform: uppercase; letter-spacing: 0.3px; }
    tbody td { padding: 9px 14px; font-size: 13px; border-bottom: 1px solid #f3f4f6; }
    tbody tr:hover td { background: #f8f9fa; }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    .cost { font-weight: 600; }
    .no-price { color: #ccc; font-style: italic; font-size: 12px; }
    .chip { display: inline-block; padding: 2px 8px; border-radius: 8px; font-size: 10px; font-weight: 600; }
    .chip-bj { background: #fee2e2; color: #dc2626; }
    .chip-box { background: #dbeafe; color: #2563eb; }
    .chip-etc { background: #f3f4f6; color: #666; }
    .loading { text-align: center; padding: 60px; color: #999; }
  </style>
</head>
<body>
<header>
  <h1>{{ vendor_name }} — 부재료·원재료 재고 현황</h1>
  <div class="nav">
    <a href="/">챗봇</a>
    <a href="/admin">관리자</a>
    <a href="/vendor/데이웰즈" class="{% if vendor_name == '데이웰즈' %}active{% endif %}">데이웰즈</a>
    <a href="/vendor/더고은" class="{% if vendor_name == '더고은' %}active{% endif %}">더고은</a>
    <a href="/vendor/정성" class="{% if vendor_name == '정성' %}active{% endif %}">정성</a>
    <a href="/vendor/청통본가" class="{% if vendor_name == '청통본가' %}active{% endif %}">청통본가</a>
  </div>
</header>
<div class="content">
  <div class="sum-grid" id="summary"></div>
  <div id="table-area"><div class="loading">로딩 중...</div></div>
</div>
<script>
const VENDOR = '{{ vendor_name }}';

function fmt(n) { return n ? Number(n).toLocaleString('ko-KR') : '0'; }

function chipFor(규격) {
  if (규격.includes('부재료')) return '<span class="chip chip-bj">' + 규격 + '</span>';
  if (규격.includes('물류') || 규격.includes('단상자')) return '<span class="chip chip-box">' + 규격 + '</span>';
  return '<span class="chip chip-etc">' + 규격 + '</span>';
}

async function load() {
  const res = await fetch('/api/vendor-data/' + encodeURIComponent(VENDOR));
  const d = await res.json();
  const s = d.summary;

  document.getElementById('summary').innerHTML = `
    <div class="sum-card" style="border-left:4px solid #e94560">
      <div class="label">부재료 (B·C·D)</div>
      <div class="val">${fmt(s.bj_count)}개 / ${fmt(s.bj_cost)}원</div>
      <div class="sub">재고수량 ${fmt(s.bj_stock)}</div>
    </div>
    <div class="sum-card" style="border-left:4px solid #059669">
      <div class="label">원재료 (A)</div>
      <div class="val">${fmt(s.wj_count)}개 / ${fmt(s.wj_cost)}원</div>
      <div class="sub">재고수량 ${fmt(s.wj_stock)}</div>
    </div>
    <div class="sum-card" style="border-left:4px solid #7c3aed">
      <div class="label">합계</div>
      <div class="val">${fmt(s.total_count)}개 / ${fmt(s.total_cost)}원</div>
      <div class="sub">총 재고수량 ${fmt(s.total_stock)}</div>
    </div>
  `;

  const bj = d.bj_items || [];
  const wj = d.wj_items || [];
  if (bj.length === 0 && wj.length === 0) {
    document.getElementById('table-area').innerHTML = '<div class="loading">해당 외주처의 데이터가 없습니다.</div>';
    return;
  }

  function buildTable(items, title, color) {
    if (!items.length) return `<h3 style="color:${color};margin:16px 0 8px">${title} (0건)</h3>`;
    let rows = items.map((it, i) => `
      <tr>
        <td>${i+1}</td>
        <td><strong>${it.품번}</strong></td>
        <td>${it.품명}</td>
        <td>${chipFor(it.규격)}</td>
        <td class="num">${fmt(it.재고량)}</td>
        <td class="num">${it.단가유무 ? fmt(it.단가) + '원' : '<span class="no-price">-</span>'}</td>
        <td class="num cost">${it.단가유무 ? fmt(it.재고금액) + '원' : '<span class="no-price">-</span>'}</td>
        <td>${it.제조업체 || '-'}</td>
        <td>${it.사이즈 || '-'}</td>
      </tr>
    `).join('');
    const totalCost = items.reduce((s,i) => s + i.재고금액, 0);
    return `
      <h3 style="color:${color};margin:16px 0 8px">${title} (${items.length}건)</h3>
      <table>
        <thead>
          <tr>
            <th>#</th><th>품번</th><th>품명</th><th>규격</th>
            <th class="num">재고량</th><th class="num">단가</th><th class="num">재고금액</th>
            <th>제조업체</th><th>사이즈</th>
          </tr>
        </thead>
        <tbody>${rows}
          <tr style="background:#f8f9fa;font-weight:700">
            <td colspan="6">소계 (${items.length}품목)</td>
            <td class="num">${fmt(totalCost)}원</td><td></td><td></td>
          </tr>
        </tbody>
      </table>`;
  }

  document.getElementById('table-area').innerHTML =
    buildTable(bj, '부재료 (B·C·D코드)', '#e94560') +
    buildTable(wj, '원재료 (A코드)', '#059669');
}

load();
</script>
</body>
</html>'''


# ────────────────────────────────────────────
# 엑셀 업로드 기능 — /upload 페이지
# ────────────────────────────────────────────
UPLOAD_DIR = 'C:/Users/jgkim/maehong-JG'

UPLOAD_FILES = {
    '재고파일 (원자재부자재 재고파악)': {
        'accept': '.xlsx',
        'target': '원자재부자재 재고파악(3월) - 최종본.xlsx',
        'convert': 'convert_excel.py',
        'desc': '외주업체 재고일지 (daily _ 완제품 재고일지 시트)',
    },
    '단가파일 (26년 원부자재 단가)': {
        'accept': '.xlsx',
        'target': '26년 원부자재 단가.xlsx',
        'convert': 'convert_price.py',
        'desc': '부자재·완제품 단가 데이터',
    },
    '부자재 규격': {
        'accept': '.xlsx',
        'target': '부자재 규격.xlsx',
        'convert': 'convert_spec.py',
        'desc': '부재료 제조업체·사이즈·재질·MOQ',
    },
    '자사재고 (자사사용 부자재)': {
        'accept': '.xlsx',
        'target': '자사사용 부자재_REV.260224_지우철_1.xlsx',
        'convert': 'convert_jasa.py',
        'desc': '자사 부자재 총재고 (생산러닝 부자재 시트)',
    },
}


@app.route('/shared/<share_id>')
def shared_page(share_id):
    """공유된 대화 페이지"""
    return render_template_string('''<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>공유된 대화 - 매홍 L&F</title>
<script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore-compat.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;600;700&display=swap" rel="stylesheet">
<style>
body{font-family:'Noto Sans KR',sans-serif;background:#f8fafc;margin:0;padding:20px}
.container{max-width:800px;margin:0 auto;background:white;border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,0.08);overflow:hidden}
.header{padding:16px 24px;border-bottom:1px solid #e5e7eb;background:#f8fafc}
.header h2{font-size:16px;color:#1e293b;margin-bottom:4px}
.header p{font-size:12px;color:#94a3b8}
.messages{padding:16px 24px}
.msg{margin-bottom:14px;padding:10px 14px;border-radius:12px;font-size:14px;line-height:1.6}
.msg.user{background:#e0e7ff;margin-left:20%;text-align:right;color:#1e293b}
.msg.bot{background:#f1f5f9;margin-right:10%;color:#1e293b}
.msg.bot table{border-collapse:collapse;width:100%;font-size:12px;margin:8px 0}
.msg.bot th{background:#334155;color:white;padding:6px 10px;text-align:left}
.msg.bot td{padding:5px 10px;border-bottom:1px solid #e5e7eb}
a.back{display:inline-block;margin:16px 24px;color:#6366f1;text-decoration:none;font-size:13px}
</style></head><body>
<div class="container">
  <div class="header"><h2 id="title">로딩 중...</h2><p id="info"></p></div>
  <div class="messages" id="msgs"></div>
  <a class="back" href="/">← 챗봇으로 이동</a>
</div>
<script>
firebase.initializeApp({apiKey:"AIzaSyBZ1FfTibE-KBkTbZJnTNEqz-pxsgew03k",authDomain:"maehong-scm.firebaseapp.com",projectId:"maehong-scm"});
const db=firebase.firestore();
const shareId="''' + share_id + '''";
db.collection("shared").doc(shareId).get().then(doc=>{
  if(!doc.exists){document.getElementById("title").textContent="공유 링크를 찾을 수 없습니다";return}
  const d=doc.data(),chat=d.chatData||{};
  document.getElementById("title").textContent=chat.title||"공유된 대화";
  document.getElementById("info").textContent="공유: "+(d.sharedByName||"")+" | "+new Date(d.sharedAt?.toDate()).toLocaleDateString("ko-KR");
  const container=document.getElementById("msgs");
  (chat.messages||[]).forEach(m=>{
    const div=document.createElement("div");
    div.className="msg "+(m.role==="user"?"user":"bot");
    div.innerHTML=m.role==="user"?m.content:marked.parse(m.content);
    container.appendChild(div);
  });
});
</script></body></html>''')


@app.route('/upload')
def upload_page():
    return render_template_string(UPLOAD_TEMPLATE)


@app.route('/api/upload', methods=['POST'])
def upload_file():
    import subprocess, shutil

    file_type = request.form.get('file_type', '')
    if file_type not in UPLOAD_FILES:
        return jsonify({'ok': False, 'msg': f'알 수 없는 파일 종류: {file_type}'}), 400

    info = UPLOAD_FILES[file_type]
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'ok': False, 'msg': '파일이 선택되지 않았습니다.'}), 400

    target_path = os.path.join(UPLOAD_DIR, info['target'])

    # 기존 파일 백업
    if os.path.exists(target_path):
        backup = target_path + '.bak'
        shutil.copy2(target_path, backup)

    # 새 파일 저장
    f.save(target_path)

    # 변환 스크립트 실행
    convert_script = os.path.join(UPLOAD_DIR, info['convert'])
    try:
        result = subprocess.run(
            ['python', convert_script],
            capture_output=True, text=True, timeout=60,
            cwd=UPLOAD_DIR, encoding='utf-8', errors='replace'
        )
        if result.returncode != 0:
            return jsonify({
                'ok': False,
                'msg': f'변환 실패: {result.stderr[:300]}',
            }), 500
    except Exception as e:
        return jsonify({'ok': False, 'msg': f'변환 오류: {str(e)}'}), 500

    return jsonify({
        'ok': True,
        'msg': f'✅ {file_type} 업로드 완료!\n파일: {info["target"]}\n변환: {info["convert"]} 실행 성공\n\n⚠️ 서버 재시작 후 데이터가 반영됩니다.',
    })


@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message', '').strip()
    chat_history = data.get('history', [])

    if not user_message:
        return jsonify({'error': '메시지가 비어 있습니다.'}), 400

    # RAG: 관련 데이터 검색
    context = search_relevant_rows(user_message)

    # Monday.com 결과는 GPT를 거치지 않고 직접 반환 (링크 보존)
    if context.startswith('[Monday.com'):
        return jsonify({
            'message': context,
            'context_rows': len(context.split('\n')),
            'source': 'monday'
        })

    # 메시지 구성 (정적 메타데이터는 항상 포함)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": STATIC_META_TEXT},
        {"role": "system", "content": f"## 검색된 참조 데이터 (이 데이터만 사용하여 답변하세요)\n\n{context}"}
    ]

    # 대화 히스토리 추가 (최근 10개)
    for h in chat_history[-10:]:
        messages.append({"role": h["role"], "content": h["content"]})

    # 현재 사용자 메시지
    messages.append({"role": "user", "content": user_message})

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0,  # 할루시네이션 방지를 위해 temperature=0
            max_tokens=2000
        )

        assistant_message = response.choices[0].message.content

        return jsonify({
            'message': assistant_message,
            'context_rows': len(context.split('\n'))
        })

    except Exception as e:
        return jsonify({'error': f'API 오류: {str(e)}'}), 500


@app.route('/api/data-info', methods=['GET'])
def data_info():
    """데이터 현황 요약"""
    destinations = DF[COL_납품처].replace('', pd.NA).dropna().unique().tolist()
    return jsonify({
        'total_rows': len(DF),
        'total_columns': len(DF.columns),
        'csv_file': os.path.basename(CSV_PATH),
        'destinations': [d for d in destinations if d],
        'columns': list(DF.columns)
    })


@app.route('/api/suggest', methods=['GET'])
def suggest():
    """유사어 추천 — 입력 키워드로 품명/품번/거래처 검색 (G/H/I 품번 포함, 내림차순)"""
    q = request.args.get('q', '').strip().lower()
    if len(q) < 1:
        return jsonify([])

    results = []
    seen = set()
    max_per = 15

    # ── 1) BOM 모품번 + 모품명 매칭 (G/H/I 제품코드 우선) ──
    if BOM_DF is not None:
        bom_unique = BOM_DF.drop_duplicates(subset=['모품번'])[['모품번', '모품명']].values.tolist()
        # G → H → I → E → F 순서 (같은 prefix 내에서는 오름차순)
        _prefix_order = {'G': 0, 'H': 1, 'I': 2, 'E': 3, 'F': 4}
        bom_unique.sort(key=lambda x: (_prefix_order.get(x[0][0], 9), x[0]))
        for parent, nm_raw in bom_unique:
            nm = str(nm_raw)[:35] if nm_raw else ''
            # 품번 또는 품명에서 매칭
            if q in parent.lower() or q in nm.lower():
                key = f'BOM:{parent}'
                if key not in seen:
                    seen.add(key)
                    results.append({'type': '제품(BOM)', 'value': parent, 'label': f"{parent} | {nm}"})
                    if len([x for x in results if x['type'] == '제품(BOM)']) >= max_per:
                        break

    # ── 2) 발주정보 품번 매칭 (내림차순) ──
    if ORDER_DF is not None and '품번' in ORDER_DF.columns:
        order_codes = sorted(ORDER_DF['품번'].unique(), reverse=True)
        for code in order_codes:
            if q in code.lower():
                key = f'발주:{code}'
                if key not in seen:
                    seen.add(key)
                    nm_rows = ORDER_DF[ORDER_DF['품번'] == code]
                    nm = str(nm_rows['품명'].iloc[0])[:30] if '품명' in nm_rows.columns and not nm_rows.empty else ''
                    results.append({'type': '발주품번', 'value': code, 'label': f"{code} | {nm}"})
                    if len([x for x in results if x['type'] == '발주품번']) >= max_per:
                        break

    # ── 3) 외주재고 품번/품명 매칭 ──
    inv_codes = sorted(DF[COL_품목].unique(), reverse=True)
    for code in inv_codes:
        if q in code.lower():
            key = f'품번:{code}'
            if key not in seen:
                seen.add(key)
                nm_rows = DF[DF[COL_품목] == code]
                nm = str(nm_rows[COL_품명].iloc[0])[:30] if not nm_rows.empty else ''
                results.append({'type': '품번', 'value': code, 'label': f"{code} | {nm}"})
                if len([x for x in results if x['type'] == '품번']) >= max_per:
                    break

    # 품명 매칭
    for _, r in DF.iterrows():
        name = str(r.get(COL_품명, '')).strip()
        if q in name.lower():
            key = f'품명:{name}'
            if key not in seen:
                seen.add(key)
                results.append({'type': '품명', 'value': name, 'label': f"{str(r.get(COL_품목,''))} | {name[:35]}"})
                if len([x for x in results if x['type'] == '품명']) >= max_per:
                    break

    # ── 4) 자사재고 품명 매칭 ──
    if JASA_DF is not None:
        col_j품번 = JASA_DF.columns[1]
        col_j품명 = JASA_DF.columns[5]
        for _, r in JASA_DF.iterrows():
            code = str(r.get(col_j품번, '')).strip()
            name = str(r.get(col_j품명, '')).strip()
            if q in code.lower() or q in name.lower():
                key = f'자사:{code}'
                if key not in seen:
                    seen.add(key)
                    results.append({'type': '자사품목', 'value': name, 'label': f"{code} | {name[:30]}"})
                    if len([x for x in results if x['type'] == '자사품목']) >= max_per:
                        break

    # ── 5) 거래처 매칭 ──
    for val in _unique_vals(COL_납품처) + _unique_vals(COL_원산지):
        if q in val.lower():
            key = f'거래처:{val}'
            if key not in seen:
                seen.add(key)
                results.append({'type': '거래처', 'value': val, 'label': val})

    # 부재료(품명/품번) 먼저, 제품(BOM) 아래로 정렬
    부재료_types = {'품명', '품번', '발주품번', '거래처', '자사품명'}
    제품_types = {'제품(BOM)'}
    부재료 = [r for r in results if r['type'] in 부재료_types]
    제품 = [r for r in results if r['type'] in 제품_types]
    기타 = [r for r in results if r['type'] not in 부재료_types and r['type'] not in 제품_types]
    sorted_results = 부재료 + 기타 + 제품
    return jsonify(sorted_results[:25])


@app.route('/api/dashboard', methods=['GET'])
def dashboard_data():
    """대시보드 차트용 데이터"""
    # 외주업체별 재고금액 (상위 9개)
    vendor_chart = []
    for vendor in _unique_vals(COL_원산지):
        rows = DF[DF[COL_원산지] == vendor]
        cost = sum(
            _num(r[COL_재고량]) * (get_price_info(r[COL_품목], r[COL_품명]) or {}).get('단가', 0) or 0
            for _, r in rows.iterrows()
        )
        vendor_chart.append({'name': vendor, 'cost': int(cost), 'count': len(rows)})
    vendor_chart.sort(key=lambda x: -x['cost'])

    # 납품처별 재고금액
    dest_chart = []
    for dest in _unique_vals(COL_납품처):
        rows = DF[DF[COL_납품처] == dest]
        cost = sum(
            _num(r[COL_재고량]) * (get_price_info(r[COL_품목], r[COL_품명]) or {}).get('단가', 0) or 0
            for _, r in rows.iterrows()
        )
        dest_chart.append({'name': dest, 'cost': int(cost), 'count': len(rows)})
    dest_chart.sort(key=lambda x: -x['cost'])

    # 규격별 분포
    spec_chart = []
    for spec in sorted(DF[COL_규격].unique()):
        if spec:
            cnt = len(DF[DF[COL_규격] == spec])
            spec_chart.append({'name': display_규격(spec), 'count': cnt})

    # 발주/출하/생산 월별 추이
    monthly_orders = {}
    monthly_sales = {}
    monthly_prod = {}
    if ORDER_DF is not None and '발주일자' in ORDER_DF.columns:
        for _, r in ORDER_DF.iterrows():
            ym = str(r.get('발주일자', ''))[:6]
            if ym and len(ym) == 6:
                monthly_orders[ym] = monthly_orders.get(ym, 0) + 1
    if SHIP_DF is not None and '출하일자' in SHIP_DF.columns:
        for _, r in SHIP_DF.iterrows():
            ym = str(r.get('출하일자', ''))[:6]
            if ym and len(ym) == 6:
                monthly_sales[ym] = monthly_sales.get(ym, 0) + 1
    if PROD_DF is not None and '실적일자' in PROD_DF.columns:
        for _, r in PROD_DF.iterrows():
            ym = str(r.get('실적일자', ''))[:6]
            if ym and len(ym) == 6:
                monthly_prod[ym] = monthly_prod.get(ym, 0) + 1

    all_months = sorted(set(list(monthly_orders.keys()) + list(monthly_sales.keys()) + list(monthly_prod.keys())))
    trend_data = {
        'labels': [f"{m[:4]}.{m[4:]}" for m in all_months],
        'orders': [monthly_orders.get(m, 0) for m in all_months],
        'sales': [monthly_sales.get(m, 0) for m in all_months],
        'production': [monthly_prod.get(m, 0) for m in all_months],
    }

    # 총 재고금액
    total_inv_cost = ADMIN_DATA.get('grand_total', 0) if ADMIN_DATA else 0
    jasa_cost = 0
    if JASA_DF is not None:
        for _, r in JASA_DF.iterrows():
            pi = get_price_info(str(r[JASA_DF.columns[1]]).strip(), str(r[JASA_DF.columns[5]]).strip())
            단가 = pi.get('단가', 0) if pi else 0
            jasa_cost += _num(r[JASA_DF.columns[7]]) * 단가

    return jsonify({
        'vendorChart': vendor_chart,
        'destChart': dest_chart,
        'specChart': spec_chart,
        'trendData': trend_data,
        'summary': {
            'invItems': len(DF),
            'invCost': total_inv_cost,
            'jasaItems': len(JASA_DF) if JASA_DF is not None else 0,
            'jasaCost': int(jasa_cost),
            'orderCount': len(ORDER_DF) if ORDER_DF is not None else 0,
            'extOrderCount': len(EXT_ORDER_DF) if EXT_ORDER_DF is not None else 0,
            'shipCount': len(SHIP_DF) if SHIP_DF is not None else 0,
            'prodCount': len(PROD_DF) if PROD_DF is not None else 0,
            'bomProducts': BOM_DF['모품번'].nunique() if BOM_DF is not None and not BOM_DF.empty else 0,
            'vendorCount': len(_unique_vals(COL_원산지)),
            'destCount': len(_unique_vals(COL_납품처)),
        }
    })


# ────────────────────────────────────────────
# HTML 템플릿
# ────────────────────────────────────────────
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>매홍 L&F - 재고 조회 챗봇</title>
  <!-- Firebase SDK -->
  <script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js"></script>
  <script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-auth-compat.js"></script>
  <script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore-compat.js"></script>
  <script>
    firebase.initializeApp({
      apiKey: "AIzaSyBZ1FfTibE-KBkTbZJnTNEqz-pxsgew03k",
      authDomain: "maehong-scm.firebaseapp.com",
      projectId: "maehong-scm",
      storageBucket: "maehong-scm.firebasestorage.app",
      messagingSenderId: "776997651051",
      appId: "1:776997651051:web:8734392ca2e791b5fb272e"
    });
    const fbAuth = firebase.auth();
    const fbDb = firebase.firestore();
  </script>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700;900&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Noto Sans KR', -apple-system, BlinkMacSystemFont, sans-serif;
      background: #f5f6fa;
      height: 100vh;
      display: flex;
      flex-direction: column;
      color: #1a1a2e;
    }

    /* ── Header ── */
    header {
      background: white;
      color: #1a1a2e;
      padding: 12px 24px;
      display: flex;
      align-items: center;
      gap: 14px;
      border-bottom: 1px solid #e5e7eb;
      box-shadow: 0 1px 4px rgba(0,0,0,0.04);
      flex-shrink: 0;
    }
    header .logo {
      width: 38px; height: 38px;
      background: linear-gradient(135deg, #4f46e5, #7c3aed);
      border-radius: 10px;
      display: flex; align-items: center; justify-content: center;
      font-size: 16px; font-weight: 900; color: #fff;
    }
    header .title-group { flex: 1; }
    header h1 { font-size: 16px; font-weight: 700; letter-spacing: -0.3px; color: #1e293b; }
    header p { font-size: 11px; color: #94a3b8; margin-top: 2px; }
    .data-badge {
      background: #f0f0ff;
      border: 1px solid #e0e7ff;
      padding: 4px 12px;
      border-radius: 20px;
      font-size: 11px;
      color: #4f46e5;
    }
    .admin-link {
      color: #6366f1;
      text-decoration: none;
      font-size: 12px;
      padding: 4px 12px;
      border: 1px solid #e0e7ff;
      border-radius: 16px;
      transition: all 0.2s;
    }
    .admin-link:hover { background: #f0f0ff; }

    /* ── Main layout — 세로 배치 (채팅 위 / 대시보드 아래) ── */
    .main-container {
      display: flex;
      flex-direction: column;
      flex: 1;
      overflow: hidden;
      width: 100%;
      gap: 0;
    }

    /* ── Dashboard (아래쪽) ── */
    .sidebar {
      width: 100%;
      flex-shrink: 0;
      padding: 14px 24px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      overflow-y: auto;
      background: #f9fafb;
      border-top: 1px solid #e5e7eb;
      max-height: 380px;
      order: 2;
    }
    .sidebar.collapsed { max-height: 0; padding: 0; overflow: hidden; opacity: 0; }

    /* KPI Cards — 가로 배치 */
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(6, 1fr);
      gap: 8px;
      width: 100%;
    }
    .kpi-card {
      background: white;
      border-radius: 12px;
      padding: 12px;
      display: flex;
      gap: 8px;
      align-items: center;
      border: 1px solid #e5e7eb;
      box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    .kpi-icon { font-size: 20px; }
    .kpi-val { font-size: 15px; font-weight: 700; color: #1e293b; }
    .kpi-label { font-size: 10px; color: #64748b; margin-top: 2px; }
    .kpi-inv .kpi-val { color: #4f46e5; }
    .kpi-jasa .kpi-val { color: #0891b2; }
    .kpi-order .kpi-val { color: #d97706; }
    .kpi-ship .kpi-val { color: #059669; }
    .kpi-prod .kpi-val { color: #db2777; }
    .kpi-bom .kpi-val { color: #7c3aed; }

    /* Chart Cards */
    .chart-card {
      background: white;
      border-radius: 12px;
      padding: 14px;
      border: 1px solid #e5e7eb;
      box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    .chart-title {
      font-size: 11px;
      font-weight: 700;
      color: #64748b;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 10px;
    }
    .chart-row {
      display: flex;
      gap: 10px;
      flex: 1;
    }
    .chart-card.half { flex: 1; }
    .chart-card { flex: 1; min-width: 0; }

    /* Quick Actions */
    .sidebar-card {
      background: white;
      border-radius: 12px;
      padding: 14px;
      border: 1px solid #e5e7eb;
      box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    .quick-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
    }
    .qbtn {
      background: #f8f9ff;
      border: 1px solid #e0e7ff;
      border-radius: 8px;
      padding: 8px 10px;
      font-size: 12px;
      color: #374151;
      cursor: pointer;
      transition: all 0.15s;
      text-align: left;
    }
    .qbtn:hover { background: #e0e7ff; border-color: #6366f1; color: #4338ca; }

    /* ── Chat area ── */
    .chat-wrapper {
      flex: 1;
      display: flex;
      flex-direction: column;
      min-width: 0;
      min-height: 0;
      overflow: hidden;
      background: #fafbfc;
    }
    .chat-header-bar {
      padding: 10px 16px;
      font-size: 13px;
      font-weight: 600;
      color: #64748b;
      border-bottom: 1px solid #e5e7eb;
      background: white;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .toggle-sidebar {
      background: #f0f0ff;
      border: 1px solid #e0e7ff;
      border-radius: 8px;
      padding: 4px 8px;
      font-size: 14px;
      cursor: pointer;
      color: #4f46e5;
    }
    .toggle-sidebar:hover { background: #e0e7ff; }

    #chat-box {
      flex: 1;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 16px;
      padding: 16px 20px 8px;
      scroll-behavior: smooth;
    }

    /* Scrollbar */
    #chat-box::-webkit-scrollbar { width: 5px; }
    #chat-box::-webkit-scrollbar-track { background: transparent; }
    #chat-box::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 3px; }

    /* ── Message bubbles ── */
    .message-row {
      display: flex;
      gap: 10px;
      animation: fadeInUp 0.25s ease-out;
    }
    @keyframes fadeInUp {
      from { opacity: 0; transform: translateY(8px); }
      to   { opacity: 1; transform: translateY(0); }
    }

    .message-row.user { flex-direction: row-reverse; }

    .avatar {
      width: 34px; height: 34px;
      border-radius: 50%;
      flex-shrink: 0;
      display: flex; align-items: center; justify-content: center;
      font-size: 14px; font-weight: 700;
    }
    .avatar.bot {
      background: linear-gradient(135deg, #6366f1, #8b5cf6);
      color: white;
    }
    .avatar.user-av {
      background: linear-gradient(135deg, #f59e0b, #f472b6);
      color: white;
    }

    .bubble-group { display: flex; flex-direction: column; gap: 4px; max-width: 75%; }
    .message-row.user .bubble-group { align-items: flex-end; }

    .sender-name {
      font-size: 11px;
      color: #999;
      padding: 0 4px;
    }

    .bubble {
      padding: 12px 16px;
      border-radius: 18px;
      font-size: 14px;
      line-height: 1.6;
      word-break: break-word;
      max-width: 100%;
    }

    .bubble.bot-bubble {
      background: white;
      border-top-left-radius: 4px;
      border: 1px solid #e5e7eb;
      color: #1e293b;
      box-shadow: 0 1px 4px rgba(0,0,0,0.04);
    }

    .bubble.user-bubble {
      background: #e0e7ff;
      border-top-right-radius: 4px;
      color: #1e293b;
      border: 1px solid #c7d2fe;
    }

    /* Markdown styles inside bot bubble */
    .bubble.bot-bubble h1, .bubble.bot-bubble h2, .bubble.bot-bubble h3 {
      margin: 10px 0 6px;
      color: #1e293b;
    }
    .bubble.bot-bubble h1 { font-size: 17px; }
    .bubble.bot-bubble h2 { font-size: 15px; }
    .bubble.bot-bubble h3 { font-size: 14px; }
    .bubble.bot-bubble p { margin-bottom: 8px; }
    .bubble.bot-bubble p:last-child { margin-bottom: 0; }
    .bubble.bot-bubble ul, .bubble.bot-bubble ol {
      padding-left: 18px;
      margin-bottom: 8px;
    }
    .bubble.bot-bubble li { margin-bottom: 4px; }
    .bubble.bot-bubble strong { color: #4338ca; }
    .bubble.bot-bubble code {
      background: #f0f0ff;
      padding: 1px 6px;
      border-radius: 4px;
      font-family: monospace;
      font-size: 13px;
      color: #e11d48;
    }
    .bubble.bot-bubble pre {
      background: #f8fafc;
      padding: 10px 14px;
      border-radius: 10px;
      overflow-x: auto;
      margin: 8px 0;
      border: 1px solid #e5e7eb;
    }
    .bubble.bot-bubble pre code {
      background: none;
      padding: 0;
      color: #334155;
    }
    .bubble.bot-bubble table {
      border-collapse: collapse;
      width: 100%;
      margin: 10px 0;
      font-size: 12px;
    }
    .bubble.bot-bubble th {
      background: #4f46e5;
      color: white;
      padding: 8px 10px;
      text-align: left;
      font-weight: 600;
    }
    .bubble.bot-bubble td {
      padding: 6px 10px;
      border-bottom: 1px solid #f1f5f9;
      color: #334155;
    }
    .bubble.bot-bubble tr:nth-child(even) td { background: #f8fafc; }
    .bubble.bot-bubble blockquote {
      border-left: 3px solid #7c3aed;
      padding-left: 12px;
      margin: 8px 0;
      color: #64748b;
    }

    /* Typing indicator */
    .typing-bubble {
      background: white;
      padding: 12px 20px;
      border-radius: 18px;
      border-top-left-radius: 4px;
      border: 1px solid #e5e7eb;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .typing-dot {
      width: 7px; height: 7px;
      background: #d1d5db;
      border-radius: 50%;
      animation: bounce 1.2s infinite;
    }
    .typing-dot:nth-child(2) { animation-delay: 0.2s; }
    .typing-dot:nth-child(3) { animation-delay: 0.4s; }
    @keyframes bounce {
      0%, 60%, 100% { transform: translateY(0); }
      30% { transform: translateY(-6px); background: #6366f1; }
    }

    /* ── Input area ── */
    .input-area {
      background: white;
      border-radius: 16px;
      padding: 10px 12px;
      display: flex;
      gap: 8px;
      align-items: flex-end;
      border: 2px solid #1e293b;
      margin: 8px 16px 14px;
    }

    #user-input {
      flex: 1;
      border: none;
      outline: none;
      font-size: 14px;
      resize: none;
      max-height: 140px;
      line-height: 1.5;
      background: transparent;
      color: #1e293b;
      padding: 4px 8px;
      font-family: inherit;
    }
    #user-input::placeholder { color: #9ca3af; }

    #send-btn {
      width: 38px; height: 38px;
      background: linear-gradient(135deg, #6366f1, #8b5cf6);
      border: none;
      border-radius: 50%;
      color: white;
      font-size: 16px;
      cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      flex-shrink: 0;
      transition: transform 0.15s, opacity 0.15s;
    }
    #send-btn:hover { transform: scale(1.08); }
    #send-btn:disabled { opacity: 0.3; cursor: not-allowed; }

    /* 유사어 추천 패널 — 입력창 위에 절대 위치 */
    .input-area { position: relative; }
    #suggest-panel {
      position: absolute;
      bottom: 100%;
      left: 0;
      right: 0;
      background: white;
      border: 1px solid #e5e7eb;
      border-radius: 10px;
      padding: 10px 14px;
      margin-bottom: 4px;
      box-shadow: 0 -4px 16px rgba(0,0,0,0.1);
      max-height: 220px;
      overflow-y: auto;
      z-index: 50;
    }
    #suggest-panel .sg-title {
      font-size: 11px;
      font-weight: 700;
      color: #94a3b8;
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    #suggest-panel .sg-group { margin-bottom: 6px; }
    #suggest-panel .sg-type {
      font-size: 10px;
      font-weight: 600;
      color: #6366f1;
      margin-bottom: 3px;
    }
    #suggest-panel label {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      background: #f8f9fa;
      border: 1px solid #e5e7eb;
      border-radius: 6px;
      padding: 3px 8px;
      margin: 2px;
      font-size: 12px;
      cursor: pointer;
      transition: all 0.15s;
      color: #374151;
    }
    #suggest-panel label:hover { background: #e0e7ff; border-color: #6366f1; }
    #suggest-panel label.checked { background: #eef2ff; border-color: #6366f1; color: #4338ca; font-weight: 600; }
    #suggest-panel input[type="checkbox"] { width: 14px; height: 14px; accent-color: #6366f1; }

    /* Welcome message */
    .welcome-card {
      background: linear-gradient(135deg, #4338ca 0%, #6366f1 50%, #818cf8 100%);
      color: white;
      border-radius: 14px;
      padding: 20px;
      text-align: center;
    }
    .welcome-card h2 { font-size: 18px; margin-bottom: 8px; font-weight: 700; }
    .welcome-card p { font-size: 12px; opacity: 0.8; line-height: 1.7; }

    .sender-name { color: #94a3b8; }
    .sidebar::-webkit-scrollbar { width: 5px; }
    .sidebar::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 3px; }

    @media (max-width: 900px) {
      .sidebar { display: none; }
      .toggle-sidebar { display: inline-flex; }
    }
    @media (min-width: 901px) {
      .toggle-sidebar { display: none; }
    }
  </style>
</head>
<body>

<!-- Header -->
<header>
  <div class="logo">M</div>
  <div class="title-group">
    <h1>매홍 L&F 통합 재고 관리</h1>
    <p>재고 / 발주 / 생산 / BOM / 매출 통합 조회</p>
  </div>
  <a href="/admin" class="admin-link">관리자</a>
  <a href="/upload" class="admin-link" id="data-status" style="text-decoration:none">로딩중...</a>
  <a href="/vendor/데이웰즈" class="admin-link">데이웰즈</a>
  <a href="/vendor/더고은" class="admin-link">더고은</a>
  <a href="/vendor/정성" class="admin-link">정성</a>
  <a href="/vendor/청통본가" class="admin-link">청통본가</a>
  <a href="/jasa" class="admin-link">자사</a>
  <!-- 사용자 정보 + 로그아웃 -->
  <div id="auth-area" style="margin-left:auto;display:flex;align-items:center;gap:8px;">
    <span id="user-name" style="font-size:12px;color:#64748b;display:none"></span>
    <button id="logout-btn" onclick="googleLogout()" style="display:none;font-size:11px;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:8px;padding:3px 10px;cursor:pointer;color:#64748b">로그아웃</button>
  </div>
</header>

<!-- 로그인 오버레이 (비로그인 시 전체 화면 가림) -->
<div id="login-overlay" style="
  position:fixed; top:0; left:0; width:100%; height:100%; z-index:9999;
  background:rgba(248,250,252,0.97); display:flex; align-items:center; justify-content:center;
  flex-direction:column; gap:20px;
">
  <div style="
    background:white; border-radius:20px; padding:48px 40px; text-align:center;
    box-shadow:0 8px 40px rgba(0,0,0,0.1); max-width:420px; width:90%;
  ">
    <div style="font-size:56px; margin-bottom:16px;">M</div>
    <h1 style="font-size:22px; color:#1e293b; margin-bottom:6px; font-weight:700;">매홍 L&F 통합 재고 관리</h1>
    <p style="color:#64748b; font-size:13px; line-height:1.7; margin-bottom:28px;">
      재고 / 발주 / 생산 / BOM / 매출 통합 조회 챗봇<br>
      Google 계정으로 로그인하여 시작하세요
    </p>
    <button id="overlay-login-btn" style="
      display:inline-flex; align-items:center; gap:10px;
      padding:14px 32px; border-radius:12px; font-size:15px; font-weight:600;
      cursor:pointer; border:1px solid #e2e8f0; background:white; color:#1e293b;
      box-shadow:0 2px 8px rgba(0,0,0,0.08); transition:all 0.2s;
    " onmouseover="this.style.boxShadow='0 4px 16px rgba(0,0,0,0.15)'" onmouseout="this.style.boxShadow='0 2px 8px rgba(0,0,0,0.08)'">
      <img src="https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg" width="20" height="20">
      Google 계정으로 로그인
    </button>
    <script>
      // 브라우저 탭 닫으면 로그인 해제 (매 접속마다 로그인 필요)
      firebase.auth().setPersistence(firebase.auth.Auth.Persistence.SESSION);

      document.getElementById('overlay-login-btn').addEventListener('click', function() {
        var provider = new firebase.auth.GoogleAuthProvider();
        firebase.auth().signInWithPopup(provider).then(function() {
          document.getElementById('login-overlay').style.display = 'none';
        }).catch(function(e) { alert('로그인 실패: ' + e.message); });
      });
      // 페이지 로드 시 이미 로그인된 상태면 즉시 오버레이 숨기기
      firebase.auth().onAuthStateChanged(function(user) {
        if (user) {
          document.getElementById('login-overlay').style.display = 'none';
        } else {
          document.getElementById('login-overlay').style.display = 'flex';
        }
      });
    </script>
    <p style="margin-top:20px; font-size:11px; color:#94a3b8;">
      로그인하면 대화 기록이 저장되고, 다른 사람에게 공유할 수 있습니다.
    </p>
  </div>
</div>

<!-- Dashboard + Chat -->
<div class="main-container">

  <!-- Left: Dashboard -->
  <!-- Chat -->
  <div class="chat-wrapper">
    <div class="chat-header-bar" style="display:flex;align-items:center;gap:8px;">
      <span>AI 채팅</span>
      <button id="new-chat-btn" onclick="newChat()" style="margin-left:8px;font-size:11px;background:#e0e7ff;border:1px solid #c7d2fe;border-radius:6px;padding:2px 8px;cursor:pointer;color:#4338ca">+ 새 대화</button>
      <button id="share-btn" onclick="shareChat()" style="font-size:11px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;padding:2px 8px;cursor:pointer;color:#166534;display:none">공유</button>
      <button id="history-btn" onclick="toggleHistory()" style="margin-left:auto;font-size:11px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:2px 8px;cursor:pointer;color:#64748b">대화 기록</button>
    </div>
    <!-- 히스토리 패널 -->
    <div id="history-panel" style="display:none;max-height:200px;overflow-y:auto;background:#f8fafc;border-bottom:1px solid #e5e7eb;padding:8px 12px;">
      <div style="font-size:11px;color:#94a3b8;margin-bottom:6px;font-weight:600">이전 대화</div>
      <div id="history-list" style="font-size:12px"></div>
    </div>
    <div id="chat-box">
      <div class="message-row">
        <div class="avatar bot">AI</div>
        <div class="bubble-group">
          <span class="sender-name">매홍 AI</span>
          <div class="bubble bot-bubble">
            <div class="welcome-card">
              <h2>매홍 L&F 통합 조회 챗봇</h2>
              <p>
                재고, 발주, 생산실적, BOM, 매출/출하 데이터를<br>
                자연어로 질문하세요. 날짜 필터도 지원합니다.<br>
                <strong>예시:</strong> "25년12월 생산실적", "정성 제품재고", "G0010 BOM 검토"
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="input-area">
      <!-- 유사어 추천 패널 -->
      <div id="suggest-panel" style="display:none"></div>
      <textarea
        id="user-input"
        rows="1"
        placeholder="질문을 입력하세요... (재고, 발주, 생산, BOM, 매출 등)"
        onkeydown="handleKeydown(event)"
        oninput="autoResize(this); debounceSuggest(this.value)"
      ></textarea>
      <button id="send-btn" onclick="sendMessage()">➤</button>
    </div>
  </div>
</div>

<!-- Monday 상세 모달 -->
<div id="monday-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:200;justify-content:center;align-items:center" onclick="if(event.target===this)this.style.display='none'">
  <div style="background:white;border-radius:16px;padding:28px;max-width:700px;width:90%;max-height:80vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,0.3)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h2 id="mm-title" style="font-size:18px;font-weight:700;color:#1e293b"></h2>
      <button onclick="document.getElementById('monday-modal').style.display='none'" style="background:none;border:none;font-size:24px;cursor:pointer;color:#999">&times;</button>
    </div>
    <div id="mm-meta" style="font-size:12px;color:#888;margin-bottom:16px"></div>
    <div id="mm-columns"></div>
    <div id="mm-updates"></div>
    <div id="mm-loading" style="text-align:center;padding:20px;color:#999">로딩 중...</div>
  </div>
</div>

<script>
  // Configure marked
  marked.setOptions({
    highlight: function(code, lang) {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(code, { language: lang }).value;
      }
      return hljs.highlightAuto(code).value;
    },
    breaks: true,
    gfm: true
  });

  let chatHistory = [];
  let isLoading = false;

  // Load data stats
  async function loadDataInfo() {
    try {
      const res = await fetch('/api/data-info');
      const info = await res.json();
      document.getElementById('stat-rows').textContent = info.total_rows.toLocaleString() + '행';
      document.getElementById('stat-cols').textContent = info.total_columns + '열';
      document.getElementById('stat-file').textContent = info.csv_file;
      document.getElementById('data-status').textContent = `✅ ${info.total_rows}개 항목 로드됨`;
    } catch(e) {
      document.getElementById('data-status').textContent = '📁 파일 업로드';
    }
  }

  function autoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 140) + 'px';
  }

  function handleKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  }

  function sendQuick(btn) {
    const text = btn.textContent.trim();
    document.getElementById('user-input').value = text;
    sendMessage();
  }

  function addMessage(role, content) {
    const chatBox = document.getElementById('chat-box');
    const row = document.createElement('div');
    row.className = `message-row ${role === 'user' ? 'user' : ''}`;

    const avatarEl = document.createElement('div');
    avatarEl.className = `avatar ${role === 'user' ? 'user-av' : 'bot'}`;
    avatarEl.textContent = role === 'user' ? '👤' : '🤖';

    const group = document.createElement('div');
    group.className = 'bubble-group';

    const name = document.createElement('span');
    name.className = 'sender-name';
    name.textContent = role === 'user' ? '사용자' : '재고 챗봇';

    const bubble = document.createElement('div');
    bubble.className = `bubble ${role === 'user' ? 'user-bubble' : 'bot-bubble'}`;

    if (role === 'user') {
      bubble.textContent = content;
    } else {
      bubble.innerHTML = marked.parse(content);
      // Monday 링크 클릭 핸들러 (monday:아이템ID 형식)
      bubble.querySelectorAll('a[href^="monday:"]').forEach(a => {
        const itemId = a.getAttribute('href').replace('monday:', '');
        a.href = '#';
        a.style.color = '#4f46e5';
        a.style.fontWeight = '600';
        a.style.textDecoration = 'underline';
        a.style.cursor = 'pointer';
        a.addEventListener('click', (e) => { e.preventDefault(); showMondayDetail(itemId); });
      });
    }

    group.appendChild(name);
    group.appendChild(bubble);
    row.appendChild(avatarEl);
    row.appendChild(group);
    chatBox.appendChild(row);
    requestAnimationFrame(() => { chatBox.scrollTop = chatBox.scrollHeight; });
    return bubble;
  }

  function showTyping() {
    const chatBox = document.getElementById('chat-box');
    const row = document.createElement('div');
    row.className = 'message-row';
    row.id = 'typing-indicator';

    const avatar = document.createElement('div');
    avatar.className = 'avatar bot';
    avatar.textContent = '🤖';

    const group = document.createElement('div');
    group.className = 'bubble-group';

    const typing = document.createElement('div');
    typing.className = 'typing-bubble';
    typing.innerHTML = '<div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div>';

    group.appendChild(typing);
    row.appendChild(avatar);
    row.appendChild(group);
    chatBox.appendChild(row);
    chatBox.scrollTop = chatBox.scrollHeight;
  }

  function removeTyping() {
    const el = document.getElementById('typing-indicator');
    if (el) el.remove();
  }

  async function sendMessage() {
    if (isLoading) return;

    // 로그인 체크
    if (!currentUser) {
      addMessage('bot', '⚠️ **로그인이 필요합니다.**\n\n우측 상단의 **Google 로그인** 버튼을 클릭해 주세요.\n로그인하면 대화 기록이 저장되고, 다른 사람에게 공유할 수 있습니다.');
      return;
    }

    const input = document.getElementById('user-input');
    const message = input.value.trim();
    if (!message) return;

    input.value = '';
    input.style.height = 'auto';
    isLoading = true;
    document.getElementById('send-btn').disabled = true;

    // Add user message
    addMessage('user', message);
    chatHistory.push({ role: 'user', content: message });

    // Show typing
    showTyping();

    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 90000); // 90초 타임아웃

      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: message,
          history: chatHistory.slice(-10)
        }),
        signal: controller.signal
      });

      clearTimeout(timeoutId);
      const data = await res.json();
      removeTyping();

      if (data.error) {
        addMessage('bot', `❌ 오류: ${data.error}`);
      } else {
        addMessage('bot', data.message);
        chatHistory.push({ role: 'assistant', content: data.message });
      }
    } catch(e) {
      removeTyping();
      if (e.name === 'AbortError') {
        addMessage('bot', '⏱️ 응답 시간이 초과되었습니다. 다시 시도해주세요.');
      } else {
        addMessage('bot', `❌ 서버 연결 오류: ${e.message}\n\n서버가 실행 중인지 확인해주세요.`);
      }
    }

    isLoading = false;
    document.getElementById('send-btn').disabled = false;
    input.focus();
    // 응답 후 최하단으로 스크롤
    const cb = document.getElementById('chat-box');
    requestAnimationFrame(() => { cb.scrollTop = cb.scrollHeight; });
  }

  function fmt(n) { return n ? Number(n).toLocaleString('ko-KR') : '0'; }
  function fmtCost(n) {
    if (!n) return '0';
    if (n >= 100000000) return (n/100000000).toFixed(1) + '억';
    if (n >= 10000) return (n/10000).toFixed(0) + '만';
    return fmt(n);
  }

  // (대시보드 차트는 /admin 페이지로 이동됨)

  // ── 유사어 추천 ──
  let suggestTimer = null;
  const suggestPanel = document.getElementById('suggest-panel');

  function debounceSuggest(val) {
    clearTimeout(suggestTimer);
    const q = val.trim();
    if (q.length < 2) { suggestPanel.style.display = 'none'; return; }
    suggestTimer = setTimeout(() => fetchSuggest(q), 300);
  }

  async function fetchSuggest(q) {
    try {
      const res = await fetch('/api/suggest?q=' + encodeURIComponent(q));
      const items = await res.json();
      if (!items || items.length === 0) {
        suggestPanel.style.display = 'none';
        return;
      }
      renderSuggest(items);
    } catch(e) {
      suggestPanel.style.display = 'none';
    }
  }

  function renderSuggest(items) {
    const groups = {};
    const typeLabels = {'품번':'품번','품명':'품명(외주)','자사품목':'품명(자사)','거래처':'거래처/업체','BOM':'BOM 제품','발주품번':'발주품번'};
    items.forEach(it => {
      if (!groups[it.type]) groups[it.type] = [];
      groups[it.type].push(it);
    });

    suggestPanel.innerHTML = '';
    const title = document.createElement('div');
    title.className = 'sg-title';
    title.textContent = '클릭하여 입력창에 반영';
    suggestPanel.appendChild(title);

    for (const [type, list] of Object.entries(groups)) {
      const grp = document.createElement('div');
      grp.className = 'sg-group';
      const typeEl = document.createElement('div');
      typeEl.className = 'sg-type';
      typeEl.textContent = typeLabels[type] || type;
      grp.appendChild(typeEl);

      list.forEach(it => {
        const lbl = document.createElement('label');
        lbl.textContent = it.label;
        lbl.style.cursor = 'pointer';
        lbl.addEventListener('click', () => {
          document.getElementById('user-input').value = it.value + ' ';
          document.getElementById('user-input').focus();
          suggestPanel.style.display = 'none';
        });
        grp.appendChild(lbl);
      });

      suggestPanel.appendChild(grp);
    }
    suggestPanel.style.display = 'block';
  }

  // Monday 상세 조회
  async function showMondayDetail(itemId) {
    const modal = document.getElementById('monday-modal');
    const loading = document.getElementById('mm-loading');
    const title = document.getElementById('mm-title');
    const meta = document.getElementById('mm-meta');
    const cols = document.getElementById('mm-columns');
    const upds = document.getElementById('mm-updates');

    modal.style.display = 'flex';
    loading.style.display = 'block';
    title.textContent = '';
    meta.textContent = '';
    cols.innerHTML = '';
    upds.innerHTML = '';

    try {
      const res = await fetch('/api/monday-item/' + itemId);
      const d = await res.json();
      loading.style.display = 'none';

      if (d.error) {
        title.textContent = '오류';
        cols.innerHTML = '<p style="color:#dc2626">' + d.error + '</p>';
        return;
      }

      title.textContent = d.name;
      meta.innerHTML = '보드: <strong>' + d.board + '</strong> | 그룹: ' + d.group +
        ' | 생성: ' + d.created + ' | 수정: ' + d.updated;

      // 컬럼값 테이블
      if (d.columns && d.columns.length > 0) {
        let html = '<h3 style="font-size:14px;font-weight:700;margin:16px 0 8px;color:#374151">상세 정보</h3>';
        html += '<table style="width:100%;border-collapse:collapse;font-size:13px">';
        d.columns.forEach(c => {
          html += '<tr><td style="padding:6px 10px;border-bottom:1px solid #f3f4f6;font-weight:600;color:#555;width:30%;vertical-align:top">'
            + c.title + '</td><td style="padding:6px 10px;border-bottom:1px solid #f3f4f6">' + c.value + '</td></tr>';
        });
        html += '</table>';
        cols.innerHTML = html;
      }

      // 하위 아이템
      if (d.subitems && d.subitems.length > 0) {
        let html = '<h3 style="font-size:14px;font-weight:700;margin:16px 0 8px;color:#374151">하위 아이템 (' + d.subitems.length + '건)</h3>';
        html += '<table style="width:100%;border-collapse:collapse;font-size:12px">';
        // 헤더: 하위 아이템들의 컬럼 제목 수집
        const allTitles = new Set();
        d.subitems.forEach(si => si.columns.forEach(c => allTitles.add(c.title)));
        const titles = Array.from(allTitles);
        html += '<thead><tr><th style="padding:6px 8px;border-bottom:2px solid #e5e7eb;text-align:left;color:#555;background:#f8f9fa">이름</th>';
        titles.forEach(t => { html += '<th style="padding:6px 8px;border-bottom:2px solid #e5e7eb;text-align:left;color:#555;background:#f8f9fa">' + t + '</th>'; });
        html += '</tr></thead><tbody>';
        d.subitems.forEach(si => {
          const colMap = {};
          si.columns.forEach(c => { colMap[c.title] = c.value; });
          html += '<tr><td style="padding:5px 8px;border-bottom:1px solid #f3f4f6;font-weight:600">' + si.name + '</td>';
          titles.forEach(t => { html += '<td style="padding:5px 8px;border-bottom:1px solid #f3f4f6">' + (colMap[t] || '-') + '</td>'; });
          html += '</tr>';
        });
        html += '</tbody></table>';
        upds.insertAdjacentHTML('beforebegin', html);
      }

      // 업데이트(코멘트)
      if (d.updates && d.updates.length > 0) {
        let html = '<h3 style="font-size:14px;font-weight:700;margin:16px 0 8px;color:#374151">최근 업데이트</h3>';
        d.updates.forEach(u => {
          html += '<div style="background:#f8f9fa;border-radius:8px;padding:10px;margin-bottom:8px;font-size:12px">';
          html += '<div style="color:#888;margin-bottom:4px"><strong>' + u.author + '</strong> · ' + u.date + '</div>';
          html += '<div>' + u.text + '</div></div>';
        });
        upds.innerHTML = html;
      }
    } catch(e) {
      loading.style.display = 'none';
      cols.innerHTML = '<p style="color:#dc2626">조회 실패: ' + e.message + '</p>';
    }
  }

  // ── Firebase Auth + Firestore ──
  let currentUser = null;
  let currentChatId = null;

  function googleLogin() {
    const provider = new firebase.auth.GoogleAuthProvider();
    fbAuth.signInWithPopup(provider).catch(e => alert('로그인 실패: ' + e.message));
  }

  function googleLogout() {
    fbAuth.signOut();
  }

  fbAuth.onAuthStateChanged(user => {
    currentUser = user;
    const input = document.getElementById('user-input');
    const sendBtn = document.getElementById('send-btn');
    const overlay = document.getElementById('login-overlay');
    if (user) {
      // 로그인 성공 → 오버레이 숨기기, 챗봇 활성화
      overlay.style.display = 'none';
      document.getElementById('user-name').style.display = 'inline';
      document.getElementById('user-name').textContent = user.displayName || user.email;
      document.getElementById('logout-btn').style.display = 'inline';
      document.getElementById('share-btn').style.display = 'inline';
      input.disabled = false;
      input.placeholder = '질문을 입력하세요... (재고, 발주, 생산, BOM, 매출 등)';
      sendBtn.disabled = false;
      loadHistory();
    } else {
      // 비로그인 → 오버레이 표시, 챗봇 잠금
      overlay.style.display = 'flex';
      document.getElementById('user-name').style.display = 'none';
      document.getElementById('logout-btn').style.display = 'none';
      document.getElementById('share-btn').style.display = 'none';
      input.disabled = true;
      input.placeholder = '🔒 Google 로그인 후 사용 가능합니다';
      sendBtn.disabled = true;
      currentChatId = null;
    }
  });

  // 새 대화
  function newChat() {
    chatHistory = [];
    document.getElementById('chat-box').innerHTML = '';
    currentChatId = null;
    if (currentUser) {
      fbDb.collection('chats').add({
        userId: currentUser.uid,
        userName: currentUser.displayName || '',
        title: '새 대화',
        messages: [],
        createdAt: firebase.firestore.FieldValue.serverTimestamp(),
      }).then(ref => {
        currentChatId = ref.id;
      });
    }
  }

  // 대화 저장 (메시지 추가 시)
  function saveMessage(role, content) {
    if (!currentUser || !currentChatId) return;
    const chatRef = fbDb.collection('chats').doc(currentChatId);
    chatRef.update({
      messages: firebase.firestore.FieldValue.arrayUnion({
        role: role,
        content: content,
        timestamp: new Date().toISOString(),
      }),
      updatedAt: firebase.firestore.FieldValue.serverTimestamp(),
    });
    // 첫 메시지면 제목 업데이트
    if (role === 'user' && chatHistory.length <= 1) {
      chatRef.update({ title: content.substring(0, 30) });
    }
  }

  // 기존 sendMessage를 확장 — 대화 자동저장
  const _origSendFn = sendMessage;
  sendMessage = async function() {
    // 로그인 상태에서 chatId 없으면 자동 생성
    if (currentUser && !currentChatId) {
      const ref = await fbDb.collection('chats').add({
        userId: currentUser.uid,
        userName: currentUser.displayName || '',
        title: '새 대화',
        messages: [],
        createdAt: firebase.firestore.FieldValue.serverTimestamp(),
      });
      currentChatId = ref.id;
    }

    const input = document.getElementById('user-input');
    const msg = input.value.trim();

    await _origSendFn();

    // 저장
    if (msg) saveMessage('user', msg);
    // 봇 응답은 약간 딜레이 후 저장 (응답 도착 대기)
    setTimeout(() => {
      if (chatHistory.length > 0) {
        const last = chatHistory[chatHistory.length - 1];
        if (last.role === 'assistant') {
          saveMessage('assistant', last.content);
        }
      }
    }, 2000);
  };

  // 대화 기록 로드
  async function loadHistory() {
    if (!currentUser) return;
    const snap = await fbDb.collection('chats')
      .where('userId', '==', currentUser.uid)
      .orderBy('createdAt', 'desc')
      .limit(20)
      .get();
    const list = document.getElementById('history-list');
    list.innerHTML = '';
    snap.forEach(doc => {
      const d = doc.data();
      const div = document.createElement('div');
      div.style.cssText = 'padding:4px 8px;margin-bottom:3px;background:white;border:1px solid #e5e7eb;border-radius:6px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
      div.textContent = d.title || '새 대화';
      div.onclick = () => loadChat(doc.id);
      div.onmouseenter = () => div.style.background = '#e0e7ff';
      div.onmouseleave = () => div.style.background = 'white';
      list.appendChild(div);
    });
    if (snap.empty) {
      list.innerHTML = '<div style="color:#94a3b8;font-size:11px">아직 대화 기록이 없습니다</div>';
    }
  }

  // 이전 대화 불러오기
  async function loadChat(chatId) {
    const doc = await fbDb.collection('chats').doc(chatId).get();
    if (!doc.exists) return;
    const d = doc.data();
    currentChatId = chatId;
    chatHistory = [];
    document.getElementById('chat-box').innerHTML = '';
    (d.messages || []).forEach(m => {
      addMessage(m.role === 'assistant' ? 'bot' : 'user', m.content);
      chatHistory.push({ role: m.role, content: m.content });
    });
    document.getElementById('history-panel').style.display = 'none';
  }

  function toggleHistory() {
    const panel = document.getElementById('history-panel');
    if (panel.style.display === 'none') {
      panel.style.display = 'block';
      loadHistory();
    } else {
      panel.style.display = 'none';
    }
  }

  // 대화 공유
  async function shareChat() {
    if (!currentUser || !currentChatId) {
      alert('저장된 대화가 없습니다. 먼저 질문을 해주세요.');
      return;
    }
    const doc = await fbDb.collection('chats').doc(currentChatId).get();
    if (!doc.exists) return;
    const ref = await fbDb.collection('shared').add({
      chatData: doc.data(),
      sharedBy: currentUser.uid,
      sharedByName: currentUser.displayName || '',
      sharedAt: firebase.firestore.FieldValue.serverTimestamp(),
    });
    const shareUrl = window.location.origin + '/shared/' + ref.id;
    // 클립보드 복사
    await navigator.clipboard.writeText(shareUrl).catch(() => {});
    alert('공유 링크가 복사되었습니다!\\n' + shareUrl);
  }

  // Init
  loadDataInfo();
</script>

</body>
</html>'''


# ────────────────────────────────────────────
# 관리자 대시보드 HTML
# ────────────────────────────────────────────
ADMIN_TEMPLATE = '''<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>관리자 재고 현황 대시보드</title>
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700;900&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Noto Sans KR', -apple-system, sans-serif;
      background: #f5f6fa;
      color: #1e293b;
      min-height: 100vh;
    }
    header {
      background: white;
      color: #1e293b;
      padding: 14px 32px;
      display: flex;
      align-items: center;
      gap: 16px;
      position: sticky;
      top: 0;
      z-index: 100;
      border-bottom: 1px solid #e5e7eb;
      box-shadow: 0 1px 4px rgba(0,0,0,0.04);
    }
    header h1 { font-size: 18px; font-weight: 700; color: #1e293b; }
    header p { font-size: 11px; color: #94a3b8; margin-top: 2px; }
    .badge {
      margin-left: auto;
      background: #f0f0ff;
      border: 1px solid #e0e7ff;
      border-radius: 20px;
      padding: 5px 14px;
      font-size: 11px;
      color: #4f46e5;
    }
    .chat-link {
      color: #4f46e5;
      text-decoration: none;
      background: #f0f0ff;
      border: 1px solid #e0e7ff;
      border-radius: 20px;
      padding: 5px 14px;
      font-size: 12px;
      font-weight: 600;
    }
    .chat-link:hover { background: #e0e7ff; }
    .admin-logo {
      width: 36px; height: 36px;
      background: linear-gradient(135deg, #4f46e5, #7c3aed);
      border-radius: 10px;
      display: flex; align-items: center; justify-content: center;
      font-size: 14px; font-weight: 900; color: #fff;
    }

    .content { padding: 24px 32px; max-width: 1400px; margin: 0 auto; }

    /* 요약 카드 */
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
      margin-bottom: 28px;
    }
    .sum-card {
      background: white;
      border-radius: 12px;
      padding: 18px 22px;
      border: 1px solid #e5e7eb;
      box-shadow: 0 1px 3px rgba(0,0,0,0.04);
      border-left: 4px solid #4f46e5;
    }
    .sum-card.bj { border-left-color: #e11d48; }
    .sum-card.wj { border-left-color: #059669; }
    .sum-card.total { border-left-color: #7c3aed; }
    .sum-card label { font-size: 11px; color: #6b7280; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
    .sum-card .val { font-size: 22px; font-weight: 700; margin-top: 6px; color: #1e293b; }
    .sum-card .sub { font-size: 11px; color: #9ca3af; margin-top: 4px; }

    /* 차트 영역 */
    .chart-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      margin-bottom: 28px;
    }
    .admin-chart-card {
      background: white;
      border-radius: 12px;
      padding: 18px;
      border: 1px solid #e5e7eb;
      box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    .admin-chart-title {
      font-size: 12px;
      font-weight: 700;
      color: #6b7280;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 12px;
    }

    /* 탭 */
    .tabs {
      display: flex;
      gap: 4px;
      margin-bottom: 20px;
      background: white;
      border-radius: 10px;
      padding: 4px;
      border: 1px solid #e5e7eb;
      width: fit-content;
    }
    .tab-btn {
      padding: 8px 20px;
      border: none;
      border-radius: 8px;
      background: transparent;
      cursor: pointer;
      font-size: 13px;
      font-weight: 600;
      color: #6b7280;
      transition: all 0.2s;
    }
    .tab-btn.active { background: #4f46e5; color: white; }
    .tab-btn.bj.active { background: #e11d48; }
    .tab-btn.wj.active { background: #059669; }

    /* 업체 섹션 */
    .vendor-section {
      background: white;
      border-radius: 12px;
      margin-bottom: 14px;
      border: 1px solid #e5e7eb;
      box-shadow: 0 1px 3px rgba(0,0,0,0.04);
      overflow: hidden;
    }
    .vendor-header {
      padding: 16px 24px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 12px;
      user-select: none;
      transition: background 0.15s;
    }
    .vendor-header:hover { background: #f8f9fa; }
    .vendor-name { font-size: 16px; font-weight: 700; flex: 1; }
    .vendor-stats { display: flex; gap: 16px; align-items: center; }
    .stat-pill {
      font-size: 12px;
      padding: 4px 10px;
      border-radius: 20px;
      font-weight: 600;
    }
    .pill-bj { background: #fee2e2; color: #dc2626; }
    .pill-wj { background: #d1fae5; color: #059669; }
    .pill-total { background: #ede9fe; color: #7c3aed; }
    .chevron { font-size: 12px; color: #999; transition: transform 0.2s; }
    .chevron.open { transform: rotate(90deg); }

    /* 테이블 */
    .table-wrap { overflow: hidden; max-height: 0; transition: max-height 0.3s ease; }
    .table-wrap.open { max-height: 3000px; }
    .section-label {
      padding: 10px 24px;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .section-label.bj { background: #fff5f5; color: #dc2626; }
    .section-label.wj { background: #f0fdf4; color: #059669; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th {
      background: #f8fafc;
      padding: 10px 16px;
      text-align: left;
      font-size: 11px;
      font-weight: 700;
      color: #4b5563;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      border-bottom: 2px solid #e5e7eb;
    }
    td {
      padding: 9px 16px;
      border-bottom: 1px solid #f3f4f6;
      vertical-align: middle;
    }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #f8f9fa; }
    .no-price { color: #aaa; font-style: italic; font-size: 12px; }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    .cost-val { font-weight: 600; color: #1a1a2e; }
    .cost-zero { color: #ccc; }
    .chip {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 10px;
      font-size: 11px;
      font-weight: 600;
    }
    .chip-bj { background: #fee2e2; color: #dc2626; }
    .chip-wj { background: #d1fae5; color: #059669; }

    /* 소계 행 */
    .subtotal-row td {
      background: #f8f9fa;
      font-weight: 700;
      font-size: 12px;
      color: #444;
      border-top: 2px solid #e5e7eb;
    }

    .loading { text-align: center; padding: 60px; color: #999; font-size: 16px; }
    .no-data { padding: 20px 24px; color: #999; font-size: 13px; }

    @media (max-width: 768px) {
      .content { padding: 16px; }
      header { padding: 14px 16px; }
      .vendor-stats { flex-wrap: wrap; gap: 6px; }
    }
  </style>
</head>
<body>

<header>
  <div class="admin-logo">M</div>
  <div>
    <h1>관리자 재고 현황 대시보드</h1>
    <p id="hdr-sub">데이터 로딩 중...</p>
  </div>
  <a href="/" class="chat-link">💬 챗봇으로 이동</a>
</header>

<div class="content">
  <div id="loading" class="loading">데이터 집계 중...</div>
  <div id="main" style="display:none">

    <!-- 요약 카드 -->
    <div class="summary-grid" id="summary-grid"></div>

    <!-- 차트 -->
    <div class="chart-grid">
      <div class="admin-chart-card">
        <div class="admin-chart-title">외주업체별 재고금액 비율</div>
        <canvas id="adminVendorChart" height="200"></canvas>
      </div>
      <div class="admin-chart-card">
        <div class="admin-chart-title">부재료 vs 원재료 금액 비교</div>
        <canvas id="adminCategoryChart" height="200"></canvas>
      </div>
    </div>

    <!-- 데이터 소스 탭 -->
    <div class="tabs" style="margin-bottom:8px">
      <button class="tab-btn active" id="src-oem" onclick="switchSource('oem')">외주소분업체 재고</button>
      <button class="tab-btn" id="src-jasa" onclick="switchSource('jasa')" style="background:#7c3aed;color:white;opacity:0.6">자사 부자재 재고</button>
    </div>

    <!-- 부재료/원재료 필터 탭 -->
    <div class="tabs">
      <button class="tab-btn active" id="tab-all" onclick="switchTab('all')">전체</button>
      <button class="tab-btn bj" id="tab-bj" onclick="switchTab('bj')">부재료만</button>
      <button class="tab-btn wj" id="tab-wj" onclick="switchTab('wj')">원재료만</button>
    </div>

    <!-- 업체별 섹션 -->
    <div id="vendor-list"></div>
  </div>
</div>

<script>
let DATA = null;
let currentTab = 'all';
let currentSource = 'oem';  // 'oem' or 'jasa'

function fmt(n) {
  if (!n) return '0';
  return Number(n).toLocaleString('ko-KR');
}

async function loadData() {
  const res = await fetch('/api/admin-data');
  DATA = await res.json();
  render();
}

function render() {
  document.getElementById('loading').style.display = 'none';
  document.getElementById('main').style.display = 'block';
  document.getElementById('hdr-sub').textContent =
    `${DATA.csv_file} | 외주업체 ${DATA.vendor_count}개 | 총 ${DATA.total_rows}개 항목`;

  renderSummary();
  renderCharts();
  renderVendors();
}

const CHART_COLORS = ['#4f46e5','#0891b2','#d97706','#059669','#db2777','#7c3aed','#ea580c','#0d9488','#c026d3','#ca8a04'];

function renderCharts() {
  // 외주업체별 도넛
  const vendors = DATA.vendors.slice(0, 9);
  const vendorTotal = vendors.reduce((s, v) => s + v.total_cost, 0);
  new Chart(document.getElementById('adminVendorChart'), {
    type: 'doughnut',
    data: {
      labels: vendors.map(v => v.vendor),
      datasets: [{ data: vendors.map(v => v.total_cost), backgroundColor: CHART_COLORS, borderWidth: 0 }]
    },
    plugins: [{
      id: 'doughnutLabels',
      afterDraw(chart) {
        const ctx = chart.ctx;
        chart.data.datasets[0].data.forEach((val, i) => {
          const meta = chart.getDatasetMeta(0).data[i];
          if (!meta || meta.hidden) return;
          const pct = vendorTotal ? ((val / vendorTotal) * 100).toFixed(1) : 0;
          if (pct < 3) return;
          const pos = meta.tooltipPosition();
          ctx.save();
          ctx.font = 'bold 10px Noto Sans KR, sans-serif';
          ctx.fillStyle = '#fff';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.fillText(pct + '%', pos.x, pos.y);
          ctx.restore();
        });
      }
    }],
    options: {
      responsive: true,
      cutout: '45%',
      plugins: {
        legend: { position: 'right', labels: { color: '#374151', font: { size: 10 }, padding: 8 } },
        tooltip: {
          callbacks: {
            label: function(ctx) {
              const pct = vendorTotal ? ((ctx.raw / vendorTotal) * 100).toFixed(1) : 0;
              return ctx.label + ': ' + Number(ctx.raw).toLocaleString() + '원 (' + pct + '%)';
            }
          }
        }
      }
    }
  });

  // 부재료 vs 원재료 바 차트
  const bjData = vendors.map(v => v.bj_cost);
  const wjData = vendors.map(v => v.wj_cost);
  new Chart(document.getElementById('adminCategoryChart'), {
    type: 'bar',
    data: {
      labels: vendors.map(v => v.vendor.length > 4 ? v.vendor.slice(0,4)+'..' : v.vendor),
      datasets: [
        { label: '부재료', data: bjData, backgroundColor: '#e11d48', borderRadius: 3 },
        { label: '원재료', data: wjData, backgroundColor: '#059669', borderRadius: 3 },
      ]
    },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: '#374151', font: { size: 10 } } } },
      scales: {
        x: { ticks: { color: '#6b7280', font: { size: 9 } }, grid: { display: false } },
        y: { ticks: { color: '#6b7280', font: { size: 9 }, callback: v => v >= 10000 ? (v/10000).toFixed(0)+'만' : v }, grid: { color: 'rgba(0,0,0,0.06)' } }
      }
    }
  });
}

function renderSummary() {
  const g = document.getElementById('summary-grid');
  if (currentSource === 'oem') {
    g.innerHTML = `
      <div class="sum-card">
        <label>외주업체 수</label>
        <div class="val">${DATA.vendor_count}개</div>
        <div class="sub">총 항목 ${DATA.total_rows}개</div>
      </div>
      <div class="sum-card bj">
        <label>부재료 재고금액</label>
        <div class="val">${fmt(DATA.grand_bj_cost)}원</div>
        <div class="sub">외주소분업체 합산</div>
      </div>
      <div class="sum-card wj">
        <label>원재료 재고금액</label>
        <div class="val">${fmt(DATA.grand_wj_cost)}원</div>
        <div class="sub">외주소분업체 합산</div>
      </div>
      <div class="sum-card total">
        <label>총 재고금액</label>
        <div class="val">${fmt(DATA.grand_total)}원</div>
        <div class="sub">부재료 + 원재료</div>
      </div>`;
  } else {
    g.innerHTML = `
      <div class="sum-card">
        <label>자사 품목 수</label>
        <div class="val">${DATA.jasa_total_rows}개</div>
        <div class="sub">구분1 기준 ${DATA.jasa_groups.length}개 그룹</div>
      </div>
      <div class="sum-card bj">
        <label>자사 부재료 재고금액</label>
        <div class="val">${fmt(DATA.jasa_grand_bj)}원</div>
        <div class="sub">F열 원물 제외</div>
      </div>
      <div class="sum-card wj">
        <label>자사 원재료 재고금액</label>
        <div class="val">${fmt(DATA.jasa_grand_wj)}원</div>
        <div class="sub">F열 원물</div>
      </div>
      <div class="sum-card total">
        <label>자사 총 재고금액</label>
        <div class="val">${fmt(DATA.jasa_grand_total)}원</div>
        <div class="sub">부재료 + 원재료</div>
      </div>`;
  }
}

function renderList() {
  const list = document.getElementById('vendor-list');
  list.innerHTML = '';
  const groups = currentSource === 'oem' ? DATA.vendors : DATA.jasa_groups;
  const nameKey = currentSource === 'oem' ? 'vendor' : 'group';

  groups.forEach((v, idx) => {
    const sec = document.createElement('div');
    sec.className = 'vendor-section';
    const showBj = currentTab === 'all' || currentTab === 'bj';
    const showWj = currentTab === 'all' || currentTab === 'wj';

    let pills = '';
    if (showBj) pills += `<span class="stat-pill pill-bj">부재료 ${fmt(v.bj_cost)}원 (${v.bj_count}품목)</span>`;
    if (showWj) pills += `<span class="stat-pill pill-wj">원재료 ${fmt(v.wj_cost)}원 (${v.wj_count}품목)</span>`;
    pills += `<span class="stat-pill pill-total">합계 ${fmt(v.total_cost)}원</span>`;

    const bjLabel = currentSource === 'oem' ? '부재료 (포장재·파우치·박스류)' : '부재료 (파우치·PP·단상자 등)';
    const wjLabel = currentSource === 'oem' ? '원재료 (완제품)' : '원재료 (원물)';
    const hasExtra = currentSource === 'jasa';

    sec.innerHTML = `
      <div class="vendor-header" onclick="toggleVendor(${idx})">
        <div class="vendor-name">${v[nameKey]}</div>
        <div class="vendor-stats">${pills}</div>
        <span class="chevron" id="chev-${idx}">&#9654;</span>
      </div>
      <div class="table-wrap" id="wrap-${idx}">
        ${showBj ? buildTable(v.bj_items, 'bj', bjLabel, hasExtra) : ''}
        ${showWj ? buildTable(v.wj_items, 'wj', wjLabel, hasExtra) : ''}
      </div>
    `;
    list.appendChild(sec);
  });
}

function buildTable(items, type, label, hasExtra) {
  if (!items || items.length === 0)
    return `<div class="section-label ${type}">${label}</div><div class="no-data">해당 항목 없음</div>`;

  const totalCost = items.reduce((s, i) => s + i['재고금액'], 0);
  const extraTh = hasExtra ? '<th>업체</th><th>분류</th>' : '<th>규격</th>';
  const rows = items.map(i => {
    const costClass = i['재고금액'] ? 'cost-val' : 'cost-zero';
    const costTxt = i['단가유무'] ? fmt(i['재고금액']) + '원' : '<span class="no-price">단가없음</span>';
    const extraTd = hasExtra
      ? `<td>${i['업체'] || '-'}</td><td><span class="chip chip-${type}">${i['구분2'] || '-'}</span></td>`
      : `<td><span class="chip chip-${type}">${i['규격'] || '-'}</span></td>`;
    return `
      <tr>
        <td>${i['품번'] || '-'}</td>
        <td>${i['품명'] || '-'}</td>
        ${extraTd}
        <td class="num">${fmt(i['재고량'])}</td>
        <td class="num">${i['단가유무'] ? fmt(i['단가']) + '원' : '<span class="no-price">-</span>'}</td>
        <td class="num ${costClass}">${costTxt}</td>
      </tr>`;
  }).join('');

  const colSpan = hasExtra ? 6 : 5;
  return `
    <div class="section-label ${type}">${label}</div>
    <table>
      <thead>
        <tr>
          <th>품번</th><th>품명</th>${extraTh}
          <th class="num">총재고</th><th class="num">단가</th><th class="num">재고금액</th>
        </tr>
      </thead>
      <tbody>
        ${rows}
        <tr class="subtotal-row">
          <td colspan="${colSpan}">소계 (${items.length}품목, 단가적용 ${items.filter(i=>i['단가유무']).length}개)</td>
          <td class="num">${fmt(totalCost)}원</td>
        </tr>
      </tbody>
    </table>`;
}

function toggleVendor(idx) {
  const wrap = document.getElementById('wrap-' + idx);
  const chev = document.getElementById('chev-' + idx);
  wrap.classList.toggle('open');
  chev.classList.toggle('open');
}

function switchTab(tab) {
  currentTab = tab;
  ['all','bj','wj'].forEach(t => {
    document.getElementById('tab-' + t).classList.toggle('active', t === tab);
  });
  renderList();
}

function switchSource(src) {
  currentSource = src;
  document.getElementById('src-oem').classList.toggle('active', src === 'oem');
  document.getElementById('src-jasa').classList.toggle('active', src === 'jasa');
  document.getElementById('src-oem').style.opacity = src === 'oem' ? '1' : '0.6';
  document.getElementById('src-jasa').style.opacity = src === 'jasa' ? '1' : '0.6';
  renderSummary();
  renderList();
}

loadData();
</script>
</body>
</html>'''


# ────────────────────────────────────────────
# 업로드 페이지 HTML
# ────────────────────────────────────────────
UPLOAD_TEMPLATE = '''<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>데이터 업로드</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Pretendard','Noto Sans KR',sans-serif; background: #f8f9fa; color: #1a1a2e; min-height: 100vh; }
    header { background: #1a1a2e; color: white; padding: 16px 32px; display: flex; align-items: center; gap: 16px; }
    header h1 { font-size: 18px; }
    .nav-links { margin-left: auto; display: flex; gap: 8px; }
    .nav-links a { color: white; text-decoration: none; background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2); border-radius: 20px; padding: 5px 14px; font-size: 12px; }
    .nav-links a:hover { background: rgba(255,255,255,0.2); }
    .content { max-width: 800px; margin: 32px auto; padding: 0 24px; }
    .upload-card { background: white; border-radius: 12px; padding: 24px; margin-bottom: 16px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); border: 1px solid #e5e7eb; }
    .upload-card h3 { font-size: 15px; margin-bottom: 4px; color: #1a1a2e; }
    .upload-card .desc { font-size: 12px; color: #888; margin-bottom: 14px; }
    .upload-card .target { font-size: 11px; color: #666; background: #f3f4f6; padding: 4px 10px; border-radius: 6px; display: inline-block; margin-bottom: 12px; }
    .file-row { display: flex; gap: 8px; align-items: center; }
    .file-input { flex: 1; font-size: 13px; }
    .upload-btn { background: #1a1a2e; color: white; border: none; border-radius: 8px; padding: 8px 20px; font-size: 13px; font-weight: 600; cursor: pointer; white-space: nowrap; }
    .upload-btn:hover { background: #0f3460; }
    .upload-btn:disabled { background: #ccc; cursor: not-allowed; }
    .status { margin-top: 10px; font-size: 12px; padding: 8px 12px; border-radius: 8px; display: none; white-space: pre-wrap; }
    .status.ok { display: block; background: #f0fdf4; color: #166534; border: 1px solid #bbf7d0; }
    .status.err { display: block; background: #fef2f2; color: #991b1b; border: 1px solid #fecaca; }
    .status.loading { display: block; background: #eff6ff; color: #1e40af; border: 1px solid #bfdbfe; }
    .info-box { background: white; border-radius: 12px; padding: 20px 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); border: 1px solid #e5e7eb; margin-bottom: 16px; }
    .info-box h3 { font-size: 14px; margin-bottom: 8px; }
    .info-box p { font-size: 12px; color: #666; line-height: 1.8; }
    .info-box code { background: #f3f4f6; padding: 2px 6px; border-radius: 4px; font-size: 11px; }
  </style>
</head>
<body>
<header>
  <h1>📁 데이터 업로드</h1>
  <div class="nav-links">
    <a href="/">💬 챗봇</a>
    <a href="/admin">📊 대시보드</a>
  </div>
</header>
<div class="content">
  <div class="info-box">
    <h3>사용 방법</h3>
    <p>
      1. 최신 엑셀 파일을 아래에서 선택하여 업로드합니다.<br>
      2. 동일한 파일명으로 기존 파일을 교체하고 CSV 변환을 자동 실행합니다.<br>
      3. 업로드 완료 후 <strong>서버 재시작</strong>이 필요합니다. (<code>run_chatbot.bat</code> 실행)<br>
    </p>
  </div>

  <div id="cards"></div>
</div>
<script>
const FILES = {
  '재고파일 (원자재부자재 재고파악)': { target: '원자재부자재 재고파악(3월) - 최종본.xlsx', desc: '외주업체 재고일지 (daily _ 완제품 재고일지 시트)' },
  '단가파일 (26년 원부자재 단가)': { target: '26년 원부자재 단가.xlsx', desc: '부자재·완제품 단가 데이터' },
  '부자재 규격': { target: '부자재 규격.xlsx', desc: '부재료 제조업체·사이즈·재질·MOQ' },
  '자사재고 (자사사용 부자재)': { target: '자사사용 부자재_REV.260224_지우철_1.xlsx', desc: '자사 부자재 총재고 (생산러닝 부자재 시트)' },
};

const container = document.getElementById('cards');
Object.entries(FILES).forEach(([name, info]) => {
  const id = name.replace(/[^a-zA-Z가-힣]/g, '');
  container.innerHTML += `
    <div class="upload-card">
      <h3>${name}</h3>
      <div class="desc">${info.desc}</div>
      <div class="target">📄 ${info.target}</div>
      <form id="form-${id}" onsubmit="return doUpload(event, '${name}', '${id}')">
        <div class="file-row">
          <input type="file" accept=".xlsx" class="file-input" name="file" required>
          <button type="submit" class="upload-btn" id="btn-${id}">업로드</button>
        </div>
      </form>
      <div class="status" id="status-${id}"></div>
    </div>
  `;
});

async function doUpload(e, fileType, id) {
  e.preventDefault();
  const form = document.getElementById('form-' + id);
  const btn = document.getElementById('btn-' + id);
  const status = document.getElementById('status-' + id);
  const fd = new FormData(form);
  fd.append('file_type', fileType);

  btn.disabled = true;
  btn.textContent = '업로드 중...';
  status.className = 'status loading';
  status.textContent = '파일 업로드 및 변환 진행 중...';

  try {
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.ok) {
      status.className = 'status ok';
      status.textContent = data.msg;
    } else {
      status.className = 'status err';
      status.textContent = '❌ ' + data.msg;
    }
  } catch (err) {
    status.className = 'status err';
    status.textContent = '❌ 업로드 실패: ' + err.message;
  }
  btn.disabled = false;
  btn.textContent = '업로드';
  return false;
}
</script>
</body>
</html>'''


# ────────────────────────────────────────────
# Firebase API 엔드포인트 (히스토리, 공유)
# ────────────────────────────────────────────
def _verify_firebase_token(req):
    """Authorization 헤더에서 Firebase ID 토큰 검증 → uid 반환"""
    auth_header = req.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None
    token = auth_header.split('Bearer ')[1]
    try:
        decoded = fb_auth.verify_id_token(token)
        return decoded
    except Exception:
        return None


@app.route('/api/chats', methods=['GET'])
def list_chats():
    """로그인 사용자의 대화 목록 조회"""
    user = _verify_firebase_token(request)
    if not user:
        return jsonify({'error': '로그인 필요'}), 401
    uid = user['uid']
    chats_ref = FIRESTORE_DB.collection('chats')
    docs = chats_ref.where('userId', '==', uid).order_by('createdAt', direction=fs_admin.Query.DESCENDING).limit(50).stream()
    result = []
    for doc in docs:
        d = doc.to_dict()
        result.append({
            'id': doc.id,
            'title': d.get('title', ''),
            'createdAt': str(d.get('createdAt', '')),
            'messageCount': len(d.get('messages', [])),
        })
    return jsonify(result)


@app.route('/api/chats', methods=['POST'])
def create_chat():
    """새 대화 생성"""
    user = _verify_firebase_token(request)
    if not user:
        return jsonify({'error': '로그인 필요'}), 401
    data = request.json or {}
    chat_ref = FIRESTORE_DB.collection('chats').document()
    chat_data = {
        'userId': user['uid'],
        'userName': user.get('name', ''),
        'title': data.get('title', '새 대화'),
        'messages': [],
        'createdAt': fs_admin.SERVER_TIMESTAMP,
        'updatedAt': fs_admin.SERVER_TIMESTAMP,
    }
    chat_ref.set(chat_data)
    return jsonify({'id': chat_ref.id})


@app.route('/api/chats/<chat_id>', methods=['GET'])
def get_chat(chat_id):
    """대화 상세 조회"""
    user = _verify_firebase_token(request)
    if not user:
        return jsonify({'error': '로그인 필요'}), 401
    doc = FIRESTORE_DB.collection('chats').document(chat_id).get()
    if not doc.exists:
        return jsonify({'error': '대화를 찾을 수 없습니다'}), 404
    d = doc.to_dict()
    if d.get('userId') != user['uid']:
        return jsonify({'error': '권한 없음'}), 403
    return jsonify({'id': doc.id, **d, 'createdAt': str(d.get('createdAt', '')), 'updatedAt': str(d.get('updatedAt', ''))})


@app.route('/api/chats/<chat_id>/messages', methods=['POST'])
def add_message(chat_id):
    """대화에 메시지 추가"""
    user = _verify_firebase_token(request)
    if not user:
        return jsonify({'error': '로그인 필요'}), 401
    data = request.json or {}
    doc_ref = FIRESTORE_DB.collection('chats').document(chat_id)
    doc = doc_ref.get()
    if not doc.exists or doc.to_dict().get('userId') != user['uid']:
        return jsonify({'error': '권한 없음'}), 403
    messages = doc.to_dict().get('messages', [])
    messages.append({
        'role': data.get('role', 'user'),
        'content': data.get('content', ''),
        'timestamp': __import__('datetime').datetime.now().isoformat(),
    })
    # 첫 메시지면 제목 업데이트
    update = {'messages': messages, 'updatedAt': fs_admin.SERVER_TIMESTAMP}
    if len(messages) == 1:
        update['title'] = data.get('content', '')[:30]
    doc_ref.update(update)
    return jsonify({'ok': True})


@app.route('/api/chats/<chat_id>/share', methods=['POST'])
def share_chat(chat_id):
    """대화 공유 링크 생성"""
    user = _verify_firebase_token(request)
    if not user:
        return jsonify({'error': '로그인 필요'}), 401
    doc = FIRESTORE_DB.collection('chats').document(chat_id).get()
    if not doc.exists or doc.to_dict().get('userId') != user['uid']:
        return jsonify({'error': '권한 없음'}), 403
    share_ref = FIRESTORE_DB.collection('shared').document()
    share_ref.set({
        'chatId': chat_id,
        'chatData': doc.to_dict(),
        'sharedBy': user['uid'],
        'sharedByName': user.get('name', ''),
        'sharedAt': fs_admin.SERVER_TIMESTAMP,
    })
    return jsonify({'shareId': share_ref.id})


@app.route('/api/shared/<share_id>', methods=['GET'])
def get_shared(share_id):
    """공유된 대화 조회 (로그인 불필요)"""
    doc = FIRESTORE_DB.collection('shared').document(share_id).get()
    if not doc.exists:
        return jsonify({'error': '공유 링크를 찾을 수 없습니다'}), 404
    d = doc.to_dict()
    chat = d.get('chatData', {})
    return jsonify({
        'title': chat.get('title', ''),
        'messages': chat.get('messages', []),
        'sharedBy': d.get('sharedByName', ''),
        'sharedAt': str(d.get('sharedAt', '')),
    })


if __name__ == '__main__':
    print("\n" + "="*60)
    print("  매홍 L&F 통합 재고 관리 챗봇")
    print("  http://localhost:5000 에서 접속하세요")
    print("  http://localhost:5000/upload 에서 파일 업로드")
    print("  Firebase Auth + Firestore 연동")
    print("="*60 + "\n")
    app.run(debug=False, port=5000, host='0.0.0.0')

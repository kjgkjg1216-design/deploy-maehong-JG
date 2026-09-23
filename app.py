"""
완제품 재고 챗봇 - RAG 기반 Flask 앱
데이터 출처: 원자재부자재 재고파악(3월) - 최종본.xlsx
시트: daily _ 완제품 재고일지
"""
import os
import re
import glob
import httpx
import threading
import json
import time
import hmac
import hashlib
import subprocess
import sys
from datetime import datetime
import pandas as pd
from flask import Flask, request, jsonify, render_template_string, g
from flask_cors import CORS
from openai import OpenAI
import firebase_admin
from firebase_admin import credentials, firestore as fs_admin, auth as fb_auth
from dotenv import load_dotenv

# 경로 베이스 (로컬 기본값=현재 Windows 경로 / 클라우드는 환경변수로 override)
BASE_DIR = os.environ.get('APP_BASE_DIR', 'C:/Users/jgkim/maehong-JG')
DATA_DIR = os.environ.get('DATA_DIR', BASE_DIR + '/data')

# .env 로드
load_dotenv(os.path.join(BASE_DIR, '.env'))

app = Flask(__name__)
CORS(app)

# Firebase Admin SDK 초기화
if not firebase_admin._apps:
    _fb_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT')  # 클라우드: 시크릿 JSON 문자열
    _fb_key = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'maehong-scm-firebase-adminsdk-fbsvc-8cae1845b3.json')
    if _fb_json:
        import json as _json
        _cred = credentials.Certificate(_json.loads(_fb_json))
        firebase_admin.initialize_app(_cred)
        print("[Firebase] 환경변수 서비스 계정으로 초기화 완료")
    elif os.path.exists(_fb_key):
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
# 서버측 구글 OAuth 로그인 (브라우저→identitytoolkit fetch 차단 우회)
#   클라이언트 Firebase 로그인이 일부 환경에서 CORS/HTTP2로 막혀,
#   페이지 전체 이동(authorization code flow)으로 전환.
# ────────────────────────────────────────────
import secrets as _secrets
import requests as _oauth_req
from urllib.parse import urlencode as _urlencode
from flask import session, redirect, Response

app.secret_key = os.environ.get('FLASK_SECRET', 'maehong-dev-secret-change-me-please')
from datetime import timedelta as _timedelta
app.permanent_session_lifetime = _timedelta(days=30)

OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '')
OAUTH_CLIENT_SECRET = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '')
OAUTH_REDIRECT_URI = os.environ.get('OAUTH_REDIRECT_URI', 'https://8.235.41.127.sslip.io/auth/callback')

# 방문이력 등 관리자 페이지 열람 허용 이메일 (쉼표구분 env로 추가 가능)
_ADMIN_EMAILS = set(filter(None, ['kjgkjg1216@gmail.com'] + [
    e.strip() for e in os.environ.get('ADMIN_EMAILS', '').split(',') if e.strip()]))

# 로그인 불필요 경로 (Monday 웹훅은 외부에서 호출하므로 공개)
_PUBLIC_PREFIXES = ('/auth/', '/shared/', '/api/shared/', '/static/', '/favicon', '/api/monday_webhook', '/api/data_version', '/api/external/',
                    '/v/', '/api/v/')   # 거래처 재고 입력 포털(토큰 링크, 2026-09-11)


def current_user():
    """세션 로그인 사용자 dict (uid/email/name/picture) 또는 None."""
    return session.get('user')


def _log_visit(kind='login'):
    """방문/로그인 이력을 Firestore 'visits' 컬렉션에 기록 (Firebase 콘솔에서 조회 가능)."""
    try:
        u = session.get('user') or {}
        FIRESTORE_DB.collection('visits').add({
            'kind': kind,                       # 'login' | 'visit'
            'email': u.get('email', ''),
            'name': u.get('name', ''),
            'uid': u.get('uid', ''),
            'path': request.path,
            'ip': (request.headers.get('X-Forwarded-For', request.remote_addr) or '').split(',')[0].strip(),
            'ua': request.headers.get('User-Agent', '')[:200],
            'ts': fs_admin.SERVER_TIMESTAMP,
        })
    except Exception as e:
        print(f"[visit log 실패] {e}")


@app.before_request
def _require_login():
    # OAuth 미설정 환경(로컬 PC)에선 로그인 게이트 비활성화 → 바로 사용.
    # 클라우드는 systemd로 GOOGLE_OAUTH_CLIENT_ID가 주입돼 있어 정상 적용됨.
    if not OAUTH_CLIENT_ID:
        return None
    p = request.path or '/'
    if p == '/login' or any(p.startswith(x) for x in _PUBLIC_PREFIXES):
        return None
    # 로컬 갱신/감지 스크립트용 토큰 우회
    if p in ('/api/reload_dfs', '/api/monday_dirty', '/api/monday_dirty/clear', '/api/vendor_entries'):
        _tok = os.environ.get('RELOAD_TOKEN', '')
        if _tok and request.args.get('token') == _tok:
            return None
    if session.get('user'):
        return None
    if p.startswith('/api/'):
        return jsonify({'error': 'login_required'}), 401
    return redirect('/login')


LOGIN_TEMPLATE = '''<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>구매/외주 대시보드 — 로그인</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,'Segoe UI',Roboto,'Malgun Gothic',sans-serif;
       background:#0b1020;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
  .card{background:#fff;border-radius:20px;padding:44px 36px;max-width:380px;width:100%;text-align:center;
        box-shadow:0 20px 60px rgba(0,0,0,.4)}
  .logo{width:72px;height:72px;border-radius:18px;background:linear-gradient(135deg,#7c3aed,#a855f7);
        display:flex;align-items:center;justify-content:center;margin:0 auto 18px;
        font-size:38px;font-weight:800;color:#fff}
  h1{font-size:21px;color:#111;margin-bottom:8px}
  p{color:#666;font-size:14px;margin-bottom:26px;line-height:1.5}
  .btn{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:13px;
       border-radius:11px;font-size:15px;font-weight:600;cursor:pointer;text-decoration:none;border:1px solid #ddd}
  .g{background:#fff;color:#222;margin-bottom:12px}
  .g:hover{background:#f5f5f5}
  .skip{background:transparent;color:#888;border-color:#eee;font-size:13px}
</style></head><body>
  <div class="card">
    <div class="logo">M</div>
    <h1>구매/외주 대시보드</h1>
    <p>BOM · 단가 · 외주처별 재고 통합 조회 플랫폼<br>Google 계정으로 로그인하여 시작하세요</p>
    <a class="btn g" href="/auth/google">
      <img src="https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg" width="20" height="20"> Google로 계속하기
    </a>
  </div>
</body></html>'''


@app.route('/login')
def login_page():
    session.pop('guest', None)   # 과거 게스트 쿠키 제거 (리다이렉트 루프 방지)
    if session.get('user'):
        return redirect('/')
    return render_template_string(LOGIN_TEMPLATE)


@app.route('/auth/google')
def auth_google():
    if not OAUTH_CLIENT_ID:
        return 'OAuth 미설정 (GOOGLE_OAUTH_CLIENT_ID 없음)', 500
    state = _secrets.token_urlsafe(24)
    session['oauth_state'] = state
    params = {
        'client_id': OAUTH_CLIENT_ID,
        'redirect_uri': OAUTH_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
        'access_type': 'online',
        'prompt': 'select_account',
    }
    return redirect('https://accounts.google.com/o/oauth2/v2/auth?' + _urlencode(params))


@app.route('/auth/callback')
def auth_callback():
    if not request.args.get('state') or request.args.get('state') != session.pop('oauth_state', None):
        return '잘못된 요청 (state). <a href="/login">다시 로그인</a>', 400
    code = request.args.get('code')
    if not code:
        return '인증 코드 없음. <a href="/login">다시 로그인</a>', 400
    try:
        tok = _oauth_req.post('https://oauth2.googleapis.com/token', data={
            'code': code,
            'client_id': OAUTH_CLIENT_ID,
            'client_secret': OAUTH_CLIENT_SECRET,
            'redirect_uri': OAUTH_REDIRECT_URI,
            'grant_type': 'authorization_code',
        }, timeout=15).json()
        access = tok.get('access_token')
        if not access:
            return '토큰 교환 실패: ' + str(tok.get('error_description') or tok), 400
        info = _oauth_req.get('https://www.googleapis.com/oauth2/v2/userinfo',
                              headers={'Authorization': 'Bearer ' + access}, timeout=15).json()
    except Exception as e:
        return '로그인 처리 오류: ' + str(e), 500
    session['user'] = {
        'uid': info.get('id', ''),
        'email': info.get('email', ''),
        'name': info.get('name', '') or info.get('email', ''),
        'picture': info.get('picture', ''),
    }
    session.pop('guest', None)
    session.permanent = True
    print(f"[로그인] {session['user'].get('email')}")
    _log_visit('login')   # 로그인 이력 → Firestore 'visits'
    return redirect('/')


@app.route('/auth/skip')
def auth_skip():
    # 게스트 사용 비활성화 — 로그인 필수
    return redirect('/login')


@app.route('/auth/logout')
def auth_logout():
    session.clear()
    return redirect('/login')


from datetime import timezone as _tz_mod, timedelta as _td_mod
_KST = _tz_mod(_td_mod(hours=9))


def _fmt_kst(ts, fmt='%Y-%m-%d %H:%M:%S'):
    """Firestore 타임스탬프(UTC)를 한국시간(KST, +9)으로 포맷."""
    if not hasattr(ts, 'strftime'):
        return str(ts or '')
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz_mod.utc)
        return ts.astimezone(_KST).strftime(fmt)
    except Exception:
        return ts.strftime(fmt)


@app.route('/admin/visits')
def admin_visits():
    """방문/로그인 이력 조회 (관리자 전용)."""
    u = current_user()
    if not u or u.get('email') not in _ADMIN_EMAILS:
        return '권한 없음 (관리자만 열람 가능)', 403
    try:
        docs = (FIRESTORE_DB.collection('visits')
                .order_by('ts', direction=fs_admin.Query.DESCENDING).limit(500).stream())
        rows = []
        for d in docs:
            x = d.to_dict()
            ts = x.get('ts')
            rows.append({
                'ts': _fmt_kst(ts, '%Y-%m-%d %H:%M:%S'),
                'kind': x.get('kind', ''), 'email': x.get('email', ''),
                'name': x.get('name', ''), 'ip': x.get('ip', ''),
            })
    except Exception as e:
        return f'조회 오류: {e}', 500

    # 채팅/검색 이력 (chats 컬렉션 — 사용자가 AI에게 질문/검색한 내역)
    chat_rows = []
    try:
        cdocs = (FIRESTORE_DB.collection('chats')
                 .order_by('createdAt', direction=fs_admin.Query.DESCENDING).limit(300).stream())
        for d in cdocs:
            x = d.to_dict()
            msgs = x.get('messages', [])
            q = next((m.get('content') for m in msgs if m.get('role') == 'user'), x.get('title', ''))
            a = next((m.get('content') for m in msgs if m.get('role') == 'assistant'), '')
            ct = x.get('createdAt')
            chat_rows.append({
                'ts': _fmt_kst(ct, '%Y-%m-%d %H:%M'),
                'name': x.get('userName', '') or x.get('userId', ''),
                'src': '대시보드' if x.get('source') == 'dashboard' else '챗봇',
                'q': (q or '')[:150], 'a': (a or '')[:2000],
            })
    except Exception as e:
        print(f"[admin chats 조회 오류] {e}")

    # 통계
    from collections import Counter
    by_email = Counter(r['email'] for r in rows if r['email'])
    logins = sum(1 for r in rows if r['kind'] == 'login')
    stat_html = ' · '.join(f'{e or "(미상)"}: {n}회' for e, n in by_email.most_common(20)) or '기록 없음'
    SHOW = 10   # 처음엔 최근 10건만, 나머지는 버튼으로 펼침
    OLD_ATTR = ' class="old"'   # f-string 식 안에 백슬래시 금지(GCP 파이썬 <3.12 호환)
    def _tr_old(i):
        return OLD_ATTR if i >= SHOW else ''
    body = ''.join(
        f'<tr{_tr_old(i)}><td class="nw">{r["ts"]}</td><td><span class="k k-{r["kind"]}">{"로그인" if r["kind"]=="login" else "방문"}</span></td>'
        f'<td>{_html_esc(r["name"])}</td><td>{_html_esc(r["email"])}</td><td class="ip">{_html_esc(r["ip"])}</td></tr>'
        for i, r in enumerate(rows))
    chat_body = ''.join(
        f'<tr{_tr_old(i)}><td class="nw">{c["ts"]}</td><td>{_html_esc(c["name"])}</td>'
        f'<td><span class="k k-{"visit" if c["src"]=="대시보드" else "login"}">{c["src"]}</span></td>'
        f'<td class="q">{_html_esc(c["q"])}</td><td class="a" title="클릭하여 펼치기/접기" onclick="this.classList.toggle(\'expanded\')">{_html_esc(c["a"])}</td></tr>'
        for i, c in enumerate(chat_rows))
    def _more_btn(tid, n):
        hidden = max(0, n - SHOW)
        if hidden <= 0:
            return ''
        return (f'<div class="more"><button class="more-btn" data-t="{tid}" data-n="{hidden}" onclick="toggleOld(this)">'
                f'▼ 과거 이력 {hidden}건 더 보기</button></div>')
    visit_more, chat_more = _more_btn('t-visit', len(rows)), _more_btn('t-chat', len(chat_rows))
    return f'''<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>방문자·검색 이력</title>
<style>
 body{{font-family:-apple-system,'Malgun Gothic',sans-serif;background:#f5f6fa;margin:0;padding:20px;color:#1e293b}}
 h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:16px;margin:22px 0 8px}}
 .sub{{color:#64748b;font-size:13px;margin-bottom:14px}}
 .stat{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px;margin-bottom:14px;font-size:13px;line-height:1.7}}
 table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;font-size:13px;table-layout:fixed}}
 th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid #eef2f7;vertical-align:top;word-break:break-word}}
 th{{background:#f8fafc;font-weight:700;color:#475569}}
 .k{{font-size:11px;font-weight:700;padding:2px 7px;border-radius:6px;white-space:nowrap}}
 .k-login{{background:#dcfce7;color:#15803d}} .k-visit{{background:#e0f2fe;color:#0369a1}}
 .ip{{color:#64748b;font-size:11px}} a{{color:#2563eb}} .nw{{white-space:nowrap;color:#64748b}}
 .q{{font-weight:600;color:#4f46e5}}
 .a{{color:#64748b;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer;max-width:0}}
 .a:hover{{color:#334155}} .a.expanded{{white-space:pre-wrap;overflow:visible;word-break:break-word}}
 tr.old{{display:none}} table.open tr.old{{display:table-row}}
 .more{{text-align:center;margin:8px 0 4px}}
 .more-btn{{padding:7px 16px;font-size:12.5px;font-weight:700;border:1px solid #cbd5e1;border-radius:8px;background:#fff;color:#475569;cursor:pointer}}
 .more-btn:hover{{background:#f8fafc;border-color:#94a3b8}}
</style>
<script>
function toggleOld(b){{var t=document.getElementById(b.dataset.t);var open=t.classList.toggle('open');
  b.textContent=(open?'▲ 최근 10건만 보기':'▼ 과거 이력 '+b.dataset.n+'건 더 보기');
  if(!open) t.scrollIntoView({{block:'start',behavior:'smooth'}});}}
</script></head><body>
 <h1>📋 방문자 · 검색 이력</h1>
 <div class="sub">관리자 전용 · <a href="/">← 대시보드</a></div>
 <div class="stat"><b>사용자별 접속 횟수:</b><br>{stat_html}<br><br><b>총 로그인:</b> {logins}회 · <b>방문기록:</b> {len(rows)}건 · <b>검색/채팅:</b> {len(chat_rows)}건</div>

 <h2>👥 방문 · 로그인 이력 <span style="font-size:12px;color:#64748b;font-weight:600">최근 {min(SHOW, len(rows))}건 표시 · 전체 {len(rows)}건</span></h2>
 <table id="t-visit"><thead><tr><th style="width:160px">시각 (KST)</th><th style="width:70px">구분</th><th style="width:110px">이름</th><th>이메일</th><th style="width:130px">IP</th></tr></thead>
 <tbody>{body or '<tr><td colspan=5 style="text-align:center;color:#64748b;padding:30px">아직 기록이 없습니다</td></tr>'}</tbody></table>
 {visit_more}

 <h2>🔍 검색 · 채팅 내역 <span style="font-size:12px;color:#64748b;font-weight:600">최근 {min(SHOW, len(chat_rows))}건 표시 · 전체 {len(chat_rows)}건</span></h2>
 <table id="t-chat"><thead><tr><th style="width:120px">시각</th><th style="width:90px">이름</th><th style="width:70px">위치</th><th style="width:32%">질문/검색</th><th>답변(요약)</th></tr></thead>
 <tbody>{chat_body or '<tr><td colspan=5 style="text-align:center;color:#64748b;padding:30px">아직 검색/채팅 기록이 없습니다</td></tr>'}</tbody></table>
 {chat_more}
</body></html>'''


def _html_esc(s):
    return (str(s or '').replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


# ────────────────────────────────────────────
# 데이터 로드 (CSV)
# ────────────────────────────────────────────
INVENTORY_ONEDRIVE_PATTERN = 'C:/Users/jgkim/OneDrive/**/*재고파악*.xlsx'
# 구매팀 실제 작업 폴더 (바탕 화면\구매\YYYY년도\N월). '바로 가기\구매팀\…'은 같은 파일의 공유 미러라 동기화 지연으로 mtime이 더 새로울 수 있음.
INVENTORY_PREFERRED_DIR = 'C:/Users/jgkim/OneDrive/바탕 화면/구매'
# 취합본만 인정 — '재고파악(N월).xlsx' / '- 마감' / '- 최종본' / 단일 마스터 '재고파악.xlsx'.
# '- 정성', '- 데이웰즈' 같은 외주처 제출본(부분 데이터)과 카톡 사본 '(1)'류는 제외.
# 2026-09-16 사용자 정의: 월 파일 = 기초재고 조정용, '마감' 파일 = 데일리 사용량이 기록되는 실재고 → 마감이 있으면 항상 마감.
_INVENTORY_NAME_RE = re.compile(
    r'원자재부자재 재고파악(\(\d+월\))?(\s*[-_]\s*(마감|최종본))?\.xlsx$')

def _find_inventory_xlsx():
    """OneDrive에서 외주재고 취합본 중 최신 파일 반환 (mtime 기준).

    ⚠️ '최종본' 고정 매칭 금지 — 파일명 관례가 '마감'으로 바뀌자 앱이 4월
    최종본을 계속 읽는 고착이 있었음(2026-09-01). 취합본 이름 규칙만 whitelist."""
    files = []
    for f in glob.glob(INVENTORY_ONEDRIVE_PATTERN, recursive=True):
        base = os.path.basename(f)
        if base.startswith('~$'):
            continue
        if _INVENTORY_NAME_RE.search(base):
            files.append(f)
    if not files:
        return None
    # 선택 규칙(2026-09-16): ① 가장 최근에 수정된 파일이 속한 '월' 그룹을 고른다(옛 달 마감이 고착되지 않게)
    # ② 그 달 안에서는 '마감' > '최종본' > 월 파일 순으로 우선(수정 시각과 무관 — 마감이 생기면 곧바로 기준)
    # ③ 같은 종류가 여러 폴더에 있으면 사용자 작업 폴더(INVENTORY_PREFERRED_DIR) > 공유 바로가기 미러 > 최신 mtime.
    # 사례: 바탕 화면\구매\2026년도\9월\(9월) - 마감.xlsx 12:05:23 vs 바로 가기\…\(9월).xlsx 12:06:15(동기화 지연) → 예전 규칙은 월 파일을 골랐음.
    def _mon(f):
        """(연, 월) — 연도는 경로의 'YYYY년/YYYY년도' 폴더명, 없으면 수정 시각의 연도. 12월→1월 넘어가도 큰 연월이 최신."""
        mm = re.search(r'\((\d+)월\)', os.path.basename(f))
        mon = int(mm.group(1)) if mm else 0
        yy = re.search(r'(20\d{2})년', f.replace('\\', '/'))
        if yy:
            year = int(yy.group(1))
        else:
            # 연도 폴더가 없으면 수정 시각의 연도 — 단, 12월 파일을 이듬해 1월에 손본 경우처럼
            # 파일 월이 수정 월보다 2개월 이상 앞서면 전년도 파일로 본다(10월 파일을 9월 말에 미리 만드는 정도는 허용).
            mt = datetime.fromtimestamp(os.path.getmtime(f))
            year = mt.year - 1 if mon - mt.month > 1 else mt.year
        return (year, mon)
    def _typ(f):
        b = os.path.basename(f)
        return 2 if '마감' in b else (1 if '최종본' in b else 0)
    def _pref(f):
        return 1 if f.replace('\\', '/').lower().startswith(INVENTORY_PREFERRED_DIR.lower()) else 0
    # 최신 파일 기준 120일 이내 후보 중 **가장 큰 (연, 월)** 그룹 — 10월 파일을 만든 뒤 9월 마감을 손봐도 10월이 유지됨.
    # (2026-09-16 v4) 그 안에서 마감 > 최종본 > 월 파일, 같은 종류면 작업 폴더 > 미러 > mtime.
    recent = max(os.path.getmtime(f) for f in files) - 120 * 86400
    # 월 값이 1~12가 아닌 이름('재고파악(52월).xlsx' 같은 임시 파일)은 제외
    cands = [f for f in files if os.path.getmtime(f) >= recent and 1 <= _mon(f)[1] <= 12] or files
    top = max(_mon(f) for f in cands)
    group = [f for f in cands if _mon(f) == top]
    return max(group, key=lambda f: (_typ(f), _pref(f), os.path.getmtime(f)))

def _parse_inventory_excel(xlsx_path):
    """원자재부자재 재고파악 Excel → DataFrame (시트4, 헤더5행).

    ⚠️ 원본을 절대 직접 열지 않는다 — 항상 임시 복사본으로 읽는다.
    앱이 원본 핸들을 잡고 있으면 ①사용자 엑셀이 '읽기전용'으로 열리고
    ②OneDrive 동기화(업로드)가 막힌다(예전 최종본 읽기전용 사태의 원인).
    복사(수십 ms)는 공유읽기라 잠금을 만들지 않는다."""
    import shutil as _sh
    import tempfile as _tf
    tmp = os.path.join(_tf.gettempdir(), '_inv_snapshot.xlsx')
    _sh.copy2(xlsx_path, tmp)
    xls = pd.ExcelFile(tmp, engine='openpyxl')
    try:
        sheet_name = xls.sheet_names[3]
        df = xls.parse(sheet_name=sheet_name, header=5, dtype=str)
    finally:
        xls.close()                    # 핸들 즉시 반환 (temp 파일이지만 습관적으로)
    df = df.loc[:, ~df.columns.str.startswith('Unnamed')]
    df = df.dropna(how='all')
    first_col = df.columns[0]
    df = df[df[first_col].notna()]
    return df.fillna('')

def load_inventory_data():
    """재고 로드 — OneDrive 최종본 Excel 직접 읽기 (CSV fallback)."""
    import datetime as _dt

    # 1) OneDrive 직접 읽기
    xlsx_path = _find_inventory_xlsx()
    if xlsx_path:
        try:
            df = _parse_inventory_excel(xlsx_path)
            raw_n = len(df)
            if '품번' in df.columns and '외주업체' in df.columns and '현재고량' in df.columns:
                df = df.drop_duplicates(subset=['품번', '외주업체', '현재고량'], keep='first').reset_index(drop=True)
            dropped = raw_n - len(df)
            mtime_str = _dt.datetime.fromtimestamp(os.path.getmtime(xlsx_path)).strftime('%Y-%m-%d %H:%M')
            print(f"[재고일지] OneDrive 직접 로드: {xlsx_path}")
            print(f"  수정:{mtime_str} / {len(df)}행 × {len(df.columns)}열" + (f" (중복 {dropped}행 제거)" if dropped else ""))
            return df, xlsx_path
        except Exception as e:
            print(f"[재고일지] Excel 읽기 오류: {e}")

    # 2) CSV fallback
    inv_files = sorted(glob.glob(f'{DATA_DIR}/*_재고일지.csv'), reverse=True)
    if not inv_files:
        raise FileNotFoundError("재고 CSV 파일을 찾을 수 없습니다. convert_excel.py를 먼저 실행하세요.")
    latest_csv = inv_files[0]
    print(f"[재고 로드] CSV fallback: {latest_csv}")
    df = pd.read_csv(latest_csv, encoding='utf-8-sig', dtype=str).fillna('')
    raw_n = len(df)
    if '품번' in df.columns and '외주업체' in df.columns and '현재고량' in df.columns:
        df = df.drop_duplicates(subset=['품번', '외주업체', '현재고량'], keep='first').reset_index(drop=True)
    dropped = raw_n - len(df)
    if dropped:
        print(f"[재고 중복 제거] {dropped}행 제거")
    print(f"[재고 로드 완료] {len(df)}행 × {len(df.columns)}열")
    return df, latest_csv

def load_price_data():
    price_files = sorted(glob.glob(f'{DATA_DIR}/*_단가.csv'), reverse=True)
    if not price_files:
        print("[단가] CSV 없음 → convert_price.py 실행 필요")
        return None
    print(f"[단가 로드] {price_files[0]}")
    df = pd.read_csv(price_files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[단가 로드 완료] {len(df)}개 품번")
    return df

def load_spec_data():
    spec_files = sorted(glob.glob(f'{DATA_DIR}/*_부자재규격.csv'), reverse=True)
    if not spec_files:
        print("[부자재규격] CSV 없음 → convert_spec.py 실행 필요")
        return None
    print(f"[부자재규격 로드] {spec_files[0]}")
    df = pd.read_csv(spec_files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[부자재규격 로드 완료] {len(df)}개 항목")
    return df

JASA_FILENAME = '자사사용 부자재_REV.260224_지우철_1.xlsx'

# 자사재고 공유원본 (팀장 OneDrive 웹문서 — 공동편집되는 실시간 원본).
# 로컬의 카톡 수신 사본들은 받은 시점 스냅샷이라 낡음 → 공유링크에서 직접 내려받는다.
# Badger 토큰(OneDrive 웹클라이언트의 익명 토큰 발급) → shares API → content.
JASA_SHARE_URL = 'https://1drv.ms/x/c/56a6094b83e56550/IQCnPOEMzBFTRJwAjpls7NBuAf4ylB5KX7XEoBzyY0yD2XE?e=IBrWF9'
JASA_SHARED_LOCAL = None   # 초기화 후 f'{BASE_DIR}/자사사용 부자재_공유원본.xlsx'
_BADGER_APPID = '5cbed6ac-a083-4e14-b191-b4ba07653de2'   # OneDrive 웹 공개 appId


def _fetch_shared_jasa():
    """공유 링크에서 자사재고 원본을 내려받아 내용이 바뀌었을 때만 로컬 저장.
    성공 시 저장경로, 변경 없음/실패 시 None (기존 파일 fallback — 오프라인 무해)."""
    import base64 as _b64
    import hashlib as _hl
    import requests as _rq
    out = f'{BASE_DIR}/자사사용 부자재_공유원본.xlsx'
    try:
        s = _rq.Session()
        s.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        tok = s.post('https://api-badgerp.svc.ms/v1.0/token',
                     json={'appid': _BADGER_APPID}, timeout=20).json().get('token')
        if not tok:
            return None
        enc = _b64.urlsafe_b64encode(JASA_SHARE_URL.encode()).decode().rstrip('=')
        r = s.get(f'https://my.microsoftpersonalcontent.com/_api/v2.0/shares/u!{enc}/driveitem/content',
                  headers={'Authorization': 'Badger ' + tok, 'Prefer': 'autoredeem'},
                  timeout=90, allow_redirects=True)
        if r.status_code != 200 or r.content[:2] != b'PK' or len(r.content) < 50000:
            print(f'[자사재고 공유원본] 다운로드 실패 http={r.status_code} {len(r.content)}B')
            return None
        new_md5 = _hl.md5(r.content).hexdigest()
        if os.path.exists(out):
            with open(out, 'rb') as f:
                if _hl.md5(f.read()).hexdigest() == new_md5:
                    return None            # 내용 동일 — mtime 유지(불필요 리로드 방지)
        tmp = out + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(r.content)
        os.replace(tmp, out)
        print(f'[자사재고 공유원본] 갱신됨: {len(r.content):,}B md5={new_md5[:8]}')
        return out
    except Exception as e:
        print(f'[자사재고 공유원본] 오류(기존 파일 유지): {e!r:.120}')
        return None


def _find_jasa_xlsx():
    """OneDrive에서 자사재고 Excel을 패턴으로 찾아 최신 mtime 반환.

    ⚠️ 파일명 고정 매칭 금지 — 팀이 '..._팩판1.xlsx'처럼 새 이름으로 저장하면
    앱이 옛 파일을 계속 읽어 재고가 몇 달씩 고착됨(2026-09-01 A0088 사례:
    6/29 파일을 읽어 재고 0 표시, 실제 최신 파일은 8/19 '..._팩판1.xlsx').
    """
    # OneDrive 전체 재귀 — 공유문서 바로가기(Add shortcut to My files)가 어느 폴더에
    # 생겨도 자동 인식. 실편집 원본은 공유 OneDrive 웹문서라 로컬 사본만으론 낡음.
    patterns = [
        'C:/Users/jgkim/OneDrive/**/자사사용 부자재*.xlsx',
        f'{BASE_DIR}/자사사용 부자재*.xlsx',
    ]
    found = []
    for pat in patterns:
        for p in glob.glob(pat, recursive=True):
            base = os.path.basename(p)
            if base.startswith('~$') or '복사본' in base:   # 엑셀 임시/복사본 제외
                continue
            try:
                found.append((p, os.path.getmtime(p)))
            except OSError:
                pass
    if not found:
        return None
    return max(found, key=lambda x: x[1])[0]

def load_jasa_data():
    """자사재고 로드 — 공유 웹원본 다운로드 → 최신 Excel 읽기 (CSV fallback)"""
    # 0) 공유 웹원본(공동편집 실시간) 최신화 — 실패해도 기존 파일로 진행
    _fetch_shared_jasa()
    # 1) OneDrive 동기화 파일 직접 읽기
    best_path = _find_jasa_xlsx()
    xlsx_paths = [best_path] if best_path else []
    xlsx_paths += [f'{BASE_DIR}/자사사용 부자재_REV.260224_지우철_1.xlsx']
    for xlsx_path in xlsx_paths:
        if not os.path.exists(xlsx_path):
            continue
        try:
            df = pd.read_excel(xlsx_path, sheet_name=0, header=2,
                               dtype=str, engine='openpyxl')
            df = df.fillna('')
            df = df.drop(columns=[c for c in df.columns if str(c).startswith('Unnamed')],
                         errors='ignore')
            df = df[df['품번'].str.strip() != '']
            for col in df.columns:
                if df[col].dtype == object:
                    df[col] = df[col].str.strip()
            # 스키마 정규화 — 이후 코드가 위치 기반(columns[1]=품번, [7]=총재고)이라
            # '창고 위치' 컬럼이 없는 새 양식(팩판 등)은 앞에 빈 컬럼을 넣어 위치를 맞춘다.
            if len(df.columns) and df.columns[0] == '품번':
                df.insert(0, '창고 위치', '')
            if list(df.columns[1:2]) != ['품번'] or '총재고' not in df.columns:
                print(f"[자사재고] 스키마 이상 — 컬럼: {list(df.columns)[:9]} ({xlsx_path})")
                continue
            mtime = os.path.getmtime(xlsx_path)
            import datetime as _dt
            mtime_str = _dt.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
            print(f"[자사재고] OneDrive 직접 로드: {xlsx_path} ({len(df)}행, 수정:{mtime_str})")
            return df
        except Exception as e:
            print(f"[자사재고] Excel 읽기 오류 ({xlsx_path}): {e}")

    # 2) CSV fallback
    files = sorted(glob.glob(f'{DATA_DIR}/*_자사재고.csv'), reverse=True)
    if not files:
        print("[자사재고] OneDrive 파일 없음 + CSV 없음")
        return None
    print(f"[자사재고 로드] CSV fallback: {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[자사재고 로드 완료] {len(df)}행")
    return df

def load_order_data():
    files = sorted(glob.glob(f'{DATA_DIR}/*_발주정보.csv'), reverse=True)
    if not files:
        print("[발주정보] CSV 없음")
        return None
    print(f"[발주정보 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[발주정보 로드 완료] {len(df)}건")
    return df

def load_wp_order_data():
    files = sorted(glob.glob(f'{DATA_DIR}/*_외주발주정보.csv'), reverse=True)
    if not files:
        print("[외주발주정보] CSV 없음")
        return None
    print(f"[외주발주정보 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[외주발주정보 로드 완료] {len(df)}건")
    return df

def load_production_data():
    files = sorted(glob.glob(f'{DATA_DIR}/*_생산실적.csv'), reverse=True)
    if not files:
        print("[생산실적] CSV 없음")
        return None
    print(f"[생산실적 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[생산실적 로드 완료] {len(df)}건")
    return df

def load_shipment_data():
    files = sorted(glob.glob(f'{DATA_DIR}/*_출하정보.csv'), reverse=True)
    if not files:
        print("[출하정보] CSV 없음")
        return None
    print(f"[출하정보 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[출하정보 로드 완료] {len(df)}건")
    return df

def load_issue_data():
    files = sorted(glob.glob(f'{DATA_DIR}/*_출고정보.csv'), reverse=True)
    if not files:
        print("[출고정보] CSV 없음")
        return None
    print(f"[출고정보 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[출고정보 로드 완료] {len(df)}건")
    return df

def load_bom_data():
    files = sorted(glob.glob(f'{DATA_DIR}/*_BOM.csv'), reverse=True)
    if not files:
        print("[BOM] CSV 없음")
        return None
    print(f"[BOM 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[BOM 로드 완료] {len(df)}건 (모품번 {df['모품번'].nunique()}개)")
    return df

def load_rcv_data():
    files = sorted(glob.glob(f'{DATA_DIR}/*_입고정보.csv'), reverse=True)
    if not files:
        print("[입고정보] CSV 없음")
        return None
    print(f"[입고정보 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[입고정보 로드 완료] {len(df)}건")
    return df

def load_stock_data():
    """아마란스 현재고 (api20A02S01501) - E/G/H/I 자사창고 재고."""
    files = sorted(glob.glob(f'{DATA_DIR}/*_현재고.csv'), reverse=True)
    if not files:
        print("[현재고] CSV 없음")
        return None
    print(f"[현재고 로드] {files[0]}")
    df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
    print(f"[현재고 로드 완료] {len(df)}건")
    return df

DF, CSV_PATH = load_inventory_data()
PRICE_DF = load_price_data()
SPEC_DF = load_spec_data()
JASA_DF = load_jasa_data()
ORDER_DF = load_order_data()
WP_ORDER_DF = load_wp_order_data()
PROD_DF = load_production_data()

def load_work_order_data():
    files = sorted(glob.glob(f'{DATA_DIR}/*_생산지시.csv'), reverse=True)
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
STOCK_DF = load_stock_data()

SALES_SOURCE = {'kind': '', 'file': '', 'unmapped': []}   # 판매 자료 출처 (건강검진·패널 표기용)
SALES_DAILY_DF = None       # 판매 API 일자별 원자료 (품번 매핑 적용) — _load_sales_from_daily가 채움
_SALES_VEL_CACHE = {}
VEL_W28 = 0.7               # 판매속도 = 최근 4주 일평균×0.7 + 최근 3개월 일평균×0.3 (납품은 주 단위로 몰려 7일은 요동이 커서 4주 기준)


def _sales_velocity():
    """품번별 일 판매속도 (2026-09-23, 사용자 "판매속도를 최근 기준으로").
    납품(delivery, 낱개×환산계수) 기준 — 우리 재고가 실제로 빠져나가는 양.
    반환 {'end': 'YYYY-MM-DD', 'by': {품번: {'v': 일평균(가중), 'v28', 'v90', 'v14', 'v14p', 'trend': v28/v90}}} 또는 None(월간 CSV 모드)."""
    df = SALES_DAILY_DF
    if df is None or df.empty or SALES_SOURCE.get('kind') != 'api':
        return None
    key = id(df)
    if _SALES_VEL_CACHE.get('key') == key:
        return _SALES_VEL_CACHE['val']
    d = df[(df['ea'] > 0) & (df['code'] != '')]
    if d.empty:
        return None
    end = pd.to_datetime(d['date'].max())
    dt = pd.to_datetime(d['date'])

    def _win(days, offset=0):
        hi = end - pd.Timedelta(days=offset)
        lo = hi - pd.Timedelta(days=days - 1)
        return d[(dt >= lo) & (dt <= hi)].groupby('code')['ea'].sum() / days
    v28, v90, v14, v14p = _win(28), _win(90), _win(14), _win(14, 14)
    by = {}
    for c in set(v90.index) | set(v28.index):
        a, b = float(v28.get(c, 0)), float(v90.get(c, 0))
        v = VEL_W28 * a + (1 - VEL_W28) * b
        if v <= 0:
            continue
        by[c] = {'v': v, 'v28': a, 'v90': b, 'v14': float(v14.get(c, 0)), 'v14p': float(v14p.get(c, 0)),
                 'trend': round(min(2.0, max(0.5, a / b)), 2) if b > 0 else 1.0}
    val = {'end': end.strftime('%Y-%m-%d'), 'by': by}
    _SALES_VEL_CACHE.update(key=key, val=val)
    return val


def _vel_basis_label(vel):
    return f"최근 4주·3개월 납품 속도 (~{vel['end'][5:].replace('-', '/')})" if vel else ''


def _load_sales_from_daily():
    """온라인팀 파트너 API 일자별 자료(data/*_판매일별.csv, fetch_partner_sales.py) → 월별 판매 집계 (2026-09-23).
    - 수량 = delivery_qty(납품, 낱개 판매단위 — 기존 CSV의 판매박스수×박스당입수와 같은 단위, 7월 대조 확인)
    - 품번 = SKU매핑_확정.csv에 있는 SKU는 검증된 확정품번×환산계수(세트→단품 환산·곤약밥 신품번 등) 우선,
             없으면 API의 self_code(아마란스 품번) 그대로 (오프라인 EAN·신규 SKU 자동 편입)
    - 매출액 = supply_amount(공급가 합계, VAT 제외) — 온라인+오프라인 전 채널
    반환 DF [ym, code, ea, prefix, amt, pcls, ch] 또는 None."""
    files = sorted(glob.glob(f'{DATA_DIR}/*_판매일별.csv'))
    if not files:
        return None
    d = pd.read_csv(files[-1], dtype=str, encoding='utf-8-sig').fillna('')
    d['dq'] = pd.to_numeric(d['delivery_qty'], errors='coerce').fillna(0)
    raw_all = d
    d = d[d['dq'] > 0].copy()
    if d.empty:
        return None
    mp = pd.DataFrame(columns=['SKU', '확정품번', '환산계수', '패널분류'])
    mfile = f'{BASE_DIR}/SKU매핑_확정.csv'
    if os.path.exists(mfile):
        mp = pd.read_csv(mfile, dtype=str, encoding='utf-8-sig').fillna('')
    mp = mp.drop_duplicates('SKU').set_index('SKU')
    sku = d['sku'].astype(str).str.strip()
    mcode = sku.map(mp['확정품번']).fillna('').str.strip().str.upper()
    selfc = d['self_code'].astype(str).str.strip().str.upper()
    selfc = selfc.where(selfc.str.match(r'^[A-Z]\d{4}$'), '')
    d['code'] = mcode.where(mcode != '', selfc)
    f = pd.to_numeric(sku.map(mp['환산계수']), errors='coerce')
    d['f'] = f.where(mcode != '', 1.0).fillna(1.0)
    SALES_SOURCE['unmapped'] = (d[d['code'] == ''].groupby(['sku', 'name'])['dq'].sum()
                                .reset_index().sort_values('dq', ascending=False)
                                .rename(columns={'sku': 'SKU', 'name': '판매제품명', 'dq': '납품수량'}).to_dict('records'))
    d = d[d['code'] != ''].copy()
    d['ea'] = d['dq'] * d['f']
    d['amt'] = pd.to_numeric(d['supply_amount'], errors='coerce').fillna(0)
    d['ym'] = d['date'].astype(str).str.replace('-', '').str[:6]
    d['prefix'] = d['code'].str[:1]
    _pfx = {'G': '자사', 'H': '유상사급', 'I': '상품매입'}
    d['pcls'] = sku.loc[d.index].map(mp['패널분류']).fillna('').str.strip() if '패널분류' in mp.columns else ''
    d.loc[d['pcls'] == '', 'pcls'] = d.loc[d['pcls'] == '', 'prefix'].map(_pfx).fillna('기타')
    d['ch'] = d['channel_type'].astype(str)
    out = (d.groupby(['ym', 'code', 'prefix', 'pcls', 'ch'], as_index=False)[['ea', 'amt']].sum())
    # 일자별 원자료 보관 (2026-09-23): 판매속도(_sales_velocity)·채널 품절 경보(/api/channel_stock)용.
    # 납품 없는 날의 POS·점재고 행도 필요하므로 전체 행에 같은 품번 규칙을 적용해 둔다.
    ra = raw_all.copy()
    rs = ra['sku'].astype(str).str.strip()
    rm = rs.map(mp['확정품번']).fillna('').str.strip().str.upper()
    rself = ra['self_code'].astype(str).str.strip().str.upper()
    rself = rself.where(rself.str.match(r'^[A-Z]\d{4}$'), '')
    ra['code'] = rm.where(rm != '', rself)
    rf = pd.to_numeric(rs.map(mp['환산계수']), errors='coerce')
    ra['f'] = rf.where(rm != '', 1.0).fillna(1.0)
    ra['ea'] = ra['dq'] * ra['f']
    ra['pos'] = pd.to_numeric(ra['pos_qty'], errors='coerce')
    ra['stock'] = pd.to_numeric(ra['stock_qty'], errors='coerce')
    ra['amt'] = pd.to_numeric(ra['supply_amount'], errors='coerce').fillna(0)   # 공급가 합계(VAT 제외) = 매출 (2026-09-23 매출 전면 대체)
    globals()['SALES_DAILY_DF'] = ra[['date', 'channel', 'channel_name', 'channel_type', 'sku', 'name', 'category', 'code', 'f',
                                      'dq', 'ea', 'pos', 'stock', 'amt']].reset_index(drop=True)
    _SALES_VEL_CACHE.clear()
    SALES_SOURCE.update(kind='api', file=os.path.basename(files[-1]))
    print(f"[판매데이터 로드] API 일자별 {os.path.basename(files[-1])} - {len(d)}행 -> {out['code'].nunique()}품번, "
          f"{out['ym'].min()}~{out['ym'].max()}, 미매핑 SKU {len(SALES_SOURCE['unmapped'])}")
    return out[['ym', 'code', 'ea', 'prefix', 'amt', 'pcls', 'ch']].reset_index(drop=True)


def load_sales_data():
    """판매 집계 — ① 파트너 API 일자별 자료(있으면 우선, _load_sales_from_daily) ② 없으면 아래 월간 CSV.
    월간 판매수량(+공급가) + SKU매핑 → 완제품 판매 집계.
    - 판매 CSV: '*판매수량*.csv'∪'*공급가*.csv' 중 **수정시각 최신** 파일
      (이름 우선이면 공급가 없는 새 달 파일이 옛 공급가 파일에 가려짐)
    - 최신 파일에 공급가 컬럼이 없으면, 공급가 있는 최신 파일에서 SKU별 최근 공급가를 보완
    - 매핑 CSV: 'SKU매핑_확정.csv' (SKU→확정품번, 환산계수)
    - 낱개 = 판매박스수 × 박스당입수 × 환산계수
    - 매출액(amt) = 낱개 × 공급가 (공급가=낱개당 공급단가, 컬럼 있을 때만)
    반환 DF: [ym, code, ea, prefix, amt]. amt는 공급가 없으면 0. 없으면 None."""
    try:
        api_df = _load_sales_from_daily()
        if api_df is not None and not api_df.empty:
            return api_df
    except Exception as e:
        print(f'[판매데이터] API 일자별 로드 실패 -> 월간 CSV로 대체: {e!r:.150}')
    SALES_SOURCE.update(kind='csv', file='', unmapped=[])
    sfiles = sorted(set(glob.glob(f'{BASE_DIR}/*판매수량*.csv'))
                    | set(glob.glob(f'{BASE_DIR}/*공급가*.csv')),
                    key=os.path.getmtime)
    mfile = f'{BASE_DIR}/SKU매핑_확정.csv'
    if not sfiles or not os.path.exists(mfile):
        print("[판매데이터] 파일 없음 → 판매기반 재고경고 비활성 (발주기반 fallback)")
        return None
    try:
        s = pd.read_csv(sfiles[-1], dtype=str, encoding='utf-8-sig').fillna('')
        if '공급가' not in s.columns:
            # 공급가 미포함 파일 → 공급가 있는 최신 파일에서 SKU별 최근월 공급가 승계
            for pf in reversed(sfiles[:-1]):
                try:
                    p = pd.read_csv(pf, dtype=str, encoding='utf-8-sig').fillna('')
                except Exception:
                    continue
                if '공급가' not in p.columns:
                    continue
                p = p[pd.to_numeric(p['공급가'], errors='coerce').fillna(0) > 0]
                pm = (p.sort_values('판매월').groupby('SKU')['공급가'].last())
                s['공급가'] = s['SKU'].map(pm).fillna('')
                print(f"[판매데이터] 공급가 승계: {os.path.basename(pf)} → SKU {s['공급가'].ne('').sum()}행 적용")
                break
        mp = pd.read_csv(mfile, dtype=str, encoding='utf-8-sig').fillna('').set_index('SKU')
        s['box']  = pd.to_numeric(s['판매박스수'], errors='coerce')
        s['ipsu'] = pd.to_numeric(s['박스당입수'], errors='coerce')
        s['code'] = s['SKU'].map(mp['확정품번']).fillna('').str.strip().str.upper()
        s['f']    = pd.to_numeric(s['SKU'].map(mp['환산계수']), errors='coerce').fillna(1)
        s['ea']   = s['box'] * s['ipsu'] * s['f']   # ERP 수량(낱개×환산계수) — 재고경고/BOM용
        # 매출액 = SKU 판매수량(박스×입수) × 공급가.
        # ⚠️ 공급가는 SKU 판매단위(봉)당 단가라 환산계수(f)를 곱하면 안 됨(세트 중복계상 방지).
        if '공급가' in s.columns:
            s['price'] = pd.to_numeric(s['공급가'], errors='coerce').fillna(0)
            s['amt'] = s['box'] * s['ipsu'] * s['price']
        else:
            s['amt'] = 0.0
        s = s[(s['code'] != '') & s['ea'].notna() & (s['ea'] > 0)].copy()
        s['ym'] = s['판매월'].astype(str).str[:6]
        s['prefix'] = s['code'].str[:1]
        s['amt'] = s['amt'].fillna(0)
        # 패널분류: 매핑표 '패널분류' 지정값 우선, 없으면 접두사 기반(G자사/H유상사급/I상품매입)
        _pfx = {'G': '자사', 'H': '유상사급', 'I': '상품매입'}
        if '패널분류' in mp.columns:
            s['pcls'] = s['SKU'].map(mp['패널분류']).fillna('').str.strip()
        else:
            s['pcls'] = ''
        s.loc[s['pcls'] == '', 'pcls'] = s.loc[s['pcls'] == '', 'prefix'].map(_pfx).fillna('기타')
        has_amt = (s['amt'] > 0).any()
        SALES_SOURCE['file'] = os.path.basename(sfiles[-1])
        print(f"[판매데이터 로드] {os.path.basename(sfiles[-1])} - {len(s)}행, {s['code'].nunique()}품번"
              + (", 매출액 포함" if has_amt else ""))
        return s[['ym', 'code', 'ea', 'prefix', 'amt', 'pcls']].reset_index(drop=True)
    except Exception as e:
        print(f"[판매데이터] 로드 오류: {e}")
        return None

SALES_DF = load_sales_data()


# ====== 판매 CSV 드롭폴더 자동 반영 + 미매핑 SKU 감지 (2026-09-17, 수작업 목록 자동화) ======
# 팀장님(타부서)이 보내는 월간 판매 파일을 이 폴더에 저장만 하면 5분 안에 대시보드에 반영된다.
# 허용: .csv(utf-8/cp949) 또는 .xlsx(첫 시트). 필수 컬럼 판매월·SKU·제품명·판매박스수·박스당입수 (+공급가 선택).
SALES_DROP_DIR = 'C:/Users/jgkim/OneDrive/바탕 화면/구매/판매자료'
_SALES_DROP_STATE = f'{DATA_DIR}/_sales_drop_state.json'
_SALES_REQ_COLS = ['판매월', 'SKU', '제품명', '판매박스수', '박스당입수']
SALES_UNMAPPED_OUT = f'{BASE_DIR}/SKU매핑_미매핑_추천.csv'
SALES_DROP_STATUS = {'last_file': '', 'last_apply': '', 'message': '', 'unmapped': 0}


def _sales_latest_file():
    fs = sorted(set(glob.glob(f'{BASE_DIR}/*판매수량*.csv')) | set(glob.glob(f'{BASE_DIR}/*공급가*.csv')),
                key=os.path.getmtime)
    return fs[-1] if fs else None


_SKU_DECIDED_PREFIX = ('사용자확정', '수정(', '품번변경')


def _sku_review_data():
    """판매 SKU 품번 정리 대상 (2026-09-23, /sku_review 화면용).
    ① mismatch: SKU매핑_확정의 확정품번 ≠ 판매 API self_code 이고 아직 결정 안 된 것
       (결정됨 = 판정이 '사용자확정/수정(/품번변경'으로 시작, 또는 환산계수≠1 세트 환산)
    ② unmapped: 매핑표에도 없고 API self_code도 없는 SKU
    반환 {pending:[...], done:[...], codes:[{code,name,stock}], editable}"""
    files = sorted(glob.glob(f'{DATA_DIR}/*_판매일별.csv'))
    mfile = f'{BASE_DIR}/SKU매핑_확정.csv'
    out = {'pending': [], 'done': [], 'codes': [], 'editable': os.path.exists('C:/Users/jgkim/OneDrive')}
    if not files or not os.path.exists(mfile):
        return out
    d = pd.read_csv(files[-1], dtype=str, encoding='utf-8-sig').fillna('')
    d['dq'] = pd.to_numeric(d['delivery_qty'], errors='coerce').fillna(0)
    cut3 = (datetime.now() - _timedelta(days=92)).strftime('%Y-%m-%d')
    g = d.groupby('sku').agg(api_code=('self_code', lambda s: next((x for x in s if x), '')),
                             api_name=('name', 'first'), ch=('channel_name', lambda s: ', '.join(sorted(set(s))[:3])),
                             total=('dq', 'sum'), last=('date', lambda s: max(s))).reset_index()
    r3 = d[d['date'] >= cut3].groupby('sku')['dq'].sum()
    g['recent3'] = g['sku'].map(r3).fillna(0)
    g = g.set_index('sku')
    names = _sales_name_map()
    stock = {}
    if STOCK_DF is not None and not STOCK_DF.empty and '품번' in STOCK_DF.columns:
        for _, r in STOCK_DF.iterrows():
            c = str(r.get('품번', '')).strip().upper()
            stock[c] = stock.get(c, 0) + _num(r.get('현재고', 0))
            if c and c not in names:
                names[c] = str(r.get('품명', '')).strip()

    def _info(c):
        c = (c or '').strip().upper()
        return {'code': c, 'name': names.get(c, ''), 'stock': int(stock.get(c, 0))} if c else None

    mp = pd.read_csv(mfile, dtype=str, encoding='utf-8-sig').fillna('')
    mp_skus = set(mp['SKU'].astype(str).str.strip())
    for _, r in mp.iterrows():
        sku = str(r['SKU']).strip()
        if sku not in g.index:
            continue
        a = g.loc[sku]
        mcode, acode = str(r['확정품번']).strip().upper(), str(a['api_code']).strip().upper()
        if mcode == acode or not acode:      # API 품번이 비어 있으면 매핑표가 채워주는 것 — 충돌 아님
            continue
        rec = {'sku': sku, 'type': 'mismatch', 'sale_name': str(r.get('판매제품명', '')) or a['api_name'],
               'api_name': a['api_name'], 'channels': a['ch'], 'recent3': int(a['recent3']), 'total': int(a['total']),
               'last': a['last'], 'factor': str(r.get('환산계수', '')).strip() or '1',
               'map': _info(mcode), 'api': _info(acode), 'decision': str(r.get('판정', ''))}
        decided = rec['decision'].startswith(_SKU_DECIDED_PREFIX) or rec['factor'] not in ('1', '1.0', '')
        (out['done'] if decided else out['pending']).append(rec)
    for sku, a in g.iterrows():
        if sku in mp_skus or str(a['api_code']).strip() or a['total'] <= 0:   # 납품 이력 없는 SKU는 계산 영향 없음
            continue
        out['pending'].append({'sku': sku, 'type': 'unmapped', 'sale_name': a['api_name'], 'api_name': a['api_name'],
                               'channels': a['ch'], 'recent3': int(a['recent3']), 'total': int(a['total']), 'last': a['last'],
                               'factor': '1', 'map': None, 'api': None, 'decision': ''})
    out['pending'].sort(key=lambda x: (-x['recent3'], -x['total']))
    out['codes'] = sorted(({'code': c, 'name': n, 'stock': int(stock.get(c, 0))} for c, n in names.items()
                           if c[:1] in ('G', 'H', 'I') and n), key=lambda x: x['code'])
    return out


@app.route('/api/sku_review', methods=['GET'])
def api_sku_review():
    return jsonify(_sku_review_data())


@app.route('/api/sku_review/resolve', methods=['POST'])
def api_sku_review_resolve():
    """body {sku, action: keep|api|custom, code}. 매핑표(SKU매핑_확정.csv)에 기록 → 판매 집계 즉시 재계산.
    로컬(호스트)에서만 — 매핑표는 로컬→GCP 30분 동기화라 GCP에서 고치면 다음 동기화에 덮여 사라짐."""
    if not os.path.exists('C:/Users/jgkim/OneDrive'):
        return jsonify({'ok': False, 'error': '품번 정리는 호스트 대시보드(localhost:5000)에서 해주세요. GCP에서 고치면 다음 동기화에 덮어써집니다.'}), 400
    b = request.get_json(silent=True) or {}
    sku = str(b.get('sku', '')).strip()
    action = str(b.get('action', '')).strip()
    code = str(b.get('code', '')).strip().upper()
    if not sku or action not in ('keep', 'api', 'custom'):
        return jsonify({'ok': False, 'error': '잘못된 요청'}), 400
    if action in ('api', 'custom') and not re.fullmatch(r'[A-Z]\d{4}', code):
        return jsonify({'ok': False, 'error': f'품번 형식 오류: {code or "(빈 값)"}'}), 400
    import shutil as _sh
    mfile = f'{BASE_DIR}/SKU매핑_확정.csv'
    bak = f'{mfile}.bak_review_{datetime.now():%Y%m%d}'
    if not os.path.exists(bak):
        _sh.copy2(mfile, bak)
    mp = pd.read_csv(mfile, dtype=str, encoding='utf-8-sig').fillna('')
    tag = f"사용자확정({datetime.now():%Y-%m-%d} {'매핑표 유지' if action == 'keep' else ('API 품번' if action == 'api' else '직접 입력')})"
    sel = mp['SKU'].astype(str).str.strip() == sku
    if sel.any():
        if action != 'keep':
            mp.loc[sel, '확정품번'] = code
            mp.loc[sel, '환산계수'] = '1'
            if '분류' in mp.columns:
                mp.loc[sel, '분류'] = code[:1]
        if '판정' not in mp.columns:
            mp['판정'] = ''
        mp.loc[sel, '판정'] = tag
    else:
        if action == 'keep':
            return jsonify({'ok': False, 'error': '매핑표에 없는 SKU — 품번을 입력하세요'}), 400
        files = sorted(glob.glob(f'{DATA_DIR}/*_판매일별.csv'))
        nm = ''
        if files:
            d = pd.read_csv(files[-1], dtype=str, encoding='utf-8-sig').fillna('')
            m = d[d['sku'].astype(str) == sku]
            nm = m['name'].iloc[0] if len(m) else ''
        row = {c: '' for c in mp.columns}
        row.update({'SKU': sku, '판매제품명': nm, '확정품번': code, '환산계수': '1', '판정': tag})
        if '분류' in mp.columns:
            row['분류'] = code[:1]
        mp = pd.concat([mp, pd.DataFrame([row])], ignore_index=True)
    mp.to_csv(mfile, index=False, encoding='utf-8-sig')
    globals()['SALES_DF'] = load_sales_data()
    _API_CACHE.clear()
    print(f'[SKU 품번 정리] {sku} -> {action} {code}')
    return jsonify({'ok': True, 'sku': sku, 'action': action, 'code': code})


@app.route('/sku_review', methods=['GET'])
def sku_review_page():
    return Response(SKU_REVIEW_TEMPLATE, mimetype='text/html')


SKU_REVIEW_TEMPLATE = r'''<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>판매 SKU 품번 정리</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#f4f6fb;--card:#fff;--bd:#e2e8f0;--tx:#0f172a;--t2:#475569;--t3:#94a3b8;--pri:#4f46e5;--ok:#047857;--warn:#b45309}
*{box-sizing:border-box}body{margin:0;font-family:'Noto Sans KR',sans-serif;background:var(--bg);color:var(--tx)}
.wrap{max-width:880px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:19px;margin:0 0 4px}.sub{font-size:12.5px;color:var(--t2);margin-bottom:14px;line-height:1.6}
.bar{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.prog{flex:1;min-width:160px;height:8px;background:#e2e8f0;border-radius:6px;overflow:hidden}.prog>i{display:block;height:100%;background:var(--pri)}
.cnt{font-size:13px;font-weight:700}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:16px;margin-bottom:12px;box-shadow:0 1px 3px rgba(15,23,42,.05)}
.card.cur{border-color:var(--pri);box-shadow:0 0 0 3px #e0e7ff}
.hd{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
.nm{font-size:15px;font-weight:700;line-height:1.4}.meta{font-size:11.5px;color:var(--t3);margin-top:3px}
.q{text-align:right;font-size:11px;color:var(--t2);white-space:nowrap}.q b{display:block;font-size:17px;color:var(--tx)}
.opts{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
.opt{border:1.5px solid var(--bd);border-radius:10px;padding:10px 12px;cursor:pointer;background:#fff;text-align:left;font:inherit;color:inherit}
.opt:hover{border-color:var(--pri);background:#f5f7ff}.opt .lb{font-size:10.5px;font-weight:700;color:var(--t3)}
.opt .cd{font-size:15px;font-weight:700;margin-top:2px}.opt .pn{font-size:12.5px;margin-top:2px;line-height:1.4}.opt .st{font-size:11px;color:var(--t2);margin-top:3px}
.opt[disabled]{opacity:.45;cursor:not-allowed;background:#f8fafc}
.cus{display:flex;gap:8px;margin-top:10px;align-items:center;flex-wrap:wrap}
.cus input{flex:1;min-width:200px;padding:8px 10px;border:1px solid var(--bd);border-radius:8px;font:inherit;font-size:13px}
.btn{padding:8px 14px;border-radius:8px;border:1px solid var(--pri);background:var(--pri);color:#fff;font-weight:700;font-size:12.5px;cursor:pointer}
.btn.gh{background:#fff;color:var(--t2);border-color:var(--bd)}
.tag{display:inline-block;font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:6px;background:#fef3c7;color:var(--warn);margin-left:6px}
.done{font-size:12px;color:var(--t2);border-top:1px solid var(--bd);padding:8px 2px;display:flex;justify-content:space-between;gap:8px}
.note{background:#fffbeb;border:1px solid #fcd34d;color:#92400e;border-radius:10px;padding:10px 12px;font-size:12.5px;margin-bottom:12px}
.empty{text-align:center;padding:40px 10px;color:var(--ok);font-weight:700}
.msg{font-size:12px;margin-top:8px;color:#dc2626}
details summary{cursor:pointer;font-size:13px;font-weight:700;color:var(--t2);margin:18px 0 6px}
@media (max-width:600px){.opts{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<h1>🧩 판매 SKU 품번 정리</h1>
<div class="sub">판매 자료(온라인팀 API)의 SKU가 어느 아마란스 품번인지 정합니다. 카드마다 <b>현재 매핑표 품번</b>과 <b>온라인팀이 준 품번</b>을 비교해 맞는 쪽을 누르세요. 맞는 게 없으면 품번을 직접 입력합니다. 누르는 즉시 매핑표에 저장되고 판매·재고 계산에 반영됩니다.</div>
<div id="note"></div>
<div class="bar"><span class="cnt" id="cnt"></span><div class="prog"><i id="pg" style="width:0"></i></div><button class="btn gh" onclick="load()">새로고침</button></div>
<div id="list"></div>
<details id="donebox"><summary id="donesum"></summary><div id="done"></div></details>
<datalist id="codes"></datalist>
</div>
<script>
let D = null, total0 = 0;
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function fmt(n){return Number(n||0).toLocaleString('ko-KR');}
async function load(){
  D = await (await fetch('/api/sku_review')).json();
  if (!total0) total0 = D.pending.length + 0;
  document.getElementById('note').innerHTML = D.editable ? '' :
    '<div class="note">여기는 GCP 서버입니다. 품번 정리는 <b>호스트 대시보드(localhost:5000)</b>에서 해주세요. 여기서 고치면 다음 동기화에 덮어써집니다.</div>';
  document.getElementById('codes').innerHTML = D.codes.map(c=>'<option value="'+esc(c.code)+'">'+esc(c.name)+' · 재고 '+fmt(c.stock)+'</option>').join('');
  render();
}
function optHtml(label, info, sku, action){
  if (!info) return '<button class="opt" disabled><div class="lb">'+label+'</div><div class="cd">없음</div><div class="pn">품번이 지정되지 않았습니다</div></button>';
  return '<button class="opt" onclick="resolve(\''+esc(sku)+'\',\''+action+'\',\''+esc(info.code)+'\')">'
    + '<div class="lb">'+label+'</div><div class="cd">'+esc(info.code)+'</div>'
    + '<div class="pn">'+esc(info.name||'(아마란스 품명 없음)')+'</div><div class="st">아마란스 재고 '+fmt(info.stock)+'</div></button>';
}
function render(){
  const P = D.pending, n = P.length, doneN = Math.max(total0 - n, 0);
  document.getElementById('cnt').textContent = n ? ('남은 '+n+'건') : '모두 정리됨';
  document.getElementById('pg').style.width = (total0 ? Math.round(doneN/total0*100) : 100) + '%';
  const L = document.getElementById('list');
  if (!n) { L.innerHTML = '<div class="card empty">✓ 확인할 SKU가 없습니다</div>'; }
  else L.innerHTML = P.map((it,i)=>{
    const um = it.type==='unmapped';
    return '<div class="card'+(i===0?' cur':'')+'" id="c_'+esc(it.sku)+'">'
      + '<div class="hd"><div><div class="nm">'+esc(it.sale_name)+(um?'<span class="tag">품번 없음</span>':'')+'</div>'
      + '<div class="meta">SKU '+esc(it.sku)+' · '+esc(it.channels)+' · 최근 납품 '+esc(it.last)+'</div></div>'
      + '<div class="q">최근 3개월 납품<b>'+fmt(it.recent3)+'</b>누적 '+fmt(it.total)+'</div></div>'
      + '<div class="opts">'+optHtml('현재 매핑표', it.map, it.sku, 'keep')+optHtml('온라인팀 API', it.api, it.sku, 'api')+'</div>'
      + '<div class="cus"><input list="codes" id="in_'+esc(it.sku)+'" placeholder="둘 다 아니면 품번 입력 (예: G0193) — 목록에서 선택 가능">'
      + '<button class="btn" onclick="custom(\''+esc(it.sku)+'\')">이 품번으로 저장</button></div>'
      + '<div class="msg" id="m_'+esc(it.sku)+'"></div></div>';
  }).join('');
  const DN = D.done;
  document.getElementById('donesum').textContent = '정리 완료 · 의도된 차이 ('+DN.length+'건)';
  document.getElementById('done').innerHTML = DN.map(it=>'<div class="done"><span>'+esc(it.sale_name)+' <span style="color:#94a3b8">SKU '+esc(it.sku)+'</span></span>'
    + '<span><b>'+esc(it.map?it.map.code:'')+'</b> '+(it.factor!=='1'?'×'+esc(it.factor)+' ':'')+'<span style="color:#94a3b8">'+esc(it.decision||'세트 환산')+'</span></span></div>').join('');
}
async function resolve(sku, action, code){
  const m = document.getElementById('m_'+sku);
  try {
    const r = await fetch('/api/sku_review/resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sku,action,code})});
    const d = await r.json();
    if (!d.ok) { m.textContent = d.error || '저장 실패'; return; }
    await load();
  } catch(e){ m.textContent = '오류: '+e.message; }
}
function custom(sku){
  const v = (document.getElementById('in_'+sku).value||'').trim().toUpperCase().split(/\s/)[0];
  if (!/^[A-Z][0-9]{4}$/.test(v)) { document.getElementById('m_'+sku).textContent = '품번 형식이 아닙니다 (예: G0193)'; return; }
  resolve(sku, 'custom', v);
}
load();
</script></body></html>'''


def _sales_unmapped(with_suggest=False):
    """최신 판매 CSV의 SKU 중 SKU매핑_확정.csv에 없는 것.
    with_suggest=True면 map_sku.py의 이름 유사도 매칭으로 추천 품번을 붙여 SALES_UNMAPPED_OUT에 저장(사람은 확정품번만 채우면 됨)."""
    if SALES_SOURCE.get('kind') == 'api':
        # API 자료는 self_code(아마란스 품번)가 있어 매핑표 없이도 연결됨 → 자사코드가 비어 있는 SKU만 미매핑
        return list(SALES_SOURCE.get('unmapped') or [])
    sf, mfile = _sales_latest_file(), f'{BASE_DIR}/SKU매핑_확정.csv'
    if not sf or not os.path.exists(mfile):
        return []
    s = pd.read_csv(sf, dtype=str, encoding='utf-8-sig').fillna('')
    mp = pd.read_csv(mfile, dtype=str, encoding='utf-8-sig').fillna('')
    if 'SKU' not in s.columns:
        return []
    known = set(mp['SKU'].astype(str).str.strip())
    um = s[~s['SKU'].astype(str).str.strip().isin(known)].drop_duplicates('SKU')
    rows = [{'SKU': str(r['SKU']).strip(), '판매제품명': str(r.get('제품명', '')).strip()} for _, r in um.iterrows()]
    if not rows or not with_suggest:
        return rows
    try:
        import map_sku as _ms
        _ms.latest = lambda t: (sorted(glob.glob(f'{DATA_DIR}/*_{t}.csv')) or [None])[-1]   # cwd 무관하게 data/ 절대경로
        master = _ms.load_master()
        master['n'] = master['품명'].map(_ms.norm)
        master['tok'] = master['품명'].map(_ms.size_tokens)
        for r in rows:
            n, tok = _ms.norm(r['판매제품명']), _ms.size_tokens(r['판매제품명'])
            cands = []
            for _, m in master.iterrows():
                sc = _ms.score(n, m['n']); sv = _ms.size_verdict(tok, m['tok'])
                cands.append((sc + (0.15 if sv == 'match' else (-0.35 if sv == 'conflict' else 0.0)), sc, sv, m))
            cands.sort(key=lambda x: -x[0])
            if cands:
                _, raw, sv, best = cands[0]
                r.update({'추천품번': best['품번'], '추천품명': best['품명'], '유사도': round(raw, 3), '용량판정': sv,
                          '판정': '자동확정' if (raw >= 0.80 and sv == 'match') else ('실패' if (sv == 'conflict' or raw < 0.55) else '검토필요'),
                          '확정품번': best['품번'] if (raw >= 0.80 and sv == 'match') else '', '환산계수': '1'})
                for i, (_, sc2, sv2, m2) in enumerate(cands[:3], 1):
                    r[f'후보{i}'] = f"{m2['품번']} | {m2['품명']} | sim={sc2:.2f} size={sv2}"
        pd.DataFrame(rows).to_csv(SALES_UNMAPPED_OUT, index=False, encoding='utf-8-sig')
    except Exception as e:
        print(f'[판매 SKU매핑] 추천 생성 실패(목록만): {e!r:.150}')
    return rows


def _sales_drop_apply(src):
    """드롭폴더 파일 검증 → 프로젝트 루트에 '월간판매수량_자동_…csv'로 저장 → SALES_DF 리로드 → 미매핑 SKU 점검."""
    base = os.path.basename(src)
    if base.lower().endswith('.xlsx'):
        df = pd.read_excel(src, dtype=str).fillna('')
    else:
        try:
            df = pd.read_csv(src, dtype=str, encoding='utf-8-sig').fillna('')
        except UnicodeDecodeError:
            df = pd.read_csv(src, dtype=str, encoding='cp949').fillna('')
    df.columns = [str(c).strip() for c in df.columns]
    miss = [c for c in _SALES_REQ_COLS if c not in df.columns]
    if miss:
        raise ValueError(f'필수 컬럼 없음: {", ".join(miss)} (있는 컬럼: {", ".join(df.columns[:8])})')
    stem = re.sub(r'[^0-9A-Za-z가-힣_]+', '_', os.path.splitext(base)[0])[:40]
    dst = f'{BASE_DIR}/월간판매수량_자동_{datetime.now():%Y%m%d}_{stem}.csv'
    df.to_csv(dst, index=False, encoding='utf-8-sig')
    globals()['SALES_DF'] = load_sales_data()
    _API_CACHE.clear()
    um = _sales_unmapped(with_suggest=True)
    SALES_DROP_STATUS.update(last_file=base, last_apply=datetime.now().strftime('%m-%d %H:%M'), unmapped=len(um),
                             message=f'{base} 반영 ({len(df)}행, 미매핑 SKU {len(um)}건)')
    return dst, len(df), um


def _sales_drop_watcher():
    threading.Event().wait(60)
    while True:
        try:
            st = {}
            if os.path.exists(_SALES_DROP_STATE):
                st = json.load(open(_SALES_DROP_STATE, encoding='utf-8'))
            done = st.setdefault('done', {})
            if os.path.isdir(SALES_DROP_DIR):
                files = [f for f in glob.glob(f'{SALES_DROP_DIR}/**/*', recursive=True)
                         if f.lower().endswith(('.csv', '.xlsx')) and not os.path.basename(f).startswith('~$')]
                for f in sorted(files, key=os.path.getmtime):
                    key = f'{os.path.abspath(f)}|{int(os.path.getmtime(f))}'
                    if key in done:
                        continue
                    if time.time() - os.path.getmtime(f) < 90:   # OneDrive 동기화/저장 중인 파일은 다음 주기에
                        continue
                    try:
                        dst, n, um = _sales_drop_apply(f)
                        done[key] = {'ts': datetime.now().strftime('%Y-%m-%d %H:%M'), 'dst': os.path.basename(dst), 'rows': n}
                        print(f'[판매자료] 드롭폴더 반영: {os.path.basename(f)} → {os.path.basename(dst)} ({n}행, 미매핑 {len(um)})')
                        body = f'{os.path.basename(f)} → {n}행 반영'
                        if um:
                            body += f'\n미매핑 SKU {len(um)}건 → SKU매핑_미매핑_추천.csv 에서 확정품번 채운 뒤 SKU매핑_확정.csv에 추가\n' + \
                                    '\n'.join(f"· {u['SKU']} {u['판매제품명'][:30]} → {u.get('추천품번', '?')} ({u.get('판정', '')})" for u in um[:10])
                        _notify('warn' if um else 'info', 'sales', '📥 판매 자료 자동 반영', body)
                    except Exception as e:
                        done[key] = {'ts': datetime.now().strftime('%Y-%m-%d %H:%M'), 'error': str(e)[:200]}
                        print(f'[판매자료] 반영 실패 {os.path.basename(f)}: {e!r:.200}')
                        _notify('error', 'sales', '📥 판매 자료 반영 실패', f'{os.path.basename(f)}: {str(e)[:200]}')
                json.dump(st, open(_SALES_DROP_STATE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        except Exception as e:
            print(f'[판매자료 감시] 오류: {e!r:.160}')
        threading.Event().wait(5 * 60)


_PARTNER_SALES_STATUS = {'last_run': '', 'last_ok': '', 'message': ''}


def _partner_sales_fetch_once():
    """fetch_partner_sales.py 실행 → 성공 시 SALES_DF 리로드."""
    import subprocess as _sp
    r = _sp.run([sys.executable, os.path.join(BASE_DIR, 'fetch_partner_sales.py')], capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=900, cwd=BASE_DIR)
    tail = ((r.stdout or '') + (r.stderr or '')).strip().splitlines()[-1:] or ['']
    _PARTNER_SALES_STATUS.update(last_run=datetime.now().strftime('%Y-%m-%d %H:%M'), message=tail[0][:200])
    if r.returncode == 0:
        globals()['SALES_DF'] = load_sales_data()
        _API_CACHE.clear()
        _PARTNER_SALES_STATUS['last_ok'] = _PARTNER_SALES_STATUS['last_run']
    print(f'[판매 API] {tail[0][:160]}')
    return r.returncode == 0


def _partner_sales_loop():
    """하루 2회(09:10 이후 1회, 14:00 이후 1회) — 온라인팀 안내: 전날 자료는 09시 이후, 하루 1~2회 호출."""
    threading.Event().wait(90)
    while True:
        try:
            now = datetime.now()
            today = now.strftime('%Y%m%d')
            has_today = bool(glob.glob(f'{DATA_DIR}/{today}_판매일별.csv'))
            last = _PARTNER_SALES_STATUS.get('last_ok') or ''
            due = (now.hour * 60 + now.minute >= 9 * 60 + 10 and not has_today) or \
                  (now.hour >= 14 and has_today and not (last.startswith(now.strftime('%Y-%m-%d')) and last[11:13] >= '14')
                   and os.path.getmtime(glob.glob(f'{DATA_DIR}/{today}_판매일별.csv')[0]) < now.replace(hour=14, minute=0).timestamp())
            if due:
                _partner_sales_fetch_once()
        except Exception as e:
            print(f'[판매 API] 오류: {e!r:.160}')
        threading.Event().wait(15 * 60)


def _start_partner_sales_loop():
    t = threading.Thread(target=_partner_sales_loop, daemon=True, name='partner-sales')
    t.start()
    print('[판매 API] 일자별 판매 수집 스케줄 시작 (09:10 이후·14:00 이후 하루 2회)')


def _start_sales_drop_watcher():
    try:
        os.makedirs(SALES_DROP_DIR, exist_ok=True)
    except OSError:
        pass
    t = threading.Thread(target=_sales_drop_watcher, daemon=True, name='sales-drop')
    t.start()
    print(f'[판매자료] 드롭폴더 감시 시작 (5분 주기): {SALES_DROP_DIR}')

def load_sale_price_data():
    """판매단가(아마란스 기준단가) — data/*_판매단가.csv → {품번: 판매단가}.
    월 판매기반 자료 패널의 매출액 산출용. 없으면 {} (매출액 0 처리)."""
    files = sorted(glob.glob(f'{DATA_DIR}/*_판매단가.csv'), reverse=True)
    if not files:
        print("[판매단가] CSV 없음 → 매출액 미산출")
        return {}
    try:
        df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
        out = {}
        for _, r in df.iterrows():
            c = str(r.get('품번', '')).strip().upper()
            v = pd.to_numeric(pd.Series([r.get('판매단가', 0)]), errors='coerce').iloc[0]
            if c and pd.notna(v) and v > 0:
                out[c] = float(v)
        print(f"[판매단가 로드] {os.path.basename(files[0])} - {len(out)}건")
        return out
    except Exception as e:
        print(f"[판매단가] 로드 오류: {e}")
        return {}

SALE_PRICE = load_sale_price_data()


def load_purchase_price_data():
    """아마란스 품목 매입단가(purchUm) — *_판매단가.csv의 '매입단가' 컬럼(fetch_sale_price가 함께 저장) → {품번: 매입단가}.
    2026-09-11: 단가 자동화 2순위 원천 (1순위 최신 발주단가, 3순위 구매팀 엑셀)."""
    files = sorted(glob.glob(f'{DATA_DIR}/*_판매단가.csv'), reverse=True)
    if not files:
        return {}
    try:
        df = pd.read_csv(files[0], encoding='utf-8-sig', dtype=str).fillna('')
        if '매입단가' not in df.columns:
            return {}
        v = pd.to_numeric(df['매입단가'], errors='coerce').fillna(0)
        out = {str(c).strip().upper(): float(p) for c, p in zip(df['품번'], v) if str(c).strip() and p > 0}
        print(f"[매입단가 로드] {os.path.basename(files[0])} - {len(out)}건")
        return out
    except Exception as e:
        print(f"[매입단가] 로드 오류: {e}")
        return {}


PURCH_PRICE = load_purchase_price_data()

# Monday.com 데이터 로드
def load_monday_data():
    mon_files = sorted(glob.glob(f'{DATA_DIR}/*_monday.csv'), reverse=True)
    if not mon_files:
        print("[Monday] CSV 없음")
        return None
    print(f"[Monday 로드] {mon_files[0]}")
    _mon_keep = {'보드ID','보드명','아이템ID','아이템명','그룹','생성일','수정일',
                 '재질','단가(원)','품번','품명','사이즈','MOQ','중량(g)','구분',
                 '요청수량','발주 요청일','업체 발주일','입고 요청일','실발주 수량','발주업체','입고처','업데이트',
                 '입항 일정','입항 물량','수입원','상태','선적 일정','실제 입고일',
                 '업체명','요청 입고지','요청 입고일','외주 요청일','외주 출고일','발주상태',
                 '시방서 첨부','고객사','발주 상태','요청자','담당자',
                 '금액','공급가액','구분','부서','진행상태',
                 '채널','업무 상태','목표 출시일','카테고리','우선순위',
                 '입항일','계약번호','부모아이템ID',
                 'box 입수량','PT적재량'}  # 입수 테스트(3D) 패널용
    df = pd.read_csv(mon_files[0], encoding='utf-8-sig', dtype=str,
                     usecols=lambda c: c in _mon_keep)
    df = df.fillna('')
    print(f"[Monday 로드 완료] {len(df)}건, 보드 {df['보드명'].nunique()}개")
    return df

MONDAY_DF = load_monday_data()


def _spec_from_monday():
    """챗봇 규격 조회용 SPEC_DF를 Monday '부자재 규격' 보드에서 생성 (2026-09-11).
    기존 *_부자재규격.csv(convert_spec, OneDrive 엑셀)는 6/9 이후 갱신이 없어 3개월 고착 →
    대시보드와 같은 Monday 보드를 원천으로 통일. 컬럼명은 _build_spec_lookup이 쓰는 이름으로 맞춤."""
    if MONDAY_DF is None or MONDAY_DF.empty or '보드명' not in MONDAY_DF.columns:
        return None
    s = MONDAY_DF[MONDAY_DF['보드명'] == '부자재 규격']
    if s.empty:
        return None
    g = lambda c: s[c].astype(str) if c in s.columns else pd.Series([''] * len(s), index=s.index)
    df = pd.DataFrame({'외주업체명': g('아이템명'), '품번': g('품번'), '품명': g('품명'), '규격(사이즈)': g('사이즈'),
                       '재질': g('재질'), 'MOQ': g('MOQ'), '단가(원)': g('단가(원)'), '중량(g)': g('중량(g)'),
                       '납품처': g('구분'), '카테고리': g('그룹')}).fillna('')
    df = df[df['품번'].str.strip() != ''].reset_index(drop=True)
    return df if not df.empty else None


_spec_m = _spec_from_monday()
if _spec_m is not None:
    SPEC_DF = _spec_m
    print(f"[부자재규격] Monday 보드 기준으로 교체: {len(SPEC_DF)}건")


# ====== API 응답 캐시 (2026-09-04) ======
# 무거운 집계 API(거래처 스코어·수급 플래너·발주 타이밍 등)가 첫 로딩에 10초+ 걸리던 문제.
# 데이터는 30분~1시간 단위로만 바뀌므로 "DF 객체 지문 + 데이터 버전"이 같으면 캐시 응답.
# DF가 리로드되면(객체 교체) 지문이 바뀌어 자동 무효화 → 리로드 지점마다 손댈 필요 없음.
_API_CACHE = {}
_API_CACHE_LOCK = threading.Lock()
_API_CACHE_DFS = ('DF', 'PRICE_DF', 'SPEC_DF', 'JASA_DF', 'ORDER_DF', 'WP_ORDER_DF', 'PROD_DF', 'WO_DF',
                  'SHIP_DF', 'ISSUE_DF', 'BOM_DF', 'RCV_DF', 'STOCK_DF', 'SALES_DF', 'MONDAY_DF')


def _df_fingerprint():
    g = globals()
    fp = []
    for n in _API_CACHE_DFS:
        v = g.get(n)
        fp.append((id(v), len(v) if hasattr(v, '__len__') else 0))
    fp.append(g.get('_DATA_VERSION'))
    fp.append(datetime.now().strftime('%Y%m%d'))   # 날짜 기반 계산(소진일 등)은 하루 단위로 갱신
    return tuple(fp)


def cached_api(ttl=900):
    """GET JSON 응답을 (엔드포인트+쿼리, DF 지문) 키로 ttl초 캐시. 비-200/비-JSON은 캐시 안 함."""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            if request.method != 'GET':
                return fn(*a, **kw)
            key = (fn.__name__, request.full_path)
            fp = _df_fingerprint()
            now = time.time()
            with _API_CACHE_LOCK:
                ent = _API_CACHE.get(key)
            if ent and ent[0] == fp and now - ent[1] < ttl:
                body, mt = ent[2]
                r = app.response_class(body, status=200, mimetype=mt)
                r.headers['X-Cache'] = 'HIT'
                return r
            resp = fn(*a, **kw)
            r, status = (resp[0], resp[1]) if isinstance(resp, tuple) else (resp, 200)
            try:
                if status == 200 and getattr(r, 'mimetype', '') == 'application/json':
                    with _API_CACHE_LOCK:
                        _API_CACHE[key] = (fp, now, (r.get_data(), r.mimetype))
                        if len(_API_CACHE) > 500:
                            _API_CACHE.clear()
            except Exception:
                pass
            return resp
        return wrapper
    return deco


@app.route('/api/cache_clear', methods=['POST'])
def api_cache_clear():
    with _API_CACHE_LOCK:
        n = len(_API_CACHE)
        _API_CACHE.clear()
    return jsonify({'ok': True, 'cleared': n})


# ====== Monday 자동 갱신 (시작시 1회 + 30분 주기 폴링) ======
MONDAY_REFRESH_LOCK = threading.Lock()
MONDAY_REFRESH_STATUS = {
    'running': False,
    'last_run': None,
    'last_status': None,   # 'success' | 'error' | None
    'message': '',
}
_MONDAY_REFRESH_INTERVAL_SEC = 30 * 60   # 30분
_MONDAY_REFRESH_PY = sys.executable
_MONDAY_REFRESH_SCRIPT = f'{BASE_DIR}/fetch_monday_dashboard.py'


def _refresh_monday_once():
    """4개 보드 재수집 + MONDAY_DF 리로드. 동시 실행 차단."""
    global MONDAY_DF
    if not MONDAY_REFRESH_LOCK.acquire(blocking=False):
        return  # 이미 실행 중
    try:
        MONDAY_REFRESH_STATUS.update(running=True, message='수집 중...')
        print('[Monday 자동갱신] 시작')
        result = subprocess.run(
            [_MONDAY_REFRESH_PY, '-u', _MONDAY_REFRESH_SCRIPT],
            cwd=f'{BASE_DIR}',
            capture_output=True, text=True, timeout=900,
            encoding='utf-8', errors='replace',
        )
        if result.returncode == 0:
            MONDAY_DF = load_monday_data()
            global _MATERIAL_CACHE
            _MATERIAL_CACHE = None
            tail = (result.stdout or '').strip().splitlines()[-1:]
            MONDAY_REFRESH_STATUS.update(
                running=False,
                last_run=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                last_status='success',
                message=tail[0] if tail else '완료',
            )
            print(f'[Monday 자동갱신] 완료 ({len(MONDAY_DF) if MONDAY_DF is not None else 0}행)')
        else:
            err = (result.stderr or '')[-300:]
            MONDAY_REFRESH_STATUS.update(
                running=False,
                last_run=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                last_status='error',
                message=err.strip() or 'subprocess 실패',
            )
            print(f'[Monday 자동갱신] 실패: {err[:200]}')
    except Exception as e:
        MONDAY_REFRESH_STATUS.update(
            running=False,
            last_run=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            last_status='error',
            message=str(e)[:200],
        )
        print(f'[Monday 자동갱신] 예외: {e}')
    finally:
        MONDAY_REFRESH_LOCK.release()


def _monday_refresh_scheduler():
    """30분마다 자동 갱신 (Flask 시작 시 즉시 1회 포함)."""
    while True:
        _refresh_monday_once()
        threading.Event().wait(_MONDAY_REFRESH_INTERVAL_SEC)


def _start_monday_refresh_loop():
    t = threading.Thread(target=_monday_refresh_scheduler, daemon=True, name='monday-refresh')
    t.start()
    print(f'[Monday 자동갱신] 스케줄러 시작 ({_MONDAY_REFRESH_INTERVAL_SEC // 60}분 주기)')


# ====== 자사재고 OneDrive 파일 변경 감지 + 자동 리로드 ======
JASA_REFRESH_STATUS = {
    'last_mtime': None,
    'last_reload': None,
    'message': '',
}
_JASA_WATCH_INTERVAL_SEC = 5 * 60   # 5분마다 mtime 체크

def _jasa_file_watcher():
    """OneDrive 자사재고 Excel 파일 변경 감지 → JASA_DF 자동 리로드."""
    global JASA_DF, JASA_META, JASA_KW_구분1, JASA_KW_업체
    import datetime as _dt
    threading.Event().wait(15)  # 앱 시작 후 15초 대기
    while True:
        try:
            _fetch_shared_jasa()   # 공유 웹원본 최신화(변경 시에만 저장) — 실패해도 무해
            current_path = _find_jasa_xlsx()
            if current_path:
                mtime = os.path.getmtime(current_path)
                if JASA_REFRESH_STATUS['last_mtime'] != mtime:
                    prev = JASA_REFRESH_STATUS['last_mtime']
                    JASA_REFRESH_STATUS['last_mtime'] = mtime
                    if prev is not None:  # 첫 로드는 스킵 (앱 시작 시 이미 로드됨)
                        print(f'[자사재고] 파일 변경 감지 → 리로드 중...')
                        new_df = load_jasa_data()
                        if new_df is not None:
                            JASA_DF = new_df
                            JASA_META = _jasa_summary()
                            JASA_KW_구분1 = set(JASA_DF[JASA_COL_구분1].unique()) if JASA_DF is not None else set()
                            JASA_KW_업체  = set(JASA_DF[JASA_COL_업체].unique())  if JASA_DF is not None else set()
                            ts = _dt.datetime.now().strftime('%H:%M')
                            JASA_REFRESH_STATUS['last_reload'] = ts
                            JASA_REFRESH_STATUS['message'] = f'{ts} 자동 리로드 완료 ({len(JASA_DF)}행)'
                            print(f'[자사재고] 리로드 완료: {len(JASA_DF)}행')
                    else:
                        JASA_REFRESH_STATUS['last_reload'] = _dt.datetime.now().strftime('%H:%M')
        except Exception as e:
            print(f'[자사재고 감시] 오류: {e}')
        threading.Event().wait(_JASA_WATCH_INTERVAL_SEC)

def _start_jasa_watcher():
    t = threading.Thread(target=_jasa_file_watcher, daemon=True, name='jasa-watcher')
    t.start()
    print('[자사재고] OneDrive 파일 감시 시작 (5분 주기)')


# ====== 외주재고(원자재부자재 재고파악 최종본) OneDrive 파일 변경 감지 ======
INVENTORY_REFRESH_STATUS = {'last_mtime': None, 'last_reload': None, 'message': ''}
_INVENTORY_WATCH_INTERVAL_SEC = 5 * 60

def _inventory_file_watcher():
    """OneDrive 최종본 Excel 변경 감지 → DF 자동 리로드."""
    global DF, CSV_PATH
    import datetime as _dt
    threading.Event().wait(20)
    while True:
        try:
            xlsx_path = _find_inventory_xlsx()
            if xlsx_path:
                mtime = os.path.getmtime(xlsx_path)
                if INVENTORY_REFRESH_STATUS['last_mtime'] != mtime:
                    prev = INVENTORY_REFRESH_STATUS['last_mtime']
                    INVENTORY_REFRESH_STATUS['last_mtime'] = mtime
                    if prev is not None:
                        print(f'[재고일지] 파일 변경 감지 → 리로드: {xlsx_path}')
                        try:
                            new_df, new_path = load_inventory_data()
                            DF = new_df
                            CSV_PATH = new_path
                            _vendor_overlay_apply()   # 거래처 입력값 재적용
                            ts = _dt.datetime.now().strftime('%H:%M')
                            INVENTORY_REFRESH_STATUS['last_reload'] = ts
                            INVENTORY_REFRESH_STATUS['message'] = f'{ts} 자동 리로드 완료 ({len(DF)}행)'
                            print(f'[재고일지] 리로드 완료: {len(DF)}행')
                        except Exception as e:
                            print(f'[재고일지] 리로드 오류: {e}')
                    else:
                        INVENTORY_REFRESH_STATUS['last_reload'] = _dt.datetime.now().strftime('%H:%M')
        except Exception as e:
            print(f'[재고일지 감시] 오류: {e}')
        threading.Event().wait(_INVENTORY_WATCH_INTERVAL_SEC)

# ====== 단가파일(NN년 원부자재 단가.xlsx) OneDrive 자동 반영 — /upload 수동 업로드 대체 ======
# 구매팀 관례: OneDrive\바탕 화면\구매팀월간\26.N월\26년 원부자재 단가.xlsx (매월 하위폴더에 1개). 연도가 바뀌면 '27년 …'이 되므로 연도는 와일드카드.
PRICE_ONEDRIVE_PATTERN = 'C:/Users/jgkim/OneDrive/**/*년 원부자재 단가.xlsx'
_PRICE_NAME_RE = re.compile(r'^(\d{2}|\d{4})년\s*원부자재\s*단가\.xlsx$')
PRICE_TARGET = f'{BASE_DIR}/26년 원부자재 단가.xlsx'     # convert_price.py 가 읽는 위치(프로젝트 사본, 이름 고정 — 원본 연도와 무관)
PRICE_REFRESH_STATUS = {'last_src': None, 'last_mtime': None, 'last_reload': None, 'message': ''}
_PRICE_WATCH_INTERVAL_SEC = 5 * 60


def _find_price_xlsx():
    """OneDrive 어디에 있든(구매팀월간/YY.N월 등) 단가 파일 중 연도(파일명 'NN년') 큰 것 → 최근 수정 순.
    연도를 먼저 보는 이유: 27년 파일이 생긴 뒤 26년 파일을 손봐도 27년이 유지되도록."""
    found = []
    for f in glob.glob(PRICE_ONEDRIVE_PATTERN, recursive=True):
        name = os.path.basename(f)
        m = _PRICE_NAME_RE.match(name)
        if not m or name.startswith('~$'):
            continue
        y = int(m.group(1))
        if y < 100:
            y += 2000
        try:
            found.append((y, os.path.getmtime(f), f))
        except OSError:
            continue
    return max(found)[2] if found else None


def _apply_price_xlsx(src):
    """OneDrive 단가 xlsx → 프로젝트 target 복사 → convert_price.py → DF 리로드. (업로드 핸들러와 동일 절차)"""
    import shutil as _sh
    import subprocess as _sp
    if os.path.exists(PRICE_TARGET):
        _sh.copy2(PRICE_TARGET, PRICE_TARGET + '.bak')
    _sh.copy2(src, PRICE_TARGET)          # 원본은 절대 직접 열지 않음(잠금/읽기전용 방지)
    r = _sp.run([sys.executable, os.path.join(BASE_DIR, 'convert_price.py')],
                capture_output=True, text=True, timeout=120, cwd=BASE_DIR,
                encoding='utf-8', errors='replace')
    if r.returncode != 0:
        raise RuntimeError('convert_price 실패: ' + (r.stderr or r.stdout)[-300:])
    _reload_aramanth_dfs()


def _price_file_watcher():
    import datetime as _dt
    threading.Event().wait(40)
    while True:
        try:
            src = _find_price_xlsx()
            if src:
                mtime = os.path.getmtime(src)
                # 첫 실행: 프로젝트 사본보다 OneDrive가 새로우면 즉시 반영(업로드 안 한 갱신분 흡수)
                local_m = os.path.getmtime(PRICE_TARGET) if os.path.exists(PRICE_TARGET) else 0
                stale = PRICE_REFRESH_STATUS['last_mtime'] is None and mtime > local_m + 1
                if stale or (PRICE_REFRESH_STATUS['last_mtime'] not in (None, mtime)):
                    print(f'[단가] OneDrive 갱신 감지 → 변환·리로드: {src}')
                    _apply_price_xlsx(src)
                    ts = _dt.datetime.now().strftime('%m-%d %H:%M')
                    PRICE_REFRESH_STATUS.update(last_reload=ts, message=f'{ts} 단가 자동 반영 ({os.path.basename(os.path.dirname(src))})')
                    print(f'[단가] 자동 반영 완료 ({ts})')
                PRICE_REFRESH_STATUS.update(last_src=src, last_mtime=mtime)
        except Exception as e:
            print(f'[단가 감시] 오류: {e!r:.160}')
        threading.Event().wait(_PRICE_WATCH_INTERVAL_SEC)


def _start_price_watcher():
    t = threading.Thread(target=_price_file_watcher, daemon=True, name='price-watcher')
    t.start()
    print('[단가] OneDrive 파일 감시 시작 (5분 주기)')


def _start_inventory_watcher():
    t = threading.Thread(target=_inventory_file_watcher, daemon=True, name='inventory-watcher')
    t.start()
    print('[재고일지] OneDrive 파일 감시 시작 (5분 주기)')


# ====== 아마란스 자동 갱신 (시작시 1회 + 60분 주기 폴링) ======
ARAMANTH_REFRESH_LOCK = threading.Lock()
ARAMANTH_REFRESH_STATUS = {
    'running': False,
    'last_run': None,
    'last_status': None,
    'message': '',
}
_ARAMANTH_REFRESH_INTERVAL_SEC = 60 * 60   # 60분 (fetch_all+fetch_bom 각 10~15분 소요)
_ARAMANTH_SCRIPTS = ['fetch_all.py', 'fetch_bom.py', 'fetch_stock.py', 'fetch_sale_price.py']   # 판매단가는 2026-09-11 추가(7/9 이후 고착 발견)


def _reload_aramanth_dfs():
    """fetch/업로드 완료 후 모든 아마란스 DF 메모리 리로드. BOM/자사 캐시도 무효화.
    각 load는 독립 try/except — 한 군데 실패해도 나머지 DF는 정상 리로드.
    반환: {'ok': [...성공 라벨], 'fail': [(라벨, 사유), ...]}"""
    global DF, CSV_PATH, PRICE_DF, SPEC_DF, JASA_DF, JASA_META
    global ORDER_DF, WP_ORDER_DF, PROD_DF, WO_DF, SHIP_DF, ISSUE_DF, BOM_DF, RCV_DF, STOCK_DF
    global _BOM_INDEX_CACHE, _BOM_EXPAND_CACHE
    global PRICE_BY_CODE, PRICE_BY_NAME
    global MONDAY_DF, _MATERIAL_CACHE, SALES_DF, SALE_PRICE
    _BOM_INDEX_CACHE = None
    _BOM_EXPAND_CACHE = {}
    _MATERIAL_CACHE = None
    ok, fail = [], []

    def _step(label, fn):
        try:
            fn()
            ok.append(label)
        except Exception as e:
            fail.append((label, str(e)[:120]))
            print(f'[리로드] {label} 실패: {e}')

    def _set_df(name, loader):
        def _do():
            globals()[name] = loader()
        return _do

    def _load_inv():
        df, path = load_inventory_data()
        globals().update(DF=df, CSV_PATH=path)
        _vendor_overlay_apply()   # 거래처 입력값 재적용
    _step('재고일지',   _load_inv)
    _step('단가',       _set_df('PRICE_DF', load_price_data))
    _step('부자재규격', _set_df('SPEC_DF', load_spec_data))
    _step('자사재고',   _set_df('JASA_DF', load_jasa_data))
    _step('자사요약',   _set_df('JASA_META', _jasa_summary))
    _step('발주정보',   _set_df('ORDER_DF', load_order_data))
    _step('외주발주',   _set_df('WP_ORDER_DF', load_wp_order_data))
    _step('생산실적',   _set_df('PROD_DF', load_production_data))
    _step('생산지시',   _set_df('WO_DF', load_work_order_data))
    _step('출하정보',   _set_df('SHIP_DF', load_shipment_data))
    _step('출고정보',   _set_df('ISSUE_DF', load_issue_data))
    _step('BOM',        _set_df('BOM_DF', load_bom_data))
    _step('입고정보',   _set_df('RCV_DF', load_rcv_data))
    _step('현재고',     _set_df('STOCK_DF', load_stock_data))
    _step('판매데이터', _set_df('SALES_DF', load_sales_data))
    _step('판매단가',   _set_df('SALE_PRICE', load_sale_price_data))
    _step('매입단가',   _set_df('PURCH_PRICE', load_purchase_price_data))
    # 단가 lookup은 발주정보·외주발주·매입단가가 모두 리로드된 뒤에 합성해야 함 (2026-09-11)
    _step('단가 lookup', lambda: globals().update(zip(('PRICE_BY_CODE', 'PRICE_BY_NAME'), _build_price_lookup())))
    _step('Monday',     _set_df('MONDAY_DF', load_monday_data))
    # 규격은 Monday 보드가 원천 → Monday 리로드 뒤에 SPEC_DF와 챗봇 역색인을 다시 만든다 (이전엔 역색인 미갱신)
    def _spec_refresh():
        sm = _spec_from_monday()
        if sm is not None:
            globals()['SPEC_DF'] = sm
        globals().update(zip(('SPEC_BY_CODE', 'SPEC_BY_NAME', 'SPEC_BY_VENDOR', 'SPEC_BY_DEST'), _build_spec_lookup()))
    _step('부자재규격(Monday)', _spec_refresh)

    if fail:
        print(f'[리로드] 완료 - 성공 {len(ok)}개, 실패 {len(fail)}개: {[l for l,_ in fail]}')
    else:
        print(f'[리로드] 완료 - 전체 {len(ok)}개 정상')
    return {'ok': ok, 'fail': fail}


def _refresh_aramanth_once():
    """fetch_all + fetch_bom 병렬 실행 → 완료 시 DF 리로드. 동시 실행 차단."""
    if not ARAMANTH_REFRESH_LOCK.acquire(blocking=False):
        return
    try:
        ARAMANTH_REFRESH_STATUS.update(running=True, message='수집 중... (10~15분)')
        print('[아마란스 자동갱신] 시작 (fetch_all + fetch_bom 병렬)')
        procs = []
        for script in _ARAMANTH_SCRIPTS:
            p = subprocess.Popen(
                [sys.executable, '-u', f'{BASE_DIR}/{script}'],
                cwd=f'{BASE_DIR}',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            procs.append((script, p))
        results = []
        for script, p in procs:
            try:
                # 75분: fetch_all이 13개월치 헤더+디테일 완주에 60분+ 걸림.
                # 과거 25분(1500초)이라 출하→입고→출고 후반부가 매번 kill돼 누락됐음.
                _, err = p.communicate(timeout=4500)
                results.append((script, p.returncode, err))
            except subprocess.TimeoutExpired:
                p.kill()
                results.append((script, -1, b'timeout'))
        failed = [(s, rc, e) for s, rc, e in results if rc != 0]
        # fetch 결과와 무관하게 항상 리로드 — 일부 스크립트만 실패했어도
        # 성공한 쪽 CSV는 디스크에 갱신되었으므로 메모리 반영해야 함.
        reload_result = _reload_aramanth_dfs()
        if not failed:
            ARAMANTH_REFRESH_STATUS.update(
                running=False,
                last_run=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                last_status='success',
                message=f'완료 (발주 {len(ORDER_DF) if ORDER_DF is not None else 0}, BOM {len(BOM_DF) if BOM_DF is not None else 0})',
            )
            print('[아마란스 자동갱신] 완료')
        else:
            msg = '; '.join(f'{s} rc={rc}' for s, rc, _ in failed)
            ARAMANTH_REFRESH_STATUS.update(
                running=False,
                last_run=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                last_status='error',
                message=f'fetch 일부 실패({msg[:120]}) — 성공분만 메모리 반영',
            )
            print(f'[아마란스 자동갱신] 부분 실패: {msg} (리로드는 진행)')
    except Exception as e:
        ARAMANTH_REFRESH_STATUS.update(
            running=False,
            last_run=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            last_status='error',
            message=str(e)[:200],
        )
        print(f'[아마란스 자동갱신] 예외: {e}')
    finally:
        ARAMANTH_REFRESH_LOCK.release()


def _aramanth_data_is_today():
    """오늘자 핵심 CSV가 모두 있는지 체크 (시작시 불필요한 재수집 방지).
    발주+BOM만 보면 앞단계(1/7)만 되고 후반부(출하5·입고6·출고7)가 kill돼도
    '완료'로 오판했음 → 후반부 파일까지 전부 확인해 누락 시 재수집 유도."""
    today = datetime.now().strftime('%Y%m%d')
    needed = ['발주정보', 'BOM', '출하정보', '입고정보', '출고정보']
    return all(glob.glob(f'{DATA_DIR}/{today}_{name}.csv') for name in needed)


def _aramanth_refresh_scheduler():
    # 시작 시: 오늘자 데이터 있으면 첫 fetch 건너뛰고 바로 60분 대기
    if _aramanth_data_is_today():
        print('[아마란스 자동갱신] 오늘자 데이터 존재 → 시작시 fetch 생략')
        ARAMANTH_REFRESH_STATUS.update(
            last_run=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            last_status='success',
            message='오늘자 데이터 존재 (스킵)',
        )
        threading.Event().wait(_ARAMANTH_REFRESH_INTERVAL_SEC)
    while True:
        _refresh_aramanth_once()
        threading.Event().wait(_ARAMANTH_REFRESH_INTERVAL_SEC)


def _start_aramanth_refresh_loop():
    t = threading.Thread(target=_aramanth_refresh_scheduler, daemon=True, name='aramanth-refresh')
    t.start()
    print(f'[아마란스 자동갱신] 스케줄러 시작 ({_ARAMANTH_REFRESH_INTERVAL_SEC // 60}분 주기)')


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


# ====== 외주재고 거래처 셀프 입력 (2026-09-11) ======
# 거래처가 카톡으로 엑셀을 보내고 구매팀이 취합하던 흐름을 대체:
#   거래처 전용 링크 /v/<토큰> (로그인 없음) → 품번별 현재고 입력 → data/vendor_stock_entries.jsonl 추가
#   → _vendor_overlay_apply()가 재고일지 DF의 현재고량에 덮어씀(원본 파일보다 나중 입력만, 원래 값은 '현재고량_일지'에 보존)
#   → 재고경고·거래처 페이지·마감 CSV 모두 즉시 반영. 토큰은 RELOAD_TOKEN(.reload_token) HMAC이라 로컬/GCP가 동일.
#   거래처는 GCP(공개 URL)에 입력하므로 로컬 앱이 1분마다 /api/vendor_entries를 끌어와(_vendor_pull_loop) 합침.
_VENDOR_ENTRIES = f'{DATA_DIR}/vendor_stock_entries.jsonl'
_VENDOR_LOCK = threading.Lock()
_VENDOR_CLOUD = 'https://8.235.41.127.sslip.io'
_VENDOR_PULL_STATE = {'last_ts': 0.0, 'last_run': '', 'pulled': 0, 'error': ''}


def _vendor_secret():
    s = os.environ.get('RELOAD_TOKEN', '')
    if not s:
        try:
            s = open(f'{BASE_DIR}/.reload_token', encoding='utf-8').read().strip()
        except Exception:
            s = ''
    return s or 'maehong-vendor-portal'


def _vendor_token(vendor):
    return hmac.new(_vendor_secret().encode('utf-8'), ('vendor:' + vendor).encode('utf-8'), hashlib.sha256).hexdigest()[:24]


def _vendor_list():
    if DF is None or DF.empty:
        return []
    vs = DF[COL_원산지].astype(str).str.strip()
    return sorted(v for v in vs.unique() if v and v.lower() != 'nan')


def _vendor_by_token(tok):
    tok = (tok or '').strip()
    for v in _vendor_list():
        if _vendor_token(v) == tok:
            return v
    return None


def _vendor_entries_read():
    out = []
    try:
        with open(_VENDOR_ENTRIES, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return out


def _vendor_entries_append(recs):
    if not recs:
        return
    with _VENDOR_LOCK:
        with open(_VENDOR_ENTRIES, 'a', encoding='utf-8') as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')


def _vendor_overlay_apply():
    """거래처 입력값을 DF 현재고량에 덮어씀. 원본 파일(CSV_PATH mtime)보다 나중 입력만 유효 →
    구매팀이 마감 파일을 새로 만들면 그 이전 입력은 자연히 파일 값에 흡수된다. 적용 행 수 반환."""
    global DF
    if DF is None or DF.empty:
        return 0
    try:
        base_ts = os.path.getmtime(CSV_PATH) if CSV_PATH and os.path.exists(str(CSV_PATH)) else 0
    except OSError:
        base_ts = 0
    if '현재고량_일지' not in DF.columns:
        DF['현재고량_일지'] = DF[COL_재고량]
    else:
        DF[COL_재고량] = DF['현재고량_일지']
    # 원본 파일 이후의 입력만: 실사(set) → 그 시점 기준값, 이후 입고(in) +, 사용(day, 자재행) −
    ents = _vendor_entries_read()
    dels = {e.get('ref') for e in ents if e.get('kind') == 'in_del'}
    latest_set = {}
    flows = {}   # (vendor, code) → list of (ts, delta)
    for e in ents:
        try:
            ts = float(e.get('ts', 0))
        except (TypeError, ValueError):
            continue
        if ts <= base_ts:
            continue
        k = (str(e.get('vendor', '')).strip(), str(e.get('code', '')).strip().upper())
        kind = e.get('kind', 'set')
        if kind == 'set':
            if k not in latest_set or ts > latest_set[k][0]:
                latest_set[k] = (ts, float(e.get('qty', 0) or 0))
        elif kind == 'in' and e.get('id') not in dels:
            flows.setdefault(k, []).append((ts, float(e.get('qty', 0) or 0)))
        elif kind == 'day' and e.get('type') == 'use':
            # 같은 (날짜) 항목은 최신 입력이 대체 → 여기선 최신만 남기기 위해 date 키로 정리
            flows.setdefault(k, []).append((ts, -float(e.get('qty', 0) or 0), e.get('date'), 'day'))
    keys = set(latest_set) | set(flows)
    if not keys:
        return 0
    vcol = DF[COL_원산지].astype(str).str.strip()
    ccol = DF[COL_품목].astype(str).str.strip().str.upper()
    n = 0
    for k in keys:
        v, c = k
        if c[:1] not in ('A', 'B', 'C', 'D'):
            continue   # 완제품/반제품 행(생산·출고)은 실사 대상 아님
        m = (vcol == v) & (ccol == c)
        if not m.any():
            continue
        st = latest_set.get(k)
        if st:
            base, t0 = st[1], st[0]
        else:
            base, t0 = _num(DF.loc[m, '현재고량_일지'].iloc[0]), base_ts
        # day 항목은 (date)별 최신 입력만 유효
        day_latest = {}
        total_in = 0.0
        for f in flows.get(k, []):
            if f[0] <= t0:
                continue
            if len(f) == 4:
                if f[2] not in day_latest or f[0] > day_latest[f[2]][0]:
                    day_latest[f[2]] = f
            else:
                total_in += f[1]
        q = base + total_in + sum(f[1] for f in day_latest.values())
        # 재고일지 컬럼은 문자열 dtype(엑셀/CSV 원본 그대로) → 문자열로 넣어야 pandas가 거부하지 않음. 소비처는 _num()으로 읽음
        DF.loc[m, COL_재고량] = str(int(q)) if float(q).is_integer() else str(round(q, 3))
        n += int(m.sum())
    try:
        with _API_CACHE_LOCK:
            _API_CACHE.clear()
    except Exception:
        pass
    return n


def _vendor_items(vendor):
    """거래처 입력 화면용 품목 목록: 원재료(A)·부재료(B/C/D) 품번 unique, 현재값·일지값·최근 입력."""
    rows = DF[DF[COL_원산지].astype(str).str.strip() == vendor] if DF is not None else None
    if rows is None or rows.empty:
        return []
    last = {}
    for e in _vendor_entries_read():
        if e.get('vendor') == vendor and e.get('kind', 'set') == 'set':
            c = str(e.get('code', '')).strip().upper()
            if c not in last or e['ts'] > last[c]['ts']:
                last[c] = e
    out, seen = [], set()
    for _, r in rows.iterrows():
        code = str(r.get(COL_품목, '')).strip()
        if not code or code.upper() in seen or code.upper()[:1] not in ('A', 'B', 'C', 'D'):
            continue
        seen.add(code.upper())
        le = last.get(code.upper())
        out.append({'code': code, 'name': str(r.get(COL_품명, '')).strip(), 'spec': str(r.get(COL_규격, '')).strip(),
                    'cls': '원재료' if code.upper().startswith('A') else '부재료',
                    'qty': _num(r.get(COL_재고량, 0)), 'qty_file': _num(r.get('현재고량_일지', r.get(COL_재고량, 0))),
                    'last_ts': le['ts'] if le else None, 'last_by': le.get('name', '') if le else '',
                    'last_at': datetime.fromtimestamp(le['ts']).strftime('%m-%d %H:%M') if le else ''})
    out.sort(key=lambda x: (0 if x['cls'] == '원재료' else 1, x['code']))
    return out


@app.route('/v/<token>')
def vendor_portal(token):
    vendor = _vendor_by_token(token)
    if not vendor:
        return '<meta charset="utf-8"><p style="font-family:sans-serif;padding:40px">유효하지 않은 링크입니다. 매홍 구매팀에 문의해 주세요.</p>', 404
    return render_template_string(VENDOR_PORTAL_TEMPLATE, vendor=vendor, token=token)


@app.route('/api/v/<token>/items', methods=['GET'])
def api_vendor_portal_items(token):
    vendor = _vendor_by_token(token)
    if not vendor:
        return jsonify({'error': 'invalid_token'}), 404
    src = os.path.basename(str(CSV_PATH)) if CSV_PATH else ''
    return jsonify({'vendor': vendor, 'items': _vendor_items(vendor), 'source': src,
                    'month': datetime.now().strftime('%Y-%m')})


@app.route('/api/v/<token>/submit', methods=['POST'])
def api_vendor_portal_submit(token):
    vendor = _vendor_by_token(token)
    if not vendor:
        return jsonify({'error': 'invalid_token'}), 404
    body = request.get_json(silent=True) or {}
    name = str(body.get('name', '') or '').strip()[:30]
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    now = time.time()
    valid = {x['code'].upper() for x in _vendor_items(vendor)}
    rows_all = {(r['code'].upper(), r['spec']): r for r in _vendor_rows(vendor)}
    recs = []
    for it in body.get('items') or []:
        c = str(it.get('code', '')).strip().upper()
        kind = str(it.get('kind', 'set') or 'set')
        try:
            q = float(str(it.get('qty', '')).replace(',', ''))
        except ValueError:
            if kind != 'in_del':   # 삭제 요청은 수량이 없음 (2026-09-16 버그: 여기서 걸러져 삭제가 안 됐음)
                continue
            q = 0.0
        if kind == 'set':
            if c not in valid or q < 0:
                continue
            recs.append({'ts': now, 'vendor': vendor, 'code': c, 'qty': q, 'name': name, 'ip': ip, 'kind': 'set'})
        elif kind == 'day':
            spec = str(it.get('spec', '') or '').strip(); date = str(it.get('date', '') or '')[:10]
            r = rows_all.get((c, spec))
            if not r or not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
                continue
            if r['type'] != 'delta' and q < 0:
                continue
            recs.append({'ts': now, 'vendor': vendor, 'code': c, 'spec': spec, 'date': date, 'qty': q,
                         'type': r['type'], 'name': name, 'ip': ip, 'kind': 'day'})
        elif kind == 'in':
            date = str(it.get('date', '') or '')[:10]; unit = str(it.get('unit', '') or '').strip()[:10]
            exp = str(it.get('exp', '') or '').strip()[:10]
            supplier = str(it.get('supplier', '') or '').strip()[:40]   # 원/부자재 업체명(F열)
            dest2 = str(it.get('dest', '') or '').strip()[:40]         # 출고처(J열)
            if c not in valid or q <= 0 or not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
                continue
            recs.append({'ts': now, 'id': f'{int(now * 1000)}-{len(recs)}', 'vendor': vendor, 'code': c, 'date': date, 'qty': q,
                         'unit': unit or ('kg' if c.startswith('A') else 'ea'), 'exp': exp, 'supplier': supplier, 'dest': dest2,
                         'name': name, 'ip': ip, 'kind': 'in'})
        elif kind == 'in_del':
            ref = str(it.get('id', '') or '')
            if ref:
                recs.append({'ts': now, 'vendor': vendor, 'code': c, 'qty': 0, 'ref': ref, 'name': name, 'ip': ip, 'kind': 'in_del'})
    if body.get('confirm'):
        recs.append({'ts': now, 'vendor': vendor, 'code': '', 'qty': 0, 'name': name, 'ip': ip, 'kind': 'confirm'})
    if not recs:
        return jsonify({'ok': False, 'error': '저장할 항목이 없습니다'}), 400
    _vendor_entries_append(recs)
    n = _vendor_overlay_apply()
    print(f"[거래처입력] {vendor} {name} {len(recs)}건 (적용 {n}행)")
    return jsonify({'ok': True, 'saved': len([r for r in recs if r['kind'] != 'confirm']), 'applied': n})


@app.route('/api/v/<token>/history', methods=['GET'])
def api_vendor_portal_history(token):
    vendor = _vendor_by_token(token)
    if not vendor:
        return jsonify({'error': 'invalid_token'}), 404
    es = [e for e in _vendor_entries_read() if e.get('vendor') == vendor]
    es.sort(key=lambda e: -float(e.get('ts', 0)))
    names = {x['code'].upper(): x['name'] for x in _vendor_items(vendor)}
    return jsonify({'items': [{'at': datetime.fromtimestamp(e['ts']).strftime('%m-%d %H:%M'), 'code': e.get('code', ''),
                               'name': names.get(str(e.get('code', '')).upper(), ''), 'qty': e.get('qty'), 'date': e.get('date', ''),
                               'by': e.get('name', ''), 'kind': e.get('kind', 'set')} for e in es[:40]]})


@app.route('/api/vendor_entries', methods=['GET'])
def api_vendor_entries():
    """전체 입력 이력 (로컬 앱의 GCP→로컬 동기화용, RELOAD_TOKEN 또는 로그인)."""
    try:
        after = float(request.args.get('after', 0) or 0)
    except ValueError:
        after = 0.0
    es = [e for e in _vendor_entries_read() if float(e.get('ts', 0)) > after]
    return jsonify({'items': es[-2000:], 'server_time': time.time()})


@app.route('/api/vendor_links', methods=['GET'])
def api_vendor_links():
    """구매팀용: 거래처별 입력 링크 + 최근 입력 현황."""
    es = _vendor_entries_read()
    ym = datetime.now().strftime('%Y-%m')
    out = []
    show_all = request.args.get('all') == '1'
    for v in _vendor_list():
        if not show_all and not any(a in v for a in ACTIVE_VENDORS):
            continue
        mine = [e for e in es if e.get('vendor') == v]
        last = max(mine, key=lambda e: float(e.get('ts', 0))) if mine else None
        month = [e for e in mine if datetime.fromtimestamp(float(e.get('ts', 0))).strftime('%Y-%m') == ym and e.get('kind', 'set') == 'set']
        tok = _vendor_token(v)
        out.append({'vendor': v, 'token': tok, 'url': f'{_VENDOR_CLOUD}/v/{tok}', 'items': len(_vendor_items(v)),
                    'last_at': datetime.fromtimestamp(float(last['ts'])).strftime('%Y-%m-%d %H:%M') if last else '',
                    'last_by': last.get('name', '') if last else '', 'month_count': len(month),
                    'month_codes': len({e.get('code') for e in month})})
    return jsonify({'items': out, 'total': len(out), 'pull': _VENDOR_PULL_STATE})


def _row_type(code, spec):
    """재고일지 행 유형 → (type, 입력 라벨, 시트 부호). 자재(A~D)=사용량(+, 행 수식이 차감), 생산=+, 출고/풀고=−(시트에 음수), 그 외=증감."""
    c = (code or '')[:1].upper(); s = spec or ''
    if c in ('A', 'B', 'C', 'D'):
        return 'use', '사용량', 1
    if '생산' in s:
        return 'prod', '생산량', 1
    if '출고' in s or '풀고' in s:
        return 'out', '출고량', -1
    return 'delta', '증감', 1


def _vendor_rows(vendor):
    """일별 입력용: 해당 거래처의 재고일지 행 전부 (품번+규격 키, 유형 포함)."""
    rows = DF[DF[COL_원산지].astype(str).str.strip() == vendor] if DF is not None else None
    if rows is None or rows.empty:
        return []
    out, seen = [], set()
    for _, r in rows.iterrows():
        code = str(r.get(COL_품목, '')).strip(); spec = str(r.get(COL_규격, '')).strip()
        if not code or (code.upper(), spec) in seen:
            continue
        seen.add((code.upper(), spec))
        t, label, sign = _row_type(code, spec)
        out.append({'code': code, 'spec': spec, 'name': str(r.get(COL_품명, '')).strip(), 'type': t, 'label': label, 'sign': sign,
                    'dest': str(r.get(COL_납품처, '')).strip(), 'unit': 'kg' if code.upper().startswith('A') else 'ea'})
    # 엑셀 양식과 같은 순서(재고일지 행 순서) 유지 — 거래처가 보던 시트와 동일하게 (2026-09-16)
    for i, r in enumerate(out):
        r['order'] = i
    return out


# 거래처 포털을 실제로 쓰는 외주처 (구매팀 정의 2026-09-16). 나머지는 /api/vendor_links?all=1 로만 표시
ACTIVE_VENDORS = ['더고은', '정성', '데이웰즈']


def _daily_col_map():
    """재고일지 DF의 'N월DD일' 컬럼 → {day: 컬럼명}. 파일의 월(첫 컬럼 기준)도 반환."""
    m = {}; mon = None
    if DF is None:
        return {}, None
    for c in DF.columns:
        mm = re.fullmatch(r'(\d+)월(\d+)일', str(c).strip())
        if mm:
            if mon is None:
                mon = int(mm.group(1))
            if int(mm.group(1)) == mon:
                m[int(mm.group(2))] = c
    return m, mon


@app.route('/api/v/<token>/sheet', methods=['GET'])
def api_vendor_portal_sheet(token):
    """엑셀 '재고일지' 양식 그대로: 행=거래처 품목(파일 순서), 열=기초·입고·현재고·1~31일. 파일 값 + 거래처 입력값."""
    import calendar as _cal
    vendor = _vendor_by_token(token)
    if not vendor:
        return jsonify({'error': 'invalid_token'}), 404
    ym = (request.args.get('ym') or datetime.now().strftime('%Y%m'))[:6]
    y, mo = int(ym[:4]), int(ym[4:6])
    ndays = _cal.monthrange(y, mo)[1]
    dcols, file_mon = _daily_col_map()
    file_is_month = (file_mon == mo)
    day, ins = _vendor_month_entries(vendor, ym)
    rows = DF[DF[COL_원산지].astype(str).str.strip() == vendor] if DF is not None else None
    out = []
    if rows is not None:
        seen = set()
        for _, r in rows.iterrows():
            code = str(r.get(COL_품목, '')).strip(); spec = str(r.get(COL_규격, '')).strip()
            if not code or (code.upper(), spec) in seen:
                continue
            seen.add((code.upper(), spec))
            t, label, sign = _row_type(code, spec)
            daily_file = {}
            if file_is_month:
                for d, c in dcols.items():
                    v = _num(r.get(c, 0))
                    if v:
                        daily_file[d] = v
            daily_entry, entry_by = {}, {}
            for (c2, s2, dstr), e in day.items():
                if c2 == code.upper() and s2 == spec:
                    d = int(dstr[8:10]); daily_entry[d] = float(e.get('qty', 0) or 0) * sign; entry_by[d] = e.get('name', '')
            out.append({'dest': str(r.get(COL_납품처, '')).strip(), 'code': code, 'spec': spec, 'name': str(r.get(COL_품명, '')).strip(),
                        'ipsu': str(r.get(COL_입수량, '')).strip(), 'type': t, 'label': label, 'sign': sign,
                        'unit': 'kg' if code.upper().startswith('A') else 'ea',
                        'base': _num(r.get(DF.columns[7], 0)), 'inb_file': _num(r.get(DF.columns[8], 0)),
                        'cur_file': _num(r.get('현재고량_일지', r.get(COL_재고량, 0))),
                        'daily_file': daily_file, 'daily_entry': daily_entry, 'entry_by': entry_by})
    inbound = sorted([{'id': e['id'], 'date': e['date'], 'code': e['code'], 'qty': e['qty'], 'unit': e.get('unit', ''), 'exp': e.get('exp', ''),
                       'supplier': e.get('supplier', ''), 'dest': e.get('dest', ''),
                       'by': e.get('name', ''), 'name': next((x['name'] for x in out if x['code'].upper() == e['code']), ''),
                       'spec': next((x['spec'] for x in out if x['code'].upper() == e['code']), '')} for e in ins], key=lambda x: (x['date'], x['id']))
    return jsonify({'vendor': vendor, 'ym': ym, 'ndays': ndays, 'rows': out, 'inbound': inbound,
                    'file_month_matches': file_is_month, 'source': os.path.basename(str(CSV_PATH)) if CSV_PATH else ''})


def _vendor_month_entries(vendor, ym):
    """해당 월의 유효 day/in 엔트리: day는 (code,spec,date)별 최신, in은 삭제 안 된 것."""
    ents = [e for e in _vendor_entries_read() if e.get('vendor') == vendor]
    dels = {e.get('ref') for e in ents if e.get('kind') == 'in_del'}
    day = {}
    ins = []
    for e in ents:
        d = str(e.get('date', '') or '')
        if not d.startswith(ym[:4] + '-' + ym[4:6]):
            continue
        if e.get('kind') == 'day':
            k = (str(e.get('code', '')).upper(), e.get('spec', ''), d)
            if k not in day or e['ts'] > day[k]['ts']:
                day[k] = e
        elif e.get('kind') == 'in' and e.get('id') not in dels:
            ins.append(e)
    return day, ins


@app.route('/api/v/<token>/daily', methods=['GET'])
def api_vendor_portal_daily(token):
    vendor = _vendor_by_token(token)
    if not vendor:
        return jsonify({'error': 'invalid_token'}), 404
    date = (request.args.get('date') or datetime.now().strftime('%Y-%m-%d'))[:10]
    ym = date[:4] + date[5:7]
    day, ins = _vendor_month_entries(vendor, ym)
    rows = _vendor_rows(vendor)
    for r in rows:
        e = day.get((r['code'].upper(), r['spec'], date))
        r['value'] = e['qty'] if e else None
        r['by'] = e.get('name', '') if e else ''
        r['month_total'] = sum(v['qty'] for (c, s, d), v in day.items() if c == r['code'].upper() and s == r['spec'])
        r['month_days'] = sum(1 for (c, s, d), v in day.items() if c == r['code'].upper() and s == r['spec'] and v['qty'])
    filled_dates = sorted({d for (_, _, d) in day.keys()})
    return jsonify({'vendor': vendor, 'date': date, 'rows': rows, 'filled_dates': filled_dates,
                    'inbound': sorted([{'id': e['id'], 'date': e['date'], 'code': e['code'], 'qty': e['qty'], 'unit': e.get('unit', ''),
                                        'by': e.get('name', ''), 'name': next((x['name'] for x in rows if x['code'].upper() == e['code']), '')}
                                       for e in ins], key=lambda x: (x['date'], x['id']))})


# ── 마감 워크북 생성 (로컬 전용: OneDrive 월 파일 복사 → 일별 열·입고일지 기입 → Excel 재계산 → 저장) ──
_CLOSING_MANIFEST = f'{DATA_DIR}/마감/generated.json'


def _closing_baseline(ym):
    """해당 월의 기준 파일. **이미 마감 파일이 있으면 마감 파일**(구매팀이 데일리 사용량을 적는 실재고 파일 — 거기에 거래처 입력분을
    덧붙임), 없으면 월 파일 '원자재부자재 재고파악(N월).xlsx'을 복사해 마감을 새로 만든다. 작업 폴더 우선."""
    mon = int(ym[4:6])
    cands, closings = [], []
    for f in glob.glob(INVENTORY_ONEDRIVE_PATTERN, recursive=True):
        b = os.path.basename(f)
        if b.startswith('~$') or '카카오톡' in f:
            continue
        if re.fullmatch(rf'원자재부자재 재고파악\({mon}월\)\s*[-_]\s*마감\.xlsx', b):
            closings.append(f)
        elif re.fullmatch(rf'원자재부자재 재고파악\({mon}월\)\.xlsx', b):
            cands.append(f)
    if closings:
        closings.sort(key=lambda f: (f.replace('\\', '/').lower().startswith(INVENTORY_PREFERRED_DIR.lower()),
                                     (ym[:4] + '년') in f or (ym[:4] + '년도') in f, os.path.getmtime(f)))
        return closings[-1]
    # 연도 폴더 우선(2026년) → 최신 mtime
    # 사용자 작업 폴더(바탕 화면\구매) > 연도 폴더명 일치 > 최신 mtime — 마감 파일도 이 폴더에 생성돼야 구매팀이 쓰는 위치와 맞음
    cands.sort(key=lambda f: (f.replace('\\', '/').lower().startswith(INVENTORY_PREFERRED_DIR.lower()),
                              (ym[:4] + '년') in f or (ym[:4] + '년도') in f, os.path.getmtime(f)))
    return cands[-1] if cands else None


def _excel_recalc(path):
    """Excel COM으로 열어 전체 재계산 후 저장 — openpyxl은 수식 결과(캐시값)를 못 쓰므로 앱/pandas가 읽을 값을 채우기 위함."""
    import pythoncom
    import win32com.client as w32
    pythoncom.CoInitialize()
    xl = None
    try:
        xl = w32.DispatchEx('Excel.Application')
        xl.Visible = False; xl.DisplayAlerts = False
        wb = xl.Workbooks.Open(os.path.abspath(path), UpdateLinks=0)
        xl.CalculateFullRebuild()
        wb.Save(); wb.Close(SaveChanges=True)
    finally:
        if xl is not None:
            xl.Quit()
        pythoncom.CoUninitialize()


@app.route('/api/closing/generate', methods=['POST'])
def api_closing_generate():
    """마감 워크북 생성. body {ym:'202609', dry_run:false}. 로컬(OneDrive) 전용."""
    body = request.get_json(silent=True) or {}
    ym = str(body.get('ym') or datetime.now().strftime('%Y%m'))[:6]
    res, code = _closing_generate(ym, dry_run=bool(body.get('dry_run')))
    return jsonify(res), code


def _closing_generate(ym, dry_run=False):
    """마감 워크북 생성 본체 (버튼·자동 스케줄 공용). 반환 (dict, http코드)."""
    import shutil as _sh
    from datetime import datetime as _dt
    if not re.fullmatch(r'\d{6}', ym):
        return {'ok': False, 'error': 'ym 형식 오류'}, 400
    if not os.path.exists('C:/Users/jgkim/OneDrive'):
        return {'ok': False, 'error': 'OneDrive 없는 환경(GCP)에선 생성 불가 — 호스트 대시보드에서 실행'}, 400
    base = _closing_baseline(ym)
    if not base:
        return {'ok': False, 'error': f'{int(ym[4:6])}월 기준 파일(원자재부자재 재고파악(N월).xlsx)을 OneDrive에서 찾지 못함'}, 404
    mon = int(ym[4:6])
    base_is_closing = bool(re.search(r'[-_]\s*마감\.xlsx$', os.path.basename(base)))
    # 마감이 이미 있으면 그 파일 자체가 대상(구매팀 데일리 기록 보존 + 거래처 입력분 덧붙임). 없으면 월 파일 옆에 새 마감 생성.
    target = base if base_is_closing else os.path.join(os.path.dirname(base), f'원자재부자재 재고파악({mon}월) - 마감.xlsx')
    side = False
    dry = bool(dry_run)
    os.makedirs(f'{DATA_DIR}/마감/backup', exist_ok=True)
    if dry:
        target = f'{DATA_DIR}/마감/{ym}_재고일지_마감_미리보기.xlsx'
    elif os.path.exists(target):
        # 덮어쓰기 전 백업 (OneDrive 밖, data/마감/backup/)
        _sh.copy2(target, f'{DATA_DIR}/마감/backup/{ym}_마감_{_dt.now():%m%d-%H%M%S}.xlsx')
    # 원본을 임시 작업본으로 복사해 Excel이 열도록 함 (마감 파일 자체를 열면 SaveAs 충돌·OneDrive 잠금)
    work = os.path.join(os.environ.get('TEMP', BASE_DIR), f'closing_work_{ym}_{int(time.time())}.xlsx')
    _sh.copy2(base, work)
    # Excel COM 전용 스크립트(closing_excel.py)에 넘길 기입 목록. openpyxl 저장본은 Excel이 열지 못해(손상 판정) COM으로만 처리.
    days, ins_all = [], []
    for vendor in _vendor_list():
        day, ins = _vendor_month_entries(vendor, ym)
        for (code, spec, date), e in day.items():
            q = float(e.get('qty', 0) or 0); sign = _row_type(code, spec)[2]; val = q * sign
            days.append({'vendor': vendor, 'code': code, 'spec': spec, 'day': int(date[8:10]),
                         'val': None if q == 0 else (int(val) if float(val).is_integer() else round(val, 3))})
        for e in sorted(ins, key=lambda x: (x['date'], x['id'])):
            q = float(e.get('qty', 0) or 0)
            ins_all.append({'vendor': vendor, 'code': e['code'], 'date': e['date'], 'qty': int(q) if q.is_integer() else q, 'unit': e.get('unit', ''),
                            'exp': e.get('exp', ''), 'supplier': e.get('supplier', ''), 'dest': e.get('dest', '')})
    spec_path = os.path.join(os.environ.get('TEMP', BASE_DIR), f'closing_{ym}_{int(time.time())}.json')
    json.dump({'baseline': work, 'target': work, 'inplace': True, 'mon': mon, 'days': days, 'ins': ins_all,
               'skip_existing_inbound': base_is_closing},
              open(spec_path, 'w', encoding='utf-8'), ensure_ascii=False)
    import subprocess as _sp
    try:
        pr = _sp.run([sys.executable, os.path.join(BASE_DIR, 'closing_excel.py'), spec_path], capture_output=True, text=True,
                     encoding='utf-8', errors='replace', timeout=600, cwd=BASE_DIR)
        res = json.loads((pr.stdout or '').strip().splitlines()[-1]) if pr.stdout and pr.stdout.strip() else {'ok': False, 'error': (pr.stderr or '')[-300:]}
    except Exception as ex:
        res = {'ok': False, 'error': str(ex)[:300]}
    try:
        os.remove(spec_path)
    except OSError:
        pass
    if not res.get('ok'):
        try:
            os.remove(work)
        except OSError:
            pass
        return {'ok': False, 'error': 'Excel 생성 실패: ' + str(res.get('error', ''))[:300]}, 500
    try:
        _sh.copy2(work, target)   # 작업본 → 실제 마감 파일(OneDrive) 또는 미리보기
    except PermissionError:
        # 구매팀이 엑셀로 열어둔 상태 — 작업본을 남기지 않고 실패 반환(자동 스케줄은 나중에 재시도)
        try:
            os.remove(work)
        except OSError:
            pass
        return {'ok': False, 'error': '마감 파일이 열려 있어 저장하지 못함(엑셀을 닫은 뒤 다시 시도)', 'locked': True}, 423
    try:
        os.remove(work)
    except OSError:
        pass
    written, n_in, missing, recalc_err = res.get('written', 0), res.get('inbound_rows', 0), res.get('missing', []), ''
    print(f'[마감생성] {target} (기준: {"기존 마감" if base_is_closing else "월 파일"}) 일별 {written}칸 · 입고 {n_in}행 · 누락 {len(missing)}')
    return {'ok': True, 'target': target, 'baseline': base, 'base_is_closing': base_is_closing, 'side_file': side,
            'daily_cells': written, 'inbound_rows': n_in, 'missing': missing[:20], 'recalc': 'ok'}, 200


# ── 마감 자동 반영 (2026-09-17): 매일 CLOSING_AUTO_HOUR 이후, 거래처 입력이 새로 있으면 마감 파일에 자동 기입 ──
_CLOSING_AUTO_STATE = f'{DATA_DIR}/마감/auto_state.json'
CLOSING_AUTO_HOUR = 18          # 구매팀이 마감 파일을 닫은 뒤(퇴근 무렵) 실행. 열려 있으면 10분 뒤 재시도(최대 21시까지)
_CLOSING_AUTO_LOCK = threading.Lock()


def _closing_auto_state():
    try:
        return json.load(open(_CLOSING_AUTO_STATE, encoding='utf-8'))
    except Exception:
        return {}


def _closing_auto_once(force=False, now=None):
    """이번 달(+월초 5일까지는 전월)에 대해 마지막 자동 반영 이후 새 거래처 입력이 있으면 마감 생성. 반환 [결과 dict]."""
    now = now or datetime.now()
    today = now.strftime('%Y-%m-%d')
    made = []
    with _CLOSING_AUTO_LOCK:
        st = _closing_auto_state()
        yms = [now.strftime('%Y%m')] + ([_ym_shift(now.strftime('%Y%m'), -1)] if now.day <= 5 else [])
        for ym in yms:
            cur = st.get(ym, {})
            if not force and cur.get('date') == today:
                continue
            if not force and cur.get('fail_date') == today and cur.get('fails', 0) >= 3:
                continue
            ents = [e for e in _vendor_entries_read()
                    if str(e.get('date', '')).startswith(ym[:4] + '-' + ym[4:6]) and e.get('kind') in ('day', 'in', 'in_del')]
            mx = max((float(e.get('ts', 0) or 0) for e in ents), default=0.0)
            if not ents or (not force and mx <= float(cur.get('last_ts', 0) or 0)):
                st[ym] = {**cur, 'date': today, 'skipped': '새 입력 없음'}
                continue
            res, code = _closing_generate(ym)
            if res.get('ok'):
                st[ym] = {'date': today, 'last_ts': mx, 'target': res['target'], 'daily_cells': res['daily_cells'],
                          'inbound_rows': res['inbound_rows'], 'at': now.strftime('%Y-%m-%d %H:%M')}
                _notify('info', 'closing', f'📗 {int(ym[4:6])}월 마감 자동 반영',
                        f"{os.path.basename(res['target'])}\n거래처 입력 일별 {res['daily_cells']}칸 · 입고 {res['inbound_rows']}행 기입"
                        + (f"\n품번 누락 {len(res['missing'])}건(마감 파일에 행 없음): " + ', '.join(res['missing'][:5]) if res.get('missing') else ''))
            else:
                fails = (cur.get('fails', 0) if cur.get('fail_date') == today else 0) + 1
                st[ym] = {**cur, 'fail_date': today, 'fails': fails, 'error': res.get('error', '')}
                if fails >= 3 or not res.get('locked'):
                    _notify('warn', 'closing', f'📗 {int(ym[4:6])}월 마감 자동 반영 실패', str(res.get('error', ''))[:200])
            made.append({'ym': ym, **res})
        os.makedirs(os.path.dirname(_CLOSING_AUTO_STATE), exist_ok=True)
        json.dump(st, open(_CLOSING_AUTO_STATE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return made


def _closing_auto_loop():
    threading.Event().wait(120)
    while True:
        try:
            now = datetime.now()
            if CLOSING_AUTO_HOUR <= now.hour < 21 and os.path.exists('C:/Users/jgkim/OneDrive'):
                made = _closing_auto_once(now=now)
                if made:
                    print(f'[마감 자동] {len(made)}건 처리: ' + ', '.join(f"{m['ym']}={'ok' if m.get('ok') else m.get('error', '')[:40]}" for m in made))
        except Exception as e:
            print(f'[마감 자동] 오류: {e!r:.160}')
        threading.Event().wait(10 * 60)


def _start_closing_auto():
    if not os.path.exists('C:/Users/jgkim/OneDrive'):
        return
    t = threading.Thread(target=_closing_auto_loop, daemon=True, name='closing-auto')
    t.start()
    print(f'[마감 자동] 스케줄 시작 (매일 {CLOSING_AUTO_HOUR}시 이후 새 거래처 입력 있으면 마감 파일 기입)')


@app.route('/api/closing/auto_run', methods=['POST'])
def api_closing_auto_run():
    """마감 자동 반영을 지금 실행 (force=1이면 새 입력 유무와 무관하게)."""
    force = (request.args.get('force') or (request.get_json(silent=True) or {}).get('force') or '') in ('1', 'true', True)
    made = _closing_auto_once(force=force)
    return jsonify({'ok': True, 'made': made, 'state': _closing_auto_state()})


@app.route('/api/closing/status', methods=['GET'])
def api_closing_status():
    ym = (request.args.get('ym') or datetime.now().strftime('%Y%m'))[:6]
    base = _closing_baseline(ym) if os.path.exists('C:/Users/jgkim/OneDrive') else None
    mon = int(ym[4:6])
    if base and re.search(r'[-_]\s*마감\.xlsx$', os.path.basename(base)):
        target = base   # 기존 마감 파일에 덧붙임
    else:
        target = os.path.join(os.path.dirname(base), f'원자재부자재 재고파악({mon}월) - 마감.xlsx') if base else ''
    tot_day, tot_in, by_vendor = 0, 0, []
    for v in _vendor_list():
        day, ins = _vendor_month_entries(v, ym)
        tot_day += len(day); tot_in += len(ins)
        by_vendor.append({'vendor': v, 'day': len(day), 'inbound': len(ins)})
    return jsonify({'ym': ym, 'baseline': base or '', 'target': target, 'target_exists': bool(target and os.path.exists(target)),
                    'target_mtime': datetime.fromtimestamp(os.path.getmtime(target)).strftime('%Y-%m-%d %H:%M') if target and os.path.exists(target) else '',
                    'day_entries': tot_day, 'inbound_entries': tot_in, 'by_vendor': by_vendor, 'local': os.path.exists('C:/Users/jgkim/OneDrive')})


def _vendor_pull_loop():
    """로컬 전용: GCP에 거래처가 입력한 값을 1분마다 끌어와 로컬 이력에 합치고 DF에 적용."""
    import requests as _rq
    tok = _vendor_secret()
    known = {(str(e.get('ts')), e.get('vendor'), e.get('code')) for e in _vendor_entries_read()}
    threading.Event().wait(30)
    while True:
        try:
            r = _rq.get(f'{_VENDOR_CLOUD}/api/vendor_entries', params={'token': tok, 'after': _VENDOR_PULL_STATE['last_ts']},
                        timeout=15)
            if r.status_code == 200:
                new = []
                for e in r.json().get('items', []):
                    k = (str(e.get('ts')), e.get('vendor'), e.get('code'))
                    if k not in known:
                        known.add(k); new.append(e)
                    _VENDOR_PULL_STATE['last_ts'] = max(_VENDOR_PULL_STATE['last_ts'], float(e.get('ts', 0)))
                if new:
                    _vendor_entries_append(new)
                    n = _vendor_overlay_apply()
                    _VENDOR_PULL_STATE['pulled'] += len(new)
                    print(f'[거래처입력 동기화] GCP→로컬 {len(new)}건 (적용 {n}행)')
                _VENDOR_PULL_STATE['error'] = ''
            else:
                _VENDOR_PULL_STATE['error'] = f'HTTP {r.status_code}'
            _VENDOR_PULL_STATE['last_run'] = datetime.now().strftime('%H:%M')
        except Exception as e:
            _VENDOR_PULL_STATE['error'] = str(e)[:80]
        threading.Event().wait(60)


def _start_vendor_pull():
    t = threading.Thread(target=_vendor_pull_loop, daemon=True)
    t.start()
    print('[거래처입력 동기화] GCP 폴링 시작 (1분 주기)')


_VENDOR_PORTAL_TEMPLATE_V1 = r'''<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ vendor }} 재고 입력 - 매홍</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box} body{font-family:'Noto Sans KR',sans-serif;background:#f4f5fa;margin:0;color:#0f172a}
.top{background:linear-gradient(135deg,#1e1b4b,#312e81);color:#fff;padding:16px 18px}
.top h1{margin:0;font-size:18px;font-weight:800}.top p{margin:4px 0 0;font-size:12px;opacity:.85}
.wrap{max-width:860px;margin:0 auto;padding:14px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.bar input[type=text]{flex:1;min-width:140px;padding:9px 12px;border:1px solid #cbd5e1;border-radius:9px;font-size:14px;font-family:inherit}
.btn{padding:10px 16px;border-radius:9px;border:0;font-weight:800;font-size:14px;cursor:pointer;font-family:inherit}
.btn.p{background:#4f46e5;color:#fff}.btn.g{background:#fff;color:#475569;border:1px solid #cbd5e1}.btn:disabled{opacity:.5}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;margin-bottom:12px}
.row{display:grid;grid-template-columns:1fr 120px;gap:8px;align-items:center;padding:10px 12px;border-bottom:1px solid #f1f5f9}
.row.changed{background:#eef2ff}.row .nm{font-weight:700;font-size:14px;line-height:1.3}.row .cd{font-size:11.5px;color:#64748b;margin-top:2px}
.row .cd b{color:#4f46e5}.row input{width:100%;padding:9px 8px;text-align:right;font-size:16px;border:1px solid #cbd5e1;border-radius:8px;font-family:inherit;font-variant-numeric:tabular-nums}
.grp{padding:8px 12px;background:#f8fafc;font-size:12px;font-weight:800;color:#334155}
.foot{position:sticky;bottom:0;background:rgba(255,255,255,.96);border-top:1px solid #e2e8f0;padding:10px 14px;display:flex;gap:8px;align-items:center;justify-content:space-between;backdrop-filter:blur(4px)}
.toast{position:fixed;left:50%;bottom:80px;transform:translateX(-50%);background:#0f172a;color:#fff;padding:10px 16px;border-radius:10px;font-size:13px;display:none;z-index:9}
.hist{font-size:12px;color:#475569}.hist div{padding:5px 12px;border-bottom:1px solid #f1f5f9}
.note{font-size:12px;color:#64748b;margin:0 0 10px}
@media(max-width:480px){.row{grid-template-columns:1fr 96px}}
</style></head><body>
<div class="top"><h1>📦 {{ vendor }} 원·부자재 재고 입력</h1><p>매홍 L&F 구매팀 · 현재 보유 수량을 품목별로 입력하고 저장하세요. 저장 즉시 매홍 대시보드에 반영됩니다.</p></div>
<div class="wrap">
  <p class="note" id="note">불러오는 중...</p>
  <div class="bar">
    <input type="text" id="who" placeholder="입력자 이름 (예: 홍길동)" maxlength="30">
    <input type="text" id="q" placeholder="품번/품명 검색" oninput="render()">
    <button class="btn g" onclick="fillAll()" title="입력칸을 현재값으로 채움">현재값 채우기</button>
  </div>
  <div class="card" id="list"></div>
  <div class="card"><div class="grp">최근 입력 이력</div><div class="hist" id="hist"></div></div>
</div>
<div class="foot">
  <span id="cnt" style="font-size:12px;color:#64748b">변경 0건</span>
  <span><button class="btn g" onclick="confirmSame()">변동 없음 확인</button> <button class="btn p" id="save" onclick="save()">저장</button></span>
</div>
<div class="toast" id="toast"></div>
<script>
const TOKEN = {{ token|tojson }};
let ITEMS = [], VALS = {};
const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const fmt = n => (Math.round(n * 100) / 100).toLocaleString();
function toast(m) { const t = document.getElementById('toast'); t.textContent = m; t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 2600); }
async function load() {
  try { document.getElementById('who').value = localStorage.getItem('mhVendorWho') || ''; } catch (e) {}
  const d = await (await fetch('/api/v/' + TOKEN + '/items')).json();
  ITEMS = d.items || []; VALS = {};
  document.getElementById('note').textContent = d.month + ' 기준 · 품목 ' + ITEMS.length + '개 · 기준 파일: ' + (d.source || '-') + ' · 값을 바꾼 품목만 저장됩니다';
  render(); loadHist();
}
function render() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const L = document.getElementById('list'); let html = ''; let cur = '';
  ITEMS.filter(x => !q || (x.code + ' ' + x.name).toLowerCase().includes(q)).forEach(x => {
    if (x.cls !== cur) { cur = x.cls; html += '<div class="grp">' + cur + '</div>'; }
    const v = VALS[x.code]; const changed = v != null && v !== '' && Number(v) !== Number(x.qty);
    html += '<div class="row' + (changed ? ' changed' : '') + '"><div><div class="nm">' + esc(x.name) + '</div><div class="cd"><b>' + esc(x.code) + '</b>' + (x.spec ? ' · ' + esc(x.spec) : '') + ' · 현재 <b>' + fmt(x.qty) + '</b>' + (x.last_at ? ' <span style="color:#94a3b8">(' + x.last_at + (x.last_by ? ' ' + esc(x.last_by) : '') + ' 입력)</span>' : '') + '</div></div>'
      + '<input type="number" inputmode="decimal" min="0" step="any" placeholder="' + fmt(x.qty) + '" value="' + (v == null ? '' : v) + '" oninput="VALS[' + JSON.stringify(x.code) + ']=this.value;upd()"></div>';
  });
  L.innerHTML = html || '<div style="padding:16px;color:#94a3b8">품목이 없습니다</div>';
  upd();
}
function changedItems() { return ITEMS.filter(x => VALS[x.code] != null && VALS[x.code] !== '' && Number(VALS[x.code]) !== Number(x.qty)).map(x => ({ code: x.code, qty: Number(VALS[x.code]) })); }
function upd() { const n = changedItems().length; document.getElementById('cnt').textContent = '변경 ' + n + '건'; document.querySelectorAll('.row').forEach((r, i) => {}); }
function fillAll() { ITEMS.forEach(x => { if (VALS[x.code] == null || VALS[x.code] === '') VALS[x.code] = x.qty; }); render(); }
function who() { const w = document.getElementById('who').value.trim(); try { localStorage.setItem('mhVendorWho', w); } catch (e) {} return w; }
async function save() {
  const items = changedItems(); if (!items.length) { toast('변경된 품목이 없습니다'); return; }
  const w = who(); if (!w) { toast('입력자 이름을 적어 주세요'); document.getElementById('who').focus(); return; }
  document.getElementById('save').disabled = true;
  try {
    const r = await fetch('/api/v/' + TOKEN + '/submit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: w, items }) });
    const d = await r.json();
    if (d.ok) { toast('저장 완료 · ' + d.saved + '건 반영'); await load(); } else toast('저장 실패: ' + (d.error || ''));
  } catch (e) { toast('오류: ' + e.message); }
  document.getElementById('save').disabled = false;
}
async function confirmSame() {
  const w = who(); if (!w) { toast('입력자 이름을 적어 주세요'); return; }
  const r = await fetch('/api/v/' + TOKEN + '/submit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: w, items: [], confirm: true }) });
  const d = await r.json(); toast(d.ok ? '변동 없음으로 기록했습니다' : '기록 실패'); loadHist();
}
async function loadHist() {
  const d = await (await fetch('/api/v/' + TOKEN + '/history')).json();
  const H = document.getElementById('hist');
  H.innerHTML = (d.items || []).length ? d.items.map(h => '<div>' + h.at + ' · ' + (h.kind === 'confirm' ? '<b>변동 없음 확인</b>' : '<b>' + esc(h.code) + '</b> ' + esc(h.name) + ' → ' + fmt(h.qty)) + (h.by ? ' <span style="color:#94a3b8">' + esc(h.by) + '</span>' : '') + '</div>').join('') : '<div style="color:#94a3b8">아직 입력 이력이 없습니다</div>';
}
load();
</script></body></html>'''

# V2 (2026-09-11): 탭 3개 — 재고 실사 / 일별 사용·생산·출고 / 입고 등록. (V3로 대체, 참고용)
_VENDOR_PORTAL_TEMPLATE_V2 = r'''<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ vendor }} 재고 입력 - 매홍</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box} body{font-family:'Noto Sans KR',sans-serif;background:#f4f5fa;margin:0;color:#0f172a;padding-bottom:70px}
.top{background:linear-gradient(135deg,#1e1b4b,#312e81);color:#fff;padding:14px 18px 10px}
.top h1{margin:0;font-size:18px;font-weight:800}.top p{margin:4px 0 8px;font-size:12px;opacity:.85}
.tabs{display:flex;gap:6px}.tab{flex:1;text-align:center;padding:9px 6px;border-radius:9px;background:rgba(255,255,255,.12);color:#fff;font-weight:700;font-size:13px;cursor:pointer}
.tab.on{background:#fff;color:#312e81}
.wrap{max-width:900px;margin:0 auto;padding:12px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.bar input[type=text],.bar input[type=date],.bar select{padding:9px 12px;border:1px solid #cbd5e1;border-radius:9px;font-size:14px;font-family:inherit;background:#fff}
.bar input[type=text]{flex:1;min-width:120px}
.btn{padding:10px 16px;border-radius:9px;border:0;font-weight:800;font-size:14px;cursor:pointer;font-family:inherit}
.btn.p{background:#4f46e5;color:#fff}.btn.g{background:#fff;color:#475569;border:1px solid #cbd5e1}.btn.d{background:#fff;color:#dc2626;border:1px solid #fecaca;padding:6px 10px;font-size:12px}.btn:disabled{opacity:.5}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;margin-bottom:12px}
.row{display:grid;grid-template-columns:1fr 120px;gap:8px;align-items:center;padding:9px 12px;border-bottom:1px solid #f1f5f9}
.row.changed{background:#eef2ff}.row .nm{font-weight:700;font-size:13.5px;line-height:1.3}.row .cd{font-size:11.5px;color:#64748b;margin-top:2px}
.row .cd b{color:#4f46e5}.row input{width:100%;padding:9px 8px;text-align:right;font-size:16px;border:1px solid #cbd5e1;border-radius:8px;font-family:inherit;font-variant-numeric:tabular-nums}
.grp{padding:8px 12px;background:#f8fafc;font-size:12px;font-weight:800;color:#334155}
.foot{position:fixed;left:0;right:0;bottom:0;background:rgba(255,255,255,.96);border-top:1px solid #e2e8f0;padding:10px 14px;display:flex;gap:8px;align-items:center;justify-content:space-between;backdrop-filter:blur(4px)}
.toast{position:fixed;left:50%;bottom:80px;transform:translateX(-50%);background:#0f172a;color:#fff;padding:10px 16px;border-radius:10px;font-size:13px;display:none;z-index:9}
.hist{font-size:12px;color:#475569}.hist div{padding:5px 12px;border-bottom:1px solid #f1f5f9}
.note{font-size:12px;color:#64748b;margin:0 0 10px;line-height:1.5}
.chip{display:inline-block;font-size:10.5px;padding:1px 7px;border-radius:6px;background:#f1f5f9;color:#475569;margin-left:4px}
.chip.use{background:#fef3c7;color:#92400e}.chip.prod{background:#dcfce7;color:#166534}.chip.out{background:#fee2e2;color:#991b1b}
.inrow{display:grid;grid-template-columns:90px 1fr 90px 60px auto;gap:6px;align-items:center;padding:8px 12px;border-bottom:1px solid #f1f5f9;font-size:12.5px}
.inform{display:grid;grid-template-columns:130px 1fr 100px 70px auto;gap:6px;align-items:center;padding:10px 12px;background:#f8fafc}
.inform input,.inform select{padding:8px;border:1px solid #cbd5e1;border-radius:8px;font-size:13px;font-family:inherit;width:100%}
.dates{font-size:11px;color:#64748b;margin:6px 0 10px}.dates span{display:inline-block;margin:2px 4px 2px 0;padding:1px 6px;border-radius:5px;background:#e0e7ff;color:#3730a3;cursor:pointer}
@media(max-width:560px){.row{grid-template-columns:1fr 96px}.inform{grid-template-columns:1fr 1fr}.inrow{grid-template-columns:80px 1fr 70px auto}}
</style></head><body>
<div class="top"><h1>📦 {{ vendor }} 원·부자재 재고 입력</h1><p>매홍 L&F 구매팀 · 저장 즉시 매홍에 반영됩니다. 월말에 이 내용으로 마감 파일이 만들어집니다.</p>
  <div class="tabs"><div class="tab on" data-t="daily" onclick="tab('daily')">📅 일별 사용·생산·출고</div><div class="tab" data-t="in" onclick="tab('in')">📥 입고 등록</div><div class="tab" data-t="stock" onclick="tab('stock')">📋 재고 실사</div></div>
</div>
<div class="wrap">
  <div class="bar"><input type="text" id="who" placeholder="입력자 이름 (예: 홍길동)" maxlength="30"><input type="text" id="q" placeholder="품번/품명 검색" oninput="render()"></div>

  <div id="pane-daily">
    <p class="note">날짜를 고른 뒤 그날의 <b>사용량(원·부자재)</b>, <b>생산량</b>, <b>출고량</b>을 입력하세요. 숫자는 모두 양수로 적습니다(출고도 양수). 비운 칸은 저장되지 않고, 0을 넣으면 그날 값이 지워집니다.</p>
    <div class="bar"><input type="date" id="ddate" onchange="loadDaily()"><button class="btn g" onclick="shiftDay(-1)">◀ 전날</button><button class="btn g" onclick="shiftDay(1)">다음날 ▶</button><span id="dsum" style="font-size:12px;color:#64748b"></span></div>
    <div class="dates" id="dates"></div>
    <div class="card" id="dlist"></div>
  </div>

  <div id="pane-in" style="display:none">
    <p class="note">매홍(또는 다른 업체)에서 <b>받은 원·부자재</b>를 등록하세요. 입고일지 시트에 그대로 기록됩니다.</p>
    <div class="card"><div class="inform"><input type="date" id="idate"><select id="icode"></select><input type="number" id="iqty" placeholder="수량" min="0" step="any" inputmode="decimal"><select id="iunit"><option>ea</option><option>kg</option><option>box</option><option>롤</option></select><button class="btn p" onclick="addIn()">추가</button></div>
      <div id="ilist"></div></div>
  </div>

  <div id="pane-stock" style="display:none">
    <p class="note">월말 실사 등 <b>현재 보유 수량을 직접</b> 알려줄 때 사용합니다. 값을 바꾼 품목만 저장됩니다. 저장하면 그 시점부터 이 값이 기준이 됩니다.</p>
    <div class="bar"><button class="btn g" onclick="fillAll()">현재값 채우기</button><button class="btn g" onclick="confirmSame()">변동 없음 확인</button></div>
    <div class="card" id="list"></div>
  </div>

  <div class="card"><div class="grp">최근 입력 이력</div><div class="hist" id="hist"></div></div>
</div>
<div class="foot"><span id="cnt" style="font-size:12px;color:#64748b">변경 0건</span><button class="btn p" id="save" onclick="save()">저장</button></div>
<div class="toast" id="toast"></div>
<script>
const TOKEN = {{ token|tojson }};
let TAB = 'daily', ITEMS = [], VALS = {}, DROWS = [], DVALS = {}, INB = [];
const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const fmt = n => n == null ? '-' : (Math.round(n * 100) / 100).toLocaleString();
const today = () => { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); };
function toast(m) { const t = document.getElementById('toast'); t.textContent = m; t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 2600); }
function who() { const w = document.getElementById('who').value.trim(); try { localStorage.setItem('mhVendorWho', w); } catch (e) {} return w; }
function tab(t) { TAB = t; document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x.dataset.t === t)); ['daily', 'in', 'stock'].forEach(p => document.getElementById('pane-' + p).style.display = p === t ? '' : 'none'); document.getElementById('save').style.display = t === 'in' ? 'none' : ''; upd(); }
async function init() {
  try { document.getElementById('who').value = localStorage.getItem('mhVendorWho') || ''; } catch (e) {}
  document.getElementById('ddate').value = today(); document.getElementById('idate').value = today();
  await loadStock(); await loadDaily(); loadHist();
}
// ── 재고 실사 ──
async function loadStock() {
  const d = await (await fetch('/api/v/' + TOKEN + '/items')).json(); ITEMS = d.items || []; VALS = {};
  const sel = document.getElementById('icode'); sel.innerHTML = ITEMS.map(x => '<option value="' + esc(x.code) + '">' + esc(x.code) + ' ' + esc(x.name) + '</option>').join('');
  render();
}
function render() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const L = document.getElementById('list'); let html = ''; let cur = '';
  ITEMS.filter(x => !q || (x.code + ' ' + x.name).toLowerCase().includes(q)).forEach(x => {
    if (x.cls !== cur) { cur = x.cls; html += '<div class="grp">' + cur + '</div>'; }
    const v = VALS[x.code]; const changed = v != null && v !== '' && Number(v) !== Number(x.qty);
    html += '<div class="row' + (changed ? ' changed' : '') + '"><div><div class="nm">' + esc(x.name) + '</div><div class="cd"><b>' + esc(x.code) + '</b>' + (x.spec ? ' · ' + esc(x.spec) : '') + ' · 현재 <b>' + fmt(x.qty) + '</b>' + (x.last_at ? ' <span style="color:#94a3b8">(' + x.last_at + (x.last_by ? ' ' + esc(x.last_by) : '') + ')</span>' : '') + '</div></div>'
      + '<input type="number" inputmode="decimal" min="0" step="any" placeholder="' + fmt(x.qty) + '" value="' + (v == null ? '' : v) + '" oninput="VALS[' + JSON.stringify(x.code) + ']=this.value;upd()"></div>';
  });
  L.innerHTML = html || '<div style="padding:16px;color:#94a3b8">품목이 없습니다</div>';
  renderDaily(); upd();
}
function changedStock() { return ITEMS.filter(x => VALS[x.code] != null && VALS[x.code] !== '' && Number(VALS[x.code]) !== Number(x.qty)).map(x => ({ kind: 'set', code: x.code, qty: Number(VALS[x.code]) })); }
function fillAll() { ITEMS.forEach(x => { if (VALS[x.code] == null || VALS[x.code] === '') VALS[x.code] = x.qty; }); render(); }
// ── 일별 ──
function shiftDay(n) { const d = new Date(document.getElementById('ddate').value); d.setDate(d.getDate() + n); document.getElementById('ddate').value = d.toISOString().slice(0, 10); loadDaily(); }
async function loadDaily() {
  const date = document.getElementById('ddate').value || today();
  const d = await (await fetch('/api/v/' + TOKEN + '/daily?date=' + date)).json();
  DROWS = d.rows || []; DVALS = {}; INB = d.inbound || [];
  document.getElementById('dates').innerHTML = (d.filled_dates || []).length ? '이번 달 입력된 날: ' + d.filled_dates.map(x => '<span onclick="document.getElementById(\x27ddate\x27).value=\x27' + x + '\x27;loadDaily()">' + x.slice(5) + '</span>').join('') : '이번 달 입력된 날이 아직 없습니다';
  renderDaily(); renderIn();
}
function renderDaily() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const L = document.getElementById('dlist'); let html = ''; let cur = ''; const names = { use: '원·부자재 사용량', prod: '생산량', out: '출고량', delta: '증감' };
  DROWS.filter(x => !q || (x.code + ' ' + x.name + ' ' + x.spec).toLowerCase().includes(q)).forEach(x => {
    const k = x.code + '|' + x.spec; if (x.type !== cur) { cur = x.type; html += '<div class="grp">' + names[cur] + '</div>'; }
    const v = DVALS[k]; const changed = v != null && v !== '' && Number(v) !== Number(x.value == null ? NaN : x.value);
    html += '<div class="row' + (changed ? ' changed' : '') + '"><div><div class="nm">' + esc(x.name) + '<span class="chip ' + x.type + '">' + esc(x.label) + '</span></div><div class="cd"><b>' + esc(x.code) + '</b>' + (x.spec ? ' · ' + esc(x.spec) : '') + (x.dest ? ' · ' + esc(x.dest) : '') + ' · 이달 누계 <b>' + fmt(x.month_total) + '</b> (' + x.month_days + '일)' + (x.by ? ' <span style="color:#94a3b8">' + esc(x.by) + '</span>' : '') + '</div></div>'
      + '<input type="number" inputmode="decimal" ' + (x.type === 'delta' ? '' : 'min="0" ') + 'step="any" placeholder="' + (x.value == null ? '' : fmt(x.value)) + '" value="' + (v == null ? '' : v) + '" oninput="DVALS[' + JSON.stringify(k) + ']=this.value;upd()"></div>';
  });
  L.innerHTML = html || '<div style="padding:16px;color:#94a3b8">품목이 없습니다</div>';
  const filled = DROWS.filter(x => x.value != null && x.value !== 0).length; document.getElementById('dsum').textContent = filled ? '이 날 입력 ' + filled + '건' : '';
}
function changedDaily() { const date = document.getElementById('ddate').value; return DROWS.filter(x => { const v = DVALS[x.code + '|' + x.spec]; return v != null && v !== '' && Number(v) !== Number(x.value == null ? NaN : x.value); }).map(x => ({ kind: 'day', code: x.code, spec: x.spec, date, qty: Number(DVALS[x.code + '|' + x.spec]) })); }
// ── 입고 ──
function renderIn() {
  const L = document.getElementById('ilist');
  L.innerHTML = INB.length ? INB.map(e => '<div class="inrow"><span>' + e.date.slice(5) + '</span><span><b style="color:#4f46e5">' + esc(e.code) + '</b> ' + esc(e.name) + '</span><span style="text-align:right;font-weight:700">' + fmt(e.qty) + '</span><span>' + esc(e.unit) + '</span><button class="btn d" onclick="delIn(' + JSON.stringify(e.id) + ',' + JSON.stringify(e.code) + ')">삭제</button></div>').join('') : '<div style="padding:14px;color:#94a3b8;font-size:12px">이번 달 등록된 입고가 없습니다</div>';
}
async function addIn() {
  const w = who(); if (!w) { toast('입력자 이름을 적어 주세요'); return; }
  const it = { kind: 'in', date: document.getElementById('idate').value, code: document.getElementById('icode').value, qty: Number(document.getElementById('iqty').value), unit: document.getElementById('iunit').value };
  if (!(it.qty > 0)) { toast('수량을 입력하세요'); return; }
  const d = await post({ name: w, items: [it] }); if (d.ok) { toast('입고 등록'); document.getElementById('iqty').value = ''; loadDaily(); loadHist(); } else toast(d.error || '실패');
}
async function delIn(id, code) { const w = who(); if (!w) { toast('입력자 이름을 적어 주세요'); return; } const d = await post({ name: w, items: [{ kind: 'in_del', id, code }] }); if (d.ok) { toast('삭제됨'); loadDaily(); loadHist(); } }
// ── 공통 ──
async function post(body) { const r = await fetch('/api/v/' + TOKEN + '/submit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); return await r.json(); }
function upd() { const n = TAB === 'stock' ? changedStock().length : TAB === 'daily' ? changedDaily().length : 0; document.getElementById('cnt').textContent = TAB === 'in' ? '입고 ' + INB.length + '건' : '변경 ' + n + '건'; }
async function save() {
  const items = TAB === 'stock' ? changedStock() : changedDaily(); if (!items.length) { toast('변경된 항목이 없습니다'); return; }
  const w = who(); if (!w) { toast('입력자 이름을 적어 주세요'); document.getElementById('who').focus(); return; }
  document.getElementById('save').disabled = true;
  try { const d = await post({ name: w, items }); if (d.ok) { toast('저장 완료 · ' + d.saved + '건'); if (TAB === 'stock') await loadStock(); else await loadDaily(); loadHist(); } else toast('저장 실패: ' + (d.error || '')); }
  catch (e) { toast('오류: ' + e.message); }
  document.getElementById('save').disabled = false;
}
async function confirmSame() { const w = who(); if (!w) { toast('입력자 이름을 적어 주세요'); return; } const d = await post({ name: w, items: [], confirm: true }); toast(d.ok ? '변동 없음으로 기록했습니다' : '기록 실패'); loadHist(); }
async function loadHist() {
  const d = await (await fetch('/api/v/' + TOKEN + '/history')).json(); const H = document.getElementById('hist');
  const kd = { set: '실사', day: '일별', in: '입고', in_del: '입고삭제', confirm: '변동없음' };
  H.innerHTML = (d.items || []).length ? d.items.map(h => '<div>' + h.at + ' · <span class="chip">' + (kd[h.kind] || h.kind) + '</span> ' + (h.kind === 'confirm' ? '<b>변동 없음 확인</b>' : '<b>' + esc(h.code) + '</b> ' + esc(h.name) + (h.date ? ' ' + h.date.slice(5) : '') + (h.kind === 'in_del' ? '' : ' → ' + fmt(h.qty))) + (h.by ? ' <span style="color:#94a3b8">' + esc(h.by) + '</span>' : '') + '</div>').join('') : '<div style="color:#94a3b8">아직 입력 이력이 없습니다</div>';
}
init();
</script></body></html>'''

# V3 (2026-09-16, 사용자 "내가 작성하는 엑셀 양식 그대로"): 거래처 엑셀 시트와 같은 월간 그리드(행=품목, 열=기초·입고·현재고·1~31일)
# + 입고일지 시트 양식 + 재고 실사. 파일(마감)에 이미 있는 값은 회색으로 깔리고, 거래처 입력값은 파랑.
VENDOR_PORTAL_TEMPLATE = r'''<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ vendor }} 재고일지 - 매홍</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box} body{font-family:'Noto Sans KR',sans-serif;background:#f4f5fa;margin:0;color:#0f172a;padding-bottom:64px}
.top{background:linear-gradient(135deg,#1e1b4b,#312e81);color:#fff;padding:12px 18px 10px}
.top h1{margin:0;font-size:17px;font-weight:800}.top p{margin:3px 0 8px;font-size:12px;opacity:.85}
.tabs{display:flex;gap:6px}.tab{padding:8px 14px;border-radius:9px;background:rgba(255,255,255,.12);color:#fff;font-weight:700;font-size:13px;cursor:pointer}.tab.on{background:#fff;color:#312e81}
.wrap{padding:10px 14px;max-width:100%}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
.bar input[type=text],.bar input[type=date],.bar select{padding:7px 10px;border:1px solid #cbd5e1;border-radius:8px;font-size:13px;font-family:inherit;background:#fff}
.btn{padding:8px 14px;border-radius:8px;border:0;font-weight:800;font-size:13px;cursor:pointer;font-family:inherit}
.btn.p{background:#4f46e5;color:#fff}.btn.g{background:#fff;color:#475569;border:1px solid #cbd5e1}.btn.d{background:#fff;color:#dc2626;border:1px solid #fecaca;padding:5px 9px;font-size:12px}.btn:disabled{opacity:.5}
.gridwrap{overflow:auto;max-height:calc(100vh - 230px);border:1px solid #cbd5e1;border-radius:10px;background:#fff}
table.grid{border-collapse:separate;border-spacing:0;font-size:12px;min-width:1400px}
.grid th,.grid td{border-right:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;padding:0;height:30px;white-space:nowrap}
.grid th{background:#1e293b;color:#fff;font-weight:700;font-size:11.5px;position:sticky;top:0;z-index:3;padding:0 6px;text-align:center}
.grid th.wk{background:#7f1d1d}.grid th.sat{background:#1e3a8a}
.grid td.txt{padding:0 8px;background:#f8fafc}
.grid .c1{position:sticky;left:0;z-index:2;min-width:70px;background:#f8fafc}.grid .c2{position:sticky;left:70px;z-index:2;min-width:64px;background:#f8fafc}
.grid .c3{position:sticky;left:134px;z-index:2;min-width:88px;background:#f8fafc}.grid .c4{position:sticky;left:222px;z-index:2;min-width:230px;max-width:230px;overflow:hidden;text-overflow:ellipsis;background:#f8fafc;box-shadow:2px 0 0 #cbd5e1}
.grid th.c1,.grid th.c2,.grid th.c3,.grid th.c4{z-index:4;background:#1e293b}
.grid td.num{text-align:right;padding:0 6px;font-variant-numeric:tabular-nums;background:#f8fafc;color:#334155}
.grid td.cur{font-weight:800;color:#0f172a;background:#fefce8}
.grid td.day{min-width:56px}
.grid td.day input{width:56px;height:29px;border:0;background:transparent;text-align:right;padding:0 5px;font-size:12px;font-family:inherit;font-variant-numeric:tabular-nums;outline:none}
.grid td.day input:focus{background:#eef2ff;box-shadow:inset 0 0 0 2px #4f46e5}
.grid td.day.file input{color:#64748b}
.grid td.day.entry input{color:#1d4ed8;font-weight:700;background:#dbeafe}
.grid td.day.changed input{background:#fef08a;color:#0f172a;font-weight:700}
.grid tr.prod td.txt{background:#f0fdf4}.grid tr.out td.txt{background:#fff1f2}
.grid td.tot{text-align:right;padding:0 6px;font-weight:700;background:#f1f5f9}
.legend{font-size:11px;color:#64748b;margin:6px 0}.legend span{display:inline-block;padding:1px 7px;border-radius:5px;margin-right:6px}
.foot{position:fixed;left:0;right:0;bottom:0;background:rgba(255,255,255,.96);border-top:1px solid #e2e8f0;padding:9px 14px;display:flex;gap:8px;align-items:center;justify-content:space-between;backdrop-filter:blur(4px);z-index:5}
.toast{position:fixed;left:50%;bottom:70px;transform:translateX(-50%);background:#0f172a;color:#fff;padding:10px 16px;border-radius:10px;font-size:13px;display:none;z-index:9}
/* 입고일지 — 엑셀 시트와 동일 열: 입고일자|품번|제품명|규격|원/부자재 업체명|입고수량|입고단위|입고처|출고처|유통기한 */
table.inbg{min-width:1180px}
.inbg th{position:sticky;top:0}
.inbg td{height:30px}
.inbg td.y{background:#FFF2CC}
.inbg td input,.inbg td select{width:100%;height:29px;border:0;background:transparent;padding:0 6px;font-size:12px;font-family:inherit;outline:none}
.inbg td input:focus,.inbg td select:focus{background:#eef2ff;box-shadow:inset 0 0 0 2px #4f46e5}
.inbg td.ro{padding:0 6px;color:#334155;background:#f8fafc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:420px}
.inbg tr.saved td{background:#f1f5f9}.inbg tr.saved td.y{background:#f1f5f9;color:#1d4ed8;font-weight:700}
.inbg td.num{text-align:right}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;margin-bottom:12px}
.row{display:grid;grid-template-columns:1fr 120px;gap:8px;align-items:center;padding:9px 12px;border-bottom:1px solid #f1f5f9}
.row.changed{background:#eef2ff}.row .nm{font-weight:700;font-size:13.5px}.row .cd{font-size:11.5px;color:#64748b;margin-top:2px}.row .cd b{color:#4f46e5}
.row input{width:100%;padding:8px;text-align:right;font-size:15px;border:1px solid #cbd5e1;border-radius:8px;font-family:inherit}
.grp{padding:8px 12px;background:#f8fafc;font-size:12px;font-weight:800;color:#334155}
.hist{font-size:12px;color:#475569}.hist div{padding:5px 12px;border-bottom:1px solid #f1f5f9}
.note{font-size:12px;color:#64748b;margin:0 0 8px;line-height:1.5}
</style></head><body>
<div class="top"><h1>📄 {{ vendor }} 원·부자재 재고일지</h1><p>매홍 L&F 구매팀 · 엑셀 양식과 같은 화면입니다. 숫자만 채우고 저장하면 매홍 마감 파일에 그대로 반영됩니다.</p>
  <div class="tabs"><div class="tab on" data-t="sheet" onclick="tab('sheet')">📅 재고일지 (일별)</div><div class="tab" data-t="in" onclick="tab('in')">📥 입고일지</div><div class="tab" data-t="stock" onclick="tab('stock')">📋 재고 실사</div></div>
</div>
<div class="wrap">
  <div class="bar"><input type="text" id="who" placeholder="작성자 이름" maxlength="30" style="width:150px"><select id="ym" onchange="loadSheet()"></select><input type="text" id="q" placeholder="품번/품명 검색" oninput="renderSheet();renderStock()" style="width:180px"><span id="note" class="note" style="margin:0"></span></div>

  <div id="pane-sheet">
    <div class="legend"><span style="background:#f1f5f9;color:#64748b">회색 = 마감 파일에 이미 있는 값</span><span style="background:#dbeafe;color:#1d4ed8">파랑 = 이 화면에서 입력한 값</span><span style="background:#fef08a">노랑 = 저장 전 변경</span> 원·부자재 사용량과 생산량은 양수, 출고는 음수(−)로 엑셀과 동일하게 적습니다. 0을 넣으면 그날 값이 지워집니다.</div>
    <div class="gridwrap"><table class="grid" id="grid"></table></div>
  </div>

  <div id="pane-in" style="display:none">
    <p class="note" style="font-weight:700;color:#b45309">▶ 노랑색 부분만 작성 부탁드립니다.</p>
    <div class="gridwrap" style="max-height:calc(100vh - 240px)"><table class="grid inbg" id="ingrid"></table></div>
    <div class="bar" style="margin-top:8px"><button class="btn g" onclick="addInRows(5)">+ 빈 행 5개 추가</button><span class="note" style="margin:0">제품명·규격은 품번을 넣으면 자동으로 채워집니다. 품번은 목록에서 고르거나 직접 입력하세요. 아래 [저장]을 누르면 입력한 행이 기록됩니다.</span></div>
    <datalist id="codelist"></datalist>
  </div>

  <div id="pane-stock" style="display:none">
    <p class="note">월말 실사 등 <b>현재 보유 수량을 직접</b> 알려줄 때만 사용합니다. 값을 바꾼 품목만 저장됩니다.</p>
    <div class="bar"><button class="btn g" onclick="confirmSame()">변동 없음 확인</button></div>
    <div class="card" id="list"></div>
  </div>

  <div class="card" style="margin-top:12px"><div class="grp">최근 입력 이력</div><div class="hist" id="hist"></div></div>
</div>
<div class="foot"><span id="cnt" style="font-size:12px;color:#64748b">변경 0건</span><span><button class="btn g" onclick="reloadAll()">새로고침</button> <button class="btn p" id="save" onclick="save()">저장</button></span></div>
<div class="toast" id="toast"></div>
<script>
const TOKEN = {{ token|tojson }};
let TAB = 'sheet', SHEET = null, CELL = {}, ITEMS = [], VALS = {};
const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const fmt = n => n == null || n === '' ? '' : (Math.round(n * 100) / 100).toLocaleString();
function toast(m) { const t = document.getElementById('toast'); t.textContent = m; t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 2600); }
function who() { const w = document.getElementById('who').value.trim(); try { localStorage.setItem('mhVendorWho', w); } catch (e) {} return w; }
function tab(t) { TAB = t; document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x.dataset.t === t)); ['sheet', 'in', 'stock'].forEach(p => document.getElementById('pane-' + p).style.display = p === t ? '' : 'none'); upd(); }
function ymOpts() { const s = document.getElementById('ym'); const d = new Date(); const opts = []; for (let i = 0; i < 3; i++) { const y = d.getFullYear(), m = d.getMonth() + 1; opts.push(String(y) + String(m).padStart(2, '0')); d.setMonth(d.getMonth() - 1); } s.innerHTML = opts.map((o, i) => '<option value="' + o + '"' + (i === 0 ? ' selected' : '') + '>' + o.slice(0, 4) + '년 ' + parseInt(o.slice(4)) + '월</option>').join(''); }
async function init() { try { document.getElementById('who').value = localStorage.getItem('mhVendorWho') || ''; } catch (e) {} ymOpts(); await reloadAll(); }
async function reloadAll() { CELL = {}; VALS = {}; await Promise.all([loadSheet(), loadStock()]); loadHist(); }
// ── 재고일지 그리드 ──
async function loadSheet() {
  const ym = document.getElementById('ym').value;
  SHEET = await (await fetch('/api/v/' + TOKEN + '/sheet?ym=' + ym)).json(); CELL = {};
  document.getElementById('note').textContent = (SHEET.file_month_matches ? '기준 파일: ' + SHEET.source : '※ 매홍 마감 파일이 아직 ' + parseInt(ym.slice(4)) + '월이 아니라 파일 값은 비어 있습니다') + ' · 품목 ' + SHEET.rows.length + '개';
  renderSheet();
  INROWS = 8; renderIn();
}
function cellVal(r, d) { const k = r.code + '|' + r.spec + '|' + d; if (k in CELL) return CELL[k]; if (d in r.daily_entry) return r.daily_entry[d] === 0 ? '' : r.daily_entry[d]; if (d in r.daily_file) return r.daily_file[d]; return ''; }
function cellCls(r, d) { const k = r.code + '|' + r.spec + '|' + d; return (k in CELL) ? 'changed' : (d in r.daily_entry && r.daily_entry[d] !== 0) ? 'entry' : (d in r.daily_file && !(d in r.daily_entry)) ? 'file' : ''; }
function rowSum(r) { let s = 0; for (let d = 1; d <= SHEET.ndays; d++) { const v = cellVal(r, d); if (v !== '' && v != null && !isNaN(v)) s += Number(v); } return s; }
function rowCur(r) { const inbEntry = (SHEET.inbound || []).filter(i => i.code === r.code.toUpperCase()).reduce((a, i) => a + Number(i.qty || 0), 0); const inb = r.inb_file + inbEntry; const s = rowSum(r); return r.type === 'use' ? r.base + inb - s : r.base + inb + s; }
function renderSheet() {
  if (!SHEET) return; const q = document.getElementById('q').value.trim().toLowerCase(); const ym = SHEET.ym; const y = +ym.slice(0, 4), m = +ym.slice(4) - 1;
  let h = '<thead><tr><th class="c1">납품처</th><th class="c2">품번</th><th class="c3">규격</th><th class="c4">품명</th><th>입수</th><th>기초재고</th><th>입고</th><th>현재고</th>';
  for (let d = 1; d <= SHEET.ndays; d++) { const wd = new Date(y, m, d).getDay(); h += '<th class="' + (wd === 0 ? 'wk' : wd === 6 ? 'sat' : '') + '">' + d + '일<br><span style="font-weight:400;opacity:.8">' + '일월화수목금토'[wd] + '</span></th>'; }
  h += '<th>합계</th></tr></thead><tbody>';
  SHEET.rows.filter(r => !q || (r.code + ' ' + r.name + ' ' + r.spec).toLowerCase().includes(q)).forEach(r => {
    h += '<tr class="' + r.type + '"><td class="txt c1">' + esc(r.dest) + '</td><td class="txt c2"><b>' + esc(r.code) + '</b></td><td class="txt c3">' + esc(r.spec) + '</td><td class="txt c4" title="' + esc(r.name) + '">' + esc(r.name) + '</td>'
      + '<td class="num">' + esc(r.ipsu) + '</td><td class="num">' + fmt(r.base) + '</td><td class="num">' + fmt(r.inb_file) + '</td><td class="num cur" id="cur-' + esc(r.code) + '-' + esc(r.spec).replace(/[^\w가-힣]/g, '_') + '">' + fmt(rowCur(r)) + '</td>';
    for (let d = 1; d <= SHEET.ndays; d++) {
      const k = r.code + '|' + r.spec + '|' + d; const v = cellVal(r, d);
      const cls = cellCls(r, d);
      h += '<td class="day ' + cls + '"><input type="number" step="any" inputmode="decimal" value="' + (v === '' ? '' : v) + '" data-k="' + esc(k) + '" title="' + (r.entry_by[d] ? '입력: ' + esc(r.entry_by[d]) : '') + '" onchange="onCell(this)" onkeydown="navCell(event,this)"></td>';
    }
    h += '<td class="tot">' + fmt(rowSum(r)) + '</td></tr>';
  });
  document.getElementById('grid').innerHTML = h + '</tbody>'; upd();
}
function onCell(inp) { const k = inp.dataset.k; const v = inp.value.trim(); const [code, spec, d] = k.split('|'); const r = SHEET.rows.find(x => x.code === code && x.spec === spec); const base = (d in r.daily_entry) ? r.daily_entry[d] : (d in r.daily_file) ? r.daily_file[d] : '';
  if (v === '' && base === '') { delete CELL[k]; } else if (v !== '' && base !== '' && Number(v) === Number(base)) { delete CELL[k]; } else { CELL[k] = v === '' ? 0 : Number(v); }
  inp.closest('td').className = 'day ' + cellCls(r, d);
  const tr = inp.closest('tr'); tr.querySelector('td.cur').textContent = fmt(rowCur(r)); tr.querySelector('td.tot').textContent = fmt(rowSum(r)); upd(); }
function navCell(e, inp) { if (e.key !== 'Enter' && e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return; e.preventDefault(); const td = inp.closest('td'); const idx = [...td.parentNode.children].indexOf(td); const tr = e.key === 'ArrowUp' ? td.parentNode.previousElementSibling : td.parentNode.nextElementSibling; if (tr && tr.children[idx]) { const n = tr.children[idx].querySelector('input'); if (n) { n.focus(); n.select(); } } }
function changedSheet() { return Object.keys(CELL).map(k => { const [code, spec, d] = k.split('|'); const r = SHEET.rows.find(x => x.code === code && x.spec === spec); const v = CELL[k]; return { kind: 'day', code, spec, date: SHEET.ym.slice(0, 4) + '-' + SHEET.ym.slice(4) + '-' + String(d).padStart(2, '0'), qty: r.type === 'delta' ? v : Math.abs(v) }; }); }
// ── 입고일지 (엑셀 시트 양식: 저장된 행 + 빈 입력행) ──
let INROWS = 8;
const VENDOR_NAME = {{ vendor|tojson }};
function codeInfo(c) { c = (c || '').trim().toUpperCase(); const r = (SHEET ? SHEET.rows : []).find(x => x.code.toUpperCase() === c); return r || null; }
function renderIn() {
  const L = SHEET ? SHEET.inbound : [];
  document.getElementById('codelist').innerHTML = (SHEET ? SHEET.rows : []).filter(r => /^[A-D]/i.test(r.code)).map(r => '<option value="' + esc(r.code) + '">' + esc(r.name) + '</option>').join('');
  let h = '<thead><tr><th style="min-width:118px">입고일자</th><th style="min-width:110px">품번</th><th style="min-width:380px">제품명</th><th style="min-width:110px">규격</th><th style="min-width:160px">원/부자재 업체명</th><th style="min-width:100px">입고수량</th><th style="min-width:80px">입고단위</th><th style="min-width:100px">입고처</th><th style="min-width:110px">출고처</th><th style="min-width:118px">유통기한</th><th style="min-width:60px"></th></tr></thead><tbody>';
  L.forEach(e => { h += '<tr class="saved"><td class="y ro">' + e.date + '</td><td class="y ro">' + esc(e.code) + '</td><td class="ro" title="' + esc(e.name) + '">' + esc(e.name) + '</td><td class="ro">' + esc(e.spec) + '</td><td class="y ro">' + esc(e.supplier || '') + '</td><td class="y ro num">' + fmt(e.qty) + '</td><td class="ro">' + esc(e.unit) + '</td><td class="ro">' + esc(VENDOR_NAME) + '</td><td class="ro">' + esc(e.dest || '') + '</td><td class="ro">' + esc(e.exp || '') + '</td><td style="text-align:center"><button class="btn d" onclick="delIn(' + JSON.stringify(e.id) + ',' + JSON.stringify(e.code) + ')">삭제</button></td></tr>'; });
  for (let i = 0; i < INROWS; i++) h += inBlankRow(i);
  document.getElementById('ingrid').innerHTML = h + '</tbody>'; upd();
}
function inBlankRow(i) { return '<tr class="blank" data-i="' + i + '"><td class="y"><input type="date" data-f="date" onkeydown="navCell(event,this)"></td><td class="y"><input type="text" list="codelist" data-f="code" placeholder="품번" oninput="inCode(this)" onkeydown="navCell(event,this)" style="text-transform:uppercase"></td><td class="ro" data-f="name"></td><td class="ro" data-f="spec"></td><td class="y"><input type="text" data-f="supplier" placeholder="업체명" onkeydown="navCell(event,this)"></td><td class="y"><input type="number" step="any" min="0" inputmode="decimal" data-f="qty" onkeydown="navCell(event,this)" oninput="upd()" style="text-align:right"></td><td class="y"><select data-f="unit"><option>ea</option><option>kg</option><option>box</option><option>롤</option></select></td><td class="ro">' + esc(VENDOR_NAME) + '</td><td class="y"><input type="text" data-f="dest" onkeydown="navCell(event,this)"></td><td class="y"><input type="date" data-f="exp" onkeydown="navCell(event,this)"></td><td></td></tr>'; }
function addInRows(n) { const tb = document.querySelector('#ingrid tbody'); for (let i = 0; i < n; i++) { tb.insertAdjacentHTML('beforeend', inBlankRow(INROWS + i)); } INROWS += n; }
function inCode(inp) { const tr = inp.closest('tr'); const r = codeInfo(inp.value); tr.querySelector('[data-f=name]').textContent = r ? r.name : (inp.value ? '(품번 없음)' : ''); tr.querySelector('[data-f=spec]').textContent = r ? r.spec : ''; if (r) tr.querySelector('[data-f=unit]').value = r.unit === 'kg' ? 'kg' : 'ea'; upd(); }
function pendingIn() { const out = []; document.querySelectorAll('#ingrid tr.blank').forEach(tr => { const g = f => { const el = tr.querySelector('[data-f=' + f + ']'); return el ? (el.value || '').trim() : ''; }; const code = g('code').toUpperCase(), qty = Number(g('qty')), date = g('date'); if (!code && !qty && !date) return; if (!code || !(qty > 0) || !date || !codeInfo(code)) { out.push({ invalid: true }); return; } out.push({ kind: 'in', date, code, qty, unit: g('unit'), supplier: g('supplier'), dest: g('dest'), exp: g('exp') }); }); return out; }
async function delIn(id, code) { const w = who(); if (!w) { toast('작성자 이름을 적어 주세요'); return; } const d = await post({ name: w, items: [{ kind: 'in_del', id, code }] }); if (d.ok) { toast('삭제됨'); await loadSheet(); loadHist(); } }
// ── 재고 실사 ──
async function loadStock() { const d = await (await fetch('/api/v/' + TOKEN + '/items')).json(); ITEMS = d.items || []; VALS = {}; renderStock(); }
function renderStock() { const q = document.getElementById('q').value.trim().toLowerCase(); const L = document.getElementById('list'); let html = ''; let cur = '';
  ITEMS.filter(x => !q || (x.code + ' ' + x.name).toLowerCase().includes(q)).forEach(x => { if (x.cls !== cur) { cur = x.cls; html += '<div class="grp">' + cur + '</div>'; } const v = VALS[x.code]; const ch = v != null && v !== '' && Number(v) !== Number(x.qty);
    html += '<div class="row' + (ch ? ' changed' : '') + '"><div><div class="nm">' + esc(x.name) + '</div><div class="cd"><b>' + esc(x.code) + '</b> · 현재 <b>' + fmt(x.qty) + '</b>' + (x.last_at ? ' <span style="color:#94a3b8">(' + x.last_at + ' ' + esc(x.last_by) + ')</span>' : '') + '</div></div><input type="number" inputmode="decimal" min="0" step="any" placeholder="' + fmt(x.qty) + '" value="' + (v == null ? '' : v) + '" oninput="VALS[' + JSON.stringify(x.code) + ']=this.value;upd()"></div>'; });
  L.innerHTML = html || '<div style="padding:16px;color:#94a3b8">품목이 없습니다</div>'; upd(); }
function changedStock() { return ITEMS.filter(x => VALS[x.code] != null && VALS[x.code] !== '' && Number(VALS[x.code]) !== Number(x.qty)).map(x => ({ kind: 'set', code: x.code, qty: Number(VALS[x.code]) })); }
async function confirmSame() { const w = who(); if (!w) { toast('작성자 이름을 적어 주세요'); return; } const d = await post({ name: w, items: [], confirm: true }); toast(d.ok ? '변동 없음으로 기록했습니다' : '기록 실패'); loadHist(); }
// ── 공통 ──
async function post(body) { const r = await fetch('/api/v/' + TOKEN + '/submit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); return await r.json(); }
function upd() { if (TAB === 'in') { const p = pendingIn(); const bad = p.filter(x => x.invalid).length; document.getElementById('cnt').textContent = '저장된 입고 ' + (SHEET ? SHEET.inbound.length : 0) + '건 · 새 입력 ' + (p.length - bad) + '행' + (bad ? ' · 미완성 ' + bad + '행(일자·품번·수량 필요)' : ''); return; } const n = TAB === 'stock' ? changedStock().length : Object.keys(CELL).length; document.getElementById('cnt').textContent = '변경 ' + n + '칸'; }
async function save() { let items; if (TAB === 'in') { const p = pendingIn(); if (p.some(x => x.invalid)) { toast('일자·품번·수량이 비었거나 품번이 목록에 없는 행이 있습니다'); return; } items = p; } else items = TAB === 'stock' ? changedStock() : changedSheet(); if (!items.length) { toast('저장할 내용이 없습니다'); return; } const w = who(); if (!w) { toast('작성자 이름을 적어 주세요'); document.getElementById('who').focus(); return; }
  document.getElementById('save').disabled = true;
  try { const d = await post({ name: w, items }); if (d.ok) { toast('저장 완료 · ' + d.saved + '칸'); await reloadAll(); } else toast('저장 실패: ' + (d.error || '')); } catch (e) { toast('오류: ' + e.message); }
  document.getElementById('save').disabled = false; }
async function loadHist() { const d = await (await fetch('/api/v/' + TOKEN + '/history')).json(); const H = document.getElementById('hist'); const kd = { set: '실사', day: '일별', in: '입고', in_del: '입고삭제', confirm: '변동없음' };
  H.innerHTML = (d.items || []).length ? d.items.map(h => '<div>' + h.at + ' · <span style="background:#f1f5f9;padding:1px 6px;border-radius:5px">' + (kd[h.kind] || h.kind) + '</span> ' + (h.kind === 'confirm' ? '<b>변동 없음 확인</b>' : '<b>' + esc(h.code) + '</b> ' + esc(h.name) + (h.date ? ' ' + h.date.slice(5) : '') + (h.kind === 'in_del' ? '' : ' → ' + fmt(h.qty))) + (h.by ? ' <span style="color:#94a3b8">' + esc(h.by) + '</span>' : '') + '</div>').join('') : '<div style="color:#94a3b8">아직 입력 이력이 없습니다</div>'; }
window.addEventListener('beforeunload', e => { if (Object.keys(CELL).length || changedStock().length || pendingIn().length) { e.preventDefault(); e.returnValue = ''; } });
init();
</script></body></html>'''

# 기동 시 1회 적용 (DF·COL_* 정의 이후)
try:
    _n0 = _vendor_overlay_apply()
    if _n0:
        print(f'[거래처입력] 기동 시 덮어쓰기 적용 {_n0}행')
except Exception as _e:
    print(f'[거래처입력] 기동 적용 오류: {_e}')

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
    """품번 → 단가 딕셔너리 + 품명 역색인 구축.

    2026-09-11 단가 자동화: 구매팀 월간 엑셀(PRICE_DF)에만 의존하던 것을
      ① 최신 발주단가(발주정보·외주발주정보, 매시간 수집) > ② 아마란스 품목 매입단가(PURCH_PRICE) > ③ 엑셀
    순으로 합성. 엑셀은 발주이력·매입단가가 없는 품목의 보조로만 쓰이므로 매달 갱신하지 않아도 된다.
    info 필드: 단가·거래처·품명·시트종류·기준년월(출처 표기)·단가출처(발주|매입단가|엑셀)·엑셀단가."""
    by_code, by_name = {}, {}
    # ③ 엑셀 (기본 정보: 품명·시트종류·거래처)
    if PRICE_DF is not None and not PRICE_DF.empty:
        for _, row in PRICE_DF.iterrows():
            code = str(row.get('품번', '')).strip().upper()
            name = str(row.get('품명', '')).strip()
            try:
                price = float(row.get('최신단가', '')) if str(row.get('최신단가', '')) not in ('', 'nan') else None
            except ValueError:
                price = None
            if code:
                by_code[code] = {'단가': price, '거래처': str(row.get('거래처', '')).strip(), '품명': name,
                                 '시트종류': str(row.get('시트종류', '')).strip(),
                                 '기준년월': '엑셀 ' + str(row.get('기준년월', '')).strip(), '단가출처': '엑셀', '엑셀단가': price}
    # ② 아마란스 품목 매입단가
    for code, p in (PURCH_PRICE or {}).items():
        info = by_code.setdefault(code, {'단가': None, '거래처': '', '품명': '', '시트종류': '', '기준년월': '', '단가출처': '', '엑셀단가': None})
        info.update({'단가': p, '기준년월': '아마란스 매입단가', '단가출처': '매입단가'})
    # ① 최신 발주단가 (구매 발주 + 외주 발주). 품번별 최신 발주일자의 단가
    latest = {}
    for df_, kind in ((ORDER_DF, '발주'), (WP_ORDER_DF, '외주발주')):
        if df_ is None or df_.empty or not {'품번', '발주일자', '단가'}.issubset(df_.columns):
            continue
        _v = df_['거래처명'] if '거래처명' in df_.columns else [''] * len(df_)
        _n = df_['품명'] if '품명' in df_.columns else [''] * len(df_)
        for c, d, p, v, n in zip(df_['품번'], df_['발주일자'], df_['단가'], _v, _n):
            c = str(c).strip().upper(); d = str(d).replace('-', '')[:8]
            try:
                p = float(str(p).replace(',', '') or 0)   # _num은 이 시점(모듈 로드 중)엔 아직 미정의
            except ValueError:
                p = 0.0
            if c and p > 0 and len(d) == 8 and (c not in latest or d > latest[c][0]):
                latest[c] = (d, p, str(v).strip(), str(n).strip(), kind)
    for c, (d, p, v, n, kind) in latest.items():
        info = by_code.setdefault(c, {'단가': None, '거래처': '', '품명': '', '시트종류': '', '기준년월': '', '단가출처': '', '엑셀단가': None})
        info.update({'단가': p, '거래처': v or info.get('거래처', ''), '품명': info.get('품명') or n,
                     '기준년월': f'{kind} {d[:4]}-{d[4:6]}-{d[6:]}', '단가출처': '발주'})
    for code, info in by_code.items():
        if info.get('품명'):
            by_name[info['품명'].lower()] = {'code': code, **info}
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

    # 최신순 정렬, 최대 5건
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

def search_sales_api(query_lower: str) -> str:
    """매출·판매 질문 → 온라인팀 판매자료(SALES_DAILY_DF) 요약 (2026-09-23).
    기간: '9월'/'26년 9월' 등 월 지정, 날짜 지정, 없으면 최근 3개월. 품명·품번 키워드 있으면 해당 상품만."""
    df = SALES_DAILY_DF
    if df is None or df.empty:
        return ''
    q = query_lower
    date_f, month_f, dtoks = _parse_date_filter(q)
    d = df.copy()
    d['d8'] = d['date'].astype(str).str.replace('-', '')
    if date_f:
        d = d[d['d8'] == date_f]; period = f'{date_f[:4]}-{date_f[4:6]}-{date_f[6:]}'
    elif month_f:
        d = d[d['d8'].str.startswith(month_f)]; period = f'{month_f[:4]}-{month_f[4:6]}'
    else:
        end = pd.to_datetime(d['date'].max())
        lo = (end - pd.Timedelta(days=89)).strftime('%Y%m%d')
        d = d[d['d8'] >= lo]; period = f'최근 3개월({lo[:4]}-{lo[4:6]}-{lo[6:]}~{end:%Y-%m-%d})'
    skip = set(SALES_TRIGGER_KW) | {'알려줘', '알려', '보여줘', '보여', '현황', '조회', '전체', '얼마', '얼마야', '어때',
                                    '월별', '채널별', '품목별', '상품별', '합계', '요약', '추이', '이번달', '지난달', '최근'} | set(dtoks or [])
    kws = [t for t in re.split(r'\s+', q) if len(t) >= 2 and t not in skip and not re.search(r'\d+월|\d+년', t)]
    if kws:
        m = pd.Series(False, index=d.index)
        for k in kws:
            m |= d['name'].astype(str).str.lower().str.contains(k, regex=False) | (d['code'].astype(str).str.lower() == k)
        if m.any():
            d = d[m]
    if d.empty:
        return ''
    tot_amt, tot_q = float(d['amt'].sum()), float(d['dq'].sum()) if 'dq' in d.columns else float(d['ea'].sum())
    lines = [f'[매출 · 온라인팀 판매자료 · 공급가(VAT 제외)] 기간 {period}' + (f" · 필터 '{' '.join(kws)}'" if kws else ''),
             f'합계 매출 {tot_amt:,.0f}원 · 납품수량 {tot_q:,.0f}개 · 자료 최종일 {df["date"].max()}']
    mo = d.assign(ym=d['date'].astype(str).str[:7]).groupby('ym')['amt'].sum()
    if len(mo) > 1:
        lines.append('월별: ' + ' / '.join(f'{k} {v/1e8:.2f}억' for k, v in mo.items()))
    ch = d.groupby('channel_name')['amt'].sum().sort_values(ascending=False)
    lines.append('채널별: ' + ' / '.join(f'{k} {v:,.0f}원' for k, v in ch.items() if v > 0))
    pr = d.groupby(['code', 'name'])[['amt', 'dq']].sum().sort_values('amt', ascending=False).head(15)
    lines.append('상품 TOP 15 (품번 | 상품명 | 매출 | 납품수량):')
    lines += [f'- {c or "-"} | {n} | {a:,.0f}원 | {q_:,.0f}개' for (c, n), (a, q_) in pr.iterrows()]
    return '\n'.join(lines)


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
    # 매출은 온라인팀 판매자료로 일원화(2026-09-23) — Monday '매출 현황' 보드는 챗봇 검색에서 제외
    df = MONDAY_DF[~MONDAY_DF['보드명'].astype(str).str.contains('매출 현황', regex=False)]

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

            # 개별 나열 (최신순 정렬, 최대 5건)
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

    # 개별 나열 (최신순 정렬, 최대 5건)
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

    # ⓪-0a 매출·판매 → 온라인팀 판매자료 (2026-09-23: 매출은 무조건 팀장님 자료). 출하/출고를 명시하면 아래 아마란스 검색
    if any(k in query_lower for k in ('매출', '판매')) and not any(k in query_lower for k in ('출하', '출고', '자재이동')):
        sa = search_sales_api(query_lower)
        if sa:
            return sa

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
    # 외부(ngrok 등) 접속이면 관리자/업로드/거래처 링크 숨김 — 로컬에서는 그대로 노출
    host = (request.host or '').split(':')[0]
    public_mode = host not in ('localhost', '127.0.0.1', '0.0.0.0')
    # 방문 이력 기록 (세션당 6시간에 1회 — 재방문 추적, 과다기록 방지)
    if current_user():
        import time as _t
        if _t.time() - session.get('_last_visit_log', 0) > 6 * 3600:
            session['_last_visit_log'] = _t.time()
            _log_visit('visit')
    import json as _json
    return render_template_string(DASHBOARD_TEMPLATE, public_mode=public_mode,
                                  user_json=_json.dumps(current_user()))


@app.route('/chat')
def chat_page():
    import json as _json
    return render_template_string(HTML_TEMPLATE, user_json=_json.dumps(current_user()))


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


@app.route('/api/spec_file/<item_id>', methods=['GET'])
def api_spec_file(item_id):
    """외주 생산 요청 아이템의 시방서 파일 — Monday assets에서 신선한 S3 서명 URL 조회.
    public_url은 1시간 만료이므로 클릭 시점에 실시간 발급. Office viewer URL도 생성."""
    import requests as _req
    from urllib.parse import quote as _quote
    api_key = os.getenv('MONDAY_API_KEy')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Monday API 키 없음'}), 500
    if not str(item_id).isdigit():
        return jsonify({'ok': False, 'error': '잘못된 item_id'}), 400
    q = '{ items(ids: [%s]) { assets { id name url public_url file_extension } } }' % item_id
    try:
        r = _req.post('https://api.monday.com/v2', json={'query': q},
                      headers={'Authorization': api_key}, timeout=20)
        data = r.json()
        its = (data.get('data') or {}).get('items') or []
        assets = its[0].get('assets') if its else None
        if not assets:
            return jsonify({'ok': False, 'error': '첨부 파일 없음'}), 404
        a = assets[0]  # 첫 파일 (시방서)
        pub = a.get('public_url') or a.get('url') or ''
        ext = (a.get('file_extension') or '').lower().lstrip('.')
        # 미리보기 URL: Office 문서는 Office Online viewer, PDF/이미지는 직접
        if ext in ('xlsx', 'xls', 'docx', 'doc', 'pptx', 'ppt'):
            # embed.aspx = 읽기 전용 임베드 (편집/복사본편집 버튼 없음)
            preview = 'https://view.officeapps.live.com/op/embed.aspx?src=' + _quote(pub, safe='')
            preview_type = 'office'
        elif ext == 'pdf':
            preview = pub; preview_type = 'pdf'
        elif ext in ('png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'):
            preview = pub; preview_type = 'image'
        else:
            preview = pub; preview_type = 'download'
        return jsonify({'ok': True, 'name': a.get('name', ''), 'ext': ext,
                        'download_url': pub, 'preview_url': preview,
                        'preview_type': preview_type})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


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
  <script>(function(){const f=window.fetch.bind(window);window.fetch=function(i,n){n=n||{};const h=new Headers(n.headers||{});if(!h.has('ngrok-skip-browser-warning'))h.set('ngrok-skip-browser-warning','true');n.headers=h;return f(i,n);};})();</script>
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
    <a href="/vendor/데이웰즈">데이웰즈</a>
    <a href="/vendor/더고은">더고은</a>
    <a href="/vendor/정성">정성</a>
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
  <script>(function(){const f=window.fetch.bind(window);window.fetch=function(i,n){n=n||{};const h=new Headers(n.headers||{});if(!h.has('ngrok-skip-browser-warning'))h.set('ngrok-skip-browser-warning','true');n.headers=h;return f(i,n);};})();</script>
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
    <a href="/vendor/데이웰즈" class="{% if vendor_name == '데이웰즈' %}active{% endif %}">데이웰즈</a>
    <a href="/vendor/더고은" class="{% if vendor_name == '더고은' %}active{% endif %}">더고은</a>
    <a href="/vendor/정성" class="{% if vendor_name == '정성' %}active{% endif %}">정성</a>
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
UPLOAD_DIR = f'{BASE_DIR}'

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
.header p{font-size:12px;color:#64748b}
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

    # 변환 스크립트 실행 (현재 Flask가 쓰는 Python 절대경로)
    convert_script = os.path.join(UPLOAD_DIR, info['convert'])
    try:
        result = subprocess.run(
            [sys.executable, convert_script],
            capture_output=True, text=True, timeout=120,
            cwd=UPLOAD_DIR, encoding='utf-8', errors='replace'
        )
        if result.returncode != 0:
            return jsonify({
                'ok': False,
                'msg': f'변환 실패: {result.stderr[:300]}',
            }), 500
    except Exception as e:
        return jsonify({'ok': False, 'msg': f'변환 오류: {str(e)}'}), 500

    # 변환 후 메모리 DF 자동 리로드 (서버 재시작 불필요)
    try:
        _reload_aramanth_dfs()
    except Exception as e:
        return jsonify({
            'ok': True,
            'msg': f'✅ {file_type} 업로드/변환 완료. 단, 메모리 리로드 실패: {str(e)[:200]}\n서버 재시작 권장.',
        })

    return jsonify({
        'ok': True,
        'msg': f'✅ {file_type} 업로드 완료!\n파일: {info["target"]}\n변환: {info["convert"]} 실행 → 메모리 자동 반영됨.',
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
            'extOrderCount': len(WP_ORDER_DF) if WP_ORDER_DF is not None else 0,
            'shipCount': len(SHIP_DF) if SHIP_DF is not None else 0,
            'prodCount': len(PROD_DF) if PROD_DF is not None else 0,
            'bomProducts': BOM_DF['모품번'].nunique() if BOM_DF is not None and not BOM_DF.empty else 0,
            'vendorCount': len(_unique_vals(COL_원산지)),
            'destCount': len(_unique_vals(COL_납품처)),
        }
    })


# ────────────────────────────────────────────
# 대시보드: G/H/I 모품번 목록
# ────────────────────────────────────────────
@app.route('/api/products', methods=['GET'])
def api_products():
    """제품 목록 = BOM 모품번 ∪ 단가마스터 품번 (G/H/I/E).
    BOM만 쓰면 레시피 미등록 제품(예: I 세트/상품 변형)이 누락됨 → 합집합.
    has_bom: BOM(레시피) 등록 여부. False면 자재 소요 전개 불가(배지 표시)."""
    groups = {'G': [], 'H': [], 'I': [], 'E': []}

    # BOM 모품번 (레시피 보유) → 이름/단위
    bom_parents = {}
    if BOM_DF is not None and not BOM_DF.empty:
        for _, r in BOM_DF.iterrows():
            code = str(r.get('모품번', '')).strip()
            if code and code not in bom_parents:
                bom_parents[code] = {
                    'name': str(r.get('모품명', '')).strip(),
                    'unit': str(r.get('모품단위', '')).strip(),
                }

    # 단가마스터 품번 (제품 마스터) → BOM 없는 제품까지 포함
    price_names = {}
    if PRICE_DF is not None and not PRICE_DF.empty and '품번' in PRICE_DF.columns:
        for _, r in PRICE_DF.iterrows():
            code = str(r.get('품번', '')).strip()
            if code and code not in price_names:
                price_names[code] = str(r.get('품명', '')).strip()

    seen = set()
    for code in (set(bom_parents) | set(price_names)):
        prefix = code[:1].upper()
        if prefix not in groups or code in seen:
            continue
        has_bom = code in bom_parents
        name = (bom_parents[code]['name'] if has_bom else '') or price_names.get(code, '')
        if name.lstrip().startswith('미사용'):   # 단종 제품 숨김
            continue
        seen.add(code)
        groups[prefix].append({
            'code': code,
            'name': name,
            'unit': bom_parents[code]['unit'] if has_bom else '',
            'has_bom': has_bom,
        })
    for k in groups:
        groups[k].sort(key=lambda x: x['code'])
    return jsonify(groups)


# ────────────────────────────────────────────
# 대시보드: 특정 모품번의 BOM + 단가 + 외주처별 재고
# ────────────────────────────────────────────
@app.route('/api/bom/<code>', methods=['GET'])
def api_bom_detail(code):
    code = str(code).strip().upper()
    if BOM_DF is None or BOM_DF.empty:
        return jsonify({'error': 'BOM 데이터 없음'}), 404
    rows = BOM_DF[BOM_DF['모품번'].str.upper() == code]
    if rows.empty:
        return jsonify({'error': f'{code} 품번을 찾을 수 없습니다'}), 404

    parent_name = str(rows.iloc[0].get('모품명', '')).strip()
    is_jasa = code.startswith(('G', 'E'))

    items = []
    for _, r in rows.iterrows():
        child_code = str(r.get('자품번', '')).strip()
        child_name = str(r.get('자품명', '')).strip()
        # 단가: BOM의 자재단가 우선, 없으면 PRICE_DF 조회
        price = _num(r.get('자재단가', 0))
        if not price:
            pi = get_price_info(child_code, child_name)
            if pi and pi.get('단가'):
                price = pi['단가']

        # 외주처별 재고
        vendor_stocks = []
        total_stock = 0
        if is_jasa:
            # 자사재고 (G/E)
            # ① E품번(반제품): JASA_DF엔 없으므로 STOCK_DF(아마란스 현재고) 우선 조회
            if child_code.upper().startswith('E') and STOCK_DF is not None and '품번' in STOCK_DF.columns:
                match = STOCK_DF[STOCK_DF['품번'].str.upper() == child_code.upper()]
                for _, sr in match.iterrows():
                    qty = _num(sr.get('현재고', 0))
                    if qty == 0:
                        continue
                    vendor_stocks.append({
                        'vendor': str(sr.get('창고명', '')).strip() or '자사',
                        'qty': qty,
                    })
                    total_stock += qty
            # ② G품번 (또는 STOCK_DF 매칭 실패한 E): 기존 JASA_DF 조회
            if not vendor_stocks and JASA_DF is not None:
                col_품번 = JASA_DF.columns[1]
                col_업체 = JASA_DF.columns[2]
                col_총재고 = JASA_DF.columns[7]
                match = JASA_DF[JASA_DF[col_품번].str.upper() == child_code.upper()]
                for _, jr in match.iterrows():
                    qty = _num(jr[col_총재고])
                    vendor_stocks.append({
                        'vendor': str(jr[col_업체]).strip() or '자사',
                        'qty': qty,
                    })
                    total_stock += qty
        else:
            # 외주재고 (H/I) - DF에서 원산지별 집계
            match = DF[DF[COL_품목].str.upper() == child_code.upper()]
            for _, dr in match.iterrows():
                qty = _num(dr[COL_재고량])
                vendor_stocks.append({
                    'vendor': str(dr[COL_원산지]).strip() or '(미지정)',
                    'qty': qty,
                })
                total_stock += qty

        # 동일 외주처 합치기
        agg = {}
        for vs in vendor_stocks:
            agg[vs['vendor']] = agg.get(vs['vendor'], 0) + vs['qty']
        vendor_stocks = [{'vendor': k, 'qty': v} for k, v in sorted(agg.items(), key=lambda x: -x[1])]

        items.append({
            'seq': str(r.get('BOM순번', '')),
            'code': child_code,
            'name': child_name,
            'category': str(r.get('자품목구분', '')).strip(),
            'unit': str(r.get('자품단위', '')).strip(),
            'qty': _num(r.get('실소요량', 0)),
            'price': price,
            'totalCost': _num(r.get('소요비용', 0)) or (price * _num(r.get('실소요량', 0))),
            'totalStock': total_stock,
            'vendorStocks': vendor_stocks,
        })

    return jsonify({
        'code': code,
        'name': parent_name,
        'isJasa': is_jasa,
        'items': items,
    })


# ────────────────────────────────────────────
# 대시보드: 캘린더 (생산계획/생산실적/외주발주/발주내역)
# ────────────────────────────────────────────
_CALENDAR_SPEC = {
    'plan':     {'df': 'WO_DF',       'date': '지시일자',  'label': '생산계획'},
    'actual':   {'df': 'PROD_DF',     'date': '실적일자',  'label': '생산실적'},
    'outsource':{'df': 'WP_ORDER_DF', 'date': '발주일자',  'label': '외주발주'},
    'order':    {'df': 'ORDER_DF',    'date': '발주일자',  'label': '발주내역'},
}

def _cal_df(type_key):
    spec = _CALENDAR_SPEC.get(type_key)
    if not spec:
        return None, None
    df = globals().get(spec['df'])
    if df is None or df.empty or spec['date'] not in df.columns:
        return None, spec
    return df, spec


@app.route('/api/calendar/<type_key>', methods=['GET'])
def api_calendar(type_key):
    """?ym=YYYYMM → 해당 월 날짜별 건수 / ?date=YYYYMMDD → 해당 날짜 상세
    / ?q=검색어 → 품번·품명 이력 검색 (날짜 무관, 최신순)"""
    df, spec = _cal_df(type_key)
    if df is None:
        return jsonify({'error': f'데이터 없음: {type_key}'}), 404

    date_col = spec['date']
    ym = (request.args.get('ym') or '').strip()
    date = (request.args.get('date') or '').strip()
    q = (request.args.get('q') or '').strip()

    if q:
        # 품번/품명 부분일치 이력 — 캘린더 모달 검색창용. 최신 날짜부터.
        qq = q.lower()
        mask = pd.Series(False, index=df.index)
        for col in ['품번', '품명']:
            if col in df.columns:
                mask |= df[col].astype(str).str.lower().str.contains(qq, regex=False, na=False)
        match = df[mask].copy()
        match['_d'] = match[date_col].astype(str).str[:8]
        match = match.sort_values('_d', ascending=False)
        total = len(match)
        match = match.head(300)   # 과도한 응답 방지 (프론트에 총건수 별도 표시)
        date = None               # 아래 date 분기로 빠지지 않게
        items = []
        for _, r in match.iterrows():
            item = {c: str(r.get(c, '')) for c in ['품번', '품명', '거래처명', '단위']}
            d8 = str(r.get(date_col, ''))[:8]
            item['날짜'] = f'{d8[:4]}-{d8[4:6]}-{d8[6:8]}' if len(d8) == 8 else d8
            for qty_col in ['지시수량', '작업수량', '양품수량', '발주수량', '입고수량']:
                if qty_col in df.columns:
                    v = r.get(qty_col, '')
                    if str(v).strip() not in ('', 'nan', '0', '0.0'):
                        item[qty_col] = str(v)
            for money_col in ['단가', '공급가액', '합계금액']:
                if money_col in df.columns:
                    v = r.get(money_col, '')
                    if str(v).strip() not in ('', 'nan', '0', '0.0'):
                        item[money_col] = str(v)
            if '비고' in df.columns:
                item['비고'] = str(r.get('비고', ''))
            items.append(item)
        return jsonify({'q': q, 'label': spec['label'], 'items': items, 'total': total})

    if date:
        match = df[df[date_col].astype(str).str[:8] == date]
        items = []
        for _, r in match.iterrows():
            item = {c: str(r.get(c, '')) for c in ['품번', '품명', '거래처명', '단위']}
            # 수량 필드 (종류별로 다름)
            for qty_col in ['지시수량', '작업수량', '양품수량', '발주수량', '입고수량']:
                if qty_col in df.columns:
                    v = r.get(qty_col, '')
                    if str(v).strip() not in ('', 'nan', '0', '0.0'):
                        item[qty_col] = str(v)
            # 금액 필드 (발주/외주발주)
            for money_col in ['단가', '공급가액', '합계금액']:
                if money_col in df.columns:
                    v = r.get(money_col, '')
                    if str(v).strip() not in ('', 'nan', '0', '0.0'):
                        item[money_col] = str(v)
            if '담당자' in df.columns:
                item['담당자'] = str(r.get('담당자', ''))
            if '비고' in df.columns:
                item['비고'] = str(r.get('비고', ''))
            items.append(item)
        return jsonify({'date': date, 'label': spec['label'], 'items': items})

    # 월별 집계
    if not ym or len(ym) != 6:
        from datetime import datetime
        ym = datetime.now().strftime('%Y%m')
    dates = {}
    for _, r in df.iterrows():
        d = str(r.get(date_col, ''))[:8]
        if len(d) == 8 and d[:6] == ym:
            dates[d] = dates.get(d, 0) + 1
    return jsonify({'ym': ym, 'label': spec['label'], 'dates': dates, 'total': sum(dates.values())})


# ────────────────────────────────────────────
# 대시보드: 통합 검색 (모품번/자품번/품명)
# ────────────────────────────────────────────
@app.route('/api/search', methods=['GET'])
@cached_api()
def api_search():
    q = (request.args.get('q') or '').strip()
    if not q or len(q) < 1:
        return jsonify({'products': [], 'items': []})

    qU = q.upper()
    qL = q.lower()

    products = []
    seen_p = set()
    if BOM_DF is not None:
        # 모품번 검색 (G/H/I)
        for _, r in BOM_DF.iterrows():
            code = str(r.get('모품번', '')).strip()
            if not code or code in seen_p:
                continue
            if code[:1].upper() not in ('G', 'H', 'I', 'E'):
                continue
            name = str(r.get('모품명', '')).strip()
            if qU in code.upper() or qL in name.lower():
                seen_p.add(code)
                products.append({'code': code, 'name': name, 'group': code[:1].upper(), 'bom': True})
    # BOM 미등록 완제품(G/H/I)도 검색에 포함 — 아마란스에 BOM이 없는 외주 품번(H 268개 중 191개, 2026-09-09)이
    # 검색에서 통째로 사라져 "업데이트가 안 됐다"고 보이던 문제. bom=False로 표시해 원인을 보이게 함.
    for df_, cc, nc in ((WP_ORDER_DF, '품번', '품명'), (STOCK_DF, '품번', '품명'), (SHIP_DF, '품번', '품명'), (ORDER_DF, '품번', '품명')):
        if df_ is None or df_.empty or cc not in df_.columns or nc not in df_.columns:
            continue
        for _, r in df_.drop_duplicates(cc).iterrows():
            code = str(r.get(cc, '')).strip()
            if not code or code in seen_p or code[:1].upper() not in ('G', 'H', 'I'):
                continue
            name = str(r.get(nc, '')).strip()
            if qU in code.upper() or qL in name.lower():
                seen_p.add(code)
                products.append({'code': code, 'name': name, 'group': code[:1].upper(), 'bom': False})
    # 정렬: 품번이 검색어로 시작하는 것 → 완제품(G 자사 / H 유상사급 / I 상품매입) → 반제품(E) → 같은 그룹 안에선 BOM 있는 것 먼저.
    # 외주 품번(H/I)이 BOM 파일 순서(E·G 먼저) 때문에 상한에 잘려 안 보이던 문제(2026-09-09)
    _gord = {'G': 0, 'H': 1, 'I': 2, 'E': 3}
    products.sort(key=lambda p: (0 if p['code'].upper().startswith(qU) else 1, _gord.get(p['group'], 9), 0 if p.get('bom') else 1, p['code']))

    items = []
    seen_i = set()
    if BOM_DF is not None:
        for _, r in BOM_DF.iterrows():
            code = str(r.get('자품번', '')).strip()
            if not code or code in seen_i:
                continue
            name = str(r.get('자품명', '')).strip()
            if qU in code.upper() or qL in name.lower():
                seen_i.add(code)
                items.append({
                    'code': code,
                    'name': name,
                    'category': str(r.get('자품목구분', '')).strip(),
                })
    # 재고일지(DF)에서도 (BOM에 없는 품목)
    for _, r in DF.iterrows():
        code = str(r[COL_품목]).strip()
        if not code or code in seen_i:
            continue
        name = str(r[COL_품명]).strip()
        if qU in code.upper() or qL in name.lower():
            seen_i.add(code)
            items.append({'code': code, 'name': name, 'category': ''})

    return jsonify({
        'products': products[:120],
        'items': items[:60],
    })




# ────────────────────────────────────────────
# 대시보드: 상단 KPI 지표
# ────────────────────────────────────────────
def _ym_shift(ym, delta):
    y = int(ym[:4]); m = int(ym[4:6]) + delta
    while m > 12: m -= 12; y += 1
    while m < 1: m += 12; y -= 1
    return f"{y}{m:02d}"

def _pick_col(df, *candidates):
    if df is None:
        return None
    for c in candidates:
        if c in df.columns:
            return c
    return None

# ───── BOM 재귀 전개 + 외주발주 소비량 ─────
_BOM_INDEX_CACHE = None
_BOM_EXPAND_CACHE = {}


def _get_bom_index():
    """모품번 → [(자품번, 실소요량), ...] 인덱스. 사용여부='사용'만."""
    global _BOM_INDEX_CACHE
    if _BOM_INDEX_CACHE is not None:
        return _BOM_INDEX_CACHE
    idx = {}
    if BOM_DF is None or BOM_DF.empty:
        _BOM_INDEX_CACHE = idx
        return idx
    for _, r in BOM_DF.iterrows():
        p = str(r.get('모품번', '')).strip().upper()
        c = str(r.get('자품번', '')).strip().upper()
        q = _num(r.get('실소요량', 0)) or _num(r.get('정미수량', 0))
        use = str(r.get('사용여부', '')).strip()
        if not p or not c or q <= 0 or use != '사용':
            continue
        idx.setdefault(p, []).append((c, q))
    _BOM_INDEX_CACHE = idx
    return idx


def _explode_bom(parent_code, depth=0):
    """parent_code 1단위당 누적 자재 소요량 dict ({자품번: qty}). 최대 깊이 6."""
    if parent_code in _BOM_EXPAND_CACHE:
        return _BOM_EXPAND_CACHE[parent_code]
    if depth > 6:
        return {parent_code: 1.0}
    idx = _get_bom_index()
    children = idx.get(parent_code)
    if not children:
        return {parent_code: 1.0}  # leaf
    unit = {}
    for c, q in children:
        for cc, qq in _explode_bom(c, depth + 1).items():
            unit[cc] = unit.get(cc, 0) + qq * q
    _BOM_EXPAND_CACHE[parent_code] = unit
    return unit


def _short_vendor(name):
    """법인명 → 짧은 거래처명 (예: '농업회사법인 주식회사 데이웰즈' → '데이웰즈')."""
    if not name:
        return ''
    s = re.sub(r'농업회사법인|주식회사|유한회사|\(주\)|\(유\)', '', name)
    s = re.sub(r'\s+', '', s).strip()
    return s


def _calc_outsource_consumption(ym_set):
    """외주발주 ym_set 기간 × BOM 전개 → 자재 코드별 총 사용량 + 거래처별 사용량.
    반환: {자품번: {'total': 누적사용량, 'vendors': {거래처명: 누적사용량}}} (A/B/C/D만)."""
    if WP_ORDER_DF is None or WP_ORDER_DF.empty:
        return {}
    df = WP_ORDER_DF
    if '발주일자' not in df.columns or '품번' not in df.columns or '발주수량' not in df.columns:
        return {}
    mask = df['발주일자'].astype(str).str[:6].isin(ym_set)
    sub = df.loc[mask]
    out = {}
    for _, r in sub.iterrows():
        p = str(r['품번']).strip().upper()
        qty = _num(r['발주수량'])
        if not p or qty <= 0:
            continue
        vendor = _short_vendor(str(r.get('거래처명', '')).strip())
        for c, q in _explode_bom(p).items():
            if c[:1] in ('A', 'B', 'C', 'D'):
                use = qty * q
                if c not in out:
                    out[c] = {'total': 0, 'vendors': {}}
                out[c]['total'] += use
                if vendor:
                    out[c]['vendors'][vendor] = out[c]['vendors'].get(vendor, 0) + use
    return out


def _calc_sales_consumption(scope):
    """실제 판매 데이터 × BOM 전개 → 자재(A~D)별 '월평균' 소비량(낱개).
    분류 규칙(사용자 확정): scope='jasa'→G제품 / 'outsource'→H + I(BOM有=예외사급).
    I(BOM無)은 순수 사입이라 BOM 전개가 안 되므로 자연히 제외됨.
    당월(부분치) 제외한 완결월 평균. SALES_DF 없으면 {} (발주기반 fallback).
    반환: {자재코드: 월평균소비량}."""
    if SALES_DF is None or SALES_DF.empty:
        return {}
    vel = _sales_velocity()
    if vel:
        # 판매 API 모드(2026-09-23): 완제품 일 판매속도(최근 4주·3개월 가중) × 30 × BOM 전개 = 자재 월 소비
        bom_parents = set(_get_bom_index().keys())
        out = {}
        for code, v in vel['by'].items():
            p = code[:1]
            if scope == 'jasa':
                if p != 'G':
                    continue
            elif not (p == 'H' or (p == 'I' and code in bom_parents)):
                continue
            m = v['v'] * 30
            for c, q in _explode_bom(code).items():
                if c[:1] in ('A', 'B', 'C', 'D'):
                    out[c] = out.get(c, 0) + m * q
        return out
    cur_ym = datetime.now().strftime('%Y%m')
    # 당월 부분치 제외 + 최근 완결 3개월만 (2026-09-23: 판매 API가 1월부터 제공 → 전체 평균이면 옛 달이 섞여 최근 흐름 희석)
    months = sorted(y for y in SALES_DF['ym'].astype(str).unique() if y < cur_ym)[-3:]
    if not months:
        return {}
    df = SALES_DF[SALES_DF['ym'].astype(str).isin(months)]
    bom_parents = set(_get_bom_index().keys())
    out = {}
    for _, r in df.iterrows():
        code, p = r['code'], r['prefix']
        if scope == 'jasa':
            if p != 'G':
                continue
        else:  # outsource: H 전체 + I(BOM 있는 예외사급)
            if not (p == 'H' or (p == 'I' and code in bom_parents)):
                continue
        ea = r['ea']
        for c, q in _explode_bom(code).items():
            if c[:1] in ('A', 'B', 'C', 'D'):
                out[c] = out.get(c, 0) + ea * q
    n = len(months)
    return {c: v / n for c, v in out.items()}        # 월평균


@app.route('/api/stock_alerts', methods=['GET'])
@cached_api()
def api_stock_alerts():
    """재고 부족 경고 TOP N — scope=jasa(자사재고) 또는 outsource(외주재고)"""
    scope = (request.args.get('scope') or 'jasa').strip()
    alerts = _stock_alert_items(scope)
    return jsonify({'items': alerts[:30], 'total': len(alerts)})


def _stock_alert_items(scope):
    """재고 경고 목록 계산 (라우트/발주타이밍 패널 공용)."""
    from datetime import datetime
    now = datetime.now()
    ym_cur = now.strftime('%Y%m')

    def _latest_ym_set(df, date_col, n=3):
        """df[date_col]의 최신 n개 월(YYYYMM)을 set으로 — 데이터가 비어도 의미 있는 룩백 보장."""
        if df is None or df.empty or date_col not in df.columns:
            return set(_ym_shift(ym_cur, -i) for i in range(n))
        yms = sorted({s[:6] for s in df[date_col].astype(str) if len(s) >= 6 and s[:6].isdigit()},
                     reverse=True)
        return set(yms[:n]) if yms else set(_ym_shift(ym_cur, -i) for i in range(n))

    if scope == 'outsource':
        ym_set = _latest_ym_set(WP_ORDER_DF, '발주일자', 3)
    else:
        ym_set = _latest_ym_set(ISSUE_DF, '출고일자', 3)

    stocks = {}
    if scope == 'outsource':
        # 재고일지에서 재고만 집계 (vendors는 외주발주 기준으로 별도 산출)
        for _, r in DF.iterrows():
            code = str(r[COL_품목]).strip().upper()
            if not code or code[:1] not in ('A', 'B', 'C', 'D'):
                continue
            qty = _num(r[COL_재고량])
            name = str(r[COL_품명]).strip()
            if code not in stocks:
                pi = get_price_info(code, name)
                stocks[code] = {'name': name, 'qty': 0, 'price': (pi.get('단가', 0) if pi else 0) or 0,
                                'vendors': {}, 'daily_avg': 0}
            stocks[code]['qty'] += qty

        # 외주발주 × BOM → 소비량 + 실제 취급 거래처
        outsource_data = _calc_outsource_consumption(ym_set)
        for code, info in outsource_data.items():
            if code in stocks:
                stocks[code]['daily_avg'] = (info['total'] / len(ym_set)) / 30
                stocks[code]['vendors'] = info['vendors']  # 외주발주 기반 거래처
    else:
        if JASA_DF is not None:
            c1, c5, c7 = JASA_DF.columns[1], JASA_DF.columns[5], JASA_DF.columns[7]
            for _, r in JASA_DF.iterrows():
                code = str(r[c1]).strip().upper()
                if not code or code[:1] not in ('A', 'B', 'C', 'D'):
                    continue
                qty = _num(r[c7])
                name = str(r[c5]).strip()
                if code not in stocks:
                    pi = get_price_info(code, name)
                    stocks[code] = {'name': name, 'qty': 0, 'price': (pi.get('단가', 0) if pi else 0) or 0}
                stocks[code]['qty'] += qty

    consumption = {}
    issue_qty_col = _pick_col(ISSUE_DF, '출고수량', '출고량')
    if ISSUE_DF is not None and '품번' in ISSUE_DF.columns and '출고일자' in ISSUE_DF.columns and issue_qty_col:
        mask = ISSUE_DF['출고일자'].astype(str).str[:6].isin(ym_set)
        for _, r in ISSUE_DF.loc[mask].iterrows():
            code = str(r['품번']).strip().upper()
            consumption[code] = consumption.get(code, 0) + _num(r.get(issue_qty_col, 0))

    # 판매기반 소비량 (월평균) — 데이터 있으면 우선, 없으면 발주/출고 기반 fallback
    sales_cons = _calc_sales_consumption(scope)
    use_sales = bool(sales_cons)

    alerts = []
    for code, info in stocks.items():
        if use_sales:
            # 실제 판매 → BOM 전개 기반 (판매기반)
            monthly_avg = sales_cons.get(code, 0)
        elif scope == 'outsource':
            # fallback: 외주발주 × BOM (발주기반)
            monthly_avg = info.get('daily_avg', 0) * 30
        else:
            # fallback: 출고정보 최근 N개월 평균
            monthly_avg = consumption.get(code, 0) / max(len(ym_set), 1)
        daily_avg = monthly_avg / 30
        if monthly_avg <= 0:
            continue
        days_left = info['qty'] / daily_avg if daily_avg > 0 else 9999
        if days_left >= 45:
            continue
        if info['qty'] <= 0:
            level = 'out'
            days_left = 0  # 음수 재고는 0일로 표시 (정렬 맨 위), 실제 qty는 그대로 노출
        elif days_left < 15:
            level = 'critical'
        elif days_left < 30:
            level = 'warning'
        else:
            level = 'low'
        alert = {
            'code': code,
            'name': info['name'],
            'qty': int(info['qty']),
            'monthly_avg': int(monthly_avg),
            'days_left': round(days_left, 1),
            'level': level,
            'value': int(info['qty'] * info['price']),
            'basis': 'sales' if use_sales else 'order',   # 판매기반/발주기반 구분
        }
        if 'vendors' in info and info['vendors']:
            # 재고 보유 외주처만 표시 (qty>0). 0/음수인 외주처는 해당 품목을 실제로 취급 안 하는 것으로 간주.
            vsorted = sorted(
                ((v, q) for v, q in info['vendors'].items() if q > 0),
                key=lambda x: -x[1],
            )
            alert['vendors'] = [v for v, _ in vsorted]
        alerts.append(alert)

    alerts.sort(key=lambda x: x['days_left'])
    return alerts


def _calc_leadtimes():
    """품번별 실측 리드타임(발주일→첫 입고일, 일수) — 최근 12개월 발주 기준 중앙값.
    반환: {품번: {'days': 중앙값, 'n': 표본수}}"""
    from datetime import datetime, timedelta
    if ORDER_DF is None or ORDER_DF.empty or RCV_DF is None or RCV_DF.empty:
        return {}
    cutoff = (datetime.now() - timedelta(days=365)).strftime('%Y%m%d')

    # 품번별 입고일 오름차순 목록
    rcv_dates = {}
    for _, r in RCV_DF.iterrows():
        code = str(r.get('품번', '')).strip().upper()
        d = str(r.get('입고일자', '')).replace('-', '')[:8]
        if code and len(d) == 8 and d.isdigit():
            rcv_dates.setdefault(code, []).append(d)
    for v in rcv_dates.values():
        v.sort()

    import bisect
    gaps = {}
    seen = set()   # (발주번호, 품번) 중복 디테일 행 제거
    po_col = '발주번호' if '발주번호' in ORDER_DF.columns else None
    for _, r in ORDER_DF.iterrows():
        code = str(r.get('품번', '')).strip().upper()
        od = str(r.get('발주일자', '')).replace('-', '')[:8]
        if not code or len(od) != 8 or od < cutoff or code not in rcv_dates:
            continue
        key = (str(r.get(po_col, '')) if po_col else od, code)
        if key in seen:
            continue
        seen.add(key)
        lst = rcv_dates[code]
        i = bisect.bisect_left(lst, od)
        if i >= len(lst):
            continue
        try:
            gap = (datetime.strptime(lst[i], '%Y%m%d') - datetime.strptime(od, '%Y%m%d')).days
        except ValueError:
            continue
        if 0 <= gap <= 120:
            gaps.setdefault(code, []).append(gap)

    out = {}
    for code, g in gaps.items():
        g.sort()
        out[code] = {'days': g[len(g) // 2], 'n': len(g)}
    return out


GOODS_RULES_PATH = f'{BASE_DIR}/상품매입_업체조건.csv'
_GOODS_RULES_CACHE = {'mtime': None, 'rules': None}


def _goods_vendor_key(name):
    """거래처명 정규화 — ERP '농업회사법인(유)아리랑식품' ↔ 표 '아리랑식품' 매칭용 (법인 표기·공백·괄호 제거)."""
    s = str(name or '')
    s = re.sub(r'\(.*?\)', '', s)
    for w in ('농업회사법인', '영농조합법인', '유한회사', '주식회사', '(주)', '(유)', '㈜', '법인', ' '):
        s = s.replace(w, '')
    return s.strip().lower()


def _load_goods_rules():
    """상품매입_업체조건.csv → {'by_code': {품번: rule}, 'by_vendor': {정규화거래처: {'lt_days', 'kind', 'rows'}}}.
    rule = {거래처, 구분(사입|시방서), 품번, 품명, MOQ, MOQ단위, PLT당수량, lt_days(최대주×7), 비고}. 파일 mtime 캐시."""
    try:
        mt = os.path.getmtime(GOODS_RULES_PATH)
    except OSError:
        return {'by_code': {}, 'by_vendor': {}, 'aliases': {}}
    if _GOODS_RULES_CACHE['mtime'] == mt and _GOODS_RULES_CACHE['rules'] is not None:
        return _GOODS_RULES_CACHE['rules']
    by_code, by_vendor, aliases = {}, {}, {}
    try:
        df = pd.read_csv(GOODS_RULES_PATH, dtype=str, encoding='utf-8-sig').fillna('')
        for _, r in df.iterrows():
            v = str(r.get('거래처', '')).strip()
            if not v:
                continue

            def _f(x):
                try:
                    return float(str(x).replace(',', '').strip())
                except ValueError:
                    return 0.0
            lt_max = _f(r.get('LT최대주', ''))
            moq, moq_qty = _f(r.get('MOQ', '')), _f(r.get('MOQ수량', ''))
            unit = str(r.get('MOQ단위', '')).strip()
            if moq_qty <= 0 and moq > 0 and unit.upper() != 'PLT':
                moq_qty = moq          # 봉/통/단상자 = ERP 판매단위와 동일
            # PLT당 수량 = MOQ수량 ÷ MOQ(PLT) — 청구수량을 PLT 배수로 올릴 때 사용
            plt_qty = _f(r.get('PLT당수량', '')) or ((moq_qty / moq) if (unit.upper() == 'PLT' and moq > 0 and moq_qty > 0) else 0.0)
            rule = {'거래처': v, '구분': str(r.get('구분', '')).strip() or '사입', '품번': str(r.get('품번', '')).strip().upper(),
                    '구품번': [x.strip().upper() for x in re.split(r'[,/;|\s]+', str(r.get('구품번', ''))) if x.strip()],
                    '품명': str(r.get('품명', '')).strip(), 'MOQ': moq, 'MOQ단위': unit, 'MOQ수량': moq_qty,
                    'PLT당수량': plt_qty, 'lt_days': int(round(lt_max * 7)) if lt_max > 0 else None,
                    '비고': str(r.get('비고', '')).strip()}
            k = _goods_vendor_key(v)
            ve = by_vendor.setdefault(k, {'거래처': v, 'kind': rule['구분'], 'lt_days': None, 'rows': []})
            ve['rows'].append(rule)
            if rule['구분'] == '시방서':
                ve['kind'] = '시방서'
            if rule['lt_days'] and (ve['lt_days'] is None or rule['lt_days'] > ve['lt_days']):
                ve['lt_days'] = rule['lt_days']     # 거래처 기본값 = 그 거래처 행들의 최대 L/T (보수적)
            if rule['품번']:
                by_code[rule['품번']] = rule
                for old in rule['구품번']:
                    aliases[old] = rule['품번']
    except Exception as e:
        print(f'[상품매입 조건] 읽기 실패: {e!r:.120}')
    _GOODS_RULES_CACHE.update(mtime=mt, rules={'by_code': by_code, 'by_vendor': by_vendor, 'aliases': aliases})
    return _GOODS_RULES_CACHE['rules']


def _goods_aliases():
    """구품번 → 신품번 (예: 곤약밥 I0019/I0020/I0021 유상사급 → I0159/I0160/I0161 상품매입, 2026-09-17).
    재고·발주 이력이 구품번에 남아 있으므로 완제품 집계 시 신품번으로 합산한다."""
    return _load_goods_rules().get('aliases', {})


def _goods_canon(code):
    c = (code or '').strip().upper()
    return _goods_aliases().get(c, c)


def _goods_rule_for(code, vendor):
    """품번 규칙 우선, 없으면 거래처 규칙. 반환 (rule|None, vendor_entry|None)."""
    rules = _load_goods_rules()
    rule = rules['by_code'].get((code or '').upper())
    vk = _goods_vendor_key(vendor)
    ve = rules['by_vendor'].get(vk)
    if ve is None and vk:
        for k, e in rules['by_vendor'].items():     # 부분 일치 (ERP명이 더 길 때)
            if k and (k in vk or vk in k):
                ve = e
                break
    return rule, ve


def _goods_reorder_items(lead=None):
    """상품매입(I코드) 완제품 발주 타이밍 (2026-09-17, 사용자 "상품매입도 진행").
    자재(A~D)와 달리 BOM 전개 없이 완제품 자체를 구매발주로 사입하므로
    판매속도(완결 3개월 추세가중, SALES_DF I코드) vs 아마란스 완제품 현재고(+구매발주 미입고 120일) 로 소진일을 본다.
    리드타임은 구매발주→입고 실측(_calc_leadtimes, I코드 48품번 표본 있음). 소진일 45일 미만만 반환."""
    if SALES_DF is None or SALES_DF.empty or STOCK_DF is None or STOCK_DF.empty:
        return []
    vel = _sales_velocity()
    vtrend = {}
    if vel:
        # 판매 API 모드: 월판매 = 일 판매속도(최근 4주·3개월 가중)×30, 추세 = 4주÷3개월
        yms, w, sales = ['vel'], [1.0], {}
        for code, v in vel['by'].items():
            if code[:1] != 'I':
                continue
            c = _goods_canon(code)
            sales.setdefault(c, [0.0])[0] += v['v'] * 30
            vtrend.setdefault(c, []).append((v['v28'], v['v90']))
        vtrend = {c: (round(min(2.0, max(0.5, sum(a for a, _ in x) / sum(b for _, b in x))), 2) if sum(b for _, b in x) > 0 else 1.0)
                  for c, x in vtrend.items()}
    else:
        yms = _complete_months(3)
        if not yms:
            return []
        w = [0.5, 0.3, 0.2][:len(yms)]
        sub = SALES_DF[(SALES_DF['ym'].astype(str).isin(yms)) & (SALES_DF['prefix'] == 'I')]
        sales = {}
        for _, r in sub.iterrows():
            c = _goods_canon(r['code'])          # 구품번 판매 이력 → 신품번으로 합산
            sales.setdefault(c, [0.0] * len(yms))[yms.index(str(r['ym']))] += float(r['ea'])
    if not sales:
        return []
    stock, names = {}, {}
    aliases = _goods_aliases()
    merged_old = {}                          # 신품번 ← 합산된 구품번 목록 (표시용)
    if '품번' in STOCK_DF.columns:
        for _, r in STOCK_DF.iterrows():
            raw = str(r.get('품번', '')).strip().upper()
            c = aliases.get(raw, raw)
            if c in sales:
                stock[c] = stock.get(c, 0) + _num(r.get('현재고', 0))
                if raw != c:
                    merged_old.setdefault(c, set()).add(raw)
                else:
                    names[c] = str(r.get('품명', '')).strip()
                names.setdefault(c, str(r.get('품명', '')).strip())
    # 발주 원천 두 가지: BOM 없는 순수 사입 = 구매발주(ORDER_DF, 입고 실측 리드 있음) /
    # BOM 있는 예외사급(I+BOM 17종) = 외주발주(WP_ORDER_DF, 입고일 없음 → 발주일→납기일 중앙값을 계획 리드로 사용)
    incoming, last_vendor, last_order, last_src = {}, {}, {}, {}
    wp_gaps = {}
    cut = (datetime.now() - _timedelta(days=120)).strftime('%Y%m%d')
    cut_lead = (datetime.now() - _timedelta(days=365)).strftime('%Y%m%d')
    for src, df_ in (('po', ORDER_DF), ('wp', WP_ORDER_DF)):
        if df_ is None or df_.empty or '품번' not in df_.columns or '발주일자' not in df_.columns:
            continue
        for _, r in df_.sort_values('발주일자').iterrows():
            raw = str(r.get('품번', '')).strip().upper()
            c = aliases.get(raw, raw)
            if c not in sales:
                continue
            if raw != c:
                merged_old.setdefault(c, set()).add(raw)
            d = str(r.get('발주일자', '')).replace('-', '')[:8]
            if not last_order.get(c) or d >= last_order[c]:
                last_order[c] = d
                last_vendor[c] = str(r.get('거래처명', '')).strip()
                last_src[c] = src
            names.setdefault(c, str(r.get('품명', '')).strip())
            if d >= cut:
                rem = _num(r.get('발주수량', 0)) - _num(r.get('입고수량', 0))
                if rem > 0:
                    incoming[c] = incoming.get(c, 0) + rem
            if src == 'wp' and d >= cut_lead:
                due = str(r.get('납기일자', '')).replace('-', '')[:8]
                if len(due) == 8 and due.isdigit() and len(d) == 8:
                    try:
                        gap = (datetime.strptime(due, '%Y%m%d') - datetime.strptime(d, '%Y%m%d')).days
                        if 0 <= gap <= 120:
                            wp_gaps.setdefault(c, []).append(gap)
                    except ValueError:
                        pass
    lead = dict(lead) if lead is not None else _calc_leadtimes()
    for c, g in wp_gaps.items():
        if c not in lead:            # 구매발주 실측이 없으면 외주발주 납기 기준(계획 리드)
            g.sort()
            lead[c] = {'days': g[len(g) // 2], 'n': len(g), 'src': '납기'}
    out = []
    for c, v in sales.items():
        mean = sum(v) / len(v)
        fc = sum(a * b for a, b in zip(v, w)) / (sum(w) or 1)     # 추세가중 월판매
        if fc <= 0:
            continue
        daily = fc / 30
        st, inc = stock.get(c, 0), incoming.get(c, 0)
        days_left = st / daily
        if st <= 0:
            level, days_left = 'out', 0.0
        elif days_left < 15:
            level = 'critical'
        elif days_left < 30:
            level = 'warning'
        elif days_left < 45:
            level = 'low'
        else:
            continue
        lt = lead.get(c)
        # 상품매입은 ERP에 발주·입고를 같은 날 등록하는 경우가 많아 실측/납기 리드가 0~1일로 나옴 → 의미 없는 값이라 기본 리드로 대체
        lead_note = ''
        if lt and lt['days'] <= 1:
            lead_note = f"발주·입고 동일자 등록 {lt['n']}회 → 실측 불가"
            lt = None
        # 업체 조건표(상품매입_업체조건.csv, 2026-09-17): 품번 규칙 > 거래처 규칙의 L/T 상한이 실측보다 우선.
        # 시방서 업체(데이웰즈·조운정미·더고은)는 출고일을 시방서로 지정하므로 리드 미적용(src='spec').
        rule, ve = _goods_rule_for(c, last_vendor.get(c, ''))
        src = last_src.get(c, '')
        vendor_note = ''
        if merged_old.get(c):
            vendor_note = '구품번 ' + '·'.join(sorted(merged_old[c])) + ' 합산'
        if rule:
            # 품번이 조건표에 있으면 조건표 거래처가 기준 (ERP 최근 발주처가 다르면 참고로 표기 — 예: I0097 표=청통본가, 최근 발주=조운정미)
            ev = last_vendor.get(c, '')
            if ev and _goods_vendor_key(ev) != _goods_vendor_key(rule['거래처']):
                vendor_note = (vendor_note + ' · ' if vendor_note else '') + f'최근 발주처 {ev}'
            last_vendor[c] = rule['거래처']
            ve = _goods_rule_for(c, rule['거래처'])[1] or ve
        if ve and ve.get('kind') == '시방서':
            src, lt, lead_note = 'spec', None, '시방서 출고 업체 — 출고일은 시방서로 지정'
        elif rule and rule.get('lt_days'):
            lt, lead_note = {'days': rule['lt_days'], 'n': 0, 'src': '업체표'}, ''
        elif ve and ve.get('lt_days'):
            lt, lead_note = {'days': ve['lt_days'], 'n': 0, 'src': '업체표'}, ''
        out.append({'code': c, 'name': names.get(c, ''), 'qty': int(st), 'incoming': int(inc), 'lead_note': lead_note,
                    'rule': rule, 'vendor_note': vendor_note, 'vendor_rule': {'거래처': ve['거래처'], 'kind': ve['kind'], 'lt_days': ve['lt_days']} if ve else None,
                    'monthly_avg': int(fc), 'trend': vtrend.get(c, 1.0) if vel else (round(fc / mean, 2) if mean else 1.0),
                    'days_left': round(days_left, 1), 'days_left_incoming': round((st + inc) / daily, 1),
                    'level': level, 'lead': lt, 'vendor': last_vendor.get(c, ''), 'last_order': last_order.get(c, ''),
                    'src': src, 'basis': 'sales'})
    out.sort(key=lambda x: x['days_left'])
    return out


@app.route('/api/reorder_advice', methods=['GET'])
@cached_api()
def api_reorder_advice():
    """지금/곧 발주해야 할 품목 — 재고 소진일이 실측 리드타임 안으로 들어온 것.
    소진일 ≤ 리드타임 → now(지금 발주) / ≤ 리드타임+7일 → soon(이번주 발주).
    scope: jasa(자사 자재) / outsource(외주 자재) / goods(상품매입 완제품, 2026-09-17)."""
    SAFETY = 7          # 발주 여유일
    DEFAULT_LEAD = 14   # 리드타임 표본 없을 때 가정값
    lead = _calc_leadtimes()
    ratio = _trend_ratio_by_material()   # 판매 추세계수 (최근 3개월 가중/평균)
    fmul = (lambda c: 1.0) if _sales_velocity() else (lambda c: ratio.get(c, 1.0))   # 판매속도 모드는 이미 최근 반영 → 곱하지 않음
    rows = []
    for scope in ('jasa', 'outsource'):
        for a in _stock_alert_items(scope):
            lt = lead.get(a['code'])
            base = lt['days'] if lt else DEFAULT_LEAD
            if a['days_left'] <= base:
                urgency = 'now'
            elif a['days_left'] <= base + SAFETY:
                urgency = 'soon'
            else:
                continue
            rows.append({
                'code': a['code'], 'name': a['name'], 'scope': scope,
                'qty': a['qty'], 'days_left': a['days_left'],
                'lead_days': lt['days'] if lt else None,
                'lead_n': lt['n'] if lt else 0,
                'urgency': urgency,
                'vendors': a.get('vendors', []),
                'trend': ratio.get(a['code'], 1.0),
                'forecast': int(a['monthly_avg'] * fmul(a['code'])),
            })
    # 상품매입 완제품: 이미 발주된 미입고분까지 합쳐도 리드+여유일을 못 넘길 때만 대상
    for a in _goods_reorder_items(lead):
        lt = a['lead']
        base = lt['days'] if lt else DEFAULT_LEAD
        if a['days_left_incoming'] > base + SAFETY:
            continue
        if a['days_left'] <= base:
            urgency = 'now'
        elif a['days_left'] <= base + SAFETY:
            urgency = 'soon'
        else:
            continue
        rows.append({
            'code': a['code'], 'name': a['name'], 'scope': 'goods', 'src': a['src'],
            'qty': a['qty'], 'incoming': a['incoming'], 'days_left': a['days_left'],
            'lead_days': lt['days'] if lt else None, 'lead_n': lt['n'] if lt else 0,
            'lead_src': (lt or {}).get('src', '실측') if lt else '', 'lead_note': a['lead_note'], 'vendor_note': a['vendor_note'],
            'urgency': urgency, 'vendors': [a['vendor']] if a['vendor'] else [],
            'trend': a['trend'], 'forecast': a['monthly_avg'],
        })
    rows.sort(key=lambda x: (0 if x['urgency'] == 'now' else 1, x['days_left']))
    return jsonify({'items': rows[:40], 'total': len(rows),
                    'safety_days': SAFETY, 'default_lead': DEFAULT_LEAD})


@app.route('/api/purchase_req_draft', methods=['GET'])
@cached_api()
def api_purchase_req_draft():
    """청구요청 초안 — 발주 타이밍 대상 품목을 아마란스 청구 라인 형태로 자동 구성.
    제안수량 = (리드타임+7일)분 소비 + 1개월 운영분 − 현재고 (10 단위 올림).
    ※ 아마란스 등록 API(api20A02I02401)는 24011로 차단 상태(더존 개통 대기)
       → 지금은 초안 생성·복사용. 개통되면 이 데이터를 그대로 등록 바디로 전송."""
    import math
    from datetime import datetime, timedelta
    lead = _calc_leadtimes()
    ratio = _trend_ratio_by_material()   # 추세 반영 예측 소비로 수량 산정

    # 품번별 최근 발주 거래처 (청구 라인의 거래처 제안)
    last_vendor = {}
    if ORDER_DF is not None and not ORDER_DF.empty:
        od = ORDER_DF[['품번', '발주일자', '거래처명']].dropna()
        od = od.sort_values('발주일자')
        for _, r in od.iterrows():
            last_vendor[str(r['품번']).strip().upper()] = str(r['거래처명']).strip()

    rows = []
    for scope in ('jasa', 'outsource'):
        for a in _stock_alert_items(scope):
            lt = lead.get(a['code'])
            base = lt['days'] if lt else 14
            if a['days_left'] > base + 7:
                continue
            monthly = a['monthly_avg'] * (1.0 if _sales_velocity() else ratio.get(a['code'], 1.0))   # 예측 월소비 (판매속도 모드는 이미 최근 반영)
            need = monthly * ((base + 7) / 30.0 + 1.0) - a['qty']
            if need <= 0:
                need = monthly  # 최소 1개월분
            qty = int(math.ceil(need / 10.0) * 10)   # 10 단위 올림
            due = (datetime.now() + timedelta(days=max(base, 3))).strftime('%Y-%m-%d')
            rows.append({
                'code': a['code'], 'name': a['name'], 'scope': scope,
                'stock': a['qty'], 'monthly_avg': int(round(monthly)),
                'days_left': a['days_left'],
                'lead_days': lt['days'] if lt else None,
                'qty': qty, 'due': due,
                'vendor': last_vendor.get(a['code'], (a.get('vendors') or [''])[0] if a.get('vendors') else ''),
            })
    # 상품매입 완제품(I코드): 제안수량 = (리드+7일)분 + 1개월 운영분 − 현재고 − 미입고
    for a in _goods_reorder_items(lead):
        lt = a['lead']
        base = lt['days'] if lt else 14
        if a['days_left_incoming'] > base + 7:
            continue
        monthly = a['monthly_avg']
        need = monthly * ((base + 7) / 30.0 + 1.0) - a['qty'] - a['incoming']
        if need <= 0:
            need = monthly
        qty = int(math.ceil(need / 10.0) * 10)
        # 업체 조건표 MOQ/PLT 적용 (품번이 채워진 규칙만): 낱개 단위 MOQ는 그대로, PLT 단위는 PLT당수량이 있을 때 환산·PLT 배수 올림
        moq_note = ''
        rule = a.get('rule')
        if rule and rule.get('MOQ'):
            unit, moq, moq_qty, plt = rule.get('MOQ단위', ''), rule['MOQ'], rule.get('MOQ수량') or 0, rule.get('PLT당수량') or 0
            if moq_qty > 0:
                qty = int(max(qty, moq_qty))
                if plt > 0:
                    qty = int(math.ceil(qty / plt) * plt)      # PLT 배수 올림
                moq_note = f'MOQ {int(moq):,}{unit}(={int(moq_qty):,}개)' + (f' · PLT {int(plt):,}개 단위' if plt > 0 else '')
            else:
                moq_note = f'MOQ {int(moq):,}{unit} (판매단위 수량 미입력 → 환산 못함)'
            if rule.get('비고'):
                moq_note += ' · ' + rule['비고']
        due = (datetime.now() + timedelta(days=max(base, 3))).strftime('%Y-%m-%d')
        rows.append({
            'code': a['code'], 'name': a['name'], 'scope': 'goods', 'src': a['src'],
            'stock': a['qty'], 'incoming': a['incoming'], 'monthly_avg': int(monthly),
            'days_left': a['days_left'], 'lead_days': lt['days'] if lt else None,
            'qty': qty, 'due': due, 'vendor': a['vendor'] or last_vendor.get(a['code'], ''),
            'moq_note': moq_note + ((' · ' if moq_note else '') + a['vendor_note'] if a['vendor_note'] else ''),
        })
    rows.sort(key=lambda x: x['days_left'])
    return jsonify({'items': rows, 'total': len(rows),
                    'req_dt': datetime.now().strftime('%Y-%m-%d'),
                    'api_ready': False})   # 더존 개통 시 True로 전환 + 실등록


# ────────────────────────────────────────────
# 알림 시스템 — 신규 품절 / 지금 발주 진입 / 데이터 이상 (즉시) + 아침 요약 (매일 08:30)
#   저장: data/alerts_log.jsonl (대시보드 🔔), 상태: data/_alert_state.json (중복 방지)
#   발송 채널(.env 있을 때만): 메일 ALERT_SMTP_* / 카톡 KAKAO_REST_KEY+KAKAO_REFRESH_TOKEN
# ────────────────────────────────────────────
_NOTIFY_LOG = f'{DATA_DIR}/alerts_log.jsonl'
_NOTIFY_STATE = f'{DATA_DIR}/_alert_state.json'
_NOTIFY_INTERVAL_SEC = 30 * 60
_NOTIFY_LOCK = threading.Lock()


def _notify_state_load():
    try:
        with open(_NOTIFY_STATE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _notify_state_save(st):
    tmp = _NOTIFY_STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, _NOTIFY_STATE)


def _notify_channels():
    """설정된 발송 채널 — 값이 채워진 것만."""
    ch = {}
    if os.environ.get('ALERT_SMTP_HOST') and os.environ.get('ALERT_MAIL_TO'):
        ch['email'] = True
    if os.environ.get('KAKAO_REST_KEY') and os.environ.get('KAKAO_REFRESH_TOKEN'):
        ch['kakao'] = True
    return ch


def _send_email(subject, body):
    import smtplib
    from email.mime.text import MIMEText
    host = os.environ['ALERT_SMTP_HOST']
    port = int(os.environ.get('ALERT_SMTP_PORT', '587'))
    user = os.environ.get('ALERT_SMTP_USER', '')
    pw = os.environ.get('ALERT_SMTP_PASS', '')
    to = [x.strip() for x in os.environ['ALERT_MAIL_TO'].split(',') if x.strip()]
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = user or to[0]
    msg['To'] = ', '.join(to)
    with smtplib.SMTP(host, port, timeout=20) as s:
        s.ehlo()
        if port != 25:
            s.starttls()
        if user and pw:
            s.login(user, pw)
        s.sendmail(msg['From'], to, msg.as_string())


def _kakao_access_token(st):
    """리프레시 토큰으로 액세스 토큰 갱신(상태파일에 캐시, 5시간 유효)."""
    import requests as _rq
    now = time.time()
    if st.get('kakao_at') and st.get('kakao_at_exp', 0) > now + 300:
        return st['kakao_at']
    r = _rq.post('https://kauth.kakao.com/oauth/token', data={
        'grant_type': 'refresh_token',
        'client_id': os.environ['KAKAO_REST_KEY'],
        'refresh_token': os.environ['KAKAO_REFRESH_TOKEN'],
    }, timeout=15).json()
    if 'access_token' not in r:
        raise RuntimeError(f'kakao token: {str(r)[:120]}')
    st['kakao_at'] = r['access_token']
    st['kakao_at_exp'] = now + int(r.get('expires_in', 21599))
    return st['kakao_at']


def _send_kakao(text, st):
    """카카오톡 '나에게 보내기'(기본 텍스트 템플릿)."""
    import requests as _rq
    tok = _kakao_access_token(st)
    tpl = {'object_type': 'text', 'text': text[:1000],
           'link': {'web_url': 'https://8.235.41.127.sslip.io',
                    'mobile_web_url': 'https://8.235.41.127.sslip.io'}}
    r = _rq.post('https://kapi.kakao.com/v2/api/talk/memo/default/send',
                 headers={'Authorization': 'Bearer ' + tok},
                 data={'template_object': json.dumps(tpl, ensure_ascii=False)}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f'kakao send {r.status_code}: {r.text[:120]}')


def _notify(level, kind, title, body, st=None, send=True):
    """알림 1건 기록 + (설정된 채널로) 발송. level: error|warn|info"""
    from datetime import datetime
    rec = {'id': int(time.time() * 1000), 'ts': datetime.now().strftime('%Y-%m-%d %H:%M'),
           'level': level, 'kind': kind, 'title': title, 'body': body, 'sent': {}}
    own_state = st is None
    if own_state:
        st = _notify_state_load()
    if send:
        for ch in _notify_channels():
            try:
                if ch == 'email':
                    _send_email('[매홍 대시보드] ' + title, body)
                elif ch == 'kakao':
                    _send_kakao(title + '\n' + body, st)
                rec['sent'][ch] = True
            except Exception as e:
                rec['sent'][ch] = f'실패: {str(e)[:100]}'
                print(f'[알림] {ch} 발송 실패: {e!r:.150}')
    with open(_NOTIFY_LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    if own_state:
        _notify_state_save(st)
    return rec


def _notify_snapshot():
    """현재 상태 스냅샷 — 품절/지금발주/데이터이상."""
    out, now_items = [], []
    for scope in ('jasa', 'outsource'):
        for a in _stock_alert_items(scope):
            if a['level'] == 'out':
                out.append({'code': a['code'], 'name': a['name'], 'scope': scope})
    lead = _calc_leadtimes()
    for scope in ('jasa', 'outsource'):
        for a in _stock_alert_items(scope):
            lt = lead.get(a['code'])
            base = lt['days'] if lt else 14
            if a['days_left'] <= base and a['level'] != 'out':
                now_items.append({'code': a['code'], 'name': a['name'], 'scope': scope,
                                  'days_left': a['days_left'], 'lead': base})
    # 상품매입 완제품(I코드): 품절/지금발주 모두 알림 대상 (미입고분으로 충분하면 제외)
    try:
        for a in _goods_reorder_items(lead):
            base = a['lead']['days'] if a['lead'] else 14
            if a['days_left_incoming'] > base + 7:
                continue
            if a['level'] == 'out':
                out.append({'code': a['code'], 'name': a['name'], 'scope': 'goods'})
            elif a['days_left'] <= base:
                now_items.append({'code': a['code'], 'name': a['name'], 'scope': 'goods',
                                  'days_left': a['days_left'], 'lead': base})
    except Exception as e:
        print(f'[알림] 상품매입 스냅샷 오류: {e!r:.120}')
    with app.test_request_context('/api/data_health'):
        health = api_data_health().get_json()
    return {'out': out, 'now': now_items, 'health': health}


def _notify_check(force_daily=False):
    """변화분만 알림 + 아침 요약. 30분마다 스케줄러가 호출."""
    from datetime import datetime
    with _NOTIFY_LOCK:
        st = _notify_state_load()
        snap = _notify_snapshot()
        made = []
        sc = lambda x: f"{x['scope'][0]}:{x['code']}"

        new_out = [x for x in snap['out'] if sc(x) not in set(st.get('out', []))]
        if new_out and st.get('out') is not None:     # 첫 실행은 기준선만 저장
            body = '\n'.join(f"· {x['code']} {x['name']} ({ {'jasa': '자사', 'outsource': '외주', 'goods': '상품매입'}.get(x['scope'], x['scope']) })" for x in new_out[:15])
            made.append(_notify('error', 'out', f'🔴 신규 품절 {len(new_out)}건', body, st))
        st['out'] = [sc(x) for x in snap['out']]

        new_now = [x for x in snap['now'] if sc(x) not in set(st.get('now', []))]
        if new_now and st.get('now') is not None:
            body = '\n'.join(f"· {x['code']} {x['name']} — 소진 {x['days_left']:.0f}일 ≤ 리드 {x['lead']}일" for x in new_now[:15])
            made.append(_notify('warn', 'reorder', f'🕐 지금 발주 진입 {len(new_now)}건', body, st))
        st['now'] = [sc(x) for x in snap['now']]

        h = snap['health']
        bad = [x['label'] for x in h.get('items', []) if x['status'] in ('error', 'warn')]
        if bad and bad != st.get('health_bad', []):
            made.append(_notify('warn' if h['overall'] == 'warn' else 'error', 'health',
                                '⚠️ 데이터 ' + h['summary'], '\n'.join(
                                    f"· {x['label']}: {x['msg']} (최신 {x['latest_ym']})"
                                    for x in h['items'] if x['status'] in ('error', 'warn')), st))
        st['health_bad'] = bad

        today = datetime.now().strftime('%Y-%m-%d')
        if force_daily or (datetime.now().hour * 60 + datetime.now().minute >= 8 * 60 + 30
                           and st.get('last_daily') != today):
            body = (f"품절 {len(snap['out'])}건 · 지금 발주 {len(snap['now'])}건 · 데이터 {h['summary']}\n"
                    + ('\n'.join(f"· {x['code']} {x['name']}" for x in snap['out'][:8]) if snap['out'] else '품절 없음 👍'))
            # 거래처 포털 입력 현황 (2026-09-17): 요약에 한 줄 + 입력하던 거래처가 영업일 3일 이상 끊기면 별도 경고
            try:
                vs = _vendor_idle_status()
                if vs:
                    body += '\n거래처 입력: ' + ' · '.join(f"{v['vendor']} {v['label']}" for v in vs)
                    idle = [v for v in vs if v['idle_bdays'] is not None and v['idle_bdays'] >= 3]
                    if idle:
                        made.append(_notify('warn', 'vendor', f'👥 거래처 입력 중단 {len(idle)}곳',
                                            '\n'.join(f"· {v['vendor']}: 마지막 입력 {v['last']} (영업일 {v['idle_bdays']}일 경과)" for v in idle)
                                            + '\n담당자에게 포털 입력을 요청하세요 (📝 외주재고 입력 메뉴에서 링크 복사)', st))
            except Exception as e:
                print(f'[알림] 거래처 입력 현황 오류: {e!r:.120}')
            made.append(_notify('info', 'daily', f'☀️ 아침 요약 {today}', body, st))
            st['last_daily'] = today
            # 매월 1~5일: 전월 월간 리포트 자동 생성 (1회)
            try:
                if datetime.now().day <= 5:
                    pym = _ym_shift(datetime.now().strftime('%Y%m'), -1)
                    if not os.path.exists(f'{_REPORT_DIR}/{pym}_월간리포트.html'):
                        _report_save(pym)
                        made.append(_notify('info', 'report', f'📊 {pym[:4]}년 {int(pym[4:6])}월 월간 리포트 생성',
                                            f'대시보드 → 월간 리포트 메뉴 또는 /report/monthly?ym={pym}', st))
            except Exception as e:
                print(f'[리포트] 자동 생성 실패: {e!r:.120}')

        st['last_run'] = datetime.now().strftime('%Y-%m-%d %H:%M')
        _notify_state_save(st)
        return made


def _vendor_idle_status(now=None):
    """운영 거래처(ACTIVE_VENDORS)별 마지막 포털 입력과 영업일 경과. 한 번도 입력 안 한 곳은 idle_bdays=None('미시작')."""
    from datetime import datetime as _dt
    now = now or _dt.now()
    last = {}
    for e in _vendor_entries_read():
        v = e.get('vendor')
        if v in ACTIVE_VENDORS:
            last[v] = max(last.get(v, 0.0), float(e.get('ts', 0) or 0))
    out = []
    for v in ACTIVE_VENDORS:
        if not last.get(v):
            out.append({'vendor': v, 'last': '', 'idle_bdays': None, 'label': '미시작'})
            continue
        d = _dt.fromtimestamp(last[v])
        bdays, cur = 0, d.date()
        while cur < now.date():
            cur += _timedelta(days=1)
            if cur.weekday() < 5:
                bdays += 1
        out.append({'vendor': v, 'last': d.strftime('%m/%d %H:%M'), 'idle_bdays': bdays,
                    'label': d.strftime('%m/%d') + ('' if bdays < 3 else f' (영업일 {bdays}일 경과)')})
    return out


def _notify_scheduler():
    threading.Event().wait(90)   # 기동 직후 DF 안정화 대기
    while True:
        try:
            made = _notify_check()
            if made:
                print(f'[알림] {len(made)}건 발생')
        except Exception as e:
            print(f'[알림] 점검 오류: {e!r:.150}')
        threading.Event().wait(_NOTIFY_INTERVAL_SEC)


def _start_notify_scheduler():
    t = threading.Thread(target=_notify_scheduler, daemon=True, name='notify')
    t.start()
    print('[알림] 스케줄러 시작 (30분 주기, 채널: ' + (', '.join(_notify_channels()) or '대시보드만') + ')')


@app.route('/api/notifications', methods=['GET'])
def api_notifications():
    """최근 알림 50건 (로그 없으면 현재 스냅샷을 가상 알림으로)."""
    items = []
    if os.path.exists(_NOTIFY_LOG):
        with open(_NOTIFY_LOG, encoding='utf-8') as f:
            lines = f.readlines()[-50:]
        for ln in lines:
            try:
                items.append(json.loads(ln))
            except Exception:
                pass
        items.reverse()
    st = _notify_state_load()
    return jsonify({'items': items, 'channels': list(_notify_channels()),
                    'last_run': st.get('last_run'), 'live': False})


@app.route('/api/notify_run', methods=['POST'])
def api_notify_run():
    """지금 점검 실행 (?daily=1 이면 아침 요약 강제)."""
    made = _notify_check(force_daily=request.args.get('daily') == '1')
    return jsonify({'ok': True, 'made': len(made), 'items': made})


@app.route('/api/notify_test', methods=['POST'])
def api_notify_test():
    """채널 발송 테스트."""
    rec = _notify('info', 'test', '🔔 테스트 알림', '대시보드 알림 채널이 정상 연결되었습니다.')
    return jsonify({'ok': True, 'sent': rec['sent'], 'channels': list(_notify_channels())})


# ────────────────────────────────────────────
# 월간 리포트 — 매출·발주·입고·단가변동·재고마감·알림을 한 페이지로 (인쇄→PDF)
#   /report/monthly?ym=YYYYMM (라이브 렌더) · /api/monthly_report (JSON)
#   매월 1~5일 아침 점검 때 전월 리포트를 data/리포트/에 자동 저장 + 알림
# ────────────────────────────────────────────
_REPORT_DIR = f'{DATA_DIR}/리포트'


def _ym_dash(ym):
    return f'{ym[:4]}-{ym[4:6]}'


def _report_data(ym):
    """리포트용 집계 — 전부 이미 로드된 DF/기존 API에서 계산."""
    from collections import Counter
    ymd = _ym_dash(ym)
    prev = _ym_shift(ym, -1)
    prevd = _ym_dash(prev)
    months = [_ym_shift(ym, -i) for i in range(11, -1, -1)]   # 12개월 (오래된→최신)

    def _find(rows, key):
        return next((r for r in rows if r.get('ym') == key), None) or {}

    # 매출 — 온라인팀 판매 API 공급가 (2026-09-23: Monday 매출현황 대체). mon = 같은 자료의 월별 합계(추이용)
    with app.test_request_context('/api/sales_summary'):
        mon = api_sales_summary().get_json()
    with app.test_request_context('/api/sales_qty'):
        sq = api_sales_qty().get_json()
    mon_cur, mon_prev = _find(mon.get('monthly', []), ymd), _find(mon.get('monthly', []), prevd)
    sq_cur, sq_prev = _find(sq.get('monthly', []), ymd), _find(sq.get('monthly', []), prevd)
    sq_cls = {k[:-4]: v for k, v in sq_cur.items() if k.endswith('_amt') and k != 'total_amt'}

    # 발주 / 입고 (아마란스)
    def _month_sum(df, dcol, acol, key):
        if df is None or df.empty or dcol not in df.columns or acol not in df.columns:
            return 0
        m = df[dcol].astype(str).str.replace('-', '', regex=False).str[:6] == key
        return int(sum(_num(v) for v in df.loc[m, acol]))
    po_cur = _month_sum(ORDER_DF, '발주일자', '합계금액', ym)
    po_prev = _month_sum(ORDER_DF, '발주일자', '합계금액', prev)
    rcv_cur = _month_sum(RCV_DF, '입고일자', '합계금액', ym)
    rcv_prev = _month_sum(RCV_DF, '입고일자', '합계금액', prev)
    po_vendor, po_item = Counter(), {}
    if ORDER_DF is not None and not ORDER_DF.empty:
        m = ORDER_DF['발주일자'].astype(str).str.replace('-', '', regex=False).str[:6] == ym
        for _, r in ORDER_DF.loc[m].iterrows():
            amt = _num(r.get('합계금액', 0))
            po_vendor[str(r.get('거래처명', '')).strip() or '(미지정)'] += amt
            code = str(r.get('품번', '')).strip().upper()
            po_item.setdefault(code, {'code': code, 'name': str(r.get('품명', '')).strip(), 'amt': 0, 'qty': 0})
            po_item[code]['amt'] += amt
            po_item[code]['qty'] += _num(r.get('발주수량', 0))
    trend = [{'ym': _ym_dash(k), 'sales': _find(mon.get('monthly', []), _ym_dash(k)).get('amount', 0) or 0,
              'po': _month_sum(ORDER_DF, '발주일자', '합계금액', k)} for k in months]

    # 단가 변동 (해당 월에 바뀐 것)
    with app.test_request_context('/api/price_changes'):
        pc = api_price_changes().get_json()
    price = [x for x in pc.get('items', []) if str(x.get('changed', ''))[:7] == ymd][:10]

    # 재고 마감 스냅샷 (data/마감/)
    def _closing(name, qcol):
        f = f'{DATA_DIR}/마감/{ym}_{name}_마감.csv'
        if not os.path.exists(f):
            return None
        try:
            df = pd.read_csv(f, dtype=str, encoding='utf-8-sig').fillna('')
            qc = qcol if qcol in df.columns else None
            zero = int(sum(1 for v in df[qc] if _num(v) <= 0)) if qc else None
            return {'rows': int(len(df)), 'zero': zero, 'file': os.path.basename(f)}
        except Exception:
            return None
    closing = {'외주': _closing('재고일지', '현재고량'), '자사': _closing('자사재고', '총재고')}

    # 알림 이력
    alerts = Counter()
    if os.path.exists(_NOTIFY_LOG):
        with open(_NOTIFY_LOG, encoding='utf-8') as f:
            for ln in f:
                try:
                    rec = json.loads(ln)
                    if str(rec.get('ts', ''))[:7] == ymd and rec.get('kind') != 'test':
                        alerts[rec['kind']] += 1
                except Exception:
                    pass
    with app.test_request_context('/api/data_health'):
        health = api_data_health().get_json()

    def _pct(a, b):
        return round((a - b) / b * 100, 1) if b else None
    return {
        'ym': ym, 'ymd': ymd, 'prev': prevd,
        'sales_monday': {'cur': mon_cur.get('amount', 0) or 0, 'prev': mon_prev.get('amount', 0) or 0,
                         'pct': _pct(mon_cur.get('amount', 0) or 0, mon_prev.get('amount', 0) or 0)},
        'sales_csv': {'cur': sq_cur.get('total_amt', 0) or 0, 'prev': sq_prev.get('total_amt', 0) or 0,
                      'pct': _pct(sq_cur.get('total_amt', 0) or 0, sq_prev.get('total_amt', 0) or 0),
                      'qty': sq_cur.get('total', 0) or 0, 'is_current': bool(sq_cur.get('is_current')),
                      'by_class': sq_cls},
        'po': {'cur': po_cur, 'prev': po_prev, 'pct': _pct(po_cur, po_prev)},
        'rcv': {'cur': rcv_cur, 'prev': rcv_prev, 'pct': _pct(rcv_cur, rcv_prev)},
        'po_vendor_top': [{'name': k, 'amt': int(v)} for k, v in po_vendor.most_common(5)],
        'po_item_top': sorted(po_item.values(), key=lambda x: -x['amt'])[:5],
        'trend': trend, 'price_changes': price, 'closing': closing,
        'alerts': dict(alerts), 'health': {'overall': health.get('overall'), 'summary': health.get('summary')},
        'generated': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }


_REPORT_CSS = """
<style>
 body{font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;background:#f4f5f7;color:#1e293b;margin:0}
 .page{max-width:1000px;margin:0 auto;padding:28px 32px 60px;background:#fff}
 h1{font-size:22px;margin:0 0 4px} .sub{color:#64748b;font-size:12px;margin-bottom:18px}
 .nav a{font-size:12px;color:#4f46e5;text-decoration:none;margin-right:12px}
 .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0 22px}
 .kpi{border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px}
 .kpi .l{font-size:11px;color:#64748b;font-weight:600} .kpi .v{font-size:20px;font-weight:800;margin-top:4px;font-variant-numeric:tabular-nums}
 .kpi .d{font-size:11px;margin-top:3px;font-weight:700} .up{color:#dc2626} .down{color:#2563eb} .flat{color:#64748b}
 h2{font-size:14px;margin:22px 0 8px;padding-bottom:6px;border-bottom:2px solid #e2e8f0}
 table{width:100%;border-collapse:collapse;font-size:12px} th{background:#f8fafc;color:#475569;font-weight:700;text-align:left;padding:6px 8px;border-bottom:1px solid #e2e8f0}
 td{padding:6px 8px;border-bottom:1px solid #f1f5f9} .num{text-align:right;font-variant-numeric:tabular-nums}
 .grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
 .bar{display:flex;align-items:center;gap:8px;font-size:11px;margin:3px 0} .bar .lb{width:58px;color:#64748b}
 .bar .b{height:12px;border-radius:3px;background:#6366f1} .bar .b2{height:12px;border-radius:3px;background:#f59e0b}
 .bar .n{color:#475569;font-variant-numeric:tabular-nums}
 .note{font-size:11px;color:#94a3b8;margin-top:24px}
 .badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:700}
 .ok{background:#f0fdf4;color:#16a34a} .warn{background:#fefce8;color:#ca8a04} .err{background:#fef2f2;color:#dc2626}
 @media print{body{background:#fff} .page{padding:0} .nav{display:none} .kpis{grid-template-columns:repeat(4,1fr)}}
</style>"""


def _fmt_won(v):
    v = int(v or 0)
    if abs(v) >= 100_000_000:
        return f'{v/100_000_000:.1f}억'
    if abs(v) >= 10_000:
        return f'{v/10_000:,.0f}만'
    return f'{v:,}'


def _report_html(d):
    def pct(p):
        if p is None:
            return '<span class="d flat">전월 데이터 없음</span>'
        cls = 'up' if p > 0 else ('down' if p < 0 else 'flat')
        return f'<span class="d {cls}">{"▲" if p > 0 else ("▼" if p < 0 else "―")} {abs(p)}% vs 전월</span>'
    def kpi(label, cur, p, extra=''):
        if not cur:   # 당월 데이터 자체가 없음(예: Monday 매출 미입력) — −100%로 오해되지 않게
            return f'<div class="kpi"><div class="l">{label}</div><div class="v" style="color:#94a3b8">미입력</div><span class="d flat">해당 월 데이터 없음</span>{extra}</div>'
        return f'<div class="kpi"><div class="l">{label}</div><div class="v">{_fmt_won(cur)}원</div>{pct(p)}{extra}</div>'
    y, m = d['ym'][:4], int(d['ym'][4:6])
    esc = lambda s: str(s).replace('&', '&amp;').replace('<', '&lt;')
    cur_tag = ' <span class="badge warn">진행중</span>' if d['sales_csv']['is_current'] else ''
    h = [_REPORT_CSS, '<div class="page">',
         f'<div class="nav"><a href="/report/monthly?ym={_ym_shift(d["ym"], -1)}">◀ 전월</a>'
         f'<a href="/report/monthly?ym={_ym_shift(d["ym"], 1)}">다음달 ▶</a><a href="/">대시보드</a>'
         f'<a href="#" onclick="window.print();return false">🖨 인쇄/PDF</a></div>',
         f'<h1>📊 {y}년 {m}월 월간 리포트{cur_tag}</h1>',
         f'<div class="sub">매홍 구매/외주 대시보드 · 생성 {d["generated"]} · 데이터 상태: '
         f'<span class="badge {"ok" if d["health"]["overall"]=="ok" else ("warn" if d["health"]["overall"]=="warn" else "err")}">{esc(d["health"]["summary"])}</span></div>',
         '<div class="kpis">',
         kpi('매출 (판매자료·공급가)', d['sales_monday']['cur'], d['sales_monday']['pct'],
             f'<div class="d flat">판매 {int(d["sales_csv"]["qty"]):,}개</div>'),
         kpi('구매 발주액', d['po']['cur'], d['po']['pct']),
         kpi('입고액', d['rcv']['cur'], d['rcv']['pct']),
         '</div>']
    # 12개월 추이
    mx_s = max([t['sales'] for t in d['trend']] + [1]); mx_p = max([t['po'] for t in d['trend']] + [1])
    h.append('<h2>12개월 추이</h2><div class="grid2"><div><div style="font-size:11px;color:#64748b;margin-bottom:4px">매출 (판매자료·공급가)</div>')
    for t in d['trend']:
        h.append(f'<div class="bar"><span class="lb">{t["ym"][2:]}</span><div class="b" style="width:{t["sales"]/mx_s*260:.0f}px"></div><span class="n">{_fmt_won(t["sales"])}</span></div>')
    h.append('</div><div><div style="font-size:11px;color:#64748b;margin-bottom:4px">구매 발주액</div>')
    for t in d['trend']:
        h.append(f'<div class="bar"><span class="lb">{t["ym"][2:]}</span><div class="b2" style="width:{t["po"]/mx_p*260:.0f}px"></div><span class="n">{_fmt_won(t["po"])}</span></div>')
    h.append('</div></div>')
    # 채널 매출 분류
    if d['sales_csv']['by_class']:
        h.append('<h2>매출 분류별</h2><table><tr><th>분류</th><th class="num">매출액</th><th class="num">비중</th></tr>')
        tot = d['sales_csv']['cur'] or 1
        for k, v in sorted(d['sales_csv']['by_class'].items(), key=lambda x: -x[1]):
            h.append(f'<tr><td>{esc(k)}</td><td class="num">{int(v):,}원</td><td class="num">{v/tot*100:.1f}%</td></tr>')
        h.append('</table>')
    # 발주 TOP
    h.append('<div class="grid2"><div><h2>발주 거래처 TOP 5</h2><table><tr><th>거래처</th><th class="num">발주액</th></tr>')
    h += [f'<tr><td>{esc(x["name"])}</td><td class="num">{x["amt"]:,}원</td></tr>' for x in d['po_vendor_top']] or ['<tr><td colspan=2>없음</td></tr>']
    h.append('</table></div><div><h2>발주 품목 TOP 5</h2><table><tr><th>품번</th><th>품명</th><th class="num">발주액</th></tr>')
    h += [f'<tr><td>{esc(x["code"])}</td><td>{esc(x["name"][:26])}</td><td class="num">{int(x["amt"]):,}원</td></tr>' for x in d['po_item_top']] or ['<tr><td colspan=3>없음</td></tr>']
    h.append('</table></div></div>')
    # 단가 변동
    h.append('<h2>이달 단가 변동 (영향액순)</h2>')
    if d['price_changes']:
        h.append('<table><tr><th>품번</th><th>품명</th><th>거래처</th><th class="num">이전→현재</th><th class="num">변동</th><th class="num">월 영향액</th></tr>')
        for x in d['price_changes']:
            cls = 'up' if x['pct'] > 0 else 'down'
            h.append(f'<tr><td>{esc(x["code"])}</td><td>{esc(x["name"][:24])}</td><td>{esc(x["vendor"][:12])}</td>'
                     f'<td class="num">{x["prev"]:,.0f}→{x["cur"]:,.0f}</td><td class="num {cls}"><b>{"+" if x["pct"]>0 else ""}{x["pct"]}%</b></td>'
                     f'<td class="num">{x["impact"]:+,}원</td></tr>')
        h.append('</table>')
    else:
        h.append('<div style="font-size:12px;color:#64748b">이달 3% 이상 단가 변동 없음</div>')
    # 재고 마감 + 알림
    h.append('<div class="grid2"><div><h2>재고 마감 스냅샷</h2><table><tr><th>구분</th><th class="num">품목수</th><th class="num">재고 0</th><th>파일</th></tr>')
    for k, v in d['closing'].items():
        h.append(f'<tr><td>{k}</td><td class="num">{v["rows"]:,}</td><td class="num">{v["zero"] if v["zero"] is not None else "-"}</td><td style="font-size:10.5px;color:#94a3b8">{esc(v["file"])}</td></tr>'
                 if v else f'<tr><td>{k}</td><td colspan=3 style="color:#94a3b8">마감 파일 없음 (해당 월 자동보관 전)</td></tr>')
    h.append('</table></div><div><h2>알림 이력</h2><table><tr><th>종류</th><th class="num">건수</th></tr>')
    names = {'out': '🔴 신규 품절', 'reorder': '🕐 지금 발주 진입', 'health': '⚠️ 데이터 이상', 'daily': '☀️ 아침 요약', 'report': '📊 월간 리포트'}
    h += [f'<tr><td>{names.get(k, k)}</td><td class="num">{v}</td></tr>' for k, v in sorted(d['alerts'].items())] or ['<tr><td colspan=2 style="color:#94a3b8">알림 없음</td></tr>']
    h.append('</table></div></div>')
    h.append('<div class="note">※ 매출은 온라인팀 판매자료(쿠팡·마트 등 온라인+오프라인 납품) 공급가 합계, VAT 제외 · 발주/입고는 아마란스 합계금액 · 단가 변동은 발주단가 3% 이상 변경분 · 재고 마감은 매월 자동보관 스냅샷</div>')
    h.append('</div>')
    return '<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>월간 리포트 ' + d['ymd'] + '</title></head><body>' + ''.join(h) + '</body></html>'


@app.route('/api/monthly_report', methods=['GET'])
def api_monthly_report():
    ym = (request.args.get('ym') or '').strip() or _ym_shift(datetime.now().strftime('%Y%m'), -1)
    return jsonify(_report_data(ym))


@app.route('/report/monthly', methods=['GET'])
def report_monthly():
    ym = (request.args.get('ym') or '').strip()
    if not (len(ym) == 6 and ym.isdigit()):
        ym = _ym_shift(datetime.now().strftime('%Y%m'), -1)   # 기본 = 전월
    return _report_html(_report_data(ym))


def _report_save(ym):
    """리포트 HTML을 data/리포트/에 저장 (자동 월간 생성용). 경로 반환."""
    os.makedirs(_REPORT_DIR, exist_ok=True)
    path = f'{_REPORT_DIR}/{ym}_월간리포트.html'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(_report_html(_report_data(ym)))
    return path


# ────────────────────────────────────────────
# 소비 추세 예측 · 수급 플래너(완제품) · 거래처 스코어카드
# ────────────────────────────────────────────
def _complete_months(n=3):
    """SALES_DF 기준 최근 완결월 n개 (이번 달 제외, 최신→과거)."""
    if SALES_DF is None or SALES_DF.empty:
        return []
    cur = datetime.now().strftime('%Y%m')
    yms = sorted({y for y in SALES_DF['ym'].astype(str) if y < cur}, reverse=True)
    return yms[:n]


def _trend_ratio_by_material():
    """자재별 추세계수 = 최근 완결 3개월 가중(0.5/0.3/0.2) ÷ 단순평균. 0.5~2.0 클립.
    재고경고의 monthly_avg(단순평균)에 곱해 '다음달 예측 소비'로 씀."""
    vel = _sales_velocity()
    if vel:
        # 판매 API 모드: 자재 추세 = Σ(완제품 4주 속도×소요) ÷ Σ(3개월 속도×소요). 표시용(소비량에 이미 최근 속도가 반영돼 곱하지 않음)
        a, b = {}, {}
        for code, v in vel['by'].items():
            for mat, q in _explode_bom(code).items():
                if mat[:1] in ('A', 'B', 'C', 'D'):
                    a[mat] = a.get(mat, 0) + v['v28'] * q
                    b[mat] = b.get(mat, 0) + v['v90'] * q
        return {m: round(min(2.0, max(0.5, a[m] / b[m])), 2) for m in a if b.get(m, 0) > 0}
    yms = _complete_months(3)
    if len(yms) < 2:
        return {}
    w = [0.5, 0.3, 0.2][:len(yms)]
    ser = {}   # material → [m0, m1, m2] (최신부터)
    sub = SALES_DF[SALES_DF['ym'].astype(str).isin(yms)]
    for _, r in sub.iterrows():
        idx = yms.index(str(r['ym']))
        for mat, q in _explode_bom(str(r['code']).strip().upper()).items():
            if mat[:1] not in ('A', 'B', 'C', 'D'):
                continue
            ser.setdefault(mat, [0.0] * len(yms))[idx] += float(r['ea']) * q
    out = {}
    for mat, v in ser.items():
        mean = sum(v) / len(v)
        if mean <= 0:
            continue
        wsum = sum(w) or 1
        weighted = sum(a * b for a, b in zip(v, w)) / wsum
        out[mat] = round(min(2.0, max(0.5, weighted / mean)), 2)
    return out


@app.route('/api/supply_plan', methods=['GET'])
@cached_api()
def api_supply_plan():
    """완제품 수급 플래너 — 월판매(완결 3개월 추세가중) vs 완제품 재고 vs 생산계획/외주입고예정.
    커버리지(주) = 재고 ÷ 주판매. 계획 반영 커버리지도 함께."""
    if SALES_DF is None or SALES_DF.empty:
        return jsonify({'items': [], 'total': 0, 'reason': '판매 데이터 없음'})
    _al = _goods_aliases()   # 구품번→신품번 (상품매입 전환 품목, 재고·계획·미입고를 신품번으로 합산)
    vel = _sales_velocity()
    vtr = {}
    sales = {}   # code → [by month]
    if vel:
        # 판매 API 모드(2026-09-23): 월판매 = 일 판매속도(최근 4주 70%·3개월 30%)×30, 추세 = 4주÷3개월
        yms, w = [], [1.0]
        acc = {}
        for code, v in vel['by'].items():
            c = _al.get(code, code)
            sales.setdefault(c, [0.0])[0] += v['v'] * 30
            a, b = acc.get(c, (0.0, 0.0))
            acc[c] = (a + v['v28'], b + v['v90'])
        vtr = {c: (round(min(2.0, max(0.5, a / b)), 2) if b > 0 else 1.0) for c, (a, b) in acc.items()}
    else:
        yms = _complete_months(3)
        w = [0.5, 0.3, 0.2][:len(yms)]
        sub = SALES_DF[SALES_DF['ym'].astype(str).isin(yms)]
        for _, r in sub.iterrows():
            c = _al.get(str(r['code']).strip().upper(), str(r['code']).strip().upper())
            sales.setdefault(c, [0.0] * len(yms))[yms.index(str(r['ym']))] += float(r['ea'])

    stock, names = {}, {}
    if STOCK_DF is not None and not STOCK_DF.empty and '품번' in STOCK_DF.columns:
        for _, r in STOCK_DF.iterrows():
            raw = str(r.get('품번', '')).strip().upper()
            c = _al.get(raw, raw)
            if c[:1] in ('G', 'H', 'I'):
                stock[c] = stock.get(c, 0) + _num(r.get('현재고', 0))
                if raw == c:
                    names[c] = str(r.get('품명', '')).strip()
                names.setdefault(c, str(r.get('품명', '')).strip())
    if BOM_DF is not None and not BOM_DF.empty:
        for _, r in BOM_DF.drop_duplicates('모품번').iterrows():
            names.setdefault(str(r['모품번']).strip().upper(), str(r['모품명']).strip())
    # 품명 보강 — 재고·BOM에 없는 상품매입(I) 등은 외주발주/발주/출하/판매SKU매핑에서
    for df_, cc, nc in ((WP_ORDER_DF, '품번', '품명'), (ORDER_DF, '품번', '품명'), (SHIP_DF, '품번', '품명')):
        if df_ is not None and not df_.empty and cc in df_.columns and nc in df_.columns:
            for _, r in df_.drop_duplicates(cc).iterrows():
                c = str(r.get(cc, '')).strip().upper()
                n = str(r.get(nc, '')).strip()
                if c[:1] in ('G', 'H', 'I') and n and n != 'nan':
                    names.setdefault(c, n)
    try:
        mp = pd.read_csv(f'{BASE_DIR}/SKU매핑_확정.csv', dtype=str, encoding='utf-8-sig').fillna('')
        for _, r in mp.iterrows():
            c = str(r.get('확정품번', '')).strip().upper()
            if c and r.get('판매제품명'):
                names.setdefault(c, str(r['판매제품명']).strip())
    except Exception:
        pass

    plan = {}    # 미마감 생산지시 잔량
    if WO_DF is not None and not WO_DF.empty and '품번' in WO_DF.columns:
        for _, r in WO_DF.iterrows():
            if str(r.get('closeDt', '')).strip() not in ('', 'nan'):
                continue
            c = _al.get(str(r.get('품번', '')).strip().upper(), str(r.get('품번', '')).strip().upper())
            rem = _num(r.get('지시수량', 0)) - _num(r.get('workQt', 0))
            if c[:1] in ('G', 'H', 'I') and rem > 0:
                plan[c] = plan.get(c, 0) + rem
    incoming = {}   # 외주발주 미입고
    if WP_ORDER_DF is not None and not WP_ORDER_DF.empty:
        cut = (datetime.now() - _timedelta(days=120)).strftime('%Y%m%d')
        for _, r in WP_ORDER_DF.iterrows():
            if str(r.get('발주일자', '')).replace('-', '')[:8] < cut:
                continue
            c = _al.get(str(r.get('품번', '')).strip().upper(), str(r.get('품번', '')).strip().upper())
            rem = _num(r.get('발주수량', 0)) - _num(r.get('입고수량', 0))
            if c[:1] in ('G', 'H', 'I') and rem > 0:
                incoming[c] = incoming.get(c, 0) + rem

    items = []
    for c, v in sales.items():
        mean = sum(v) / len(v) if v else 0
        wsum = sum(w) or 1
        fc = sum(a * b for a, b in zip(v, w)) / wsum if v else 0     # 추세가중 월판매
        if fc <= 0:
            continue
        weekly = fc / 4.33
        st = stock.get(c, 0)
        pl = plan.get(c, 0) + incoming.get(c, 0)
        cov = st / weekly
        cov_plan = (st + pl) / weekly
        if st <= 0:
            level = 'out'
        elif cov < 2:
            level = 'critical'
        elif cov < 4:
            level = 'warning'
        elif cov < 8:
            level = 'low'
        else:
            level = 'ok'
        items.append({'code': c, 'name': names.get(c, ''), 'cls': {'G': '자사', 'H': '유상사급', 'I': '상품매입'}.get(c[:1], ''),
                      'monthly': int(fc), 'trend': vtr.get(c, 1.0) if vel else (round(fc / mean, 2) if mean else 1.0),
                      'stock': int(st), 'plan': int(plan.get(c, 0)), 'incoming': int(incoming.get(c, 0)),
                      'cov_weeks': round(cov, 1), 'cov_plan_weeks': round(cov_plan, 1), 'level': level})
    # 채널 재고 합산 (2026-09-23): 쿠팡 센터·마트 매장 재고까지 더한 커버. 판정(level)은 창고 기준 유지(생산 착수 판단),
    # 채널 포함 커버는 참고 — 창고가 부족해도 채널이 4주 이상이면 '채널 여유', 창고는 괜찮아도 채널 합계가 2주 미만이면 '채널 부족'.
    try:
        chs = _channel_stock_by_code()
    except Exception:
        chs = {}
    for x in items:
        cs = chs.get(x['code'])
        weekly = x['monthly'] / 4.33 if x['monthly'] else 0
        x['ch_stock'] = int(cs['qty']) if cs else None
        x['cov_ch_weeks'] = round((max(x['stock'], 0) + cs['qty']) / weekly, 1) if (cs and weekly) else None
        x['ch_note'] = ''
        if cs and x['cov_ch_weeks'] is not None:
            if x['level'] in ('out', 'critical') and x['cov_ch_weeks'] >= 4:
                x['ch_note'] = '채널 여유'
            elif x['level'] in ('ok', 'low') and cs['qty'] / weekly < 1:
                x['ch_note'] = '채널 부족'
    order = {'out': 0, 'critical': 1, 'warning': 2, 'low': 3, 'ok': 4}
    items.sort(key=lambda x: (order[x['level']], x['cov_plan_weeks']))
    summary = {k: sum(1 for x in items if x['level'] == k) for k in order}
    # 전체 반환 — 프론트가 검색 필터별 집계를 다시 계산하므로 잘라내지 않음 (완제품 ~100개)
    return jsonify({'items': items, 'total': len(items), 'summary': summary, 'months': yms, 'basis': _vel_basis_label(vel)})


# ====== 채널 품절 경보 (2026-09-23, 판매 API의 점재고·센터재고 ÷ POS 판매속도) ======
CH_STOCK_SKIP = {'homeplus_hyper', 'homeplus_express', 'costco'}   # 점재고 미제공(통합 homeplus에만) / 코스트코는 납품만


@app.route('/api/channel_stock', methods=['GET'])
@cached_api()
def api_channel_stock():
    """채널(쿠팡 센터·마트 매장) 재고가 며칠 버티는지 — 최신 재고 ÷ 최근 14일 POS 일평균.
    level: out(재고 0, 판매 중) / critical(3일 미만) / warning(7일 미만). stale=최근 7일 재고값이 변하지 않음(이마트 등 스냅샷 부족 → 신뢰 낮음).
    ?all=1 이면 7일 이상도 포함."""
    df = SALES_DAILY_DF
    if df is None or df.empty:
        return jsonify({'items': [], 'total': 0, 'reason': '판매 API 자료 없음'})
    show_all = request.args.get('all') == '1'
    d = df[~df['channel'].isin(CH_STOCK_SKIP)]
    end = pd.to_datetime(d['date'].max())
    dt = pd.to_datetime(d['date'])
    d = d.assign(_dt=dt)[dt >= end - pd.Timedelta(days=44)]
    w14 = d[d['_dt'] >= end - pd.Timedelta(days=13)]
    ours = {}
    if STOCK_DF is not None and not STOCK_DF.empty and '품번' in STOCK_DF.columns:
        _al = _goods_aliases()
        for _, r in STOCK_DF.iterrows():
            c = str(r.get('품번', '')).strip().upper()
            c = _al.get(c, c)
            ours[c] = ours.get(c, 0) + _num(r.get('현재고', 0))
    items = []
    for (ch, sku), g in d.groupby(['channel', 'sku']):
        st = g[g['stock'].notna()].sort_values('_dt')
        if st.empty:
            continue
        last = st.iloc[-1]
        if (end - last['_dt']).days > 7:          # 재고 스냅샷이 오래됨
            continue
        p = w14[(w14['channel'] == ch) & (w14['sku'] == sku)]
        pdays = int(p['pos'].notna().sum())
        if pdays < 5:                              # POS가 5일 미만이면 판매속도 신뢰 불가
            continue
        pos_d = float(p['pos'].sum()) / pdays
        if pos_d <= 0:
            continue
        stock = float(last['stock'])
        cover = stock / pos_d if stock > 0 else 0.0
        level = 'out' if stock <= 0 else ('critical' if cover < 3 else ('warning' if cover < 7 else 'ok'))
        if level == 'ok' and not show_all:
            continue
        s7 = st[st['_dt'] >= end - pd.Timedelta(days=6)]['stock']
        dl = g[g['ea'] > 0].sort_values('_dt')
        code = str(g['code'].iloc[0] or '')
        items.append({'channel': ch, 'channel_name': str(g['channel_name'].iloc[0]), 'channel_type': str(g['channel_type'].iloc[0]),
                      'sku': sku, 'code': code, 'name': str(g['name'].iloc[-1]),
                      'stock': int(stock), 'stock_date': last['date'], 'pos_d': round(pos_d, 1), 'cover': round(cover, 1),
                      'level': level, 'stale': bool(len(s7) >= 4 and s7.nunique() == 1 and stock > 0),
                      'last_delivery': dl['date'].iloc[-1] if len(dl) else '',
                      'last_delivery_qty': int(dl['ea'].iloc[-1] / (dl['f'].iloc[-1] or 1)) if len(dl) else 0,
                      'ours': int(ours.get(code, 0)) if code else None})
    order = {'out': 0, 'critical': 1, 'warning': 2, 'ok': 3}
    items.sort(key=lambda x: (order[x['level']], x['stale'], x['cover'], -x['pos_d']))
    summary = {k: sum(1 for x in items if x['level'] == k) for k in ('out', 'critical', 'warning')}
    return jsonify({'items': items, 'total': len(items), 'summary': summary, 'as_of': end.strftime('%Y-%m-%d')})


def _channel_stock_by_code():
    """품번별 채널 재고 합계 (쿠팡 센터·마트 매장) — 채널·SKU별 최신 스냅샷(자료 최종일 7일 이내)의 합. {code: {'qty', 'n'}}"""
    df = SALES_DAILY_DF
    if df is None or df.empty:
        return {}
    d = df[(~df['channel'].isin(CH_STOCK_SKIP)) & df['stock'].notna() & (df['code'] != '')]
    if d.empty:
        return {}
    end = pd.to_datetime(df['date'].max())
    d = d[pd.to_datetime(d['date']) >= end - pd.Timedelta(days=7)]
    last = d.sort_values('date').groupby(['channel', 'sku']).tail(1)
    _al = _goods_aliases()
    out = {}
    for _, r in last.iterrows():
        c = _al.get(r['code'], r['code'])
        e = out.setdefault(c, {'qty': 0.0, 'n': 0})
        e['qty'] += max(float(r['stock']), 0.0) * float(r['f'] or 1)   # 세트 SKU는 환산계수만큼 단품으로
        e['n'] += 1
    return out


# ====== 판매 분석 3종 (2026-09-23): 납품 vs POS 괴리 · 채널 공급단가 변동 · 납품 요일 패턴 ======
@app.route('/api/sales_gap', methods=['GET'])
@cached_api()
def api_sales_gap():
    """최근 28일 납품 vs POS(실판매) — POS를 주는 채널·SKU만 비교(코스트코 등 POS 없는 채널 제외, POS 14일 이상 보고).
    ratio = 납품/POS. over(≥1.5): 채널에 재고가 쌓이는 중 → 곧 발주 감소 신호 / under(≤0.5): 채널 재고 소진 중 → 곧 추가 발주 신호."""
    df = SALES_DAILY_DF
    if df is None or df.empty:
        return jsonify({'items': [], 'reason': '판매 API 자료 없음'})
    end = pd.to_datetime(df['date'].max())
    start = end - pd.Timedelta(days=27)
    d = df[(pd.to_datetime(df['date']) >= start) & (df['code'] != '')].copy()
    pdays = d.groupby(['channel', 'sku'])['pos'].apply(lambda s: int(s.notna().sum()))
    ok = set(pdays[pdays >= 14].index)
    d = d[[k in ok for k in zip(d['channel'], d['sku'])]]
    if d.empty:
        return jsonify({'items': []})
    d['pos_ea'] = d['pos'].fillna(0) * d['f']
    names = _sales_name_map()
    stk = d[d['stock'].notna()].sort_values('date')
    items = []
    for code, g in d.groupby('code'):
        dl, ps = float(g['ea'].sum()), float(g['pos_ea'].sum())
        if ps < 100 and dl < 100:
            continue
        ratio = dl / ps if ps > 0 else None
        s = stk[stk['code'] == code]
        first = s.groupby(['channel', 'sku'])['stock'].first().sum() if len(s) else None
        last = s.groupby(['channel', 'sku'])['stock'].last().sum() if len(s) else None
        level = 'over' if (ratio is None or ratio >= 1.5) else ('under' if ratio <= 0.5 else 'ok')
        items.append({'code': code, 'name': names.get(code, '') or str(g['name'].iloc[-1]),
                      'delivery': int(dl), 'pos': int(ps), 'ratio': round(ratio, 2) if ratio is not None else None,
                      'stock_change': int(last - first) if first is not None else None,
                      'channels': ', '.join(sorted(set(g['channel_name']))[:3]), 'level': level})
    items.sort(key=lambda x: (x['level'] == 'ok', -abs((x['ratio'] or 9) - 1) * (x['pos'] + x['delivery'])))
    return jsonify({'items': items, 'from': start.strftime('%Y-%m-%d'), 'to': end.strftime('%Y-%m-%d'),
                    'summary': {k: sum(1 for x in items if x['level'] == k) for k in ('over', 'under', 'ok')}})


@app.route('/api/channel_price_changes', methods=['GET'])
@cached_api()
def api_channel_price_changes():
    """채널 공급단가 변동 — 채널·SKU별 unit_supply_price 시계열(최근 180일). 현재가 = 최근 연속 구간(2회 이상 관측, 단발 행사가 제외),
    이전가 = 그 직전 다른 가격. |변화| ≥ 3%. 월 영향액 = (현재가−이전가) × 최근 90일 납품수량 ÷ 3 (판매가 쪽이므로 인하=마진 압박)."""
    df = SALES_DAILY_DF
    if df is None or df.empty:
        return jsonify({'items': []})
    files = sorted(glob.glob(f'{DATA_DIR}/*_판매일별.csv'))
    if not files:
        return jsonify({'items': []})
    raw = pd.read_csv(files[-1], dtype=str, encoding='utf-8-sig', usecols=['date', 'channel', 'channel_name', 'sku', 'name', 'self_code',
                                                                          'delivery_qty', 'unit_supply_price']).fillna('')
    raw['p'] = pd.to_numeric(raw['unit_supply_price'], errors='coerce')
    raw['q'] = pd.to_numeric(raw['delivery_qty'], errors='coerce').fillna(0)
    end = pd.to_datetime(raw['date'].max())
    raw = raw[pd.to_datetime(raw['date']) >= end - pd.Timedelta(days=180)]
    code_of = dict(zip(df['sku'].astype(str), df['code']))
    names = _sales_name_map()
    cut90 = (end - pd.Timedelta(days=89)).strftime('%Y-%m-%d')
    items = []
    for (ch, sku), g in raw[raw['p'].notna() & (raw['p'] > 0)].sort_values('date').groupby(['channel', 'sku']):
        ps = list(g['p'].round(0)); ds = list(g['date'])
        if len(ps) < 3:
            continue
        cur = ps[-1]; i = len(ps) - 1
        while i > 0 and ps[i - 1] == cur:
            i -= 1
        if len(ps) - i < 2 or i == 0:        # 현재가가 1회뿐(단발) 이거나 변동 없음
            continue
        prev = ps[i - 1]
        pct = (cur - prev) / prev * 100
        if abs(pct) < 3:
            continue
        q90 = float(raw[(raw['channel'] == ch) & (raw['sku'] == sku) & (raw['date'] >= cut90)]['q'].sum())
        code = code_of.get(str(sku), '') or str(g['self_code'].iloc[-1])
        items.append({'channel_name': str(g['channel_name'].iloc[-1]), 'sku': sku, 'code': code,
                      'name': names.get(code, '') or str(g['name'].iloc[-1]), 'prev': int(prev), 'cur': int(cur),
                      'pct': round(pct, 1), 'since': ds[i], 'impact': int((cur - prev) * q90 / 3)})
    items.sort(key=lambda x: -abs(x['impact']))
    return jsonify({'items': items, 'to': end.strftime('%Y-%m-%d')})


@app.route('/api/delivery_weekday', methods=['GET'])
@cached_api()
def api_delivery_weekday():
    """최근 12주 납품 요일 패턴 — 요일별 납품 수량·금액, 온라인/오프라인, 주요 채널별 비중."""
    df = SALES_DAILY_DF
    if df is None or df.empty:
        return jsonify({'days': []})
    end = pd.to_datetime(df['date'].max())
    d = df[(pd.to_datetime(df['date']) >= end - pd.Timedelta(days=83)) & (df['dq'] > 0)].copy()
    d['wd'] = pd.to_datetime(d['date']).dt.dayofweek
    labels = ['월', '화', '수', '목', '금', '토', '일']
    tot = float(d['dq'].sum()) or 1
    top_ch = list(d.groupby('channel_name')['dq'].sum().sort_values(ascending=False).head(4).index)
    days = []
    for w in range(7):
        g = d[d['wd'] == w]
        days.append({'wd': labels[w], 'qty': int(g['dq'].sum()), 'amt': int(g['amt'].sum()), 'share': round(float(g['dq'].sum()) / tot * 100, 1),
                     'online': int(g[g['channel_type'] == 'online']['dq'].sum()), 'offline': int(g[g['channel_type'] == 'offline']['dq'].sum()),
                     'by_ch': {c: int(g[g['channel_name'] == c]['dq'].sum()) for c in top_ch}})
    return jsonify({'days': days, 'channels': top_ch, 'from': (end - pd.Timedelta(days=83)).strftime('%Y-%m-%d'), 'to': end.strftime('%Y-%m-%d')})


@app.route('/api/vendor_scorecard', methods=['GET'])
@cached_api()
def api_vendor_scorecard():
    """거래처 스코어카드(최근 12개월) — 구매(발주정보) + 외주(외주발주정보) 통합.
    구매: 납기준수율·입고충족률·리드타임·단가안정·발주액 / 외주: 발주액·충족률·단가안정
    (외주는 입고정보에 입고일이 없고 납기일자=발주일자라 납기·리드 산출 불가).
    점수 = 납기 40 + 충족 30 + 단가안정 30 (없는 지표는 가중치 제외)."""
    import bisect
    has_po = ORDER_DF is not None and not ORDER_DF.empty
    has_wp = WP_ORDER_DF is not None and not WP_ORDER_DF.empty
    if not has_po and not has_wp:
        return jsonify({'items': [], 'total': 0})
    today = datetime.now()
    cut = (today - _timedelta(days=365)).strftime('%Y%m%d')
    settle = (today - _timedelta(days=14)).strftime('%Y%m%d')   # 충족률은 14일 지난 발주만

    rcv = {}   # (vendor, code) → sorted 입고일 list (구매 입고만 존재)
    if RCV_DF is not None and not RCV_DF.empty:
        for _, r in RCV_DF.iterrows():
            v = str(r.get('거래처명', '')).strip()
            c = str(r.get('품번', '')).strip().upper()
            d = str(r.get('입고일자', '')).replace('-', '')[:8]
            if v and c and len(d) == 8:
                rcv.setdefault((v, c), []).append(d)
    for k in rcv:
        rcv[k].sort()

    V = {}

    def _slot(v):
        return V.setdefault(v, {'vendor': v, 'items': set(),
                                'po': {'amt': 0.0, 'docs': set(), 'ord_q': 0.0, 'rcv_q': 0.0},
                                'wp': {'amt': 0.0, 'docs': set(), 'ord_q': 0.0, 'rcv_q': 0.0},
                                'ontime': 0, 'late': 0, 'gaps': []})

    # ── 구매 발주 ──
    if has_po:
        po_col = '발주번호' if '발주번호' in ORDER_DF.columns else None
        for _, r in ORDER_DF.iterrows():
            od = str(r.get('발주일자', '')).replace('-', '')[:8]
            if len(od) != 8 or od < cut:
                continue
            v = str(r.get('거래처명', '')).strip() or '(미지정)'
            c = str(r.get('품번', '')).strip().upper()
            s = _slot(v); k = s['po']
            k['amt'] += _num(r.get('합계금액', 0))
            k['docs'].add(str(r.get(po_col, od)) if po_col else od)
            s['items'].add(c)
            if od <= settle:
                k['ord_q'] += _num(r.get('발주수량', 0))
                k['rcv_q'] += min(_num(r.get('입고수량', 0)), _num(r.get('발주수량', 0)))
            due = str(r.get('납기일자', '')).replace('-', '')[:8]
            lst = rcv.get((v, c))
            if lst:
                i = bisect.bisect_left(lst, od)
                if i < len(lst):
                    try:
                        gap = (datetime.strptime(lst[i], '%Y%m%d') - datetime.strptime(od, '%Y%m%d')).days
                        if 0 <= gap <= 120:
                            s['gaps'].append(gap)
                            if len(due) == 8:
                                s['ontime' if lst[i] <= due else 'late'] += 1
                    except ValueError:
                        pass

    # ── 외주 발주 ── (합계금액 없음 → 공급가액, 문서번호=외주발주번호)
    if has_wp:
        wp_col = '외주발주번호' if '외주발주번호' in WP_ORDER_DF.columns else None
        for _, r in WP_ORDER_DF.iterrows():
            od = str(r.get('발주일자', '')).replace('-', '')[:8]
            if len(od) != 8 or od < cut:
                continue
            v = str(r.get('거래처명', '')).strip() or '(미지정)'
            c = str(r.get('품번', '')).strip().upper()
            s = _slot(v); k = s['wp']
            k['amt'] += _num(r.get('공급가액', 0))
            k['docs'].add(str(r.get(wp_col, od)) if wp_col else od)
            s['items'].add(c)
            if od <= settle:
                k['ord_q'] += _num(r.get('발주수량', 0))
                k['rcv_q'] += min(_num(r.get('입고수량', 0)), _num(r.get('발주수량', 0)))

    pc_by = {}
    for x in _detect_price_changes(ORDER_DF) + _detect_price_changes(WP_ORDER_DF):
        pc_by.setdefault(x['vendor'], []).append(x['pct'])

    def _fill(k):
        return round(min(100.0, k['rcv_q'] / k['ord_q'] * 100), 1) if k['ord_q'] > 0 else None

    items = []
    for v, s in V.items():
        po, wp = s['po'], s['wp']
        n_ot = s['ontime'] + s['late']
        ontime = round(s['ontime'] / n_ot * 100, 1) if n_ot else None
        ord_q = po['ord_q'] + wp['ord_q']; rcv_q = po['rcv_q'] + wp['rcv_q']
        fill = round(min(100.0, rcv_q / ord_q * 100), 1) if ord_q > 0 else None
        gaps = sorted(s['gaps'])
        lead = gaps[len(gaps) // 2] if gaps else None
        pcs = pc_by.get(v, [])
        avg_pct = round(sum(abs(p) for p in pcs) / len(pcs), 1) if pcs else 0.0
        stab = max(0.0, 100.0 - avg_pct * 2)
        parts = [(ontime, 0.4), (fill, 0.3), (stab, 0.3)]
        avail = [(val, wt) for val, wt in parts if val is not None]
        score = round(sum(val * wt for val, wt in avail) / (sum(wt for _, wt in avail) or 1)) if avail else None
        kind = 'both' if (po['docs'] and wp['docs']) else ('wp' if wp['docs'] else 'po')
        items.append({'vendor': v, 'kind': kind,
                      'amt': int(po['amt'] + wp['amt']), 'po_amt': int(po['amt']), 'wp_amt': int(wp['amt']),
                      'n_po': len(po['docs']) + len(wp['docs']), 'po_n': len(po['docs']), 'wp_n': len(wp['docs']),
                      'n_items': len(s['items']),
                      'ontime': ontime, 'ontime_n': n_ot, 'fill': fill, 'po_fill': _fill(po), 'wp_fill': _fill(wp),
                      'lead': lead, 'lead_n': len(gaps),
                      'price_changes': len(pcs), 'price_avg_pct': avg_pct, 'score': score})
    items.sort(key=lambda x: -x['amt'])
    n_wp = sum(1 for x in items if x['kind'] != 'po')
    return jsonify({'items': items, 'total': len(items), 'n_wp': n_wp, 'since': _ym_dash(cut[:6])})


@app.route('/api/vendor_orders', methods=['GET'])
@cached_api()
def api_vendor_orders():
    """거래처 최근 발주 이력 — kind=po(발주정보) | wp(외주발주정보), 문서(발주번호) 단위 최신순 limit건."""
    vendor = (request.args.get('vendor') or '').strip()
    kind = (request.args.get('kind') or 'po').strip()
    try:
        limit = max(1, min(50, int(request.args.get('limit', 10))))
    except ValueError:
        limit = 10
    if kind == 'wp':
        df, doc_col, amt_col = WP_ORDER_DF, '외주발주번호', '공급가액'
    else:
        df, doc_col, amt_col = ORDER_DF, '발주번호', '합계금액'
    if not vendor or df is None or df.empty or '거래처명' not in df.columns:
        return jsonify({'vendor': vendor, 'kind': kind, 'docs': [], 'total': 0})
    sub = df[df['거래처명'].astype(str).str.strip() == vendor]
    docs = {}
    for _, r in sub.iterrows():
        od = str(r.get('발주일자', '')).replace('-', '')[:8]
        doc = str(r.get(doc_col, '')).strip() or od
        d = docs.setdefault(doc, {'doc': doc, 'date': od, 'due': str(r.get('납기일자', '')).replace('-', '')[:8],
                                  'amt': 0.0, 'ord_q': 0.0, 'rcv_q': 0.0, 'items': []})
        if od > d['date']:
            d['date'] = od
        oq, rq = _num(r.get('발주수량', 0)), _num(r.get('입고수량', 0))
        d['amt'] += _num(r.get(amt_col, 0)); d['ord_q'] += oq; d['rcv_q'] += rq
        d['items'].append({'code': str(r.get('품번', '')).strip(), 'name': str(r.get('품명', '')).strip(),
                           'qty': oq, 'rcv': rq, 'price': _num(r.get('단가', 0))})
    out = sorted(docs.values(), key=lambda x: (x['date'], x['doc']), reverse=True)[:limit]
    for d in out:
        d['amt'] = int(d['amt'])
        d['date'] = f"{d['date'][:4]}-{d['date'][4:6]}-{d['date'][6:8]}" if len(d['date']) == 8 else d['date']
        d['due'] = f"{d['due'][:4]}-{d['due'][4:6]}-{d['due'][6:8]}" if len(d['due']) == 8 else ''
        d['status'] = 'done' if d['ord_q'] > 0 and d['rcv_q'] >= d['ord_q'] - 1e-9 else ('part' if d['rcv_q'] > 0 else 'open')
    return jsonify({'vendor': vendor, 'kind': kind, 'docs': out, 'total': len(docs)})


@app.route('/api/return_cost', methods=['GET'])
@cached_api()
def api_return_cost():
    """완제품 회송 원가 역산 — 완제품 품번×수량을 BOM으로 끝까지 전개해 원재료·부재료별 소요량과 금액.
    단가 우선순위: 최신 발주단가(발주정보) > 단가표(_단가.csv 최신단가) > BOM 자재단가. 출처를 함께 반환."""
    code = (request.args.get('code') or '').strip().upper()
    try:
        qty = float(request.args.get('qty') or 1)
    except ValueError:
        qty = 1.0
    if qty <= 0:
        qty = 1.0
    if not code:
        return jsonify({'error': '품번 없음'}), 400
    idx = _get_bom_index()
    if code not in idx:
        return jsonify({'error': f'{code}: BOM 없음', 'code': code}), 404

    # 자품 메타(단위·구분·품명·BOM단가)
    meta = {}
    pname = {}
    if BOM_DF is not None and not BOM_DF.empty:
        for _, r in BOM_DF.iterrows():
            c = str(r.get('자품번', '')).strip().upper()
            if c and c not in meta:
                meta[c] = {'name': str(r.get('자품명', '')).strip(), 'unit': str(r.get('자품단위', '')).strip(),
                           'cls': str(r.get('자품목구분', '')).strip(), 'bom_price': _num(r.get('자재단가', 0))}
            p = str(r.get('모품번', '')).strip().upper()
            if p and p not in pname:
                pname[p] = str(r.get('모품명', '')).strip()
    # 최신 발주단가
    last_po = {}
    if ORDER_DF is not None and not ORDER_DF.empty:
        for _, r in ORDER_DF.iterrows():
            c = str(r.get('품번', '')).strip().upper(); d = str(r.get('발주일자', '')).replace('-', '')[:8]
            p = _num(r.get('단가', 0))
            if c and p > 0 and len(d) == 8 and (c not in last_po or d > last_po[c][1]):
                last_po[c] = (p, d)
    # 단가표 = 합성 단가(아마란스 매입단가 > 엑셀). 발주단가는 위 last_po가 우선이라 여기선 나머지만
    tbl = {}
    for c, info in (PRICE_BY_CODE or {}).items():
        p = info.get('단가')
        if p and p > 0:
            tbl[c] = (float(p), '매입단가' if info.get('단가출처') == '매입단가' else ('단가표' if info.get('단가출처') == '엑셀' else '단가표'))

    RAW = {'원재료', '반제품(외주)'}
    SUB = {'부재료', '물류박스', '라벨스티커', 'RRP', '단상자', '부자재'}
    lines = []
    for c, per in sorted(_explode_bom(code).items()):
        if c == code:
            continue
        m = meta.get(c, {})
        cls_raw = m.get('cls', '')
        if cls_raw == '비용' or c.startswith('Z'):
            grp = '기타'
        elif cls_raw in RAW or c.startswith('A') or c.startswith('F'):
            grp = '원재료'
        elif cls_raw in SUB or c[:1] in ('B', 'C', 'D'):
            grp = '부재료'
        else:
            grp = '기타'
        if c in last_po:
            price, src = last_po[c][0], '발주 ' + f'{last_po[c][1][:4]}-{last_po[c][1][4:6]}-{last_po[c][1][6:]}'
        elif c in tbl:
            price, src = tbl[c]
        elif m.get('bom_price', 0) > 0:
            price, src = m['bom_price'], 'BOM'
        else:
            price, src = 0.0, '없음'
        total_qty = per * qty
        lines.append({'code': c, 'name': m.get('name', ''), 'grp': grp, 'cls': cls_raw, 'unit': m.get('unit', ''),
                      'per_unit': round(per, 6), 'total_qty': round(total_qty, 4),
                      'price': round(price, 2), 'price_src': src, 'amount': round(total_qty * price),
                      # 1개 기준 반올림 전 값 — 화면이 수량을 곱해 즉시 계산할 때 오차 없도록 (2026-09-23)
                      'unit_amount': per * price, 'unit_qty': per})
    order = {'원재료': 0, '부재료': 1, '기타': 2}
    lines.sort(key=lambda x: (order[x['grp']], -x['amount']))
    sub = {}
    for x in lines:
        sub[x['grp']] = sub.get(x['grp'], 0) + x['amount']
    total = sub.get('원재료', 0) + sub.get('부재료', 0)   # 기타(비용)는 총액 제외
    sale = 0
    try:
        sale = _num((SALE_PRICE or {}).get(code, 0))
    except Exception:
        sale = 0
    # BOM 종류(일반/외주/외주(미사용대체)) + 외주 거래처 — fetch_bom 외주BOM 수집분(2026-09-09)
    bom_kind, bom_vendor = '일반', ''
    if BOM_DF is not None and 'BOM종류' in BOM_DF.columns:
        _pr = BOM_DF[BOM_DF['모품번'].astype(str).str.strip().str.upper() == code]
        if not _pr.empty:
            bom_kind = str(_pr.iloc[0].get('BOM종류', '') or '일반')
            bom_vendor = str(_pr.iloc[0].get('외주거래처', '') or '') if '외주거래처' in _pr.columns else ''
    return jsonify({'code': code, 'name': pname.get(code, ''), 'qty': qty, 'lines': lines, 'sub': sub,
                    'total': total, 'unit_cost': round(total / qty, 2) if qty else 0,
                    'sale_price': sale, 'missing': sum(1 for x in lines if x['price_src'] == '없음'),
                    'bom_kind': bom_kind, 'bom_vendor': bom_vendor})


@app.route('/api/kpi_summary', methods=['GET'])
@cached_api(ttl=300)
def api_kpi_summary():
    """상단 KPI 요약 띠 — 오늘 봐야 할 숫자만 (각 패널 API 재사용, 캐시 덕에 가벼움)."""
    from datetime import datetime as _dt, timedelta as _td
    out = {}

    def _call(fn, path):
        try:
            with app.test_request_context(path):
                r = fn()
                r = r[0] if isinstance(r, tuple) else r
                return r.get_json() or {}
        except Exception as e:
            return {'_err': str(e)[:80]}

    # 재고 경고 (자사+외주)
    j = _stock_alert_items('jasa'); o = _stock_alert_items('outsource')
    out['stock'] = {
        'out': sum(1 for a in j + o if a['level'] == 'out'),
        'critical': sum(1 for a in j + o if a['level'] == 'critical'),
        'jasa_out': sum(1 for a in j if a['level'] == 'out'),
        'os_out': sum(1 for a in o if a['level'] == 'out'),
    }
    # 발주 타이밍
    ra = _call(api_reorder_advice, '/api/reorder_advice')
    items = ra.get('items', [])
    out['reorder'] = {'now': sum(1 for x in items if x.get('urgency') == 'now'),
                      'soon': sum(1 for x in items if x.get('urgency') == 'soon')}
    # 예상 입고 (부자재+원료)
    pp = _call(api_po_pending, '/api/po_pending')
    pitems = pp.get('items', [])
    today = _dt.now().strftime('%Y-%m-%d'); week = (_dt.now() + _td(days=7)).strftime('%Y-%m-%d')
    out['incoming'] = {
        'total': len(pitems),
        'unknown': sum(1 for x in pitems if not x.get('has_schedule')),
        'week': sum(1 for x in pitems if x.get('has_schedule') and today <= str(x.get('eta_sort', '')) <= week),
        'overdue': sum(1 for x in pitems if x.get('has_schedule') and str(x.get('eta_sort', '')) < today),
    }
    # 단가 변동
    pc = _call(api_price_changes, '/api/price_changes')
    out['price'] = {'total': pc.get('total', 0),
                    'up': sum(1 for x in pc.get('items', []) if x.get('pct', 0) > 0)}
    # 완제품 수급
    sp = _call(api_supply_plan, '/api/supply_plan')
    s = sp.get('summary', {}) or {}
    out['supply'] = {'out': s.get('out', 0), 'critical': s.get('critical', 0), 'warning': s.get('warning', 0)}
    # 데이터 건강
    dh = _call(api_data_health, '/api/data_health')
    out['health'] = {'overall': dh.get('overall', ''), 'summary': dh.get('summary', '')}
    out['at'] = _dt.now().strftime('%H:%M')
    return jsonify(out)


@app.route('/api/price_changes', methods=['GET'])
@cached_api()
def api_price_changes():
    """단가 변동 감지 — 발주정보의 품목별 단가 시계열에서 최근(180일) 변경 감지.
    현재가 = 최신 발주 단가, 이전가 = 그와 다른 직전 단가. |변화율| 3% 이상만."""
    items = _detect_price_changes(ORDER_DF)
    return jsonify({'items': items[:40], 'total': len(items)})


def _detect_price_changes(df):
    """발주 DataFrame(발주정보/외주발주정보 공용)에서 품목별 단가 변동 목록 산출.
    필요 컬럼: 품번·품명·거래처명·발주일자·단가·발주수량. 영향액 큰 순 정렬."""
    from datetime import datetime, timedelta
    if df is None or df.empty:
        return []
    recent_cut = (datetime.now() - timedelta(days=180)).strftime('%Y%m%d')
    qty3m_cut = (datetime.now() - timedelta(days=90)).strftime('%Y%m%d')

    series = {}   # code → list[(date, price, vendor, name, qty)]
    for _, r in df.iterrows():
        code = str(r.get('품번', '')).strip().upper()
        if not code or code[:1] in ('Z',):     # ZQ 비용성 품번 제외
            continue
        d = str(r.get('발주일자', '')).replace('-', '')[:8]
        p = _num(r.get('단가', 0))
        if len(d) != 8 or p <= 0:
            continue
        series.setdefault(code, []).append(
            (d, p, str(r.get('거래처명', '')).strip(), str(r.get('품명', '')).strip(),
             _num(r.get('발주수량', 0))))

    items = []
    for code, rows in series.items():
        rows.sort(key=lambda x: x[0])
        cur_price = rows[-1][1]
        # 현재가 연속 구간의 시작(=변경일)과 그 직전의 다른 단가
        changed, prev_price = rows[-1][0], None
        for d, p, _, _, _ in reversed(rows):
            if abs(p - cur_price) < 1e-9:
                changed = d
            else:
                prev_price = p
                break
        if prev_price is None or changed < recent_cut:
            continue
        pct = (cur_price - prev_price) / prev_price * 100
        if abs(pct) < 3:
            continue
        qty3m = sum(q for d, _, _, _, q in rows if d >= qty3m_cut)
        items.append({
            'code': code, 'name': rows[-1][3], 'vendor': rows[-1][2],
            'prev': round(prev_price, 2), 'cur': round(cur_price, 2),
            'pct': round(pct, 1),
            'changed': f'{changed[:4]}-{changed[4:6]}-{changed[6:8]}',
            'impact': int((cur_price - prev_price) * qty3m / 3),   # 월 환산 영향액
        })
    items.sort(key=lambda x: -abs(x['impact']))
    return items


def _box_tokens_from_raw(m_str):
    """단상자/박스 전용 추상 토큰 추출 (모듈 수준 — API에서도 재사용).
    LOOCV 최적화 결과: 골지(이중/단일벽)·레이어·인쇄지·인쇄도수·라미·표면처리 분리.
    인쇄/라미 구분이 가격을 좌우하므로 별도 토큰으로 강조."""
    import re as _r
    toks = []
    s = m_str.replace(' ', '').replace('.', '')

    # 골 타입 — 이중벽(EB/BB/AB/BA/AA/EE) vs 단일벽(A/B/E/F)
    for fl in ['EB골', 'BB골', 'AB골', 'BA골', 'AA골', 'EE골']:
        if fl in s:
            toks.append('이중벽'); toks.append(fl); break
    else:
        for fl in ['E골', 'B골', 'A골', 'F골']:
            if fl in s:
                toks.append('단일벽'); toks.append(fl); break

    # 레이어 수
    n_layers = len(_r.findall(r'(?:SK|SQ|SC|TLB|KLB|AK|WK|RIV|S|K|W|CK)\d*', s, _r.IGNORECASE))
    if n_layers >= 5:   toks.append('5겹')
    elif n_layers >= 3: toks.append('3겹')
    elif n_layers >= 2: toks.append('2겹')

    # 인쇄지 / 인쇄 도수 (가격 핵심)
    if _r.search(r'인쇄지|SC\d', m_str): toks.append('인쇄지')
    pm = _r.search(r'(\d+)도', m_str)
    if pm:
        toks.append('인쇄됨')
        toks.append('고도수' if int(pm.group(1)) >= 4 else '저도수')

    # 라미네이션
    if '무광' in m_str: toks.append('무광라미')
    if '유광' in m_str: toks.append('유광라미')

    # 기타 재질 특성
    if '후렉소' in m_str: toks.append('후렉소')
    if '합지'  in m_str:  toks.append('합지')
    if '삼면접착' in m_str: toks.append('삼면접착')
    if _r.search(r'RIV|IVY|아이보리|로얄', m_str, _r.IGNORECASE): toks.append('아이보리지')

    return toks if toks else None


_PCALC_CAT_MAP = {'파우치': '파우치', '단상자': '단상자', 'RRP 및 물류박스': '박스'}


def _material_tokens(m_str, raw_cat):
    """재질 문자열 → 토큰 리스트. 단가계산기 샘플/예측(_build_material_regression)과 동일 기준.
    박스/단상자: _box_tokens_from_raw (골·겹수·인쇄 등 추상 토큰). 그 외: / 와 + 분리 + 정규화."""
    import re as _re
    m_str = (m_str or '').strip()
    if not m_str:
        return []
    cat = _PCALC_CAT_MAP.get((raw_cat or '').strip(), (raw_cat or '').strip())
    _MANUAL_MAP = {'LLDP115': 'LLDPE115', 'LLDPE110(D': 'LLDPE110'}

    def _norm(tok):
        t = tok.replace('㎛', '').replace('μm', '').replace('μ', '')
        t = _re.sub(r'[,\s]+', '', t).strip()
        t = _re.sub(r'[a-zA-Z]+', lambda mm: mm.group().upper(), t)
        return _MANUAL_MAP.get(t, t)

    tokens = []
    if cat in ('단상자', '박스'):
        box_tok = _box_tokens_from_raw(m_str)
        if box_tok:
            tokens = list(box_tok)
    if not tokens:
        seen = set()
        for t in _re.split(r'\s*/\s*|\s*\+\s*', m_str):
            nn = _norm(t)
            if nn and nn not in seen:
                seen.add(nn)
                tokens.append(nn)
    return tokens


def _predict_box_price(items, sel_set, area, is_carton=False):
    """단상자/박스 전용 예측 (LOOCV 최적화).
    - RRP/물류박스: 단위면적 median kNN (aw=1.5, k=8, 인쇄/라미 클래스 페널티)
    - 단상자: log-log 회귀(고정비/규모경제 반영) + 토큰 잔차보정 (소형 단상자 정확도↑)"""
    import math as _m
    import statistics as _stat
    qa = max(area, 1)
    log_qa = _m.log(qa)

    # ── 단상자: log-log 회귀 + 토큰 잔차보정 ──────────────────────────
    if is_carton:
        train = [r for r in items
                 if r.get('category') == '단상자'
                 and r.get('area', 0) > 0 and r.get('price', 0) > 0]
        if len(train) >= 4:
            xs = [_m.log(r['area']) for r in train]
            ys = [_m.log(r['price']) for r in train]
            n = len(xs); mx = sum(xs)/n; my = sum(ys)/n
            num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
            den = sum((x-mx)**2 for x in xs)
            b = num/den if den else 0.5
            a = my - b*mx
            def _base(ar): return a + b*_m.log(max(ar, 1))
            # 토큰 유사 이웃의 잔차 가중평균 (0.6 보정)
            resid = [(_m.log(r['price']) - _base(r['area']), set(r['materials'])) for r in train]
            sc = []
            for rr, rs in resid:
                u = sel_set | rs
                j = len(sel_set & rs) / len(u) if u else 0
                if j >= 0.2:
                    sc.append((j**1.5, rr))
            adj = 0.0
            if sc:
                tw = sum(s for s, _ in sc)
                adj = sum(s*r for s, r in sc) / tw * 0.6
            return _m.exp(_base(qa) + adj), 'carton_loglinear'
        # 단상자 데이터 부족 → 박스 kNN으로 폴백

    # ── RRP/물류박스: 단위면적 median kNN ───────────────────────────
    # 주: '이중벽' 명시 페널티는 짝지은 부트스트랩에서 유의하지 않고(ΔMAPE -0.4%p, CI 0포함),
    #     '이중벽'이 이미 재질 토큰이라 Jaccard에 반영됨(중복) → 추가하지 않음 (rigor2_estimate.py)
    q_printed = ('인쇄됨' in sel_set) or ('인쇄지' in sel_set)
    q_lami    = ('무광라미' in sel_set) or ('유광라미' in sel_set)
    train = [r for r in items
             if r.get('category') in ('박스', '단상자')
             and r.get('area', 0) > 0 and r.get('price', 0) > 0]
    if not train:
        return None, 'no_match'

    scored = []
    for r in train:
        rs = set(r['materials'])
        union = sel_set | rs
        jacc = len(sel_set & rs) / len(union) if union else 0
        if jacc < 0.1:
            continue
        r_printed = ('인쇄됨' in rs) or ('인쇄지' in rs)
        r_lami    = ('무광라미' in rs) or ('유광라미' in rs)
        pen = 1.0
        if q_printed != r_printed: pen *= 0.4
        if q_lami != r_lami:       pen *= 0.7
        ld = abs(log_qa - _m.log(r['area']))
        score = jacc * _m.exp(-1.5 * ld) * pen
        if score > 0.003:
            scored.append((score, _m.log(r['price'] / r['area'])))

    if not scored:
        return None, 'no_match'
    scored.sort(reverse=True)
    top = scored[:8]
    log_unit = _stat.median([lu for _, lu in top])
    return _m.exp(log_unit) * qa, 'box_unitarea_knn'


# 단가 계산기에서 제외할 품번 (단종/미사용 부재료, 동일스펙 가격불일치 등 노이즈 유발 항목)
_PCALC_EXCLUDE_CODES = {'B0120', 'B0368'}

def _build_material_regression():
    """부자재 규격 보드를 파싱해 계층 kNN 예측에 필요한 형태로 정리.
    예측은 _predict_material_price()가 train 샘플에서 즉시 계산 (사전 모델 학습 없음).
    카테고리별 단순 단위면적 단가는 재질 패널 표시용으로 함께 계산."""
    from collections import Counter
    import re as _re
    if MONDAY_DF is None or MONDAY_DF.empty:
        return {'materials': [], 'total_samples': 0, 'items': []}
    spec = MONDAY_DF[MONDAY_DF['보드명'] == '부자재 규격']
    if spec.empty:
        return {'materials': [], 'total_samples': 0, 'items': []}

    # 데이터에 남은 비표준 토큰을 표준형으로 매핑 (Monday CSV에 살아있는 항목만 유지).
    _MANUAL_MAP = {
        'LLDP115': 'LLDPE115',
        'LLDPE110(D': 'LLDPE110',
    }
    def _norm(tok):
        t = tok.replace('㎛', '').replace('μm', '').replace('μ', '')
        t = _re.sub(r'[,\s]+', '', t).strip()
        t = _re.sub(r'[a-zA-Z]+', lambda mm: mm.group().upper(), t)
        return _MANUAL_MAP.get(t, t)

    _cat_map = {'파우치': '파우치', '단상자': '단상자', 'RRP 및 물류박스': '박스'}

    # 모듈 수준 함수 재사용
    _box_tokens = _box_tokens_from_raw

    rows = []
    for _, r in spec.iterrows():
        if str(r.get('품번', '')).strip() in _PCALC_EXCLUDE_CODES:
            continue  # 단종/미사용 부재료 제외
        m_str = str(r.get('재질', '')).strip()
        p = str(r.get('단가(원)', '')).strip()
        if not m_str or not p:
            continue
        try:
            price = float(p)
        except Exception:
            continue
        if price <= 0:
            continue
        raw_cat = str(r.get('그룹', '')).strip()
        cat = _cat_map.get(raw_cat, raw_cat)

        # 토큰화 — api_spec_list(체크박스 연동)와 완전히 동일한 기준 사용
        tokens = _material_tokens(m_str, raw_cat)
        if not tokens:
            continue
        try:
            moq = float(str(r.get('MOQ', '0') or '0').replace(',', '').strip() or 0)
        except Exception:
            moq = 0
        size_s = str(r.get('사이즈', '')).strip()
        w = h = d = 0
        # 롤파우치(m, 500m 등) 제외 — 면적 기반 단가 체계 부적합
        _is_roll = bool(_re.search(r'\d+\s*[mM]\b|자동롤|롤파우치|두께\d+', size_s))
        if _is_roll:
            continue
        # 사이즈 파싱: 숫자 전후 단위(mm 등) 허용
        _size_re = r'[A-Za-z]*\s*(\d+)\s*[A-Za-z]*\s*[*×xX]\s*[A-Za-z]*\s*(\d+)'
        if cat == '파우치':
            mwh = _re.search(_size_re, size_s)
            if mwh:
                w, h = int(mwh.group(1)), int(mwh.group(2))
            mg = _re.search(r'밑지\s*(\d+)', size_s)
            if mg:
                d = int(mg.group(1))
        else:
            mwhd = _re.search(r'(\d+)\s*[*×xX]\s*(\d+)\s*[*×xX]\s*(\d+)', size_s)
            if mwhd:
                w, h, d = int(mwhd.group(1)), int(mwhd.group(2)), int(mwhd.group(3))
            else:
                mwh = _re.search(_size_re, size_s)
                if mwh:
                    w, h = int(mwh.group(1)), int(mwh.group(2))
        if cat == '파우치':
            area = w * (h + d) if (w and h) else 0
        else:
            if w and h and d:
                area = 2 * (w*h + w*d + h*d)
            else:
                area = w * h
        # 사이즈 미기재(면적 계산 불가) 행 제외 — 비용처리 항목 등 비(非)스펙 데이터.
        # 어차피 예측에서 area>0 필터로 빠지지만, 샘플 수·재질 목록 오염 방지차 여기서 제거.
        if area <= 0:
            continue
        rows.append({
            'materials': tokens, 'price': price,
            'code': str(r.get('품번', '')).strip(),
            'name': str(r.get('품명', '')).strip(),
            'size': size_s, 'area': area,
            'w': w, 'h': h, 'd': d,
            'moq': moq,
            'vendor': str(r.get('아이템명', '')).strip(),
            'category': cat,
        })

    usage_all = Counter(t for r in rows for t in r['materials'])
    cat_usage = {}
    for r in rows:
        c = r['category']
        if not c:
            continue
        for t in r['materials']:
            cat_usage.setdefault(t, {}).setdefault(c, 0)
            cat_usage[t][c] += 1

    # 재질 패널 표시용 단가: 카테고리별 단위면적당 가격 × 카테고리 평균 area
    import statistics as _stat
    cat_unit_price = {}
    cat_avg_area = {}
    for cat in ('파우치', '단상자', '박스'):
        sub = [r for r in rows if r['category'] == cat and r['area'] > 0]
        if not sub:
            continue
        cat_unit_price[cat] = _stat.median([r['price']/r['area'] for r in sub])
        cat_avg_area[cat] = _stat.median([r['area'] for r in sub])

    # 재질별 근사 단가: 이 재질을 포함한 샘플들의 단위면적당 가격 median × 카테고리 평균 area
    materials = []
    for m in sorted(usage_all.keys()):
        cats_for_m = sorted(cat_usage.get(m, {}).keys())
        best_cat = max(cats_for_m, key=lambda c: cat_usage[m].get(c, 0)) if cats_for_m else None
        sub = [r for r in rows if m in r['materials'] and r['area'] > 0
               and (best_cat is None or r['category'] == best_cat)]
        if sub:
            unit = _stat.median([r['price']/r['area'] for r in sub])
            avg_area = cat_avg_area.get(best_cat, _stat.median([r['area'] for r in sub]))
            # 재질이 보통 1개 들어가는 경우의 기여 단가 ≈ 단위면적 × 평균 area / 평균 토큰수
            avg_tokens = _stat.median([len(r['materials']) for r in sub]) or 1
            price = max(unit * avg_area / avg_tokens, 0)
        else:
            price = 0.0
        materials.append({
            'name': m, 'price': round(price, 2),
            'usage': usage_all[m], 'categories': cats_for_m,
        })
    materials.sort(key=lambda x: (-x['usage'], x['name']))

    return {
        'materials': materials, 'total_samples': len(rows),
        'items': rows,
        'cat_unit_price': cat_unit_price,
        'cat_avg_area': cat_avg_area,
    }


_PCALC_AREA_BETA = 0.65  # T2/T3 면적 멱법칙 보정 지수 (규모의 경제). LOOCV 최적: 파우치 MAPE 18.0→14.5%, ±20% 73.8→76.2%

def _predict_material_price(items, sel_set, cat, area, vendor='', query_code=''):
    """로그 공간 kNN 예측 (개선판).
    T1: 완전 동일 재질 → log-log 선형 보간 (economies-of-scale 반영)
    T2: Jaccard 가중 kNN → 이웃 단가를 목표 면적으로 멱법칙(β) 스케일 후 log 공간 가중 평균
    특징:
    - 이웃 단가에 (목표면적/이웃면적)^β 보정 → 소형 포장재 과대추정 해소 (β=0.65, 규모의 경제)
    - log 공간 가중 평균으로 왜곡값 영향 최소화
    - + 구분 재질도 정상 파싱됨
    """
    import math as _m
    _beta = _PCALC_AREA_BETA
    qa = max(area, 1)
    log_qa = _m.log(qa)

    # 자기 자신 제외 + 카테고리 필터 + 유효 데이터
    train = [r for r in items
             if r.get('category') == cat
             and r.get('area', 0) > 0 and r.get('price', 0) > 0
             and (not query_code or r.get('code', '') != query_code)]

    if not train:
        return None, 'no_match'

    # ── T1: 완전 동일 재질 → log-log 보간 ──────────────────────────────
    exact = [r for r in train if set(r['materials']) == sel_set]
    if exact:
        log_areas  = [_m.log(r['area'])  for r in exact]
        log_prices = [_m.log(r['price']) for r in exact]
        if len(exact) == 1:
            # 단일 레퍼런스: 면적 비율 0.65승 보정 (economies of scale)
            ratio = qa / exact[0]['area']
            pred  = exact[0]['price'] * (ratio ** 0.65)
        else:
            # OLS: log(price) = a + b·log(area)
            n      = len(exact)
            mla    = sum(log_areas) / n
            mlp    = sum(log_prices) / n
            num    = sum((la - mla) * (lp - mlp) for la, lp in zip(log_areas, log_prices))
            den    = sum((la - mla) ** 2 for la in log_areas)
            b      = max(0.4, min(1.0, num / den)) if den > 0 else 0.65
            log_pred = mlp + b * (log_qa - mla)
            pred  = _m.exp(log_pred)
        return pred, 'tier1_loglinear'

    # ── T2: Jaccard × 로그면적 유사도 → log 공간 가중 평균 ───────────────
    scored = []
    for r in train:
        rs    = set(r['materials'])
        union = sel_set | rs
        jacc  = len(sel_set & rs) / len(union) if union else 0
        if jacc < 0.15:
            continue
        log_dist = abs(log_qa - _m.log(r['area']))
        area_sim = _m.exp(-1.8 * log_dist)          # 면적 유사도 (로그 거리 기반)
        mat_w    = jacc ** 1.2                        # 재질 유사도 가중
        vb       = 1.2 if (r.get('vendor', '') == vendor and vendor) else 1.0
        score    = mat_w * area_sim * vb
        if score > 0.005:
            adj_price = r['price'] * ((qa / r['area']) ** _beta)  # 면적 멱법칙 보정
            scored.append((score, _m.log(adj_price)))            # log 공간 단가 저장

    if scored:
        scored.sort(reverse=True)
        top     = scored[:12]
        total_w = sum(s for s, _ in top)
        log_est = sum(s * lp for s, lp in top) / total_w
        return _m.exp(log_est), 'tier2_knn_log'

    # ── T3: 재질 무관, 면적만으로 kNN ─────────────────────────────────
    # 재질이 전혀 매칭 안 될 때 면적만으로 nearest-neighbor 예측
    area_sims = []
    for r in train:
        log_dist = abs(log_qa - _m.log(r['area']))
        score    = _m.exp(-2.0 * log_dist)
        if score > 0.005:
            adj_price = r['price'] * ((qa / r['area']) ** _beta)  # 면적 멱법칙 보정
            area_sims.append((score, _m.log(adj_price)))
    if area_sims:
        area_sims.sort(reverse=True)
        top     = area_sims[:8]
        total_w = sum(s for s, _ in top)
        log_est = sum(s * lp for s, lp in top) / total_w
        return _m.exp(log_est), 'tier3_area_only'

    return None, 'no_match'


_MATERIAL_CACHE = None
def _get_material_data():
    global _MATERIAL_CACHE
    if _MATERIAL_CACHE is None:
        _MATERIAL_CACHE = _build_material_regression()
    return _MATERIAL_CACHE


def _mat_sort_key(m):
    name = m.get('name', '') or ''
    first = name[0] if name else ''
    if first.isdigit():
        cat = 0
    elif first.isascii() and first.isalpha():
        cat = 1
    elif '가' <= first <= '힣':
        cat = 2
    else:
        cat = 3
    return (cat, name.lower())


@app.route('/api/materials', methods=['GET'])
@cached_api()
def api_materials():
    """부자재 규격 재질별 역산 단가 목록 (단가 0원 제외, 숫자→영어→한글 순)"""
    d = _get_material_data()
    mats = [m for m in d['materials'] if m['price'] > 0]
    mats.sort(key=_mat_sort_key)
    return jsonify({
        'materials': mats,
        'total_samples': d['total_samples'],
    })


_ROLL_RE = re.compile(r'\d+\s*[mM]\b|자동롤|롤파우치|두께\s*\d+|이지컷LLD|500M|1000M', re.IGNORECASE)

@app.route('/api/material_estimate', methods=['POST'])
def api_material_estimate():
    """선택한 재질 + 사이즈 → 예상 단가 + 유사 샘플 (계층 kNN)."""
    payload = request.get_json(silent=True) or {}
    selected = [s.strip() for s in (payload.get('materials') or []) if s and s.strip()]
    req_cat = (payload.get('category') or '').strip()
    if not selected:
        return jsonify({'estimate': 0, 'breakdown': [], 'similar': [], 'method': 'none'})

    # 롤파우치 감지 — 면적 기반 모델 부적용, 예측 불가 반환
    size_raw = (payload.get('size_raw') or '').strip()
    if size_raw and _ROLL_RE.search(size_raw):
        return jsonify({'estimate': 0, 'breakdown': [], 'similar': [], 'method': 'roll_film',
                        'message': '롤파우치는 단위면적 단가 모델 적용 불가'})
    d = _get_material_data()

    # 1) 카테고리 결정 (요청 > 재질 빈도 투표)
    valid_cats = {'파우치', '단상자', '박스'}
    cat = req_cat if req_cat in valid_cats else None
    if not cat:
        from collections import Counter as _C
        votes = _C()
        for m in selected:
            for mm in d['materials']:
                if mm['name'] == m:
                    for c in mm.get('categories', []):
                        votes[c] += 1
                    break
        cat = votes.most_common(1)[0][0] if votes else '파우치'
    cat = cat if cat in valid_cats else '파우치'

    # 박스/단상자 + material_raw 있으면 box_tokens 서버사이드 적용
    material_raw = (payload.get('material_raw') or '').strip()
    if cat in ('단상자', '박스') and material_raw:
        box_toks = _box_tokens_from_raw(material_raw)
        if box_toks:
            selected = box_toks
    sel_set = set(selected)

    # 2) 사이즈 → effective area
    try:
        w_in = float(payload.get('width') or 0)
        h_in = float(payload.get('height') or 0)
        d_in = float(payload.get('depth') or 0)
    except Exception:
        w_in = h_in = d_in = 0
    if w_in > 0 and h_in > 0:
        if cat == '파우치':
            eff_area = w_in * (h_in + d_in) if d_in > 0 else w_in * h_in
        else:
            eff_area = 2 * (w_in*h_in + w_in*d_in + h_in*d_in) if d_in > 0 else w_in * h_in
    else:
        eff_area = d.get('cat_avg_area', {}).get(cat, 0) or 1

    vendor = (payload.get('vendor') or '').strip()
    # 박스/단상자는 전용 단위면적 예측기 사용 (파우치는 기존 계층 kNN)
    if cat in ('단상자', '박스'):
        estimate, method = _predict_box_price(d['items'], sel_set, eff_area, is_carton=(cat == '단상자'))
        if estimate is None or estimate <= 0:
            estimate, method = _predict_material_price(d['items'], sel_set, '박스', eff_area, vendor)
    else:
        estimate, method = _predict_material_price(d['items'], sel_set, cat, eff_area, vendor)
    if estimate is None or estimate <= 0:
        # 최후 fallback: 카테고리 평균 단위면적 단가 × area
        unit = d.get('cat_unit_price', {}).get(cat, 0) or 0
        if unit > 0:
            estimate = unit * eff_area
            method = 'fallback_cat_avg'
        else:
            return jsonify({'estimate': 0, 'breakdown': [], 'similar': [], 'method': 'no_match', 'category': cat})

    # 재질별 기여 근사 — 토큰 수로 균등 분배 (계층 kNN은 재질별 분리가 불가)
    n_tokens = max(len(selected), 1)
    per = round(estimate / n_tokens, 2)
    breakdown = [{'name': s, 'price': per} for s in selected]

    return jsonify({
        'estimate': round(estimate, 1),
        'breakdown': breakdown,
        'similar': _pcalc_similar(d['items'], sel_set, selected),
        'method': method,
        'category': cat,
    })


def _pcalc_similar(items, sel_set, selected):
    out = []
    for r in items:
        inter = sel_set & set(r['materials'])
        if not inter:
            continue
        out.append({
            'code': r['code'], 'name': r['name'], 'vendor': r['vendor'],
            'size': r['size'], 'price': r['price'],
            'overlap': len(inter), 'total': len(r['materials']),
            'materials': r['materials'],
        })
    out.sort(key=lambda x: (-x['overlap'], abs(x['total'] - len(selected))))
    return out[:10]


# 입고 일정 말풍선 작성자 — 남소민(주담당) + 김장군(부재 시 대리 업로드, 같은 팀)
PO_SCHEDULE_AUTHORS = {'남소민', '김장군'}
_po_header_re = re.compile(r'^\s*(?:└\s*)?\[(\d{4}-\d{2}-\d{2})\s+([^\]]+)\]\s*(.*)$', re.DOTALL)
_po_date_re = re.compile(
    r'\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}(?:\([월화수목금토일]\))?'   # YYYY.MM.DD (요일?)
    r'|(?<![\d/])\d{1,2}\/\d{1,2}(?![\d])'                      # M/D
)

# 입고일이 아닌 날짜 — "9/2 기준 발주 진행 X" 처럼 '기준' 앞의 날짜는 참조 시점일 뿐
_po_ref_date_re = re.compile(
    r'(?:\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}(?:\([월화수목금토일]\))?|(?<![\d/])\d{1,2}\/\d{1,2}(?![\d]))'
    r'\s*(?:기준|현재|자로)'
)
# 발주 미진행 말풍선 — 날짜가 있어도 입고 일정으로 보지 않음
_po_noorder_re = re.compile(
    r'미\s*발주'
    r'|발주\s*(?:진행\s*)?(?:[X×x✕]\b|안\s*(?:함|됨|됩|들어|해)|불가|보류|취소|중단|미진행|미정)'
)

def _extract_po_dates(upd_text):
    """업데이트 전체에서 '남소민' 저자 글만 추출, 각 글에서 날짜 토큰 추출.
    - 'M/D 기준' 형태의 참조 날짜는 제외
    - '발주 진행 X' / '미발주' / '발주 보류·취소' 말풍선은 일정 아님 → 제외
    반환: [{'posted': 'YYYY-MM-DD', 'dates': [...]}] — 최신순"""
    if not upd_text:
        return []
    entries = []
    for chunk in upd_text.split('\n---\n'):
        m = _po_header_re.match(chunk)
        if not m:
            continue
        posted, author, body = m.group(1), m.group(2).strip(), m.group(3)
        if author not in PO_SCHEDULE_AUTHORS:
            continue
        if _po_noorder_re.search(body):
            continue
        body = _po_ref_date_re.sub(' ', body)
        dates = list(dict.fromkeys(_po_date_re.findall(body)))
        if dates:
            entries.append({'posted': posted, 'author': author, 'dates': dates})
    entries.sort(key=lambda e: e['posted'], reverse=True)
    return entries


def _extract_po_updates(upd_text):
    """업데이트 전체를 말풍선 단위로 파싱 (작성자 무관).
    반환: [{'posted': 'YYYY-MM-DD', 'author': str, 'body': str}] — 최신순"""
    if not upd_text:
        return []
    bubbles = []
    for chunk in upd_text.split('\n---\n'):
        m = _po_header_re.match(chunk)
        if not m:
            continue
        posted, author, body = m.group(1), m.group(2).strip(), m.group(3).strip()
        if not body:
            continue
        bubbles.append({'posted': posted, 'author': author, 'body': body})
    bubbles.sort(key=lambda e: e['posted'], reverse=True)
    return bubbles


# 잔량 패턴: "잔량 1,860통" / "잔여 안전재고 1파트" / "잔량 2,280통" 등
_po_remain_re = re.compile(r'(?:잔량|잔여(?:\s*[가-힣]+)*)\s*([\d,]+)\s*([가-힣A-Za-z]*)')
# 한국식 수량 패턴: "1만매 선 입고" / "2만5천통" / "1만 매" 등
_ko_num_re = re.compile(
    r'([\d,]+(?:\.\d+)?)\s*만\s*(?:([\d,]+(?:\.\d+)?)\s*천)?\s*([가-힣A-Za-z]*)|'  # Xman [Ycheon] unit
    r'([\d,]+(?:\.\d+)?)\s*천\s*([가-힣A-Za-z]*)'                                   # Xcheon unit
)


# 한국식 수량을 '잔량'으로 볼 수 있는 맥락 / 볼 수 없는 맥락
# 잔량 = 발주 후 아직 입고되지 않은 물량. 재고(보유 수량)·입고 완료/예정 수량과는 다른 개념 (2026-09-14 사용자 정의)
_po_ko_pos_re = re.compile(r'잔량|잔여|미입고|남은\s*(?:수량|물량|발주)|남아\s*있|남음')
_po_ko_neg_re = re.compile(r'대체|발주\s*진행|주문|생산|출고|소진|폐기|사용\s*예정|재고|보유|입고\s*(?:예정|되|됨|완료)')
# 재고(보유) 언급: "재고 4천매 이상 보유중", "재고가 2,556매" → 별도 '재고' 배지
_po_stock_ctx_re = re.compile(r'재고|보유')
_PO_UNIT = r'(매|장|통|개|봉|롤|팩|박스|병|캔|파트|ea|EA|kg|KG|g)?'
# 숫자 구절: "2만5천", "4천", "1만", "2,556" (만·천 조합 통째로)
_PO_NUM = r'(\d[\d,]*(?:\.\d+)?\s*만(?:\s*\d[\d,]*\s*천)?|\d[\d,]*(?:\.\d+)?\s*천|\d[\d,]*(?:\.\d+)?)'
_po_stock_after_re = re.compile(r'재고(?:량|가|는|은|:)?\s*(?:약|현재|총)?\s*' + _PO_NUM + r'\s*' + _PO_UNIT)
_po_stock_before_re = re.compile(_PO_NUM + r'\s*' + _PO_UNIT + r'\s*(?:이상|정도|가량|만)?\s*보유')


def _ko_amount(s):
    """'2만5천'→25000, '4천'→4000, '1만'→10000, '2,556'→2556."""
    s = s.replace(',', '').replace(' ', '')
    m = re.fullmatch(r'(\d+(?:\.\d+)?)만(?:(\d+)천)?', s)
    if m:
        return float(m.group(1)) * 10000 + (float(m.group(2)) * 1000 if m.group(2) else 0)
    m = re.fullmatch(r'(\d+(?:\.\d+)?)천', s)
    if m:
        return float(m.group(1)) * 1000
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_ko_qty(text):
    """'1만매', '2만5천통', '1만 5천매', '3천매' → (qty:int, unit:str) or None."""
    m = _ko_num_re.search(text)
    if not m:
        return None
    if m.group(1) is not None:  # X만 [Y천] unit
        man = float(m.group(1).replace(',', ''))
        cheon = float(m.group(2).replace(',', '')) if m.group(2) else 0
        qty = int(man * 10000 + cheon * 1000)
        unit = (m.group(3) or '').strip()
    else:  # X천 unit
        cheon = float(m.group(4).replace(',', ''))
        qty = int(cheon * 1000)
        unit = (m.group(5) or '').strip()
    return (qty, unit) if qty > 0 else None


def _extract_po_remain(upd_text):
    """남소민 메시지 중 가장 최신에서 '잔량 X단위' 또는 'X만매 선입고' 등 수량 추출.
    우선순위: 잔량/잔여 키워드 → 한국식 수(만/천) + 단위."""
    if not upd_text:
        return None
    posts = []      # 남소민 메시지 (posted, body)
    all_posts = []  # 전체 작성자 메시지 (posted, body) — 잔량 키워드 fallback용
    for chunk in upd_text.split('\n---\n'):
        m = _po_header_re.match(chunk)
        if not m:
            continue
        posted, author, body = m.group(1), m.group(2).strip(), m.group(3)
        all_posts.append((posted, body))
        if author in PO_SCHEDULE_AUTHORS:
            posts.append((posted, body))
    posts.sort(key=lambda p: p[0], reverse=True)
    for _, body in posts:
        # 1) 잔량/잔여 키워드 + 아라비아숫자
        m = _po_remain_re.search(body)
        if m:
            try:
                qty = int(m.group(1).replace(',', ''))
            except Exception:
                qty = 0
            unit = (m.group(2) or '').strip()
            if qty > 0:
                return {'qty': qty, 'unit': unit}
        # 2) 한국식 수량 (1만매, 2만5천통 등) — 잔량 키워드 없어도 인식하되 **절 단위로 맥락 확인**
        #    (2026-09-14) "단종으로 인한 RRP 1만장 대체사용"의 1만장(다른 품번 대체분)을 잔량으로 오인 →
        #    화살표/줄바꿈/쉼표로 나눈 절에 잔량·재고·선입고 맥락이 있고, 대체·발주·소진 맥락이 없을 때만 채택
        for clause in re.split(r'[→\n,.;/]|\s{2,}', body):
            if not _ko_num_re.search(clause):
                continue
            if _po_ko_neg_re.search(clause) or not _po_ko_pos_re.search(clause):
                continue
            parsed = _parse_ko_qty(clause)
            if parsed:
                return {'qty': parsed[0], 'unit': parsed[1]}
    # 3) 남소민 메시지에 잔량 없으면 — 전체 작성자 중 '잔량/잔여 N단위' 명시만 인식 (최신 우선)
    all_posts.sort(key=lambda p: p[0], reverse=True)
    for _, body in all_posts:
        m = _po_remain_re.search(body)
        if m:
            try:
                qty = int(m.group(1).replace(',', ''))
            except Exception:
                qty = 0
            unit = (m.group(2) or '').strip()
            if qty > 0:
                return {'qty': qty, 'unit': unit}
    return None


def _extract_po_stock(upd_text):
    """말풍선의 '재고/보유' 수량 (잔량과 구분해 별도 배지로 표시). 담당자 글 우선, 최신순."""
    if not upd_text:
        return None
    posts, others = [], []
    for chunk in upd_text.split('\n---\n'):
        m = _po_header_re.match(chunk)
        if not m:
            continue
        posted, author, body = m.group(1), m.group(2).strip(), m.group(3)
        (posts if author in PO_SCHEDULE_AUTHORS else others).append((posted, body))
    for group in (posts, others):
        group.sort(key=lambda p: p[0], reverse=True)
        for _, body in group:
            for clause in re.split(r'[→\n;/]|\s{2,}', body):
                if not _po_stock_ctx_re.search(clause) or '소진' in clause or '없' in clause:
                    continue
                if re.search(r'보유\s*예정|재고\s*예정', clause):   # "4000개 가량 보유 예정" = 미래 계획, 현재 재고 아님
                    continue
                for rx in (_po_stock_after_re, _po_stock_before_re):
                    mm = rx.search(clause)
                    if not mm:
                        continue
                    q = _ko_amount(mm.group(1))
                    if q > 0:
                        return {'qty': int(q), 'unit': mm.group(2) or ''}
    return None


@app.route('/api/po_pending', methods=['GET'])
@cached_api()
def api_po_pending():
    """대시보드 '예상입고일' 통합 데이터:
    - 부자재: Monday '원/부자재 발주 요청' 보드 '발주 완료' 그룹 (남소민 말풍선 최신 일자)
    - 원료: Monday '원료 입고 일정' 보드 입고완료 제외 (입항 일정)
    정렬: 입고예정일/입항일 빠른순, 미정은 맨 뒤."""
    if MONDAY_DF is None or MONDAY_DF.empty:
        return jsonify({'items': [], 'total': 0})

    items = []

    # 부자재 발주완료
    m_po = (MONDAY_DF['보드명'] == '원/부자재 발주 요청') & (MONDAY_DF['그룹'] == '발주 완료')
    for _, r in MONDAY_DF.loc[m_po].iterrows():
        upd = str(r.get('업데이트', '') or '').strip()
        entries = _extract_po_dates(upd)
        if entries and entries[0].get('dates'):
            eta_display = entries[0]['dates'][0]
            eta_sort = _normalize_eta(eta_display, entries[0]['posted'])
            has_eta = True
        else:
            eta_display = ''; eta_sort = '9999-99-99'; has_eta = False
        remain = _extract_po_remain(upd)  # 남소민 최신 메시지의 잔량
        # 수량: 실발주 수량 우선 (실제 발주량), 없으면 요청수량
        _qty = str(r.get('실발주 수량', '') or '').strip() or str(r.get('요청수량', '')).strip()
        items.append({
            'type': 'po',
            'code': str(r.get('아이템명', '')).strip() or str(r.get('품번', '')).strip(),
            'name': str(r.get('품명', '')).strip(),
            'qty': _qty,
            'remain': remain,  # {'qty': int, 'unit': str} 또는 None — 발주 후 미입고 물량
            'stock': _extract_po_stock(upd),  # 말풍선의 재고(보유) 언급 — 잔량과 별개
            'request_date': str(r.get('발주 요청일', '')).strip(),
            'vendor': str(r.get('발주업체', '')).strip(),
            'destination': str(r.get('입고처', '')).strip(),
            'schedule_entries': entries,
            'updates': _extract_po_updates(upd),  # 전체 말풍선 (작성자 무관)
            'has_schedule': has_eta,
            'eta_display': eta_display,
            'eta_sort': eta_sort,
        })

    # 원료 입고 일정 (입고완료 제외)
    m_raw = (MONDAY_DF['보드명'] == '원료 입고 일정') & (MONDAY_DF['그룹'] != '입고완료')
    for _, r in MONDAY_DF.loc[m_raw].iterrows():
        eta = str(r.get('입항 일정', '')).strip()
        has_eta = bool(eta)
        eta_sort = eta if re.match(r'^\d{4}-\d{2}-\d{2}', eta or '') else '9999-99-99'
        # 짧은 표시용: YYYY-MM-DD → M/D
        if has_eta and len(eta) >= 10:
            try:
                eta_display = f"{int(eta[5:7])}/{int(eta[8:10])}"
            except Exception:
                eta_display = eta
        else:
            eta_display = ''
        items.append({
            'type': 'raw',
            'code': str(r.get('그룹', '')).strip(),    # 오트밀/고구마류/유탕류/칩류
            'name': str(r.get('아이템명', '')).strip(),
            'qty': str(r.get('입항 물량', '')).strip(),
            'request_date': str(r.get('선적 일정', '')).strip(),
            'vendor': str(r.get('수입원', '')).strip(),
            'destination': str(r.get('상태', '')).strip(),  # 선적 예정/입항 중
            'schedule_entries': [],
            'has_schedule': has_eta,
            'eta_display': eta_display,
            'eta_sort': eta_sort,
        })

    # 원재료 수입 현황 (하위아이템 우선, 없으면 메인아이템)
    IMPORT_BOARD = '원재료 수입 현황'
    IMPORT_SUB   = '원재료 수입 현황_하위'
    board_names  = MONDAY_DF['보드명'].values
    imp_src = IMPORT_SUB if IMPORT_SUB in board_names else (IMPORT_BOARD if IMPORT_BOARD in board_names else None)
    if imp_src:
        m_imp = (MONDAY_DF['보드명'] == imp_src) & (MONDAY_DF['그룹'] != '입고 완료')
        for _, r in MONDAY_DF.loc[m_imp].iterrows():
            if imp_src == IMPORT_SUB:
                contract = str(r.get('계약번호', '')).strip()
                sub_name = str(r.get('아이템명', '')).strip()
                qty      = str(r.get('입항 물량', '')).strip()
                vendor   = str(r.get('수입원', '')).strip()
            else:
                contract = str(r.get('아이템명', '')).strip()
                sub_name = ''
                qty      = ''
                vendor   = str(r.get('수입원', '')).strip()
            eta = str(r.get('입항일', '')).strip()
            has_eta  = bool(eta)
            eta_sort = eta if re.match(r'^\d{4}-\d{2}-\d{2}', eta or '') else '9999-99-99'
            if has_eta and len(eta) >= 10:
                try:    eta_display = f"{int(eta[5:7])}/{int(eta[8:10])}"
                except: eta_display = eta
            else:
                eta_display = ''
            # Monday 구조화 내역 (컬럼값 상세)
            _det_fields = [
                ('계약번호', contract),
                ('품목', sub_name),
                ('수입원', vendor),
                ('입항 물량', qty),
                ('선적 일정', str(r.get('선적 일정', '')).strip()),
                ('입항일', eta),
                ('상태', str(r.get('상태', '')).strip()),
                ('진행 그룹', str(r.get('그룹', '')).strip()),
                ('실제 입고일', str(r.get('실제 입고일', '')).strip()),
            ]
            mon_detail = [{'label': k, 'value': v} for k, v in _det_fields if v]
            # 말풍선(부모 메인아이템에 달림 → 하위행에 물려받아 저장됨)
            imp_updates = _extract_po_updates(str(r.get('업데이트', '') or '').strip())
            items.append({
                'type': 'import_raw',
                'code': vendor,
                'name': sub_name,
                'contract': contract,
                'qty': qty,
                'request_date': '',
                'vendor': vendor,
                'destination': str(r.get('그룹', '')).strip(),
                'schedule_entries': [],
                'updates': imp_updates,
                'mon_detail': mon_detail,
                'has_schedule': has_eta,
                'eta_display': eta_display,
                'eta_sort': eta_sort,
            })

    items.sort(key=lambda x: (not x['has_schedule'], x['eta_sort']))
    return jsonify({'items': items, 'total': len(items)})


def _ext_qty_to_int(s):
    """'5,000' / '5000통' → 5000(int), 파싱 불가 시 None."""
    if not s:
        return None
    m = re.search(r'[\d,]+', str(s))
    if not m:
        return None
    try:
        v = int(m.group(0).replace(',', ''))
        return v if v > 0 else None
    except Exception:
        return None


def _ext_cors(resp):
    """외부 read-only API 응답에 CORS 헤더 부착."""
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'X-API-Key, Content-Type'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    return resp


@app.route('/api/external/incoming', methods=['GET', 'OPTIONS'])
def api_external_incoming():
    """외부 시스템(매홍 생산관리)용 입고예정일 read-only API.
    인증: X-API-Key 헤더(= MAEHONG_API_KEY env). 없거나 틀리면 401.
    데이터: Monday '원/부자재 발주 요청' 보드 '발주 완료' 그룹(=미입고분).
    남소민 최신 일정 메시지 입고예정일(확정) + 일정없는 건도 eta_date=null/status='미정'으로 포함.
    eta_date 오름차순, 미정은 맨 뒤."""
    # CORS preflight — 인증 불요
    if request.method == 'OPTIONS':
        return _ext_cors(app.make_response(('', 204)))

    # API 키 인증
    expected = os.environ.get('MAEHONG_API_KEY', '')
    if not expected or request.headers.get('X-API-Key', '') != expected:
        return _ext_cors(jsonify({'error': 'Invalid API key'})), 401

    items = []
    if MONDAY_DF is not None and not MONDAY_DF.empty:
        m_po = (MONDAY_DF['보드명'] == '원/부자재 발주 요청') & (MONDAY_DF['그룹'] == '발주 완료')
        for _, r in MONDAY_DF.loc[m_po].iterrows():
            code = str(r.get('아이템명', '')).strip() or str(r.get('품번', '')).strip()
            if not code:
                continue
            # 입고예정일 파싱 — 없거나 정규화 실패 시 '미정'(eta_date=null)으로 포함
            upd = str(r.get('업데이트', '') or '').strip()
            entries = _extract_po_dates(upd)
            eta = None
            if entries and entries[0].get('dates'):
                _e = _normalize_eta(entries[0]['dates'][0], entries[0]['posted'])
                if re.match(r'^\d{4}-\d{2}-\d{2}$', _e) and not _e.startswith('999'):
                    eta = _e
            qty = _ext_qty_to_int(str(r.get('실발주 수량', '') or '').strip()
                                  or str(r.get('요청수량', '')).strip())
            # supplier = 입고처(매홍/더고은/데이웰즈 등). 발주업체 컬럼은 미입력이라 입고처로 식별.
            supplier = str(r.get('입고처', '')).strip()
            po_no = str(r.get('발주번호', '') or r.get('발주 번호', '') or '').strip()
            name = str(r.get('품명', '')).strip()
            items.append({
                'code': code,
                'name': name or None,
                'eta_date': eta,
                'qty': qty,
                'supplier': supplier or None,
                'po_no': po_no or None,
                'status': '확정' if eta else '미정',
            })

    # eta 확정 → 날짜 오름차순, 미정(null)은 맨 뒤
    items.sort(key=lambda x: (x['eta_date'] is None, x['eta_date'] or ''))
    try:
        synced = datetime.fromtimestamp(_DATA_SYNCED_AT).strftime('%Y-%m-%dT%H:%M:%S')
    except Exception:
        synced = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    return _ext_cors(jsonify({'synced_at': synced, 'items': items, 'total': len(items)}))


@app.route('/api/outsource_pending', methods=['GET'])
@cached_api()
def api_outsource_pending():
    """Monday '외주 생산 요청' 보드 — 외주 입고 물량.
    대상 그룹: 발주 완료, 생산 요청, 확인 필요
    말풍선에서 남소민 담당자 최신 입고예정일 파싱."""
    if MONDAY_DF is None or MONDAY_DF.empty:
        return jsonify({'items': [], 'total': 0})
    m = (MONDAY_DF['보드명'] == '외주 생산 요청') & (MONDAY_DF['그룹'].isin(['발주 완료', '생산 요청', '확인 필요']))
    sub = MONDAY_DF.loc[m]
    if sub.empty:
        return jsonify({'items': [], 'total': 0})

    items = []
    for _, r in sub.iterrows():
        group = str(r.get('그룹', '')).strip()
        is_hold = (group == '확인 필요')
        is_requesting = (group == '생산 요청')
        upd = str(r.get('업데이트', '') or '').strip()
        entries = _extract_po_dates(upd)
        if entries and entries[0].get('dates'):
            eta_display = entries[0]['dates'][0]
            eta_sort = _normalize_eta(eta_display, entries[0]['posted'])
            has_eta = True
        else:
            eta_display = ''; eta_sort = '9999-99-99'; has_eta = False
        # 발주상태: 신양식 '발주 상태'(공백) 우선, 구양식 '발주상태' fallback
        order_status = str(r.get('발주 상태', '') or r.get('발주상태', '') or '').strip()
        # 시방서 첨부 (신양식) — Monday protected_static URL
        spec_url = str(r.get('시방서 첨부', '') or '').strip()
        # 고객사 (신양식) — 없으면 업체명 fallback
        customer = str(r.get('고객사', '') or '').strip()
        vendor = str(r.get('업체명', '') or '').strip()
        items.append({
            'code': str(r.get('아이템명', '')).strip(),
            'item_id': str(r.get('아이템ID', '')).strip(),
            'name': str(r.get('품명', '')).strip(),
            'qty': str(r.get('요청수량', '')).strip(),
            'vendor': vendor or customer,
            'customer': customer,
            'spec_url': spec_url,
            'has_spec': bool(spec_url),
            'destination': str(r.get('요청 입고지', '')).strip(),
            'request_date': str(r.get('외주 요청일', '')).strip(),
            'requested_eta': str(r.get('요청 입고일', '')).strip(),
            'schedule_entries': entries,
            'updates': _extract_po_updates(upd),  # 전체 말풍선 (작성자 무관)
            'has_schedule': has_eta and not is_hold,
            'is_hold': is_hold,
            'is_requesting': is_requesting,
            'order_status': order_status,
            'group': group,
            'eta_display': eta_display,
            # 정렬: 발주완료(0) → 생산요청(1) → 확인필요(2)
            'eta_sort': eta_sort if not is_hold else '9999-99-98',
            '_sort_group': 0 if group == '발주 완료' else (1 if group == '생산 요청' else 2),
        })
    # 정렬: 그룹(발주완료→생산요청→확인필요) → 일정확정 → 날짜 오름차순
    items.sort(key=lambda x: (x['_sort_group'], not x['has_schedule'], x['eta_sort']))
    return jsonify({'items': items, 'total': len(items)})


@app.route('/api/spec_list', methods=['GET'])
@cached_api()
def api_spec_list():
    """부자재 규격 보드 전체 항목 — 대시보드 하단 패널용."""
    if MONDAY_DF is None or MONDAY_DF.empty:
        return jsonify({'items': [], 'total': 0})
    spec = MONDAY_DF[MONDAY_DF['보드명'] == '부자재 규격']
    if spec.empty:
        return jsonify({'items': [], 'total': 0})
    items = []
    _code_re = re.compile(r'^[A-Za-z0-9]+$')
    for _, r in spec.iterrows():
        code = str(r.get('품번', '')).strip()
        if not code or not _code_re.match(code):
            continue
        name = str(r.get('품명', '')).strip()
        size = str(r.get('사이즈', '')).strip()
        material = str(r.get('재질', '')).strip()
        moq_raw = str(r.get('MOQ', '')).strip().replace(',', '')
        try:
            moq = int(float(moq_raw)) if moq_raw else 0
        except Exception:
            moq = 0
        price_raw = str(r.get('단가(원)', '')).strip().replace(',', '')
        try:
            price = int(float(price_raw)) if price_raw else 0
        except Exception:
            price = 0
        vendor = str(r.get('아이템명', '')).strip()
        cat = str(r.get('그룹', '')).strip()
        div = str(r.get('구분', '')).strip()
        items.append({
            'code': code, 'name': name, 'size': size,
            'material': material, 'moq': moq, 'price': price,
            'vendor': vendor, 'category': cat, 'div': div,
            'tokens': _material_tokens(material, cat),  # 단가계산기 연동용 (서버 동일 토큰)
        })
    items.sort(key=lambda x: x['code'])  # 품번 오름차순
    return jsonify({'items': items, 'total': len(items)})


# ────────────────────────────────────────────
# 입수 테스트 (3D) — 부자재 규격 사이즈 파싱 + 박스입수
# ────────────────────────────────────────────
# 사이즈 표기 예: "130*155+밑지40", "220*290", "W70*H175", "200mm*275mm=삼방"
_IPSU_SIZE_RE = re.compile(
    r'(?:W)?\s*(\d+(?:\.\d+)?)\s*(?:mm)?\s*[\*xX×]\s*(?:H)?\s*(\d+(?:\.\d+)?)\s*(?:mm)?'
    r'(?:\s*[\+/]\s*(?:밑지|거싯|가젯)?\s*(\d+(?:\.\d+)?))?', re.IGNORECASE)
_IPSU_N_RE = re.compile(r'(\d+)\s*(?:개입|입|번들)')
# 롤/원단류는 파우치 치수가 아님 → 제외
_IPSU_ROLL_RE = re.compile(r'\d+\s*[mM]\b|자동롤|롤파우치|폭\s*\d+|두께\s*\d+', re.IGNORECASE)
# 단상자/외박스(RRP·전용박스)는 'W*D*H' 3차원 표기 — 예 "530*360*255", "140*85*190"
# ※ '150*200+35'(밑지)는 구분자가 '+'라 여기 걸리지 않는다
_IPSU_BOX_RE = re.compile(
    r'^\s*(?:W)?\s*(\d+(?:\.\d+)?)\s*(?:mm)?\s*[\*xX×]\s*(?:D|L)?\s*(\d+(?:\.\d+)?)\s*(?:mm)?'
    r'\s*[\*xX×]\s*(?:H)?\s*(\d+(?:\.\d+)?)', re.IGNORECASE)


def _parse_ipsu_size(size_s: str):
    """'130*155+밑지40' → {'W':130,'H':155,'base':40,'form':'스탠드'}
    '530*360*255'      → {'W':530,'D':360,'H':255,'form':'상자'}. 실패 시 None."""
    s = (size_s or '').strip()
    if not s or _IPSU_ROLL_RE.search(s):
        return None
    # 3차원 표기 우선 — 상자류는 강체라 실치수 그대로 사용
    mb = _IPSU_BOX_RE.match(s.replace(' ', ''))
    if mb:
        W, D, H = (float(mb.group(i)) for i in (1, 2, 3))
        if all(0 < v <= 2000 for v in (W, D, H)):
            return {'W': W, 'D': D, 'H': H, 'base': 0.0, 'form': '상자'}
    m = _IPSU_SIZE_RE.search(s.replace(' ', ''))
    if not m:
        return None
    W = float(m.group(1))
    H = float(m.group(2))
    base = float(m.group(3)) if m.group(3) else 0.0
    if W <= 0 or H <= 0 or W > 2000 or H > 2000:
        return None
    return {'W': W, 'H': H, 'base': base,
            'form': '스탠드' if base > 0 else '3면실링'}


def _ipsu_tier(name: str) -> str:
    """품명에서 1차/2차 파우치 구분."""
    if '2차' in name:
        return '2차'
    if '1차' in name:
        return '1차'
    return ''


def _ipsu_specs_list():
    """부자재 규격 보드 → 3D 파라미터가 붙은 spec 목록."""
    if MONDAY_DF is None or MONDAY_DF.empty:
        return []
    specs = []
    spec_df = MONDAY_DF[MONDAY_DF['보드명'] == '부자재 규격']
    _code_re = re.compile(r'^[A-Za-z0-9]+$')
    for _, r in spec_df.iterrows():
        code = str(r.get('품번', '')).strip()
        if not code or not _code_re.match(code):
            continue
        name = str(r.get('품명', '')).strip()
        size = str(r.get('사이즈', '')).strip()
        dim = _parse_ipsu_size(size)
        n = _IPSU_N_RE.search(name)
        price_raw = str(r.get('단가(원)', '')).strip().replace(',', '')
        try:
            price = float(price_raw) if price_raw else 0.0
        except Exception:
            price = 0.0
        specs.append({
            'code': code, 'name': name, 'size': size,
            'material': str(r.get('재질', '')).strip(),
            'vendor': str(r.get('아이템명', '')).strip(),
            'div': str(r.get('구분', '')).strip(),
            'tier': _ipsu_tier(name),
            'ipsu': int(n.group(1)) if n else None,   # 품명의 'N입'
            'price': price,                            # 단가(원) — 포장재비 계산용
            'dim': dim,                                # None이면 수동 입력 필요
        })
    specs.sort(key=lambda x: x['code'])
    return specs


def _ipsu_boxes_list():
    """박스입수 적재량 보드 → 완제품별 박스입수/PT적재량."""
    if MONDAY_DF is None or MONDAY_DF.empty:
        return []
    boxes = []
    box_df = MONDAY_DF[MONDAY_DF['보드명'] == '박스입수 적재량']
    for _, r in box_df.iterrows():
        code = str(r.get('아이템명', '')).strip().upper()
        raw = str(r.get('box 입수량', '')).strip().replace(',', '')
        if not code or not raw:
            continue
        try:
            cnt = int(float(raw))
        except Exception:
            continue
        plt_raw = str(r.get('PT적재량', '')).strip().replace(',', '')
        try:
            plt = int(float(plt_raw)) if plt_raw else 0
        except Exception:
            plt = 0
        boxes.append({'code': code, 'name': str(r.get('품명', '')).strip(),
                      'ipsu': cnt, 'pallet': plt, 'div': str(r.get('구분', '')).strip()})
    boxes.sort(key=lambda x: x['code'])
    return boxes


@app.route('/api/ipsu_specs', methods=['GET'])
@cached_api()
def api_ipsu_specs():
    """입수 테스트 패널용 — 부자재 규격(파우치/단상자/외박스 치수) + 박스입수 적재량."""
    specs, boxes = _ipsu_specs_list(), _ipsu_boxes_list()
    parsed = sum(1 for s in specs if s['dim'])
    return jsonify({'specs': specs, 'boxes': boxes,
                    'total': len(specs), 'parsed': parsed, 'box_total': len(boxes)})


@app.route('/api/ipsu_bom', methods=['GET'])
@cached_api()
def api_ipsu_bom():
    """완제품 → 포장 부자재 자동 매핑 (BOM 전개).

    G0010 같은 완제품 하나를 고르면 BOM을 재귀 전개해
    ①1차파우치 ②2차파우치/단상자 ③외박스를 한 번에 채운다.
    - 자품이 부자재규격(B/C코드)이면 채택, 반제품(E코드)이면 한 단계 더 내려감
    - 상자류(form='상자')는 C코드=외박스, B코드=단상자로 구분
    - 파우치는 depth 0=2차, depth 1 이상=1차 (품명의 '1차/2차' 표기가 있으면 우선)
    """
    if BOM_DF is None or BOM_DF.empty:
        return jsonify({'products': [], 'total': 0})

    spec_by = {s['code']: s for s in _ipsu_specs_list() if s['dim']}
    box_by = {b['code']: b for b in _ipsu_boxes_list()}

    bom = BOM_DF.copy()
    bom['_mo'] = bom['모품번'].astype(str).str.strip()
    bom['_ja'] = bom['자품번'].astype(str).str.strip()
    # 소요량 — 외박스는 0.111처럼 1 미만으로 들어가며 그 역수가 곧 박스입수량
    bom['_q'] = pd.to_numeric(bom.get('정미수량'), errors='coerce')
    bom['_q'] = bom['_q'].fillna(pd.to_numeric(bom.get('실소요량'), errors='coerce')).fillna(1.0)
    child_map = {}
    name_map = {}
    for mo, grp in bom.groupby('_mo'):
        child_map[mo] = list(zip(grp['_ja'], grp['_q']))
        name_map[mo] = str(grp['모품명'].iloc[0]).strip()

    def _expand(code, depth=0, seen=None, qty=1.0):
        """(depth, spec, 누적소요량) 목록 — 부자재규격에 있는 자품만 수집.
        누적소요량 = 완제품 1개당 그 부자재가 몇 개 쓰이는지 (경로상 수량의 곱)."""
        seen = seen or set()
        if code in seen or depth > 3:
            return []
        seen.add(code)
        out = []
        for c, q in child_map.get(code, []):
            try:
                q = float(q)
            except Exception:
                q = 1.0
            if q <= 0:
                q = 1.0
            if c in spec_by:
                out.append((depth, spec_by[c], qty * q))
            else:
                out += _expand(c, depth + 1, seen, qty * q)
        return out

    def _ratio(num, den):
        """상위/하위 소요량 비 → 입수량. 1~100000 범위의 정수로 떨어질 때만 채택."""
        if not num or not den or den <= 0:
            return None
        v = num / den
        if v < 1 or v > 100000:
            return None
        r = round(v)
        return r if abs(v - r) < 0.05 * max(r, 1) else None

    # 다른 품목의 자품으로 쓰이는 코드는 중간 반제품 → 최상위(완제품)만 남김
    consumed = set(bom['_ja'])

    products = []
    for mo in child_map:
        if mo in consumed:
            continue
        parts = _expand(mo)
        if not parts:
            continue
        outer = inner_box = None
        pouches = []
        qty = {}                                # spec code → 완제품당 누적소요량
        for depth, s, q in parts:
            qty.setdefault(s['code'], q)
            if (s['dim'] or {}).get('form') == '상자':
                if s['code'].upper().startswith('C'):
                    outer = outer or s          # 외박스(RRP/전용박스)
                else:
                    inner_box = inner_box or s  # 단상자
            else:
                pouches.append((depth, s))
        # 품명 표기(1차/2차) 우선, 없으면 BOM 깊이로 판정 — 깊을수록 안쪽(1차)
        a = next((s for _, s in pouches if s['tier'] == '1차'), None)
        b = next((s for _, s in pouches if s['tier'] == '2차'), None)
        rest = [(d, s) for d, s in pouches if s is not a and s is not b]
        rest.sort(key=lambda x: -x[0])
        for _, s in rest:
            if a is None:
                a = s                           # 가장 안쪽 = 1차파우치
            elif b is None:
                b = s                           # 그 바깥 = 2차파우치
        b = inner_box or b                      # 단상자가 있으면 내부용기로 우선
        c = outer
        if not (a or b or c):
            continue
        # BOM 소요량 비로 입수량 산출 — 외박스는 1 미만(0.111=9입)으로 들어감
        qa = qty.get(a['code']) if a else None
        qb = qty.get(b['code']) if b else None
        qc = qty.get(c['code']) if c else None
        bom_n1 = _ratio(qa, qb)                       # 내부용기당 1차 입수
        # 박스당 내부용기(없으면 1차, 그마저 없으면 완제품 1개 기준) 입수
        bom_n2 = _ratio(qb or qa or 1.0, qc)
        bx = box_by.get(mo.upper(), {})
        products.append({
            'code': mo, 'name': name_map.get(mo, ''),
            'ipsu': bx.get('ipsu'), 'pallet': bx.get('pallet'),
            'bomN1': bom_n1, 'bomN2': bom_n2,
            'a': a, 'b': b, 'c': c,
        })
    products.sort(key=lambda x: x['code'])
    full = sum(1 for p in products if p['a'] and p['b'] and p['c'])
    return jsonify({'products': products, 'total': len(products), 'full': full,
                    'withIpsu': sum(1 for p in products if p['ipsu']),
                    'withBomN2': sum(1 for p in products if p['bomN2'])})


def _ipsu_norm(s: str) -> str:
    """품명 정규화 — 대괄호/괄호·기호 제거 후 비교용 키."""
    s = re.sub(r'\[.*?\]|\(.*?\)', '', s or '')
    return re.sub(r'[^\w가-힣]', '', s).lower()


@app.route('/api/ipsu_calib', methods=['GET'])
def api_ipsu_calib():
    """실측 캘리브레이션용 — 부자재 규격(파우치 치수) ↔ 박스입수 적재량(실측 입수) 매칭 쌍.
    프론트에서 실측 입수와 기하 계산치를 비교해 눌림(압축)계수를 역산한다."""
    if MONDAY_DF is None or MONDAY_DF.empty:
        return jsonify({'pairs': [], 'total': 0})

    spec_df = MONDAY_DF[MONDAY_DF['보드명'] == '부자재 규격']
    box_df = MONDAY_DF[MONDAY_DF['보드명'] == '박스입수 적재량']
    _code_re = re.compile(r'^[A-Za-z0-9]+$')

    boxes = []
    for _, r in box_df.iterrows():
        raw = str(r.get('box 입수량', '')).strip().replace(',', '')
        if not raw:
            continue
        try:
            cnt = int(float(raw))
        except Exception:
            continue
        nm = str(r.get('품명', '')).strip()
        boxes.append({'code': str(r.get('아이템명', '')).strip().upper(),
                      'name': nm, 'key': _ipsu_norm(nm), 'ipsu': cnt})

    pairs = []
    for _, r in spec_df.iterrows():
        code = str(r.get('품번', '')).strip()
        if not code or not _code_re.match(code):
            continue
        name = str(r.get('품명', '')).strip()
        dim = _parse_ipsu_size(str(r.get('사이즈', '')).strip())
        if not dim:
            continue
        key = _ipsu_norm(name)[:8]
        if len(key) < 4:
            continue
        for b in boxes:
            if key in b['key']:
                pairs.append({
                    'spec_code': code, 'spec_name': name, 'dim': dim,
                    'tier': _ipsu_tier(name),
                    'box_code': b['code'], 'box_name': b['name'],
                    'actual': b['ipsu'],       # 실측 박스당 입수
                })
                break
    return jsonify({'pairs': pairs, 'total': len(pairs)})


@app.route('/api/sales_summary', methods=['GET'])
@cached_api()
def api_sales_summary():
    """매출 현황 — 온라인팀(팀장님) 판매 API 공급가 합계 기준 월별 추이 + 채널별 (2026-09-23 사용자 지시: 매출은 전부 팀장님 자료).
    Monday '2026년 매출 현황' 보드는 더 이상 쓰지 않음 (_sales_summary_monday_legacy 참고용 보존)."""
    df = SALES_DAILY_DF
    empty = {'monthly': [], 'div_map': {}, 'latest_month': '', 'kpi': {}, 'source': '판매 API 자료 없음'}
    if df is None or df.empty or 'amt' not in df.columns:
        return jsonify(empty)
    d = df[df['amt'] > 0].copy()
    if d.empty:
        return jsonify(empty)
    d['ym'] = d['date'].astype(str).str[:7]
    monthly = d.groupby('ym')['amt'].sum()
    months = sorted(monthly.index)
    div_map = {}
    for ym, g in d.groupby('ym'):
        s = g.groupby('channel_name')['amt'].sum().sort_values(ascending=False)
        div_map[ym] = [{'div': k, 'amount': int(v)} for k, v in s.items() if v > 0]
    latest = months[-1]
    prev = months[-2] if len(months) >= 2 else ''
    cur_amt, prev_amt = float(monthly.get(latest, 0)), float(monthly.get(prev, 0))
    kpi = {'latest_month': latest, 'latest_amount': int(cur_amt), 'prev_amount': int(prev_amt),
           'mom_pct': round((cur_amt - prev_amt) / prev_amt * 100, 1) if prev_amt > 0 else 0,
           'avg_12m': int(sum(monthly[m] for m in months[-12:]) / max(len(months[-12:]), 1))}
    return jsonify({'monthly': [{'ym': m, 'amount': int(monthly[m])} for m in months], 'div_map': div_map,
                    'latest_month': latest, 'kpi': kpi, 'as_of': str(d['date'].max()),
                    'current_month': datetime.now().strftime('%Y-%m'),
                    'source': '온라인팀 판매자료 · 공급가(VAT 제외) · 온라인+오프라인'})


def _sales_summary_monday_legacy():
    """(미사용) Monday '2026년 매출 현황' 보드 — 월별 매출 추이 + 구분별 분석. 2026-09-23 판매 API로 대체."""
    if MONDAY_DF is None or MONDAY_DF.empty:
        return jsonify({'monthly': [], 'by_div': [], 'latest_month': '', 'kpi': {}})
    s = MONDAY_DF[MONDAY_DF['보드명'] == '2026년 매출 현황'].copy()
    if s.empty:
        return jsonify({'monthly': [], 'by_div': [], 'latest_month': '', 'kpi': {}})

    def _parse_ym(g):
        m = re.search(r'(\d{4})년\s*(\d{1,2})월', str(g))
        return f'{m.group(1)}-{int(m.group(2)):02d}' if m else None

    def _num(v):
        try:
            return float(str(v).replace(',', '').strip() or 0)
        except Exception:
            return 0.0

    # 월별 합계 (Duplicate 그룹 제외)
    monthly = {}       # ym -> 합계
    div_by_month = {}  # ym -> {구분: 합계}
    for _, r in s.iterrows():
        grp = str(r.get('그룹', ''))
        if 'Duplicate' in grp:
            continue
        ym = _parse_ym(grp)
        if not ym:
            continue
        amt = _num(r.get('금액', ''))
        if amt <= 0:
            continue
        monthly[ym] = monthly.get(ym, 0) + amt
        div = str(r.get('구분', '')).strip() or '기타'
        div_by_month.setdefault(ym, {})
        div_by_month[ym][div] = div_by_month[ym].get(div, 0) + amt

    months = sorted(monthly.keys())
    # 전체 월 반환 (프론트에서 12개월 윈도우 슬라이딩)
    monthly_list = [{'ym': m, 'amount': int(monthly[m])} for m in months]
    latest = months[-1] if months else ''
    prev = months[-2] if len(months) >= 2 else ''

    # 월별 구분 데이터 (윈도우 끝월 기준 표시용)
    div_map = {}
    for ym, dd in div_by_month.items():
        div_map[ym] = [{'div': k, 'amount': int(v)} for k, v in
                       sorted(dd.items(), key=lambda x: -x[1]) if v > 0]

    # KPI: 당월/전월/증감
    cur_amt = monthly.get(latest, 0)
    prev_amt = monthly.get(prev, 0)
    mom = ((cur_amt - prev_amt) / prev_amt * 100) if prev_amt > 0 else 0
    kpi = {
        'latest_month': latest,
        'latest_amount': int(cur_amt),
        'prev_amount': int(prev_amt),
        'mom_pct': round(mom, 1),
        'avg_12m': int(sum(monthly[m] for m in months[-12:]) / max(len(months[-12:]), 1)) if months else 0,
    }
    return jsonify({'monthly': monthly_list, 'div_map': div_map,
                    'latest_month': latest, 'kpi': kpi})


@app.route('/api/order_receipt_summary', methods=['GET'])
@cached_api()
def api_order_receipt_summary():
    """아마란스 발주/입고 — 월별 발주액·입고액 추이 (합계금액 기준)."""
    from datetime import datetime as _dt

    def _num(v):
        try:
            return float(str(v).replace(',', '').strip() or 0)
        except Exception:
            return 0.0

    def _monthly(df, date_col, amt_col='합계금액'):
        """{ym(YYYYMM): 합계금액} 집계."""
        out = {}
        if df is None or df.empty or date_col not in df.columns or amt_col not in df.columns:
            return out
        for _, r in df.iterrows():
            ds = str(r.get(date_col, '')).strip()
            ym = ds[:6]
            if len(ym) != 6 or not ym.isdigit():
                continue
            out[ym] = out.get(ym, 0) + _num(r.get(amt_col, 0))
        return out

    order_m   = _monthly(ORDER_DF, '발주일자')
    receipt_m = _monthly(RCV_DF, '입고일자')

    cur_ym = _dt.now().strftime('%Y%m')

    # 공통 월축 (합집합) — 당월(진행 중) 포함 전체 반환. 부분치도 그대로 표시.
    sel = sorted(set(order_m) | set(receipt_m))

    monthly_list = []
    for ym in sel:
        oa = order_m.get(ym)
        ra = receipt_m.get(ym)
        monthly_list.append({
            'ym': f'{ym[:4]}-{ym[4:6]}',
            'order_amt':   int(oa) if oa is not None else None,
            'receipt_amt': int(ra) if ra is not None else None,
            'is_current':  (ym == cur_ym),   # 당월(진행 중) 표시용
        })

    # KPI: 값 있는 최신월 (당월 진행 중이면 표시)
    last_order = next((m for m in reversed(monthly_list) if m['order_amt'] is not None), None)
    last_recv  = next((m for m in reversed(monthly_list) if m['receipt_amt'] is not None), None)
    kpi = {
        'latest_order_month': last_order['ym'] if last_order else '',
        'latest_order_amt': last_order['order_amt'] if last_order else 0,
        'latest_order_current': bool(last_order and last_order.get('is_current')),
        'latest_recv_month': last_recv['ym'] if last_recv else '',
        'latest_recv_amt': last_recv['receipt_amt'] if last_recv else 0,
        'latest_recv_current': bool(last_recv and last_recv.get('is_current')),
    }
    return jsonify({'monthly': monthly_list, 'kpi': kpi})


@app.route('/api/sales_qty', methods=['GET'])
@cached_api()
def api_sales_qty():
    """월 판매기반 자료 — 판매 데이터(SKU→아마란스 품번) 월별 판매수량(낱개) 추이.
    분류: 자사(G)/사급(H)/예외사급(I+BOM)/사입(I-BOM無). + 최근 완결월 제품 TOP10."""
    if SALES_DF is None or SALES_DF.empty:
        return jsonify({'monthly': [], 'top': [], 'classes': [], 'latest_month': ''})

    cur_ym = datetime.now().strftime('%Y%m')

    def _cls(code, p):
        # 패널 표시 용어: G=자사, H=유상사급, I=상품매입 (소비량 계산 로직과 별개)
        if p == 'G':
            return '자사'
        if p == 'H':
            return '유상사급'
        if p == 'I':
            return '상품매입'
        return '기타'

    # 품번 → 표시명 (BOM 모품명 우선, 없으면 단가/현재고/출하 품명 순으로 보완)
    name_of = {}
    if BOM_DF is not None and not BOM_DF.empty:
        for _, r in BOM_DF.drop_duplicates('모품번').iterrows():
            nm = str(r.get('모품명', '')).strip()
            if nm:
                name_of.setdefault(str(r['모품번']).strip().upper(), nm)
    for _df in (PRICE_DF, STOCK_DF, SHIP_DF):
        if _df is not None and not _df.empty and '품번' in _df.columns and '품명' in _df.columns:
            for _, r in _df.iterrows():
                c = str(r['품번']).strip().upper()
                nm = str(r.get('품명', '')).strip()
                if c and nm and c not in name_of:
                    name_of[c] = nm

    df = SALES_DF.copy()
    # 분류: 매핑표 패널분류(pcls) 우선, 없으면 접두사 기반
    if 'pcls' in df.columns:
        df['cls'] = df['pcls'].where(df['pcls'].astype(str).str.strip() != '',
                                     [_cls(c, p) for c, p in zip(df['code'], df['prefix'])])
    else:
        df['cls'] = [_cls(c, p) for c, p in zip(df['code'], df['prefix'])]
    cls_of = dict(zip(df['code'].astype(str).str.upper(), df['cls']))   # 품번→분류
    if 'amt' not in df.columns:
        df['amt'] = 0.0
    has_amt = bool((df['amt'] > 0).any())   # 공급가 있으면 매출액 표시

    CLASSES = ['자사', '유상사급', '상품매입']
    months = sorted(df['ym'].unique())
    monthly = []
    for ym in months:
        sub = df[df['ym'] == ym]
        row = {'ym': f'{ym[:4]}-{ym[4:6]}', 'is_current': (ym == cur_ym),
               'total': int(sub['ea'].sum()), 'total_amt': int(sub['amt'].sum())}
        for c in CLASSES:
            csub = sub.loc[sub['cls'] == c]
            row[c] = int(csub['ea'].sum())              # 분류별 수량
            row[c + '_amt'] = int(csub['amt'].sum())    # 분류별 매출액
        monthly.append(row)

    # 제품별 — 최근 완결월(당월 제외) + 최근 3개월 평균, 수량·매출액 (전체, 검색용)
    complete = [m for m in months if m != cur_ym]
    latest = complete[-1] if complete else (months[-1] if months else '')
    last3 = complete[-3:] if complete else []
    products = []
    if latest:
        n3 = max(len(last3), 1)
        m1 = df[df['ym'] == latest]; s3 = df[df['ym'].isin(last3)]
        m1q = m1.groupby('code')['ea'].sum();  m1a = m1.groupby('code')['amt'].sum()
        s3q = s3.groupby('code')['ea'].sum();  s3a = s3.groupby('code')['amt'].sum()
        for code in (set(m1q.index) | set(s3q.index)):
            rec = {'code': code, 'name': name_of.get(code, ''),
                   'cls': cls_of.get(str(code).upper(), _cls(code, str(code)[:1].upper())),
                   'm1': int(round(float(m1q.get(code, 0)))),
                   'avg3': int(round(float(s3q.get(code, 0)) / n3)),
                   'm1_amt': int(round(float(m1a.get(code, 0)))),
                   'avg3_amt': int(round(float(s3a.get(code, 0)) / n3))}
            if rec['m1'] == 0 and rec['avg3'] == 0:
                continue
            products.append(rec)
        products.sort(key=lambda x: -(x['avg3_amt'] if has_amt else x['avg3']))

    return jsonify({'monthly': monthly, 'products': products, 'classes': CLASSES,
                    'latest_month': f'{latest[:4]}-{latest[4:6]}' if latest else '',
                    'avg_months': len(last3), 'has_amt': has_amt})


def _sales_name_map():
    """품번 → 표시명 (BOM 모품명 우선, 단가/현재고/출하 품명 보완). sales_qty와 동일 규칙."""
    name_of = {}
    if BOM_DF is not None and not BOM_DF.empty:
        for _, r in BOM_DF.drop_duplicates('모품번').iterrows():
            nm = str(r.get('모품명', '')).strip()
            if nm:
                name_of.setdefault(str(r['모품번']).strip().upper(), nm)
    for _df in (PRICE_DF, STOCK_DF, SHIP_DF):
        if _df is not None and not _df.empty and '품번' in _df.columns and '품명' in _df.columns:
            for _, r in _df.iterrows():
                c = str(r['품번']).strip().upper()
                nm = str(r.get('품명', '')).strip()
                if c and nm and c not in name_of:
                    name_of[c] = nm
    return name_of


def _sales_with_cls(df):
    """SALES_DF에 분류(자사/유상사급/상품매입) 컬럼 부여. sales_qty와 동일 규칙."""
    def _cls(code, p):
        return {'G': '자사', 'H': '유상사급', 'I': '상품매입'}.get(p, '기타')
    d = df.copy()
    if 'pcls' in d.columns:
        d['cls'] = d['pcls'].where(d['pcls'].astype(str).str.strip() != '',
                                   [_cls(c, p) for c, p in zip(d['code'], d['prefix'])])
    else:
        d['cls'] = [_cls(c, p) for c, p in zip(d['code'], d['prefix'])]
    return d


@app.route('/api/sales_qty_class', methods=['GET'])
def api_sales_qty_class():
    """특정 월+분류의 제품별 판매 상세 — 매출·판매 추이 차트 세그먼트(자사/유상사급/상품매입) 클릭용."""
    ym = (request.args.get('ym') or '').strip().replace('-', '')   # YYYYMM
    cls = (request.args.get('cls') or '').strip()
    empty = {'ym': ym, 'cls': cls, 'items': [], 'has_amt': False, 'total_ea': 0, 'total_amt': 0}
    if not ym or not cls or SALES_DF is None or SALES_DF.empty:
        return jsonify(empty)

    df = _sales_with_cls(SALES_DF)
    if 'amt' not in df.columns:
        df['amt'] = 0.0
    sub = df[(df['ym'].astype(str) == ym) & (df['cls'] == cls)]
    if sub.empty:
        return jsonify(empty)

    name_of = _sales_name_map()
    g = sub.groupby('code').agg(ea=('ea', 'sum'), amt=('amt', 'sum')).reset_index()
    has_amt = bool((g['amt'] > 0).any())
    g = g.sort_values('amt' if has_amt else 'ea', ascending=False)
    items = [{'code': str(r['code']), 'name': name_of.get(str(r['code']).strip().upper(), ''),
              'ea': int(r['ea']), 'amt': int(r['amt'])} for _, r in g.iterrows()]
    return jsonify({'ym': f'{ym[:4]}-{ym[4:6]}', 'cls': cls, 'items': items, 'has_amt': has_amt,
                    'total_ea': int(g['ea'].sum()), 'total_amt': int(g['amt'].sum())})


@app.route('/api/sales_qty_product', methods=['GET'])
def api_sales_qty_product():
    """특정 제품(품번)의 월별 판매수량·매출액 추이 (과거~현재). 제품 TOP10 행 클릭용."""
    code = (request.args.get('code') or '').strip()
    if not code or SALES_DF is None or SALES_DF.empty:
        return jsonify({'code': code, 'monthly': [], 'has_amt': False})

    cur_ym = datetime.now().strftime('%Y%m')
    df = SALES_DF
    sub = df[df['code'].astype(str).str.strip().str.upper() == code.upper()]
    if sub.empty:
        return jsonify({'code': code, 'monthly': [], 'has_amt': False})

    has_amt = bool(('amt' in sub.columns) and (sub['amt'] > 0).any())
    # 전체 기간을 빈 달까지 채워 연속 추이로 (판매 없는 달=0)
    all_months = sorted(df['ym'].astype(str).unique())
    ea_by = sub.groupby('ym')['ea'].sum()
    amt_by = sub.groupby('ym')['amt'].sum() if 'amt' in sub.columns else {}
    first = str(sub['ym'].min())
    monthly = []
    for ym in all_months:
        if ym < first:                       # 첫 판매월 이전은 생략
            continue
        ea = int(ea_by.get(ym, 0))
        amt = int(amt_by.get(ym, 0)) if len(amt_by) else 0
        monthly.append({'ym': f'{ym[:4]}-{ym[4:6]}', 'is_current': (ym == cur_ym),
                        'ea': ea, 'amt': amt})
    return jsonify({'code': code, 'monthly': monthly, 'has_amt': has_amt})


@app.route('/api/sales_detail', methods=['GET'])
def api_sales_detail():
    """특정 월 매출 TOP 10 — 상품(품번)별 공급가 합계, 채널 표기. 판매 API 기준 (2026-09-23)."""
    ym = (request.args.get('ym') or '').strip()  # 'YYYY-MM'
    df = SALES_DAILY_DF
    if df is None or df.empty or not re.match(r'^\d{4}-\d{2}$', ym):
        return jsonify({'ym': ym, 'items': [], 'total': 0, 'count': 0})
    d = df[(df['date'].astype(str).str[:7] == ym) & (df['amt'] > 0)].copy()
    if d.empty:
        return jsonify({'ym': ym, 'items': [], 'total': 0, 'count': 0})
    d['key'] = d['code'].where(d['code'] != '', 'SKU ' + d['sku'].astype(str))
    names = _sales_name_map()
    g = d.groupby('key').agg(amount=('amt', 'sum'), sname=('name', 'first'),
                             chs=('channel_name', lambda s: ', '.join(sorted(set(s))[:3]))).sort_values('amount', ascending=False)
    rows = [{'name': f"{k} {names.get(k, '') or r['sname']}".strip(), 'amount': int(r['amount']), 'div': r['chs']}
            for k, r in g.iterrows()]
    total = float(d['amt'].sum())
    return jsonify({'ym': ym, 'items': rows[:10], 'total': int(total), 'count': len(rows)})


def _sales_detail_monday_legacy(ym):
    """(미사용) Monday 매출 현황 TOP 10. 2026-09-23 판매 API로 대체."""
    if MONDAY_DF is None or MONDAY_DF.empty or not re.match(r'^\d{4}-\d{2}$', ym):
        return jsonify({'ym': ym, 'items': [], 'total': 0})
    s = MONDAY_DF[MONDAY_DF['보드명'] == '2026년 매출 현황']

    def _pym(g):
        m = re.search(r'(\d{4})년\s*(\d{1,2})월', str(g))
        return f'{m.group(1)}-{int(m.group(2)):02d}' if m else None

    def _num(v):
        try:
            return float(str(v).replace(',', '').strip() or 0)
        except Exception:
            return 0.0

    rows, total = [], 0.0
    for _, r in s.iterrows():
        grp = str(r.get('그룹', ''))
        if 'Duplicate' in grp or _pym(grp) != ym:
            continue
        amt = _num(r.get('금액', ''))
        if amt <= 0:
            continue
        total += amt
        rows.append({
            'name': str(r.get('아이템명', '')).strip(),
            'amount': int(amt),
            'div': str(r.get('구분', '')).strip(),
        })
    rows.sort(key=lambda x: -x['amount'])
    return jsonify({'ym': ym, 'items': rows[:10], 'total': int(total), 'count': len(rows)})


@app.route('/api/order_receipt_detail', methods=['GET'])
def api_order_receipt_detail():
    """특정 월 발주·입고 거래처 TOP 10 — 발주입고 차트 클릭 시."""
    ym = (request.args.get('ym') or '').strip().replace('-', '')  # 'YYYYMM'
    if not re.match(r'^\d{6}$', ym):
        return jsonify({'ym': ym, 'order': [], 'receipt': []})

    def _num(v):
        try:
            return float(str(v).replace(',', '').strip() or 0)
        except Exception:
            return 0.0

    def _top(df, date_col):
        if df is None or df.empty or date_col not in df.columns or '거래처명' not in df.columns or '합계금액' not in df.columns:
            return [], 0
        agg = {}
        for _, r in df.iterrows():
            if str(r.get(date_col, ''))[:6] != ym:
                continue
            v = str(r.get('거래처명', '')).strip() or '미지정'
            agg[v] = agg.get(v, 0) + _num(r.get('합계금액', ''))
        items = [{'name': k, 'amount': int(v)} for k, v in
                 sorted(agg.items(), key=lambda x: -x[1]) if v > 0]
        return items[:10], int(sum(agg.values()))

    order_top, order_total = _top(ORDER_DF, '발주일자')
    recv_top, recv_total = _top(RCV_DF, '입고일자')
    ym_disp = f'{ym[:4]}-{ym[4:6]}'
    return jsonify({'ym': ym_disp, 'order': order_top, 'order_total': order_total,
                    'receipt': recv_top, 'receipt_total': recv_total})


def _normalize_eta(raw, posted):
    """raw 날짜 문자열을 비교 가능한 'YYYY-MM-DD'로 정규화.
    M/D 형식은 posted의 연도 사용 (raw < posted 이면 다음 해로 간주)."""
    s = re.sub(r'\([월화수목금토일]\)', '', raw).strip()
    m = re.match(r'^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})$', s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r'^(\d{1,2})/(\d{1,2})$', s)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        try:
            py = int(posted[:4]); pm = int(posted[5:7])
        except Exception:
            py, pm = 2026, 1
        # M/D 가 작성월보다 6개월 이상 이전이면 내년 예정으로 간주
        year = py + 1 if (mm - pm) <= -6 else py
        return f"{year:04d}-{mm:02d}-{dd:02d}"
    return '9998-99-99'


# ────────────────────────────────────────────
# 대시보드: 자품번(부재료) 상세 정보
# ────────────────────────────────────────────
@app.route('/api/item/<code>', methods=['GET'])
def api_item_detail(code):
    code = str(code).strip().upper()
    if not code:
        return jsonify({'error': '품번이 비어 있습니다'}), 400

    # 기본 정보 (BOM 또는 재고에서 품명 확보)
    name = ''
    category = ''
    unit = ''
    if BOM_DF is not None:
        hit = BOM_DF[BOM_DF['자품번'].str.upper() == code]
        if not hit.empty:
            name = str(hit.iloc[0].get('자품명', '')).strip()
            category = str(hit.iloc[0].get('자품목구분', '')).strip()
            unit = str(hit.iloc[0].get('자품단위', '')).strip()
    if not name:
        m = DF[DF[COL_품목].str.upper() == code]
        if not m.empty:
            name = str(m.iloc[0][COL_품명]).strip()

    # 규격
    spec = SPEC_BY_CODE.get(code)

    # 단가
    price_info = get_price_info(code, name)

    # 재고 (외주처별) — 외주처 기준 집계.
    # 재고일지엔 같은 (품번, 외주처) 조합이 여러 행으로 중복될 수 있어 (공용 자재가 여러 제품 컨텍스트로 등장)
    # 중복 제거: 같은 (외주처, 재고량) 한 번만 카운트.
    unit_price = (price_info or {}).get('단가', 0) if price_info else 0
    match = DF[DF[COL_품목].str.upper() == code]
    agg = {}
    seen_pairs = set()  # (vendor, qty) 중복 제거 키
    for _, r in match.iterrows():
        vendor = str(r[COL_원산지]).strip() or '(미지정)'
        qty = _num(r[COL_재고량])
        pair_key = (vendor, qty)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        if vendor in agg:
            agg[vendor]['qty'] += qty
        else:
            agg[vendor] = {'vendor': vendor, 'qty': qty}
    total_stock = sum(v['qty'] for v in agg.values())
    for v in agg.values():
        v['cost'] = int(v['qty'] * (unit_price or 0))
    vendor_stocks = sorted(agg.values(), key=lambda x: -x['qty'])
    # 자사재고도 확인. E품번(반제품)은 JASA_DF에 없으므로 STOCK_DF(아마란스 현재고)에서 우선 조회.
    jasa_stock = None
    if code.startswith('E') and STOCK_DF is not None and '품번' in STOCK_DF.columns:
        sm = STOCK_DF[STOCK_DF['품번'].str.upper() == code]
        if not sm.empty:
            stock_qty = sum(_num(r.get('현재고', 0)) for _, r in sm.iterrows())
            if stock_qty > 0:
                jasa_stock = {
                    'qty': stock_qty,
                    'vendor': str(sm.iloc[0].get('창고명', '')).strip() or '자사',
                }
                total_stock += stock_qty
    if jasa_stock is None and JASA_DF is not None:
        col_품번 = JASA_DF.columns[1]
        col_총재고 = JASA_DF.columns[7]
        col_업체 = JASA_DF.columns[2]
        jm = JASA_DF[JASA_DF[col_품번].str.upper() == code]
        if not jm.empty:
            jasa_qty = sum(_num(r[col_총재고]) for _, r in jm.iterrows())
            jasa_stock = {
                'qty': jasa_qty,
                'vendor': str(jm.iloc[0][col_업체]).strip(),
            }
            total_stock += jasa_qty  # 자사재고도 총 재고에 합산

    # 최근 발주 내역 (최대 5건)
    orders = []
    if ORDER_DF is not None and '품번' in ORDER_DF.columns:
        om = ORDER_DF[ORDER_DF['품번'].str.upper() == code]
        if '발주일자' in om.columns:
            om = om.sort_values('발주일자', ascending=False)
        for _, r in om.head(5).iterrows():
            orders.append({
                'date': str(r.get('발주일자', '')),
                'vendor': str(r.get('거래처명', '')),
                'qty': _num(r.get('발주수량', 0)),
                'price': _num(r.get('단가', 0)),
            })

    # 최근 입고 내역 (최대 5건)
    receipts = []
    if RCV_DF is not None and '품번' in RCV_DF.columns:
        rm = RCV_DF[RCV_DF['품번'].str.upper() == code]
        date_col = '입고일자' if '입고일자' in rm.columns else None
        if date_col:
            rm = rm.sort_values(date_col, ascending=False)
        for _, r in rm.head(5).iterrows():
            receipts.append({
                'date': str(r.get(date_col, '')) if date_col else '',
                'vendor': str(r.get('거래처명', '')),
                'qty': _num(r.get('입고수량', 0)),
            })

    return jsonify({
        'code': code,
        'name': name,
        'category': category,
        'unit': unit,
        'spec': spec,
        'price': price_info,
        'totalStock': total_stock,
        'vendorStocks': vendor_stocks,
        'jasaStock': jasa_stock,
        'orders': orders,
        'receipts': receipts,
    })


# ────────────────────────────────────────────
# 대시보드 템플릿
# ────────────────────────────────────────────
DASHBOARD_TEMPLATE = '''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>구매/외주 대시보드</title>
<script>(function(){const f=window.fetch.bind(window);window.fetch=function(i,n){n=n||{};const h=new Headers(n.headers||{});if(!h.has('ngrok-skip-browser-warning'))h.set('ngrok-skip-browser-warning','true');n.headers=h;return f(i,n);};})();</script>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-auth-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore-compat.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --surface: #ffffff;
    --surface-2: #fafbfd;
    --border: rgba(15,23,42,0.09);
    --border-2: rgba(15,23,42,0.14);
    --text: #0b1220;
    --text-2: #475569;
    --text-3: #64748b;
    --brand: #4f46e5;
    --brand-2: #7c3aed;
    --brand-soft: rgba(79,70,229,0.08);
    --success: #059669;
    --danger: #dc2626;
    --warning: #d97706;
    --radius: 14px;
    --shadow-xs: 0 1px 2px rgba(15,23,42,0.04);
    --shadow-sm: 0 2px 6px rgba(15,23,42,0.05), 0 1px 2px rgba(15,23,42,0.03);
    --shadow-md: 0 8px 20px -6px rgba(15,23,42,0.08), 0 2px 4px rgba(15,23,42,0.04);
    --shadow-lg: 0 20px 40px -12px rgba(15,23,42,0.14), 0 8px 16px -6px rgba(15,23,42,0.06);
    --shadow-xl: 0 32px 64px -16px rgba(15,23,42,0.2);
  }
  body {
    font-family: 'Noto Sans KR', -apple-system, 'Segoe UI', Roboto, sans-serif;
    background:
      radial-gradient(ellipse 80% 60% at 30% 0%, rgba(79,70,229,0.08), transparent 60%),
      radial-gradient(ellipse 60% 50% at 80% 20%, rgba(124,58,237,0.06), transparent 60%),
      #f4f5fa;
    color: var(--text); min-height: 100vh;
    -webkit-font-smoothing: antialiased;
    letter-spacing: -0.01em;
  }

  /* ───── Chart Panels ───── */
  .chart-grid {
    max-width: 1440px; margin: 14px auto 0; padding: 0 32px;
    display: grid; grid-template-columns: repeat(30, 1fr); gap: 12px;
  }
  .chart-panel.po-inline     { grid-column: span 15; border-top: 3px solid #059669; }
  .chart-panel.os-inline     { grid-column: span 15; border-top: 3px solid #dc2626; }
  .chart-panel.pcalc-panel   { grid-column: span 18; }
  .chart-panel.alert-span    { grid-column: span 10; }   /* 자사·외주·발주타이밍 3등분 */
  .chart-panel.price-span    { grid-column: span 12; }   /* 단가변동 (단가계산기와 한 줄) */
  .chart-panel.plan-panel    { grid-column: span 10; border-top: 3px solid #0891b2; }   /* 수급 플래너 — 시안 (2026-09-23: 수급·채널품절·거래처 3등분 한 줄) */
  .chart-panel.chstock-panel { grid-column: span 10; border-top: 3px solid #ea580c; }   /* 채널 품절 경보 — 주황 (2026-09-23) */
  .chart-panel.vendor-panel  { grid-column: span 10; border-top: 3px solid #7c3aed; }   /* 거래처 스코어 — 보라 */
  /* 3등분 폭에 맞춰 헤더 부제목은 한 줄 말줄임, 헤더 오른쪽 컨트롤은 줄바꿈 허용 */
  .plan-panel .chart-head, .chstock-panel .chart-head, .vendor-panel .chart-head { flex-wrap: wrap; row-gap: 6px; }
  .plan-panel .chart-sub, .chstock-panel .chart-sub, .vendor-panel .chart-sub { display: block; margin-left: 0; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
  .ch-tag { display:inline-block; font-size:10px; font-weight:700; padding:1px 6px; border-radius:5px; margin-left:4px; background:#fff7ed; color:#c2410c; vertical-align:1px; }
  .ch-tag.on { background:#eff6ff; color:#1d4ed8; }
  .vk-chips { display:flex; gap:3px; background:#f1f5f9; border-radius:8px; padding:2px; }
  .vk-chip { border:0; background:transparent; font-size:11px; font-weight:700; color:#64748b; padding:3px 9px; border-radius:6px; cursor:pointer; font-family:inherit; }
  .vk-chip.on { background:#fff; color:#7c3aed; box-shadow:0 1px 2px rgba(0,0,0,.12); }
  .vk-tag { display:inline-block; font-size:10px; font-weight:700; padding:1px 6px; border-radius:5px; margin-left:6px; vertical-align:1px; }
  .vk-tag.po { background:#ede9fe; color:#6d28d9; }
  .vk-tag.wp { background:#ffedd5; color:#c2410c; }
  .vk-tag.both { background:linear-gradient(90deg,#ede9fe,#ffedd5); color:#7c3aed; }

  /* ── KPI 요약 띠 ── */
  .kpi-strip { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:12px; margin:16px 24px 4px; }
  .kpi-tile { position:relative; display:flex; align-items:center; gap:12px; min-width:0; cursor:pointer;
    background:#fff; border:1px solid #e6e9f0; border-radius:14px; padding:13px 14px 12px 14px; overflow:hidden;
    box-shadow:0 1px 2px rgba(15,23,42,.04); transition:transform .14s, box-shadow .14s; }
  .kpi-tile::before { content:''; position:absolute; left:0; top:0; right:0; height:3px; background:var(--kpi-bar,#94a3b8); }
  .kpi-tile:hover { transform:translateY(-2px); box-shadow:0 10px 22px rgba(15,23,42,.10); }
  .kpi-tile .kpi-ic { flex:0 0 40px; width:40px; height:40px; border-radius:12px; display:flex; align-items:center; justify-content:center;
    font-size:19px; background:var(--kpi-soft,#f1f5f9); box-shadow:inset 0 0 0 1px rgba(255,255,255,.6); }
  .kpi-tile .kpi-body { min-width:0; flex:1; }
  .kpi-tile .kpi-v { font-size:24px; font-weight:800; line-height:1.05; letter-spacing:-.6px; color:var(--kpi-fg,#0f172a); font-variant-numeric:tabular-nums; }
  .kpi-tile .kpi-l { font-size:11.5px; font-weight:700; color:#334155; margin-top:3px; white-space:nowrap; }
  .kpi-tile .kpi-s { font-size:10.5px; color:#8a94a6; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .kpi-tile.lv-ok   { --kpi-bar:linear-gradient(90deg,#16a34a,#4ade80); --kpi-soft:#ecfdf3; --kpi-fg:#15803d; border-color:#cdeedb; background:linear-gradient(180deg,#fff 0%,#f6fdf8 100%); }
  .kpi-tile.lv-warn { --kpi-bar:linear-gradient(90deg,#d97706,#fbbf24); --kpi-soft:#fff7e6; --kpi-fg:#b45309; border-color:#f3e2b8; background:linear-gradient(180deg,#fff 0%,#fffbf2 100%); }
  .kpi-tile.lv-bad  { --kpi-bar:linear-gradient(90deg,#dc2626,#f87171); --kpi-soft:#fef1f1; --kpi-fg:#b91c1c; border-color:#f5cfcf; background:linear-gradient(180deg,#fff 0%,#fff7f7 100%); }
  .kpi-tile.lv-info { --kpi-bar:linear-gradient(90deg,#0891b2,#22d3ee); --kpi-soft:#ecfbfe; --kpi-fg:#0e7490; border-color:#c6ecf3; background:linear-gradient(180deg,#fff 0%,#f4fcfe 100%); }
  @media (max-width:1300px) { .kpi-strip { grid-template-columns:repeat(3,minmax(0,1fr)); } }
  @media (max-width:900px) { .kpi-strip { grid-template-columns:repeat(2,minmax(0,1fr)); margin:10px 12px 0; gap:8px; }
    .kpi-tile { padding:10px 11px; gap:9px; } .kpi-tile .kpi-ic { flex-basis:32px; width:32px; height:32px; font-size:15px; border-radius:9px; } .kpi-tile .kpi-v { font-size:20px; } }

  /* ── 섹션 점프 내비 ── */
  .sec-nav { position:fixed; right:10px; top:50%; transform:translateY(-50%); z-index:60; display:flex; flex-direction:column; gap:3px;
    background:rgba(255,255,255,.92); border:1px solid #e2e8f0; border-radius:12px; padding:6px 4px; box-shadow:0 6px 18px rgba(15,23,42,.10); backdrop-filter:blur(4px); }
  .sec-nav a { display:block; font-size:10.5px; font-weight:700; color:#64748b; text-decoration:none; padding:4px 7px; border-radius:7px; text-align:center; line-height:1.2; }
  .sec-nav a:hover { background:#f1f5f9; color:#0f172a; }
  .sec-nav a.on { background:#4f46e5; color:#fff; }
  @media (max-width:1100px) { .sec-nav { display:none; } }

  /* ── 회송 원가 역산 ── */
  .return-strip { max-width: 1440px; margin: 12px auto 0; padding: 0 32px; }
  .chart-panel.return-panel { border-top: 3px solid #db2777; }
  .rc-form { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:10px; }
  .rc-field { display:flex; align-items:center; gap:6px; }
  .rc-field label { font-size:11px; color:var(--text-3); font-weight:700; }
  .rc-form input { padding:7px 10px; font-size:12px; border:1px solid var(--border-2,#cbd5e1); border-radius:8px; outline:none; width:100%; font-family:inherit; }
  .rc-form input:focus { border-color:#db2777; box-shadow:0 0 0 3px rgba(219,39,119,.12); }
  .rc-btn { padding:7px 14px; font-size:12px; font-weight:700; border-radius:8px; border:1px solid #db2777; background:#db2777; color:#fff; cursor:pointer; font-family:inherit; }
  .rc-btn.ghost { background:#fff; color:#db2777; }
  .rc-btn:disabled { opacity:.45; cursor:default; }
  .rc-sug { position:absolute; left:0; right:0; top:100%; z-index:40; background:#fff; border:1px solid #e2e8f0; border-radius:8px; box-shadow:0 8px 20px rgba(15,23,42,.12); max-height:240px; overflow:auto; margin-top:4px; }
  .rc-sug div { padding:7px 10px; font-size:12px; cursor:pointer; display:flex; gap:8px; align-items:center; }
  .rc-sug div:hover, .rc-sug div.sel { background:#fdf2f8; }
  .rc-sug .c { font-weight:800; color:#db2777; min-width:52px; }
  .rc-sug .t { margin-left:auto; font-size:10px; color:#94a3b8; }
  .rc-sug div.nobom { color:#94a3b8; }
  .rc-sug div.nobom .c { color:#cbd5e1; }
  .rc-nobom { font-size:10px; font-weight:700; color:#b91c1c; background:#fee2e2; padding:1px 6px; border-radius:5px; white-space:nowrap; }
  .rc-summary { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; margin-bottom:10px; }
  .rc-card { background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px; padding:9px 12px; min-width:0; }
  .rc-card .k { font-size:10.5px; color:#64748b; font-weight:700; }
  .rc-card .v { font-size:18px; font-weight:800; color:#0f172a; margin-top:2px; font-variant-numeric:tabular-nums; letter-spacing:-.3px; }
  .rc-card .s { font-size:10.5px; color:#94a3b8; margin-top:1px; }
  .rc-card.raw .v { color:#b45309; } .rc-card.sub .v { color:#0e7490; } .rc-card.tot .v { color:#db2777; }
  .rc-table { border:1px solid #e2e8f0; border-radius:10px; overflow:auto; max-height:420px; }
  .rc-table table { width:100%; border-collapse:collapse; font-size:12px; }
  .rc-table th { position:sticky; top:0; background:#f8fafc; color:#64748b; font-size:11px; font-weight:700; text-align:left; padding:7px 9px; border-bottom:1px solid #e2e8f0; white-space:nowrap; }
  .rc-table td { padding:7px 9px; border-bottom:1px solid #f1f5f9; vertical-align:middle; }
  .rc-table tr.off td { opacity:.4; text-decoration:line-through; }
  .rc-table tr.grp td { background:#fafafa; font-weight:800; color:#334155; font-size:11.5px; }
  .rc-table .num { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
  .rc-table .code { font-weight:800; color:#db2777; cursor:pointer; white-space:nowrap; }
  .rc-src { display:inline-block; font-size:10px; padding:1px 6px; border-radius:5px; background:#f1f5f9; color:#64748b; white-space:nowrap; }
  .rc-src.po { background:#ecfdf5; color:#047857; } .rc-src.tbl { background:#eff6ff; color:#1d4ed8; } .rc-src.bom { background:#fef3c7; color:#92400e; } .rc-src.none { background:#fee2e2; color:#b91c1c; }
  @media (max-width:900px) { .return-strip { padding:0 12px; } .rc-summary { grid-template-columns:repeat(2,minmax(0,1fr)); } }

  /* ── 패널 접기 ── */
  .col-btn { border:1px solid #e2e8f0; background:#f8fafc; color:#475569; font-size:11px; font-weight:700; padding:3px 9px; border-radius:7px; cursor:pointer; font-family:inherit; white-space:nowrap; margin-left:8px; }
  .col-btn:hover { background:#eef2ff; color:#4f46e5; }
  section.collapsed .chart-panel > *:not(.chart-head) { display:none !important; }
  section.collapsed .chart-panel { padding-bottom:12px; }
  section.collapsed .chart-head { margin-bottom:0; }
  .sc-pill { display:inline-block; min-width:34px; text-align:center; padding:2px 7px; border-radius:7px; font-weight:800; font-size:12px; }
  .sc-a { background:#f0fdf4; color:#16a34a } .sc-b { background:#fefce8; color:#ca8a04 } .sc-c { background:#fef2f2; color:#dc2626 } .sc-n { background:#f1f5f9; color:#94a3b8 }
  .ck-overlay { display:none; position:fixed; inset:0; z-index:140; background:rgba(10,10,16,.55); backdrop-filter:blur(2px); align-items:flex-start; justify-content:center; padding-top:12vh }
  .ck-overlay.show { display:flex }
  .ck-box { width:min(680px,92vw); background:var(--card,#fff); border-radius:14px; box-shadow:0 18px 60px rgba(0,0,0,.4); overflow:hidden }
  .ck-box input { width:100%; box-sizing:border-box; padding:14px 18px; font-size:15px; border:none; border-bottom:1px solid var(--border); background:transparent; color:var(--text); outline:none }
  .ck-list { max-height:56vh; overflow:auto }
  .ck-row { display:flex; gap:10px; align-items:center; padding:9px 18px; cursor:pointer; font-size:13px; border-bottom:1px solid var(--border) }
  .ck-row:hover, .ck-row.sel { background:var(--surface-2,#f8fafc) }
  .ck-row .cd { font-weight:800; color:#4f46e5; min-width:64px } .ck-row .tp { font-size:10.5px; color:var(--text-3); margin-left:auto; white-space:nowrap }
  .ck-hint { padding:8px 18px; font-size:11px; color:var(--text-3) }
  .spec-strip { max-width: 1440px; margin: 12px auto 0; padding: 0 32px; }
  /* ───── 입수 테스트 (3D) ───── */
  .ipsu-strip { max-width: 1440px; margin: 12px auto 0; padding: 0 32px; }
  .chart-panel.ipsu-panel { border-top: 3px solid #7c3aed; }
  .ipsu-block { border: 1px solid var(--border); border-radius: 9px; padding: 8px 10px; margin-bottom: 8px; }
  .ipsu-block .ib-t { font-size: 11px; font-weight: 700; color: #7c3aed; margin-bottom: 6px; }
  .ipsu-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: flex-end; }
  .ipsu-fld { display: flex; flex-direction: column; gap: 3px; }
  .ipsu-fld label { font-size: 10px; color: var(--text-3); font-weight: 600; }
  .ipsu-num { width: 54px; padding: 5px 4px; font-size: 11.5px; border: 1px solid var(--border-2);
              border-radius: 6px; text-align: center; font-variant-numeric: tabular-nums; outline: none; }
  .ipsu-num.big { width: 62px; font-size: 13px; font-weight: 700; color: #6d28d9; }
  .ipsu-panel select { padding: 5px 7px; font-size: 11.5px; border: 1px solid var(--border-2);
                       border-radius: 6px; outline: none; background: #fff; }
  .ipsu-search { position: relative; }
  .ipsu-search input { width: 210px; padding: 5px 8px; font-size: 11.5px;
                       border: 1px solid var(--border-2); border-radius: 6px; outline: none; }
  .ipsu-sug { position: absolute; z-index: 40; top: 100%; left: 0; width: 340px; max-height: 210px;
              overflow-y: auto; background: #fff; border: 1px solid var(--border-2); border-radius: 8px;
              box-shadow: 0 8px 24px rgba(15,23,42,.14); display: none; }
  .ipsu-sug div { padding: 6px 9px; font-size: 11px; cursor: pointer; border-bottom: 1px solid var(--border); }
  .ipsu-sug div:hover { background: #f5f3ff; }
  .ipsu-sug .sg-c { font-weight: 700; color: #6d28d9; margin-right: 5px; }
  .ipsu-sug .sg-s { color: var(--text-3); font-size: 10px; }
  .ipsu-stages { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
  .ipsu-cv { width: 100%; height: 260px; background: #0f172a; border-radius: 9px;
             overflow: hidden; position: relative; cursor: grab; }
  .ipsu-cv:active { cursor: grabbing; }
  /* 3D 확대 보기 모달 — 캔버스를 통째로 옮겨와 크게 표시 */
  .ipsu-zoom-overlay { display: none; position: fixed; inset: 0; z-index: 130;
    background: rgba(10, 10, 16, 0.72); backdrop-filter: blur(3px);
    align-items: center; justify-content: center; padding: 24px; }
  .ipsu-zoom-overlay.show { display: flex; }
  .ipsu-zoom-box { width: min(1500px, 96vw); background: var(--card, #fff);
    border-radius: 14px; padding: 14px 18px 18px; box-shadow: 0 18px 60px rgba(0,0,0,.45); }
  .ipsu-zoom-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  .ipsu-zoom-head h3 { margin: 0; font-size: 15px; font-weight: 800; }
  .ipsu-zoom-head .zx { border: none; background: none; font-size: 26px; line-height: 1;
    cursor: pointer; color: var(--text-3, #94a3b8); padding: 0 4px; }
  .ipsu-zoom-body .ipsu-cv { height: min(78vh, 900px); }
  .ipsu-zoom-sub { margin-top: 8px; font-size: 12px; color: var(--text-2, #475569); }
  .ipsu-cv .cv-hint { position: absolute; bottom: 5px; left: 7px; font-size: 9.5px;
                      color: #94a3b8; pointer-events: none; }
  .ipsu-sh { margin: 0 0 5px; font-size: 12px; display: flex; justify-content: space-between; align-items: baseline; }
  .ipsu-sh .sc { font-size: 16px; font-weight: 800; color: #7c3aed; font-variant-numeric: tabular-nums; }
  .ipsu-sh .sc-wrap { display: flex; align-items: baseline; gap: 3px; white-space: nowrap; }
  .ipsu-sh .sc-lbl { font-size: 11px; font-weight: 700; color: var(--text-3); }
  .ipsu-sh .sc-unit { font-size: 11px; font-weight: 700; color: var(--text-3); }
  .ipsu-badge { font-size: 10.5px; color: var(--text-2); margin-top: 5px; }
  .ipsu-total { background: #7c3aed; color: #fff; border-radius: 9px; padding: 10px 14px;
                margin-top: 10px; display: flex; justify-content: space-between; align-items: center; }
  .ipsu-total .bg { font-size: 26px; font-weight: 800; font-variant-numeric: tabular-nums; }
  .ipsu-total .lb { font-size: 11.5px; opacity: .92; }
  .ipsu-total .wn { color: #fde68a; font-size: 10.5px; }
  @media (max-width: 900px) { .ipsu-stages { grid-template-columns: 1fr; } }
  /* ───── 매출 현황 + NPD ───── */
  .sales-npd-strip {
    max-width: 1440px; margin: 12px auto 0; padding: 0 32px;
    display: grid; grid-template-columns: 1.55fr 1fr; gap: 12px;
  }
  .chart-panel.sales-panel { border-top: 3px solid #0ea5e9; }
  .chart-panel.prod-panel { border-top: 3px solid #f59e0b; }
  .prod-chart-wrap { height: 240px; position: relative; }
  /* ───── 월 판매기반 자료 패널 ───── */
  .salesbase-strip { max-width: 1440px; margin: 12px auto 0; padding: 0 32px; }
  .retired { display: none !important; }   /* 제거한 영역 (JS가 style.display를 바꿔도 숨김 유지) */
  /* ───── 판매 분석 (2026-09-23) ───── */
  .lens-strip { max-width: 1440px; margin: 12px auto 0; padding: 0 32px; display: grid; grid-template-columns: 1.15fr 1.15fr 0.8fr; gap: 12px; }
  .chart-panel.lens-gap { border-top: 3px solid #0d9488; }
  .chart-panel.lens-price { border-top: 3px solid #9333ea; }
  .chart-panel.lens-wd { border-top: 3px solid #64748b; }
  .wd-row { display: grid; grid-template-columns: 22px 1fr 58px 44px; align-items: center; gap: 6px; font-size: 11.5px; padding: 4px 0; }
  .wd-bar { height: 14px; border-radius: 4px; background: #f1f5f9; overflow: hidden; display: flex; }
  .wd-bar i { display: block; height: 100%; }
  .wd-row .n { text-align: right; font-variant-numeric: tabular-nums; color: var(--text-2); }
  .wd-row .p { text-align: right; font-weight: 700; font-variant-numeric: tabular-nums; }
  @media (max-width: 900px) { .lens-strip { grid-template-columns: 1fr; padding: 0 12px; } }
  .chart-panel.salesbase-panel { border-top: 3px solid #3f9e8f; }
  .salesbase-body { display: grid; grid-template-columns: 1fr 1.7fr; gap: 16px; align-items: start; }
  .salesbase-chart-wrap { height: 460px; position: relative; min-width: 0; }
  /* 매출현황 자리(발주·입고 추이 옆)로 옮기면서 폭이 줄어 차트:목록 비율·높이 조정 (2026-09-23) */
  .sales-npd-strip .salesbase-body { grid-template-columns: 1.15fr 1fr; gap: 12px; }
  .sales-npd-strip .salesbase-chart-wrap { height: 400px; }
  .sales-npd-strip .sb-top-list { max-height: 330px; }
  .sales-npd-strip .prod-chart-wrap { height: 360px; }
  .salesbase-side { display: flex; flex-direction: column; min-width: 0; }
  .sb-side-title { font-size: 11px; font-weight: 700; color: var(--text-2); margin-bottom: 6px; }
  .sb-top-list { flex: 1; overflow-y: auto; max-height: 460px; margin: 0 -2px; }
  .sb-top-list::-webkit-scrollbar { width: 6px; }
  .sb-top-list::-webkit-scrollbar-thumb { background: rgba(15,23,42,0.12); border-radius: 6px; }
  .sb-top-row { display: flex; align-items: center; gap: 8px; padding: 7px 6px; border-bottom: 1px solid rgba(15,23,42,0.05); font-size: 12px; border-radius: 5px; transition: background 0.12s; cursor: pointer; }
  .sb-top-row:nth-child(even) { background: rgba(15,23,42,0.022); }
  .sb-top-row:hover { background: rgba(63,158,143,0.09); }
  .sb-top-rank { width: 20px; color: var(--text-3); font-weight: 800; text-align: center; flex: none; font-variant-numeric: tabular-nums; font-size: 12.5px; }
  .sb-top-row:nth-child(-n+3) .sb-top-rank { color: #3f9e8f; }
  .sb-top-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-2); }
  .sb-top-code { color: #45589f; font-weight: 700; font-variant-numeric: tabular-nums; }
  .sb-top-qty { width: 66px; flex: none; text-align: right; font-weight: 700; font-variant-numeric: tabular-nums; }
  .sb-top-qty.avg { color: var(--text-2); }
  .sb-top-qty.m1  { color: #2f8576; padding-left: 9px; border-left: 1px solid var(--border-2); }
  .sb-metric { width: 66px; flex: none; display: flex; flex-direction: column; align-items: flex-end; line-height: 1.28; }
  .sb-metric.m1 { padding-left: 9px; border-left: 1px solid var(--border-2); }
  .sb-metric b { font-weight: 700; font-variant-numeric: tabular-nums; font-size: 11.5px; }
  .sb-metric.avg b { color: var(--text-2); }
  .sb-metric.m1 b  { color: #2f8576; }
  .sb-metric i { font-style: normal; font-variant-numeric: tabular-nums; color: var(--text-3); font-size: 9.5px; }
  .sb-list-head { display: flex; align-items: center; gap: 8px; padding: 2px 6px 5px; font-size: 9.5px; font-weight: 700; color: var(--text-3); border-bottom: 1.5px solid var(--border-2); }
  .sb-lh-name { flex: 1; min-width: 0; padding-left: 28px; }
  .sb-lh-avg, .sb-lh-m1 { width: 66px; flex: none; text-align: right; }
  .sb-lh-m1 { padding-left: 9px; }
  .sb-lh-avg { color: var(--text-2); }
  .sb-lh-m1 { color: #2f8576; }
  .sb-cls { font-size: 9px; font-weight: 700; padding: 1px 5px; border-radius: 4px; flex: none; }
  .sb-cls.jasa  { background:#ebeef9; color:#45589f; }
  .sb-cls.sagup { background:#f7efda; color:#927029; }
  .sb-cls.saip  { background:#e4f1ed; color:#2f8576; }
  @media (max-width: 900px) { .salesbase-body { grid-template-columns: 1fr; } }
  .chart-nav { display: inline-flex; gap: 3px; }
  .chart-nav button {
    width: 24px; height: 24px; border: 1px solid var(--border-2);
    background: var(--surface, #fff); border-radius: 6px; cursor: pointer;
    font-size: 15px; line-height: 1; color: var(--text-2); font-weight: 700;
    display: flex; align-items: center; justify-content: center; padding: 0;
    transition: background 0.15s;
  }
  .chart-nav button:hover:not(:disabled) { background: var(--surface-2, #f1f5f9); color: var(--text); }
  .chart-nav button:disabled { opacity: 0.3; cursor: default; }
  .sales-body { display: flex; gap: 14px; align-items: stretch; }
  .sales-chart-wrap { flex: 1; min-width: 0; height: 240px; position: relative; }
  .sales-div-list {
    width: 168px; flex-shrink: 0; display: flex; flex-direction: column;
    gap: 5px; overflow-y: auto; max-height: 240px; padding-right: 2px;
  }
  .sales-div-row {
    display: flex; align-items: center; justify-content: space-between;
    font-size: 11px; padding: 5px 8px; border-radius: 7px; background: var(--surface-2);
  }
  .sales-div-row .dv-name { color: var(--text-2); font-weight: 600; }
  .sales-div-row .dv-amt { font-weight: 800; color: #0369a1; font-variant-numeric: tabular-nums; }
  @media (max-width: 900px) {
    .sales-npd-strip { grid-template-columns: 1fr; padding: 0 12px; }
    .sales-body { flex-direction: column; }
    .sales-div-list { width: 100%; flex-direction: row; flex-wrap: wrap; max-height: none; }
    .sales-div-row { flex: 1; min-width: 110px; }
  }
  .chart-panel.spec-panel { border-top: 3px solid #6366f1; }
  .spec-list { max-height: 310px; overflow-y: auto; margin: 0 -4px; padding-right: 4px; }
  .spec-list::-webkit-scrollbar { width: 6px; }
  .spec-list::-webkit-scrollbar-thumb { background: rgba(15,23,42,0.12); border-radius: 6px; }
  .spec-row {
    display: grid; grid-template-columns: 70px 1.4fr 60px 132px 1.1fr 28px 76px 76px 92px;
    align-items: center; gap: 8px;
    padding: 6px 8px; border-radius: 6px;
    font-size: 11px; line-height: 1.3;
  }
  .spec-row + .spec-row { border-top: 1px dashed rgba(15,23,42,0.05); }
  .spec-row:hover { background: var(--surface-2); }
  .spec-row.sc-linked { background: #eef2ff !important; }
  .spec-row .sc-code { font-weight: 700; color: #6366f1; letter-spacing: -0.01em; }
  .spec-row .sc-name { color: var(--text); font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .spec-row .sc-size { color: #0369a1; font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .spec-row .sc-mat { color: #0d9488; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 10.5px; letter-spacing: -0.01em; }
  .spec-row .sc-moq { color: #c2410c; font-weight: 600; font-variant-numeric: tabular-nums; text-align: right; }
  .spec-row .sc-price { color: #b91c1c; font-weight: 700; font-variant-numeric: tabular-nums; text-align: right; }
  .spec-row .sc-vendor {
    display: inline-block; font-size: 10px; font-weight: 600;
    background: #eef2ff; color: #4338ca; padding: 2px 7px; border-radius: 8px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%;
  }
  .spec-row .sc-cat-pouch { background: #ecfdf5; color: #047857; }
  .spec-row .sc-cat-단상자 { background: #fef3c7; color: #92400e; }
  .spec-row .sc-cat-rrp { background: #fee2e2; color: #b91c1c; }
  .spec-row .sc-div {
    display: inline-block; font-size: 9.5px; font-weight: 700;
    padding: 2px 6px; border-radius: 6px; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; max-width: 100%;
    background: #f1f5f9; color: #475569;
  }
  .sc-div-쿠팡 { background: #fff7ed !important; color: #c2410c !important; }
  .sc-div-홈플러스 { background: #ede9fe !important; color: #6d28d9 !important; }
  .sc-div-롯데 { background: #fef2f2 !important; color: #b91c1c !important; }
  .sc-div-이마트 { background: #fef3c7 !important; color: #92400e !important; }
  .sc-div-3P { background: #dbeafe !important; color: #1d4ed8 !important; }
  .sc-div-공용 { background: #f1f5f9 !important; color: #475569 !important; }
  .spec-row .sc-check { display: flex; align-items: center; justify-content: center; }
  .spec-row .sc-check input[type=checkbox] { width: 14px; height: 14px; cursor: pointer; accent-color: #6366f1; }
  .spec-head {
    display: grid; grid-template-columns: 70px 1.4fr 60px 132px 1.1fr 28px 76px 76px 92px;
    gap: 8px; padding: 4px 8px;
    font-size: 10px; font-weight: 700; color: var(--text-3);
    border-bottom: 1px solid var(--border); margin-bottom: 4px;
    position: sticky; top: 0; z-index: 2;
    background: var(--surface, #fff);
    box-shadow: 0 1px 0 var(--border);
  }
  .spec-head .h-moq, .spec-head .h-price { text-align: right; }
  .spec-head .h-check { text-align: center; color: #6366f1; font-size: 12px; cursor: default; }
  .chart-panel {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 12px 16px 10px;
    box-shadow: var(--shadow-sm);
  }
  .chart-panel.scope-jasa     { border-top: 3px solid #4f46e5; }
  .chart-panel.scope-outsource { border-top: 3px solid #f59e0b; }
  .chart-panel.reorder-panel  { border-top: 3px solid #e11d48; }   /* 발주 타이밍 — 로즈(긴급 액션) */
  .chart-panel.price-span     { border-top: 3px solid #0d9488; }   /* 단가 변동 — 틸(가격/재무) */
  .chart-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
  .chart-title { font-size: 12.5px; font-weight: 700; color: var(--text); letter-spacing: -0.01em; }
  .chart-sub { font-size: 10.5px; color: var(--text-3); font-weight: 500; margin-left: 6px; }
  .kpi-row { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 8px; }
  .kpi-chip {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 3px 9px; border-radius: 999px; font-size: 11px; line-height: 1.4;
    background: color-mix(in srgb, var(--c) 9%, transparent);
    border: 1px solid color-mix(in srgb, var(--c) 22%, transparent);
    color: var(--text-2);
  }
  .kpi-chip .kpi-label { color: var(--text-3); font-weight: 600; }
  .kpi-chip .kpi-val { color: var(--c); font-weight: 800; font-variant-numeric: tabular-nums; }
  .kpi-chip .kpi-live {
    font-size: 9px; font-weight: 700; color: #dc2626;
    background: #fee2e2; padding: 1px 5px; border-radius: 999px;
  }
  .chart-wrap { position: relative; height: 200px; }

  /* ───── Stock Alert List ───── */
  .alert-list { max-height: 310px; overflow-y: auto; margin: 0 -8px; }
  .alert-list::-webkit-scrollbar { width: 6px; }
  .alert-list::-webkit-scrollbar-thumb { background: rgba(15,23,42,0.12); border-radius: 6px; }
  .alert-row {
    display: grid;
    grid-template-columns: 74px 1fr auto auto;
    align-items: center; gap: 10px;
    padding: 8px 10px; border-radius: 8px;
    cursor: pointer; transition: background 0.12s;
    font-size: 12px;
  }
  .alert-row + .alert-row { border-top: 1px dashed rgba(15,23,42,0.05); }
  .alert-row:hover { background: var(--surface-2); }
  .alert-badge {
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 10px; font-weight: 700; padding: 2px 6px;
    border-radius: 5px; letter-spacing: -0.01em; white-space: nowrap;
  }
  /* 신호등 그라데이션: 품절(빨강) → 위험(주황) → 주의(노랑) → 관찰(초록) */
  .alert-badge.out      { background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }
  .alert-badge.critical { background: #fff7ed; color: #ea580c; border: 1px solid #fed7aa; }
  .alert-badge.warning  { background: #fefce8; color: #ca8a04; border: 1px solid #fde68a; }
  .alert-badge.low      { background: #f0fdf4; color: #16a34a; border: 1px solid #bbf7d0; }
  .alert-name { font-weight: 600; color: var(--text); font-size: 12px; }
  .alert-vendor { font-size: 10.5px; color: #c2410c; font-weight: 500; }
  .alert-code { font-size: 10.5px; color: var(--text-3); font-weight: 500; margin-top: 1px; }
  .alert-days { font-weight: 700; font-variant-numeric: tabular-nums; }
  .alert-days.out      { color: #dc2626; }
  .alert-days.critical { color: #ea580c; }
  .alert-days.warning  { color: #ca8a04; }
  .alert-days.low      { color: #16a34a; }
  .alert-qty { font-size: 11px; color: var(--text-2); font-variant-numeric: tabular-nums; text-align: right; }
  .alert-empty { padding: 24px; text-align: center; color: var(--text-3); font-size: 12px; }

  /* ───── Price Calculator ───── */
  .chart-panel.pcalc-panel { border-top: 3px solid #0ea5e9; }
  .pcalc-cat-tabs {
    display: flex; gap: 4px; margin-bottom: 6px;
  }
  .pcalc-cat-tab {
    font-size: 11px; font-weight: 600; padding: 5px 12px;
    border: 1px solid var(--border-2); border-radius: 14px;
    background: white; color: var(--text-2); cursor: pointer;
    transition: all 0.12s;
  }
  .pcalc-cat-tab:hover { border-color: #0ea5e9; color: #0369a1; }
  .pcalc-cat-tab.active { background: #0ea5e9; color: white; border-color: #0ea5e9; }
  .pcalc-selected-bar {
    display: flex; flex-wrap: wrap; gap: 4px;
    padding: 6px 8px; margin-bottom: 6px;
    border: 1px dashed var(--border-2); border-radius: 8px;
    background: linear-gradient(135deg, #f8fafc, #f1f5f9);
    min-height: 32px; max-height: 60px; overflow-y: auto;
    align-items: center;
  }
  .pcalc-selected-bar:empty::before {
    content: '선택한 재질이 여기에 표시됩니다';
    font-size: 10.5px; color: var(--text-3); font-style: italic;
  }
  .pcalc-sel-chip {
    display: inline-flex; align-items: center; gap: 4px;
    background: #0ea5e9; color: white;
    font-size: 11.5px; font-weight: 600;
    padding: 3px 4px 3px 9px; border-radius: 14px;
    line-height: 1.3;
  }
  .pcalc-sel-x {
    display: inline-flex; align-items: center; justify-content: center;
    width: 18px; height: 18px; border: none; cursor: pointer;
    background: rgba(255,255,255,0.25); color: white;
    border-radius: 50%; font-size: 13px; font-weight: 700;
    padding: 0; line-height: 1;
    transition: background 0.12s;
  }
  .pcalc-sel-x:hover { background: rgba(255,255,255,0.5); }

  .pcalc-item {
    display: grid; grid-template-columns: 18px 1fr auto;
    align-items: center; gap: 10px;
    padding: 6px 10px; border-radius: 6px;
    cursor: pointer; font-size: 12px;
    transition: background 0.12s;
  }
  .pcalc-item:hover { background: var(--surface-2); }
  .pcalc-item input[type="checkbox"] { width: 14px; height: 14px; accent-color: #0ea5e9; cursor: pointer; }
  .pcalc-name { font-weight: 500; color: var(--text); }
  .pcalc-price { font-size: 11.5px; font-weight: 700; color: #0c4a6e; font-variant-numeric: tabular-nums; white-space: nowrap; }

  .pcalc-sim {
    padding: 6px 8px; border-radius: 6px;
    cursor: pointer; transition: background 0.12s;
    font-size: 11.5px;
  }
  .pcalc-sim + .pcalc-sim { border-top: 1px dashed rgba(15,23,42,0.06); }
  .pcalc-sim:hover { background: var(--surface-2); }
  .pcalc-sim-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  .pcalc-sim-code { font-size: 10.5px; font-weight: 700; color: #4f46e5; letter-spacing: -0.01em; }
  .pcalc-sim-price { font-size: 12px; font-weight: 800; color: var(--text); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .pcalc-sim-name { font-size: 11px; color: var(--text); margin-top: 2px; line-height: 1.3; }
  .pcalc-sim-meta { font-size: 10.5px; color: #0369a1; font-weight: 600; margin-top: 3px; letter-spacing: -0.01em; }
  .pcalc-sim-size.clickable { cursor: pointer; text-decoration: underline; text-decoration-style: dotted; text-underline-offset: 2px; }
  .pcalc-sim-size.clickable:hover { color: #0c4a6e; background: rgba(14,165,233,0.08); border-radius: 3px; padding: 0 2px; }
  .pcalc-sim-mats {
    display: flex; flex-wrap: wrap; gap: 3px; margin-top: 6px;
    padding: 6px 8px; background: #fafbfd; border-radius: 6px;
  }
  .pcalc-mat-chip {
    font-size: 10px; padding: 2px 6px; border-radius: 10px;
    background: #e2e8f0; color: #475569; font-weight: 500;
  }
  .pcalc-mat-chip.match { background: #0ea5e9; color: white; font-weight: 600; }
  .pcalc-mat-chip.clickable { cursor: pointer; transition: filter 0.12s, transform 0.08s; user-select: none; }
  .pcalc-mat-chip.clickable:hover { filter: brightness(0.92); transform: translateY(-1px); }
  .pcalc-mat-chip.clickable:active { transform: translateY(0); }
  .pcalc-sim-empty { padding: 14px; text-align: center; color: var(--text-3); font-size: 11px; }

  /* ───── PO Pending Inline List ───── */
  .po-inline-search {
    width: 100%; padding: 6px 10px; margin-bottom: 6px;
    font-size: 11.5px; border: 1px solid var(--border-2);
    border-radius: 6px; outline: none;
  }
  .po-inline-list { max-height: 260px; overflow-y: auto; margin: 0 -4px; }
  .po-inline-list::-webkit-scrollbar { width: 6px; }
  .po-inline-list::-webkit-scrollbar-thumb { background: rgba(15,23,42,0.12); border-radius: 6px; }
  .po-inline-row {
    display: grid; grid-template-columns: 70px 1fr auto auto auto auto;
    align-items: center; gap: 8px;
    padding: 7px 8px; border-radius: 7px;
    cursor: pointer; font-size: 11.5px;
    transition: background 0.12s;
  }
  .po-inline-stock {
    display: inline-block; font-size: 10.5px; font-weight: 700;
    padding: 2px 8px; border-radius: 10px;
    background: #e0f2fe; color: #075985; border: 1px solid #bae6fd;
    letter-spacing: -0.01em; font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  .po-inline-remain {
    display: inline-block; font-size: 10.5px; font-weight: 700;
    padding: 2px 8px; border-radius: 10px;
    background: #fef3c7; color: #92400e; border: 1px solid #fde68a;
    letter-spacing: -0.01em; font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  .po-inline-qty {
    display: inline-block; font-size: 10.5px; font-weight: 700;
    padding: 2px 8px; border-radius: 10px;
    background: #f1f5f9; color: #334155; letter-spacing: -0.01em;
    font-variant-numeric: tabular-nums; white-space: nowrap;
    max-width: 110px; overflow: hidden; text-overflow: ellipsis;
  }
  .po-inline-dest {
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    font-size: 10.5px; font-weight: 600;
    padding: 3px 8px; border-radius: 10px;
    background: #e2e8f0; color: #475569; letter-spacing: -0.01em;
    max-width: 240px; line-height: 1.35; cursor: pointer;
    white-space: normal; word-break: keep-all; overflow-wrap: anywhere;
    overflow: hidden; text-overflow: ellipsis; transition: none;
  }
  .po-inline-dest.expanded { -webkit-line-clamp: unset; }
  .po-inline-row + .po-inline-row { border-top: 1px dashed rgba(15,23,42,0.06); }
  .po-inline-row:hover { background: var(--surface-2); }
  .po-inline-code { font-weight: 700; color: #059669; font-size: 11px; letter-spacing: -0.01em; }
  .po-inline-row.raw .po-inline-code { color: #7c3aed; }
  .po-inline-row.raw .po-inline-chip { background: #7c3aed; }
  .po-inline-row.import-raw .po-inline-code { color: #0369a1; }
  .po-inline-row.import-raw .po-inline-chip { background: #0369a1; }
  .po-inline-row.os .po-inline-code { color: #dc2626; }
  .spec-doc-btn {
    display: inline-flex; align-items: center; gap: 3px;
    font-size: 10px; font-weight: 700; color: #0369a1;
    background: #e0f2fe; border: 1px solid #bae6fd; cursor: pointer;
    padding: 3px 8px; border-radius: 7px; white-space: nowrap;
    transition: background 0.15s;
  }
  .spec-doc-btn:hover { background: #bae6fd; }
  /* 시방서 미리보기/다운로드 팝오버 메뉴 */
  .spec-menu {
    position: fixed; z-index: 200; background: var(--surface, #fff);
    border: 1px solid var(--border-2); border-radius: 10px;
    box-shadow: 0 8px 24px rgba(15,23,42,0.16); padding: 6px;
    display: flex; flex-direction: column; gap: 2px; min-width: 150px;
  }
  .spec-menu button {
    display: flex; align-items: center; gap: 8px; width: 100%;
    background: none; border: none; cursor: pointer; text-align: left;
    padding: 8px 10px; border-radius: 7px; font-size: 12px; font-weight: 600;
    color: var(--text);
  }
  .spec-menu button:hover { background: var(--surface-2, #f1f5f9); }
  .spec-menu button .ico { font-size: 14px; }
  /* 시방서 미리보기 모달 */
  #spec-preview-modal .modal {
    max-width: none; width: 97vw; height: 95vh; display: flex; flex-direction: column;
  }
  #spec-preview-modal .modal-header { padding-bottom: 10px; }
  #spec-preview-modal iframe {
    flex: 1; width: 100%; border: none; border-radius: 0 0 14px 14px; background: #f8fafc;
  }
  #spec-preview-modal img { max-width: 100%; max-height: 100%; object-fit: contain; margin: auto; }
  .po-inline-name { font-size: 11px; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .po-inline-sub { font-size: 10px; color: var(--text-3); margin-top: 1px; }
  .po-inline-chip {
    display: inline-block; font-size: 11px; font-weight: 700;
    padding: 2px 8px; border-radius: 10px;
    background: #dc2626; color: white; letter-spacing: -0.01em;
    font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  /* '미정'·'보류' 배지는 행 종류(raw/import-raw) 배경색 덮어쓰기보다 우선 → 가독성 확보 */
  .po-inline-row .po-inline-chip.empty { background: #fee2e2; color: #b91c1c; border: 1px solid #fca5a5; }
  .po-inline-row .po-inline-chip.hold { background: #ffedd5; color: #c2410c; border: 1px solid #fdba74; }

  /* ───── PO Pending Modal ───── */
  .po-row {
    display: grid; grid-template-columns: 80px 1fr 80px 100px 110px 90px 1.4fr;
    align-items: start; gap: 10px;
    padding: 10px 12px; border-bottom: 1px solid var(--border);
    font-size: 12px;
  }
  .po-row.head {
    font-size: 10.5px; font-weight: 700; color: var(--text-3);
    text-transform: uppercase; letter-spacing: 0.04em;
    border-bottom: 1px solid var(--border-2); background: var(--surface-2);
    position: sticky; top: 0;
  }
  /* 외주 입고 모달 — 시방서 컬럼 추가로 8컬럼 */
  .po-row.os-modal-row {
    grid-template-columns: 70px 1fr 78px 92px 90px 80px 72px 1.3fr;
  }
  .po-code { font-weight: 700; color: #059669; }
  .po-name { color: var(--text); font-weight: 500; line-height: 1.35; }
  .po-qty { font-variant-numeric: tabular-nums; font-weight: 700; text-align: right; }
  .po-date { color: var(--text-2); font-variant-numeric: tabular-nums; }
  .po-vendor { font-size: 11px; color: var(--text-2); }
  .po-schedule {
    font-size: 11px; color: var(--text); line-height: 1.4;
    padding: 6px 8px; background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 6px;
    display: flex; flex-wrap: wrap; gap: 4px; align-content: flex-start;
  }
  .po-sch-chip {
    display: inline-block; font-size: 11px; font-weight: 700;
    padding: 2px 8px; border-radius: 10px;
    background: #dc2626; color: white; letter-spacing: -0.01em;
    font-variant-numeric: tabular-nums;
  }
  .po-sch-meta { font-size: 10px; color: #065f46; width: 100%; margin-top: 2px; }
  .po-schedule[onclick]:hover { filter: brightness(0.97); box-shadow: 0 0 0 1.5px #10b981 inset; }
  .po-sch-bubble { font-size: 11px; opacity: 0.7; margin-left: 2px; }
  /* 말풍선 팝업 */
  .po-bubble {
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px;
    padding: 8px 11px; margin-bottom: 8px;
  }
  .po-bubble.mine { background: #ecfdf5; border-color: #a7f3d0; }
  .po-bubble-head { display: flex; justify-content: space-between; align-items: center; font-size: 11px; color: var(--text-2); margin-bottom: 4px; }
  .po-bubble-head b { color: var(--text); font-weight: 700; }
  .po-bubble-body { font-size: 12.5px; color: var(--text); line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
  .po-schedule.empty {
    background: #fef2f2; border-color: #fecaca; color: #b91c1c;
    font-weight: 700; text-align: center;
    display: flex; align-items: center; justify-content: center;
    min-height: 28px;
  }
  .po-schedule.hold {
    background: #fff7ed; border: 1px solid #fed7aa; color: #c2410c;
    font-weight: 700; text-align: center;
    display: flex; align-items: center; justify-content: center;
    min-height: 28px; padding: 6px 8px; border-radius: 6px;
  }

  @media (max-width: 980px) {
    .chart-grid { grid-template-columns: 1fr; padding: 0 16px; }
    .chart-panel.po-inline, .chart-panel.os-inline, .chart-panel.pcalc-panel, .chart-panel.alert-span, .chart-panel.price-span, .chart-panel.plan-panel, .chart-panel.chstock-panel, .chart-panel.vendor-panel { grid-column: auto; }
  }

  /* ───── Header ───── */
  header {
    position: sticky; top: 0; z-index: 50;
    background:
      radial-gradient(135% 200% at 0% 0%, rgba(99,102,241,0.20) 0%, transparent 55%),
      radial-gradient(120% 180% at 100% 0%, rgba(168,85,247,0.14) 0%, transparent 52%),
      linear-gradient(118deg, rgba(22,26,46,0.94) 0%, rgba(13,17,30,0.93) 52%, rgba(8,11,20,0.95) 100%);
    backdrop-filter: saturate(180%) blur(24px);
    -webkit-backdrop-filter: saturate(180%) blur(24px);
    color: white; padding: 16px 36px;
    display: flex; justify-content: space-between; align-items: center;
    border-bottom: 1px solid rgba(255,255,255,0.09);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.08), 0 12px 34px -14px rgba(0,0,0,0.7);
  }
  header::after {
    content: ''; position: absolute; left: 0; right: 0; bottom: -1px; height: 1px;
    background: linear-gradient(90deg, transparent 0%, rgba(129,140,248,0.55) 28%, rgba(168,85,247,0.45) 72%, transparent 100%);
    pointer-events: none;
  }
  header h1 {
    font-size: 17px; font-weight: 700; letter-spacing: -0.02em;
    display: flex; align-items: center; gap: 12px;
    background: linear-gradient(135deg,#fff 0%,#c7d2fe 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  header h1::before {
    content:''; width:30px; height:30px; border-radius:9px;
    background: linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
    box-shadow: 0 6px 20px rgba(99,102,241,0.5), inset 0 1px 0 rgba(255,255,255,0.3);
    flex-shrink: 0;
  }
  header .subtitle {
    font-size: 10.5px; opacity: 0.5; margin-top: 3px;
    margin-left: 42px; letter-spacing: 0.03em; font-weight: 400;
    color: rgba(255,255,255,0.9);
  }
  .nav-links { display: flex; gap: 2px; flex-wrap: wrap; justify-content: flex-end; align-items: center; }
  .nav-links a {
    color: rgba(255,255,255,0.65); font-size: 12px;
    padding: 7px 13px; border-radius: 8px;
    text-decoration: none; transition: all 0.15s;
    white-space: nowrap; font-weight: 500;
  }
  .nav-links a:hover { background: rgba(255,255,255,0.08); color: white; }
  .nav-refresh-btn {
    display: inline-flex; align-items: center; gap: 5px;
    background: rgba(34,197,94,0.15); color: #86efac;
    border: 1px solid rgba(34,197,94,0.35);
    padding: 5px 11px; border-radius: 8px; font-size: 11.5px; font-weight: 600;
    cursor: pointer; transition: all 0.15s; white-space: nowrap;
    font-family: inherit;
  }
  .nav-refresh-btn:hover:not(:disabled) { background: rgba(34,197,94,0.25); color: #bbf7d0; }
  .nav-refresh-btn:disabled { opacity: 0.6; cursor: not-allowed; }
  .nav-refresh-btn.refreshing #refresh-icon {
    display: inline-block; animation: spin 1s linear infinite;
  }
  .nav-refresh-btn.success { background: rgba(34,197,94,0.25); color: #bbf7d0; }
  .nav-refresh-btn.error { background: rgba(239,68,68,0.18); color: #fca5a5; border-color: rgba(239,68,68,0.4); }
  .nav-refresh-amber {
    background: rgba(251,191,36,0.15); color: #fcd34d;
    border-color: rgba(251,191,36,0.35);
  }
  .nav-refresh-amber:hover:not(:disabled) { background: rgba(251,191,36,0.25); color: #fde68a; }
  .nav-refresh-amber.refreshing #refresh-aramanth-icon { display: inline-block; animation: spin 1s linear infinite; }
  @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

  /* ───── Layout ───── */
  .wrap { max-width: 1440px; margin: 0 auto; padding: 32px 32px 140px; display: grid; grid-template-columns: 1fr; gap: 24px; }
  .main-col { min-width: 0; }
  /* 캘린더 가로 스트립 (검색창 위) */
  .cal-strip { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 20px; }
  @media (max-width: 1024px) {
    .cal-strip { grid-template-columns: repeat(2, 1fr); }
  }
  @media (max-width: 560px) {
    .cal-strip { grid-template-columns: 1fr; }
  }

  /* ───── Calendar Widget ───── */
  .cal-widget { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 10px 12px; box-shadow: var(--shadow-xs); }
  .cal-widget .cal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
  .cal-widget .cal-title { font-size: 11px; font-weight: 700; color: var(--text); letter-spacing: 0.04em; text-transform: uppercase; display: flex; align-items: center; gap: 6px; }
  .cal-widget .cal-title .dot { width: 8px; height: 8px; border-radius: 50%; }
  .cal-widget.t-plan .dot { background: #4f46e5; }
  .cal-widget.t-actual .dot { background: #059669; }
  .cal-widget.t-outsource .dot { background: #d97706; }
  .cal-widget.t-order .dot { background: #db2777; }
  .cal-widget .cal-nav { display: flex; gap: 2px; align-items: center; }
  .cal-widget .cal-nav button { background: transparent; border: none; width: 22px; height: 22px; border-radius: 5px; cursor: pointer; color: var(--text-2); font-size: 14px; line-height: 1; transition: all 0.1s; }
  .cal-widget .cal-nav button:hover { background: #f1f5f9; color: var(--text); }
  .cal-widget .cal-ym { font-size: 11px; color: var(--text-2); font-weight: 600; min-width: 66px; text-align: center; font-variant-numeric: tabular-nums; }
  .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 1px; }
  .cal-grid .dow { font-size: 9px; text-align: center; padding: 3px 0; color: var(--text-3); font-weight: 600; }
  .cal-grid .dow.sun { color: #dc2626; }
  .cal-grid .dow.sat { color: #2563eb; }
  .cal-cell { aspect-ratio: 1; font-size: 11px; display: flex; align-items: center; justify-content: center; border-radius: 5px; cursor: pointer; position: relative; color: var(--text-2); font-variant-numeric: tabular-nums; transition: all 0.1s; }
  .cal-cell.empty { cursor: default; color: transparent; }
  .cal-cell.has-data { font-weight: 700; color: var(--text); }
  .cal-cell.has-data::after { content: ''; position: absolute; bottom: 2px; left: 50%; transform: translateX(-50%); width: 4px; height: 4px; border-radius: 50%; }
  .cal-widget.t-plan .cal-cell.has-data::after { background: #4f46e5; }
  .cal-widget.t-actual .cal-cell.has-data::after { background: #059669; }
  .cal-widget.t-outsource .cal-cell.has-data::after { background: #d97706; }
  .cal-widget.t-order .cal-cell.has-data::after { background: #db2777; }
  .cal-cell:not(.empty):hover { background: #f1f5f9; }
  .cal-cell.today { background: var(--brand-soft); color: var(--brand); font-weight: 700; }
  .cal-cell.selected { background: var(--brand); color: white; }
  .cal-cell.selected::after { background: white !important; }
  .cal-cell.sun { color: #dc2626; }
  .cal-cell.sat { color: #2563eb; }
  .cal-total { text-align: right; font-size: 10px; color: var(--text-3); margin-top: 4px; font-weight: 500; }

  /* ───── 입고 캘린더 (모던) ───── */
  .po-cal-btn { display: inline-flex; align-items: center; gap: 5px; padding: 6px 13px; font-size: 11.5px; font-weight: 700;
    color: var(--brand); background: linear-gradient(135deg,#eef2ff,#e0e7ff); border: 1px solid #c7d2fe; border-radius: 10px;
    cursor: pointer; white-space: nowrap; transition: all 0.16s cubic-bezier(.4,0,.2,1); letter-spacing: -0.01em; box-shadow: var(--shadow-xs); }
  .po-cal-btn:hover { background: linear-gradient(135deg,var(--brand),#6366f1); color: #fff; border-color: transparent;
    transform: translateY(-1px); box-shadow: var(--shadow-md); }
  .pocal-search-wrap { flex: 1; display: flex; align-items: center; gap: 9px; max-width: 560px; margin: 0 24px;
    background: #f8fafc; border: 1px solid var(--border-2); border-radius: 12px; padding: 0 16px; height: 46px;
    transition: all 0.16s; }
  .pocal-search-wrap:focus-within { background: #fff; border-color: var(--brand); box-shadow: 0 0 0 4px rgba(79,70,229,0.1); }
  .pocal-search-ico { font-size: 14px; opacity: 0.5; }
  .pocal-search-wrap input { flex: 1; border: none; background: transparent; outline: none; font-size: 14px;
    color: var(--text); font-family: inherit; }
  #pocal-search-hit, #oscal-search-hit { font-size: 12px; font-weight: 800; color: var(--brand); white-space: nowrap;
    background: var(--brand-soft); padding: 3px 9px; border-radius: 999px; }
  #pocal-search-hit:empty, #oscal-search-hit:empty { display: none; }
  .pocal-wrap { display: grid; grid-template-columns: 1.45fr 1fr; gap: 24px; }
  @media (max-width: 760px) { .pocal-wrap { grid-template-columns: 1fr; } }

  .pocal-cal { user-select: none; background: linear-gradient(180deg,#ffffff,#fbfcfe);
    border: 1px solid var(--border); border-radius: 20px; padding: 20px 20px 22px; box-shadow: var(--shadow-sm); }
  .pocal-nav { display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; }
  .pocal-nav .pocal-ym { font-size: 21px; font-weight: 800; color: var(--text); letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
  .pocal-nav button { background: #fff; border: 1px solid var(--border-2); width: 38px; height: 38px; border-radius: 50%;
    cursor: pointer; color: var(--text-2); font-size: 18px; line-height: 1; display: flex; align-items: center; justify-content: center;
    transition: all 0.16s; box-shadow: var(--shadow-xs); }
  .pocal-nav button:hover { background: var(--brand); border-color: var(--brand); color: #fff; transform: translateY(-1px); box-shadow: var(--shadow-md); }
  .pocal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }
  .pocal-grid .dow { font-size: 11px; text-align: center; padding: 2px 0 9px; color: var(--text-3); font-weight: 700; letter-spacing: 0.03em; }
  .pocal-grid .dow.sun { color: #ef4444; } .pocal-grid .dow.sat { color: #3b82f6; }
  .pocal-cell { aspect-ratio: 1; border-radius: 14px; cursor: pointer; position: relative; padding: 9px 0 0 11px;
    border: 1px solid transparent; background: #fff; font-variant-numeric: tabular-nums; min-height: 66px;
    transition: transform 0.16s cubic-bezier(.4,0,.2,1), box-shadow 0.16s, background 0.16s, border-color 0.16s; }
  .pocal-cell .dnum { font-size: 15px; color: var(--text-2); font-weight: 600; letter-spacing: -0.01em;
    display: inline-flex; align-items: center; justify-content: center; min-width: 24px; height: 24px; }
  .pocal-cell.empty { background: transparent; cursor: default; }
  .pocal-cell.sun .dnum { color: #ef4444; } .pocal-cell.sat .dnum { color: #3b82f6; }
  /* 일정 없는 날은 클릭 핸들러가 없으므로 클릭 가능한 것처럼 보이지 않게 (커서·호버 제거) */
  .pocal-cell:not(.has-data) { cursor: default; }
  .pocal-cell.has-data { background: linear-gradient(160deg,#eff6ff,#f0f9ff); border-color: #dbeafe; box-shadow: var(--shadow-xs); }
  .pocal-cell.has-data:hover { transform: translateY(-2px); box-shadow: var(--shadow-md); border-color: #93c5fd; }
  .pocal-cell.today .dnum { background: var(--brand); color: #fff !important; border-radius: 50%; font-weight: 700; box-shadow: 0 2px 6px rgba(79,70,229,0.4); }
  .pocal-cell.selected { background: linear-gradient(160deg,var(--brand),#6366f1); border-color: transparent;
    box-shadow: 0 10px 22px -8px rgba(79,70,229,0.55); transform: translateY(-2px); }
  .pocal-cell.selected .dnum { color: #fff !important; background: transparent; box-shadow: none; }
  .pocal-dots { display: flex; gap: 3px; flex-wrap: wrap; margin-top: 6px; padding-right: 6px; }
  .pocal-dot { width: 7px; height: 7px; border-radius: 50%; }
  .pocal-dot.po, .pocal-dot.os-done { background: #2563eb; } .pocal-dot.raw { background: #059669; }
  .pocal-dot.import_raw, .pocal-dot.os-req { background: #d97706; }
  .pocal-cell.selected .pocal-dot { box-shadow: 0 0 0 1.5px rgba(255,255,255,0.7); }
  .pocal-cnt { position: absolute; right: 7px; bottom: 7px; min-width: 18px; height: 18px; padding: 0 5px;
    display: inline-flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 800;
    color: #1d4ed8; background: rgba(37,99,235,0.12); border-radius: 9px; }
  .pocal-cell.selected .pocal-cnt { color: var(--brand); background: rgba(255,255,255,0.92); }
  .pocal-legend { display: flex; gap: 8px; margin-top: 18px; flex-wrap: wrap; }
  .pocal-legend span { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--text-2);
    font-weight: 600; background: #f8fafc; border: 1px solid var(--border); padding: 4px 11px; border-radius: 999px; }

  /* 미니 캘린더 (전월·다음월) */
  .pocal-mini { display: flex; gap: 14px; margin-top: 16px; padding-top: 15px; border-top: 1px dashed var(--border); }
  .pmini { flex: 1; background: #f8fafc; border: 1px solid var(--border); border-radius: 12px; padding: 10px 11px 11px; }
  .pmini-ym { font-size: 11.5px; font-weight: 700; color: var(--text-2); text-align: center; margin-bottom: 8px; letter-spacing: 0.02em; }
  .pmini-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 1px; }
  .pmd-dow { font-size: 9.5px; text-align: center; color: var(--text-3); font-weight: 700; padding: 1px 0 3px; }
  .pmd-dow.sun { color: #ef4444; } .pmd-dow.sat { color: #3b82f6; }
  .pmd-cell { font-size: 11px; text-align: center; color: var(--text-2); padding: 2.5px 0; font-variant-numeric: tabular-nums; }
  .pmd-cell.empty { color: transparent; }
  .pmd-cell.sun { color: #ef4444; } .pmd-cell.sat { color: #3b82f6; }
  @media (max-width: 760px) { .pocal-mini { gap: 10px; } }

  .pocal-detail { max-height: 604px; overflow-y: auto; padding: 2px 6px 2px 2px; }
  .pocal-detail::-webkit-scrollbar { width: 6px; }
  .pocal-detail::-webkit-scrollbar-thumb { background: var(--border-2); border-radius: 3px; }
  .pocal-detail::-webkit-scrollbar-thumb:hover { background: var(--text-3); }
  @media (max-width: 760px) { .pocal-detail { border-top: 1px solid var(--border); padding-top: 12px; } }
  .pocal-detail h3 { font-size: 14px; font-weight: 800; color: var(--text); margin: 0 0 14px; letter-spacing: -0.01em;
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .pocal-dgroup { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 9px; font-size: 12.5px;
    font-weight: 800; color: var(--text); background: rgba(248,250,252,0.96); -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px);
    border: 1px solid var(--border); border-radius: 11px; padding: 8px 12px; margin: 16px 0 7px; cursor: pointer; transition: all 0.14s; }
  .pocal-dgroup::before { content: ''; width: 4px; height: 15px; border-radius: 2px; background: var(--brand); }
  .pocal-dgroup:first-child { margin-top: 0; }
  .pocal-dgroup:hover { border-color: #c7d2fe; background: #eef2ff; }
  .pocal-dgroup .dgw { font-weight: 600; color: var(--text-3); }
  .pocal-dgroup .dgn { margin-left: auto; font-size: 11px; font-weight: 800; color: var(--brand); background: var(--brand-soft);
    padding: 2px 9px; border-radius: 999px; }
  .pocal-clear { font-size: 11px; color: var(--brand); cursor: pointer; font-weight: 700; background: var(--brand-soft);
    padding: 3px 9px; border-radius: 999px; transition: all 0.12s; }
  .pocal-clear:hover { background: var(--brand); color: #fff; }
  .pocal-drow { display: flex; align-items: center; gap: 10px; padding: 10px 11px; border-radius: 11px; margin-bottom: 2px;
    border: 1px solid transparent; transition: all 0.12s; }
  .pocal-drow:hover { background: #f8fafc; border-color: var(--border); }
  .pocal-drow .tag { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .pocal-drow .tag.po, .pocal-drow .tag.os-done { background: #2563eb; } .pocal-drow .tag.raw { background: #059669; }
  .pocal-drow .tag.import_raw, .pocal-drow .tag.os-req { background: #d97706; }
  .pocal-drow .dcode { font-size: 11px; font-weight: 800; color: var(--brand); background: var(--brand-soft);
    padding: 4px 8px; border-radius: 8px; min-width: 46px; text-align: center; letter-spacing: 0.01em; }
  .pocal-drow .dname { font-size: 12.5px; color: var(--text); flex: 1; line-height: 1.35; font-weight: 500; }
  .pocal-drow .dsub { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    font-size: 10.5px; color: var(--text-3); margin-top: 2px; font-weight: 400; cursor: pointer;
    word-break: keep-all; overflow-wrap: anywhere; }
  .pocal-drow .dsub.expanded { -webkit-line-clamp: unset; }
  .pocal-drow .dqty { font-size: 11px; color: var(--text-2); font-weight: 700; white-space: nowrap; background: #f1f5f9;
    padding: 4px 9px; border-radius: 8px; }
  .pocal-drow .dqty:empty { display: none; }
  .pocal-empty { color: var(--text-3); font-size: 12.5px; padding: 40px 0; text-align: center; }

  /* ───── Search ───── */
  .search-bar { position: relative; margin-bottom: 24px; }
  @keyframes searchPulse {
    0%, 100% { box-shadow: 0 6px 22px rgba(79,70,229,0.16); }
    50% { box-shadow: 0 6px 22px rgba(79,70,229,0.16), 0 0 0 7px rgba(79,70,229,0.12); }
  }
  .search-bar input {
    width: 100%; padding: 18px 175px 18px 54px;
    font-size: 15px; font-weight: 500; color: var(--text); font-family: inherit;
    border: 2px solid #a5b4fc; border-radius: 16px;
    background: linear-gradient(180deg, #ffffff, #f3f1ff); outline: none;
    box-shadow: 0 6px 22px rgba(79,70,229,0.16);
    transition: all 0.2s;
    animation: searchPulse 2.4s ease-in-out 3;
  }
  .search-bar input::placeholder { color: #6366f1; opacity: 0.78; font-weight: 500; }
  .search-bar input:focus {
    border-color: #4f46e5; background: #fff; animation: none;
    box-shadow: 0 0 0 4px rgba(79,70,229,0.18), 0 8px 26px rgba(79,70,229,0.22);
  }
  .search-icon { position: absolute; left: 18px; top: 50%; transform: translateY(-50%); font-size: 18px; opacity: 1; }
  .search-clear {
    position: absolute; right: 14px; top: 50%; transform: translateY(-50%);
    background: #f1f5f9; border: none; width: 28px; height: 28px;
    border-radius: 50%; cursor: pointer; font-size: 18px; color: #64748b;
    line-height: 1; transition: all 0.15s;
  }
  .search-clear:hover { background: #e2e8f0; color: var(--text); }
  .search-new {
    position: absolute; right: 52px; top: 50%; transform: translateY(-50%);
    background: var(--brand-soft); border: 1px solid rgba(79,70,229,0.18);
    padding: 6px 12px; border-radius: 8px; cursor: pointer;
    font-size: 11px; color: var(--brand); font-weight: 600; font-family: inherit;
    transition: all 0.15s;
  }
  .search-new:hover { background: var(--brand); color: white; border-color: var(--brand); }
  .chat-badge {
    display: inline-flex; align-items: center; gap: 6px;
    background: linear-gradient(135deg,#eef2ff,#faf5ff);
    border: 1px solid rgba(79,70,229,0.18);
    color: var(--brand); padding: 5px 12px;
    border-radius: 20px; font-size: 11px; font-weight: 600; margin-bottom: 12px;
  }
  .search-result { margin-bottom: 24px; }
  .search-empty {
    padding: 22px; color: var(--text-3); font-size: 13px; text-align: center;
    background: var(--surface); border: 1px dashed var(--border-2);
    border-radius: 14px;
  }
  .item-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 14px; cursor: pointer;
    transition: all 0.2s;
  }
  .item-card:hover {
    border-color: rgba(5,150,105,0.3);
    box-shadow: 0 6px 16px rgba(5,150,105,0.1); transform: translateY(-1px);
  }
  .item-card .code { font-size: 11.5px; font-weight: 700; color: var(--success); letter-spacing: 0.03em; }
  .item-card .name { font-size: 12px; color: var(--text-2); margin-top: 3px; line-height: 1.4; }

  /* ───── Tabs ───── */
  .tabs {
    display: inline-flex; gap: 2px; padding: 4px;
    background: rgba(15,23,42,0.04);
    border-radius: 11px; margin-bottom: 28px;
    box-shadow: inset 0 1px 2px rgba(15,23,42,0.04);
  }
  .tab {
    padding: 9px 22px; border-radius: 8px; border: none;
    background: transparent; cursor: pointer;
    font-weight: 600; font-size: 13px; color: var(--text-2);
    transition: all 0.2s; font-family: inherit;
    letter-spacing: -0.01em;
  }
  .tab.active {
    background: var(--surface); color: var(--brand);
    box-shadow: var(--shadow-sm);
  }
  .tab:hover:not(.active) { color: var(--text); }

  /* ───── Section / Cards ───── */
  .section-title {
    font-size: 11.5px; font-weight: 700; color: var(--text-2);
    margin: 4px 0 14px; display: flex; align-items: center; gap: 10px;
    text-transform: uppercase; letter-spacing: 0.1em;
  }
  .section-title .count {
    background: var(--brand-soft); color: var(--brand);
    font-size: 10px; padding: 3px 9px;
    border-radius: 10px; font-weight: 700; letter-spacing: 0;
  }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(232px, 1fr)); gap: 10px; }
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px 16px; cursor: pointer;
    transition: all 0.25s cubic-bezier(0.4,0,0.2,1);
    position: relative; overflow: hidden;
    box-shadow: var(--shadow-xs);
  }
  .card::before {
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
    background: linear-gradient(90deg, var(--brand), var(--brand-2));
    opacity: 0; transition: opacity 0.25s;
  }
  .card:hover {
    border-color: transparent; box-shadow: var(--shadow-lg);
    transform: translateY(-3px);
  }
  .card:hover::before { opacity: 1; }
  .card .code { font-size: 12px; font-weight: 700; color: var(--brand); letter-spacing: 0.04em; font-variant-numeric: tabular-nums; }
  .card .name { font-size: 13px; color: var(--text); margin-top: 5px; line-height: 1.45; min-height: 38px; font-weight: 500; }
  .loading { text-align: center; padding: 56px; color: var(--text-3); font-size: 13px; }

  /* ───── AI Response Card ───── */
  .ai-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px; padding: 20px 24px; margin-bottom: 14px;
    position: relative; box-shadow: var(--shadow-sm);
    animation: slideIn 0.3s ease-out;
  }
  @keyframes slideIn { from { opacity: 0; transform: translateY(8px) } to { opacity: 1; transform: translateY(0) } }
  .ai-card::before {
    content: ''; position: absolute; top: 0; left: 20px; right: 20px; height: 2px;
    background: linear-gradient(90deg, var(--brand), var(--brand-2));
    border-radius: 2px;
  }
  .ai-card .ai-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; gap: 12px; }
  .ai-card .ai-header .q {
    font-weight: 700; color: var(--text); font-size: 14.5px; line-height: 1.5;
    display: flex; gap: 10px; align-items: flex-start;
  }
  .ai-card .ai-header .q::before {
    content: '🤖'; flex-shrink: 0;
    width: 28px; height: 28px; border-radius: 8px;
    background: linear-gradient(135deg,#eef2ff,#faf5ff);
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 14px; margin-top: -2px;
  }
  .ai-card .ai-header .q > span:first-child { display: none; }
  .ai-card .ai-header .meta { font-size: 10.5px; color: var(--text-3); font-weight: 500; }
  .ai-card .ai-close {
    background: transparent; border: none; font-size: 18px; color: var(--text-3);
    cursor: pointer; padding: 2px 8px; border-radius: 6px; transition: all 0.15s;
  }
  .ai-card .ai-close:hover { background: #f1f5f9; color: var(--text); }
  .ai-card .ai-body { font-size: 13.5px; line-height: 1.75; color: var(--text); padding-left: 38px; }
  .ai-card .ai-body p { margin: 0.5em 0; }
  .ai-card .ai-body ul, .ai-card .ai-body ol { padding-left: 22px; margin: 0.5em 0; }
  .ai-card .ai-body li { margin: 0.25em 0; }
  .ai-card .ai-body strong { color: var(--text); font-weight: 700; }
  .ai-card .ai-body table { width: 100%; margin: 12px 0; border-collapse: collapse; font-size: 12px; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .ai-card .ai-body th, .ai-card .ai-body td { border: 1px solid var(--border); padding: 8px 10px; text-align: left; }
  .ai-card .ai-body th { background: var(--surface-2); font-weight: 600; color: var(--text-2); }
  .ai-card .ai-body code { background: #f1f5f9; padding: 2px 7px; border-radius: 5px; font-size: 12px; font-family: 'SF Mono', Menlo, monospace; }
  .ai-card .ai-body pre { background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 10px; overflow-x: auto; font-size: 12px; margin: 10px 0; }
  .ai-card.loading-ai { opacity: 0.9; }
  .ai-dots { display: inline-flex; gap: 3px; align-items: center; }
  .ai-dots span { display: inline-block; width: 7px; height: 7px; background: var(--brand); border-radius: 50%; animation: aibounce 1.4s infinite both; }
  .ai-dots span:nth-child(2) { animation-delay: 0.15s; }
  .ai-dots span:nth-child(3) { animation-delay: 0.3s; }
  @keyframes aibounce { 0%, 80%, 100% { transform: scale(0.6); opacity: 0.4 } 40% { transform: scale(1); opacity: 1 } }

  /* ───── Login Overlay ───── */
  .login-overlay {
    position: fixed; inset: 0; z-index: 1000;
    background: radial-gradient(ellipse at 30% 20%, rgba(99,102,241,0.18), transparent 50%),
                radial-gradient(ellipse at 70% 80%, rgba(168,85,247,0.15), transparent 50%),
                #0b0f1a;
    display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .login-box {
    background: rgba(255,255,255,0.98);
    backdrop-filter: blur(20px);
    border: 1px solid rgba(255,255,255,0.2);
    border-radius: 24px; padding: 52px 44px; text-align: center;
    box-shadow: 0 40px 80px -20px rgba(0,0,0,0.5);
    max-width: 440px; width: 100%;
  }
  .login-box .logo {
    width: 64px; height: 64px; margin: 0 auto 20px;
    border-radius: 18px;
    background: linear-gradient(135deg,#4f46e5 0%,#7c3aed 50%,#a855f7 100%);
    display: flex; align-items: center; justify-content: center;
    color: white; font-size: 32px; font-weight: 900;
    box-shadow: 0 20px 40px -10px rgba(79,70,229,0.5), inset 0 1px 0 rgba(255,255,255,0.3);
  }
  .login-box h1 { font-size: 22px; color: var(--text); margin-bottom: 8px; font-weight: 700; letter-spacing: -0.02em; }
  .login-box p { color: var(--text-2); font-size: 13px; line-height: 1.7; margin-bottom: 32px; }
  .login-btn {
    display: inline-flex; align-items: center; gap: 12px;
    padding: 14px 28px; border-radius: 12px; font-size: 14.5px; font-weight: 600;
    cursor: pointer; border: 1px solid var(--border-2); background: white; color: var(--text);
    box-shadow: var(--shadow-sm); transition: all 0.2s; font-family: inherit;
  }
  .login-btn:hover { box-shadow: var(--shadow-md); transform: translateY(-1px); border-color: var(--brand); }
  .user-chip {
    display: flex; align-items: center; gap: 10px;
    background: rgba(255,255,255,0.08); padding: 4px 10px 4px 4px;
    border-radius: 20px; margin-left: 12px;
    border: 1px solid rgba(255,255,255,0.1);
  }
  .user-chip img { width: 26px; height: 26px; border-radius: 50%; }
  .user-chip .name { color: white; font-size: 12px; font-weight: 500; }
  .user-chip button {
    background: none; border: none; color: rgba(255,255,255,0.6);
    cursor: pointer; font-size: 11px; padding: 4px 8px; border-radius: 6px;
    transition: all 0.15s;
  }
  .user-chip button:hover { color: white; background: rgba(255,255,255,0.1); }

  /* ───── Modal ───── */
  .modal-overlay {
    position: fixed; inset: 0;
    background: rgba(11,15,26,0.55);
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
    display: none; align-items: center; justify-content: center;
    z-index: 100; padding: 24px;
    animation: fadeIn 0.2s ease-out;
  }
  @keyframes fadeIn { from { opacity: 0 } to { opacity: 1 } }
  .modal-overlay.show { display: flex; }
  .modal {
    background: var(--surface); border-radius: 20px;
    max-width: 1180px; width: 100%; max-height: 92vh;
    overflow: hidden; display: flex; flex-direction: column;
    box-shadow: var(--shadow-xl);
    animation: scaleIn 0.25s cubic-bezier(0.4,0,0.2,1);
  }
  @keyframes scaleIn { from { opacity: 0; transform: scale(0.96) translateY(8px) } to { opacity: 1; transform: scale(1) translateY(0) } }
  .modal-header {
    padding: 22px 28px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: flex-start; gap: 16px;
    background: var(--surface-2);
  }
  .modal-header h2 { font-size: 18px; color: var(--text); font-weight: 700; letter-spacing: -0.02em; }
  .modal-header .code-badge {
    display: inline-block;
    background: linear-gradient(135deg,var(--brand) 0%,var(--brand-2) 100%);
    color: white; font-size: 11px; padding: 4px 12px;
    border-radius: 6px; margin-bottom: 8px; font-weight: 700; letter-spacing: 0.04em;
    box-shadow: 0 4px 12px rgba(79,70,229,0.3);
  }
  .modal-close {
    background: transparent; border: none; font-size: 24px; color: var(--text-3);
    cursor: pointer; line-height: 1; width: 34px; height: 34px;
    border-radius: 8px; transition: all 0.15s;
  }
  .modal-close:hover { background: #f1f5f9; color: var(--text); }
  .modal-body { padding: 24px 28px; overflow-y: auto; flex: 1; }

  /* ───── Tables ───── */
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th {
    text-align: left; padding: 11px 12px;
    background: var(--surface-2); color: var(--text-3);
    font-weight: 600; font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase;
    border-bottom: 1px solid var(--border); position: sticky; top: 0;
  }
  td { padding: 13px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
  tr:hover td { background: var(--surface-2); }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .chip {
    display: inline-block; padding: 3px 10px; border-radius: 6px;
    font-size: 11px; background: #f1f5f9; color: var(--text-2);
    margin: 2px; font-weight: 500;
  }
  .chip .q { color: var(--text); font-weight: 700; margin-left: 5px; }
  .cat-badge {
    font-size: 10px; padding: 3px 8px; border-radius: 5px;
    background: #e0e7ff; color: #3730a3; font-weight: 600;
  }

  /* ───── Production Panel ───── */
  .prod-panel {
    background: linear-gradient(135deg,#eef2ff 0%,#f5f3ff 50%,#f0fdf4 100%);
    border: 1px solid rgba(79,70,229,0.15);
    border-radius: 16px; padding: 18px 22px;
    box-shadow: var(--shadow-sm);
  }
  .prod-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  .prod-row label { font-weight: 700; color: var(--brand); font-size: 13.5px; letter-spacing: -0.01em; }
  .prod-row input {
    font-size: 20px; font-weight: 700; padding: 10px 16px;
    border: 1px solid rgba(79,70,229,0.2); border-radius: 10px;
    width: 160px; text-align: right; outline: none; background: white;
    color: var(--text); font-variant-numeric: tabular-nums;
    transition: all 0.2s; font-family: inherit;
  }
  .prod-row input:focus {
    border-color: var(--brand);
    box-shadow: 0 0 0 4px rgba(79,70,229,0.12);
  }
  .prod-unit { font-size: 12px; color: var(--text-2); flex: 1; }
  .prod-preset { display: flex; gap: 5px; }
  .prod-preset button {
    font-size: 11px; padding: 6px 12px; background: white;
    border: 1px solid rgba(79,70,229,0.2); border-radius: 7px;
    cursor: pointer; color: var(--brand); font-weight: 600;
    font-family: inherit; transition: all 0.15s;
  }
  .prod-preset button:hover {
    background: var(--brand); color: white; border-color: var(--brand);
    transform: translateY(-1px); box-shadow: var(--shadow-sm);
  }
  .prod-summary {
    display: flex; gap: 24px; margin-top: 14px; padding-top: 14px;
    border-top: 1px dashed rgba(79,70,229,0.25);
  }
  .prod-summary > div { flex: 1; }
  .prod-summary .label {
    display: block; font-size: 10.5px; color: var(--text-2);
    margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600;
  }
  .prod-summary b { font-size: 17px; color: var(--text); font-weight: 700; letter-spacing: -0.01em; }
  tr.row-short td { background: rgba(220,38,38,0.04) !important; }
  tr.row-short:hover td { background: rgba(220,38,38,0.08) !important; }

  /* ───── Scrollbar ───── */
  .modal-body::-webkit-scrollbar { width: 10px; }
  .modal-body::-webkit-scrollbar-track { background: transparent; }
  .modal-body::-webkit-scrollbar-thumb { background: rgba(15,23,42,0.15); border-radius: 10px; border: 2px solid white; }
  .modal-body::-webkit-scrollbar-thumb:hover { background: rgba(15,23,42,0.25); }

  @media (max-width: 768px) {
    /* ───── Header ───── */
    header { padding: 12px 14px; flex-direction: column; gap: 10px; align-items: stretch; }
    header h1 { font-size: 15px; }
    header h1::before { width: 26px; height: 26px; }
    header .subtitle { display: none; }
    .nav-links { gap: 4px; justify-content: flex-start; }
    .nav-links a { padding: 6px 10px; font-size: 11.5px; }
    .nav-refresh-btn { padding: 5px 9px; font-size: 11px; }

    /* ───── Layout ───── */
    .wrap { padding: 14px 12px 80px; gap: 14px; }
    .sidebar { padding-bottom: 4px; }
    .sidebar > * { min-width: 220px; }
    .chart-grid { padding: 0 12px; gap: 10px; margin-top: 10px; }
    .chart-panel { padding: 10px 12px 10px; }
    .chart-title { font-size: 12px; }
    .chart-sub { font-size: 10px; display: block; margin-left: 0; margin-top: 2px; }

    /* ───── Calendar ───── */
    .cal-widget { padding: 10px 12px; }
    .cal-title { font-size: 12px; }
    .cal-day { font-size: 10.5px; padding: 4px 0; }

    /* ───── PO / OS Inline Rows ───── */
    .po-inline-row {
      grid-template-columns: 60px 1fr auto auto;
      gap: 6px; padding: 7px 6px; font-size: 11px;
    }
    .po-inline-row > .po-inline-qty,
    .po-inline-row > .po-inline-dest,
    .po-inline-row > .po-inline-remain { grid-column: 2 / -1; justify-self: start; max-width: 100%; }
    .po-inline-name { font-size: 11px; }
    /* 모바일 가로 넘침 방지(2026-09-04): 그리드 자식 min-width:auto 때문에 긴 품명이 1fr 칸을 밀어 화면 밖으로 나가던 문제 */
    .chart-panel, .chart-grid > *, .po-inline-row > *, .alert-row > *, .po-inline-list, .alert-list { min-width: 0; }
    .po-inline-name, .alert-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .po-inline-row .po-inline-name > *, .alert-row .alert-name > * { overflow: hidden; text-overflow: ellipsis; }
    body { overflow-x: hidden; }

    /* ───── Alert Rows ───── */
    .alert-row { grid-template-columns: 56px 1fr auto auto; gap: 6px; padding: 7px 8px; font-size: 11px; }
    .alert-list { max-height: 240px; }

    /* ───── 부자재 규격 → 카드 형태 ───── */
    .spec-head { display: none; }
    .spec-row {
      grid-template-columns: 1fr auto;
      grid-template-areas:
        "code price"
        "name name"
        "div check"
        "size size"
        "mat mat"
        "vendor moq";
      gap: 4px 8px; padding: 10px 12px;
      font-size: 11.5px;
    }
    .spec-row .sc-check  { grid-area: check; display: flex; align-items: center; justify-content: flex-end; }
    .spec-row .sc-code   { grid-area: code; }
    .spec-row .sc-name   { grid-area: name; white-space: normal; }
    .spec-row .sc-div    { grid-area: div; justify-self: start; }
    .spec-row .sc-size   { grid-area: size; }
    .spec-row .sc-mat    { grid-area: mat; white-space: normal; font-size: 11px; }
    .spec-row .sc-moq    { grid-area: moq; text-align: right; }
    .spec-row .sc-price  { grid-area: price; text-align: right; }
    .spec-row .sc-vendor { grid-area: vendor; justify-self: start; }
    .spec-row + .spec-row { border-top: 1px solid rgba(15,23,42,0.08); }
    .spec-list { max-height: 360px; }
    .spec-strip { padding: 0 12px; }
    .ipsu-strip { padding: 0 12px; }

    /* ───── 단가 계산기 ───── */
    .pcalc-cat-tabs { gap: 4px; flex-wrap: wrap; }
    .pcalc-cat-tab { padding: 6px 12px; font-size: 11.5px; }
    #pcalc-w, #pcalc-h, #pcalc-d { width: 56px !important; }
    #pcalc-list, #pcalc-similar { height: 240px !important; }

    /* ───── 검색바 ───── */
    .search-bar input { padding: 12px 14px; font-size: 13px; }
    .po-inline-search { padding: 8px 12px; font-size: 12px; }

    /* ───── 모달 ───── */
    .modal { max-width: 100vw !important; margin: 8px; max-height: calc(100vh - 16px); }
    .modal-body { padding: 12px 14px; }

    /* ───── 입고 캘린더 (모바일) ───── */
    .chart-head > div:first-child { flex-wrap: wrap; gap: 6px 8px !important; }
    .po-cal-btn { padding: 5px 10px; font-size: 10.5px; }
    .modal-header { padding: 16px 16px; flex-wrap: wrap; }
    .modal-header h2 { font-size: 15px; }
    .pocal-search-wrap { order: 5; flex-basis: 100%; width: 100%; max-width: none; margin: 12px 0 0; height: 42px; }
    .pocal-cal { padding: 14px 12px 16px; border-radius: 16px; }
    .pocal-nav .pocal-ym { font-size: 17px; }
    .pocal-nav button { width: 34px; height: 34px; }
    .pocal-grid { gap: 4px; }
    .pocal-cell { min-height: 46px; border-radius: 11px; padding: 6px 0 0 7px; }
    .pocal-cell .dnum { font-size: 13px; min-width: 21px; height: 21px; }
    .pocal-dots { margin-top: 4px; gap: 2px; }
    .pocal-dot { width: 6px; height: 6px; }
    .pocal-cnt { right: 4px; bottom: 4px; font-size: 10px; min-width: 16px; height: 16px; }
    .pocal-detail { max-height: 320px; }
    .pocal-drow { padding: 9px 8px; gap: 8px; }
    .pocal-drow .dname { font-size: 12px; }
  }
</style>
</head>
<body>

<!-- 로그인 오버레이 -->
<div class="login-overlay" id="login-overlay">
  <div class="login-box">
    <div class="logo">M</div>
    <h1>구매/외주 대시보드</h1>
    <p>BOM · 단가 · 외주처별 재고 통합 조회 플랫폼<br>Google 계정으로 로그인하여 시작하세요</p>
    <button class="login-btn" onclick="doLogin()">
      <img src="https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg" width="20" height="20">
      Google로 계속하기
    </button>
    <button class="login-btn" onclick="skipLogin()" style="margin-top:12px;background:var(--surface-2,#f1f5f9);color:var(--text,#334155);border:1px solid #cbd5e1;font-size:13px;">
      🔓 로그인 없이 바로 사용
    </button>
  </div>
</div>

<header>
  <div>
    <h1>구매/외주 대시보드</h1>
    <div class="subtitle">완제품/외주제품 BOM · 단가 · 외주처별 재고 조회</div>
  </div>
  <nav class="nav-links">
    {% if not public_mode %}
    <button id="refresh-monday-btn" class="nav-refresh-btn" onclick="triggerMondayRefresh()" title="Monday 4개 보드 재수집 (3~5분)">
      <span id="refresh-icon">↻</span>
      <span id="refresh-label">Monday 새로고침</span>
    </button>
    <button id="refresh-aramanth-btn" class="nav-refresh-btn nav-refresh-amber" onclick="triggerAramanthRefresh()" title="아마란스 fetch_all + fetch_bom 재수집 (10~15분)">
      <span id="refresh-aramanth-icon">↻</span>
      <span id="refresh-aramanth-label">아마란스 새로고침</span>
    </button>
    {% endif %}
    <button id="reload-dfs-btn" class="nav-refresh-btn" onclick="triggerReloadDfs()" title="fetch 없이 디스크 CSV만 메모리에 다시 로드 (1~2초)" style="background:#f1f5f9;color:#475569;border-color:#cbd5e1">
      <span style="font-weight:700">⟳</span>
      <span id="reload-dfs-label">메모리 리로드</span>
    </button>
    <button id="data-health-btn" class="nav-refresh-btn" onclick="openDataHealth()"
            title="데이터 건강검진" style="display:none"></button>
    <button id="notify-btn" class="nav-refresh-btn" onclick="openNotify()" title="알림 센터"
            style="background:#f1f5f9;color:#475569;border-color:#cbd5e1;position:relative">
      <span style="font-size:13px">🔔</span><span id="notify-label">알림</span>
      <span id="notify-badge" style="display:none;position:absolute;top:-6px;right:-6px;min-width:18px;height:18px;
        padding:0 5px;border-radius:9px;background:#dc2626;color:#fff;font-size:10.5px;font-weight:800;
        line-height:18px;text-align:center"></span>
    </button>
    <a href="/report/monthly" target="_blank" title="전월 월간 리포트 (인쇄→PDF)">📊 월간 리포트</a>
    <a href="#" onclick="openVendorLinks();return false;" title="더고은·정성·데이웰즈 담당자 입력 링크 · 입력 현황 · 마감 생성">📝 외주재고 입력</a>
    <a href="#" onclick="openHistory();return false;">채팅내역</a>
    {% if not public_mode %}
    <!-- 관리자 메뉴 제거(2026-09-02): 미사용. 라우트 /admin은 유지 -->
    <!-- 업로드 메뉴 제거(2026-09-02): 재고·자사재고·단가 전부 OneDrive/공유링크 자동 반영. /upload 라우트는 비상용으로 유지 -->

    <a href="/vendor/데이웰즈" target="_blank">데이웰즈</a>
    <a href="/vendor/더고은" target="_blank">더고은</a>
    <a href="/vendor/정성" target="_blank">정성</a>
    <!-- 청통본가 메뉴 제거(2026-09-02): 부재료를 외주처가 직접 관리 → 매홍 부재료 재고 없음 -->

    {% endif %}
    <div class="user-chip" id="user-chip" style="display:none">
      <img id="user-photo" src="" alt="">
      <span class="name" id="user-name"></span>
      <button onclick="doLogout()">로그아웃</button>
    </div>
  </nav>
</header>

<!-- KPI 요약 띠 (2026-09-04): 오늘 봐야 할 숫자 — 클릭하면 해당 패널로 이동 -->
<section class="kpi-strip" id="kpi-strip">
  <div class="kpi-tile" data-go=".scope-jasa" id="kpi-stock"><div class="kpi-ic">📦</div><div class="kpi-body"><div class="kpi-v">–</div><div class="kpi-l">품절 부자재</div><div class="kpi-s"></div></div></div>
  <div class="kpi-tile" data-go=".reorder-panel" id="kpi-reorder"><div class="kpi-ic">🛒</div><div class="kpi-body"><div class="kpi-v">–</div><div class="kpi-l">지금 발주</div><div class="kpi-s"></div></div></div>
  <div class="kpi-tile" data-go=".po-inline" id="kpi-incoming"><div class="kpi-ic">🚚</div><div class="kpi-body"><div class="kpi-v">–</div><div class="kpi-l">입고 미정</div><div class="kpi-s"></div></div></div>
  <div class="kpi-tile" data-go=".plan-panel" id="kpi-supply"><div class="kpi-ic">🏭</div><div class="kpi-body"><div class="kpi-v">–</div><div class="kpi-l">완제품 품절·2주↓</div><div class="kpi-s"></div></div></div>
  <div class="kpi-tile" data-go=".price-span" id="kpi-price"><div class="kpi-ic">📈</div><div class="kpi-body"><div class="kpi-v">–</div><div class="kpi-l">단가 변동</div><div class="kpi-s"></div></div></div>
  <div class="kpi-tile" data-go="health" id="kpi-health"><div class="kpi-ic">🩺</div><div class="kpi-body"><div class="kpi-v">–</div><div class="kpi-l">데이터 상태</div><div class="kpi-s"></div></div></div>
</section>

<!-- 섹션 점프 내비 (우측 고정) -->
<nav class="sec-nav" id="sec-nav" aria-label="섹션 이동">
  <a href="#kpi-strip" data-sec="kpi-strip" title="맨 위로">▲</a>
  <a href="#sec-alerts" data-sec="sec-alerts">재고</a>
  <a href="#sec-price" data-sec="sec-price">단가</a>
  <a href="#sec-plan" data-sec="sec-plan">수급</a>
  <a href="#sec-spec" data-sec="sec-spec">규격</a>
  <a href="#sec-return" data-sec="sec-return">회송</a>
  <a href="#sec-ipsu" data-sec="sec-ipsu">3D</a>
  <a href="#sec-sales" data-sec="sec-sales">매출</a>
  <a href="#sec-lens" data-sec="sec-lens">분석</a>
  <a href="#sec-cal" data-sec="sec-cal">달력</a>
</nav>

<!-- 차트 그리드: 상단 TOP5 + 단가 계산 / 하단 재고경고 2개 -->
<section class="chart-grid">
  <div class="chart-panel po-inline">
    <div class="chart-head">
      <div style="display:flex;align-items:center;gap:12px">
        <div><span class="chart-title">🚚 원/부자재 예상입고일</span><span class="chart-sub">부자재·원료·수입현황</span></div>
        <button class="po-cal-btn" onclick="openPoCalModal()" title="입고일정 캘린더 보기">📅 입고 캘린더</button>
      </div>
      <div id="po-inline-count" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
    </div>
    <input id="po-inline-search" class="po-inline-search" type="text" placeholder="품번/품명 검색…" oninput="renderPoInline()">
    <div id="po-inline-list" class="po-inline-list"><div class="loading" style="padding:16px">로딩 중...</div></div>
  </div>
  <div class="chart-panel os-inline">
    <div class="chart-head">
      <div style="display:flex;align-items:center;gap:12px">
        <div><span class="chart-title">🏭 외주 입고 물량</span><span class="chart-sub">외주 생산 요청 · 발주완료·생산요청·확인필요</span></div>
        <button class="po-cal-btn" onclick="openOsCalModal()" title="외주 입고일정 캘린더 보기">📅 입고 캘린더</button>
      </div>
      <div id="os-inline-count" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
    </div>
    <input id="os-inline-search" class="po-inline-search" type="text" placeholder="품번/품명 검색…" oninput="renderOsInline()">
    <div id="os-inline-list" class="po-inline-list"><div class="loading" style="padding:16px">로딩 중...</div></div>
  </div>
  <div class="chart-panel scope-jasa alert-span">
    <div class="chart-head">
      <div><span class="chart-title">🏠 자사 재고 경고</span><span class="chart-sub">자사창고 기준 · 45일 이하 · 클릭=상세</span></div>
      <div id="alert-count-jasa" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
    </div>
    <div id="alert-list-jasa" class="alert-list"><div class="loading" style="padding:20px">로딩 중...</div></div>
  </div>
  <div class="chart-panel scope-outsource alert-span">
    <div class="chart-head">
      <div><span class="chart-title">🚨 외주 재고 경고</span><span class="chart-sub">외주처 기준 · 45일 이하 · 클릭=상세</span></div>
      <div style="display:flex;align-items:center;gap:8px">
        <button class="po-cal-btn" onclick="openVendorLinks()" title="거래처 재고 입력 링크 · 입력 현황">👥 거래처 입력</button>
        <div id="alert-count-outsource" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
      </div>
    </div>
    <div id="alert-list-outsource" class="alert-list"><div class="loading" style="padding:20px">로딩 중...</div></div>
  </div>
  <div class="chart-panel alert-span reorder-panel">
    <div class="chart-head">
      <div><span class="chart-title">🕐 발주 타이밍</span><span class="chart-sub">자재 + 상품매입 완제품 · 소진일 vs 실측 리드타임 · 클릭=상세</span></div>
      <div style="display:flex;align-items:center;gap:8px">
        <button onclick="openPrDraft()" style="padding:3px 9px;font-size:11px;font-weight:700;border:1px solid #e11d48;
          border-radius:6px;background:#fff;color:#e11d48;cursor:pointer;white-space:nowrap"
          title="발주 필요 품목을 아마란스 청구요청 양식으로 자동 구성">📋 청구요청 초안</button>
        <div id="reorder-count" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
      </div>
    </div>
    <div id="reorder-list" class="alert-list"><div class="loading" style="padding:20px">로딩 중...</div></div>
  </div>
  <!-- 줄 순서(2026-09-04): 수급·거래처 → 단가변동·계산기 → (아래 규격 스트립). 규격의 ↑가 계산기에 적용되므로 계산기가 규격 바로 위에 와야 함 -->
  <div class="chart-panel plan-panel">
    <div class="chart-head">
      <div><span class="chart-title">📦 완제품 수급 플래너</span><span class="chart-sub">최근 판매속도 vs 창고재고(판정)·채널재고(참고) vs 생산계획·입고예정 · 클릭=상세</span></div>
      <div style="display:flex;align-items:center;gap:8px">
        <input id="plan-search" type="text" placeholder="품번/품명" oninput="renderPlan()" style="width:120px;padding:3px 8px;font-size:11px;border:1px solid var(--border);border-radius:6px">
        <div id="plan-count" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
      </div>
    </div>
    <div id="plan-summary" style="font-size:11px;color:var(--text-2);margin-bottom:6px"></div>
    <div id="plan-list" class="alert-list"><div class="loading" style="padding:20px">로딩 중...</div></div>
  </div>
  <div class="chart-panel chstock-panel">
    <div class="chart-head">
      <div><span class="chart-title">🏬 채널 품절 경보</span><span class="chart-sub">쿠팡 센터·마트 매장 재고 ÷ 최근 14일 POS · 7일 미만 · 클릭=상세</span></div>
      <div style="display:flex;align-items:center;gap:8px">
        <div class="vk-chips">
          <button class="vk-chip on" data-t="all" onclick="setChType('all',this)">전체</button>
          <button class="vk-chip" data-t="online" onclick="setChType('online',this)">온라인</button>
          <button class="vk-chip" data-t="offline" onclick="setChType('offline',this)">오프라인</button>
        </div>
        <div id="chstock-count" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
      </div>
    </div>
    <div id="chstock-summary" style="font-size:11px;color:var(--text-2);margin-bottom:6px"></div>
    <div id="chstock-list" class="alert-list"><div class="loading" style="padding:20px">로딩 중...</div></div>
  </div>
  <div class="chart-panel vendor-panel">
    <div class="chart-head">
      <div><span class="chart-title">🏷 거래처 스코어카드</span><span class="chart-sub">최근 12개월 · 구매+외주 통합 · 납기준수·입고충족·단가안정 · 클릭=상세</span></div>
      <div style="display:flex;align-items:center;gap:8px">
        <div id="vendor-count" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
        <div class="vk-chips">
          <button class="vk-chip on" data-k="all" onclick="setVendorKind('all',this)">전체</button>
          <button class="vk-chip" data-k="po" onclick="setVendorKind('po',this)">구매</button>
          <button class="vk-chip" data-k="wp" onclick="setVendorKind('wp',this)">외주</button>
        </div>
      </div>
    </div>
    <div id="vendor-list" class="alert-list"><div class="loading" style="padding:20px">로딩 중...</div></div>
  </div>
  <div class="chart-panel price-span">
    <div class="chart-head">
      <div><span class="chart-title">📈 단가 변동</span><span class="chart-sub">최근 6개월 발주단가 변경 · 영향액순 · 클릭=상세</span></div>
      <div id="price-chg-count" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
    </div>
    <div id="price-chg-list" class="alert-list"><div class="loading" style="padding:20px">로딩 중...</div></div>
  </div>
  <div class="chart-panel pcalc-panel">
    <div class="chart-head">
      <div><span class="chart-title">🧮 예상 단가 계산기</span><span class="chart-sub">재질 선택 · <span id="pcalc-samples">-</span>건 샘플 기반 추정</span></div>
      <div id="pcalc-est-inline" style="font-size:14px;font-weight:800;color:#0c4a6e;font-variant-numeric:tabular-nums">0<span style="font-size:10.5px;font-weight:600;margin-left:2px;color:#0369a1">원</span></div>
    </div>
    <div id="pcalc-compare-bar" style="display:none;margin-bottom:6px"></div>
    <div id="pcalc-cat-tabs" class="pcalc-cat-tabs" style="align-items:center">
      <button class="pcalc-cat-tab active" data-cat="">전체</button>
      <button class="pcalc-cat-tab" data-cat="파우치">파우치</button>
      <button class="pcalc-cat-tab" data-cat="단상자">단상자</button>
      <button class="pcalc-cat-tab" data-cat="박스">박스</button>
      <span style="margin-left:auto;font-size:10.5px;color:#b45309;background:#fef3c7;padding:2px 8px;border-radius:6px;font-weight:600;letter-spacing:-0.01em;white-space:nowrap">※ 동일 규격이라도 인쇄 도수·디자인에 따라 단가 편차 발생</span>
    </div>
    <div style="display:flex;gap:4px;margin-bottom:6px;align-items:center;flex-wrap:wrap">
      <input id="pcalc-search" type="text" placeholder="재질 검색…" style="flex:1;min-width:120px;padding:6px 10px;font-size:11.5px;border:1px solid var(--border-2);border-radius:6px;outline:none" oninput="renderPriceCalcList()">
      <span style="font-size:10.5px;color:var(--text-3)">가로</span>
      <input id="pcalc-w" type="number" placeholder="W" min="0" tabindex="1" style="width:48px;padding:6px 4px;font-size:11.5px;border:1px solid var(--border-2);border-radius:6px;outline:none;font-variant-numeric:tabular-nums;text-align:center" oninput="updatePriceCalc()" onkeydown="if(event.key==='Enter'||event.key==='Tab'){if(!event.shiftKey){event.preventDefault();document.getElementById('pcalc-h').focus();document.getElementById('pcalc-h').select()}}">
      <span style="font-size:10.5px;color:var(--text-3)">세로</span>
      <input id="pcalc-h" type="number" placeholder="H" min="0" tabindex="2" style="width:48px;padding:6px 4px;font-size:11.5px;border:1px solid var(--border-2);border-radius:6px;outline:none;font-variant-numeric:tabular-nums;text-align:center" oninput="updatePriceCalc()" onkeydown="if(event.key==='Enter'||event.key==='Tab'){if(!event.shiftKey){event.preventDefault();document.getElementById('pcalc-d').focus();document.getElementById('pcalc-d').select()}else{event.preventDefault();document.getElementById('pcalc-w').focus();document.getElementById('pcalc-w').select()}}">
      <span id="pcalc-d-label" style="font-size:10.5px;color:var(--text-3)">밑지/높이</span>
      <input id="pcalc-d" type="number" placeholder="D" min="0" tabindex="3" style="width:48px;padding:6px 4px;font-size:11.5px;border:1px solid var(--border-2);border-radius:6px;outline:none;font-variant-numeric:tabular-nums;text-align:center" oninput="updatePriceCalc()" onkeydown="if(event.key==='Enter'||event.key==='Tab'){if(!event.shiftKey){event.preventDefault();document.getElementById('pcalc-w').focus();document.getElementById('pcalc-w').select()}else{event.preventDefault();document.getElementById('pcalc-h').focus();document.getElementById('pcalc-h').select()}}">
      <span style="font-size:10.5px;color:var(--text-3)">mm</span>
      <button onclick="resetPriceCalc()" style="padding:6px 10px;font-size:11px;border:1px solid var(--border-2);border-radius:6px;background:white;cursor:pointer;color:var(--text-2)">초기화</button>
    </div>
    <div id="pcalc-selected-bar" class="pcalc-selected-bar"></div>
    <div style="display:grid;grid-template-columns:1.4fr 1fr;gap:8px">
      <div id="pcalc-list" style="height:200px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:4px"></div>
      <div id="pcalc-similar" style="height:200px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:4px"></div>
    </div>
  </div>
</section>

<!-- 부자재 규격 한눈에 보기 -->
<section class="spec-strip">
  <div class="chart-panel spec-panel">
    <div class="chart-head">
      <div><span class="chart-title">📐 부자재 규격</span><span class="chart-sub">Monday 부자재 규격 보드 · 카테고리·품번 정렬</span></div>
      <div id="spec-count" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
    </div>
    <input id="spec-search" type="text" placeholder="품번/품명/재질/업체/구분 검색…" oninput="renderSpecList()"
           style="width:100%;padding:6px 10px;margin-bottom:6px;font-size:11.5px;border:1px solid var(--border-2);border-radius:6px;outline:none">
    <div id="spec-list" class="spec-list"><div class="loading" style="padding:14px">로딩 중...</div></div>
  </div>
</section>

<!-- 입수 테스트 (3D) -->
<!-- 회송 원가 역산 (2026-09-09): 완제품 품번×수량 → BOM 전개 → 원재료·부재료 금액 -->
<section class="return-strip">
  <div class="chart-panel return-panel">
    <div class="chart-head">
      <div><span class="chart-title">🔁 회송 원가 역산</span><span class="chart-sub">완제품 품번·수량 → BOM 역산 · 원재료/부재료 금액 · 단가=최신 발주가 › 단가표 › BOM</span></div>
      <div id="rc-count" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
    </div>
    <div class="rc-form">
      <div class="rc-field" style="position:relative;flex:1;min-width:220px">
        <input id="rc-q" type="text" placeholder="완제품 품번/품명 검색 (예: G0039, 누룽지)" autocomplete="off"
               oninput="rcSuggest()" onkeydown="rcKey(event)" onblur="setTimeout(()=>document.getElementById('rc-sug').style.display='none',150)">
        <div id="rc-sug" class="rc-sug" style="display:none"></div>
      </div>
      <div class="vk-chips" id="rc-grp" title="검색 대상">
        <button class="vk-chip on" data-g="all" onclick="rcGroup('all',this)">전체</button>
        <button class="vk-chip" data-g="G" onclick="rcGroup('G',this)">자사</button>
        <button class="vk-chip" data-g="HI" onclick="rcGroup('HI',this)">외주</button>
      </div>
      <div class="rc-field"><label>수량</label><input id="rc-qty" type="number" min="0" value="1" style="width:90px;text-align:right" oninput="rcQtyChange()" placeholder="수량"></div>
      <button class="rc-btn" onclick="rcRun()">계산</button>
      <button class="rc-btn ghost" onclick="rcCopy()" id="rc-copy" disabled>복사</button>
      <span id="rc-msg" style="font-size:11px;color:var(--text-3)"></span>
    </div>
    <div id="rc-summary" class="rc-summary" style="display:none"></div>
    <div id="rc-table" class="rc-table" style="display:none"></div>
  </div>
</section>

<section class="ipsu-strip">
  <div class="chart-panel ipsu-panel">
    <div class="chart-head">
      <div><span class="chart-title">📦 입수 테스트 (3D)</span>
        <span class="chart-sub">부자재 규격 자동 연동 · 실측 입수 기반 시각화</span></div>
      <div id="ipsu-stat" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
    </div>

    <!-- ⓪ 완제품 (BOM 자동 채움) -->
    <div class="ipsu-block" style="border-color:#86efac;background:#f0fdf4">
      <div class="ib-t">⓪ 완제품 선택 — BOM으로 ①②③ 한번에 채움</div>
      <div class="ipsu-row">
        <div class="ipsu-fld" style="flex:2;min-width:230px"><label>완제품 품번/품명</label>
          <div class="ipsu-search">
            <input id="ipsu-p-q" type="text" placeholder="예: G0010 · 고구마말랭이…" autocomplete="off"
                   oninput="ipsuSuggest('p')" onblur="ipsuHideSug('p')">
            <div id="ipsu-p-sug" class="ipsu-sug"></div>
          </div></div>
        <div class="ipsu-fld" style="flex:3;min-width:260px"><label>자동 채움 결과</label>
          <div id="ipsu-p-sel" style="font-size:10.5px;color:var(--text-3);padding:5px 0;line-height:1.5">-</div></div>
      </div>
    </div>

    <!-- ① 1차파우치 -->
    <div class="ipsu-block">
      <div class="ib-t">① 1차파우치</div>
      <div class="ipsu-row">
        <div class="ipsu-fld"><label>제품 검색 (부자재 규격)</label>
          <div class="ipsu-search">
            <input id="ipsu-a-q" type="text" placeholder="품번/품명 검색…" autocomplete="off"
                   oninput="ipsuSuggest('a')" onblur="ipsuHideSug('a')">
            <div id="ipsu-a-sug" class="ipsu-sug"></div>
          </div></div>
        <div class="ipsu-fld"><label>형태</label>
          <select id="ipsu-a-form" onchange="ipsuOnForm('a')">
            <option value="스탠드">스탠드파우치</option>
            <option value="3면실링">3면실링</option>
          </select></div>
        <div class="ipsu-fld"><label>가로 W</label><input id="ipsu-aw" class="ipsu-num" type="number" value="130" oninput="ipsuCalc()"></div>
        <div class="ipsu-fld"><label>세로 H</label><input id="ipsu-ah" class="ipsu-num" type="number" value="155" oninput="ipsuCalc()"></div>
        <div class="ipsu-fld" id="ipsu-a-basefld"><label>밑지</label><input id="ipsu-abase" class="ipsu-num" type="number" value="80" oninput="ipsuCalc()"></div>
        <div class="ipsu-fld"><label>채움두께 T</label><input id="ipsu-at" class="ipsu-num" type="number" value="30" oninput="ipsuCalc()"></div>
        <div class="ipsu-fld" style="flex:1;min-width:120px"><label>선택됨</label>
          <div id="ipsu-a-sel" style="font-size:10.5px;color:var(--text-3);padding:5px 0">-</div></div>
      </div>
    </div>

    <!-- ② 내부용기 -->
    <div class="ipsu-block">
      <div class="ib-t">② 내부용기 (2차파우치 / 단상자)</div>
      <div class="ipsu-row">
        <div class="ipsu-fld"><label>제품 검색 (부자재 규격)</label>
          <div class="ipsu-search">
            <input id="ipsu-b-q" type="text" placeholder="품번/품명 검색…" autocomplete="off"
                   oninput="ipsuSuggest('b')" onblur="ipsuHideSug('b')">
            <div id="ipsu-b-sug" class="ipsu-sug"></div>
          </div></div>
        <div class="ipsu-fld"><label>형태</label>
          <select id="ipsu-b-form" onchange="ipsuOnForm('b')">
            <option value="스탠드">스탠드파우치</option>
            <option value="3면실링">3면실링</option>
            <option value="단상자">단상자(박스)</option>
            <option value="없음">없음 — 1차파우치 → 외박스 직행</option>
          </select></div>
        <span id="ipsu-b-pouch" style="display:contents">
          <div class="ipsu-fld"><label>가로 W</label><input id="ipsu-bw" class="ipsu-num" type="number" value="220" oninput="ipsuCalc()"></div>
          <div class="ipsu-fld"><label>세로 H</label><input id="ipsu-bh" class="ipsu-num" type="number" value="290" oninput="ipsuCalc()"></div>
          <div class="ipsu-fld" id="ipsu-b-basefld"><label>밑지</label><input id="ipsu-bbase" class="ipsu-num" type="number" value="60" oninput="ipsuCalc()"></div>
          <div class="ipsu-fld"><label>채움두께 T</label><input id="ipsu-bt" class="ipsu-num" type="number" value="60" oninput="ipsuCalc()"></div>
        </span>
        <span id="ipsu-b-box" style="display:none">
          <div class="ipsu-fld"><label>내치수 W×D×H</label>
            <div style="display:flex;gap:3px">
              <input id="ipsu-bbw" class="ipsu-num" type="number" value="200" oninput="ipsuCalc()">
              <input id="ipsu-bbd" class="ipsu-num" type="number" value="140" oninput="ipsuCalc()">
              <input id="ipsu-bbh" class="ipsu-num" type="number" value="170" oninput="ipsuCalc()">
            </div></div>
        </span>
        <div class="ipsu-fld" style="flex:1;min-width:120px"><label>선택됨</label>
          <div id="ipsu-b-sel" style="font-size:10.5px;color:var(--text-3);padding:5px 0">-</div></div>
      </div>
    </div>

    <!-- ③ 외박스 + ④ 입수 -->
    <div class="ipsu-block" style="border-color:#c4b5fd;background:#faf5ff">
      <div class="ib-t">③ 외박스 외치수 · ④ 실측 입수/배열</div>
      <div class="ipsu-row">
        <div class="ipsu-fld" style="min-width:170px"><label>외박스 규격 검색 (C코드)</label>
          <div class="ipsu-search">
            <input id="ipsu-o-q" type="text" placeholder="예: 공용 RRP · C0018…" autocomplete="off"
                   oninput="ipsuSuggest('o')" onblur="ipsuHideSug('o')">
            <div id="ipsu-o-sug" class="ipsu-sug"></div>
          </div>
          <div id="ipsu-o-sel" style="font-size:10px;color:var(--text-3);margin-top:2px"></div></div>
        <div class="ipsu-fld">
          <label title="외치수 입력 · 적재 계산은 골판지 벽두께(편면 5mm)를 뺀 내부공간 기준">외박스 외치수 W×D×H</label>
          <div style="display:flex;gap:3px">
            <input id="ipsu-cw" class="ipsu-num" type="number" value="530" oninput="ipsuSyncOuter()">
            <input id="ipsu-cd" class="ipsu-num" type="number" value="360" oninput="ipsuSyncOuter()">
            <input id="ipsu-ch" class="ipsu-num" type="number" value="255" oninput="ipsuSyncOuter()">
          </div></div>
        <div class="ipsu-fld"><label title="공기 든 파우치가 적재 시 두께 방향으로 눌리는 비율">눌림(압축) %</label>
          <input id="ipsu-comp" class="ipsu-num" type="number" value="0" min="0" max="80" step="5" oninput="ipsuCalc()"></div>
        <div class="ipsu-fld"><label title="실제 박스당 최대입수를 넣으면 파우치 두께를 자동 역산해 맞춥니다">실측 최대입수(두께보정)</label>
          <input id="ipsu-realmax" class="ipsu-num" type="number" placeholder="선택" min="1" oninput="ipsuCalibThickness()"></div>
        <div style="width:1px;background:#ddd6fe;align-self:stretch;margin:0 3px"></div>
        <div class="ipsu-fld"><label>입력 방식</label>
          <select id="ipsu-mode" onchange="ipsuOnMode()">
            <option value="grid">배열 직접 입력 (열×줄×층)</option>
            <option value="count">입수 개수 입력</option>
          </select></div>
        <span id="ipsu-step1fields" style="display:contents">
          <span id="ipsu-g1" style="display:contents">
            <div class="ipsu-fld"><label>1단계 · 2차 안 배열 (열×줄×층)</label>
              <div style="display:flex;gap:3px;align-items:center">
                <input id="ipsu-g1c" class="ipsu-num" type="number" value="3" min="1" oninput="ipsuCalc()">
                <span style="color:var(--text-3)">×</span>
                <input id="ipsu-g1r" class="ipsu-num" type="number" value="1" min="1" oninput="ipsuCalc()">
                <span style="color:var(--text-3)">×</span>
                <input id="ipsu-g1l" class="ipsu-num" type="number" value="1" min="1" oninput="ipsuCalc()">
                <span style="font-size:11px;font-weight:700;color:#6d28d9;margin-left:4px" id="ipsu-g1n">=3</span>
              </div></div>
          </span>
          <span id="ipsu-c1" style="display:none">
            <div class="ipsu-fld"><label>1단계 · 2차당 1차 입수</label><input id="ipsu-n1" class="ipsu-num big" type="number" value="3" min="1" placeholder="자동" title="비우면 최대치 자동 계산" oninput="ipsuCalc()"></div>
          </span>
          <div class="ipsu-fld"><label>1단계 배열</label>
            <select id="ipsu-pat1" onchange="ipsuCalc()">
              <option value="row-alt">세워서 · 교대 뒤집기(네스팅)</option>
              <option value="row">세워서 줄로</option>
              <option value="stack">눕혀서 적층</option>
            </select></div>
        </span>
        <span id="ipsu-g2" style="display:contents">
          <div class="ipsu-fld"><label id="ipsu-g2label">2단계 · 박스 안 배열 (열×줄×층)</label>
            <div style="display:flex;gap:3px;align-items:center">
              <input id="ipsu-g2c" class="ipsu-num" type="number" value="5" min="1" oninput="ipsuCalc()">
              <span style="color:var(--text-3)">×</span>
              <input id="ipsu-g2r" class="ipsu-num" type="number" value="2" min="1" oninput="ipsuCalc()">
              <span style="color:var(--text-3)">×</span>
              <input id="ipsu-g2l" class="ipsu-num" type="number" value="1" min="1" oninput="ipsuCalc()">
              <span style="font-size:11px;font-weight:700;color:#6d28d9;margin-left:4px" id="ipsu-g2n">=10</span>
            </div></div>
        </span>
        <span id="ipsu-c2" style="display:none">
          <div class="ipsu-fld"><label id="ipsu-n2label">2단계 · 박스당 2차 입수</label><input id="ipsu-n2" class="ipsu-num big" type="number" value="10" min="1" placeholder="자동" title="비우면 최대치 자동 계산" oninput="ipsuCalc()"></div>
        </span>
        <div class="ipsu-fld"><label id="ipsu-pat2label">2단계 배열</label>
          <select id="ipsu-pat2" onchange="ipsuCalc()">
            <option value="row">세워서 줄로</option>
            <option value="row-alt">세워서 · 교대 뒤집기(네스팅)</option>
            <option value="stack">눕혀서 적층</option>
          </select></div>
        <div class="ipsu-fld"><label>박스입수 조회</label>
          <div class="ipsu-search">
            <input id="ipsu-c-q" type="text" placeholder="완제품 품번/품명…" autocomplete="off"
                   oninput="ipsuSuggest('c')" onblur="ipsuHideSug('c')">
            <div id="ipsu-c-sug" class="ipsu-sug"></div>
          </div></div>
        <div class="ipsu-fld"><label>&nbsp;</label>
          <button onclick="ipsuBackSolveT()" style="padding:5px 9px;font-size:11px;border:1px solid #b45309;
            border-radius:6px;background:#fff;color:#b45309;cursor:pointer;font-weight:600;white-space:nowrap"
            title="현재 입력한 입수가 딱 맞도록 파우치 채움두께(T)를 역산해 넣습니다">📏 입수→두께 역산</button></div>
        <div class="ipsu-fld"><label>&nbsp;</label>
          <button onclick="ipsuSuggestBox()" style="padding:5px 9px;font-size:11px;border:1px solid #7c3aed;
            border-radius:6px;background:#fff;color:#6d28d9;cursor:pointer;font-weight:600;white-space:nowrap"
            title="현재 파우치/용기로 낭비 최소인 외박스 내치수 추천">📐 최적 박스 추천</button></div>
        <div class="ipsu-fld"><label>&nbsp;</label>
          <button onclick="ipsuCalibrate()" style="padding:5px 9px;font-size:11px;border:1px solid #0369a1;
            border-radius:6px;background:#fff;color:#0369a1;cursor:pointer;font-weight:600;white-space:nowrap"
            title="실측 입수와 계산치를 비교해 실제 눌림(압축)률 역산">🎯 실측 캘리브레이션</button></div>
      </div>
      <div id="ipsu-reco" style="display:none;margin-top:8px;padding:8px 10px;background:#fff;
        border:1px solid var(--border-2);border-radius:8px;font-size:11px"></div>
    </div>

    <!-- ⑤ 팔레트 적재 -->
    <div class="ipsu-block" style="border-color:#bae6fd;background:#f0f9ff">
      <div class="ib-t" style="color:#0369a1">⑤ 팔레트 적재</div>
      <div class="ipsu-row">
        <div class="ipsu-fld"><label>팔레트 W×D (mm)</label>
          <div style="display:flex;gap:3px">
            <input id="ipsu-plw" class="ipsu-num" type="number" value="1100" oninput="ipsuCalc()">
            <input id="ipsu-pld" class="ipsu-num" type="number" value="1100" oninput="ipsuCalc()">
          </div></div>
        <div class="ipsu-fld"><label>최대 적재높이</label><input id="ipsu-plh" class="ipsu-num" type="number" value="1700" oninput="ipsuCalc()"></div>
        <div class="ipsu-fld"><label title="③ 외박스 외치수와 자동 연동됩니다">외박스 외치수 W×D×H <span style="color:#0369a1">(③ 자동연동)</span></label>
          <div style="display:flex;gap:3px">
            <input id="ipsu-bow" class="ipsu-num" type="number" value="530" style="background:#f0f9ff" oninput="ipsuCalc()">
            <input id="ipsu-bod" class="ipsu-num" type="number" value="360" style="background:#f0f9ff" oninput="ipsuCalc()">
            <input id="ipsu-boh" class="ipsu-num" type="number" value="255" style="background:#f0f9ff" oninput="ipsuCalc()">
          </div></div>
        <div class="ipsu-fld"><label>오버행 허용</label><input id="ipsu-over" class="ipsu-num" type="number" value="0" oninput="ipsuCalc()"></div>
        <div class="ipsu-fld" style="flex:1;min-width:180px"><label>PT적재량 실측 대조</label>
          <div id="ipsu-pt" style="font-size:10.5px;color:var(--text-3);padding:5px 0">완제품을 검색하면 실측값과 비교됩니다</div></div>
      </div>
    </div>

    <div class="ipsu-stages" id="ipsu-stages">
      <div id="ipsu-stage1">
        <div class="ipsu-sh"><span id="ipsu-s1title">1단계 · 1차파우치 → 2차파우치</span><span class="sc-wrap"><span class="sc-lbl">입수량</span><span class="sc" id="ipsu-s1cnt">0</span><span class="sc-unit">개</span></span></div>
        <div class="ipsu-cv" id="ipsu-cv1"><div class="cv-hint">드래그 회전 · 휠 확대 · 클릭 크게보기</div></div>
        <div class="ipsu-badge" id="ipsu-s1badge"></div>
      </div>
      <div>
        <div class="ipsu-sh"><span id="ipsu-s2title">2단계 · 2차파우치 → 외박스</span><span class="sc-wrap"><span class="sc-lbl">입수량</span><span class="sc" id="ipsu-s2cnt">0</span><span class="sc-unit">개</span></span></div>
        <div class="ipsu-cv" id="ipsu-cv2"><div class="cv-hint">드래그 회전 · 휠 확대 · 클릭 크게보기</div></div>
        <div class="ipsu-badge" id="ipsu-s2badge"></div>
      </div>
      <div>
        <div class="ipsu-sh"><span style="color:#0369a1">🚛 팔레트 적재 (외박스)</span><span class="sc-wrap"><span class="sc-lbl" style="color:#0369a1">적재량</span><span class="sc" id="ipsu-s3cnt" style="color:#0369a1">0</span><span class="sc-unit" style="color:#0369a1">박스</span></span></div>
        <div class="ipsu-cv" id="ipsu-cv3"><div class="cv-hint">드래그 회전 · 휠 확대 · 클릭 크게보기</div></div>
        <div class="ipsu-badge" id="ipsu-s3badge"></div>
      </div>
    </div>

    <!-- 3D 확대 보기 — 클릭한 단계의 캔버스를 통째로 옮겨와 크게 표시 -->
    <div class="ipsu-zoom-overlay" id="ipsu-zoom">
      <div class="ipsu-zoom-box">
        <div class="ipsu-zoom-head">
          <h3 id="ipsu-zoom-title"></h3>
          <button class="zx" onclick="ipsuZoomClose()" title="닫기 (ESC)">&times;</button>
        </div>
        <div class="ipsu-zoom-body" id="ipsu-zoom-holder"></div>
        <div class="ipsu-zoom-sub" id="ipsu-zoom-sub"></div>
      </div>
    </div>

    <div class="ipsu-total">
      <div><div class="lb">외박스 1개당 1차파우치 총 입수</div>
        <div class="wn" id="ipsu-cost">※ 실측 입수 기반 시각화 — 유연 파우치는 눌림/부풂으로 형상 차이 발생</div></div>
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:12px;opacity:.9" id="ipsu-eq">3 × 10 =</span>
        <span class="bg" id="ipsu-total">30 <span style="font-size:13px;font-weight:600">개</span></span></div>
    </div>
  </div>
</section>

<!-- 월 매출·판매 추이 + 발주·입고 추이 (2026-09-23: '매출 현황' 패널을 없애고 이 패널로 통합 — 채널별 보기·월 상품 TOP10은 [채널별] 모드로 흡수) -->
<section class="sales-npd-strip">
  <div class="chart-panel salesbase-panel">
    <div class="chart-head">
      <div><span class="chart-title">🛍 월 매출·판매 추이</span><span class="chart-sub" style="color:#2f8576">온라인팀 판매자료 · 온라인+오프라인 · 공급가 기준</span></div>
      <div style="display:flex;align-items:center;gap:8px">
        <div class="vk-chips" id="sb-mode">
          <button class="vk-chip on" data-m="cls" onclick="setSbMode('cls',this)" title="자사·유상사급·상품매입 · 막대 클릭=분류 상세">분류별</button>
          <button class="vk-chip" data-m="ch" onclick="setSbMode('ch',this)" title="쿠팡·롯데·이마트 등 · 막대 클릭=그 달 상품 TOP 10">채널별</button>
        </div>
        <div id="sb-kpi" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
      </div>
    </div>
    <div class="salesbase-body">
      <div class="salesbase-chart-wrap"><canvas id="salesBaseChart"></canvas></div>
      <div class="salesbase-side">
        <div class="sb-side-title" id="sb-top-title">제품 TOP 10</div>
        <input id="sb-search" type="text" placeholder="품번·품명 검색…" oninput="renderSbList()"
               style="width:100%;padding:6px 10px;margin-bottom:6px;font-size:11.5px;border:1px solid var(--border-2);border-radius:6px;outline:none">
        <div class="sb-list-head"><span class="sb-lh-name">제품 (낱개/월)</span><span class="sb-lh-avg">3개월평균</span><span class="sb-lh-m1">최근1개월</span></div>
        <div id="sb-top-list" class="sb-top-list"><div class="loading" style="padding:12px">로딩 중...</div></div>
      </div>
    </div>
  </div>
  <div class="chart-panel prod-panel">
    <div class="chart-head">
      <div><span class="chart-title">📊 발주 · 입고 추이</span><span class="chart-sub" style="color:#dc2626">아마란스 월별 추이</span></div>
      <div class="chart-nav">
        <button id="or-prev" onclick="moveOr(1)" title="과거로">‹</button>
        <button id="or-next" onclick="moveOr(-1)" title="최근으로">›</button>
      </div>
    </div>
    <div id="or-kpi" class="kpi-row"></div>
    <div class="prod-chart-wrap"><canvas id="orChart"></canvas></div>
  </div>
</section>


<!-- 판매 분석 (2026-09-23): 납품 vs POS 괴리 · 채널 공급단가 변동 · 납품 요일 패턴 -->
<section class="lens-strip">
  <div class="chart-panel lens-gap">
    <div class="chart-head">
      <div><span class="chart-title">🔎 납품 vs 실판매(POS) 괴리</span><span class="chart-sub" id="gap-sub">최근 28일 · POS 보고 채널만 · 클릭=상세</span></div>
      <div id="gap-count" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
    </div>
    <div id="gap-summary" style="font-size:11px;color:var(--text-2);margin-bottom:6px"></div>
    <div id="gap-list" class="alert-list"><div class="loading" style="padding:20px">로딩 중...</div></div>
  </div>
  <div class="chart-panel lens-price">
    <div class="chart-head">
      <div><span class="chart-title">🏷 채널 공급단가 변동</span><span class="chart-sub">최근 6개월 · 3% 이상 · 월 영향액순 · 클릭=상세</span></div>
      <div id="cpc-count" style="font-size:11px;color:var(--text-3);font-weight:600"></div>
    </div>
    <div id="cpc-list" class="alert-list"><div class="loading" style="padding:20px">로딩 중...</div></div>
  </div>
  <div class="chart-panel lens-wd">
    <div class="chart-head">
      <div><span class="chart-title">📅 납품 요일 패턴</span><span class="chart-sub" id="wd-sub">최근 12주</span></div>
    </div>
    <div id="wd-body"><div class="loading" style="padding:20px">로딩 중...</div></div>
  </div>
</section>

<!-- 채팅내역 모달 -->
<div class="modal-overlay" id="history-modal" style="z-index:120">
  <div class="modal" style="max-width:680px">
    <div class="modal-header">
      <div>
        <div class="code-badge" style="background:#4f46e5">💬 채팅내역</div>
        <h2 id="hist-title">최근 AI 검색 기록</h2>
      </div>
      <button class="modal-close" onclick="closeHistory()">&times;</button>
    </div>
    <div class="modal-body" id="hist-body">
      <div class="loading">로딩 중...</div>
    </div>
  </div>
</div>

<div class="wrap">
<div class="main-col">
  <!-- 캘린더 가로 스트립 (외주발주·발주내역·생산실적·생산계획) -->
  <section class="cal-strip">
  <div class="cal-widget t-outsource" data-type="outsource">
    <div class="cal-head">
      <div class="cal-title"><span class="dot"></span>외주발주</div>
      <div class="cal-nav"><button onclick="calNav(this,-1)">‹</button><span class="cal-ym"></span><button onclick="calNav(this,1)">›</button></div>
    </div>
    <div class="cal-body"></div>
    <div class="cal-total"></div>
  </div>
  <div class="cal-widget t-order" data-type="order">
    <div class="cal-head">
      <div class="cal-title"><span class="dot"></span>발주내역</div>
      <div class="cal-nav"><button onclick="calNav(this,-1)">‹</button><span class="cal-ym"></span><button onclick="calNav(this,1)">›</button></div>
    </div>
    <div class="cal-body"></div>
    <div class="cal-total"></div>
  </div>
  <div class="cal-widget t-actual" data-type="actual">
    <div class="cal-head">
      <div class="cal-title"><span class="dot"></span>생산실적</div>
      <div class="cal-nav"><button onclick="calNav(this,-1)">‹</button><span class="cal-ym"></span><button onclick="calNav(this,1)">›</button></div>
    </div>
    <div class="cal-body"></div>
    <div class="cal-total"></div>
  </div>
  <div class="cal-widget t-plan" data-type="plan">
    <div class="cal-head">
      <div class="cal-title"><span class="dot"></span>생산계획</div>
      <div class="cal-nav"><button onclick="calNav(this,-1)">‹</button><span class="cal-ym"></span><button onclick="calNav(this,1)">›</button></div>
    </div>
    <div class="cal-body"></div>
    <div class="cal-total"></div>
  </div>
  </section>
  <!-- 검색창 (타이핑=카드 매칭 · Enter=AI 답변) -->
  <div class="search-bar">
    <span class="search-icon">🔍</span>
    <input type="text" id="search-input" placeholder="품번/품명 검색 · Enter로 AI에게 질문 (연속 질문 가능)" autocomplete="off">
    <button class="search-clear" id="search-clear" onclick="clearSearch()" style="display:none">&times;</button>
    <button class="search-new" id="search-new" onclick="resetConversation()" title="새 대화 시작" style="display:none">🔄 새 대화</button>
  </div>
  <div id="ai-area"></div>
  <div id="search-area"></div>

  <!-- 2026-09-23 사용자 요청: 맨 아래 E/G/H/I 품목 나열 제거. 검색 스크립트가 .tabs/#product-area 표시를 토글하므로 요소는 남기고 retired(!important)로 항상 숨김 -->
  <div class="tabs retired">
    <button class="tab active" data-tab="E">E · 반제품</button>
    <button class="tab" data-tab="G">G · 자사제품</button>
    <button class="tab" data-tab="H">H · 외주제품</button>
    <button class="tab" data-tab="I">I · 외주생산제품</button>
  </div>

  <div id="product-area" class="retired"></div>
</div>
</div>

<!-- 외주 입고 상세 모달 -->
<div class="modal-overlay" id="os-modal" style="z-index:118">
  <div class="modal" style="max-width:1100px">
    <div class="modal-header">
      <div>
        <div class="code-badge" style="background:linear-gradient(135deg,#dc2626,#ef4444)">외주 입고</div>
        <h2>외주 생산 요청 — 입고 상세 일정</h2>
        <div style="font-size:11.5px;color:var(--text-2);margin-top:4px">
          Monday <b>외주 생산 요청</b> · <b>발주 완료</b> 그룹 · 말풍선 <b>남소민</b> 입고예정일
        </div>
      </div>
      <button class="modal-close" onclick="closeOsModal()">&times;</button>
    </div>
    <div class="modal-body" id="os-body">
      <div class="loading">로딩 중...</div>
    </div>
  </div>
</div>

<!-- 데이터 건강검진 모달 -->
<div class="modal-overlay" id="dh-modal" style="z-index:129">
  <div class="modal" style="max-width:620px">
    <div class="modal-header">
      <div>
        <div class="code-badge" id="dh-badge" style="background:#0369a1">데이터 점검</div>
        <h2 id="dh-title" style="font-size:15px">데이터 건강검진</h2>
        <div id="dh-sub" style="font-size:11.5px;color:var(--text-2);margin-top:4px"></div>
      </div>
      <button class="modal-close" onclick="closeDataHealth()">&times;</button>
    </div>
    <div class="modal-body" id="dh-body" style="max-height:62vh;overflow:auto">
      <div class="loading" style="padding:24px">로딩 중...</div>
    </div>
  </div>
</div>

<!-- Ctrl+K 통합 검색 -->
<div class="ck-overlay" id="ck-overlay">
  <div class="ck-box">
    <input id="ck-input" type="text" placeholder="품번·품명 검색 (Ctrl+K)  ·  ↑↓ 이동 · Enter 열기 · Esc 닫기" autocomplete="off">
    <div class="ck-list" id="ck-list"></div>
    <div class="ck-hint">완제품(G/H/I) · 자재(A~D) 모두 검색 · 선택하면 상세 모달이 열립니다</div>
  </div>
</div>

<!-- 알림 센터 모달 -->
<div class="modal-overlay" id="notify-modal" style="z-index:129">
  <div class="modal" style="max-width:min(760px, 94vw)">
    <div class="modal-header">
      <div>
        <div class="code-badge" style="background:linear-gradient(135deg,#475569,#64748b)">알림</div>
        <h2 style="font-size:15px">🔔 알림 센터</h2>
        <div id="notify-sub" style="font-size:11.5px;color:var(--text-2);margin-top:4px"></div>
      </div>
      <button class="modal-close" onclick="document.getElementById('notify-modal').classList.remove('show')">&times;</button>
    </div>
    <div class="modal-body" id="notify-body" style="max-height:60vh;overflow:auto">
      <div class="loading">로딩 중...</div>
    </div>
    <div style="padding:10px 24px 16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <button onclick="notifyRunNow()" style="padding:6px 12px;font-size:11.5px;font-weight:700;border:1px solid #475569;
        border-radius:7px;background:#fff;color:#475569;cursor:pointer">🔄 지금 점검</button>
      <button onclick="notifyRunNow(true)" style="padding:6px 12px;font-size:11.5px;font-weight:700;border:1px solid #0369a1;
        border-radius:7px;background:#fff;color:#0369a1;cursor:pointer">☀️ 요약 보내기</button>
      <button onclick="notifyTest()" style="padding:6px 12px;font-size:11.5px;font-weight:700;border:1px solid #059669;
        border-radius:7px;background:#fff;color:#059669;cursor:pointer">📨 채널 테스트</button>
      <span id="notify-msg" style="font-size:11.5px;color:#059669;font-weight:600"></span>
    </div>
  </div>
</div>

<!-- 청구요청 초안 모달 -->
<div class="modal-overlay" id="pr-draft-modal" style="z-index:129">
  <div class="modal" style="max-width:min(1100px, 94vw)">
    <div class="modal-header">
      <div>
        <div class="code-badge" style="background:linear-gradient(135deg,#e11d48,#f43f5e)">청구요청</div>
        <h2 style="font-size:15px">청구요청 초안 <span id="pr-draft-date" style="font-size:12px;color:var(--text-2);font-weight:600"></span></h2>
        <div style="font-size:11.5px;color:var(--text-2);margin-top:4px">
          수량·납기 수정 후 <b>복사</b> → 아마란스 청구요청 화면에 붙여넣기
          <span style="color:#b45309">(API 직접등록은 더존 개통 대기중)</span></div>
      </div>
      <button class="modal-close" onclick="document.getElementById('pr-draft-modal').classList.remove('show')">&times;</button>
    </div>
    <div class="modal-body" id="pr-draft-body" style="max-height:64vh;overflow:auto">
      <div class="loading">로딩 중...</div>
    </div>
    <div style="padding:10px 24px 16px;display:flex;gap:8px;align-items:center">
      <button onclick="copyPrDraft()" style="padding:7px 16px;font-size:12.5px;font-weight:700;border:none;
        border-radius:8px;background:#e11d48;color:#fff;cursor:pointer">📋 선택 항목 복사</button>
      <span id="pr-draft-msg" style="font-size:11.5px;color:#059669;font-weight:600"></span>
    </div>
  </div>
</div>

<!-- 차트 TOP 10 상세 모달 -->
<div class="modal-overlay" id="chart-detail-modal" style="z-index:128">
  <div class="modal" style="max-width:min(1200px, 92vw)">
    <div class="modal-header">
      <div>
        <div class="code-badge" id="cd-badge" style="background:linear-gradient(135deg,#0ea5e9,#38bdf8)">상세</div>
        <h2 id="cd-title" style="font-size:15px">상세</h2>
        <div id="cd-sub" style="font-size:11.5px;color:var(--text-2);margin-top:4px"></div>
      </div>
      <button class="modal-close" onclick="closeChartDetail()">&times;</button>
    </div>
    <div class="modal-body" id="cd-body" style="max-height:62vh;overflow:auto">
      <div class="loading" style="padding:24px">로딩 중...</div>
    </div>
  </div>
</div>

<!-- 시방서 미리보기 모달 -->
<div class="modal-overlay" id="spec-preview-modal" style="z-index:130">
  <div class="modal">
    <div class="modal-header">
      <div>
        <div style="display:flex;align-items:center;gap:6px">
          <div class="code-badge" style="background:linear-gradient(135deg,#0369a1,#0ea5e9)">시방서</div>
          <span style="font-size:10px;font-weight:700;color:#64748b;background:#f1f5f9;border:1px solid #e2e8f0;padding:2px 8px;border-radius:6px">🔒 읽기 전용</span>
        </div>
        <h2 id="spec-preview-title" style="font-size:15px">시방서 미리보기</h2>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <a id="spec-preview-dl" href="#" target="_blank" rel="noopener"
           style="font-size:12px;font-weight:700;color:#fff;background:#0369a1;padding:7px 14px;border-radius:8px;text-decoration:none">⬇ 다운로드</a>
        <button class="modal-close" onclick="closeSpecPreview()">&times;</button>
      </div>
    </div>
    <div id="spec-preview-content" style="flex:1;display:flex;overflow:auto;min-height:300px">
      <div class="loading" style="margin:auto">로딩 중...</div>
    </div>
  </div>
</div>

<!-- 발주완료 모달 -->
<div class="modal-overlay" id="po-modal" style="z-index:118">
  <div class="modal" style="max-width:1100px">
    <div class="modal-header">
      <div>
        <div class="code-badge" style="background:linear-gradient(135deg,#059669,#10b981)">발주완료</div>
        <h2>원/부자재 발주 완료 — 입고 일정</h2>
        <div style="font-size:11.5px;color:var(--text-2);margin-top:4px">
          Monday <b>원/부자재 발주 요청</b> 보드의 <b>발주 완료</b> 그룹. 말풍선(업데이트)이 입고 일정이며 비어있으면 <span style="color:var(--danger);font-weight:700">미정</span>
        </div>
      </div>
      <button class="modal-close" onclick="closePoPending()">&times;</button>
    </div>
    <div class="modal-body" id="po-body">
      <div class="loading">로딩 중...</div>
    </div>
  </div>
</div>

<!-- 발주완료 말풍선 팝업 -->
<div class="modal-overlay" id="po-upd-modal" style="z-index:120">
  <div class="modal" style="max-width:560px">
    <div class="modal-header">
      <div>
        <div class="code-badge" style="background:linear-gradient(135deg,#059669,#10b981)">💬 말풍선</div>
        <h2 id="po-upd-title" style="font-size:16px"></h2>
        <div style="font-size:11.5px;color:var(--text-2);margin-top:4px">Monday <b>원/부자재 발주 요청</b> 보드의 업데이트 원문</div>
      </div>
      <button class="modal-close" onclick="closePoUpdates()">&times;</button>
    </div>
    <div class="modal-body" id="po-upd-body"></div>
  </div>
</div>

<!-- 캘린더 상세 모달 -->
<div class="modal-overlay" id="cal-modal" style="z-index:115">
  <div class="modal" style="max-width:min(1400px, 94vw)">
    <div class="modal-header">
      <div>
        <div class="code-badge" id="cal-badge" style="background:linear-gradient(135deg,#4f46e5,#7c3aed)"></div>
        <h2 id="cal-title"></h2>
      </div>
      <button class="modal-close" onclick="closeCalModal()">&times;</button>
    </div>
    <div style="padding:10px 24px 0">
      <input id="cal-search" type="text" placeholder="품번·품명 검색 — 입력하면 날짜와 무관하게 전체 이력이 표시됩니다"
             autocomplete="off" oninput="calSearchInput()"
             style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;
                    font-size:13px;background:var(--bg);color:var(--text)">
    </div>
    <div class="modal-body" id="cal-body">
      <div class="loading">로딩 중...</div>
    </div>
  </div>
</div>

<!-- 원/부자재 입고일정 캘린더 모달 -->
<div class="modal-overlay" id="po-cal-modal" style="z-index:116">
  <div class="modal" style="max-width:1140px">
    <div class="modal-header">
      <div>
        <div class="code-badge" style="background:linear-gradient(135deg,#0ea5e9,#0369a1)">입고일정</div>
        <h2>원/부자재 예상입고 캘린더</h2>
      </div>
      <div class="pocal-search-wrap">
        <span class="pocal-search-ico">🔍</span>
        <input id="pocal-search" type="text" placeholder="품번·품명·업체·계약번호 검색…" oninput="onPoCalSearch()" autocomplete="off">
        <span id="pocal-search-hit"></span>
      </div>
      <button class="modal-close" onclick="closePoCalModal()">&times;</button>
    </div>
    <div class="modal-body" style="max-width:1140px">
      <div class="pocal-wrap">
        <div class="pocal-cal">
          <div class="pocal-nav">
            <button onclick="poCalNav(-1)">‹</button>
            <span class="pocal-ym" id="pocal-ym"></span>
            <button onclick="poCalNav(1)">›</button>
          </div>
          <div class="pocal-grid" id="pocal-grid"></div>
          <div class="pocal-legend">
            <span><span class="pocal-dot po"></span>부자재</span>
            <span><span class="pocal-dot raw"></span>원료</span>
            <span><span class="pocal-dot import_raw"></span>수입</span>
          </div>
          <div class="pocal-mini" id="pocal-mini"></div>
        </div>
        <div class="pocal-detail" id="pocal-detail">
          <div class="pocal-empty">날짜를 클릭하면 입고 예정 품목이 표시됩니다</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- 외주 입고일정 캘린더 모달 -->
<div class="modal-overlay" id="os-cal-modal" style="z-index:116">
  <div class="modal" style="max-width:1140px">
    <div class="modal-header">
      <div>
        <div class="code-badge" style="background:linear-gradient(135deg,#7c3aed,#5b21b6)">외주입고</div>
        <h2>외주 입고 물량 캘린더</h2>
      </div>
      <div class="pocal-search-wrap">
        <span class="pocal-search-ico">🔍</span>
        <input id="oscal-search" type="text" placeholder="품번·품명·업체 검색…" oninput="onOsCalSearch()" autocomplete="off">
        <span id="oscal-search-hit"></span>
      </div>
      <button class="modal-close" onclick="closeOsCalModal()">&times;</button>
    </div>
    <div class="modal-body" style="max-width:1140px">
      <div class="pocal-wrap">
        <div class="pocal-cal">
          <div class="pocal-nav">
            <button onclick="osCalNav(-1)">‹</button>
            <span class="pocal-ym" id="oscal-ym"></span>
            <button onclick="osCalNav(1)">›</button>
          </div>
          <div class="pocal-grid" id="oscal-grid"></div>
          <div class="pocal-legend">
            <span><span class="pocal-dot os-done"></span>발주완료</span>
            <span><span class="pocal-dot os-req"></span>생산요청</span>
          </div>
          <div class="pocal-mini" id="oscal-mini"></div>
        </div>
        <div class="pocal-detail" id="oscal-detail">
          <div class="pocal-empty">날짜를 클릭하면 입고 예정 품목이 표시됩니다</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- 상세 모달 (BOM) -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <div class="modal-header">
      <div>
        <div class="code-badge" id="m-code"></div>
        <h2 id="m-name"></h2>
      </div>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-body" id="m-body">
      <div class="loading">로딩 중...</div>
    </div>
  </div>
</div>

<!-- 부재료 상세 모달 -->
<div class="modal-overlay" id="item-modal" style="z-index:110">
  <div class="modal" style="max-width:860px">
    <div class="modal-header">
      <div>
        <div class="code-badge" id="i-code" style="background:#059669"></div>
        <h2 id="i-name"></h2>
      </div>
      <button class="modal-close" onclick="closeItemModal()">&times;</button>
    </div>
    <div class="modal-body" id="i-body">
      <div class="loading">로딩 중...</div>
    </div>
  </div>
</div>


<script>
  firebase.initializeApp({
    apiKey: "AIzaSyBZ1FfTibE-KBkTbZJnTNEqz-pxsgew03k",
    authDomain: "maehong-scm.firebaseapp.com",
    projectId: "maehong-scm",
    storageBucket: "maehong-scm.firebasestorage.app",
    messagingSenderId: "776997651051",
    appId: "1:776997651051:web:8734392ca2e791b5fb272e"
  });
  const fbDb = firebase.firestore();   // shared(공개) 읽기 등 비인증 용도만 사용
  // 서버 세션 사용자 (구글 서버측 OAuth로 로그인). 게스트/미로그인이면 null.
  const SERVER_USER = {{ user_json|safe }};
  let currentUser = SERVER_USER;

  function doLogin() { location.href = '/auth/google'; }
  function doLogout() { location.href = '/auth/logout'; }

  // ───── Monday 새로고침 ─────
  let _refreshPollTimer = null;
  async function triggerMondayRefresh() {
    const btn = document.getElementById('refresh-monday-btn');
    const label = document.getElementById('refresh-label');
    if (btn.classList.contains('refreshing')) return;
    btn.classList.remove('success', 'error');
    btn.classList.add('refreshing');
    btn.disabled = true;
    label.textContent = '수집 중...';
    try {
      const r = await fetch('/api/refresh_monday', { method: 'POST' });
      const j = await r.json();
      if (!j.started && j.reason === 'already_running') {
        label.textContent = '이미 실행 중...';
      }
    } catch (e) {
      btn.classList.remove('refreshing');
      btn.classList.add('error');
      btn.disabled = false;
      label.textContent = '실패';
      return;
    }
    pollRefreshStatus();
  }

  function pollRefreshStatus() {
    if (_refreshPollTimer) clearInterval(_refreshPollTimer);
    _refreshPollTimer = setInterval(async () => {
      try {
        const r = await fetch('/api/refresh_monday/status');
        const s = await r.json();
        if (!s.running) {
          clearInterval(_refreshPollTimer); _refreshPollTimer = null;
          const btn = document.getElementById('refresh-monday-btn');
          const label = document.getElementById('refresh-label');
          btn.classList.remove('refreshing');
          btn.disabled = false;
          if (s.last_status === 'success') {
            btn.classList.add('success');
            label.textContent = '갱신 완료 ' + (s.last_run || '').slice(11, 16);
            setTimeout(() => { btn.classList.remove('success'); label.textContent = 'Monday 새로고침'; }, 5000);
            // 패널 자동 새로고침
            if (typeof loadPoInline === 'function') loadPoInline();
            if (typeof loadOsInline === 'function') loadOsInline();
          } else {
            btn.classList.add('error');
            label.textContent = '실패';
            setTimeout(() => { btn.classList.remove('error'); label.textContent = 'Monday 새로고침'; }, 8000);
          }
        }
      } catch (e) { /* keep polling */ }
    }, 3000);
  }

  // 페이지 로드 시 진행 중 갱신 있는지 확인
  window.addEventListener('load', async () => {
    try {
      const r = await fetch('/api/refresh_monday/status');
      const s = await r.json();
      if (s.running) {
        const btn = document.getElementById('refresh-monday-btn');
        const label = document.getElementById('refresh-label');
        btn.classList.add('refreshing'); btn.disabled = true;
        label.textContent = '수집 중...';
        pollRefreshStatus();
      }
    } catch (e) {}
    try {
      const r = await fetch('/api/refresh_aramanth/status');
      const s = await r.json();
      if (s.running) {
        const btn = document.getElementById('refresh-aramanth-btn');
        const label = document.getElementById('refresh-aramanth-label');
        btn.classList.add('refreshing'); btn.disabled = true;
        label.textContent = '수집 중...';
        pollAramanthStatus();
      }
    } catch (e) {}
  });

  // ───── 메모리 리로드 (fetch 없이 디스크만) ─────
  async function triggerReloadDfs() {
    const btn = document.getElementById('reload-dfs-btn');
    const label = document.getElementById('reload-dfs-label');
    if (btn.disabled) return;
    btn.disabled = true;
    const orig = label.textContent;
    label.textContent = '리로드 중...';
    try {
      const r = await fetch('/api/reload_dfs', { method: 'POST' });
      const j = await r.json();
      if (j.ok) {
        label.textContent = '리로드 완료 (' + j.loaded + ')';
        if (j.failed && j.failed.length) {
          console.warn('리로드 실패 항목:', j.failed);
        }
        setTimeout(() => { label.textContent = orig; btn.disabled = false; }, 3000);
      } else {
        label.textContent = '실패';
        setTimeout(() => { label.textContent = orig; btn.disabled = false; }, 3000);
      }
    } catch (e) {
      label.textContent = '실패';
      setTimeout(() => { label.textContent = orig; btn.disabled = false; }, 3000);
    }
  }

  // ───── 아마란스 새로고침 ─────
  let _aramanthPollTimer = null;
  async function triggerAramanthRefresh() {
    const btn = document.getElementById('refresh-aramanth-btn');
    const label = document.getElementById('refresh-aramanth-label');
    if (btn.classList.contains('refreshing')) return;
    if (!confirm('아마란스 fetch는 10~15분 걸려요. 시작할까요?')) return;
    btn.classList.remove('success', 'error');
    btn.classList.add('refreshing');
    btn.disabled = true;
    label.textContent = '수집 중... (10~15분)';
    try {
      const r = await fetch('/api/refresh_aramanth', { method: 'POST' });
      const j = await r.json();
      if (!j.started && j.reason === 'already_running') {
        label.textContent = '이미 실행 중...';
      }
    } catch (e) {
      btn.classList.remove('refreshing'); btn.classList.add('error'); btn.disabled = false;
      label.textContent = '실패';
      return;
    }
    pollAramanthStatus();
  }

  function pollAramanthStatus() {
    if (_aramanthPollTimer) clearInterval(_aramanthPollTimer);
    _aramanthPollTimer = setInterval(async () => {
      try {
        const r = await fetch('/api/refresh_aramanth/status');
        const s = await r.json();
        if (!s.running) {
          clearInterval(_aramanthPollTimer); _aramanthPollTimer = null;
          const btn = document.getElementById('refresh-aramanth-btn');
          const label = document.getElementById('refresh-aramanth-label');
          btn.classList.remove('refreshing'); btn.disabled = false;
          if (s.last_status === 'success') {
            btn.classList.add('success');
            label.textContent = '갱신 완료 ' + (s.last_run || '').slice(11, 16);
            setTimeout(() => { btn.classList.remove('success'); label.textContent = '아마란스 새로고침'; }, 5000);
          } else {
            btn.classList.add('error');
            label.textContent = '실패';
            setTimeout(() => { btn.classList.remove('error'); label.textContent = '아마란스 새로고침'; }, 8000);
          }
        }
      } catch (e) {}
    }, 5000);
  }
  // localhost 또는 사내망에서는 네트워크 오류로 구글 로그인 불가 → 자동 우회
  function skipLogin() {
    const overlay = document.getElementById('login-overlay');
    const chip = document.getElementById('user-chip');
    if (overlay) overlay.style.display = 'none';
    if (chip) { chip.style.display = 'flex'; }
    const nameEl = document.getElementById('user-name');
    const photoEl = document.getElementById('user-photo');
    if (nameEl) nameEl.textContent = '사내 사용자';
    if (photoEl) photoEl.src = 'https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg';
  }
  // 서버 세션 기반 사용자 표시 (로그인 게이트는 서버 /login 이 처리)
  (function initUserChip(){
    const overlay = document.getElementById('login-overlay');
    if (overlay) overlay.style.display = 'none';
    const chip = document.getElementById('user-chip');
    if (chip) chip.style.display = 'flex';
    const nameEl = document.getElementById('user-name');
    const photoEl = document.getElementById('user-photo');
    if (SERVER_USER) {
      if (nameEl) nameEl.textContent = SERVER_USER.name || SERVER_USER.email || '사용자';
      if (photoEl && SERVER_USER.picture) photoEl.src = SERVER_USER.picture;
    } else {
      if (nameEl) nameEl.textContent = '게스트';
    }
  })();

  // ── 데이터 자동 새로고침 (Monday 실시간 반영) — 45초마다 버전 확인 ──
  (function dataAutoRefresh(){
    let _ver = null;
    async function check(){
      try {
        const r = await fetch('/api/data_version', {cache:'no-store'});
        const d = await r.json();
        if (_ver === null) { _ver = d.version; return; }
        if (d.version !== _ver) {
          _ver = d.version;
          // 2026-09-23: 자동 새로고침 제거 — 보던 화면이 갑자기 바뀌는 불편(사용자 요청). 항상 배너만 띄우고 클릭 시 새로고침.
          showBanner();
        }
      } catch(e){}
    }
    function showBanner(){
      if (document.getElementById('data-reload-banner')) return;
      const b = document.createElement('div');
      b.id = 'data-reload-banner';
      b.textContent = '🔄 새 데이터 도착 — 클릭하여 새로고침';
      b.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#4f46e5;color:#fff;padding:10px 18px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.25);z-index:9999';
      b.onclick = () => location.reload();
      b.title = '클릭하면 최신 데이터로 새로고침됩니다';
      document.body.appendChild(b);
    }
    setInterval(check, 45000);
    check();
  })();

  async function saveHistory(question, answer) {
    if (!currentUser) return;
    try {
      await fetch('/api/history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, answer }),
      });
    } catch (e) { console.warn('기록 저장 실패', e); }
  }

  async function openHistory() {
    const modal = document.getElementById('history-modal');
    modal.classList.add('show');
    const body = document.getElementById('hist-body');
    if (!currentUser) {
      body.innerHTML = '<div class="loading">로그인 후 이용 가능합니다</div>';
      return;
    }
    body.innerHTML = '<div class="loading">로딩 중...</div>';
    try {
      const res = await fetch('/api/history');
      const docs = await res.json();
      if (!Array.isArray(docs) || docs.length === 0) {
        body.innerHTML = '<div class="loading">저장된 채팅내역이 없습니다</div>';
        return;
      }
      let html = '<div style="display:flex;flex-direction:column;gap:8px">';
      docs.slice(0, 50).forEach(({ id, ts, question, answer }) => {
        html += '<div style="border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px;background:#f8fafc">'
          + '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px">'
          + '<div style="flex:1;cursor:pointer" onclick="replayQuestion(' + JSON.stringify(question).replace(/"/g,'&quot;') + ')">'
          + '<div style="font-size:14px;font-weight:600;color:#4f46e5;margin-bottom:4px">🤖 ' + escapeHtml(question) + '</div>'
          + '<div style="font-size:11px;color:#64748b">' + ts + '</div>'
          + '</div>'
          + '<button onclick="deleteHistory(\\'' + id + '\\')" style="background:none;border:none;color:#cbd5e1;cursor:pointer;font-size:16px" title="삭제">&times;</button>'
          + '</div>'
          + '<details style="margin-top:8px"><summary style="font-size:11px;color:#64748b;cursor:pointer">답변 보기</summary>'
          + '<div style="font-size:13px;margin-top:8px;color:#334155;line-height:1.6">' + marked.parse(answer) + '</div>'
          + '</details>'
          + '</div>';
      });
      html += '</div>';
      body.innerHTML = html;
    } catch (e) {
      body.innerHTML = '<div class="loading" style="color:#ef4444">로드 실패: ' + escapeHtml(e.message) + '</div>';
    }
  }

  function closeHistory() {
    document.getElementById('history-modal').classList.remove('show');
  }

  async function deleteHistory(id) {
    if (!confirm('삭제하시겠습니까?')) return;
    try {
      await fetch('/api/history/' + id, { method: 'DELETE' });
      openHistory();
    } catch (e) { alert('삭제 실패: ' + e.message); }
  }

  function replayQuestion(q) {
    closeHistory();
    searchInput.value = q;
    askAI(q);
  }

  document.addEventListener('DOMContentLoaded', () => {
    const hm = document.getElementById('history-modal');
    if (hm) hm.addEventListener('click', (e) => {
      if (e.target.id === 'history-modal') closeHistory();
    });
  });

  let PRODUCTS = { E: [], G: [], H: [], I: [] };
  let CURRENT_TAB = 'E';

  async function loadProducts() {
    try {
      const res = await fetch('/api/products');
      PRODUCTS = await res.json();
      renderTab(CURRENT_TAB);
    } catch (e) {
      document.getElementById('product-area').innerHTML = '<div class="loading">로드 실패: ' + e.message + '</div>';
    }
  }

  function categorize(name) {
    const n = String(name || '');
    if (n.includes('누룽지')) return '누룽지';
    if (n.includes('말랭이') || (n.includes('고구마') && !n.includes('스틱'))) return '고구마말랭이';
    return '기타';
  }

  function renderTab(tab) {
    CURRENT_TAB = tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    const list = PRODUCTS[tab] || [];
    const area = document.getElementById('product-area');
    const labels = { G: '자사제품', H: '외주제품', I: '외주생산제품', E: '반제품' };
    if (!list.length) {
      area.innerHTML = '<div class="loading">' + tab + ' 품번이 없습니다</div>';
      return;
    }

    const groups = { '누룽지': [], '고구마말랭이': [], '기타': [] };
    list.forEach(p => { groups[categorize(p.name)].push(p); });

    let html = '<div style="font-size:13px;color:#64748b;margin-bottom:16px">' + tab + ' · ' + labels[tab] + ' — 총 ' + list.length + '개</div>';

    ['누룽지', '고구마말랭이', '기타'].forEach(cat => {
      const items = groups[cat];
      if (!items.length) return;
      const icon = cat === '누룽지' ? '🍚' : (cat === '고구마말랭이' ? '🍠' : '📦');
      html += '<div class="section-title">' + icon + ' ' + cat + ' <span class="count">' + items.length + '개</span></div>';
      html += '<div class="grid" style="margin-bottom:24px">';
      items.forEach(p => {
        const noBom = (p.has_bom === false);
        const badge = noBom
          ? '<span style="font-size:10px;font-weight:700;color:#b45309;background:#fef3c7;border:1px solid #fde68a;border-radius:5px;padding:1px 6px;margin-left:6px;white-space:nowrap">BOM 미등록</span>'
          : '';
        html += '<div class="card' + (noBom ? ' no-bom' : '') + '" onclick="openModal(\\'' + p.code + '\\')"' + (noBom ? ' style="opacity:.82"' : '') + '>'
          + '<div class="code">' + p.code + badge + '</div>'
          + '<div class="name">' + escapeHtml(p.name) + '</div>'
          + '</div>';
      });
      html += '</div>';
    });

    area.innerHTML = html;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
  }

  function fmtNum(n) {
    if (n === null || n === undefined || isNaN(n)) return '-';
    return Number(n).toLocaleString('ko-KR', { maximumFractionDigits: 2 });
  }

  let CURRENT_BOM = null;

  async function openModal(code) {
    const modal = document.getElementById('modal');
    modal.classList.add('show');
    document.getElementById('m-code').textContent = code;
    document.getElementById('m-name').textContent = '';
    document.getElementById('m-body').innerHTML = '<div class="loading">BOM 로딩 중...</div>';
    // BOM 미등록 제품: 서버 조회 없이 안내
    const _prod = Object.values(PRODUCTS || {}).flat().find(p => p.code === code);
    if (_prod && _prod.has_bom === false) {
      document.getElementById('m-name').textContent = _prod.name || '';
      document.getElementById('m-body').innerHTML =
        '<div class="loading" style="color:#b45309;line-height:1.7">이 제품은 <b>BOM(자재명세서)이 등록되어 있지 않습니다.</b><br>'
        + '아마란스 ERP에 BOM을 등록하면 자재 구성·소요량·재고가 표시됩니다.</div>';
      return;
    }
    try {
      const res = await fetch('/api/bom/' + encodeURIComponent(code));
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      CURRENT_BOM = data;
      document.getElementById('m-name').textContent = data.name;
      renderBom(0);
    } catch (e) {
      document.getElementById('m-body').innerHTML = '<div class="loading" style="color:#ef4444">오류: ' + e.message + '</div>';
    }
  }

  function renderBom(qty) {
    const data = CURRENT_BOM;
    if (!data) return;
    qty = Math.max(0, Number(qty) || 0);

    // 패널(입력창)은 한 번만 렌더, 결과 영역만 갱신
    let html = '<div class="prod-panel">'
      + '<div class="prod-row">'
      + '<label>🏭 생산수량</label>'
      + '<input type="text" inputmode="numeric" id="prod-qty" value="' + qty + '" onfocus="this.select()">'
      + '<button onclick="prodQtyBackspace()" title="한 자리 지우기" style="padding:6px 10px;font-size:14px;border:1px solid #cbd5e1;border-radius:6px;background:#f8fafc;cursor:pointer;line-height:1;color:#64748b;margin-left:2px;">⌫</button>'
      + '<div class="prod-unit">' + escapeHtml(data.name.slice(0, 24)) + (data.name.length > 24 ? '…' : '') + ' 생산시</div>'
      + '<div class="prod-preset">'
      + '<button onclick="setQty(100)">100</button>'
      + '<button onclick="setQty(500)">500</button>'
      + '<button onclick="setQty(1000)">1,000</button>'
      + '<button onclick="setQty(5000)">5,000</button>'
      + '</div>'
      + '</div>'
      + '<div id="prod-summary-area"></div>'
      + '</div>'
      + '<div id="bom-result-area"></div>';

    document.getElementById('m-body').innerHTML = html;

    const input = document.getElementById('prod-qty');
    input.addEventListener('input', (e) => {
      const raw = e.target.value.replace(/[^0-9]/g, '');
      if (raw !== e.target.value) e.target.value = raw;
      const v = Number(raw) || 0;
      updateBomResult(v);
    });
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    updateBomResult(qty);
  }

  function updateBomResult(qty) {
    const data = CURRENT_BOM;
    if (!data) return;
    qty = Math.max(0, Number(qty) || 0);

    let totalRequiredCost = 0;
    let shortageCount = 0;
    data.items.forEach(it => {
      const req = it.qty * qty;
      totalRequiredCost += (it.price || 0) * req;
      if (qty > 0 && req > it.totalStock) shortageCount++;
    });
    document.getElementById('prod-summary-area').innerHTML =
      '<div class="prod-summary">'
      + '<div><span class="label">총 소요 비용</span><b>' + fmtNum(Math.round(totalRequiredCost)) + '원</b></div>'
      + '<div><span class="label">부족 품목</span><b style="color:' + (shortageCount ? '#dc2626' : '#059669') + '">' + shortageCount + '개</b></div>'
      + '<div><span class="label">자품목</span><b>' + data.items.length + '개</b></div>'
      + '</div>';

    let html = '<div style="margin-bottom:8px;font-size:12px;color:#64748b">재고 출처: ' + (data.isJasa ? '자사재고' : '외주재고(재고일지)') + '</div>';
    html += '<table><thead><tr>'
      + '<th style="width:36px">#</th>'
      + '<th>자품번 / 품명</th>'
      + '<th style="width:64px">구분</th>'
      + '<th class="num" style="width:90px">단위소요</th>'
      + '<th class="num" style="width:100px">필요량</th>'
      + '<th class="num" style="width:90px">단가</th>'
      + '<th class="num" style="width:110px">필요 비용</th>'
      + '<th class="num" style="width:90px">총재고</th>'
      + '<th class="num" style="width:100px">과부족</th>'
      + '<th style="width:220px">외주처별 재고</th>'
      + '</tr></thead><tbody>';

    data.items.forEach(it => {
      const req = it.qty * qty;
      const reqCost = (it.price || 0) * req;
      const diff = it.totalStock - req;
      const isShortage = qty > 0 && diff < 0;
      let vendorHtml = '';
      if (it.vendorStocks.length) {
        vendorHtml = it.vendorStocks.map(v =>
          '<span class="chip">' + escapeHtml(v.vendor) + '<span class="q">' + fmtNum(v.qty) + '</span></span>'
        ).join(' ');
      } else {
        vendorHtml = '<span style="color:#cbd5e1;font-size:12px">재고 없음</span>';
      }
      html += '<tr' + (isShortage ? ' class="row-short"' : '') + '>'
        + '<td>' + it.seq + '</td>'
        + '<td style="cursor:pointer" onclick="openItemModal(\\'' + it.code + '\\')"><b style="color:#059669;text-decoration:underline">' + escapeHtml(it.code) + '</b><div style="font-size:12px;color:#64748b;margin-top:2px">' + escapeHtml(it.name) + '</div></td>'
        + '<td><span class="cat-badge">' + escapeHtml(it.category) + '</span></td>'
        + '<td class="num">' + fmtNum(it.qty) + ' ' + escapeHtml(it.unit) + '</td>'
        + '<td class="num"><b>' + fmtNum(req) + '</b></td>'
        + '<td class="num">' + (it.price ? fmtNum(it.price) + '원' : '-') + '</td>'
        + '<td class="num">' + (reqCost ? fmtNum(Math.round(reqCost)) + '원' : '-') + '</td>'
        + '<td class="num">' + fmtNum(it.totalStock) + '</td>'
        + '<td class="num"><b style="color:' + (isShortage ? '#dc2626' : (qty > 0 ? '#059669' : '#64748b')) + '">' + (qty > 0 ? (diff >= 0 ? '+' : '') + fmtNum(diff) : '-') + '</b></td>'
        + '<td>' + vendorHtml + '</td>'
        + '</tr>';
    });
    html += '</tbody></table>';
    document.getElementById('bom-result-area').innerHTML = html;
  }

  function prodQtyBackspace() {
    const input = document.getElementById('prod-qty');
    if (!input) return;
    const v = input.value.replace(/[^0-9]/g, '');
    input.value = v.slice(0, -1) || '';
    input.dispatchEvent(new Event('input'));
    input.focus();
  }

  function setQty(n) {
    const input = document.getElementById('prod-qty');
    if (!input) return;
    const current = Number(input.value) || 0;
    const next = current + n;
    input.value = next;
    input.focus();
    input.setSelectionRange(String(next).length, String(next).length);
    updateBomResult(next);
  }

  function closeModal() {
    document.getElementById('modal').classList.remove('show');
  }

  async function openItemModal(code) {
    const modal = document.getElementById('item-modal');
    // 다른 모달(거래처 상세 등) 위에서 열릴 때 뒤로 깔리지 않도록 z-index를 현재 최상위 모달보다 높게
    let topZ = 110;
    document.querySelectorAll('.modal-overlay.show').forEach(o => {
      if (o === modal) return;
      const z = parseInt(getComputedStyle(o).zIndex, 10);
      if (!isNaN(z) && z >= topZ) topZ = z + 1;
    });
    modal.style.zIndex = topZ;
    modal.classList.add('show');
    document.getElementById('i-code').textContent = code;
    document.getElementById('i-name').textContent = '';
    document.getElementById('i-body').innerHTML = '<div class="loading">로딩 중...</div>';
    try {
      const res = await fetch('/api/item/' + encodeURIComponent(code));
      const d = await res.json();
      if (d.error) throw new Error(d.error);
      document.getElementById('i-name').textContent = d.name;

      let html = '';
      // 요약 카드
      html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:18px">';
      html += infoCard('구분', (d.category || '-') + (d.unit ? ' / ' + d.unit : ''));
      html += infoCard('현재 단가', d.price && d.price['단가'] ? fmtNum(d.price['단가']) + '원' : '-');
      html += infoCard('총 재고', fmtNum(d.totalStock));
      html += infoCard('외주처 수', d.vendorStocks.length + '곳');
      html += '</div>';

      // 규격 정보
      if (d.spec) {
        html += '<h3 style="font-size:14px;margin:8px 0 10px;color:#334155">📐 규격 정보</h3>';
        html += '<table style="margin-bottom:18px">';
        html += infoRow('외주업체', d.spec['외주업체명']);
        html += infoRow('납품처', d.spec['납품처']);
        html += infoRow('규격(사이즈)', d.spec['규격(사이즈)']);
        html += infoRow('재질', d.spec['재질']);
        html += infoRow('MOQ', d.spec['MOQ']);
        html += infoRow('단가', d.spec['단가(원)'] ? fmtNum(d.spec['단가(원)']) + '원' : '-');
        html += infoRow('중량', d.spec['중량(g)'] ? d.spec['중량(g)'] + 'g' : '-');
        html += '</table>';
      }

      // 단가 상세
      if (d.price) {
        html += '<h3 style="font-size:14px;margin:8px 0 10px;color:#334155">💰 단가 상세</h3>';
        html += '<table style="margin-bottom:18px">';
        html += infoRow('거래처', d.price['거래처'] || '-');
        html += infoRow('단가', d.price['단가'] ? fmtNum(d.price['단가']) + '원' : '-');
        html += infoRow('기준년월', d.price['기준년월'] || '-');
        html += infoRow('구분', d.price['시트종류'] || '-');
        html += '</table>';
      }

      // 자사재고
      if (d.jasaStock) {
        html += '<h3 style="font-size:14px;margin:8px 0 10px;color:#334155">🏭 자사재고</h3>';
        html += '<table style="margin-bottom:18px">';
        html += infoRow('업체', d.jasaStock.vendor || '-');
        html += infoRow('총재고', fmtNum(d.jasaStock.qty));
        html += '</table>';
      }

      // 외주처별 재고 상세
      if (d.vendorStocks.length) {
        html += '<h3 style="font-size:14px;margin:8px 0 10px;color:#334155">📦 외주처별 재고</h3>';
        html += '<table style="margin-bottom:18px"><thead><tr>'
          + '<th>외주처</th><th class="num">재고수량</th><th class="num">재고금액</th>'
          + '</tr></thead><tbody>';
        let sumQty = 0, sumCost = 0;
        d.vendorStocks.forEach(v => {
          sumQty += v.qty; sumCost += v.cost || 0;
          html += '<tr><td>' + escapeHtml(v.vendor) + '</td>'
            + '<td class="num"><b>' + fmtNum(v.qty) + '</b></td>'
            + '<td class="num">' + (v.cost ? fmtNum(v.cost) + '원' : '-') + '</td></tr>';
        });
        html += '<tr style="background:#f1f5f9;font-weight:700">'
          + '<td>합계</td>'
          + '<td class="num">' + fmtNum(sumQty) + '</td>'
          + '<td class="num">' + (sumCost ? fmtNum(sumCost) + '원' : '-') + '</td>'
          + '</tr>';
        html += '</tbody></table>';
      }

      // 최근 발주 내역
      if (d.orders.length) {
        html += '<h3 style="font-size:14px;margin:8px 0 10px;color:#334155">📋 최근 발주 (최대 5건)</h3>';
        html += '<table style="margin-bottom:18px"><thead><tr>'
          + '<th style="width:100px">발주일</th><th>거래처</th><th class="num">수량</th><th class="num">단가</th>'
          + '</tr></thead><tbody>';
        d.orders.forEach(o => {
          html += '<tr><td>' + escapeHtml(fmtDate(o.date)) + '</td><td>' + escapeHtml(o.vendor) + '</td>'
            + '<td class="num">' + fmtNum(o.qty) + '</td>'
            + '<td class="num">' + fmtNum(o.price) + '원</td></tr>';
        });
        html += '</tbody></table>';
      }

      // 최근 입고 내역
      if (d.receipts.length) {
        html += '<h3 style="font-size:14px;margin:8px 0 10px;color:#334155">📥 최근 입고 (최대 5건)</h3>';
        html += '<table><thead><tr>'
          + '<th style="width:100px">입고일</th><th>거래처</th><th class="num">수량</th>'
          + '</tr></thead><tbody>';
        d.receipts.forEach(r => {
          html += '<tr><td>' + escapeHtml(fmtDate(r.date)) + '</td><td>' + escapeHtml(r.vendor) + '</td>'
            + '<td class="num">' + fmtNum(r.qty) + '</td></tr>';
        });
        html += '</tbody></table>';
      }

      if (!d.spec && !d.price && !d.vendorStocks.length && !d.jasaStock && !d.orders.length && !d.receipts.length) {
        html += '<div class="loading">조회 가능한 추가 정보가 없습니다</div>';
      }

      document.getElementById('i-body').innerHTML = html;
    } catch (e) {
      document.getElementById('i-body').innerHTML = '<div class="loading" style="color:#ef4444">오류: ' + e.message + '</div>';
    }
  }

  function closeItemModal() {
    const m = document.getElementById('item-modal');
    m.classList.remove('show');
    m.style.zIndex = 110;
  }

  function infoCard(label, val) {
    return '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px">'
      + '<div style="font-size:11px;color:#64748b;margin-bottom:4px">' + label + '</div>'
      + '<div style="font-size:15px;font-weight:700;color:#0f172a">' + escapeHtml(val) + '</div>'
      + '</div>';
  }

  function infoRow(label, val) {
    return '<tr><td style="width:140px;color:#64748b;font-size:12px;background:#f8fafc">' + label + '</td>'
      + '<td>' + escapeHtml(val || '-') + '</td></tr>';
  }

  function fmtDate(s) {
    s = String(s || '');
    if (s.length === 8) return s.slice(0,4) + '-' + s.slice(4,6) + '-' + s.slice(6,8);
    return s;
  }

  document.getElementById('modal').addEventListener('click', (e) => {
    if (e.target.id === 'modal') closeModal();
  });
  document.getElementById('item-modal').addEventListener('click', (e) => {
    if (e.target.id === 'item-modal') closeItemModal();
  });

  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => renderTab(t.dataset.tab));
  });

  let searchTimer = null;
  let chatHistory = [];
  const searchInput = document.getElementById('search-input');
  let lastInputHadValue = false;
  searchInput.addEventListener('input', () => {
    const q = searchInput.value.trim();
    document.getElementById('search-clear').style.display = q ? 'block' : 'none';
    clearTimeout(searchTimer);
    if (!q) {
      document.getElementById('search-area').innerHTML = '';
      document.querySelector('.tabs').style.display = '';
      document.getElementById('product-area').style.display = '';
      // 사용자가 직접 타이핑 내용을 모두 지운 경우 AI 대화도 초기화하고 메인 복귀
      if (lastInputHadValue) {
        document.getElementById('ai-area').innerHTML = '';
        chatHistory = [];
        document.getElementById('search-new').style.display = 'none';
      }
      lastInputHadValue = false;
      return;
    }
    lastInputHadValue = true;
    searchTimer = setTimeout(() => runSearch(q), 180);
  });
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const q = searchInput.value.trim();
      if (q) askAI(q);
    }
  });

  function updateChatBadge() {
    const area = document.getElementById('ai-area');
    const btn = document.getElementById('search-new');
    const turns = chatHistory.filter(m => m.role === 'user').length;
    if (turns > 0) btn.style.display = 'block'; else btn.style.display = 'none';
    // 배지 업데이트
    let badge = document.getElementById('chat-turn-badge');
    if (turns > 0) {
      const html = '<div class="chat-badge" id="chat-turn-badge">💬 대화 중 · ' + turns + '회 질문 · 연속 질문 가능</div>';
      if (badge) badge.outerHTML = html;
      else area.insertAdjacentHTML('afterbegin', html);
    } else if (badge) {
      badge.remove();
    }
  }

  async function askAI(question) {
    const area = document.getElementById('ai-area');
    const cardId = 'ai-' + Date.now();
    // 새 카드는 기존 대화 아래(최신이 아래쪽)로 쌓아 대화 스레드처럼 표시
    const loadingHtml =
      '<div class="ai-card loading-ai" id="' + cardId + '">'
      + '<div class="ai-header"><div class="q">🤖 ' + escapeHtml(question) + '</div>'
      + '<button class="ai-close" onclick="this.closest(\\'.ai-card\\').remove()">&times;</button></div>'
      + '<div class="ai-body"><span class="ai-dots"><span></span><span></span><span></span></span> AI가 데이터를 분석하고 있습니다...</div>'
      + '</div>';
    area.insertAdjacentHTML('beforeend', loadingHtml);
    // 입력창 비우고 포커스 유지 (연속 질문을 위해)
    searchInput.value = '';
    document.getElementById('search-clear').style.display = 'none';
    // 타이핑 매칭 결과 제거 + 메인 대시보드 복원
    document.getElementById('search-area').innerHTML = '';
    document.querySelector('.tabs').style.display = '';
    document.getElementById('product-area').style.display = '';
    searchInput.focus();
    document.getElementById(cardId).scrollIntoView({ behavior: 'smooth', block: 'end' });

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: question, history: chatHistory }),
      });
      const d = await res.json();
      const msg = d.message || d.error || '답변 없음';
      chatHistory.push({ role: 'user', content: question });
      chatHistory.push({ role: 'assistant', content: msg });
      if (chatHistory.length > 20) chatHistory = chatHistory.slice(-20);
      saveHistory(question, msg);

      const card = document.getElementById(cardId);
      card.classList.remove('loading-ai');
      card.innerHTML =
        '<div class="ai-header"><div class="q">🤖 ' + escapeHtml(question) + '</div>'
        + '<div style="display:flex;gap:6px;align-items:center">'
        + '<span class="meta">' + (d.context_rows ? d.context_rows + '행 참조' : '') + '</span>'
        + '<button class="ai-close" onclick="this.closest(\\'.ai-card\\').remove()">&times;</button>'
        + '</div></div>'
        + '<div class="ai-body">' + marked.parse(msg) + '</div>';
      updateChatBadge();
      card.scrollIntoView({ behavior: 'smooth', block: 'end' });
    } catch (e) {
      const card = document.getElementById(cardId);
      if (card) card.querySelector('.ai-body').innerHTML = '<span style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</span>';
    }
  }

  function resetConversation() {
    chatHistory = [];
    document.getElementById('ai-area').innerHTML = '';
    document.getElementById('search-new').style.display = 'none';
    searchInput.focus();
  }

  async function runSearch(q) {
    document.querySelector('.tabs').style.display = 'none';
    document.getElementById('product-area').style.display = 'none';
    const area = document.getElementById('search-area');
    area.innerHTML = '<div class="loading">검색 중...</div>';
    try {
      const res = await fetch('/api/search?q=' + encodeURIComponent(q));
      const d = await res.json();
      let html = '';
      if (d.products.length) {
        html += '<div class="section-title">🏷️ 제품 <span class="count">' + d.products.length + '개</span></div>';
        html += '<div class="grid" style="margin-bottom:24px">';
        d.products.forEach(p => {
          html += '<div class="card" onclick="openModal(\\'' + p.code + '\\')">'
            + '<div class="code">[' + p.group + '] ' + p.code + '</div>'
            + '<div class="name">' + escapeHtml(p.name) + '</div>'
            + '</div>';
        });
        html += '</div>';
      }
      if (d.items.length) {
        html += '<div class="section-title">🧩 자품번/부재료 <span class="count">' + d.items.length + '개</span></div>';
        html += '<div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr));margin-bottom:24px">';
        d.items.forEach(it => {
          html += '<div class="item-card" onclick="openItemModal(\\'' + it.code + '\\')">'
            + '<div class="code">' + it.code + (it.category ? ' · ' + escapeHtml(it.category) : '') + '</div>'
            + '<div class="name">' + escapeHtml(it.name) + '</div>'
            + '</div>';
        });
        html += '</div>';
      }
      if (!d.products.length && !d.items.length) {
        html = '<div class="search-empty">"' + escapeHtml(q) + '" 에 대한 검색 결과가 없습니다</div>';
      }
      area.innerHTML = html;
    } catch (e) {
      area.innerHTML = '<div class="search-empty" style="color:#ef4444">오류: ' + e.message + '</div>';
    }
  }

  function clearSearch() {
    searchInput.value = '';
    document.getElementById('search-clear').style.display = 'none';
    document.getElementById('search-area').innerHTML = '';
    document.querySelector('.tabs').style.display = '';
    document.getElementById('product-area').style.display = '';
    searchInput.focus();
  }

  // loadProducts();   // 2026-09-23 하단 품목 나열 제거 — 목록을 불러오지 않음

  // ───── Calendar ─────
  const CAL_STATE = {};
  function todayYm() { const d = new Date(); return d.getFullYear() + String(d.getMonth()+1).padStart(2,'0'); }
  function todayYmd() { const d = new Date(); return d.getFullYear() + String(d.getMonth()+1).padStart(2,'0') + String(d.getDate()).padStart(2,'0'); }
  function daysInMonth(ym) {
    const y = +ym.slice(0,4), m = +ym.slice(4,6);
    return new Date(y, m, 0).getDate();
  }
  function firstWeekday(ym) {
    const y = +ym.slice(0,4), m = +ym.slice(4,6);
    return new Date(y, m-1, 1).getDay();
  }

  async function loadCalendar(widget, ym) {
    const type = widget.dataset.type;
    CAL_STATE[type] = { ym: ym };
    const body = widget.querySelector('.cal-body');
    widget.querySelector('.cal-ym').textContent = ym.slice(0,4) + '.' + ym.slice(4,6);
    body.innerHTML = '<div style="font-size:11px;color:#64748b;text-align:center;padding:20px">로딩...</div>';
    try {
      const res = await fetch('/api/calendar/' + type + '?ym=' + ym);
      const d = await res.json();
      if (d.error) throw new Error(d.error);
      renderCalendar(widget, ym, d.dates || {});
      widget.querySelector('.cal-total').textContent = '이달 ' + (d.total || 0) + '건';
    } catch (e) {
      body.innerHTML = '<div style="font-size:11px;color:#ef4444;padding:10px">오류: ' + e.message + '</div>';
    }
  }

  function renderCalendar(widget, ym, dateMap) {
    const type = widget.dataset.type;
    const body = widget.querySelector('.cal-body');
    const days = daysInMonth(ym);
    const first = firstWeekday(ym);
    const today = todayYmd();
    let html = '<div class="cal-grid">';
    const dows = ['일','월','화','수','목','금','토'];
    dows.forEach((d,i) => {
      html += '<div class="dow' + (i===0?' sun':i===6?' sat':'') + '">' + d + '</div>';
    });
    for (let i=0; i<first; i++) html += '<div class="cal-cell empty"></div>';
    for (let day=1; day<=days; day++) {
      const dd = String(day).padStart(2,'0');
      const ymd = ym + dd;
      const wd = (first + day - 1) % 7;
      const classes = ['cal-cell'];
      if (dateMap[ymd]) classes.push('has-data');
      if (ymd === today) classes.push('today');
      if (wd === 0) classes.push('sun');
      if (wd === 6) classes.push('sat');
      const title = dateMap[ymd] ? dateMap[ymd] + '건' : '';
      html += '<div class="' + classes.join(' ') + '" data-date="' + ymd + '" title="' + title + '" onclick="openCalDetail(\\'' + type + '\\',\\'' + ymd + '\\')">' + day + '</div>';
    }
    html += '</div>';
    body.innerHTML = html;
  }

  function calNav(btn, delta) {
    const widget = btn.closest('.cal-widget');
    const type = widget.dataset.type;
    const cur = (CAL_STATE[type] && CAL_STATE[type].ym) || todayYm();
    let y = +cur.slice(0,4), m = +cur.slice(4,6) + delta;
    while (m > 12) { m -= 12; y += 1; }
    while (m < 1) { m += 12; y -= 1; }
    const ym = y + String(m).padStart(2,'0');
    loadCalendar(widget, ym);
  }

  let _calType = null, _calYmd = null, _calSearchTimer = null;

  function renderCalTable(items, headNote) {
    const body = document.getElementById('cal-body');
    if (!items || items.length === 0) {
      body.innerHTML = '<div class="search-empty">' + (headNote || '데이터가 없습니다') + '</div>';
      return;
    }
    const allKeys = new Set();
    items.forEach(it => Object.keys(it).forEach(k => { if (String(it[k]).trim() !== '') allKeys.add(k); }));
    const colOrder = ['날짜','품번','품명','거래처명','단위','지시수량','작업수량','양품수량','발주수량','입고수량','단가','공급가액','합계금액','담당자','비고'];
    const cols = colOrder.filter(k => allKeys.has(k));
    const numCols = ['지시수량','작업수량','양품수량','발주수량','입고수량','단가','공급가액','합계금액'];
    let html = '<div style="margin-bottom:12px;font-size:12px;color:#64748b">' + (headNote || '총 ' + items.length + '건') + '</div>';
    html += '<table><thead><tr>';
    cols.forEach(c => {
      const isNum = numCols.includes(c);
      html += '<th' + (isNum ? ' class="num"' : '') + '>' + c + '</th>';
    });
    html += '</tr></thead><tbody>';
    items.forEach(it => {
      html += '<tr>';
      cols.forEach(c => {
        const isNum = numCols.includes(c);
        const v = it[c] || '';
        html += '<td' + (isNum ? ' class="num"' : '') + '>' + (isNum && v ? fmtNum(v) : escapeHtml(v)) + '</td>';
      });
      html += '</tr>';
    });
    html += '</tbody></table>';
    body.innerHTML = html;
  }

  async function openCalDetail(type, ymd) {
    const modal = document.getElementById('cal-modal');
    modal.classList.add('show');
    _calType = type; _calYmd = ymd;
    const si = document.getElementById('cal-search');
    si.value = '';
    const labels = { plan: '생산계획', actual: '생산실적', outsource: '외주발주', order: '발주내역' };
    document.getElementById('cal-badge').textContent = labels[type] || type;
    document.getElementById('cal-title').textContent = ymd.slice(0,4) + '-' + ymd.slice(4,6) + '-' + ymd.slice(6,8);
    const body = document.getElementById('cal-body');
    body.innerHTML = '<div class="loading">로딩 중...</div>';
    try {
      const res = await fetch('/api/calendar/' + type + '?date=' + ymd);
      const d = await res.json();
      if (d.error) throw new Error(d.error);
      if (_calYmd !== ymd || si.value.trim()) return;   // 그 사이 검색 시작됐으면 무시
      renderCalTable(d.items, d.items && d.items.length ? '' : '해당 날짜에 데이터가 없습니다');
    } catch (e) {
      body.innerHTML = '<div class="search-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }

  // 검색창 입력 → 날짜 무관 품번·품명 이력 (디바운스 300ms). 비우면 원래 날짜 상세로 복귀.
  function calSearchInput() {
    clearTimeout(_calSearchTimer);
    _calSearchTimer = setTimeout(async () => {
      const q = document.getElementById('cal-search').value.trim();
      if (!_calType) return;
      if (!q) { if (_calYmd) openCalDetail(_calType, _calYmd); return; }
      const body = document.getElementById('cal-body');
      body.innerHTML = '<div class="loading">검색 중...</div>';
      try {
        const res = await fetch('/api/calendar/' + _calType + '?q=' + encodeURIComponent(q));
        const d = await res.json();
        if (d.error) throw new Error(d.error);
        const cur = document.getElementById('cal-search').value.trim();
        if (cur !== q) return;   // 최신 입력만 반영
        const note = d.total > d.items.length
          ? '"' + escapeHtml(q) + '" 이력 ' + d.total + '건 중 최근 ' + d.items.length + '건 (최신순)'
          : '"' + escapeHtml(q) + '" 이력 ' + d.items.length + '건 (최신순)';
        renderCalTable(d.items, d.items.length ? note : '"' + escapeHtml(q) + '" 검색 결과 없음');
      } catch (e) {
        body.innerHTML = '<div class="search-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
      }
    }, 300);
  }

  function closeCalModal() {
    document.getElementById('cal-modal').classList.remove('show');
  }
  document.getElementById('cal-modal').addEventListener('click', (e) => {
    if (e.target.id === 'cal-modal') closeCalModal();
  });

  // 초기 로드
  document.querySelectorAll('.cal-widget').forEach(w => loadCalendar(w, todayYm()));

  // ───── KPI + Trend Charts ─────
  function fmtInt(v) { return Number(v || 0).toLocaleString(); }

  let _poInlineItems = [];
  async function loadPoInline() {
    try {
      const res = await fetch('/api/po_pending');
      const d = await res.json();
      _poInlineItems = d.items || [];
      renderPoInline();
    } catch (e) {
      document.getElementById('po-inline-list').innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }
  function renderPoInline() {
    const list = document.getElementById('po-inline-list');
    const cnt = document.getElementById('po-inline-count');
    const q = (document.getElementById('po-inline-search').value || '').toLowerCase().trim();
    const all = _poInlineItems;
    const items = q
      ? all.filter(it => (it.code||'').toLowerCase().includes(q) || (it.name||'').toLowerCase().includes(q) || (it.contract||'').toLowerCase().includes(q))
      : all;
    const pending = items.filter(x => !x.has_schedule).length;
    cnt.textContent = items.length + '/' + all.length + '건 · 미정 ' + pending;
    if (!items.length) {
      list.innerHTML = '<div class="alert-empty">' + (q ? '검색 결과 없음' : '발주완료 항목 없음') + '</div>';
      return;
    }
    list.innerHTML = items.map(it => {
        const code = escapeHtml(it.code || '-');
        const name = escapeHtml(it.name || '');
        const isImport = it.type === 'import_raw';
        const typeCls = it.type === 'raw' ? ' raw' : isImport ? ' import-raw' : '';
        const qtyText = (it.type === 'raw' || isImport) ? (it.qty || '') : (it.qty ? fmtNum(it.qty) : '');
        const qtyChip = qtyText ? '<span class="po-inline-qty" title="' + escapeHtml(qtyText) + '">' + escapeHtml(qtyText) + '</span>' : '<span></span>';
        const chip = it.has_schedule
          ? '<span class="po-inline-chip">' + escapeHtml(it.eta_display || '-') + '</span>'
          : '<span class="po-inline-chip empty">미정</span>';
        const destChip = it.destination
          ? '<span class="po-inline-dest" title="클릭하여 펼치기/접기" onclick="event.stopPropagation();this.classList.toggle(\\'expanded\\')">' + escapeHtml(it.destination) + '</span>'
          : '<span></span>';
        // 잔량(발주 후 미입고) 노랑 / 재고(보유 수량) 파랑 — 개념이 다르므로 배지 분리 (2026-09-14). 그리드 칸 수 유지 위해 한 span에 묶음
        const remainChip = '<span style="display:inline-flex;gap:4px;flex-wrap:wrap">'
          + (it.remain && it.remain.qty ? '<span class="po-inline-remain" title="잔량 = 발주 후 아직 입고되지 않은 물량">잔량 ' + fmtNum(it.remain.qty) + escapeHtml(it.remain.unit || '') + '</span>' : '')
          + (it.stock && it.stock.qty ? '<span class="po-inline-stock" title="말풍선에 언급된 보유 재고">재고 ' + fmtNum(it.stock.qty) + escapeHtml(it.stock.unit || '') + '</span>' : '')
          + '</span>';
        // import_raw: 계약번호(소) + 품목(대) 두 줄 표시
        const nameBlock = isImport
          ? '<div><div class="po-inline-sub">' + escapeHtml(it.contract || '') + '</div>'
              + '<div class="po-inline-name" title="' + name + '">' + (name || '-') + '</div></div>'
          : '<div><div class="po-inline-name" title="' + name + '">' + name + '</div></div>';
        const clickFn = isImport
          ? 'onclick="showImportDetail(' + _poInlineItems.indexOf(it) + ')" style="cursor:pointer" title="Monday 수입 내역 보기"'
          : 'onclick="openPoPending()"';
        return '<div class="po-inline-row' + typeCls + '" ' + clickFn + '>'
             + '<span class="po-inline-code">' + code + '</span>'
             + nameBlock
             + qtyChip + destChip + remainChip + chip + '</div>';
      }).join('');
  }

  // ───── 원/부자재 입고일정 캘린더 ─────
  let _poCalYm = '';        // 'YYYYMM'
  let _poCalSel = '';       // 선택된 'YYYY-MM-DD'
  let _poCalQ = '';         // 검색어 (소문자)
  function _poCalScheduled() {
    const base = (_poInlineItems || []).filter(x => x.has_schedule && /^\\d{4}-\\d{2}-\\d{2}/.test(x.eta_sort || ''));
    if (!_poCalQ) return base;
    return base.filter(x =>
      (x.code||'').toLowerCase().includes(_poCalQ) ||
      (x.name||'').toLowerCase().includes(_poCalQ) ||
      (x.contract||'').toLowerCase().includes(_poCalQ) ||
      (x.vendor||'').toLowerCase().includes(_poCalQ) ||
      (x.destination||'').toLowerCase().includes(_poCalQ));
  }
  function onPoCalSearch() {
    _poCalQ = (document.getElementById('pocal-search').value || '').toLowerCase().trim();
    const matches = _poCalScheduled();
    const hit = document.getElementById('pocal-search-hit');
    hit.textContent = _poCalQ ? matches.length + '건' : '';
    // 현재 월에 매칭 결과가 없으면 가장 가까운 매칭 월로 이동
    if (_poCalQ && matches.length) {
      const inMonth = matches.some(x => x.eta_sort.slice(0,4)+x.eta_sort.slice(5,7) === _poCalYm);
      if (!inMonth) {
        const first = matches.map(x => x.eta_sort.slice(0,10)).sort()[0];
        _poCalYm = first.slice(0,4) + first.slice(5,7);
      }
    }
    _poCalSel = '';
    renderPoCal();
    poCalRenderDetail();
  }
  function openPoCalModal() {
    document.getElementById('po-cal-modal').classList.add('show');
    _poCalQ = '';
    const si = document.getElementById('pocal-search'); if (si) si.value = '';
    document.getElementById('pocal-search-hit').textContent = '';
    const sched = _poCalScheduled();
    // 오늘 이후 가장 가까운 입고월, 없으면 현재월
    const today = todayYmd().slice(0,4) + '-' + todayYmd().slice(4,6) + '-' + todayYmd().slice(6,8);
    const future = sched.map(x => x.eta_sort.slice(0,10)).filter(d => d >= today).sort();
    const target = future.length ? future[0] : (sched.length ? sched.map(x=>x.eta_sort.slice(0,10)).sort().slice(-1)[0] : null);
    _poCalYm = target ? target.slice(0,4) + target.slice(5,7) : todayYm();
    _poCalSel = '';
    renderPoCal();
    poCalRenderDetail();
  }
  function closePoCalModal() { document.getElementById('po-cal-modal').classList.remove('show'); }
  function poCalNav(delta) {
    let y = +_poCalYm.slice(0,4), m = +_poCalYm.slice(4,6) + delta;
    while (m > 12) { m -= 12; y += 1; }
    while (m < 1) { m += 12; y -= 1; }
    _poCalYm = y + String(m).padStart(2,'0');
    renderPoCal();
  }
  function _adjYm(ym, delta) {
    let y = +ym.slice(0,4), m = +ym.slice(4,6) + delta;
    while (m > 12) { m -= 12; y++; } while (m < 1) { m += 12; y--; }
    return y + String(m).padStart(2,'0');
  }
  function _miniCalHtml(ym, label) {
    const days = daysInMonth(ym), first = firstWeekday(ym);
    const dows = ['일','월','화','수','목','금','토'];
    let g = '';
    dows.forEach((d,i) => g += '<span class="pmd-dow' + (i===0?' sun':i===6?' sat':'') + '">' + d + '</span>');
    for (let i=0; i<first; i++) g += '<span class="pmd-cell empty"></span>';
    for (let day=1; day<=days; day++) {
      const wd = (first + day - 1) % 7;
      g += '<span class="pmd-cell' + (wd===0?' sun':wd===6?' sat':'') + '">' + day + '</span>';
    }
    return '<div class="pmini"><div class="pmini-ym">' + label + ' · ' + (+ym.slice(4,6)) + '월'
         + '</div><div class="pmini-grid">' + g + '</div></div>';
  }
  function _renderMini(id, ym) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = _miniCalHtml(_adjYm(ym,-1), '전월') + _miniCalHtml(_adjYm(ym,1), '다음월');
  }
  function renderPoCal() {
    const ym = _poCalYm;
    document.getElementById('pocal-ym').textContent = ym.slice(0,4) + '.' + ym.slice(4,6);
    // 날짜별 그룹핑
    const byDate = {};
    _poCalScheduled().forEach(it => {
      const ymd = it.eta_sort.slice(0,10);          // YYYY-MM-DD
      if (ymd.slice(0,4) + ymd.slice(5,7) !== ym) return;
      (byDate[ymd] = byDate[ymd] || []).push(it);
    });
    const days = daysInMonth(ym);
    const first = firstWeekday(ym);
    const todayY = todayYmd();
    const todayDash = todayY.slice(0,4) + '-' + todayY.slice(4,6) + '-' + todayY.slice(6,8);
    const dows = ['일','월','화','수','목','금','토'];
    let html = '';
    dows.forEach((d,i) => html += '<div class="dow' + (i===0?' sun':i===6?' sat':'') + '">' + d + '</div>');
    for (let i=0; i<first; i++) html += '<div class="pocal-cell empty"></div>';
    for (let day=1; day<=days; day++) {
      const dd = String(day).padStart(2,'0');
      const ymd = ym.slice(0,4) + '-' + ym.slice(4,6) + '-' + dd;
      const wd = (first + day - 1) % 7;
      const list = byDate[ymd] || [];
      const cls = ['pocal-cell'];
      if (list.length) cls.push('has-data');
      if (ymd === todayDash) cls.push('today');
      if (ymd === _poCalSel) cls.push('selected');
      if (wd === 0) cls.push('sun'); if (wd === 6) cls.push('sat');
      let dots = '';
      if (list.length) {
        const types = [...new Set(list.map(x => x.type))];
        dots = '<div class="pocal-dots">' + types.map(t => '<span class="pocal-dot ' + t + '"></span>').join('') + '</div>';
      }
      const cnt = list.length ? '<span class="pocal-cnt">' + list.length + '</span>' : '';
      html += '<div class="' + cls.join(' ') + '" ' + (list.length ? 'onclick="poCalPick(\\'' + ymd + '\\')"' : '')
            + '><span class="dnum">' + day + '</span>' + dots + cnt + '</div>';
    }
    document.getElementById('pocal-grid').innerHTML = html;
    _renderMini('pocal-mini', ym);
  }
  const _poTypeLabel = { po:'부자재', raw:'원료', import_raw:'수입' };
  function _poCalRowHtml(it) {
    const code = escapeHtml(it.code || '-');
    const name = escapeHtml(it.name || (it.contract || '-'));
    const sub = it.type === 'import_raw' && it.contract ? '<div class="dsub">' + escapeHtml(it.contract) + '</div>' : '';
    const qtyText = (it.type === 'raw' || it.type === 'import_raw') ? (it.qty || '') : (it.qty ? fmtNum(it.qty) : '');
    const dest = it.destination ? ' · ' + escapeHtml(it.destination) : '';
    return '<div class="pocal-drow">'
         + '<span class="tag ' + it.type + '"></span>'
         + '<span class="dcode">' + code + '</span>'
         + '<div class="dname">' + name + '<span class="dsub" title="클릭하여 펼치기/접기" onclick="event.stopPropagation();this.classList.toggle(\\'expanded\\')">' + (_poTypeLabel[it.type]||'') + dest + '</span>' + sub + '</div>'
         + '<span class="dqty">' + escapeHtml(qtyText) + '</span></div>';
  }
  const _DOW = ['일','월','화','수','목','금','토'];
  function _ymdDow(ymd) { const y=+ymd.slice(0,4),m=+ymd.slice(5,7),d=+ymd.slice(8,10); return _DOW[new Date(y,m-1,d).getDay()]; }
  function poCalRenderDetail() {
    const detail = document.getElementById('pocal-detail');
    const all = _poCalScheduled();
    if (!all.length) {
      detail.innerHTML = '<div class="pocal-empty">' + (_poCalQ ? '검색 결과가 없습니다' : '예정된 입고 일정이 없습니다') + '</div>';
      return;
    }
    // 특정 날짜 선택 시 해당 날짜만
    if (_poCalSel) {
      const list = all.filter(it => it.eta_sort.slice(0,10) === _poCalSel);
      const md = (+_poCalSel.slice(5,7)) + '월 ' + (+_poCalSel.slice(8,10)) + '일 (' + _ymdDow(_poCalSel) + ')';
      detail.innerHTML = '<h3>' + md + ' · 입고 예정 ' + list.length + '건'
        + ' <span class="pocal-clear" onclick="poCalShowAll()">✕ 전체보기</span></h3>'
        + list.map(_poCalRowHtml).join('');
      return;
    }
    // 전체: 날짜별 그룹 (오름차순)
    const byDate = {};
    all.forEach(it => { const k = it.eta_sort.slice(0,10); (byDate[k]=byDate[k]||[]).push(it); });
    const keys = Object.keys(byDate).sort();
    let html = '<h3>전체 입고 일정 · ' + all.length + '건</h3>';
    keys.forEach(k => {
      const md = (+k.slice(5,7)) + '월 ' + (+k.slice(8,10)) + '일';
      html += '<div class="pocal-dgroup" onclick="poCalPick(\\'' + k + '\\')">'
            + md + ' <span class="dgw">(' + _ymdDow(k) + ')</span>'
            + '<span class="dgn">' + byDate[k].length + '건</span></div>';
      html += byDate[k].map(_poCalRowHtml).join('');
    });
    detail.innerHTML = html;
  }
  function poCalShowAll() { _poCalSel = ''; renderPoCal(); poCalRenderDetail(); }
  function poCalPick(ymd) {
    _poCalSel = (_poCalSel === ymd) ? '' : ymd;
    // 선택한 날짜의 달로 캘린더 이동
    if (_poCalSel) { _poCalYm = _poCalSel.slice(0,4) + _poCalSel.slice(5,7); }
    renderPoCal();
    poCalRenderDetail();
  }
  document.getElementById('po-cal-modal').addEventListener('click', (e) => {
    if (e.target.id === 'po-cal-modal') closePoCalModal();
  });

  // ───── 외주 입고일정 캘린더 ─────
  let _osCalYm = '';
  let _osCalSel = '';
  let _osCalQ = '';
  function _osCalType(it) { return it.group === '발주 완료' ? 'os-done' : 'os-req'; }
  function _osCalScheduled() {
    const base = (_osInlineItems || []).filter(x => x.has_schedule && /^\\d{4}-\\d{2}-\\d{2}/.test(x.eta_sort || ''));
    if (!_osCalQ) return base;
    return base.filter(x =>
      (x.code||'').toLowerCase().includes(_osCalQ) ||
      (x.name||'').toLowerCase().includes(_osCalQ) ||
      (x.vendor||'').toLowerCase().includes(_osCalQ) ||
      (x.destination||'').toLowerCase().includes(_osCalQ));
  }
  function onOsCalSearch() {
    _osCalQ = (document.getElementById('oscal-search').value || '').toLowerCase().trim();
    const matches = _osCalScheduled();
    document.getElementById('oscal-search-hit').textContent = _osCalQ ? matches.length + '건' : '';
    if (_osCalQ && matches.length) {
      const inMonth = matches.some(x => x.eta_sort.slice(0,4)+x.eta_sort.slice(5,7) === _osCalYm);
      if (!inMonth) {
        const first = matches.map(x => x.eta_sort.slice(0,10)).sort()[0];
        _osCalYm = first.slice(0,4) + first.slice(5,7);
      }
    }
    _osCalSel = '';
    renderOsCal();
    osCalRenderDetail();
  }
  function openOsCalModal() {
    document.getElementById('os-cal-modal').classList.add('show');
    _osCalQ = '';
    const si = document.getElementById('oscal-search'); if (si) si.value = '';
    document.getElementById('oscal-search-hit').textContent = '';
    const sched = _osCalScheduled();
    const today = todayYmd().slice(0,4) + '-' + todayYmd().slice(4,6) + '-' + todayYmd().slice(6,8);
    const future = sched.map(x => x.eta_sort.slice(0,10)).filter(d => d >= today).sort();
    const target = future.length ? future[0] : (sched.length ? sched.map(x=>x.eta_sort.slice(0,10)).sort().slice(-1)[0] : null);
    _osCalYm = target ? target.slice(0,4) + target.slice(5,7) : todayYm();
    _osCalSel = '';
    renderOsCal();
    osCalRenderDetail();
  }
  function closeOsCalModal() { document.getElementById('os-cal-modal').classList.remove('show'); }
  function osCalNav(delta) {
    let y = +_osCalYm.slice(0,4), m = +_osCalYm.slice(4,6) + delta;
    while (m > 12) { m -= 12; y += 1; }
    while (m < 1) { m += 12; y -= 1; }
    _osCalYm = y + String(m).padStart(2,'0');
    renderOsCal();
  }
  function renderOsCal() {
    const ym = _osCalYm;
    document.getElementById('oscal-ym').textContent = ym.slice(0,4) + '.' + ym.slice(4,6);
    const byDate = {};
    _osCalScheduled().forEach(it => {
      const ymd = it.eta_sort.slice(0,10);
      if (ymd.slice(0,4) + ymd.slice(5,7) !== ym) return;
      (byDate[ymd] = byDate[ymd] || []).push(it);
    });
    const days = daysInMonth(ym);
    const first = firstWeekday(ym);
    const todayY = todayYmd();
    const todayDash = todayY.slice(0,4) + '-' + todayY.slice(4,6) + '-' + todayY.slice(6,8);
    const dows = ['일','월','화','수','목','금','토'];
    let html = '';
    dows.forEach((d,i) => html += '<div class="dow' + (i===0?' sun':i===6?' sat':'') + '">' + d + '</div>');
    for (let i=0; i<first; i++) html += '<div class="pocal-cell empty"></div>';
    for (let day=1; day<=days; day++) {
      const dd = String(day).padStart(2,'0');
      const ymd = ym.slice(0,4) + '-' + ym.slice(4,6) + '-' + dd;
      const wd = (first + day - 1) % 7;
      const list = byDate[ymd] || [];
      const cls = ['pocal-cell'];
      if (list.length) cls.push('has-data');
      if (ymd === todayDash) cls.push('today');
      if (ymd === _osCalSel) cls.push('selected');
      if (wd === 0) cls.push('sun'); if (wd === 6) cls.push('sat');
      let dots = '';
      if (list.length) {
        const types = [...new Set(list.map(_osCalType))];
        dots = '<div class="pocal-dots">' + types.map(t => '<span class="pocal-dot ' + t + '"></span>').join('') + '</div>';
      }
      const cnt = list.length ? '<span class="pocal-cnt">' + list.length + '</span>' : '';
      html += '<div class="' + cls.join(' ') + '" ' + (list.length ? 'onclick="osCalPick(\\'' + ymd + '\\')"' : '')
            + '><span class="dnum">' + day + '</span>' + dots + cnt + '</div>';
    }
    document.getElementById('oscal-grid').innerHTML = html;
    _renderMini('oscal-mini', ym);
  }
  function _osCalRowHtml(it) {
    const code = escapeHtml(it.code || '-');
    const name = escapeHtml(it.name || '-');
    const t = _osCalType(it);
    const dest = it.destination ? ' · ' + escapeHtml(it.destination) : '';
    const grp = escapeHtml(it.group || '');
    return '<div class="pocal-drow">'
         + '<span class="tag ' + t + '"></span>'
         + '<span class="dcode">' + code + '</span>'
         + '<div class="dname">' + name + '<span class="dsub" title="클릭하여 펼치기/접기" onclick="event.stopPropagation();this.classList.toggle(\\'expanded\\')">' + grp + dest + '</span></div>'
         + '<span class="dqty">' + escapeHtml(it.qty || '') + '</span></div>';
  }
  function osCalRenderDetail() {
    const detail = document.getElementById('oscal-detail');
    const all = _osCalScheduled();
    if (!all.length) {
      detail.innerHTML = '<div class="pocal-empty">' + (_osCalQ ? '검색 결과가 없습니다' : '예정된 입고 일정이 없습니다') + '</div>';
      return;
    }
    if (_osCalSel) {
      const list = all.filter(it => it.eta_sort.slice(0,10) === _osCalSel);
      const md = (+_osCalSel.slice(5,7)) + '월 ' + (+_osCalSel.slice(8,10)) + '일 (' + _ymdDow(_osCalSel) + ')';
      detail.innerHTML = '<h3>' + md + ' · 입고 예정 ' + list.length + '건'
        + ' <span class="pocal-clear" onclick="osCalShowAll()">✕ 전체보기</span></h3>'
        + list.map(_osCalRowHtml).join('');
      return;
    }
    const byDate = {};
    all.forEach(it => { const k = it.eta_sort.slice(0,10); (byDate[k]=byDate[k]||[]).push(it); });
    const keys = Object.keys(byDate).sort();
    let html = '<h3>전체 입고 일정 · ' + all.length + '건</h3>';
    keys.forEach(k => {
      const md = (+k.slice(5,7)) + '월 ' + (+k.slice(8,10)) + '일';
      html += '<div class="pocal-dgroup" onclick="osCalPick(\\'' + k + '\\')">'
            + md + ' <span class="dgw">(' + _ymdDow(k) + ')</span>'
            + '<span class="dgn">' + byDate[k].length + '건</span></div>';
      html += byDate[k].map(_osCalRowHtml).join('');
    });
    detail.innerHTML = html;
  }
  function osCalShowAll() { _osCalSel = ''; renderOsCal(); osCalRenderDetail(); }
  function osCalPick(ymd) {
    _osCalSel = (_osCalSel === ymd) ? '' : ymd;
    // 선택한 날짜의 달로 캘린더 이동 (다른 달 일정을 클릭해도 바로 보이게)
    if (_osCalSel) { _osCalYm = _osCalSel.slice(0,4) + _osCalSel.slice(5,7); }
    renderOsCal();
    osCalRenderDetail();
  }
  document.getElementById('os-cal-modal').addEventListener('click', (e) => {
    if (e.target.id === 'os-cal-modal') closeOsCalModal();
  });

  async function loadAlerts(scope) {
    const list = document.getElementById('alert-list-' + scope);
    const count = document.getElementById('alert-count-' + scope);
    try {
      const res = await fetch('/api/stock_alerts?scope=' + scope);
      const d = await res.json();
      const items = d.items || [];
      count.textContent = (d.total || items.length) + '건';
      if (!items.length) {
        list.innerHTML = '<div class="alert-empty">경고 항목이 없습니다</div>';
        return;
      }
      const labelMap = { out:'품절', critical:'위험', warning:'주의', low:'관찰' };
      list.innerHTML = items.map(a => {
        const daysTxt = a.level === 'out' ? '0일' : (a.days_left.toFixed(1) + '일');
        const vendorTxt = (a.vendors && a.vendors.length)
          ? ' <span class="alert-vendor">· ' + escapeHtml(a.vendors.slice(0,2).join(', ')) + (a.vendors.length > 2 ? ' 외 ' + (a.vendors.length - 2) : '') + '</span>'
          : '';
        return '<div class="alert-row" onclick="openItemModal(\\'' + a.code + '\\')">'
             + '<span class="alert-badge ' + a.level + '">' + labelMap[a.level] + '</span>'
             + '<div><div class="alert-name">' + escapeHtml(a.name) + vendorTxt + '</div>'
             + '<div class="alert-code">' + escapeHtml(a.code) + ' · ' + (a.basis === 'sales' ? '판매속도 월환산 ' : '월평균 ') + fmtInt(a.monthly_avg) + '</div></div>'
             + '<div class="alert-qty">재고 ' + fmtInt(a.qty) + '</div>'
             + '<div class="alert-days ' + a.level + '">' + daysTxt + '</div>'
             + '</div>';
      }).join('');
    } catch (e) {
      list.innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }

  // ───── 발주 타이밍 (리드타임 기반) ─────
  async function loadReorder() {
    const list = document.getElementById('reorder-list');
    try {
      const d = await (await fetch('/api/reorder_advice')).json();
      const items = d.items || [];
      document.getElementById('reorder-count').textContent = (d.total || items.length) + '건';
      if (!items.length) { list.innerHTML = '<div class="alert-empty">지금 발주할 품목이 없습니다 👍</div>'; return; }
      list.innerHTML = items.map(a => {
        const now = a.urgency === 'now';
        const leadTxt = a.lead_days != null
          ? (a.lead_src === '업체표' ? '리드 ' + a.lead_days + '일(업체 L/T 상한)' : '리드 ' + a.lead_days + '일(' + a.lead_n + '회 ' + (a.lead_src === '납기' ? '납기계획' : '실측') + ')')
          : (a.src === 'spec' ? '시방서 출고 · 기준 ' + (d.default_lead || 14) + '일' : '리드 미상(기본 ' + (d.default_lead || 14) + '일' + (a.lead_note ? ' · 동일자등록' : '') + ')');
        const vendorTxt = (a.vendors && a.vendors.length)
          ? ' <span class="alert-vendor">· ' + escapeHtml(a.vendors.slice(0, 2).join(', ')) + (a.vendor_note ? ' (' + escapeHtml(a.vendor_note) + ')' : '') + '</span>' : '';
        return '<div class="alert-row" onclick="openItemModal(\\'' + a.code + '\\')">'
          + '<span class="alert-badge ' + (now ? 'out' : 'warning') + '">' + (a.src === 'spec' ? (now ? '시방서 발행' : '시방서 준비') : (now ? '지금 발주' : '이번주')) + '</span>'
          + '<div><div class="alert-name">' + escapeHtml(a.name) + vendorTxt + '</div>'
          + '<div class="alert-code">' + escapeHtml(a.code) + ' · ' + ({ jasa: '자사', outsource: '외주', goods: '상품매입' }[a.scope] || a.scope)
          + (a.scope === 'goods' ? ({ po: '(구매발주)', wp: '(외주발주)', spec: '(시방서)' }[a.src] || '(발주이력 없음)') : '') + ' · ' + leadTxt
          + (a.scope === 'goods' && a.incoming ? ' · <span style="color:#0891b2">미입고 ' + fmtInt(a.incoming) + '</span>' : '')
          + (a.trend >= 1.15 ? ' · <span style="color:#dc2626">▲추세 ' + Math.round((a.trend - 1) * 100) + '%</span>' : (a.trend <= 0.85 ? ' · <span style="color:#2563eb">▼추세 ' + Math.round((1 - a.trend) * 100) + '%</span>' : '')) + '</div></div>'
          + '<div class="alert-qty">재고 ' + fmtInt(a.qty) + '</div>'
          + '<div class="alert-days ' + (now ? 'out' : 'warning') + '">' + a.days_left.toFixed(1) + '일</div>'
          + '</div>';
      }).join('');
    } catch (e) {
      list.innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }

  // ───── 완제품 수급 플래너 ─────
  let _planItems = [];
  const PLAN_LV = { out: ['품절', 'out'], critical: ['2주↓', 'critical'], warning: ['4주↓', 'warning'], low: ['8주↓', 'low'], ok: ['여유', 'low'] };
  async function loadPlan() {
    const list = document.getElementById('plan-list');
    try {
      const d = await (await fetch('/api/supply_plan')).json();
      _planItems = d.items || [];
      _planMeta = { months: d.months || [], reason: d.reason || '', basis: d.basis || '' };
      renderPlan();
    } catch (e) { list.innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>'; }
  }
  let _planMeta = { months: [], reason: '' };
  function renderPlan() {
    const list = document.getElementById('plan-list');
    const q = (document.getElementById('plan-search').value || '').toLowerCase().trim();
    const matched = q ? _planItems.filter(x => (x.code + ' ' + x.name).toLowerCase().includes(q)) : _planItems;
    // 상단 집계는 검색 결과(필터 적용분) 기준으로 갱신
    const s = { out: 0, critical: 0, warning: 0, low: 0, ok: 0 };
    matched.forEach(x => { if (x.level in s) s[x.level]++; });
    document.getElementById('plan-count').textContent = (q ? matched.length + '/' + _planItems.length : _planItems.length) + '품목';
    document.getElementById('plan-summary').innerHTML = (_planMeta.months.length || _planMeta.basis)
      ? (_planMeta.basis ? '기준 ' + escapeHtml(_planMeta.basis) + ' · '
                         : '기준 ' + _planMeta.months.slice().reverse().map(m => m.slice(2, 4) + '/' + m.slice(4)).join('·') + ' 판매 · ')
        + (q ? '<span style="color:#0891b2;font-weight:700">검색 ' + matched.length + '건</span> · ' : '')
        + '<b style="color:#dc2626">품절 ' + s.out + '</b> · <b style="color:#ea580c">2주↓ ' + s.critical + '</b> · '
        + '<b style="color:#ca8a04">4주↓ ' + s.warning + '</b> · 8주↓ ' + s.low + ' · 여유 ' + s.ok
      : _planMeta.reason;
    const items = matched.slice(0, 40);
    if (!items.length) { list.innerHTML = '<div class="alert-empty">' + (q ? '검색 결과 없음' : '판매 데이터가 없습니다') + '</div>'; return; }
    list.innerHTML = items.map(x => {
      const [lb, cls] = PLAN_LV[x.level];
      const plus = (x.plan || x.incoming) ? ' <span style="color:#0891b2">+계획 ' + fmtInt(x.plan + x.incoming) + ' → ' + x.cov_plan_weeks + '주</span>' : '';
      const tr = x.trend >= 1.15 ? ' <span style="color:#dc2626">▲추세</span>' : (x.trend <= 0.85 ? ' <span style="color:#2563eb">▼추세</span>' : '');
      return '<div class="alert-row" onclick="openItemModal(\\'' + x.code + '\\')">'
        + '<span class="alert-badge ' + cls + '">' + lb + '</span>'
        + '<div><div class="alert-name">' + escapeHtml(x.name || x.code) + '</div>'
        + '<div class="alert-code">' + escapeHtml(x.code) + ' · ' + x.cls + ' · 월판매 ' + fmtInt(x.monthly) + tr + plus + '</div>'
        + (x.ch_stock != null ? '<div class="alert-code" style="margin-top:1px">채널재고 ' + fmtInt(x.ch_stock) + ' → 채널 포함 <b>' + x.cov_ch_weeks + '주</b>'
            + (x.ch_note ? ' <span class="ch-tag' + (x.ch_note === '채널 여유' ? ' on' : '') + '">' + x.ch_note + '</span>' : '') + '</div>' : '')
        + '</div>'
        + '<div class="alert-qty">창고 ' + fmtInt(x.stock) + '</div>'
        + '<div class="alert-days ' + cls + '">' + x.cov_weeks + '주</div></div>';
    }).join('');
  }

  // ───── 판매 분석 3종 (2026-09-23) ─────
  async function loadSalesGap() {
    const list = document.getElementById('gap-list');
    try {
      const d = await (await fetch('/api/sales_gap')).json();
      const items = (d.items || []).filter(x => x.level !== 'ok');
      const s = d.summary || {};
      document.getElementById('gap-count').textContent = items.length + '건';
      if (d.from) document.getElementById('gap-sub').textContent = (d.from.slice(5) + '~' + d.to.slice(5)).split('-').join('/') + ' 28일 · POS 보고 채널만 · 클릭=상세';
      document.getElementById('gap-summary').innerHTML = '<b style="color:#c2410c">납품 과다 ' + (s.over || 0) + '</b> (채널에 재고 쌓임 → 곧 발주 감소) · '
        + '<b style="color:#1d4ed8">납품 부족 ' + (s.under || 0) + '</b> (채널 재고 소진 중 → 곧 추가 발주) · 정상 ' + (s.ok || 0);
      if (!items.length) { list.innerHTML = '<div class="alert-empty">납품과 실판매가 비슷합니다 👍</div>'; return; }
      list.innerHTML = items.slice(0, 40).map(x => {
        const over = x.level === 'over';
        const sc = x.stock_change == null ? '' : ' · 채널재고 ' + (x.stock_change >= 0 ? '+' : '') + fmtInt(x.stock_change);
        return '<div class="alert-row" onclick="openItemModal(&quot;' + x.code + '&quot;)">'
          + '<span class="alert-badge ' + (over ? 'warning' : 'low') + '" style="' + (over ? '' : 'background:#dbeafe;color:#1d4ed8') + '">' + (over ? '납품 과다' : '납품 부족') + '</span>'
          + '<div><div class="alert-name">' + escapeHtml(x.name) + '</div>'
          + '<div class="alert-code">' + escapeHtml(x.code) + ' · 납품 ' + fmtInt(x.delivery) + ' · POS ' + fmtInt(x.pos) + sc + ' · ' + escapeHtml(x.channels) + '</div></div>'
          + '<div class="alert-qty"></div>'
          + '<div class="alert-days" style="color:' + (over ? '#c2410c' : '#1d4ed8') + '">' + (x.ratio == null ? 'POS 0' : '×' + x.ratio) + '</div></div>';
      }).join('');
    } catch (e) { list.innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>'; }
  }
  async function loadChannelPrice() {
    const list = document.getElementById('cpc-list');
    try {
      const d = await (await fetch('/api/channel_price_changes')).json();
      const items = d.items || [];
      document.getElementById('cpc-count').textContent = items.length + '건';
      if (!items.length) { list.innerHTML = '<div class="alert-empty">최근 6개월 공급단가 변동이 없습니다</div>'; return; }
      list.innerHTML = items.slice(0, 40).map(x => {
        const up = x.pct > 0;
        const click = x.code ? ' onclick="openItemModal(&quot;' + x.code + '&quot;)"' : '';
        return '<div class="alert-row"' + click + '>'
          + '<span class="alert-badge ' + (up ? 'low' : 'out') + '">' + (up ? '▲' : '▼') + ' ' + Math.abs(x.pct) + '%</span>'
          + '<div><div class="alert-name">' + escapeHtml(x.name) + '<span class="ch-tag">' + escapeHtml(x.channel_name) + '</span></div>'
          + '<div class="alert-code">' + escapeHtml(x.code || ('SKU ' + x.sku)) + ' · ' + fmtInt(x.prev) + '원 → <b>' + fmtInt(x.cur) + '원</b> (' + escapeHtml(x.since.slice(5).replace('-', '/')) + '부터)</div></div>'
          + '<div class="alert-qty">월 영향</div>'
          + '<div class="alert-days" style="color:' + (x.impact >= 0 ? '#047857' : '#dc2626') + '">' + (x.impact >= 0 ? '+' : '−') + fmtEok(Math.abs(x.impact)) + '</div></div>';
      }).join('');
    } catch (e) { list.innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>'; }
  }
  async function loadWeekday() {
    const body = document.getElementById('wd-body');
    try {
      const d = await (await fetch('/api/delivery_weekday')).json();
      const days = d.days || [];
      if (!days.length) { body.innerHTML = '<div class="alert-empty">자료 없음</div>'; return; }
      if (d.from) document.getElementById('wd-sub').textContent = '최근 12주 (' + d.from.slice(5).replace('-', '/') + '~' + d.to.slice(5).replace('-', '/') + ') · 납품수량';
      const mx = Math.max(...days.map(x => x.qty), 1);
      body.innerHTML = '<div style="display:flex;gap:10px;font-size:10.5px;color:var(--text-3);margin-bottom:6px">'
        + '<span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:#2563eb;margin-right:3px"></i>온라인</span>'
        + '<span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:#f59e0b;margin-right:3px"></i>오프라인</span></div>'
        + days.map(x => '<div class="wd-row" title="' + escapeHtml(Object.entries(x.by_ch).map(([k, v]) => k + ' ' + v.toLocaleString()).join(' / ')) + '">'
          + '<span style="font-weight:700;color:' + (x.wd === '일' ? '#dc2626' : (x.wd === '토' ? '#2563eb' : 'var(--text-1)')) + '">' + x.wd + '</span>'
          + '<div class="wd-bar"><i style="width:' + (x.online / mx * 100) + '%;background:#2563eb"></i><i style="width:' + (x.offline / mx * 100) + '%;background:#f59e0b"></i></div>'
          + '<span class="n">' + fmtInt(x.qty) + '</span><span class="p">' + x.share + '%</span></div>').join('')
        + '<div style="font-size:10.5px;color:var(--text-3);margin-top:8px;line-height:1.5">막대에 마우스를 올리면 주요 채널(' + escapeHtml((d.channels || []).join(', ')) + ')별 수량이 보입니다.</div>';
    } catch (e) { body.innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>'; }
  }

  // ───── 채널 품절 경보 (2026-09-23) ─────
  let _chItems = [], _chType = 'all', _chMeta = {};
  const CH_LV = { out: ['품절', 'out'], critical: ['3일↓', 'critical'], warning: ['7일↓', 'warning'], ok: ['여유', 'low'] };
  async function loadChStock() {
    const list = document.getElementById('chstock-list');
    try {
      const d = await (await fetch('/api/channel_stock')).json();
      _chItems = d.items || []; _chMeta = d;
      renderChStock();
    } catch (e) { list.innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>'; }
  }
  function setChType(t, el) {
    _chType = t;
    document.querySelectorAll('.chstock-panel .vk-chip').forEach(b => b.classList.toggle('on', b === el));
    renderChStock();
  }
  function renderChStock() {
    const list = document.getElementById('chstock-list');
    const items = _chItems.filter(x => _chType === 'all' || x.channel_type === _chType);
    const s = { out: 0, critical: 0, warning: 0 };
    items.forEach(x => { if (x.level in s) s[x.level]++; });
    document.getElementById('chstock-count').textContent = items.length + '건';
    document.getElementById('chstock-summary').innerHTML = _chMeta.reason ? escapeHtml(_chMeta.reason)
      : ('재고 기준 ~' + escapeHtml((_chMeta.as_of || '').slice(5).replace('-', '/')) + ' · '
        + '<b style="color:#dc2626">품절 ' + s.out + '</b> · <b style="color:#ea580c">3일↓ ' + s.critical + '</b> · '
        + '<b style="color:#ca8a04">7일↓ ' + s.warning + '</b>');
    if (!items.length) { list.innerHTML = '<div class="alert-empty">7일 안에 비는 채널 재고가 없습니다 👍</div>'; return; }
    list.innerHTML = items.slice(0, 40).map(x => {
      const [lb, cls] = CH_LV[x.level] || CH_LV.warning;
      const dl = x.last_delivery ? ' · 최근납품 ' + x.last_delivery.slice(5).replace('-', '/') + ' ' + fmtInt(x.last_delivery_qty) : '';
      const ours = (x.ours != null) ? ' · 우리재고 ' + fmtInt(x.ours) : '';
      const stale = x.stale ? ' <span style="color:#94a3b8" title="최근 7일 재고값이 변하지 않음 — 채널 재고 스냅샷이 갱신되지 않았을 수 있음">(재고값 정체)</span>' : '';
      const click = x.code ? ' onclick="openItemModal(\\'' + x.code + '\\')"' : '';
      return '<div class="alert-row"' + click + '>'
        + '<span class="alert-badge ' + cls + '">' + lb + '</span>'
        + '<div><div class="alert-name">' + escapeHtml(x.name) + '<span class="ch-tag' + (x.channel_type === 'online' ? ' on' : '') + '">' + escapeHtml(x.channel_name) + '</span></div>'
        + '<div class="alert-code">' + escapeHtml(x.code || ('SKU ' + x.sku)) + ' · POS 일 ' + x.pos_d + dl + ours + stale + '</div></div>'
        + '<div class="alert-qty">채널재고 ' + fmtInt(x.stock) + '</div>'
        + '<div class="alert-days ' + cls + '">' + x.cover + '일</div></div>';
    }).join('');
  }

  // ───── 거래처 스코어카드 ─────
  let _vendorItems = [];
  function scPill(s) {
    if (s == null) return '<span class="sc-pill sc-n">-</span>';
    return '<span class="sc-pill ' + (s >= 80 ? 'sc-a' : (s >= 60 ? 'sc-b' : 'sc-c')) + '">' + s + '</span>';
  }
  let _vendorAll = [], _vendorKind = 'all', _vendorMeta = {};
  const _vkLabel = { po: '구매', wp: '외주', both: '구매·외주' };
  const _fmtEok = a => a >= 1e8 ? (a / 1e8).toFixed(1) + '억' : Math.round(a / 1e4).toLocaleString() + '만';
  function setVendorKind(k, btn) {
    _vendorKind = k;
    document.querySelectorAll('.vk-chip').forEach(b => b.classList.toggle('on', b.dataset.k === k));
    renderVendors();
  }
  function renderVendors() {
    const list = document.getElementById('vendor-list');
    // 구매 필터=구매 실적 있는 곳, 외주 필터=외주 실적 있는 곳 (겸업 거래처는 양쪽에 표시)
    _vendorItems = _vendorAll.filter(v => _vendorKind === 'all' || v.kind === 'both' || v.kind === _vendorKind);
    if (_vendorKind !== 'all') _vendorItems = _vendorItems.slice().sort((a, b) => (b[_vendorKind + '_amt'] || 0) - (a[_vendorKind + '_amt'] || 0));
    const cnt = document.getElementById('vendor-count');
    cnt.textContent = (_vendorKind === 'all' ? (_vendorMeta.total || 0) + '개 거래처' : _vendorItems.length + '개 ' + _vkLabel[_vendorKind] + '처') + ' · ' + (_vendorMeta.since || '') + '~';
    if (!_vendorItems.length) { list.innerHTML = '<div class="alert-empty">발주 데이터가 없습니다</div>'; return; }
    list.innerHTML = _vendorItems.slice(0, 25).map((v, i) => {
      const f = (x, suf) => x == null ? '-' : x + suf;
      const amtShown = _vendorKind === 'all' ? v.amt : (v[_vendorKind + '_amt'] || 0);
      const cntTxt = v.kind === 'both'
        ? '구매 ' + v.po_n + '건 · 외주 ' + v.wp_n + '건'
        : v.n_po + '건 · ' + v.n_items + '품목';
      return '<div class="alert-row" onclick="openVendorDetail(' + i + ')">'
        + scPill(v.score)
        + '<div><div class="alert-name">' + escapeHtml(v.vendor) + '<span class="vk-tag ' + v.kind + '">' + _vkLabel[v.kind] + '</span></div>'
        + '<div class="alert-code">납기 ' + f(v.ontime, '%') + (v.ontime_n ? '(' + v.ontime_n + '건)' : '') + ' · 충족 ' + f(v.fill, '%')
        + ' · 리드 ' + f(v.lead, '일') + ' · 단가변동 ' + v.price_changes + '건</div></div>'
        + '<div class="alert-qty">' + cntTxt + '</div>'
        + '<div class="alert-days" style="color:' + (v.kind === 'wp' ? '#c2410c' : '#7c3aed') + '">' + _fmtEok(amtShown) + '</div></div>';
    }).join('');
  }
  async function loadVendors() {
    const list = document.getElementById('vendor-list');
    try {
      const d = await (await fetch('/api/vendor_scorecard')).json();
      _vendorAll = d.items || [];
      _vendorMeta = { total: d.total, since: d.since, n_wp: d.n_wp };
      renderVendors();
    } catch (e) { list.innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>'; }
  }
  function openVendorDetail(i) {
    const v = _vendorItems[i]; if (!v) return;
    const modal = document.getElementById('chart-detail-modal');
    modal.classList.add('show');
    if (typeof _phChart !== 'undefined' && _phChart) { _phChart.destroy(); _phChart = null; }
    const badge = document.getElementById('cd-badge'); badge.textContent = '거래처'; badge.style.background = 'linear-gradient(135deg,#7c3aed,#a78bfa)';
    document.getElementById('cd-title').textContent = v.vendor + ' (' + _vkLabel[v.kind] + ')';
    document.getElementById('cd-sub').textContent = '최근 12개월 · 종합점수 ' + (v.score == null ? '-' : v.score + '점') + ' (납기 40% · 충족 30% · 단가안정 30%)';
    const f = (x, suf) => x == null ? '<span style="color:#94a3b8">데이터 부족</span>' : '<b>' + x + suf + '</b>';
    const row = (k, val, note) => '<tr><td style="padding:8px 10px;color:#475569;width:150px">' + k + '</td><td style="padding:8px 10px">' + val + '</td><td style="padding:8px 10px;font-size:11px;color:#94a3b8">' + (note || '') + '</td></tr>';
    const fillNote = v.kind === 'both'
      ? '구매 ' + (v.po_fill == null ? '-' : v.po_fill + '%') + ' · 외주 ' + (v.wp_fill == null ? '-' : v.wp_fill + '%') + ' · 발주 14일 경과분 기준'
      : '발주 14일 경과분 기준 입고수량/발주수량';
    let html = '<table style="width:100%;font-size:13px;border-collapse:collapse">'
      + row('총 발주액', '<b>' + fmtInt(v.amt) + '원</b>', v.n_po + '건 발주 · ' + v.n_items + '개 품목');
    const vq = JSON.stringify(v.vendor).replace(/"/g, '&quot;');
    const clickRow = (k, kind, val, note) => '<tr class="vd-k" data-kind="' + kind + '" onclick="vendorOrdersToggle(' + vq + ',&quot;' + kind + '&quot;,this)" style="cursor:pointer" title="클릭=최근 10건 이력">'
      + '<td style="padding:8px 10px;color:#475569;width:150px">' + k + ' <span style="font-size:10px;color:#7c3aed">▼</span></td><td style="padding:8px 10px">' + val + '</td><td style="padding:8px 10px;font-size:11px;color:#94a3b8">' + note + '</td></tr>';
    if (v.po_n) html += clickRow('　구매 발주', 'po', '<b>' + fmtInt(v.po_amt) + '원</b>', v.po_n + '건 · 발주정보(합계금액) · 클릭=최근 10건');
    if (v.wp_n) html += clickRow('　외주 발주', 'wp', '<b style="color:#c2410c">' + fmtInt(v.wp_amt) + '원</b>', v.wp_n + '건 · 외주발주정보(공급가액) · 클릭=최근 10건');
    html += row('납기 준수율', f(v.ontime, '%'), v.ontime_n ? v.ontime_n + '건 중 납기 내 입고 (구매 기준)' : (v.kind === 'wp' ? '외주는 입고일 데이터 없음 → 제외' : '납기일자 또는 입고 매칭 없음'))
      + row('입고 충족률', f(v.fill, '%'), fillNote)
      + row('실측 리드타임', f(v.lead, '일'), v.lead_n ? '중앙값 · ' + v.lead_n + '건 (구매 기준)' : (v.kind === 'wp' ? '외주는 입고일 데이터 없음 → 제외' : ''))
      + row('단가 변동', '<b>' + v.price_changes + '건</b>' + (v.price_changes ? ' · 평균 ±' + v.price_avg_pct + '%' : ''), '최근 6개월 3% 이상 변경 (구매+외주)')
      + '</table>'
      + '<div style="margin-top:12px;font-size:11px;color:#94a3b8">점수는 상대 비교용 지표입니다. 납기·충족 데이터가 없는 항목은 가중치에서 제외해 계산합니다.'
      + (v.wp_n ? ' 외주발주는 아마란스 입고정보에 입고일이 없고 납기일자가 발주일자와 같아 납기·리드타임은 구매 실적만으로 계산됩니다.' : '') + '</div>'
      + '<div id="vd-orders" style="margin-top:12px"></div>';
    document.getElementById('cd-body').innerHTML = html;
    _vdOpenKind = null;
  }
  let _vdOpenKind = null;
  async function vendorOrdersToggle(vendor, kind, tr) {
    const box = document.getElementById('vd-orders'); if (!box) return;
    document.querySelectorAll('#cd-body tr.vd-k').forEach(r => r.style.background = '');
    if (_vdOpenKind === kind) { _vdOpenKind = null; box.innerHTML = ''; return; }
    _vdOpenKind = kind;
    if (tr) tr.style.background = kind === 'wp' ? '#fff7ed' : '#f5f3ff';
    const color = kind === 'wp' ? '#c2410c' : '#7c3aed';
    const label = kind === 'wp' ? '외주 발주' : '구매 발주';
    box.innerHTML = '<div class="loading" style="padding:12px">이력 로딩 중...</div>';
    try {
      const d = await (await fetch('/api/vendor_orders?vendor=' + encodeURIComponent(vendor) + '&kind=' + kind + '&limit=10')).json();
      if (_vdOpenKind !== kind) return;
      const docs = d.docs || [];
      if (!docs.length) { box.innerHTML = '<div class="alert-empty">발주 이력이 없습니다</div>'; return; }
      const st = s => s === 'done' ? '<span style="color:#16a34a;font-weight:700">입고완료</span>' : s === 'part' ? '<span style="color:#ca8a04;font-weight:700">부분입고</span>' : '<span style="color:#dc2626;font-weight:700">미입고</span>';
      const cell = 'padding:7px 8px;border-bottom:1px solid #f1f5f9;vertical-align:top';
      box.innerHTML = '<div style="font-size:12px;font-weight:700;color:' + color + ';margin:4px 0 6px">' + label + ' 최근 ' + docs.length + '건 <span style="color:#94a3b8;font-weight:500">(전체 ' + d.total + '건)</span></div>'
        + '<div style="max-height:360px;overflow:auto;border:1px solid #e2e8f0;border-radius:8px">'
        + '<table style="width:100%;font-size:12px;border-collapse:collapse">'
        + '<thead><tr style="background:#f8fafc;color:#64748b;font-size:11px"><th style="' + cell + ';text-align:left">발주일</th><th style="' + cell + ';text-align:left">발주번호</th><th style="' + cell + ';text-align:left">품목</th><th style="' + cell + ';text-align:right">단가</th><th style="' + cell + ';text-align:right">수량</th><th style="' + cell + ';text-align:right">금액</th><th style="' + cell + ';text-align:center">상태</th></tr></thead><tbody>'
        + docs.map(o => {
          const items = o.items.map(it => '<div><span style="color:' + color + ';font-weight:700;cursor:pointer" onclick="openItemModal(&quot;' + escapeHtml(it.code) + '&quot;)">' + escapeHtml(it.code) + '</span> ' + escapeHtml(it.name)
            + ' <span style="color:#94a3b8">×' + fmtInt(it.qty) + (it.rcv < it.qty ? ' (입고 ' + fmtInt(it.rcv) + ')' : '') + '</span></div>').join('');
          // 단가: 품목 줄과 1:1로 맞춰 표시 (한 문서 안에서 품목별 단가가 다를 수 있음)
          const prices = o.items.map(it => '<div style="font-weight:600;color:#334155">' + (it.price ? fmtInt(it.price) : '-') + '</div>').join('');
          return '<tr><td style="' + cell + ';white-space:nowrap">' + o.date + (o.due && o.due !== o.date && kind === 'po' ? '<div style="font-size:10px;color:#94a3b8">납기 ' + o.due.slice(5) + '</div>' : '') + '</td>'
            + '<td style="' + cell + ';white-space:nowrap;font-family:monospace;font-size:11px">' + escapeHtml(o.doc) + '</td>'
            + '<td style="' + cell + '">' + items + '</td>'
            + '<td style="' + cell + ';text-align:right;white-space:nowrap">' + prices + '</td>'
            + '<td style="' + cell + ';text-align:right;white-space:nowrap">' + fmtInt(o.ord_q) + '</td>'
            + '<td style="' + cell + ';text-align:right;white-space:nowrap;font-weight:700">' + fmtInt(o.amt) + '</td>'
            + '<td style="' + cell + ';text-align:center;white-space:nowrap">' + st(o.status) + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    } catch (e) { box.innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>'; }
  }

  // ───── 거래처 재고 입력 링크·현황 (2026-09-11) ─────
  async function openVendorLinks() {
    const modal = document.getElementById('chart-detail-modal'); modal.classList.add('show');
    if (typeof _phChart !== 'undefined' && _phChart) { _phChart.destroy(); _phChart = null; }
    const badge = document.getElementById('cd-badge'); badge.textContent = '거래처 입력'; badge.style.background = 'linear-gradient(135deg,#dc2626,#f87171)';
    document.getElementById('cd-title').textContent = '외주 재고 거래처 셀프 입력';
    document.getElementById('cd-sub').textContent = '거래처에 링크를 보내면 로그인 없이 품번별 재고를 입력합니다. 저장 즉시 재고경고·거래처 페이지·마감에 반영됩니다.';
    const body = document.getElementById('cd-body'); body.innerHTML = '<div class="loading" style="padding:20px">로딩 중...</div>';
    try {
      const d = await (await fetch('/api/vendor_links')).json();
      const cell = 'padding:8px 10px;border-bottom:1px solid #f1f5f9;vertical-align:middle';
      body.innerHTML = '<table style="width:100%;font-size:12.5px;border-collapse:collapse"><thead><tr style="background:#f8fafc;color:#64748b;font-size:11px"><th style="' + cell + ';text-align:left">거래처</th><th style="' + cell + '">품목</th><th style="' + cell + ';text-align:left">최근 입력</th><th style="' + cell + '">이번달</th><th style="' + cell + ';text-align:left">링크</th></tr></thead><tbody>'
        + (d.items || []).map(v => '<tr><td style="' + cell + ';font-weight:700">' + escapeHtml(v.vendor) + '</td><td style="' + cell + ';text-align:center">' + v.items + '</td>'
          + '<td style="' + cell + '">' + (v.last_at ? v.last_at + ' <span style="color:#94a3b8">' + escapeHtml(v.last_by || '') + '</span>' : '<span style="color:#cbd5e1">없음</span>') + '</td>'
          + '<td style="' + cell + ';text-align:center">' + (v.month_count ? v.month_codes + '품목 · ' + v.month_count + '건' : '-') + '</td>'
          + '<td style="' + cell + '"><input readonly value="' + escapeHtml(v.url) + '" style="width:260px;font-size:11px;padding:4px 6px;border:1px solid #e2e8f0;border-radius:6px;color:#475569" onclick="this.select()"> '
          + '<button class="po-cal-btn" onclick="navigator.clipboard.writeText(&quot;' + escapeHtml(v.url) + '&quot;).then(()=>{this.textContent=&quot;복사됨&quot;;setTimeout(()=>this.textContent=&quot;복사&quot;,1500)})">복사</button> '
          + '<a class="po-cal-btn" href="' + escapeHtml(v.url) + '" target="_blank" style="text-decoration:none">열기</a></td></tr>').join('')
        + '</tbody></table>'
        + '<div style="margin-top:12px;font-size:11px;color:#94a3b8">링크는 거래처별 고유 토큰이라 외부에 공유하지 마세요. 입력값은 원본 재고 파일보다 나중 것만 적용되며, 구매팀이 마감 파일을 새로 만들면 그 시점 이전 입력은 파일 값으로 흡수됩니다.'
        + (d.pull && d.pull.last_run ? ' · GCP 동기화 ' + d.pull.last_run + (d.pull.error ? ' (오류: ' + escapeHtml(d.pull.error) + ')' : ' 정상') : '') + '</div>'
        + '<div id="closing-box" style="margin-top:14px;padding:12px;border:1px solid #e2e8f0;border-radius:10px;background:#fafafa"><div class="loading">마감 상태 확인 중...</div></div>';
      loadClosingStatus();
    } catch (e) { body.innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>'; }
  }
  function _ymOpts() { const out = []; const d = new Date(); for (let i = 0; i < 3; i++) { const y = d.getFullYear(), m = d.getMonth() + 1; out.push(String(y) + String(m).padStart(2, '0')); d.setMonth(d.getMonth() - 1); } return out; }
  async function loadClosingStatus(ym) {
    const box = document.getElementById('closing-box'); if (!box) return;
    ym = ym || _ymOpts()[0];
    try {
      const s = await (await fetch('/api/closing/status?ym=' + ym)).json();
      const sel = '<select id="closing-ym" onchange="loadClosingStatus(this.value)" style="padding:4px 8px;border:1px solid #cbd5e1;border-radius:6px;font-size:12px">' + _ymOpts().map(x => '<option value="' + x + '"' + (x === ym ? ' selected' : '') + '>' + x.slice(0, 4) + '년 ' + parseInt(x.slice(4)) + '월</option>').join('') + '</select>';
      box.innerHTML = '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><b style="font-size:13px">📁 마감 워크북 생성</b> ' + sel
        + '<span style="font-size:11.5px;color:#475569">일별 입력 ' + s.day_entries + '건 · 입고 ' + s.inbound_entries + '건 · ' + (s.by_vendor || []).filter(v => v.day || v.inbound).map(v => v.vendor + '(' + v.day + '/' + v.inbound + ')').join(', ') + '</span>'
        + (s.local ? '<button class="po-cal-btn" onclick="generateClosing(&quot;' + ym + '&quot;)" style="margin-left:auto">마감 생성</button>' : '<span style="margin-left:auto;font-size:11px;color:#b45309">호스트(OneDrive) 대시보드에서만 생성 가능</span>') + '</div>'
        + '<div style="font-size:11px;color:#64748b;margin-top:6px">기준 파일: ' + (s.baseline ? escapeHtml(s.baseline.split(/[\\/]/).pop()) : '<span style="color:#dc2626">없음</span>') + ' → 대상: ' + (s.target ? escapeHtml(s.target.split(/[\\/]/).pop()) : '-') + (s.target_exists ? ' <span style="color:#16a34a">(존재 · ' + s.target_mtime + ')</span>' : ' <span style="color:#94a3b8">(아직 없음)</span>') + '</div>'
        + '<div id="closing-result" style="font-size:12px;margin-top:6px"></div>';
    } catch (e) { box.innerHTML = '<span style="color:#dc2626;font-size:12px">마감 상태 조회 실패: ' + escapeHtml(e.message) + '</span>'; }
  }
  async function generateClosing(ym) {
    const R = document.getElementById('closing-result'); R.innerHTML = '<span class="loading">생성 중... (Excel 재계산 포함 30초~1분)</span>';
    try {
      const r = await fetch('/api/closing/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ym }) });
      const d = await r.json();
      if (!d.ok) { R.innerHTML = '<span style="color:#dc2626">실패: ' + escapeHtml(d.error || '') + '</span>'; return; }
      R.innerHTML = '<span style="color:#16a34a;font-weight:700">생성 완료</span> · ' + escapeHtml(d.target.split(/[\\/]/).pop()) + (d.side_file ? ' <span style="color:#b45309">(기존 마감이 수정된 파일이라 옆에 별도 저장)</span>' : '')
        + ' · 일별 ' + d.daily_cells + '칸 · 입고 ' + d.inbound_rows + '행 · 재계산 ' + (d.recalc === 'ok' ? '완료' : '<span style="color:#dc2626">' + escapeHtml(d.recalc) + '</span>')
        + (d.missing && d.missing.length ? '<div style="color:#b45309">행 못 찾음 ' + d.missing.length + ': ' + escapeHtml(d.missing.join(', ')) + '</div>' : '');
      setTimeout(() => loadClosingStatus(ym), 800);
    } catch (e) { R.innerHTML = '<span style="color:#dc2626">오류: ' + escapeHtml(e.message) + '</span>'; }
  }

  // ───── Ctrl+K 통합 검색 ─────
  let _ckTimer = null, _ckSel = 0, _ckRows = [];
  function ckOpen() { const o = document.getElementById('ck-overlay'); o.classList.add('show'); const i = document.getElementById('ck-input'); i.value = ''; document.getElementById('ck-list').innerHTML = ''; setTimeout(() => i.focus(), 30); }
  function ckClose() { document.getElementById('ck-overlay').classList.remove('show'); }
  function ckRender() {
    const l = document.getElementById('ck-list');
    if (!_ckRows.length) { l.innerHTML = '<div class="ck-hint">결과 없음</div>'; return; }
    l.innerHTML = _ckRows.map((r, i) => '<div class="ck-row' + (i === _ckSel ? ' sel' : '') + '" onmousedown="ckPick(' + i + ')">'
      + '<span class="cd">' + escapeHtml(r.code) + '</span><span>' + escapeHtml(r.name) + '</span><span class="tp">' + r.type + '</span></div>').join('');
    const el = l.querySelector('.ck-row.sel'); if (el) el.scrollIntoView({ block: 'nearest' });
  }
  function ckPick(i) { const r = _ckRows[i]; if (!r) return; ckClose(); openItemModal(r.code); }
  document.getElementById('ck-input').addEventListener('input', e => {
    clearTimeout(_ckTimer);
    const q = e.target.value.trim();
    _ckTimer = setTimeout(async () => {
      if (!q) { _ckRows = []; ckRender(); return; }
      try {
        const d = await (await fetch('/api/search?q=' + encodeURIComponent(q))).json();
        const P = (d.products || []).map(p => ({ code: p.code, name: p.name, type: { G: '자사제품', H: '유상사급', I: '상품매입' }[p.group] || '완제품' }));
        const I = (d.items || []).map(p => ({ code: p.code, name: p.name, type: '자재' }));
        _ckRows = P.concat(I).slice(0, 30); _ckSel = 0; ckRender();
      } catch (err) { _ckRows = []; ckRender(); }
    }, 200);
  });
  document.getElementById('ck-input').addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { _ckSel = Math.min(_ckRows.length - 1, _ckSel + 1); ckRender(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { _ckSel = Math.max(0, _ckSel - 1); ckRender(); e.preventDefault(); }
    else if (e.key === 'Enter') { ckPick(_ckSel); }
    else if (e.key === 'Escape') { ckClose(); }
  });
  document.getElementById('ck-overlay').addEventListener('mousedown', e => { if (e.target.id === 'ck-overlay') ckClose(); });
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); ckOpen(); }
  });

  // ───── 알림 센터 ─────
  const NOTIFY_LV = { error: ['#fef2f2', '#dc2626'], warn: ['#fefce8', '#ca8a04'], info: ['#f0f9ff', '#0369a1'] };
  function _notifySeen() { try { return +(localStorage.getItem('notifySeenId') || 0); } catch (e) { return 0; } }
  async function loadNotifyBadge() {
    try {
      const d = await (await fetch('/api/notifications')).json();
      const seen = _notifySeen();
      const unread = (d.items || []).filter(x => x.id > seen && x.kind !== 'test').length;
      const b = document.getElementById('notify-badge');
      b.textContent = unread > 99 ? '99+' : unread;
      b.style.display = unread ? 'block' : 'none';
    } catch (e) {}
  }
  async function openNotify() {
    const modal = document.getElementById('notify-modal');
    modal.classList.add('show');
    const body = document.getElementById('notify-body');
    document.getElementById('notify-msg').textContent = '';
    body.innerHTML = '<div class="loading" style="padding:20px">로딩 중...</div>';
    try {
      const d = await (await fetch('/api/notifications')).json();
      const items = d.items || [];
      document.getElementById('notify-sub').textContent =
        '채널: ' + (d.channels.length ? d.channels.map(c => c === 'email' ? '메일' : '카톡').join(' · ') : '대시보드만 (.env에 메일/카톡 설정 시 발송)')
        + (d.last_run ? ' · 마지막 점검 ' + d.last_run : '');
      if (!items.length) { body.innerHTML = '<div class="search-empty">아직 알림이 없습니다. "지금 점검"을 눌러 시작하세요.</div>'; }
      else {
        const seen = _notifySeen();
        body.innerHTML = items.map(x => {
          const [bg, fg] = NOTIFY_LV[x.level] || NOTIFY_LV.info;
          const sent = Object.entries(x.sent || {}).map(([k, v]) => (k === 'email' ? '메일' : '카톡') + (v === true ? '✓' : '✗')).join(' ');
          return '<div style="padding:10px 12px;margin-bottom:8px;border-radius:9px;background:' + bg + ';border-left:4px solid ' + fg
            + (x.id > seen ? ';box-shadow:0 0 0 1px ' + fg + '33' : '') + '">'
            + '<div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px">'
            + '<b style="color:' + fg + ';font-size:13px">' + escapeHtml(x.title) + '</b>'
            + '<span style="font-size:10.5px;color:var(--text-3);white-space:nowrap">' + x.ts + (sent ? ' · ' + sent : '') + '</span></div>'
            + '<div style="font-size:12px;color:var(--text-2);margin-top:4px;white-space:pre-line">' + escapeHtml(x.body) + '</div></div>';
        }).join('');
        try { localStorage.setItem('notifySeenId', String(Math.max(...items.map(x => x.id)))); } catch (e) {}
      }
      loadNotifyBadge();
    } catch (e) {
      body.innerHTML = '<div class="search-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }
  async function notifyRunNow(daily) {
    const m = document.getElementById('notify-msg');
    m.textContent = '점검 중...';
    try {
      const d = await (await fetch('/api/notify_run' + (daily ? '?daily=1' : ''), { method: 'POST' })).json();
      m.textContent = '✓ 점검 완료 — 신규 알림 ' + d.made + '건';
      openNotify();
    } catch (e) { m.textContent = '오류: ' + e.message; }
  }
  async function notifyTest() {
    const m = document.getElementById('notify-msg');
    m.textContent = '발송 중...';
    try {
      const d = await (await fetch('/api/notify_test', { method: 'POST' })).json();
      const r = Object.entries(d.sent || {}).map(([k, v]) => (k === 'email' ? '메일' : '카톡') + ': ' + (v === true ? '성공' : v)).join(' / ');
      m.textContent = d.channels.length ? '✓ ' + r : '설정된 채널 없음 — 대시보드에만 기록됨';
      openNotify();
    } catch (e) { m.textContent = '오류: ' + e.message; }
  }
  document.getElementById('notify-modal').addEventListener('click', e => {
    if (e.target.id === 'notify-modal') e.target.classList.remove('show');
  });
  setInterval(loadNotifyBadge, 5 * 60 * 1000);

  // ───── 청구요청 초안 (발주 타이밍 → 아마란스 양식) ─────
  let _prDraftItems = [];
  async function openPrDraft() {
    const modal = document.getElementById('pr-draft-modal');
    modal.classList.add('show');
    const body = document.getElementById('pr-draft-body');
    document.getElementById('pr-draft-msg').textContent = '';
    body.innerHTML = '<div class="loading" style="padding:20px">초안 구성 중...</div>';
    try {
      const d = await (await fetch('/api/purchase_req_draft')).json();
      _prDraftItems = d.items || [];
      document.getElementById('pr-draft-date').textContent = '· 청구일 ' + d.req_dt + ' · ' + _prDraftItems.length + '건';
      if (!_prDraftItems.length) { body.innerHTML = '<div class="search-empty">청구 대상 품목이 없습니다 👍</div>'; return; }
      let html = '<table><thead><tr><th style="width:34px"><input type="checkbox" checked onchange="document.querySelectorAll(\\'.pr-chk\\').forEach(c=>c.checked=this.checked)"></th>'
        + '<th>품번</th><th>품명</th><th class="num">현재고</th><th class="num">예측월소비</th><th class="num">소진일</th>'
        + '<th class="num">청구수량</th><th>납기</th><th>거래처</th></tr></thead><tbody>';
      _prDraftItems.forEach((it, i) => {
        html += '<tr>'
          + '<td><input type="checkbox" class="pr-chk" checked data-i="' + i + '"></td>'
          + '<td style="font-weight:700">' + escapeHtml(it.code) + '</td>'
          + '<td>' + escapeHtml(it.name) + (it.scope === 'outsource' ? ' <span style="font-size:10px;color:#b45309">외주</span>' : (it.scope === 'goods' ? ' <span style="font-size:10px;color:#0f766e">상품매입' + ({ po: '·구매발주', wp: '·외주발주', spec: '·시방서' }[it.src] || '') + (it.incoming ? ' · 미입고 ' + fmtInt(it.incoming) : '') + (it.moq_note ? ' · ' + escapeHtml(it.moq_note) : '') + '</span>' : '')) + '</td>'
          + '<td class="num">' + fmtInt(it.stock) + '</td>'
          + '<td class="num">' + fmtInt(it.monthly_avg) + '</td>'
          + '<td class="num" style="color:' + (it.days_left < 7 ? '#dc2626' : '#ca8a04') + ';font-weight:700">' + it.days_left.toFixed(1) + '일</td>'
          + '<td class="num"><input type="number" class="pr-qty" data-i="' + i + '" value="' + it.qty + '" style="width:84px;padding:3px 6px;border:1px solid var(--border);border-radius:6px;text-align:right"></td>'
          + '<td><input type="date" class="pr-due" data-i="' + i + '" value="' + it.due + '" style="padding:3px 6px;border:1px solid var(--border);border-radius:6px"></td>'
          + '<td style="font-size:11.5px">' + escapeHtml(it.vendor || '-') + '</td>'
          + '</tr>';
      });
      html += '</tbody></table>';
      html += '<div style="margin-top:8px;font-size:10.5px;color:var(--text-3)">제안수량 = (리드타임+7일)분 소비 + 1개월 운영분 − 현재고, 10 단위 올림 · 납기 = 오늘 + 리드타임</div>';
      body.innerHTML = html;
    } catch (e) {
      body.innerHTML = '<div class="search-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }
  function copyPrDraft() {
    const rows = [['품번', '품명', '청구수량', '납기', '거래처', '비고']];
    document.querySelectorAll('.pr-chk').forEach(chk => {
      if (!chk.checked) return;
      const i = +chk.dataset.i, it = _prDraftItems[i];
      const qty = document.querySelector('.pr-qty[data-i="' + i + '"]').value;
      const due = document.querySelector('.pr-due[data-i="' + i + '"]').value;
      rows.push([it.code, it.name, qty, due.replaceAll('-', ''), it.vendor || '', '대시보드 발주타이밍 자동초안']);
    });
    if (rows.length === 1) { document.getElementById('pr-draft-msg').textContent = '선택된 항목이 없습니다'; return; }
    const tsv = rows.map(r => r.join('\\t')).join('\\n');
    navigator.clipboard.writeText(tsv).then(() => {
      document.getElementById('pr-draft-msg').textContent = '✓ ' + (rows.length - 1) + '건 복사됨 — 아마란스/엑셀에 붙여넣기 하세요';
    }).catch(() => {
      document.getElementById('pr-draft-msg').textContent = '복사 실패 — 표를 드래그해 복사해 주세요';
    });
  }
  document.getElementById('pr-draft-modal').addEventListener('click', e => {
    if (e.target.id === 'pr-draft-modal') e.target.classList.remove('show');
  });

  // ───── 단가 변동 감지 ─────
  async function loadPriceChanges() {
    const list = document.getElementById('price-chg-list');
    try {
      const d = await (await fetch('/api/price_changes')).json();
      const items = d.items || [];
      document.getElementById('price-chg-count').textContent = (d.total || items.length) + '건';
      if (!items.length) { list.innerHTML = '<div class="alert-empty">최근 단가 변동이 없습니다</div>'; return; }
      list.innerHTML = items.map(a => {
        const up = a.pct > 0;
        const impTxt = a.impact ? ' · 월영향 ' + (a.impact > 0 ? '+' : '') + fmtInt(a.impact) + '원' : '';
        return '<div class="alert-row" onclick="openItemModal(\\'' + a.code + '\\')">'
          + '<span class="alert-badge ' + (up ? 'out' : 'low') + '">' + (up ? '▲' : '▼') + Math.abs(a.pct) + '%</span>'
          + '<div><div class="alert-name">' + escapeHtml(a.name) + '</div>'
          + '<div class="alert-code">' + escapeHtml(a.code) + ' · ' + escapeHtml(a.vendor)
          + ' · ' + a.changed + '부터' + impTxt + '</div></div>'
          + '<div class="alert-days" style="color:' + (up ? '#dc2626' : '#16a34a') + ';white-space:nowrap">'
          + fmtNum(a.prev) + '→' + fmtNum(a.cur) + '원</div>'
          + '</div>';
      }).join('');
    } catch (e) {
      list.innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }

  // ───── 재질 단가 계산기 (인라인) ─────
  let _pcalcMaterials = [];
  let _pcalcCat = '';
  const _pcalcSelected = new Set();
  let _pcalcBase = null;  // {price, w, h, d, sizeLabel} — 체크박스 연동 시 기준값
  let _pcalcSizeRaw = '';  // 체크박스 연동 시 원본 사이즈 문자열 (롤파우치 감지용)
  let _pcalcMatRaw  = '';  // 체크박스 연동 시 원본 재질 문자열 (박스 토크나이저용)
  let _pcalcActualPrice = 0;  // 체크박스로 연동한 품목의 실제 단가 (원래 사이즈일 때 표시)

  async function loadPriceCalc() {
    try {
      const res = await fetch('/api/materials');
      const d = await res.json();
      _pcalcMaterials = d.materials || [];
      document.getElementById('pcalc-samples').textContent = d.total_samples || 0;
      document.querySelectorAll('.pcalc-cat-tab').forEach(t => {
        t.addEventListener('click', () => {
          document.querySelectorAll('.pcalc-cat-tab').forEach(x => x.classList.remove('active'));
          t.classList.add('active');
          _pcalcCat = t.dataset.cat || '';
          updatePcalcDepthLabel();
          renderPriceCalcList();
          updatePriceCalc();
        });
      });
      updatePcalcDepthLabel();
      renderPriceCalcList();
      renderSelectedBar();
      updatePriceCalc();
    } catch (e) {
      document.getElementById('pcalc-list').innerHTML = '<div class="search-empty" style="color:#ef4444;padding:12px">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }

  function resetPriceCalc() {
    _pcalcSelected.clear();
    _pcalcActualPrice = 0;
    _pcalcBase = null;
    const cb = document.getElementById('pcalc-compare-bar');
    if (cb) cb.style.display = 'none';
    document.getElementById('pcalc-search').value = '';
    document.getElementById('pcalc-w').value = '';
    document.getElementById('pcalc-h').value = '';
    document.getElementById('pcalc-d').value = '';
    _pcalcCat = '';
    document.querySelectorAll('.pcalc-cat-tab').forEach((t,i) => t.classList.toggle('active', i === 0));
    updatePcalcDepthLabel();
    renderPriceCalcList();
    renderSelectedBar();
    updatePriceCalc();
  }

  function updatePcalcDepthLabel() {
    const el = document.getElementById('pcalc-d-label');
    if (!el) return;
    el.textContent = _pcalcCat === '파우치' ? '밑지'
                   : (_pcalcCat === '단상자' || _pcalcCat === '박스') ? '높이'
                   : '밑지/높이';
  }

  function renderPriceCalcList() {
    const q = (document.getElementById('pcalc-search').value || '').toLowerCase().trim();
    const list = document.getElementById('pcalc-list');
    const filtered = _pcalcMaterials.filter(m => {
      if (q && !m.name.toLowerCase().includes(q)) return false;
      if (_pcalcCat && !(m.categories || []).includes(_pcalcCat)) return false;
      return true;
    });
    if (!filtered.length) {
      list.innerHTML = '<div class="search-empty" style="padding:20px">검색 결과 없음</div>';
      return;
    }
    list.innerHTML = filtered.map(m => {
      const checked = _pcalcSelected.has(m.name) ? 'checked' : '';
      const safeName = m.name.replace(/"/g,'&quot;');
      return '<label class="pcalc-item">'
           + '<input type="checkbox" ' + checked + ' data-name="' + escapeHtml(safeName) + '" onchange="togglePriceCalc(this)">'
           + '<span class="pcalc-name">' + escapeHtml(m.name) + '</span>'
           + '<span class="pcalc-price">' + fmtInt(m.price) + '원</span>'
           + '</label>';
    }).join('');
  }

  function togglePriceCalc(cb) {
    const name = cb.dataset.name;
    if (cb.checked) _pcalcSelected.add(name);
    else _pcalcSelected.delete(name);
    renderSelectedBar();
    updatePriceCalc();
  }

  function removePriceCalc(name) {
    _pcalcSelected.delete(name);
    renderPriceCalcList();
    renderSelectedBar();
    updatePriceCalc();
  }

  function renderSelectedBar() {
    const bar = document.getElementById('pcalc-selected-bar');
    const arr = Array.from(_pcalcSelected);
    if (!arr.length) { bar.innerHTML = ''; return; }
    bar.innerHTML = arr.map(n => {
      const safe = n.replace(/"/g,'&quot;').replace(/'/g,"\\\\'");
      return '<span class="pcalc-sel-chip">' + escapeHtml(n)
           + '<button class="pcalc-sel-x" onclick="removePriceCalc(\\'' + safe + '\\')" title="삭제">&times;</button>'
           + '</span>';
    }).join('');
  }

  async function updatePriceCalc() {
    const estEl = document.getElementById('pcalc-est-inline');
    const simEl = document.getElementById('pcalc-similar');
    if (_pcalcSelected.size === 0) {
      estEl.innerHTML = '0<span style="font-size:10.5px;font-weight:600;margin-left:2px;color:#0369a1">원</span>';
      simEl.innerHTML = '<div class="pcalc-sim-empty">재질 선택 시 유사 제품 표시</div>';
      return;
    }
    const selected = Array.from(_pcalcSelected);
    const selSet = new Set(selected);
    const wInput = +document.getElementById('pcalc-w').value || 0;
    const hInput = +document.getElementById('pcalc-h').value || 0;
    const dInput = +document.getElementById('pcalc-d').value || 0;
    try {
      const res = await fetch('/api/material_estimate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ materials: selected, category: _pcalcCat || '', width: wInput, height: hInput, depth: dInput, size_raw: _pcalcSizeRaw || '', material_raw: _pcalcMatRaw || '' })
      });
      const d = await res.json();
      // 롤파우치 — 예측 불가 안내
      if (d.method === 'roll_film') {
        estEl.innerHTML = '<span style="font-size:11px;color:#dc2626;font-weight:600">⚠ 롤파우치 — 단위면적 단가 예측 불가</span>';
        simEl.innerHTML = '<div class="pcalc-sim-empty">롤파우치는 면적 기반 단가 모델이 적용되지 않습니다</div>';
        return;
      }
      const sizeTag = (wInput > 0 && hInput > 0)
        ? ' · ' + wInput + '×' + hInput + (dInput > 0 ? '×' + dInput : '')
        : '';
      const methodTag = d.method === 'exact_match' ? ' · 실측' : (d.category ? ' · ' + escapeHtml(d.category) : '');
      estEl.innerHTML = fmtInt(Math.round(d.estimate || 0)) + '<span style="font-size:10.5px;font-weight:600;margin-left:2px;color:#0369a1">원 · ' + selected.length + '개' + sizeTag + methodTag + '</span>';

      // 알고있는 품목(체크박스 연동) + 원래 사이즈 그대로면 → 실제 단가를 우선 표시 (예상은 참고)
      if (_pcalcActualPrice > 0 && _pcalcBase
          && wInput === _pcalcBase.w && hInput === _pcalcBase.h && dInput === _pcalcBase.d) {
        estEl.innerHTML =
          '<span style="font-size:9px;font-weight:800;background:#dcfce7;color:#15803d;padding:2px 6px;border-radius:6px;vertical-align:middle">실제</span>'
          + '<span style="color:#059669;font-size:16px;font-weight:800;margin-left:5px;vertical-align:middle">' + fmtInt(_pcalcActualPrice)
          + '<span style="font-size:10px;font-weight:700;margin-left:1px">원</span></span>'
          + '<span style="font-size:10px;font-weight:600;color:#9aa5b1;margin-left:7px;vertical-align:middle">예상 ' + fmtInt(Math.round(d.estimate || 0)) + '원</span>';
      }

      // 비교바 처리
      const cmpBar = document.getElementById('pcalc-compare-bar');
      if (_pcalcBase) {
        if (_pcalcBase.price === null) {
          // 첫 번째 estimate → 기준가 저장
          _pcalcBase.price = d.estimate || 0;
          if (cmpBar) cmpBar.style.display = 'none';
        } else {
          // 사이즈 변경 여부 확인
          const sizeChanged = (wInput !== _pcalcBase.w || hInput !== _pcalcBase.h || dInput !== _pcalcBase.d);
          if (sizeChanged && cmpBar) {
            const basePrice = _pcalcBase.price;
            const newPrice  = d.estimate || 0;
            const diff      = newPrice - basePrice;
            const diffPct   = basePrice > 0 ? Math.round(diff / basePrice * 100) : 0;
            const diffSign  = diff >= 0 ? '+' : '';
            const diffColor = diff > 0 ? '#dc2626' : diff < 0 ? '#16a34a' : '#64748b';
            const diffBg    = diff > 0 ? '#fef2f2' : diff < 0 ? '#f0fdf4' : '#f8fafc';
            // 변경된 치수만 하이라이트하는 사이즈 HTML 생성
            function dimHtml(val, refVal, accentBg, accentColor, baseColor) {
              if (val !== refVal) {
                return '<span style="background:' + accentBg + ';color:' + accentColor + ';font-weight:900;padding:1px 5px;border-radius:4px;font-size:12px">' + val + '</span>';
              }
              return '<span style="color:' + baseColor + ';font-weight:600;font-size:11px">' + val + '</span>';
            }
            function sepHtml(color) { return '<span style="color:' + color + ';font-size:10px;margin:0 1px">×</span>'; }
            const bw = _pcalcBase.w, bh = _pcalcBase.h, bd = _pcalcBase.d;
            const hasD = bw > 0 && (bd > 0 || dInput > 0);
            const baseSzHtml = dimHtml(bw, wInput, '#bfdbfe', '#1e3a8a', '#3b82f6') + sepHtml('#93c5fd')
              + dimHtml(bh, hInput, '#bfdbfe', '#1e3a8a', '#3b82f6')
              + (hasD ? sepHtml('#93c5fd') + dimHtml(bd, dInput, '#bfdbfe', '#1e3a8a', '#3b82f6') : '');
            const newSzHtml = dimHtml(wInput, bw, '#ddd6fe', '#3b0764', '#7c3aed') + sepHtml('#c4b5fd')
              + dimHtml(hInput, bh, '#ddd6fe', '#3b0764', '#7c3aed')
              + (hasD ? sepHtml('#c4b5fd') + dimHtml(dInput, bd, '#ddd6fe', '#3b0764', '#7c3aed') : '');
            cmpBar.style.display = 'block';
            cmpBar.innerHTML =
              '<div style="padding:8px 12px;background:linear-gradient(135deg,#eff6ff 0%,#f5f3ff 100%);border:1px solid #c7d2fe;border-radius:10px;font-size:11px">'
              // ── 단가 행 ──
              + '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
              + '<div style="display:flex;flex-direction:column;align-items:center;background:#dbeafe;border-radius:7px;padding:4px 12px;min-width:64px">'
              + '<span style="font-size:9px;font-weight:800;color:#1d4ed8;letter-spacing:0.04em">기준</span>'
              + '<span style="font-weight:900;color:#1d4ed8;font-size:15px;line-height:1.25">' + fmtInt(basePrice) + '<span style="font-size:10px;font-weight:600">원</span></span>'
              + '</div>'
              + '<span style="color:#a5b4fc;font-size:16px">→</span>'
              + '<div style="display:flex;flex-direction:column;align-items:center;background:#ede9fe;border-radius:7px;padding:4px 12px;min-width:64px">'
              + '<span style="font-size:9px;font-weight:800;color:#6d28d9;letter-spacing:0.04em">변경</span>'
              + '<span style="font-weight:900;color:#6d28d9;font-size:15px;line-height:1.25">' + fmtInt(newPrice) + '<span style="font-size:10px;font-weight:600">원</span></span>'
              + '</div>'
              + (diff !== 0
                ? '<div style="margin-left:4px;display:flex;flex-direction:column;align-items:center;background:' + diffBg + ';border-radius:7px;padding:4px 12px;min-width:64px">'
                  + '<span style="font-size:9px;font-weight:700;color:' + diffColor + '">차이</span>'
                  + '<span style="font-weight:900;font-size:13px;color:' + diffColor + '">' + diffSign + fmtInt(diff) + '원</span>'
                  + '<span style="font-size:10px;font-weight:700;color:' + diffColor + '">(' + diffSign + diffPct + '%)</span>'
                  + '</div>'
                : '')
              + '</div>'
              // ── 사이즈 행 ──
              + '<div style="margin-top:7px;padding-top:6px;border-top:1px dashed #c7d2fe;display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
              + '<span style="font-size:9.5px;font-weight:700;color:#64748b;min-width:28px">사이즈</span>'
              + '<span style="display:inline-flex;align-items:center;gap:1px">' + baseSzHtml + '</span>'
              + '<span style="color:#a5b4fc;font-size:13px">→</span>'
              + '<span style="display:inline-flex;align-items:center;gap:1px">' + newSzHtml + '</span>'
              + '<span style="font-size:9px;color:#64748b">(강조 = 변경된 치수)</span>'
              + '</div>'
              + '</div>';
          } else if (!sizeChanged && cmpBar) {
            cmpBar.style.display = 'none';
          }
        }
      } else if (cmpBar) {
        cmpBar.style.display = 'none';
      }
      const sim = d.similar || [];
      if (!sim.length) {
        simEl.innerHTML = '<div class="pcalc-sim-empty">일치하는 샘플 없음</div>';
        return;
      }
      simEl.innerHTML = sim.map((s, i) => {
        const mats = (s.materials || []).map(m => {
          const safeAttr = m.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
          return '<span class="pcalc-mat-chip clickable' + (selSet.has(m) ? ' match' : '') + '"'
               + ' data-mat="' + safeAttr + '"'
               + ' onclick="event.stopPropagation();togglePcalcMatChip(this.dataset.mat)"'
               + ' title="클릭하여 재질 추가/제거">' + escapeHtml(m) + '</span>';
        }).join('');
        let metaHtml = '';
        if (s.vendor || s.size) {
          const vendorPart = escapeHtml(s.vendor || '');
          const sizePart = s.size
            ? '<span class="pcalc-sim-size clickable" data-size="' + escapeHtml(s.size).replace(/"/g,'&quot;')
              + '" onclick="event.stopPropagation();applyPcalcSize(this.dataset.size)"'
              + ' title="클릭하여 사이즈 적용"> · ' + escapeHtml(s.size) + '</span>'
            : '';
          metaHtml = '<div class="pcalc-sim-meta">' + vendorPart + sizePart + '</div>';
        }
        const codeKey = (s.code || ('idx' + i));
        const codeAttr = codeKey.replace(/&/g,'&amp;').replace(/"/g,'&quot;');
        const isOpen = _pcalcExpandedCodes.has(codeKey);
        return '<div class="pcalc-sim" data-codekey="' + codeAttr + '" onclick="togglePcalcSim(this.dataset.codekey, ' + i + ')">'
             + '<div class="pcalc-sim-head">'
             + '<span class="pcalc-sim-code">' + escapeHtml(s.code || '-') + ' · ' + s.overlap + '/' + s.total + '재질</span>'
             + '<span class="pcalc-sim-price">' + fmtInt(s.price) + '원</span>'
             + '</div>'
             + '<div class="pcalc-sim-name">' + escapeHtml(s.name) + '</div>'
             + metaHtml
             + '<div class="pcalc-sim-mats" id="pcalc-mats-' + i + '" style="display:' + (isOpen ? 'flex' : 'none') + '">' + mats + '</div>'
             + '</div>';
      }).join('');
    } catch (e) {
      simEl.innerHTML = '<div class="pcalc-sim-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }

  // 펼친 유사 샘플(코드 키 기준) — 재렌더 후에도 유지
  const _pcalcExpandedCodes = new Set();
  function togglePcalcSim(codeKey, i) {
    const decoded = codeKey.replace(/&quot;/g,'"').replace(/&amp;/g,'&');
    if (_pcalcExpandedCodes.has(decoded)) _pcalcExpandedCodes.delete(decoded);
    else _pcalcExpandedCodes.add(decoded);
    const el = document.getElementById('pcalc-mats-' + i);
    if (el) el.style.display = _pcalcExpandedCodes.has(decoded) ? 'flex' : 'none';
  }

  function togglePcalcMatChip(name) {
    // 유사 샘플 모달의 재질 칩 클릭 → 메인 선택 리스트에 추가/제거
    const decoded = name.replace(/&#39;/g,"'").replace(/&quot;/g,'"').replace(/&amp;/g,'&');
    if (_pcalcSelected.has(decoded)) {
      _pcalcSelected.delete(decoded);
    } else {
      _pcalcSelected.add(decoded);
    }
    renderPriceCalcList();
    renderSelectedBar();
    updatePriceCalc();
  }

  function applyPcalcSize(sizeStr) {
    // "100*85*122" 또는 "270*350+밑지90" 형식 파싱하여 W/H/D 입력란에 적용
    const decoded = sizeStr.replace(/&#39;/g,"'").replace(/&quot;/g,'"').replace(/&amp;/g,'&');
    let w = 0, h = 0, d = 0;
    const wxhxd = decoded.match(/(\d+)\s*\*\s*(\d+)\s*\*\s*(\d+)/);
    if (wxhxd) {
      w = +wxhxd[1]; h = +wxhxd[2]; d = +wxhxd[3];
    } else {
      const wxh = decoded.match(/(\d+)\s*\*\s*(\d+)/);
      if (wxh) { w = +wxh[1]; h = +wxh[2]; }
      const m = decoded.match(/밑지\s*(\d+)/);
      if (m) d = +m[1];
    }
    if (w > 0) document.getElementById('pcalc-w').value = w;
    if (h > 0) document.getElementById('pcalc-h').value = h;
    if (d > 0) document.getElementById('pcalc-d').value = d;
    updatePriceCalc();
  }

  // ───── 발주완료 모달 ─────
  let _poItems = [];
  let _osModalItems = [];
  // 말풍선 팝업 — Monday 업데이트 원문 표시 (공용)
  function showPoUpdates(idx) { _showUpdatesPopup(_poItems[idx]); }
  function showOsUpdates(idx) { _showUpdatesPopup(_osModalItems[idx]); }
  function _showUpdatesPopup(it) {
    if (!it) return;
    const ups = it.updates || [];
    const det = it.mon_detail || [];
    const pop = document.getElementById('po-upd-modal');
    document.getElementById('po-upd-title').textContent =
      (it.code || '') + ' ' + (it.name || '');
    const bodyEl = document.getElementById('po-upd-body');
    let html = '';
    if (det.length) {
      html += '<div style="border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:' + (ups.length ? '12px' : '0') + '">'
        + det.map((d, i) =>
            '<div style="display:flex;font-size:12.5px;' + (i ? 'border-top:1px solid var(--border);' : '') + '">'
            + '<span style="width:96px;flex:none;padding:7px 10px;background:var(--surface-2);color:var(--text-2);font-weight:700">' + escapeHtml(d.label) + '</span>'
            + '<span style="flex:1;padding:7px 10px;color:var(--text)">' + escapeHtml(d.value) + '</span>'
            + '</div>').join('')
        + '</div>';
    }
    if (ups.length) {
      html += ups.map(u => {
        const mine = ['남소민', '김장군'].includes(u.author || '');
        return '<div class="po-bubble' + (mine ? ' mine' : '') + '">'
          + '<div class="po-bubble-head"><b>' + escapeHtml(u.author || '') + '</b>'
          + '<span>' + escapeHtml(u.posted || '') + '</span></div>'
          + '<div class="po-bubble-body">' + escapeHtml(u.body || '').replace(/\\n/g, '<br>') + '</div>'
          + '</div>';
      }).join('');
    } else if (!det.length) {
      html = '<div class="search-empty">말풍선(업데이트)이 없습니다</div>';
    }
    bodyEl.innerHTML = html;
    pop.classList.add('show');
  }
  function showImportDetail(idx) { _showUpdatesPopup(_poInlineItems[idx]); }
  function closePoUpdates() { document.getElementById('po-upd-modal').classList.remove('show'); }

  async function openPoPending() {
    const modal = document.getElementById('po-modal');
    const body = document.getElementById('po-body');
    modal.classList.add('show');
    body.innerHTML = '<div class="loading" style="padding:24px">로딩 중...</div>';
    try {
      const res = await fetch('/api/po_pending');
      const d = await res.json();
      const items = d.items || [];
      _poItems = items;
      if (!items.length) {
        body.innerHTML = '<div class="search-empty">발주완료 항목이 없습니다</div>';
        return;
      }
      const pending = items.filter(x => !x.has_schedule).length;
      const summary = '<div style="display:flex;gap:16px;margin-bottom:12px;padding:10px 14px;background:var(--surface-2);border-radius:10px;font-size:12px">'
        + '<span>총 <b>' + items.length + '</b>건</span>'
        + '<span style="color:var(--text-2)">일정 미정: <b style="color:#b91c1c">' + pending + '</b>건</span>'
        + '<span style="color:var(--text-2)">일정 확정: <b style="color:#059669">' + (items.length - pending) + '</b>건</span>'
        + '</div>';
      const head = '<div class="po-row head">'
        + '<div>품번</div><div>품명</div><div style="text-align:right">요청수량</div>'
        + '<div>발주요청일</div><div>발주업체</div><div>입고처</div><div>입고 일정</div></div>';
      const rows = items.map((it, idx) => {
        let schHtml;
        if (it.type === 'po') {
          const hasUpd = (it.updates || []).length > 0;
          const clickAttr = hasUpd
            ? ' onclick="showPoUpdates(' + idx + ')" style="cursor:pointer" title="말풍선 보기"'
            : '';
          const bubbleIco = hasUpd ? '<span class="po-sch-bubble">💬</span>' : '';
          if (it.has_schedule && (it.schedule_entries || []).length) {
            const latest = it.schedule_entries[0];
            const chips = (latest.dates || []).map(d =>
              '<span class="po-sch-chip">' + escapeHtml(d) + '</span>'
            ).join('');
            schHtml = '<div class="po-schedule"' + clickAttr + '>' + chips + bubbleIco
                    + '<div class="po-sch-meta">' + escapeHtml(latest.posted) + ' ' + escapeHtml(latest.author || '') + '</div></div>';
          } else {
            schHtml = '<div class="po-schedule empty"' + clickAttr + '>미정' + bubbleIco + '</div>';
          }
        } else {
          // 원료
          schHtml = it.has_schedule
            ? '<div class="po-schedule"><span class="po-sch-chip">' + escapeHtml(it.eta_display) + '</span>'
              + '<div class="po-sch-meta">입항 예정</div></div>'
            : '<div class="po-schedule empty">미정</div>';
        }
        const qtyDisplay = it.type === 'raw' ? escapeHtml(it.qty || '') : fmtNum(it.qty);
        const codeColor = it.type === 'raw' ? '#7c3aed' : '#059669';
        return '<div class="po-row">'
          + '<div class="po-code" style="color:' + codeColor + '">' + escapeHtml(it.code) + '</div>'
          + '<div class="po-name">' + escapeHtml(it.name) + '</div>'
          + '<div class="po-qty">' + qtyDisplay + '</div>'
          + '<div class="po-date">' + escapeHtml(it.request_date) + '</div>'
          + '<div class="po-vendor">' + escapeHtml(it.vendor) + '</div>'
          + '<div class="po-vendor">' + escapeHtml(it.destination || '') + '</div>'
          + schHtml
          + '</div>';
      }).join('');
      body.innerHTML = summary + '<div style="border:1px solid var(--border);border-radius:10px;overflow:hidden">' + head + rows + '</div>';
    } catch (e) {
      body.innerHTML = '<div class="search-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }
  function closePoPending() { document.getElementById('po-modal').classList.remove('show'); }
  document.getElementById('po-modal').addEventListener('click', (e) => {
    if (e.target.id === 'po-modal') closePoPending();
  });
  document.getElementById('po-upd-modal').addEventListener('click', (e) => {
    if (e.target.id === 'po-upd-modal') closePoUpdates();
  });

  let _osInlineItems = [];
  async function loadOsInline() {
    try {
      const res = await fetch('/api/outsource_pending');
      const d = await res.json();
      _osInlineItems = d.items || [];
      renderOsInline();
    } catch (e) {
      document.getElementById('os-inline-list').innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }
  // ───── 시방서 미리보기/다운로드 ─────
  let _specMenuEl = null;
  function closeSpecMenu() {
    if (_specMenuEl) { _specMenuEl.remove(); _specMenuEl = null; }
  }
  document.addEventListener('click', closeSpecMenu);

  function openSpecMenu(ev, itemId) {
    closeSpecMenu();
    const menu = document.createElement('div');
    menu.className = 'spec-menu';
    menu.innerHTML =
        '<button onclick="previewSpec(\\'' + itemId + '\\')"><span class="ico">👁</span> 미리보기</button>'
      + '<button onclick="downloadSpec(\\'' + itemId + '\\')"><span class="ico">⬇</span> 다운로드</button>';
    document.body.appendChild(menu);
    // 버튼 위치 기준 배치
    const rect = ev.currentTarget.getBoundingClientRect();
    let top = rect.bottom + 4, left = rect.left;
    if (left + 160 > window.innerWidth) left = window.innerWidth - 165;
    if (top + 90 > window.innerHeight) top = rect.top - 90;
    menu.style.top = top + 'px';
    menu.style.left = left + 'px';
    menu.addEventListener('click', e => e.stopPropagation());
    _specMenuEl = menu;
  }

  async function _fetchSpec(itemId) {
    const res = await fetch('/api/spec_file/' + itemId);
    return await res.json();
  }

  async function downloadSpec(itemId) {
    closeSpecMenu();
    try {
      const d = await _fetchSpec(itemId);
      if (!d.ok) { alert('시방서를 찾을 수 없습니다: ' + (d.error || '')); return; }
      window.open(d.download_url, '_blank');
    } catch (e) { alert('오류: ' + e.message); }
  }

  async function previewSpec(itemId) {
    closeSpecMenu();
    const modal = document.getElementById('spec-preview-modal');
    const content = document.getElementById('spec-preview-content');
    const title = document.getElementById('spec-preview-title');
    const dl = document.getElementById('spec-preview-dl');
    modal.classList.add('show');
    content.innerHTML = '<div class="loading" style="margin:auto">시방서 불러오는 중...</div>';
    try {
      const d = await _fetchSpec(itemId);
      if (!d.ok) { content.innerHTML = '<div class="search-empty" style="margin:auto">시방서 없음: ' + escapeHtml(d.error||'') + '</div>'; return; }
      title.textContent = d.name || '시방서 미리보기';
      dl.href = d.download_url;
      if (d.preview_type === 'image') {
        content.innerHTML = '<img src="' + escapeHtml(d.preview_url) + '" alt="시방서">';
      } else {
        // office viewer / pdf → iframe
        content.innerHTML = '<iframe src="' + escapeHtml(d.preview_url) + '" '
          + 'sandbox="allow-scripts allow-same-origin allow-popups allow-forms"></iframe>';
      }
    } catch (e) {
      content.innerHTML = '<div class="search-empty" style="margin:auto;color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }

  function closeSpecPreview() {
    const modal = document.getElementById('spec-preview-modal');
    modal.classList.remove('show');
    document.getElementById('spec-preview-content').innerHTML = '';
  }
  document.getElementById('spec-preview-modal').addEventListener('click', (e) => {
    if (e.target.id === 'spec-preview-modal') closeSpecPreview();
  });

  function renderOsInline() {
    const list = document.getElementById('os-inline-list');
    const cnt = document.getElementById('os-inline-count');
    const q = (document.getElementById('os-inline-search').value || '').toLowerCase().trim();
    const all = _osInlineItems;
    const items = q
      ? all.filter(it => (it.code||'').toLowerCase().includes(q) || (it.name||'').toLowerCase().includes(q))
      : all;
    const hold = items.filter(x => x.is_hold).length;
    const requesting = items.filter(x => x.is_requesting).length;
    const pending = items.filter(x => !x.has_schedule && !x.is_hold && !x.is_requesting).length;
    cnt.textContent = items.length + '/' + all.length + '건 · 미정 ' + pending + ' · 생산요청 ' + requesting + ' · 보류 ' + hold;
    if (!items.length) {
      list.innerHTML = '<div class="alert-empty">' + (q ? '검색 결과 없음' : '외주 입고 항목 없음') + '</div>';
      return;
    }
    list.innerHTML = items.map(it => {
      const code = escapeHtml(it.code || '-');
      const name = escapeHtml(it.name || '');
      let chip;
      if (it.is_hold) {
        chip = '<span class="po-inline-chip hold">보류</span>';
      } else if (it.is_requesting) {
        chip = it.has_schedule
          ? '<span class="po-inline-chip" style="background:#7c3aed;color:#fff">' + escapeHtml(it.eta_display) + '</span>'
          : '<span class="po-inline-chip" style="background:#7c3aed;color:#fff">생산요청</span>';
      } else {
        chip = it.has_schedule
          ? '<span class="po-inline-chip">' + escapeHtml(it.eta_display || '-') + '</span>'
          : '<span class="po-inline-chip empty">미정</span>';
      }
      const destChip = it.destination
        ? '<span class="po-inline-dest" title="클릭하여 펼치기/접기" onclick="event.stopPropagation();this.classList.toggle(\\'expanded\\')">' + escapeHtml(it.destination) + '</span>'
        : '<span></span>';
      const qtyChip = it.qty
        ? '<span class="po-inline-qty" title="' + escapeHtml(it.qty) + '">' + escapeHtml(it.qty) + '</span>'
        : '<span></span>';
      const vendor = it.vendor ? escapeHtml(it.vendor) : '';
      const groupLabel = it.is_requesting ? '생산요청' : (it.is_hold ? '확인필요' : '발주완료');
      const groupColor = it.is_requesting ? '#7c3aed' : (it.is_hold ? '#dc2626' : '#059669');
      // 시방서 버튼 — 클릭 시 미리보기/다운로드 메뉴 (빈 공간 셀에 배치)
      const specBtn = (it.has_spec && it.item_id)
        ? '<button class="spec-doc-btn" onclick="event.stopPropagation();openSpecMenu(event,\\'' + escapeHtml(it.item_id) + '\\')" '
          + 'title="시방서 보기">📄 시방서</button>'
        : '<span></span>';
      return '<div class="po-inline-row os" onclick="openOsModal()">'
           + '<div style="display:flex;flex-direction:column;align-items:flex-start;gap:1px">'
           + '<span class="po-inline-code">' + code + '</span>'
           + '<span style="font-size:9px;font-weight:700;color:' + groupColor + ';letter-spacing:-0.01em">' + groupLabel + '</span>'
           + '</div>'
           + '<div><div class="po-inline-name" title="' + name + '">' + name + '</div>'
           + '<div class="po-inline-sub">' + vendor + '</div></div>'
           + specBtn + qtyChip + destChip + chip + '</div>';
    }).join('');
  }

  async function openOsModal() {
    const modal = document.getElementById('os-modal');
    const body = document.getElementById('os-body');
    modal.classList.add('show');
    body.innerHTML = '<div class="loading" style="padding:24px">로딩 중...</div>';
    try {
      const res = await fetch('/api/outsource_pending');
      const d = await res.json();
      const items = d.items || [];
      _osModalItems = items;
      if (!items.length) {
        body.innerHTML = '<div class="search-empty">외주 발주완료 항목이 없습니다</div>';
        return;
      }
      const hold = items.filter(x => x.is_hold).length;
      const pending = items.filter(x => !x.has_schedule && !x.is_hold).length;
      const confirmed = items.length - pending - hold;
      const summary = '<div style="display:flex;gap:16px;margin-bottom:12px;padding:10px 14px;background:var(--surface-2);border-radius:10px;font-size:12px">'
        + '<span>총 <b>' + items.length + '</b>건</span>'
        + '<span style="color:var(--text-2)">일정 확정: <b style="color:#059669">' + confirmed + '</b>건</span>'
        + '<span style="color:var(--text-2)">일정 미정: <b style="color:#b91c1c">' + pending + '</b>건</span>'
        + '<span style="color:var(--text-2)">보류: <b style="color:#c2410c">' + hold + '</b>건</span>'
        + '</div>';
      const head = '<div class="po-row head os-modal-row">'
        + '<div>품번</div><div>품명</div><div style="text-align:right">요청수량</div>'
        + '<div>요청입고일</div><div>업체명</div><div>입고지</div><div>시방서</div><div>입고 일정</div></div>';
      const rows = items.map((it, idx) => {
        let schHtml;
        const hasUpd = (it.updates || []).length > 0;
        const clickAttr = hasUpd
          ? ' onclick="showOsUpdates(' + idx + ')" style="cursor:pointer" title="말풍선 보기"'
          : '';
        const bubbleIco = hasUpd ? '<span class="po-sch-bubble">💬</span>' : '';
        if (it.is_hold) {
          schHtml = '<div class="po-schedule hold"' + clickAttr + '>보류 (확인 필요)' + bubbleIco + '</div>';
        } else if (it.has_schedule && (it.schedule_entries || []).length) {
          const latest = it.schedule_entries[0];
          const chips = (latest.dates || []).map(d =>
            '<span class="po-sch-chip">' + escapeHtml(d) + '</span>'
          ).join('');
          schHtml = '<div class="po-schedule"' + clickAttr + '>' + chips + bubbleIco
                  + '<div class="po-sch-meta">' + escapeHtml(latest.posted) + ' 남소민</div></div>';
        } else {
          schHtml = '<div class="po-schedule empty"' + clickAttr + '>미정' + bubbleIco + '</div>';
        }
        const specCell = it.has_spec
          ? '<a href="' + escapeHtml(it.spec_url) + '" target="_blank" rel="noopener" '
            + 'title="시방서 열기 (Monday 로그인 필요)" '
            + 'style="display:inline-flex;align-items:center;gap:2px;font-size:11px;font-weight:700;color:#0369a1;'
            + 'background:#e0f2fe;padding:2px 8px;border-radius:6px;text-decoration:none">📄 열기</a>'
          : '<span style="color:var(--text-3);font-size:11px">-</span>';
        return '<div class="po-row os-modal-row">'
          + '<div class="po-code" style="color:#dc2626">' + escapeHtml(it.code) + '</div>'
          + '<div class="po-name">' + escapeHtml(it.name) + '</div>'
          + '<div class="po-qty">' + escapeHtml(it.qty || '') + '</div>'
          + '<div class="po-date">' + escapeHtml(it.requested_eta || '') + '</div>'
          + '<div class="po-vendor">' + escapeHtml(it.vendor || '') + '</div>'
          + '<div class="po-vendor">' + escapeHtml(it.destination || '') + '</div>'
          + '<div>' + specCell + '</div>'
          + schHtml
          + '</div>';
      }).join('');
      body.innerHTML = summary + '<div style="border:1px solid var(--border);border-radius:10px;overflow:hidden">' + head + rows + '</div>';
    } catch (e) {
      body.innerHTML = '<div class="search-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }
  function closeOsModal() { document.getElementById('os-modal').classList.remove('show'); }
  document.getElementById('os-modal').addEventListener('click', (e) => {
    if (e.target.id === 'os-modal') closeOsModal();
  });

  loadAlerts('jasa');
  loadAlerts('outsource');
  loadReorder();
  loadPriceChanges();
  loadNotifyBadge();
  loadPlan();
  loadChStock();
  loadSalesGap();
  loadChannelPrice();
  loadWeekday();
  loadVendors();
  loadPoInline();
  loadOsInline();
  loadPriceCalc();
  loadSpecList();
  loadIpsuSpecs();
  // loadSalesSummary();   // 2026-09-23 매출 현황 패널 제거 — 월 매출·판매 추이 [채널별] 모드가 /api/sales_summary 사용
  loadOrderReceipt();
  loadSalesBased();
  loadKpi();

  // ───── KPI 요약 띠 ─────
  async function loadKpi() {
    try {
      const d = await (await fetch('/api/kpi_summary')).json();
      const set = (id, v, sub, lv) => {
        const el = document.getElementById(id); if (!el) return;
        el.querySelector('.kpi-v').textContent = v;
        el.querySelector('.kpi-s').textContent = sub || '';
        el.className = 'kpi-tile lv-' + lv;
      };
      const st = d.stock || {}, ro = d.reorder || {}, inc = d.incoming || {}, su = d.supply || {}, pr = d.price || {}, he = d.health || {};
      set('kpi-stock', st.out || 0, '위험 ' + (st.critical || 0) + ' · 자사 ' + (st.jasa_out || 0) + '/외주 ' + (st.os_out || 0), st.out > 0 ? 'bad' : (st.critical > 0 ? 'warn' : 'ok'));
      set('kpi-reorder', ro.now || 0, '이번주 ' + (ro.soon || 0) + '건', ro.now > 0 ? 'bad' : (ro.soon > 0 ? 'warn' : 'ok'));
      set('kpi-incoming', inc.unknown || 0, '전체 ' + (inc.total || 0) + ' · 7일내 ' + (inc.week || 0) + (inc.overdue ? ' · 지연 ' + inc.overdue : ''), inc.overdue > 0 ? 'bad' : (inc.unknown > 0 ? 'warn' : 'ok'));
      set('kpi-supply', (su.out || 0) + (su.critical || 0), '품절 ' + (su.out || 0) + ' · 2주↓ ' + (su.critical || 0) + ' · 4주↓ ' + (su.warning || 0), su.out > 0 ? 'bad' : (su.critical > 0 ? 'warn' : 'ok'));
      set('kpi-price', pr.total || 0, '인상 ' + (pr.up || 0) + ' · 인하 ' + ((pr.total || 0) - (pr.up || 0)) + ' (6개월)', pr.up > 0 ? 'warn' : 'info');
      const hv = he.overall === 'ok' ? '정상' : (he.overall === 'warn' ? '확인' : (he.overall === 'error' ? '이상' : '–'));
      set('kpi-health', hv, (he.summary || '') + (d.at ? ' · ' + d.at : ''), he.overall === 'ok' ? 'ok' : (he.overall === 'warn' ? 'warn' : (he.overall ? 'bad' : 'info')));
    } catch (e) { /* KPI는 부가 정보 — 실패해도 조용히 */ }
  }
  // 고정 헤더에 가리지 않도록 헤더 높이 + 여백만큼 위로 띄워 스크롤 (KPI 클릭·섹션 내비 공용)
  function scrollToPanel(el, highlight) {
    const hdr = document.querySelector('header');
    const pos = hdr ? getComputedStyle(hdr).position : '';
    const off = ((pos === 'sticky' || pos === 'fixed') ? hdr.offsetHeight : 0) + 16;
    window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - off, behavior: 'smooth' });
    if (highlight) { el.style.boxShadow = '0 0 0 3px #4f46e5'; setTimeout(() => el.style.boxShadow = '', 1600); }
  }
  document.querySelectorAll('.kpi-tile').forEach(t => t.addEventListener('click', () => {
    const go = t.dataset.go;
    if (go === 'health') { if (typeof openDataHealth === 'function') openDataHealth(); return; }
    const el = document.querySelector(go); if (!el) return;
    const sec = el.closest('section'); if (sec && sec.classList.contains('collapsed')) toggleSection(sec, false);
    scrollToPanel(el, true);
  }));

  // ───── 회송 원가 역산 ─────
  let _rcTimer = null, _rcRows = [], _rcSel = 0, _rcData = null, _rcOff = new Set(), _rcGroup = 'all';
  function rcGroup(g, btn) {
    _rcGroup = g;
    document.querySelectorAll('#rc-grp .vk-chip').forEach(b => b.classList.toggle('on', b.dataset.g === g));
    const inp = document.getElementById('rc-q'); if (inp.value.trim()) { rcSuggest(); inp.focus(); }
  }
  const _rcFmt = n => Math.round(n).toLocaleString();
  const _rcQty = n => (Math.abs(n) >= 100 ? Math.round(n) : Math.round(n * 100) / 100).toLocaleString();
  function rcSuggest() {
    clearTimeout(_rcTimer);
    const q = document.getElementById('rc-q').value.trim();
    const box = document.getElementById('rc-sug');
    if (!q) { box.style.display = 'none'; return; }
    _rcTimer = setTimeout(async () => {
      try {
        const d = await (await fetch('/api/search?q=' + encodeURIComponent(q))).json();
        const all = d.products || [];   // 서버가 G/H/I 우선 정렬
        _rcRows = (_rcGroup === 'all' ? all : all.filter(p => _rcGroup === 'HI' ? (p.group === 'H' || p.group === 'I') : p.group === _rcGroup)).slice(0, 40); _rcSel = 0;
        if (!_rcRows.length) { box.style.display = 'none'; return; }
        box.innerHTML = _rcRows.map((p, i) => '<div class="' + (i === _rcSel ? 'sel' : '') + (p.bom === false ? ' nobom' : '') + '" onmousedown="rcPick(' + i + ')"><span class="c">' + escapeHtml(p.code) + '</span><span>' + escapeHtml(p.name || '') + '</span>'
          + (p.bom === false ? '<span class="rc-nobom">BOM 없음</span>' : '')
          + '<span class="t">' + ({ G: '자사', H: '외주·유상사급', I: '외주·상품매입', E: '반제품' }[p.group] || '') + '</span></div>').join('');
        box.style.display = 'block';
      } catch (e) { box.style.display = 'none'; }
    }, 180);
  }
  function rcKey(e) {
    const box = document.getElementById('rc-sug');
    if (box.style.display === 'none' || !_rcRows.length) { if (e.key === 'Enter') rcRun(); return; }
    if (e.key === 'ArrowDown') { _rcSel = Math.min(_rcRows.length - 1, _rcSel + 1); }
    else if (e.key === 'ArrowUp') { _rcSel = Math.max(0, _rcSel - 1); }
    else if (e.key === 'Enter') { e.preventDefault(); rcPick(_rcSel); return; }
    else if (e.key === 'Escape') { box.style.display = 'none'; return; }
    else return;
    e.preventDefault();
    [...box.children].forEach((el, i) => el.classList.toggle('sel', i === _rcSel));
  }
  function rcPick(i) {
    const p = _rcRows[i]; if (!p) return;
    document.getElementById('rc-q').value = p.code;
    document.getElementById('rc-sug').style.display = 'none';
    if (p.bom === false) {
      document.getElementById('rc-msg').innerHTML = '<span style="color:#b91c1c;font-weight:700">' + escapeHtml(p.code) + ' 은(는) 아마란스에 BOM이 등록되어 있지 않아 역산할 수 없습니다.</span> 아마란스 BOM 등록 후 다음 수집(1시간 내)부터 계산됩니다.';
      document.getElementById('rc-summary').style.display = 'none'; document.getElementById('rc-table').style.display = 'none';
      return;
    }
    rcRun();
  }
  // 수량 (2026-09-23): 비었거나 0 이하면 0 → 수량에 따라 달라지는 금액·총소요량은 비움.
  // 금액은 수량에 정비례하므로 서버는 품번이 바뀔 때만 부르고, 수량 변경은 화면에서 바로 곱한다(늦게 온 이전 응답이 덮어쓰는 문제도 제거).
  function _rcQtyNow() { const v = parseFloat(document.getElementById('rc-qty').value); return (isFinite(v) && v > 0) ? v : 0; }
  function _rcCode() { const raw = document.getElementById('rc-q').value.trim(); return (raw.split(/[ \t]/)[0] || '').toUpperCase(); }
  function rcQtyChange() {
    if (_rcData && _rcData.code === _rcCode()) rcRender();   // 같은 품번 → 즉시 재계산
    else rcRun();
  }
  let _rcSeq = 0;
  async function rcRun() {
    const raw = document.getElementById('rc-q').value.trim();
    const code = _rcCode();
    const msg = document.getElementById('rc-msg');
    if (!/^[A-Z][0-9]{3,}/.test(code)) { msg.textContent = raw ? '품번을 선택해 주세요' : ''; return; }
    msg.textContent = '계산 중...';
    const seq = ++_rcSeq;
    try {
      const r = await fetch('/api/return_cost?code=' + encodeURIComponent(code) + '&qty=1');   // 1개 기준으로 받아 수량은 화면에서 곱함
      const d = await r.json();
      if (seq !== _rcSeq) return;   // 더 최근 요청이 있으면 버림
      if (!r.ok) { msg.textContent = d.error || '오류'; document.getElementById('rc-summary').style.display = 'none'; document.getElementById('rc-table').style.display = 'none'; _rcData = null; return; }
      _rcData = d; _rcOff = new Set(d.lines.filter(x => x.grp === '기타').map(x => x.code));
      msg.textContent = '';
      rcRender();
    } catch (e) { if (seq === _rcSeq) msg.textContent = '오류: ' + e.message; }
  }
  function rcToggle(code) { if (_rcOff.has(code)) _rcOff.delete(code); else _rcOff.add(code); rcRender(); }
  // 1개 기준 금액·소요량 (서버 응답 qty로 나눠 정규화)
  const _rcUnitAmt = x => (x.unit_amount != null ? x.unit_amount : x.amount / (_rcData.qty || 1));
  const _rcUnitTot = x => (x.unit_qty != null ? x.unit_qty : x.total_qty / (_rcData.qty || 1));
  function rcRender() {
    const d = _rcData; if (!d) return;
    const q = _rcQtyNow();
    const on = d.lines.filter(x => !_rcOff.has(x.code));
    const usum = g => on.filter(x => x.grp === g).reduce((a, x) => a + _rcUnitAmt(x), 0);   // 1개 기준
    const uraw = usum('원재료'), usub = usum('부재료'), uetc = usum('기타'), unit = uraw + usub + uetc;
    const raw = uraw * q, sub = usub * q, etc = uetc * q, tot = unit * q;
    const won = v => q ? _rcFmt(v) + '원' : '-';
    const ratio = d.sale_price > 0 ? (unit / d.sale_price * 100).toFixed(1) + '%' : '-';
    const kindTxt = d.bom_kind && d.bom_kind !== '일반' ? ' · ' + d.bom_kind + 'BOM' + (d.bom_vendor ? '(' + d.bom_vendor + ')' : '') : '';
    document.getElementById('rc-count').textContent = escapeHtml(d.code) + ' · ' + (d.name || '') + ' · 자재 ' + d.lines.length + '종' + kindTxt + (d.missing ? ' · 단가없음 ' + d.missing : '');
    const S = document.getElementById('rc-summary');
    S.style.display = 'grid';
    const noq = '<div class="s" style="color:#b45309">수량을 입력하세요</div>';
    S.innerHTML = '<div class="rc-card raw"><div class="k">원재료</div><div class="v">' + won(raw) + '</div>' + (q ? '<div class="s">' + on.filter(x => x.grp === '원재료').length + '종</div>' : noq) + '</div>'
      + '<div class="rc-card sub"><div class="k">부재료</div><div class="v">' + won(sub) + '</div>' + (q ? '<div class="s">' + on.filter(x => x.grp === '부재료').length + '종</div>' : noq) + '</div>'
      + '<div class="rc-card tot"><div class="k">합계' + (q ? ' (' + _rcQty(q) + '개)' : '') + '</div><div class="v">' + won(tot) + '</div>' + (q ? '<div class="s">' + (etc ? '기타 ' + _rcFmt(etc) + '원 포함' : '체크 해제 항목 제외') + '</div>' : noq) + '</div>'
      + '<div class="rc-card"><div class="k">개당 자재원가</div><div class="v">' + _rcFmt(unit) + '원</div><div class="s">원재료 ' + _rcFmt(uraw) + ' + 부재료 ' + _rcFmt(usub) + '</div></div>'
      + '<div class="rc-card"><div class="k">판매단가 대비</div><div class="v">' + ratio + '</div><div class="s">' + (d.sale_price > 0 ? '판매단가 ' + _rcFmt(d.sale_price) + '원' : '판매단가 없음') + '</div></div>';
    const src = s => s.startsWith('발주') ? 'po' : s === '단가표' ? 'tbl' : s === 'BOM' ? 'bom' : 'none';
    let html = '<table><thead><tr><th></th><th>품번</th><th>품명</th><th>구분</th><th class="num">개당 소요</th><th class="num">총 소요량</th><th>단위</th><th class="num">단가</th><th>단가 출처</th><th class="num">금액</th></tr></thead><tbody>';
    ['원재료', '부재료', '기타'].forEach(g => {
      const rows = d.lines.filter(x => x.grp === g); if (!rows.length) return;
      html += '<tr class="grp"><td colspan="9">' + g + ' · ' + rows.length + '종</td><td class="num">' + won(usum(g) * q) + '</td></tr>';
      rows.forEach(x => {
        const off = _rcOff.has(x.code);
        html += '<tr class="' + (off ? 'off' : '') + '"><td><input type="checkbox" ' + (off ? '' : 'checked') + ' onchange="rcToggle(&quot;' + x.code + '&quot;)"></td>'
          + '<td class="code" onclick="openItemModal(&quot;' + x.code + '&quot;)">' + escapeHtml(x.code) + '</td><td>' + escapeHtml(x.name) + '</td><td style="font-size:11px;color:#64748b">' + escapeHtml(x.cls || '') + '</td>'
          + '<td class="num">' + _rcQty(x.per_unit) + '</td><td class="num"><b>' + (q ? _rcQty(_rcUnitTot(x) * q) : '-') + '</b></td><td>' + escapeHtml(x.unit) + '</td>'
          + '<td class="num">' + (x.price ? _rcFmt(x.price) : '-') + '</td><td><span class="rc-src ' + src(x.price_src) + '">' + escapeHtml(x.price_src) + '</span></td>'
          + '<td class="num"><b>' + (q ? _rcFmt(_rcUnitAmt(x) * q) : '-') + '</b></td></tr>';
      });
    });
    html += '</tbody></table>';
    const T = document.getElementById('rc-table'); T.style.display = 'block'; T.innerHTML = html;
    document.getElementById('rc-copy').disabled = !q;
  }
  function rcCopy() {
    const d = _rcData; if (!d) return;
    const q = _rcQtyNow();
    if (!q) { document.getElementById('rc-msg').textContent = '수량을 입력하세요'; return; }
    const on = d.lines.filter(x => !_rcOff.has(x.code));
    const TAB = String.fromCharCode(9), NL = String.fromCharCode(10);   // 파이썬 문자열 안이라 탭/줄바꿈 이스케이프를 직접 쓰면 깨짐
    const lines = [['품번', '품명', '구분', '개당소요', '총소요량', '단위', '단가', '출처', '금액'].join(TAB)];
    on.forEach(x => lines.push([x.code, x.name, x.grp, x.per_unit, Math.round(_rcUnitTot(x) * q * 100) / 100, x.unit, x.price, x.price_src, Math.round(_rcUnitAmt(x) * q)].join(TAB)));
    lines.push(['합계', d.code + ' × ' + q, '', '', '', '', '', '', Math.round(on.reduce((a, x) => a + _rcUnitAmt(x), 0) * q)].join(TAB));
    navigator.clipboard.writeText(lines.join(NL)).then(() => { const m = document.getElementById('rc-msg'); m.textContent = '복사됨 (엑셀에 붙여넣기)'; setTimeout(() => m.textContent = '', 2000); });
  }

  // ───── 패널 접기 (상태는 브라우저에 기억) ─────
  const COL_DEFAULT = { 'sec-ipsu': true };   // 기본 접힘: 3D 입수 (월 매출 추이는 2026-09-23 매출 섹션으로 통합, 기본 펼침)
  function colState() { try { return JSON.parse(localStorage.getItem('mhCollapsed') || '{}'); } catch (e) { return {}; } }
  function isCollapsed(id) { const s = colState(); return id in s ? !!s[id] : !!COL_DEFAULT[id]; }
  function toggleSection(sec, collapse) {
    const id = sec.id; const c = collapse == null ? !sec.classList.contains('collapsed') : collapse;
    sec.classList.toggle('collapsed', c);
    const b = sec.querySelector('.col-btn'); if (b) b.textContent = c ? '▸ 펼치기' : '▾ 접기';
    try { const s = colState(); s[id] = c; localStorage.setItem('mhCollapsed', JSON.stringify(s)); } catch (e) {}
    if (!c) {   // 펼칠 때 지연 초기화 (3D 뷰어·차트 리사이즈)
      if (id === 'sec-ipsu' && typeof ipsuInitViewers === 'function') { ipsuInitViewers(); setTimeout(() => [_ipsuV1, _ipsuV2, _ipsuV3].forEach(v => v && v.resize && v.resize()), 50); }
      if (id === 'sec-sales' && typeof _sbChart !== 'undefined' && _sbChart) setTimeout(() => _sbChart.resize(), 50);
      window.dispatchEvent(new Event('resize'));
    }
  }
  function initCollapsibles() {
    const map = [['.chart-grid', 'sec-alerts'], ['.spec-strip', 'sec-spec'], ['.return-strip', 'sec-return'], ['.ipsu-strip', 'sec-ipsu'], ['.sales-npd-strip', 'sec-sales'], ['.lens-strip', 'sec-lens'], ['.cal-strip', 'sec-cal']];
    map.forEach(([sel, id]) => { const s = document.querySelector('section' + sel); if (s && !s.id) s.id = id; });
    // 그리드 내부 앵커 (내비용)
    const pr = document.querySelector('.price-span'); if (pr) pr.id = pr.id || 'sec-price';
    const pl = document.querySelector('.plan-panel'); if (pl) pl.id = pl.id || 'sec-plan';
    ['sec-spec', 'sec-return', 'sec-ipsu', 'sec-sales', 'sec-lens'].forEach(id => {
      const sec = document.getElementById(id); if (!sec) return;
      const head = sec.querySelector('.chart-head'); if (!head) return;
      const b = document.createElement('button'); b.className = 'col-btn'; b.type = 'button';
      b.onclick = () => toggleSection(sec);
      // 헤더 오른쪽 요소(spec-count 등)는 로더가 textContent로 덮어쓰므로 그 안에 넣지 말고 래퍼로 감싼다
      const right = head.children[1];
      if (right) {
        const wrap = document.createElement('div'); wrap.style.cssText = 'display:flex;align-items:center;gap:8px';
        head.insertBefore(wrap, right); wrap.appendChild(right); wrap.appendChild(b);
      } else { head.appendChild(b); }
      const c = isCollapsed(id); sec.classList.toggle('collapsed', c); b.textContent = c ? '▸ 펼치기' : '▾ 접기';
    });
    // 캘린더 스트립은 접기 헤더 없음 (2026-09-04 사용자 요청: 공간만 차지) — 항상 펼침
    const cal = document.getElementById('sec-cal');
    if (cal) cal.classList.remove('collapsed');
  }
  initCollapsibles();
  initSecNav();   // 섹션 id 부여(initCollapsibles) 이후에 실행

  // ───── 섹션 점프 내비 (스크롤 스파이) ─────
  function initSecNav() {
    const nav = document.getElementById('sec-nav'); if (!nav) return;
    const links = [...nav.querySelectorAll('a')];
    links.forEach(a => a.addEventListener('click', e => {
      e.preventDefault(); const id = a.dataset.sec; const el = document.getElementById(id); if (!el) return;
      const sec = el.closest('section') || el; if (sec.classList.contains('collapsed')) toggleSection(sec, false);
      if (id === 'kpi-strip') { window.scrollTo({ top: 0, behavior: 'smooth' }); return; }
      scrollToPanel(el, false);
    }));
    const targets = links.map(a => document.getElementById(a.dataset.sec)).filter(Boolean);
    const spy = () => {
      const y = window.scrollY + 120; let cur = targets[0];
      targets.forEach(t => { if (t.getBoundingClientRect().top + window.scrollY <= y) cur = t; });
      links.forEach(a => a.classList.toggle('on', cur && a.dataset.sec === cur.id));
    };
    window.addEventListener('scroll', spy, { passive: true }); spy();
  }

  // ───── 매출 현황 패널 ─────
  let _salesChart = null;
  const SALES_WINDOW = 12;
  let _salesAll = [];      // 전체 월별 [{ym, amount}]
  let _salesDivMap = {};   // {ym: [{div, amount}]}
  let _salesOffset = 0;    // 0=최신, +면 과거로
  let _salesCurMonth = '', _salesAsOf = '';
  function fmtEok(v) {
    if (v >= 1e8) return (v/1e8).toFixed(1) + '억';
    if (v >= 1e4) return Math.round(v/1e4) + '만';
    return String(v);
  }
  async function loadSalesSummary() {
    try {
      const res = await fetch('/api/sales_summary');
      const d = await res.json();
      _salesAll = d.monthly || [];
      _salesDivMap = d.div_map || {};
      _salesCurMonth = d.current_month || ''; _salesAsOf = d.as_of || '';
      _salesOffset = 0;
      renderSalesWindow();
    } catch (e) {
      document.getElementById('sales-div-list').innerHTML = '<div style="color:#ef4444;font-size:11px">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }
  function moveSales(dir) {
    // dir +1 = 과거로, -1 = 최근으로
    const maxOff = Math.max(0, _salesAll.length - SALES_WINDOW);
    _salesOffset = Math.min(maxOff, Math.max(0, _salesOffset + dir));
    renderSalesWindow();
  }
  function renderSalesWindow() {
    const n = _salesAll.length;
    if (!n) return;
    const end = n - _salesOffset;
    const start = Math.max(0, end - SALES_WINDOW);
    const win = _salesAll.slice(start, end);
    // 윈도우 평균
    const avg = win.length ? win.reduce((s,m)=>s+m.amount,0)/win.length/1e8 : 0;
    const labels = win.map(m => m.ym.slice(2).replace('-', '/'));
    const vals = win.map(m => +(m.amount/1e8).toFixed(2));
    // KPI: 윈도우 마지막 월 기준 + 전월 대비
    const last = win[win.length-1];
    const prev = win[win.length-2];
    const kpiEl = document.getElementById('sales-kpi');
    if (last) {
      let momHtml = '';
      if (prev && prev.amount > 0) {
        const mom = (last.amount - prev.amount) / prev.amount * 100;
        const c = mom >= 0 ? '#dc2626' : '#2563eb';
        momHtml = ' <span style="color:' + c + '">' + (mom>=0?'▲':'▼') + Math.abs(mom).toFixed(1) + '%</span>';
      }
      const partial = (last.ym === _salesCurMonth);
      if (partial) momHtml = '';   // 진행 중인 달은 전월 대비가 의미 없음
      kpiEl.innerHTML = last.ym + (partial ? ' <span style="color:#b45309">(~' + escapeHtml(_salesAsOf.slice(5).replace('-', '/')) + ' 진행중)</span>' : '')
        + ' <b style="color:#0369a1">' + fmtEok(last.amount) + '</b>' + momHtml;
    }
    // 구분별 (윈도우 마지막 월)
    const divEl = document.getElementById('sales-div-list');
    const dd = last ? (_salesDivMap[last.ym] || []) : [];
    divEl.innerHTML = dd.map(x =>
      '<div class="sales-div-row"><span class="dv-name">' + escapeHtml(x.div) + '</span>'
      + '<span class="dv-amt">' + fmtEok(x.amount) + '</span></div>'
    ).join('') || '<div style="font-size:11px;color:var(--text-3)">데이터 없음</div>';
    // 버튼 활성/비활성
    const maxOff = Math.max(0, n - SALES_WINDOW);
    document.getElementById('sales-prev').disabled = _salesOffset >= maxOff;
    document.getElementById('sales-next').disabled = _salesOffset <= 0;
    // 클릭 시 상세용 윈도우 월 배열 저장
    const winYms = win.map(m => m.ym);
    // 차트
    const ctx = document.getElementById('salesChart');
    if (_salesChart) _salesChart.destroy();
    _salesChart = new Chart(ctx, {
      data: { labels, datasets: [
        { type: 'bar', label: '월매출(억)', data: vals,
          backgroundColor: (c) => { const a = c.chart.chartArea; if (!a) return '#2563eb';
            const g = c.chart.ctx.createLinearGradient(0, a.top, 0, a.bottom);
            g.addColorStop(0, '#2563eb'); g.addColorStop(1, '#dbeafe'); return g; },
          borderColor: '#1d4ed8', borderWidth: 1, borderRadius: 6, borderSkipped: false, order: 3 },
        { type: 'line', label: '추세', data: vals,
          borderColor: '#1e3a8a', backgroundColor: '#1e3a8a', borderWidth: 2.5,
          borderDash: [6, 4], pointRadius: 3, pointBackgroundColor: '#1e3a8a',
          tension: 0.35, fill: false, order: 1 },
        { type: 'line', label: '윈도우 평균', data: labels.map(() => +avg.toFixed(2)),
          borderColor: '#dc2626', backgroundColor: '#dc2626', borderWidth: 3, borderDash: [6,4],
          pointRadius: 0, fill: false, order: 2 }
      ]},
      options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
        onClick: (e, els) => { if (els.length) { const ym = winYms[els[0].index]; if (ym) openSalesDetail(ym); } },
        onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        plugins: { legend: { display: true, labels: { boxWidth: 12, font: { size: 10 } } },
          tooltip: { callbacks: { label: c => c.dataset.label + ': ' + c.parsed.y + '억',
            footer: () => '클릭하면 TOP 10 상세' } } },
        scales: { y: { beginAtZero: true, ticks: { font: { size: 10 },
          callback: v => v + '억' }, grid: { color: '#eef2f7' } },
          x: { ticks: { font: { size: 10 } }, grid: { display: false } } }
      }
    });
  }

  // ───── 데이터 건강검진 ─────
  // 파일 날짜가 최신이어도 '내용'이 옛 달에 멈추는 조용한 절단이 반복돼(입고·출고 2회),
  // 로드된 데이터의 실제 최신월을 검사해 이상 시 헤더에 경고를 띄운다.
  let _dhData = null;
  const DH_STYLE = {
    error: { bg: '#fef2f2', fg: '#b91c1c', bd: '#fca5a5', ico: '⚠' },
    warn:  { bg: '#fffbeb', fg: '#b45309', bd: '#fcd34d', ico: '⚠' },
    ok:    { bg: '#ecfdf5', fg: '#047857', bd: '#6ee7b7', ico: '✓' },
    unknown: { bg: '#f1f5f9', fg: '#475569', bd: '#cbd5e1', ico: '·' }
  };
  async function loadDataHealth() {
    try {
      const d = await (await fetch('/api/data_health')).json();
      _dhData = d;
      const btn = document.getElementById('data-health-btn');
      if (!btn) return;
      if (d.overall === 'ok') { btn.style.display = 'none'; return; }   // 정상이면 숨김
      const st = DH_STYLE[d.overall] || DH_STYLE.unknown;
      btn.style.display = '';
      btn.style.background = st.bg; btn.style.color = st.fg; btn.style.borderColor = st.bd;
      btn.style.fontWeight = '700';
      btn.textContent = st.ico + ' ' + d.summary;
      btn.title = '클릭하면 데이터 점검 상세 (' + d.checked_at + ' 기준)';
    } catch (e) { /* 점검 실패는 대시보드 동작에 영향 주지 않음 */ }
  }
  function closeDataHealth() { document.getElementById('dh-modal').classList.remove('show'); }
  async function openDataHealth() {
    const modal = document.getElementById('dh-modal');
    modal.classList.add('show');
    const body = document.getElementById('dh-body');
    body.innerHTML = '<div class="loading" style="padding:24px">로딩 중...</div>';
    try {
      const d = _dhData || await (await fetch('/api/data_health')).json();
      _dhData = d;
      const st = DH_STYLE[d.overall] || DH_STYLE.unknown;
      const badge = document.getElementById('dh-badge');
      badge.textContent = d.overall === 'ok' ? '정상' : (d.overall === 'warn' ? '확인 필요' : '이상');
      badge.style.background = st.fg;
      document.getElementById('dh-sub').textContent =
        d.summary + ' · 기준월 ' + d.cur_ym + ' · 점검 ' + d.checked_at;
      body.innerHTML =
        '<div style="font-size:11px;color:var(--text-3);margin-bottom:8px">'
        + '수집한 데이터의 <b>실제 최신 날짜</b>를 검사합니다. 파일 날짜가 최신이어도 내용이 옛 날짜에 멈추면(수집 중단) 여기서 잡힙니다. 주말·연휴 감안 7일까지 정상.</div>'
        + '<div style="border:1px solid var(--border);border-radius:10px;overflow:hidden">'
        + '<div style="display:flex;font-size:10.5px;font-weight:700;color:var(--text-3);background:var(--surface-2);padding:7px 10px">'
        +   '<span style="flex:1">데이터</span><span style="width:88px;text-align:center">최신 데이터</span>'
        +   '<span style="width:62px;text-align:right">행수</span><span style="width:150px;text-align:right">상태</span></div>'
        + (d.items || []).map((it, i) => {
            const s = DH_STYLE[it.status] || DH_STYLE.unknown;
            return '<div style="display:flex;align-items:center;font-size:12px;padding:8px 10px;'
              + (i ? 'border-top:1px solid var(--border);' : '') + '">'
              + '<span style="flex:1;font-weight:700">' + escapeHtml(it.label) + '</span>'
              + '<span style="width:88px;text-align:center;font-variant-numeric:tabular-nums">' + escapeHtml(it.latest_ym || '-') + '</span>'
              + '<span style="width:62px;text-align:right;font-variant-numeric:tabular-nums;color:var(--text-3)">' + fmtInt(it.rows) + '</span>'
              + '<span style="width:150px;text-align:right"><span style="font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:6px;'
              +   'background:' + s.bg + ';color:' + s.fg + ';border:1px solid ' + s.bd + '">'
              +   s.ico + ' ' + escapeHtml(it.msg) + '</span></span>'
              + '</div>';
          }).join('')
        + '</div>'
        + '<a href="/sku_review" target="_blank" style="display:inline-block;margin-top:10px;padding:7px 12px;border:1px solid #6366f1;border-radius:8px;'
        +   'font-size:12px;font-weight:700;color:#4f46e5;text-decoration:none;background:#eef2ff">🧩 판매 SKU 품번 정리 열기</a>'
        + (d.overall === 'error'
            ? '<div style="margin-top:10px;padding:10px 12px;background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;font-size:11.5px;color:#b91c1c">'
              + '<b>조치</b> — 해당 데이터만 타겟 재수집이 필요합니다. 전체 fetch_all은 시간이 오래 걸려 후반 단계가 잘릴 수 있으니, 문제 데이터만 다시 받는 것이 안전합니다.</div>'
            : '');
    } catch (e) {
      body.innerHTML = '<div style="color:#ef4444;padding:16px">점검 실패: ' + escapeHtml(e.message) + '</div>';
    }
  }
  document.getElementById('dh-modal').addEventListener('click', e => {
    if (e.target.id === 'dh-modal') closeDataHealth();
  });
  loadDataHealth();

  // ───── 차트 TOP 10 상세 모달 ─────
  let _phChart = null;
  function closeChartDetail() {
    document.getElementById('chart-detail-modal').classList.remove('show');
    if (_phChart) { _phChart.destroy(); _phChart = null; }
  }
  // 제품 TOP10 행 클릭 → 해당 품번 월별 판매 추이
  async function openProductHistory(code) {
    const p = (_sbProducts || []).find(x => x.code === code) || { code: code, name: '', cls: '' };
    const modal = document.getElementById('chart-detail-modal');
    modal.classList.add('show');
    const badge = document.getElementById('cd-badge');
    badge.textContent = p.cls || '제품';
    badge.style.background = 'linear-gradient(135deg,#3f9e8f,#49aa9c)';
    document.getElementById('cd-title').textContent = code + ' ' + (p.name || '');
    document.getElementById('cd-sub').textContent = '월별 판매 추이 (과거~현재)';
    const body = document.getElementById('cd-body');
    body.innerHTML = '<div class="loading" style="padding:24px">로딩 중...</div>';
    if (_phChart) { _phChart.destroy(); _phChart = null; }
    try {
      const d = await (await fetch('/api/sales_qty_product?code=' + encodeURIComponent(code))).json();
      const m = d.monthly || [];
      if (!m.length) { body.innerHTML = '<div style="padding:20px;color:var(--text-3);font-size:12.5px">판매 데이터가 없습니다.</div>'; return; }
      const hasAmt = !!d.has_amt;
      const peak = m.reduce((a, x) => x.ea > a.ea ? x : a, m[0]);
      const totQty = m.reduce((s, x) => s + x.ea, 0);
      document.getElementById('cd-sub').textContent =
        m.length + '개월 · 누적 ' + fmtInt(totQty) + '개 · 최고 ' + peak.ym + ' ' + fmtInt(peak.ea) + '개';
      body.innerHTML =
        '<div style="height:230px;position:relative;margin:2px 0 12px"><canvas id="ph-chart"></canvas></div>'
        + '<div style="max-height:200px;overflow:auto"><table style="width:100%;border-collapse:collapse;font-size:11.5px">'
        + '<thead><tr style="color:var(--text-3);border-bottom:1.5px solid var(--border-2)">'
        + '<th style="text-align:left;padding:4px 6px;font-weight:700">월</th>'
        + '<th style="text-align:right;padding:4px 6px;font-weight:700">판매수량</th>'
        + (hasAmt ? '<th style="text-align:right;padding:4px 6px;font-weight:700;color:#2f8576">매출액</th>' : '')
        + '</tr></thead><tbody>'
        + m.slice().reverse().map(x =>
            '<tr style="border-bottom:1px solid var(--border)' + (x.is_current ? ';color:var(--text-3)' : '') + '">'
            + '<td style="padding:4px 6px">' + x.ym + (x.is_current ? ' <span style="font-size:9px">(진행중)</span>' : '') + '</td>'
            + '<td style="padding:4px 6px;text-align:right;font-variant-numeric:tabular-nums;font-weight:700">' + fmtInt(x.ea) + '</td>'
            + (hasAmt ? '<td style="padding:4px 6px;text-align:right;font-variant-numeric:tabular-nums;color:#2f8576">' + fmtWonC(x.amt) + '</td>' : '')
            + '</tr>').join('')
        + '</tbody></table></div>';
      const labels = m.map(x => x.ym.slice(2) + (x.is_current ? '*' : ''));
      const qty = m.map(x => x.ea);
      const datasets = [{
        type: 'bar', label: '판매수량(개)', data: qty, yAxisID: 'y',
        backgroundColor: m.map(x => x.is_current ? '#9fd8cc' : '#49aa9c'), borderRadius: 3, order: 2
      }];
      if (hasAmt) {
        datasets.push({
          type: 'line', label: '매출액(억)', data: m.map(x => +(x.amt / 1e8).toFixed(2)),
          yAxisID: 'y1', borderColor: '#dc7a29', backgroundColor: '#dc7a29',
          borderWidth: 2, pointRadius: 2, tension: 0.3, order: 1
        });
      }
      _phChart = new Chart(document.getElementById('ph-chart'), {
        data: { labels: labels, datasets: datasets },
        options: {
          responsive: true, maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: { legend: { display: hasAmt, labels: { font: { size: 10 }, boxWidth: 12 } },
            tooltip: { callbacks: { label: c => c.dataset.type === 'line'
              ? '매출액: ' + c.parsed.y + '억' : '판매수량: ' + fmtInt(c.parsed.y) + '개' } } },
          scales: {
            y: { position: 'left', beginAtZero: true, ticks: { font: { size: 10 }, callback: v => fmtInt(v) }, grid: { color: '#eef2f7' } },
            y1: { position: 'right', display: hasAmt, beginAtZero: true, ticks: { font: { size: 10 }, callback: v => v + '억' }, grid: { display: false } },
            x: { ticks: { font: { size: 9.5 }, maxRotation: 0, autoSkip: true }, grid: { display: false } }
          }
        }
      });
    } catch (e) { body.innerHTML = '<div style="color:#ef4444;padding:16px">오류: ' + escapeHtml(e.message) + '</div>'; }
  }
  // 매출·판매 추이 차트 세그먼트 클릭 → 해당 월+분류 제품별 상세
  async function openClassDetail(ym, cls) {
    const modal = document.getElementById('chart-detail-modal');
    modal.classList.add('show');
    if (_phChart) { _phChart.destroy(); _phChart = null; }
    const color = SB_CLS_COLOR[cls] || '#3f9e8f';
    const badge = document.getElementById('cd-badge');
    badge.textContent = cls;
    badge.style.background = color;
    document.getElementById('cd-title').textContent = ym + ' ' + cls + ' 판매 상세';
    const sub = document.getElementById('cd-sub');
    sub.textContent = '로딩 중...';
    const body = document.getElementById('cd-body');
    body.innerHTML = '<div class="loading" style="padding:24px">로딩 중...</div>';
    try {
      const d = await (await fetch('/api/sales_qty_class?ym=' + encodeURIComponent(ym) + '&cls=' + encodeURIComponent(cls))).json();
      const items = d.items || [];
      if (!items.length) { body.innerHTML = '<div style="padding:20px;color:var(--text-3);font-size:12.5px">해당 분류 판매 데이터가 없습니다.</div>'; sub.textContent = ''; return; }
      const hasAmt = !!d.has_amt;
      sub.textContent = '제품 ' + items.length + '종 · 판매 ' + fmtInt(d.total_ea) + '개' + (hasAmt ? ' · 매출 ' + fmtEok(d.total_amt) : '');
      const denom = hasAmt ? (d.total_amt || 1) : (d.total_ea || 1);
      const maxV = hasAmt ? (items[0].amt || 1) : (items[0].ea || 1);
      body.innerHTML = '<div style="font-size:10px;color:var(--text-3);margin-bottom:4px">제품 클릭 시 월별 추이 →</div>'
        + items.map((it, i) => {
          const val = hasAmt ? it.amt : it.ea;
          const barW = maxV > 0 ? (val / maxV * 100) : 0;
          const pct = denom > 0 ? (val / denom * 100) : 0;
          return '<div class="cd-prow" data-code="' + escapeHtml(it.code) + '" style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);cursor:pointer">'
            + '<span style="width:18px;font-size:11px;font-weight:700;color:' + color + '">' + (i + 1) + '</span>'
            + '<div style="flex:1;min-width:0">'
            +   '<div style="font-size:11.5px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><b style="color:#45589f">' + escapeHtml(it.code) + '</b> ' + escapeHtml(it.name || '') + '</div>'
            +   '<div style="height:4px;background:' + color + '22;border-radius:2px;margin-top:3px;overflow:hidden"><div style="height:100%;width:' + barW + '%;background:' + color + '"></div></div>'
            + '</div>'
            + '<div style="text-align:right;flex-shrink:0">'
            +   '<div style="font-size:12px;font-weight:800;color:' + color + '">' + (hasAmt ? fmtWonC(it.amt) : fmtInt(it.ea) + '개') + '</div>'
            +   '<div style="font-size:9px;color:var(--text-3)">' + (hasAmt ? fmtInt(it.ea) + '개 · ' : '') + pct.toFixed(1) + '%</div>'
            + '</div></div>';
        }).join('');
      if (!body._cdProwBound) {
        body.addEventListener('click', ev => {
          const r = ev.target.closest('.cd-prow');
          if (r && r.dataset.code) openProductHistory(r.dataset.code);
        });
        body._cdProwBound = true;
      }
    } catch (e) { body.innerHTML = '<div style="color:#ef4444;padding:16px">오류: ' + escapeHtml(e.message) + '</div>'; sub.textContent = ''; }
  }
  document.getElementById('chart-detail-modal').addEventListener('click', e => {
    if (e.target.id === 'chart-detail-modal') closeChartDetail();
  });
  function _cdRows(items, total, color) {
    if (!items || !items.length) return '<div style="color:var(--text-3);font-size:12px;padding:8px">데이터 없음</div>';
    const max = items[0].amount || 1;
    return items.map((it, i) => {
      const pct = total > 0 ? (it.amount / total * 100) : 0;
      const barW = (it.amount / max * 100);
      const sub = it.div ? ' <span style="font-size:9.5px;color:var(--text-3)">' + escapeHtml(it.div) + '</span>' : '';
      return '<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">'
        + '<span style="width:18px;font-size:11px;font-weight:700;color:' + color + '">' + (i+1) + '</span>'
        + '<div style="flex:1;min-width:0">'
        +   '<div style="font-size:11.5px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(it.name || '-') + sub + '</div>'
        +   '<div style="height:4px;background:' + color + '22;border-radius:2px;margin-top:3px;overflow:hidden"><div style="height:100%;width:' + barW + '%;background:' + color + '"></div></div>'
        + '</div>'
        + '<div style="text-align:right;flex-shrink:0">'
        +   '<div style="font-size:12px;font-weight:800;color:' + color + '">' + fmtEok(it.amount) + '</div>'
        +   '<div style="font-size:9px;color:var(--text-3)">' + pct.toFixed(1) + '%</div>'
        + '</div></div>';
    }).join('');
  }
  async function openSalesDetail(ym) {
    const modal = document.getElementById('chart-detail-modal');
    modal.classList.add('show');
    document.getElementById('cd-badge').textContent = '매출';
    document.getElementById('cd-badge').style.background = 'linear-gradient(135deg,#0ea5e9,#38bdf8)';
    document.getElementById('cd-title').textContent = ym + ' 매출 TOP 10';
    document.getElementById('cd-sub').textContent = '상품별 매출 (온라인팀 판매자료 · 공급가)';
    const body = document.getElementById('cd-body');
    body.innerHTML = '<div class="loading" style="padding:24px">로딩 중...</div>';
    try {
      const d = await (await fetch('/api/sales_detail?ym=' + encodeURIComponent(ym))).json();
      document.getElementById('cd-sub').textContent = '상품별 매출 (공급가) · 전체 ' + fmtEok(d.total) + ' (' + d.count + '품목)';
      body.innerHTML = _cdRows(d.items, d.total, '#0369a1');
    } catch (e) { body.innerHTML = '<div style="color:#ef4444;padding:16px">오류: ' + escapeHtml(e.message) + '</div>'; }
  }
  async function openOrderReceiptDetail(ym) {
    const modal = document.getElementById('chart-detail-modal');
    modal.classList.add('show');
    document.getElementById('cd-badge').textContent = '발주·입고';
    document.getElementById('cd-badge').style.background = 'linear-gradient(135deg,#f59e0b,#0f766e)';
    document.getElementById('cd-title').textContent = ym + ' 발주·입고 TOP 10';
    document.getElementById('cd-sub').textContent = '거래처별 (아마란스)';
    const body = document.getElementById('cd-body');
    body.innerHTML = '<div class="loading" style="padding:24px">로딩 중...</div>';
    try {
      const d = await (await fetch('/api/order_receipt_detail?ym=' + encodeURIComponent(ym))).json();
      body.innerHTML =
        '<div style="font-size:12px;font-weight:800;color:#b45309;margin:4px 0 6px">📦 발주 · 전체 ' + fmtEok(d.order_total) + '</div>'
        + _cdRows(d.order, d.order_total, '#b45309')
        + '<div style="font-size:12px;font-weight:800;color:#0f766e;margin:16px 0 6px">📥 입고 · 전체 ' + fmtEok(d.receipt_total) + '</div>'
        + _cdRows(d.receipt, d.receipt_total, '#0f766e');
    } catch (e) { body.innerHTML = '<div style="color:#ef4444;padding:16px">오류: ' + escapeHtml(e.message) + '</div>'; }
  }

  // ───── 발주 · 입고 추이 패널 ─────
  let _orChart = null;
  const OR_WINDOW = 12;
  let _orAll = [];      // 전체 [{ym, order_amt, receipt_amt}]
  let _orOffset = 0;
  async function loadOrderReceipt() {
    try {
      const res = await fetch('/api/order_receipt_summary');
      const d = await res.json();
      _orAll = d.monthly || [];
      _orOffset = 0;
      renderOrWindow();
    } catch (e) {
      document.getElementById('or-kpi').innerHTML = '<span style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</span>';
    }
  }
  function moveOr(dir) {
    const maxOff = Math.max(0, _orAll.length - OR_WINDOW);
    _orOffset = Math.min(maxOff, Math.max(0, _orOffset + dir));
    renderOrWindow();
  }
  function renderOrWindow() {
    const n = _orAll.length;
    if (!n) return;
    const end = n - _orOffset;
    const start = Math.max(0, end - OR_WINDOW);
    const win = _orAll.slice(start, end);
    const labels = win.map(m => m.ym.slice(2).replace('-', '/'));
    const orderVals = win.map(m => m.order_amt != null ? +(m.order_amt/1e8).toFixed(2) : null);
    const recvVals  = win.map(m => m.receipt_amt != null ? +(m.receipt_amt/1e8).toFixed(2) : null);
    // KPI: 윈도우 내 값 있는 최신월 (당월은 진행중 표시)
    const lastOrder = [...win].reverse().find(m => m.order_amt != null);
    const lastRecv  = [...win].reverse().find(m => m.receipt_amt != null);
    const live = '<span class="kpi-live">진행중</span>';
    const chip = (color, label, ym, val, isCur) =>
      '<span class="kpi-chip" style="--c:' + color + '">'
      + '<span class="kpi-label">' + label + ' ' + ym + '</span>'
      + '<span class="kpi-val">' + fmtEok(val) + '</span>'
      + (isCur ? live : '') + '</span>';
    const parts = [];
    if (lastOrder) parts.push(chip('#d97706', '발주', lastOrder.ym, lastOrder.order_amt, lastOrder.is_current));
    if (lastRecv)  parts.push(chip('#0f766e', '입고', lastRecv.ym, lastRecv.receipt_amt, lastRecv.is_current));
    document.getElementById('or-kpi').innerHTML = parts.join('');
    // 버튼
    const maxOff = Math.max(0, n - OR_WINDOW);
    document.getElementById('or-prev').disabled = _orOffset >= maxOff;
    document.getElementById('or-next').disabled = _orOffset <= 0;
    // 당월(진행 중) 막대/점은 연하게 구분
    const recvPointColors = win.map(m => m.is_current ? '#f43f5e' : '#0f766e');
    const recvPointRadius = win.map(m => m.is_current ? 5.5 : 3.5);
    // 발주액 막대: 또렷한 앰버 그라데이션 (당월은 진행중이라 연하게)
    const orderBarBg = (c) => {
      const a = c.chart.chartArea; if (!a) return '#f59e0b';
      const cur = win[c.dataIndex] && win[c.dataIndex].is_current;
      const g = c.chart.ctx.createLinearGradient(0, a.top, 0, a.bottom);
      if (cur) { g.addColorStop(0, 'rgba(245,158,11,0.40)'); g.addColorStop(1, 'rgba(250,204,21,0.34)'); }
      else     { g.addColorStop(0, '#f59e0b'); g.addColorStop(1, '#facc15'); }
      return g;
    };
    // 차트
    const ctx = document.getElementById('orChart');
    if (_orChart) _orChart.destroy();
    _orChart = new Chart(ctx, {
      data: {
        labels,
        datasets: [
          { type: 'bar', label: '발주액(억)', data: orderVals, backgroundColor: orderBarBg,
            borderColor: '#d97706', borderWidth: 1, borderRadius: 6, borderSkipped: false, order: 2 },
          { type: 'line', label: '입고액(억)', data: recvVals,
            borderColor: '#0f766e', backgroundColor: '#0f766e', borderWidth: 3,
            pointRadius: recvPointRadius, pointBackgroundColor: recvPointColors,
            pointBorderColor: '#ffffff', pointBorderWidth: 1.5,
            tension: 0.35, spanGaps: false, order: 1 }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
        onClick: (e, els) => { if (els.length) { const m = win[els[0].index]; if (m) openOrderReceiptDetail(m.ym); } },
        onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        plugins: {
          legend: { display: true, labels: { boxWidth: 12, font: { size: 10 } } },
          tooltip: { callbacks: { label: c => c.dataset.label.replace('(억)','') + ': '
            + (c.parsed.y != null ? c.parsed.y + '억' : '데이터 없음'),
            footer: () => '클릭하면 TOP 10 상세' } }
        },
        scales: {
          y: { beginAtZero: true, ticks: { font: { size: 10 }, callback: v => v + '억' },
            grid: { color: '#eef2f7' } },
          x: { ticks: { font: { size: 10 } }, grid: { display: false } }
        }
      }
    });
  }

  // ───── 월 판매기반 자료 패널 ─────
  let _sbChart = null;
  let _sbProducts = [];      // 최근 완결월 제품별 판매수량/매출액 (전체)
  let _sbLatestMonth = '';
  let _sbHasAmt = false;     // 공급가(매출액) 있으면 true
  let _sbMonthly = [];       // 월별 원본 (차트 세그먼트 클릭 시 ym 조회용)
  const SB_CLS_KEY = { '자사': 'jasa', '유상사급': 'sagup', '상품매입': 'saip' };
  const SB_CLS_COLOR = { '자사': '#6276c5', '유상사급': '#dcb058', '상품매입': '#49aa9c' };
  function fmtWonC(v) {   // 매출액 압축 표기: 억/만/원
    v = +v || 0;
    if (v >= 1e8) return (v / 1e8).toFixed(1) + '억';
    if (v >= 1e4) return Math.round(v / 1e4).toLocaleString('ko-KR') + '만';
    return fmtInt(v);
  }

  function renderSbList() {
    const list = document.getElementById('sb-top-list');
    const title = document.getElementById('sb-top-title');
    if (!list) return;
    const q = (document.getElementById('sb-search').value || '').trim().toLowerCase();
    const lhName = document.querySelector('.sb-lh-name');
    if (lhName) lhName.textContent = _sbHasAmt ? '제품 (매출·수량/월)' : '제품 (낱개/월)';
    let rows = _sbProducts;
    if (q) {
      rows = rows.filter(t => (t.code || '').toLowerCase().includes(q) || (t.name || '').toLowerCase().includes(q));
      title.textContent = (_sbLatestMonth || '') + ' 검색 ' + rows.length + '건';
    } else {
      rows = rows.slice(0, 10);
      title.textContent = (_sbLatestMonth || '') + ' 제품 TOP 10';
    }
    if (!rows.length) {
      list.innerHTML = '<div class="alert-empty">' + (q ? '검색 결과가 없습니다' : '데이터 없음') + '</div>';
      return;
    }
    const cell = _sbHasAmt
      ? (amt, qty, v) => '<span class="sb-metric ' + v + '"><b>' + fmtWonC(amt) + '</b><i>' + fmtInt(qty) + '개</i></span>'
      : (amt, qty, v) => '<span class="sb-top-qty ' + v + '">' + fmtInt(qty) + '</span>';
    list.innerHTML = rows.map((t, i) =>
      '<div class="sb-top-row" data-code="' + escapeHtml(t.code) + '" title="클릭 시 판매 추이">'
      + '<span class="sb-top-rank">' + (q ? '·' : (i + 1)) + '</span>'
      + '<span class="sb-cls ' + (SB_CLS_KEY[t.cls] || '') + '">' + t.cls + '</span>'
      + '<span class="sb-top-name" title="' + escapeHtml(t.code + ' ' + t.name) + '">'
      + '<b class="sb-top-code">' + escapeHtml(t.code) + '</b> ' + escapeHtml(t.name || '') + '</span>'
      + cell(t.avg3_amt, t.avg3, 'avg')
      + cell(t.m1_amt, t.m1, 'm1')
      + '</div>'
    ).join('');
    if (!list._phBound) {
      list.addEventListener('click', ev => {
        const row = ev.target.closest('.sb-top-row');
        if (row && row.dataset.code) openProductHistory(row.dataset.code);
      });
      list._phBound = true;
    }
  }

  async function loadSalesBased() {
    try {
      const res = await fetch('/api/sales_qty');
      const d = await res.json();
      const monthly = d.monthly || [];
      const topList = document.getElementById('sb-top-list');
      if (!monthly.length) {
        if (topList) topList.innerHTML = '<div class="alert-empty">판매 데이터가 없습니다</div>';
        document.getElementById('sb-kpi').textContent = '';
        return;
      }
      _sbHasAmt = !!d.has_amt;   // 공급가(매출액) 있으면 매출액 표시
      // KPI: 최근 완결월(당월 제외) + 전월대비
      const complete = monthly.filter(m => !m.is_current);
      const last = complete[complete.length - 1] || monthly[monthly.length - 1];
      const prev = complete[complete.length - 2];
      const kval = m => _sbHasAmt ? m.total_amt : m.total;
      let kpi = last ? (last.ym + (_sbHasAmt ? ' 매출 ' + fmtEok(last.total_amt) : ' 판매 ' + fmtInt(last.total) + '개')) : '';
      if (last && prev && kval(prev)) {
        const g = (kval(last) - kval(prev)) / kval(prev) * 100;
        kpi += '  ' + (g >= 0 ? '▲' : '▼') + Math.abs(g).toFixed(0) + '%';
      }
      document.getElementById('sb-kpi').textContent = kpi;
      _sbMonthly = monthly;
      _sbProducts = d.products || [];
      _sbLatestMonth = d.latest_month || '';
      renderSbList();
      try { const s = await (await fetch('/api/sales_summary')).json(); _sbChData = s.div_map || {}; } catch (e) { _sbChData = {}; }
      drawSbChart();
    } catch (e) {
      const el = document.getElementById('sb-top-list');
      if (el) el.innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }

  // [분류별 / 채널별] 전환 (2026-09-23 매출 현황 패널 통합)
  let _sbMode = 'cls', _sbChData = {};
  const SB_CH_COLORS = ['#2563eb', '#f59e0b', '#10b981', '#ef4444', '#8b5cf6', '#0ea5e9', '#94a3b8'];
  function setSbMode(m, el) {
    _sbMode = m;
    document.querySelectorAll('#sb-mode .vk-chip').forEach(b => b.classList.toggle('on', b === el));
    drawSbChart();
  }
  function drawSbChart() {
    const monthly = _sbMonthly; if (!monthly || !monthly.length) return;
    const chMode = _sbMode === 'ch' && _sbHasAmt;
      // 차트: 분류별 (매출액 억 / 없으면 판매수량) 누적막대 · 채널별 모드는 상위 6채널 + 기타
      const CLS = [['자사', '#6276c5'], ['유상사급', '#dcb058'], ['상품매입', '#49aa9c']];
      const labels = monthly.map(m => m.ym.slice(2).replace('-', '/') + (m.is_current ? '*' : ''));
      let datasets;
      if (chMode) {
        const tot = {};
        Object.values(_sbChData).forEach(arr => arr.forEach(x => { tot[x.div] = (tot[x.div] || 0) + x.amount; }));
        const top = Object.keys(tot).sort((a, b) => tot[b] - tot[a]).slice(0, 6);
        const amtOf = (ym, ch) => ((_sbChData[ym] || []).find(x => x.div === ch) || {}).amount || 0;
        datasets = top.map((ch, i) => ({ label: ch, data: monthly.map(m => +(amtOf(m.ym, ch) / 1e8).toFixed(2)),
          backgroundColor: SB_CH_COLORS[i], stack: 'sb', borderRadius: 3, borderSkipped: false }));
        datasets.push({ label: '기타', backgroundColor: SB_CH_COLORS[6], stack: 'sb', borderRadius: 3, borderSkipped: false,
          data: monthly.map(m => +(((_sbChData[m.ym] || []).filter(x => !top.includes(x.div)).reduce((s, x) => s + x.amount, 0)) / 1e8).toFixed(2)) });
      } else {
        datasets = CLS.map(([k, c]) => ({
          label: k,
          data: monthly.map(m => _sbHasAmt ? +(((m[k + '_amt']) || 0) / 1e8).toFixed(2) : (m[k] || 0)),
          backgroundColor: c, stack: 'sb', borderRadius: 3, borderSkipped: false
        }));
      }
      // 막대 세그먼트에 분류별 금액 + 상단 합계 표기 (매출액 모드일 때)
      const sbSegLabels = {
        id: 'sbSegLabels',
        afterDatasetsDraw(chart) {
          if (!_sbHasAmt) return;
          const cx = chart.ctx;
          cx.save();
          cx.textAlign = 'center';
          cx.textBaseline = 'middle';
          // 분류별 세그먼트 값 (흰 글씨 + 어두운 외곽선)
          cx.font = '700 9.5px -apple-system, "Malgun Gothic", sans-serif';
          chart.data.datasets.forEach((ds, di) => {
            const meta = chart.getDatasetMeta(di);
            meta.data.forEach((bar, i) => {
              const v = +ds.data[i] || 0;
              if (v <= 0) return;
              const yTop = bar.y, yBase = bar.base;
              if (Math.abs(yBase - yTop) < 7) return;   // 극단적으로 얇을 때만 생략
              const t = v.toFixed(1);
              const midY = (yTop + yBase) / 2;
              cx.lineWidth = 2.8; cx.strokeStyle = 'rgba(255,255,255,0.9)';
              cx.strokeText(t, bar.x, midY);
              cx.fillStyle = '#1f2d3d';
              cx.fillText(t, bar.x, midY);
            });
          });
          // 막대 상단 합계(억)
          cx.font = '800 10.5px -apple-system, "Malgun Gothic", sans-serif';
          const meta0 = chart.getDatasetMeta(0);
          meta0.data.forEach((bar, i) => {
            let tot = 0;
            chart.data.datasets.forEach(ds => tot += +ds.data[i] || 0);
            if (tot <= 0) return;
            const topY = chart.scales.y.getPixelForValue(tot) - 7;
            const t = tot.toFixed(1) + '억';
            cx.lineWidth = 3; cx.strokeStyle = 'rgba(255,255,255,0.85)';
            cx.strokeText(t, bar.x, topY);
            cx.fillStyle = '#0f172a';
            cx.fillText(t, bar.x, topY);
          });
          cx.restore();
        }
      };
      const ctx = document.getElementById('salesBaseChart');
      if (_sbChart) _sbChart.destroy();
      _sbChart = new Chart(ctx, {
        type: 'bar',
        data: { labels, datasets },
        plugins: [sbSegLabels],
        options: {
          responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
          onHover: (evt, els) => {
            const pts = _sbChart.getElementsAtEventForMode(evt, 'nearest', { intersect: true }, false);
            if (evt.native && evt.native.target) evt.native.target.style.cursor = pts.length ? 'pointer' : 'default';
          },
          onClick: (evt) => {
            const pts = _sbChart.getElementsAtEventForMode(evt, 'nearest', { intersect: true }, false);
            if (!pts.length) return;
            const p = pts[0];
            const cls = _sbChart.data.datasets[p.datasetIndex].label;
            const mo = _sbMonthly[p.index];
            if (!mo || !mo.ym) return;
            if (chMode) openSalesDetail(mo.ym);      // 채널별 모드: 그 달 상품 TOP 10 (구 매출 현황 상세)
            else openClassDetail(mo.ym, cls);
          },
          plugins: {
            legend: { display: true, labels: { boxWidth: 11, font: { size: 10 } } },
            tooltip: { mode: chMode ? 'index' : 'nearest', callbacks: {
              label: c => c.dataset.label + ': ' + (_sbHasAmt ? c.parsed.y + '억' : fmtInt(c.parsed.y) + '개'),
              footer: items => '합계 ' + (_sbHasAmt
                ? items.reduce((s, i) => s + i.parsed.y, 0).toFixed(2) + '억'
                : fmtInt(items.reduce((s, i) => s + i.parsed.y, 0)) + '개') + '  ·  클릭 시 ' + (chMode ? '상품 TOP 10' : '분류별 상세')
            } }
          },
          scales: {
            x: { stacked: true, ticks: { font: { size: 10 } }, grid: { display: false } },
            y: { stacked: true, beginAtZero: true, grace: '12%', ticks: { font: { size: 10 }, callback: v => _sbHasAmt ? v + '억' : fmtInt(v) }, grid: { color: '#eef2f7' } }
          }
        }
      });
  }

  // ───── 부자재 규격 패널 ─────
  let _specItems = [];
  async function loadSpecList() {
    try {
      const res = await fetch('/api/spec_list');
      const d = await res.json();
      _specItems = d.items || [];
      renderSpecList();
    } catch (e) {
      document.getElementById('spec-list').innerHTML = '<div class="alert-empty" style="color:#ef4444">오류: ' + escapeHtml(e.message) + '</div>';
    }
  }
  // ═══════════════ 입수 테스트 (3D) ═══════════════
  let _ipsuSpecs = [], _ipsuBoxes = [], _ipsuV1 = null, _ipsuV2 = null,
      _ipsuSelA = null, _ipsuSelB = null, _ipsuV3 = null, _ipsuThinMul = 1,
      _ipsuProducts = [], _ipsuOuters = [], _ipsuSelP = null;
  // 적재율/여백 배지 문자열
  function ipsuStats(lay, cont, count) {
    if (!lay || !lay.used) return '';
    const cv = cont.W * cont.H * cont.D;
    const pct = cv > 0 ? Math.round(count * lay.itemVol / cv * 100) : 0;
    const mW = Math.round(cont.W - lay.used.W), mD = Math.round(cont.D - lay.used.D),
          mH = Math.round(cont.H - lay.used.H);
    return ` · 적재율 ${pct}% · 여백 W${mW}·D${mD}·H${mH}`;
  }

  async function loadIpsuSpecs() {
    try {
      const res = await fetch('/api/ipsu_specs');
      const d = await res.json();
      _ipsuSpecs = (d.specs || []).filter(s => s.dim);   // 치수 파싱된 것만
      _ipsuBoxes = d.boxes || [];
      // 외박스(RRP/전용박스) — 3차원 규격 중 단상자(B코드)를 뺀 것
      _ipsuOuters = _ipsuSpecs.filter(s => s.dim.form === '상자' && !/^B/i.test(s.code));
      let pn = 0;
      try {
        const bd = await (await fetch('/api/ipsu_bom')).json();
        _ipsuProducts = bd.products || [];
        pn = _ipsuProducts.length;
      } catch (e) { _ipsuProducts = []; }
      document.getElementById('ipsu-stat').textContent =
        `완제품 ${pn}건 · 규격 ${_ipsuSpecs.length}건(외박스 ${_ipsuOuters.length}) · 박스입수 ${_ipsuBoxes.length}건`;
    } catch (e) {
      document.getElementById('ipsu-stat').textContent = '규격 로드 실패';
    }
    // 접힌 상태면 3D 뷰어 초기화를 펼칠 때로 미룸 (display:none 상태에선 캔버스 크기 0)
    const _sec = document.getElementById('sec-ipsu');
    if (!_sec || !_sec.classList.contains('collapsed')) ipsuInitViewers();
  }

  // ── 검색/자동 채움 ──
  function ipsuSuggest(who) {
    const q = document.getElementById('ipsu-' + who + '-q').value.trim().toLowerCase();
    const box = document.getElementById('ipsu-' + who + '-sug');
    if (!q) { box.style.display = 'none'; return; }
    let list, html;
    if (who === 'p') {
      list = _ipsuProducts.filter(p => (p.code + ' ' + p.name).toLowerCase().includes(q)).slice(0, 30);
      html = list.map(p => {
        const tag = [p.a ? '①' : '', p.b ? '②' : '', p.c ? '③' : ''].join('') || '-';
        const n = p.bomN2 ? ' · 박스 ' + p.bomN2 + '입'
                : (p.ipsu ? ' · 박스 ' + p.ipsu + '입(Monday)' : '');
        return `<div onmousedown="ipsuPickProduct('${p.code}')">` +
          `<span class="sg-c">${escapeHtml(p.code)}</span>${escapeHtml(p.name.slice(0, 30))}` +
          `<span class="sg-s"> · ${tag}${n}</span></div>`;
      }).join('');
    } else if (who === 'o') {
      list = _ipsuOuters.filter(s => (s.code + ' ' + s.name).toLowerCase().includes(q)).slice(0, 30);
      html = list.map(s => `<div onmousedown="ipsuPickOuter('${s.code}')">` +
        `<span class="sg-c">${escapeHtml(s.code)}</span>${escapeHtml(s.name.slice(0, 30))}` +
        `<span class="sg-s"> · ${escapeHtml(s.size)}</span></div>`).join('');
    } else if (who === 'c') {
      list = _ipsuBoxes.filter(b => (b.code + ' ' + b.name).toLowerCase().includes(q)).slice(0, 30);
      html = list.map(b => `<div onmousedown="ipsuPickBox('${b.code}')">` +
        `<span class="sg-c">${escapeHtml(b.code)}</span>${escapeHtml(b.name.slice(0, 34))}` +
        `<span class="sg-s"> · 박스입수 ${b.ipsu}</span></div>`).join('');
    } else {
      list = _ipsuSpecs.filter(s => (s.code + ' ' + s.name).toLowerCase().includes(q)).slice(0, 30);
      html = list.map(s => `<div onmousedown="ipsuPick('${who}','${s.code}')">` +
        `<span class="sg-c">${escapeHtml(s.code)}</span>${escapeHtml(s.name.slice(0, 34))}` +
        `<span class="sg-s"> · ${escapeHtml(s.size)}${s.ipsu ? ' · ' + s.ipsu + '입' : ''}</span></div>`).join('');
    }
    box.innerHTML = html || '<div style="color:#94a3b8">결과 없음</div>';
    box.style.display = 'block';
  }
  function ipsuHideSug(who) { setTimeout(() => {
    const b = document.getElementById('ipsu-' + who + '-sug'); if (b) b.style.display = 'none'; }, 150); }

  function ipsuPick(who, code) {
    const s = _ipsuSpecs.find(x => x.code === code); if (s) ipsuApplySpec(who, s);
  }
  // 규격 1건을 ①/② 블록에 적용. form '상자'(3차원 규격)는 단상자로 변환.
  function ipsuApplySpec(who, s) {
    const d = s.dim; if (!d) return;
    const isBox = (d.form === '상자');
    if (who === 'a') {
      document.getElementById('ipsu-a-form').value = isBox ? '3면실링' : d.form;
      document.getElementById('ipsu-aw').value = d.W;
      document.getElementById('ipsu-ah').value = d.H;
      document.getElementById('ipsu-abase').value = d.base;
      if (isBox) document.getElementById('ipsu-at').value = Math.round(d.D || 30);
      else if (!+document.getElementById('ipsu-at').value) document.getElementById('ipsu-at').value = 30;
      _ipsuSelA = s;
    } else {
      document.getElementById('ipsu-b-form').value = isBox ? '단상자' : d.form;
      if (isBox) {
        // 규격표는 외치수 → 지기 두께(편면 1mm)를 뺀 내치수로 환산
        document.getElementById('ipsu-bbw').value = Math.max(1, Math.round(d.W - 2));
        document.getElementById('ipsu-bbd').value = Math.max(1, Math.round((d.D || d.W) - 2));
        document.getElementById('ipsu-bbh').value = Math.max(1, Math.round(d.H - 2));
      } else {
        document.getElementById('ipsu-bw').value = d.W;
        document.getElementById('ipsu-bh').value = d.H;
        document.getElementById('ipsu-bbase').value = d.base;
        if (!+document.getElementById('ipsu-bt').value) document.getElementById('ipsu-bt').value = 60;
      }
      _ipsuSelB = s;
    }
    // 품명의 'N입' → 입수 자동. 내부용기 '없음'이면 1단계가 없으므로 박스당 입수로.
    if (s.ipsu) {
      const none = document.getElementById('ipsu-b-form').value === '없음';
      document.getElementById(none ? 'ipsu-n2' : 'ipsu-n1').value = s.ipsu;
    }
    document.getElementById('ipsu-' + who + '-sel').innerHTML =
      `<b>${escapeHtml(s.code)}</b> ${escapeHtml(s.size)}${s.tier ? ' · ' + s.tier : ''}${s.ipsu ? ' · ' + s.ipsu + '입' : ''}`;
    document.getElementById('ipsu-' + who + '-q').value = s.code + ' ' + s.name.slice(0, 20);
    ipsuOnForm(who);
  }
  // ③ 외박스 — 규격표의 3차원 외치수를 그대로 적용 (⑤ 팔레트까지 동기화)
  function ipsuPickOuter(code) {
    const s = _ipsuSpecs.find(x => x.code === code); if (!s || !s.dim) return;
    const d = s.dim;
    document.getElementById('ipsu-cw').value = Math.round(d.W);
    document.getElementById('ipsu-cd').value = Math.round(d.D || d.H);
    document.getElementById('ipsu-ch').value = Math.round(d.H);
    document.getElementById('ipsu-o-q').value = s.code + ' ' + s.name.slice(0, 16);
    document.getElementById('ipsu-o-sel').innerHTML =
      `<b>${escapeHtml(s.code)}</b> ${escapeHtml(s.size)}`;
    ipsuSyncOuter();
  }
  // ⓪ 완제품 — BOM 전개 결과로 ①②③ 일괄 적용
  function ipsuPickProduct(code) {
    const p = _ipsuProducts.find(x => x.code === code); if (!p) return;
    const done = [];
    const tag = s => `${escapeHtml(s.code)} ${escapeHtml(s.size)}`;
    if (p.a) { ipsuApplySpec('a', p.a); done.push('① ' + tag(p.a)); }
    if (p.b) { ipsuApplySpec('b', p.b); done.push('② ' + tag(p.b)); }
    else {
      document.getElementById('ipsu-b-form').value = '없음';
      document.getElementById('ipsu-b-sel').textContent = '내부용기 없음';
      document.getElementById('ipsu-b-q').value = '';
      ipsuOnForm('b');
      done.push('② 없음 — 1차파우치 → 외박스 직행');
    }
    if (p.c) { ipsuPickOuter(p.c.code); done.push('③ ' + tag(p.c)); }
    _ipsuSelP = p;
    // 박스당 입수 — ★아마란스 BOM(소요량 역수)이 기준. Monday 박스입수는 폴백/대조용.
    // ※ '개수'만 알고 배열은 모르므로 개수 입력 모드로 전환해야 값이 보인다.
    //   (배열 모드에선 n1/n2 필드가 숨겨져 있어 채워도 화면에 반영되지 않음)
    const n2 = p.bomN2 || p.ipsu;
    if (n2) {
      document.getElementById('ipsu-n2').value = n2;
      document.getElementById('ipsu-mode').value = 'count';
      ipsuOnMode();
      const src = p.bomN2
        ? `<b>아마란스 BOM ${p.bomN2}입</b> (외박스 소요량 역수)`
        : `<span style="color:#b45309">Monday 박스입수 ${p.ipsu}입</span> (아마란스 BOM에 외박스 없음)`;
      done.push('④ ' + src + (p.pallet ? ` · PT ${p.pallet}박스` : '')
                + ' <span style="color:#0369a1">→ 개수 입력 모드</span>');
      // Monday 보드 값과 다르면 그쪽이 갱신 대상
      if (p.bomN2 && p.ipsu && p.ipsu !== p.bomN2)
        done.push(`<span style="color:#b45309">⚠ Monday 박스입수는 ${p.ipsu}입 — ${Math.abs(p.ipsu - p.bomN2)}입 차이(보드 갱신 필요)</span>`);
    } else {
      // 이전 제품의 입수가 남아 잘못된 값으로 보이지 않도록 비움 (빈칸 = 최대치 자동계산)
      document.getElementById('ipsu-n2').value = '';
      done.push('<span style="color:#b45309">④ 아마란스·Monday 모두 입수 데이터 없음 — 최대치 자동계산</span>');
    }
    // 내부용기당 1차 입수 — 여기도 아마란스 BOM 우선, 없으면 품명 'N입'.
    // 둘 다 없으면 이전 제품 값이 남지 않도록 비움(= 최대치 자동계산)
    if (p.b)
      document.getElementById('ipsu-n1').value =
        p.bomN1 || (p.a && p.a.ipsu) || '';
    document.getElementById('ipsu-p-q').value = p.code + ' ' + p.name.slice(0, 22);
    // ②는 위에서 '없음'으로 명시 처리했으므로 누락 경고에서 제외
    const miss = [!p.a && '①', !p.c && '③'].filter(Boolean);
    document.getElementById('ipsu-p-sel').innerHTML =
      done.join('<br>') +
      (miss.length ? `<br><span style="color:#b45309">⚠ BOM에 ${miss.join('')} 부자재 없음 — 직접 입력</span>` : '');
    ipsuCalc();
  }
  function ipsuPickBox(code) {
    const b = _ipsuBoxes.find(x => x.code === code); if (!b) return;
    _ipsuSelP = null;                       // 이쪽 선택이 ⓪보다 우선
    document.getElementById('ipsu-n2').value = b.ipsu;
    if (ipsuIsGrid()) { document.getElementById('ipsu-mode').value = 'count'; ipsuOnMode(); }
    document.getElementById('ipsu-c-q').value = b.code + ' (박스입수 ' + b.ipsu + ')';
    ipsuCalc();
  }

  function ipsuOnForm(who) {
    if (who === 'a') {
      document.getElementById('ipsu-a-basefld').style.display =
        document.getElementById('ipsu-a-form').value === '스탠드' ? 'flex' : 'none';
    } else {
      const f = document.getElementById('ipsu-b-form').value;
      const isBox = (f === '단상자'), none = (f === '없음');
      document.getElementById('ipsu-b-pouch').style.display = (isBox || none) ? 'none' : 'contents';
      document.getElementById('ipsu-b-box').style.display = isBox ? 'contents' : 'none';
      document.getElementById('ipsu-b-basefld').style.display = (f === '스탠드') ? 'flex' : 'none';
      // 내부용기 없음 → 1단계 제거, 1차파우치가 외박스로 직행
      document.getElementById('ipsu-step1fields').style.display = none ? 'none' : 'contents';
      document.getElementById('ipsu-stage1').style.display = none ? 'none' : '';
      document.getElementById('ipsu-stages').classList.toggle('solo', none);
      const bName = isBox ? '단상자' : '2차파우치';
      document.getElementById('ipsu-s1title').textContent = `1단계 · 1차파우치 → ${bName}`;
      document.getElementById('ipsu-s2title').textContent =
        none ? '1차파우치 → 외박스 (직행)' : `2단계 · ${bName} → 외박스`;
      document.getElementById('ipsu-n2label').textContent =
        none ? '박스당 1차파우치 입수' : '2단계 · 박스당 2차 입수';
      document.getElementById('ipsu-pat2label').textContent = none ? '배열' : '2단계 배열';
      if (none) {
        document.getElementById('ipsu-b-sel').textContent = '내부용기 없음';
        // 1단계가 사라지므로, 선택된 1차파우치의 'N입'을 박스당 입수로 이관
        const n2el = document.getElementById('ipsu-n2');
        if (_ipsuSelA && _ipsuSelA.ipsu) n2el.value = _ipsuSelA.ipsu;
        // 비어 있으면 그대로 둠 → ipsuCalc가 최대치 자동 계산
      }
      // 레이아웃(1단/2단) 변경 → 캔버스 폭이 바뀌므로 리사이즈 필요
      if (_ipsuV1) { _ipsuV1.resize(); _ipsuV2.resize(); if (_ipsuV3) _ipsuV3.resize(); }
    }
    ipsuCalc();
  }

  // ── 파우치 지오메트리 ──
  function _sp(v, p) { return Math.sign(v) * Math.pow(Math.abs(v), p); }
  function _sm(t) { t = Math.max(0, Math.min(1, t)); return t * t * (3 - 2 * t); }
  function _lp(a, b, t) { return a + (b - a) * t; }
  // ── 파우치 텍스처: 바탕색 + eatus 로고(누끼) ──
  // /static/eatus.png 가 있으면 사용. 흰 배경은 자동 제거해 로고만 남김.
  let _ipsuTex = null;
  function ipsuPouchTexture() {
    if (_ipsuTex) return _ipsuTex;
    const CW = 1024, CH = 512;
    const cv = document.createElement('canvas'); cv.width = CW; cv.height = CH;
    const cx = cv.getContext('2d');
    cx.fillStyle = '#dc6a10'; cx.fillRect(0, 0, CW, CH);      // 군고구마 앰버 바탕
    _ipsuTex = new THREE.CanvasTexture(cv);
    _ipsuTex.wrapS = THREE.ClampToEdgeWrapping;
    _ipsuTex.wrapT = THREE.ClampToEdgeWrapping;

    const img = new Image();
    img.onload = () => {
      // 1) 오프스크린에 로고를 그리고 흰/밝은 배경 픽셀을 투명 처리(누끼)
      const t = document.createElement('canvas');
      t.width = img.width; t.height = img.height;
      const tc = t.getContext('2d');
      tc.drawImage(img, 0, 0);
      try {
        const d = tc.getImageData(0, 0, t.width, t.height), a = d.data;
        for (let i = 0; i < a.length; i += 4) {
          const r = a[i], g = a[i + 1], b = a[i + 2];
          // 거의 흰색(무채색+밝음) → 배경으로 보고 제거
          if (r > 226 && g > 226 && b > 226 && Math.max(r, g, b) - Math.min(r, g, b) < 22) a[i + 3] = 0;
        }
        tc.putImageData(d, 0, 0);
      } catch (e) { /* 캔버스 오염 시 원본 그대로 사용 */ }
      // 2) 앞면·뒷면 좌우대칭 배치 (앞면 기준 좌측 상단)
      const SIZE = 0.051;   // 로고 폭 (이전 0.085의 60%)
      // 앞면 패널 u 0.125~0.375이나 가장자리는 급격히 휘어짐 → 0.05 이상 밀면 찌그러짐
      // 줄일수록 화면 왼쪽. -0.05부터 옆면 곡면에 말려 폭 급감(0.077→0.055).
      const DX = -0.03;
      const VY = 0.31;      // 위에서부터의 세로 위치
      const lw = CW * SIZE, lh = lw * (img.height / img.width);
      const ly = CH * VY - lh / 2;
      // 텍스처 u가 둘레를 감싸며 화면상 왼쪽으로 진행 → 양면 모두 좌우반전해야 바로 읽힘
      const stamp = (uc) => { cx.save(); cx.translate(CW * uc, 0); cx.scale(-1, 1);
        cx.drawImage(t, -lw / 2, ly, lw, lh); cx.restore(); };
      stamp(0.25 - DX);   // 앞면
      stamp(0.75 + DX);   // 뒷면 (좌우대칭 위치)
      _ipsuTex.needsUpdate = true;
      if (typeof ipsuCalc === 'function') ipsuCalc();
    };
    img.onerror = () => { /* 파일 없으면 바탕색만 유지 */ };
    img.src = '/static/eatus.png';
    return _ipsuTex;
  }

  function ipsuLoft(rings, seg, botY) {
    const v = [], uv = [], idx = [], R = rings.length;
    for (let i = 0; i < R; i++) { const r = rings[i];
      for (let j = 0; j <= seg; j++) { const th = j / seg * Math.PI * 2;
        // 지수 0.88 = 모서리를 둥글게(부드러운 필름). 낮을수록 각진 블록.
        v.push(r.ax * _sp(Math.cos(th), 0.88), r.y, r.bz * _sp(Math.sin(th), 0.88));
        uv.push(j / seg, i / (R - 1));   // u=둘레(0.25가 정면), v=높이
      } }
    const st = seg + 1;
    for (let i = 0; i < R - 1; i++) for (let j = 0; j < seg; j++) {
      const a = i * st + j, b = a + 1, c = a + st, dd = c + 1; idx.push(a, c, b, b, c, dd); }
    // 바닥 중심을 살짝 올리면 스탠드파우치 밑지(거싯) 접힘이 표현됨
    const bc = v.length / 3; v.push(0, botY !== undefined ? botY : rings[0].y, 0); uv.push(0.5, 0);
    for (let j = 0; j < seg; j++) idx.push(bc, j, j + 1);
    const ts = (R - 1) * st, tc = v.length / 3; v.push(0, rings[R - 1].y, 0); uv.push(0.5, 1);
    for (let j = 0; j < seg; j++) idx.push(tc, ts + j + 1, ts + j);
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(v, 3));
    g.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
    g.setIndex(idx); g.computeVertexNormals(); return g;
  }
  // 공기 든 파우치는 적재 시 두께 방향으로 눌림 → Z축만 압축 (폭·높이 유지)
  function ipsuPouchGeomC(form, W, H, base, T, comp, thick) {
    const g = ipsuPouchGeom(form, W, H, base, T, thick);
    const c = Math.max(0, Math.min(0.8, comp || 0));
    if (c > 0) g.scale(1, 1, 1 - c);
    return g;
  }
  // thick > 0 이면 그 값을 실제 채움두께로 사용(실측 입수에서 역산한 값)
  function ipsuPouchGeom(form, W, H, base, T, thick) {
    const rings = [];
    if (form === '스탠드') {
      // 실물 스탠드파우치: 바닥 밑지가 가장 두껍고 위로 갈수록 얇아지는 쐐기형,
      // 정면은 아래가 살짝 넓은 사다리꼴, 상단은 얇은 실링선 + 어깨 라운드
      // 채움 두께 계수 × 실측보정 배율(_ipsuThinMul) — 실측 최대입수 입력 시 자동 역산
      const THIN = 1.215 * _ipsuThinMul, N = 30;
      // 채움두께 T가 입력되면 그 값이 실제 두께(고정). 없으면 밑지 기준 추정.
      const body = thick > 0 ? thick : (T > 0 ? T : Math.max(base, 4) * THIN);
      const bs = Math.min(Math.max(base, 2) * ((thick > 0 || T > 0) ? 1 : THIN), body);
      // ★ 밑지가 펼쳐지면 그 접힘분만큼 실제 세운 높이가 낮아짐 (평면 H − 밑지/2)
      const Hs = Math.max(20, H - Math.max(base, 0) * 0.5);
      for (let i = 0; i <= N; i++) {
        const h = i / N, y = h * Hs;
        // 두께: 바닥 밑지 → 중앙에서 부드럽게 부풀고 → 상단 실링으로 얇게 (플라토 없이 곡면)
        let tf;
        if (h < 0.05) tf = _lp((bs / body) * 0.55, (bs / body), _sm(h / 0.05));   // 바닥 거싯
        else if (h < 0.34) tf = _lp((bs / body), 1.0, _sm((h - 0.05) / 0.29));    // 몸통 중앙까지 부풂(최대)
        else tf = Math.pow(Math.max(0, 1 - (h - 0.34) / 0.66), 0.72);             // 상단으로 얇아짐
        const bz = Math.max(0.6, (body / 2) * tf);
        let ax = W / 2 * _lp(1.0, 0.95, _sm(h));                                  // 아래가 넓은 사다리꼴
        if (h > 0.9) ax *= _lp(1, 0.86, _sm((h - 0.9) / 0.1));                    // 어깨 라운드
        rings.push({ y, ax, bz });
      }
      return ipsuLoft(rings, 24, Hs * 0.05);   // 바닥 중심 살짝 올림 = 밑지 접힘
    }
    const N = 20, Tm = thick > 0 ? thick : Math.max(T, 4) * _ipsuThinMul;
    for (let i = 0; i <= N; i++) { const h = i / N, y = h * H, ax = W / 2;
      const bz = 0.8 + (Tm / 2 - 0.8) * Math.sin(Math.PI * h);
      rings.push({ y, ax, bz: Math.max(0.6, bz) }); }
    return ipsuLoft(rings, 20);
  }
  function ipsuOrient(geom, posture, flip, swap) {
    const g = geom.clone();
    if (posture === '눕힘') g.applyMatrix4(new THREE.Matrix4().makeRotationX(Math.PI / 2));
    if (flip) g.applyMatrix4(new THREE.Matrix4().makeRotationZ(Math.PI));
    if (swap) g.applyMatrix4(new THREE.Matrix4().makeRotationY(Math.PI / 2));   // 평면 90° 회전
    g.computeBoundingBox(); const b = g.boundingBox;
    g.translate(-b.min.x, -b.min.y, -b.min.z); g.computeBoundingBox();
    const s = g.boundingBox.max;
    return { geom: g, size: { x: s.x, y: s.y, z: s.z } };
  }

  // ── 배열(레이아웃) ──
  function ipsuGrid(count, Wc, Dc) {
    let cols = Math.max(1, Math.round(Math.sqrt(count * Wc / Math.max(1, Dc))));
    let rows = Math.ceil(count / cols);
    while (cols * rows < count) cols++;
    while (cols > 1 && (cols - 1) * rows >= count) cols--;
    return { cols, rows };
  }
  // ── 혼합 방향 배치 계획 (일부를 90° 돌려 남는 띠 공간까지 채움) ──
  function ipsuColsIn(width, iw, nest) {
    if (iw <= 0 || iw > width) return 0;
    return Math.floor((width - iw) / (iw * nest)) + 1;
  }
  // W×D 바닥에 w×h 아이템을 정방향/90°회전 혼합으로 최대 배치
  function ipsuFitPlan(W, D, w, h, nest) {
    const mk = (x, z, bw, bd, rot) => {
      const iw = rot ? h : w, id = rot ? w : h;
      const cols = ipsuColsIn(bw, iw, nest);
      const rows = id > 0 ? Math.floor(bd / id) : 0;
      return { x, z, cols, rows, rot, iw, id, pitchX: iw * nest, pitchZ: id };
    };
    let best = { count: 0, blocks: [] };
    const take = (blocks) => {
      const bs = blocks.filter(b => b.cols > 0 && b.rows > 0);
      const c = bs.reduce((s, b) => s + b.cols * b.rows, 0);
      if (c > best.count) best = { count: c, blocks: bs };
    };
    take([mk(0, 0, W, D, false)]);          // 전부 정방향
    take([mk(0, 0, W, D, true)]);           // 전부 90° 회전
    for (const rotA of [false, true]) {     // 가로로 잘라 두 블록
      const a0 = mk(0, 0, W, D, rotA);
      for (let n = 1; n <= a0.cols; n++) {
        const span = (n - 1) * a0.pitchX + a0.iw, rest = W - span;
        if (rest <= 0) continue;
        take([{ ...a0, cols: n }, mk(span, 0, rest, D, !rotA)]);
      }
    }
    for (const rotA of [false, true]) {     // 세로로 잘라 두 블록
      const a0 = mk(0, 0, W, D, rotA);
      for (let n = 1; n <= a0.rows; n++) {
        const span = n * a0.id, rest = D - span;
        if (rest <= 0) continue;
        take([{ ...a0, rows: n }, mk(0, span, W, rest, !rotA)]);
      }
    }
    return best;
  }
  // 파우치 배치 계획 (바닥 계획 + 층수)
  function ipsuPouchPlan(cont, form, W, H, base, T, pattern, comp, thick) {
    const posture = pattern === 'stack' ? '눕힘' : '세움';
    const nest = pattern === 'row-alt' ? 0.68 : 1.0;
    const s0 = ipsuOrient(ipsuPouchGeomC(form, W, H, base, T, comp, thick), posture, false, false).size;
    const layers = s0.y > 0 ? Math.floor(cont.H / s0.y) : 0;
    const plan = ipsuFitPlan(cont.W, cont.D, s0.x, s0.z, nest);
    return { posture, nest, s0, layers, plan, perLayer: plan.count, cap: plan.count * layers };
  }

  // ★ 실제 치수(1:1)로 배치 — 크기 보정 없음. 가로·세로·높이 실제 수용량대로 채움.
  function ipsuLayout(count, cont, form, W, H, base, T, pattern, comp, thick) {
    count = Math.max(1, Math.round(count));
    const P = ipsuPouchPlan(cont, form, W, H, base, T, pattern, comp, thick);
    const mk = (flip, swap) => ipsuOrient(ipsuPouchGeomC(form, W, H, base, T, comp, thick), P.posture, flip, swap).geom;
    const geoms = { n: mk(false, false), f: mk(true, false), s: mk(false, true), sf: mk(true, true) };

    const blocks = P.plan.blocks.length ? P.plan.blocks
      : [{ x: 0, z: 0, cols: 1, rows: 1, rot: false, pitchX: P.s0.x, pitchZ: P.s0.z }];
    const per = Math.max(1, P.perLayer);
    const needLayers = Math.max(P.layers, Math.ceil(count / per));   // 초과분은 위로 쌓아 표시
    const items = [];
    let k = 0;
    for (let L = 0; L < needLayers && k < count; L++) {
      for (const b of blocks) {
        for (let r = 0; r < b.rows && k < count; r++) {
          for (let c = 0; c < b.cols && k < count; c++) {
            const flip = pattern === 'row-alt' && (c % 2 === 1);
            items.push({ x: b.x + c * b.pitchX, z: b.z + r * b.pitchZ, y: L * P.s0.y, sc: 1,
                         gk: (b.rot ? 's' : 'n') + (flip ? 'f' : '') });
            k++;
          }
        }
      }
    }
    // 점유 범위(여백 계산용) + 아이템 부피
    let ux = 0, uz = 0;
    for (const b of blocks) {
      const iw = b.rot ? P.s0.z : P.s0.x, id = b.rot ? P.s0.x : P.s0.z;
      ux = Math.max(ux, b.x + (b.cols - 1) * b.pitchX + iw);
      uz = Math.max(uz, b.z + (b.rows - 1) * b.pitchZ + id);
    }
    const usedLayers = Math.min(needLayers, Math.ceil(count / per));
    const kind = pattern === 'stack' ? '눕혀서' : '세워서';
    const nestLab = pattern === 'row-alt' ? ' · 교대 뒤집기' : '';
    const mix = blocks.length > 1 ? ` · 혼합방향(${blocks.map(b => b.cols + '×' + b.rows + (b.rot ? '↻' : '')).join('+')})` : '';
    const shownL = count > P.cap ? usedLayers : P.layers;   // 초과 시 실제 쌓인 층수
    const arr = blocks.length > 1 ? `${P.perLayer}개/층 × ${shownL}층`
                                  : `${blocks[0].cols}×${blocks[0].rows}×${shownL}층`;
    const over = count > P.cap
      ? ` · ⚠용량초과(박스 수용 ${P.cap}개 = ${P.perLayer}개/층 × ${P.layers}층)` : '';
    const dims = `단품 ${P.s0.x.toFixed(0)}×${P.s0.z.toFixed(0)}×${P.s0.y.toFixed(0)}mm`;
    return { items, geoms, geom: geoms.n, geomFlip: geoms.f, cap: P.cap,
      used: { W: ux, D: uz, H: usedLayers * P.s0.y },
      itemVol: P.s0.x * P.s0.y * P.s0.z,
      label: `${kind} ${arr}${mix}${nestLab}${over} · ${dims}` };
  }
  // ── 최대 입수 계산 (입수 칸 비우면 자동 적용) ──
  function ipsuMaxFit(cont, form, W, H, base, T, pattern, comp, thick) {
    return ipsuPouchPlan(cont, form, W, H, base, T, pattern, comp, thick).cap;   // 0 = 안 들어감
  }
  // 실측 입수 N이 정확히 담기는 최대 두께를 역산 (두꺼울수록 적게 들어감)
  function ipsuSolveThick(cont, form, W, H, base, T, pattern, comp, N) {
    if (!(N > 0)) return 0;
    const def = Math.max(T, base, 4) * 1.215 * _ipsuThinMul;   // 기본(봉투 용량 기준) 두께
    if (ipsuMaxFit(cont, form, W, H, base, T, pattern, comp, def) >= N) return def;  // 기본으로도 충분
    let lo = 0.5, hi = def;                                     // 얇게(많이) ↔ 두껍게(적게)
    if (ipsuMaxFit(cont, form, W, H, base, T, pattern, comp, lo) < N) return 0;      // 아무리 얇아도 불가
    for (let i = 0; i < 36; i++) { const mid = (lo + hi) / 2;
      if (ipsuMaxFit(cont, form, W, H, base, T, pattern, comp, mid) >= N) lo = mid; else hi = mid; }
    return lo;
  }
  // ★ 배열 직접 입력 모드: 열×줄×층이 주어지면 그 칸 크기에 맞춰 두께를 산출해 배치
  //   → 현장 실제 배열을 그대로 옮기므로 캘리브레이션 불필요
  function ipsuGridLayout(cont, form, W, H, base, T, pattern, comp, cols, rows, layers) {
    cols = Math.max(1, Math.round(cols)); rows = Math.max(1, Math.round(rows));
    layers = Math.max(1, Math.round(layers));
    const posture = pattern === 'stack' ? '눕힘' : '세움';
    // 입력 순서와 무관하게, 파우치가 실제로 들어가는 방향을 자동 선택
    let B = null;
    for (const [cw, rw] of [[cols, rows], [rows, cols]]) {
      const cellW = cont.W / cw, cellD = cont.D / rw, cellH = cont.H / layers;
      const t = Math.max(2, (posture === '눕힘' ? cellH : cellD) * (1 - comp));
      const on = ipsuOrient(ipsuPouchGeomC(form, W, H, base, T, comp, t), posture, false, false);
      const s = on.size;
      let viol = 0;
      if (s.x > cellW + 0.5) viol++;
      if (posture === '눕힘' ? (s.z > cellD + 0.5) : (s.y > cellH + 0.5)) viol++;
      const score = viol * 1e6 + Math.max(0, s.x - cellW);
      if (!B || score < B.score) B = { cols: cw, rows: rw, cellW, cellD, cellH, t, on, s, score };
    }
    const of = ipsuOrient(ipsuPouchGeomC(form, W, H, base, T, comp, B.t), posture, true, false);
    const { cols: cw, rows: rw, cellW, cellD, cellH, t, s } = B;
    const items = [];
    for (let l = 0; l < layers; l++) for (let r = 0; r < rw; r++) for (let c = 0; c < cw; c++) {
      items.push({
        x: c * cellW + Math.max(0, (cellW - s.x) / 2),
        z: r * cellD + Math.max(0, (cellD - s.z) / 2),
        y: l * cellH + Math.max(0, (cellH - s.y) / 2),
        sc: 1, gk: (pattern === 'row-alt' && c % 2 === 1) ? 'f' : 'n' });
    }
    const n = cw * rw * layers;
    // 파우치 실치수가 칸을 넘으면 경고 (배열이 물리적으로 불가능)
    const warn = [];
    if (s.x > cellW + 0.5) warn.push(`폭 ${s.x.toFixed(0)}>칸 ${cellW.toFixed(0)}`);
    if (posture === '눕힘' ? (s.z > cellD + 0.5) : (s.y > cellH + 0.5))
      warn.push(`높이 ${(posture === '눕힘' ? s.z : s.y).toFixed(0)}>칸 ${(posture === '눕힘' ? cellD : cellH).toFixed(0)}`);
    const kind = pattern === 'stack' ? '눕혀서' : '세워서';
    const nestLab = pattern === 'row-alt' ? ' · 교대 뒤집기' : '';
    return { items, geoms: { n: B.on.geom, f: of.geom }, geom: B.on.geom, geomFlip: of.geom,
      cap: n, count: n, derivedT: t,
      used: { W: cw * cellW, D: rw * cellD, H: layers * cellH },
      itemVol: s.x * s.y * s.z,
      label: `${kind} ${cw}열×${rw}줄×${layers}층 = ${n}개${nestLab}`
        + ` · 단품 ${s.x.toFixed(0)}×${s.z.toFixed(0)}×${s.y.toFixed(0)}mm`
        + ` · 산출두께 ${t.toFixed(1)}mm`
        + (warn.length ? ` · ⚠${warn.join(', ')}` : '') };
  }

  // 자세만 결정 (두께는 고정) — 선택 배열로 한 개도 못 담으면 반대 자세로
  function ipsuPickPattern(cont, form, W, H, base, T, pattern, comp) {
    if (ipsuMaxFit(cont, form, W, H, base, T, pattern, comp) > 0) return { pattern, note: '' };
    const alt = pattern === 'stack' ? 'row' : 'stack';
    if (ipsuMaxFit(cont, form, W, H, base, T, alt, comp) > 0) return { pattern: alt,
      note: alt === 'stack' ? ' · 세워선 높이 초과 → 눕혀서' : ' · 눕혀선 불가 → 세워서' };
    return { pattern, note: ' · 이 박스엔 안 들어감' };
  }
  // 선택 배열로 안 들어가면 반대 자세로 자동 대체
  function ipsuAutoFit(cont, form, W, H, base, T, pattern, comp, thick) {
    const n = ipsuMaxFit(cont, form, W, H, base, T, pattern, comp, thick);
    if (n > 0) return { n, pattern, note: '' };
    const alt = pattern === 'stack' ? 'row' : 'stack';
    const n2 = ipsuMaxFit(cont, form, W, H, base, T, alt, comp, thick);
    if (n2 > 0) return { n: n2, pattern: alt,
      note: pattern === 'stack' ? ' · 눕혀선 안 들어가 세워서 적용' : ' · 세워선 높이 초과 → 눕혀서 적용' };
    return { n: 0, pattern, note: ' · 들어가지 않음' };
  }
  // 단상자 배치 계획: 수직축 3가지 × 바닥 혼합방향
  function ipsuBoxPlan(cont, W, D, H) {
    const dims = [W, D, H];
    let best = null;
    for (let v = 0; v < 3; v++) {
      const vert = dims[v], f = dims.filter((_, i) => i !== v);
      if (vert <= 0) continue;
      const layers = Math.floor(cont.H / vert);
      if (layers <= 0) continue;
      const plan = ipsuFitPlan(cont.W, cont.D, f[0], f[1], 1.0);
      const cap = plan.count * layers;
      if (!best || cap > best.cap) best = { vert, fw: f[0], fh: f[1], layers, plan, cap, perLayer: plan.count };
    }
    return best || { vert: H, fw: W, fh: D, layers: 0, plan: { count: 0, blocks: [] }, cap: 0, perLayer: 0 };
  }
  function ipsuMaxFitBox(cont, W, D, H) { return ipsuBoxPlan(cont, W, D, H).cap; }

  // ★ 단상자도 실제 치수(1:1) + 혼합방향 배치
  function ipsuLayoutBox(count, cont, W, D, H) {
    count = Math.max(1, Math.round(count));
    const P = ipsuBoxPlan(cont, W, D, H);
    const gap = 1.2;   // 인접 상자 구분용 시각 간격
    const box = (bw, bd) => { const g = new THREE.BoxGeometry(bw - gap, P.vert - gap, bd - gap);
      g.translate(bw / 2, P.vert / 2, bd / 2); return g; };
    const geoms = { n: box(P.fw, P.fh), s: box(P.fh, P.fw) };
    const blocks = P.plan.blocks.length ? P.plan.blocks
      : [{ x: 0, z: 0, cols: 1, rows: 1, rot: false, pitchX: P.fw, pitchZ: P.fh }];
    const per = Math.max(1, P.perLayer);
    const needLayers = Math.max(P.layers, Math.ceil(count / per));
    const items = []; let k = 0;
    for (let L = 0; L < needLayers && k < count; L++) {
      for (const b of blocks) {
        for (let r = 0; r < b.rows && k < count; r++) {
          for (let c = 0; c < b.cols && k < count; c++) {
            items.push({ x: b.x + c * b.pitchX, z: b.z + r * b.pitchZ, y: L * P.vert, sc: 1,
                         gk: b.rot ? 's' : 'n' });
            k++;
          }
        }
      }
    }
    let ux = 0, uz = 0;
    for (const b of blocks) {
      const iw = b.rot ? P.fh : P.fw, id = b.rot ? P.fw : P.fh;
      ux = Math.max(ux, b.x + (b.cols - 1) * b.pitchX + iw);
      uz = Math.max(uz, b.z + (b.rows - 1) * b.pitchZ + id);
    }
    const usedLayers = Math.min(needLayers, Math.ceil(count / per));
    const mix = blocks.length > 1 ? ` · 혼합방향(${blocks.map(b => b.cols + '×' + b.rows + (b.rot ? '↻' : '')).join('+')})` : '';
    const arr = blocks.length > 1 ? `${P.perLayer}개/층 × ${P.layers}층`
                                  : `${blocks[0].cols}×${blocks[0].rows}×${P.layers}층`;
    const over = count > P.cap ? ` · ⚠용량초과(최대 ${P.cap})` : '';
    return { items, geoms, geom: geoms.n, geomFlip: geoms.n, cap: P.cap, isBox: true,
      used: { W: ux, D: uz, H: usedLayers * P.vert },
      itemVol: P.fw * P.vert * P.fh,
      label: `${arr}${mix}${over}` };
  }

  // ── 3D 뷰어 ──
  // 실물 플라스틱 팔레트 — 통짜 박스가 아니라 윗판 격자(구멍 숭숭) + 3×3 다리.
  // 좌표계: 컨테이너와 동일(x 0~W, z 0~D), 팔레트는 y 0 ~ -deckH 를 차지.
  function ipsuPalletMesh(W, D, deckH) {
    const g = new THREE.Group();
    const matTop = new THREE.MeshStandardMaterial({ color: 0xa9b45e, roughness: 0.52, metalness: 0.0 });
    const matLeg = new THREE.MeshStandardMaterial({ color: 0x8a9448, roughness: 0.58, metalness: 0.0 });
    const add = (w, h, d, x, y, z, mat) => {
      const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat || matTop);
      m.position.set(x, y, z); g.add(m);
    };
    const topH = Math.min(35, deckH * 0.30);   // 윗판 두께
    const legH = deckH - topH;                 // 다리 높이 (지게차 포크 공간)
    const topY = -topH / 2;
    const bw = Math.min(75, W * 0.08);         // 테두리 폭
    // 윗판 테두리 4변
    add(W, topH, bw, W / 2, topY, bw / 2);
    add(W, topH, bw, W / 2, topY, D - bw / 2);
    add(bw, topH, D - 2 * bw, bw / 2, topY, D / 2);
    add(bw, topH, D - 2 * bw, W - bw / 2, topY, D / 2);
    // 내부 격자 슬랫 — X·Z 교차로 사각 구멍이 숭숭 뚫린 상판
    const n = 4, sw = Math.min(45, W * 0.045);
    for (let i = 1; i <= n; i++) {
      const t = i / (n + 1);
      add(W - 2 * bw, topH, sw, W / 2, topY, bw + t * (D - 2 * bw));   // 가로 슬랫
      add(sw, topH, D - 2 * bw, bw + t * (W - 2 * bw), topY, D / 2);   // 세로 슬랫
    }
    // 3×3 다리 (모서리·변 중앙·중심) — 사이가 뚫려 지게차 진입구가 보임
    const ls = Math.min(170, W * 0.155), legY = -topH - legH / 2;
    [ls / 2, W / 2, W - ls / 2].forEach(x =>
      [ls / 2, D / 2, D - ls / 2].forEach(z => add(ls, legH, ls, x, legY, z, matLeg)));
    return g;
  }

  function ipsuMakeViewer(el) {
    const scene = new THREE.Scene();
    const cam = new THREE.PerspectiveCamera(45, el.clientWidth / el.clientHeight, 1, 200000);
    // preserveDrawingBuffer: 온디맨드 렌더라 버퍼를 유지해야 캡처/이미지 저장 가능
    const rd = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    rd.setPixelRatio(window.devicePixelRatio);
    rd.setSize(el.clientWidth, el.clientHeight);
    rd.outputEncoding = THREE.sRGBEncoding;
    rd.toneMapping = THREE.ACESFilmicToneMapping;
    rd.toneMappingExposure = 0.92;
    el.insertBefore(rd.domElement, el.firstChild);
    rd.setClearColor(0x17171d);   // 딥 차콜 스튜디오 배경(비네트는 CSS로)
    // 3점 조명 + 반구광 (무광 필름 질감)
    scene.add(new THREE.HemisphereLight(0xf3ede2, 0x24242c, 0.30));
    scene.add(new THREE.AmbientLight(0xffffff, 0.08));
    const l1 = new THREE.DirectionalLight(0xfff2dc, 0.58); l1.position.set(1.1, 2.0, 1.4); scene.add(l1);   // key(웜)
    const l2 = new THREE.DirectionalLight(0xc8dcff, 0.18); l2.position.set(-1.4, 0.6, -0.9); scene.add(l2); // fill(쿨)
    const l3 = new THREE.DirectionalLight(0xffffff, 0.22); l3.position.set(-0.6, 1.1, -1.9); scene.add(l3); // rim
    const root = new THREE.Group(); scene.add(root);
    let yaw = 0.7, pitch = 0.5, dist = 800, target = new THREE.Vector3();
    function draw() { rd.render(scene, cam); }
    function upd() { cam.position.set(
      target.x + dist * Math.cos(pitch) * Math.sin(yaw), target.y + dist * Math.sin(pitch),
      target.z + dist * Math.cos(pitch) * Math.cos(yaw)); cam.lookAt(target); draw(); }
    let drag = false, lx = 0, ly = 0;
    el.addEventListener('mousedown', e => { drag = true; lx = e.clientX; ly = e.clientY; });
    window.addEventListener('mouseup', () => drag = false);
    window.addEventListener('mousemove', e => { if (!drag) return;
      yaw -= (e.clientX - lx) * 0.008; pitch += (e.clientY - ly) * 0.008;
      pitch = Math.max(-1.4, Math.min(1.4, pitch)); lx = e.clientX; ly = e.clientY; upd(); });
    el.addEventListener('wheel', e => { e.preventDefault();
      dist *= (1 + Math.sign(e.deltaY) * 0.12); dist = Math.max(40, dist); upd(); }, { passive: false });
    (function loop() { requestAnimationFrame(loop); rd.render(scene, cam); })();
    return {
      render(cont, lay, shell, deck) {
        while (root.children.length) { const c = root.children[0]; root.remove(c); }
        if (deck > 0) root.add(ipsuPalletMesh(cont.W, cont.D, deck));   // 실물형 팔레트(격자+다리)
        if (shell) {
          // 2차파우치 셸 — 프로스트 글래스 느낌
          root.add(new THREE.Mesh(shell, new THREE.MeshStandardMaterial({
            color: 0xe9e4d9, roughness: 0.62, metalness: 0.10,
            transparent: true, opacity: 0.10, side: THREE.DoubleSide, depthWrite: false })));
          root.add(new THREE.LineSegments(new THREE.EdgesGeometry(shell),
            new THREE.LineBasicMaterial({ color: 0xbcae95, transparent: true, opacity: 0.34 })));
        } else {
          const e = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(cont.W, cont.H, cont.D)),
            new THREE.LineBasicMaterial({ color: 0x7c7a86, transparent: true, opacity: 0.75 }));
          e.position.set(cont.W / 2, cont.H / 2, cont.D / 2); root.add(e);
        }
        // 박스: 무광 크래프트 골판지(현행 유지) / 파우치: 군고구마 속살 앰버
        // 박스: 크래프트 골판지 / 파우치: 군고구마 앰버 + eatus 로고 텍스처
        const tex = lay.isBox ? null : ipsuPouchTexture();
        const m1 = lay.isBox
          ? new THREE.MeshStandardMaterial({ color: 0xbfa877, roughness: 0.82, metalness: 0.03, side: THREE.DoubleSide })
          : new THREE.MeshStandardMaterial({ map: tex, color: 0xffffff, roughness: 0.72, metalness: 0.04, side: THREE.DoubleSide });
        const m2 = lay.isBox
          ? new THREE.MeshStandardMaterial({ color: 0x9c855c, roughness: 0.85, metalness: 0.03, side: THREE.DoubleSide })
          : new THREE.MeshStandardMaterial({ map: tex, color: 0xbb8a6e, roughness: 0.76, metalness: 0.04, side: THREE.DoubleSide });
        for (const it of lay.items) {
          const gk = it.gk || (it.flip ? 'f' : 'n');
          const gm = (lay.geoms && lay.geoms[gk]) || (it.flip ? lay.geomFlip : lay.geom);
          const mesh = new THREE.Mesh(gm, gk.indexOf('f') >= 0 ? m2 : m1);
          mesh.position.set(it.x, it.y, it.z); root.add(mesh);
        }
        target.set(cont.W / 2, cont.H / 2, cont.D / 2);
        dist = Math.max(cont.W, cont.H, cont.D) * 1.9; upd();
      },
      resize() { rd.setSize(el.clientWidth, el.clientHeight);
        cam.aspect = el.clientWidth / el.clientHeight; cam.updateProjectionMatrix(); draw(); }
    };
  }
  function ipsuInitViewers() {
    if (_ipsuV1 || typeof THREE === 'undefined') return;
    _ipsuV1 = ipsuMakeViewer(document.getElementById('ipsu-cv1'));
    _ipsuV2 = ipsuMakeViewer(document.getElementById('ipsu-cv2'));
    _ipsuV3 = ipsuMakeViewer(document.getElementById('ipsu-cv3'));
    // 클릭(드래그 아님) → 확대 보기. 6px 이상 움직이면 회전 드래그로 간주.
    [1, 2, 3].forEach(n => {
      const el = document.getElementById('ipsu-cv' + n);
      let sx = 0, sy = 0;
      el.addEventListener('mousedown', e => { sx = e.clientX; sy = e.clientY; });
      el.addEventListener('mouseup', e => {
        if (Math.abs(e.clientX - sx) < 6 && Math.abs(e.clientY - sy) < 6) ipsuZoomOpen(n);
      });
    });
    ipsuOnForm('a'); ipsuOnForm('b'); ipsuOnMode();
  }

  // ── 3D 확대 보기: 캔버스 컨테이너를 모달로 통째로 이동 → 닫으면 원위치 ──
  let _ipsuZoomN = 0, _ipsuZoomHome = null;
  function ipsuZoomOpen(n) {
    if (_ipsuZoomN) return;
    const cv = document.getElementById('ipsu-cv' + n);
    const viewer = [null, _ipsuV1, _ipsuV2, _ipsuV3][n];
    if (!cv || !viewer) return;
    _ipsuZoomN = n;
    _ipsuZoomHome = { parent: cv.parentNode, next: cv.nextSibling };
    const titles = [null,
      document.getElementById('ipsu-s1title').textContent,
      document.getElementById('ipsu-s2title').textContent,
      '🚛 팔레트 적재 (외박스)'];
    document.getElementById('ipsu-zoom-title').textContent = titles[n];
    const badge = document.getElementById(['', 'ipsu-s1badge', 'ipsu-s2badge', 'ipsu-s3badge'][n]);
    document.getElementById('ipsu-zoom-sub').textContent = badge ? badge.textContent : '';
    document.getElementById('ipsu-zoom-holder').appendChild(cv);
    document.getElementById('ipsu-zoom').classList.add('show');
    viewer.resize();
  }
  function ipsuZoomClose() {
    if (!_ipsuZoomN) return;
    const n = _ipsuZoomN;
    const cv = document.getElementById('ipsu-cv' + n);
    document.getElementById('ipsu-zoom').classList.remove('show');
    if (_ipsuZoomHome) _ipsuZoomHome.parent.insertBefore(cv, _ipsuZoomHome.next);
    _ipsuZoomN = 0; _ipsuZoomHome = null;
    const viewer = [null, _ipsuV1, _ipsuV2, _ipsuV3][n];
    if (viewer) viewer.resize();
  }
  document.getElementById('ipsu-zoom').addEventListener('mousedown', e => {
    if (e.target.id === 'ipsu-zoom') ipsuZoomClose();   // 배경 클릭 닫기
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') ipsuZoomClose(); });
  // ── 실측 캘리브레이션: 실측 입수를 맞추는 눌림(압축)률 역산 ──
  async function ipsuCalibrate() {
    const el = document.getElementById('ipsu-reco');
    el.style.display = 'block';
    el.textContent = '실측 데이터 대조 중…';
    let pairs = [];
    try {
      const r = await fetch('/api/ipsu_calib');
      pairs = (await r.json()).pairs || [];
    } catch (e) { el.textContent = '캘리브레이션 데이터 로드 실패'; return; }

    const C = ipsuInnerC();   // ③은 외치수 → 내부공간으로 환산
    const pat = document.getElementById('ipsu-pat2').value;
    const rows = [];
    for (const p of pairs) {
      const d = p.dim;
      const T = d.base > 0 ? d.base : 30;      // 채움두께 추정: 밑지 기준
      // 눌림 0~70% 중 실측과 가장 근접한 값 탐색
      let bestC = null, bestErr = Infinity;
      for (let c = 0; c <= 70; c += 5) {
        const n = ipsuMaxFit(C, d.form, d.W, d.H, d.base, T, pat, c / 100);
        const err = Math.abs(n - p.actual);
        if (n > 0 && err < bestErr) { bestErr = err; bestC = c; }
      }
      if (bestC === null) continue;
      const calc0 = ipsuMaxFit(C, d.form, d.W, d.H, d.base, T, pat, 0);
      rows.push({ code: p.spec_code, name: p.spec_name, actual: p.actual,
                  calc0, comp: bestC, err: bestErr });
    }
    if (!rows.length) { el.textContent = '대조 가능한 항목이 없습니다.'; return; }
    const good = rows.filter(r => r.err <= 2);
    const comps = good.map(r => r.comp).sort((a, b) => a - b);
    const med = comps.length ? comps[Math.floor(comps.length / 2)] : 0;
    const avg = comps.length ? Math.round(comps.reduce((s, v) => s + v, 0) / comps.length) : 0;
    const sample = rows.slice(0, 6).map(r =>
      `<div style="padding:2px 0;color:var(--text-2)">${r.code} · 실측 ${r.actual} / 무압축계산 ${r.calc0}
       → <b style="color:#0369a1">눌림 ${r.comp}%</b> <span style="color:var(--text-3)">(오차 ${r.err})</span></div>`).join('');
    el.innerHTML = `<b style="color:#0369a1">🎯 실측 캘리브레이션</b>
      <span style="color:var(--text-3)">현재 외박스 ${C.W}×${C.D}×${C.H} · 배열 기준 · ${rows.length}건 대조
      (오차 ≤2 인 ${good.length}건으로 산출)</span><br>
      <div style="margin:5px 0"><b>추정 눌림률 — 중앙값 ${med}% / 평균 ${avg}%</b>
      <span onclick="ipsuApplyComp(${med})" style="margin-left:8px;padding:4px 9px;border:1px solid #0369a1;
        border-radius:6px;cursor:pointer;background:#f0f9ff;color:#0369a1;font-weight:600">이 값 적용</span></div>
      ${sample}
      <div style="margin-top:5px;color:var(--text-3)">※ 실측 입수는 제품마다 박스 규격이 달라, 현재 입력된 외박스 기준 추정치입니다</div>`;
  }
  function ipsuApplyComp(v) {
    document.getElementById('ipsu-comp').value = v;
    ipsuCalc();
  }

  // ── 입수 → 채움두께(T) 역산: 현재 입수가 딱 들어가는 두께를 T칸에 기록 ──
  function ipsuBackSolveT() {
    const g = id => document.getElementById(id);
    const comp = Math.max(0, Math.min(0.8, _ig('ipsu-comp') / 100));
    const bForm = g('ipsu-b-form').value;
    const aForm = g('ipsu-a-form').value;
    const aW = _ig('ipsu-aw'), aH = _ig('ipsu-ah'), aBase = aForm === '스탠드' ? _ig('ipsu-abase') : 0;
    const C = ipsuInnerC();
    const msg = [];
    if (bForm === '없음') {
      const n = Math.round(_ig('ipsu-n2'));
      const t = ipsuSolveThick(C, aForm, aW, aH, aBase, 0, g('ipsu-pat2').value, comp, n);
      if (t > 0) { g('ipsu-at').value = Math.round(t * 10) / 10; msg.push(`1차 두께 ${t.toFixed(1)}mm`); }
    } else {
      // 2차부터 (외박스 기준) → 그 두께가 1차의 컨테이너 깊이가 됨
      let bInner = null;
      if (bForm !== '단상자') {
        const bW = _ig('ipsu-bw'), bH = _ig('ipsu-bh'), bB = bForm === '스탠드' ? _ig('ipsu-bbase') : 0;
        const n2 = Math.round(_ig('ipsu-n2'));
        const t2 = ipsuSolveThick(C, bForm, bW, bH, bB, 0, g('ipsu-pat2').value, comp, n2);
        if (t2 > 0) { g('ipsu-bt').value = Math.round(t2 * 10) / 10; msg.push(`2차 두께 ${t2.toFixed(1)}mm`);
          bInner = { W: bW, H: Math.max(20, bH - bB * 0.5), D: t2 * (1 - comp) }; }
      } else bInner = { W: _ig('ipsu-bbw'), H: _ig('ipsu-bbh'), D: _ig('ipsu-bbd') };
      if (bInner) {
        const n1 = Math.round(_ig('ipsu-n1'));
        const t1 = ipsuSolveThick(bInner, aForm, aW, aH, aBase, 0, g('ipsu-pat1').value, comp, n1);
        if (t1 > 0) { g('ipsu-at').value = Math.round(t1 * 10) / 10; msg.push(`1차 두께 ${t1.toFixed(1)}mm`); }
      }
    }
    const el = document.getElementById('ipsu-reco');
    el.style.display = 'block';
    el.innerHTML = msg.length
      ? `<b style="color:#b45309">📏 입수 기준 역산 완료</b> — ${msg.join(' · ')}
         <span style="color:var(--text-3)">채움두께 T 칸에 반영됨. 이제 입수를 올리면 위로 쌓이고 초과 시 경고합니다.</span>`
      : '<span style="color:#b45309">역산 실패 — 현재 입수가 이 박스에 담길 수 없습니다.</span>';
    ipsuCalc();
  }

  // ── 최적 외박스 역제안 ──
  // 2단계에 담기는 아이템(파우치 또는 단상자)의 실제 점유 크기
  function ipsuStage2Item() {
    const bForm = document.getElementById('ipsu-b-form').value;
    const comp = Math.max(0, Math.min(0.8, _ig('ipsu-comp') / 100));
    const pat2 = document.getElementById('ipsu-pat2').value;
    const posture = pat2 === 'stack' ? '눕힘' : '세움';
    if (bForm === '단상자')
      return { s: { x: _ig('ipsu-bbw'), y: _ig('ipsu-bbh'), z: _ig('ipsu-bbd') }, name: '단상자' };
    if (bForm === '없음') {
      const f = document.getElementById('ipsu-a-form').value;
      const base = f === '스탠드' ? _ig('ipsu-abase') : 0;
      return { s: ipsuOrient(ipsuPouchGeomC(f, _ig('ipsu-aw'), _ig('ipsu-ah'), base, _ig('ipsu-at'), comp),
               posture, false, false).size, name: '1차파우치' };
    }
    const base = bForm === '스탠드' ? _ig('ipsu-bbase') : 0;
    return { s: ipsuOrient(ipsuPouchGeomC(bForm, _ig('ipsu-bw'), _ig('ipsu-bh'), base, _ig('ipsu-bt'), comp),
             posture, false, false).size, name: '2차파우치' };
  }
  function ipsuSuggestBox() {
    const it = ipsuStage2Item(), s = it.s;
    const target = Math.max(1, Math.round(_ig('ipsu-n2')) || 12);
    const PW = _ig('ipsu-plw') || 1100, PD = _ig('ipsu-pld') || 1100, PH = _ig('ipsu-plh') || 1700;
    const cand = [], seen = new Set();
    for (let layers = 1; layers <= Math.min(8, target); layers++) {
      const perLayer = Math.ceil(target / layers);
      for (let cols = 1; cols <= perLayer; cols++) {
        const rows = Math.ceil(perLayer / cols);
        if (cols * rows * layers < target) continue;
        for (const rot of [false, true]) {          // 아이템 평면 방향
          const iw = rot ? s.z : s.x, id = rot ? s.x : s.z;
          // 올림(+여유 1mm) — 반올림하면 필요 치수보다 작아져 실제로 안 들어감
          let W = Math.ceil(cols * iw) + 1, D = Math.ceil(rows * id) + 1;
          const H = Math.ceil(layers * s.y) + 1;
          let cc = cols, rr = rows;
          if (W > D) { const t = W; W = D; D = t; const tc = cc; cc = rr; rr = tc; }  // 미러 중복 제거
          const key = `${W}x${D}x${H}`;
          if (seen.has(key)) continue; seen.add(key);
          // ── 실무 제약: 너무 납작·길쭉하거나 과대/과소한 박스 제외 ──
          const mn = Math.min(W, D, H), mx = Math.max(W, D, H);
          if (mn < 120 || mx > 650) continue;          // 취급 가능 범위
          if (mx / mn > 3) continue;                   // 극단적 비율 제외
          if (H > Math.min(W, D) * 2.2) continue;      // 너무 높아 쓰러지는 형태 제외
          const cols2 = cc, rows2 = rr;
          const cap = cols * rows * layers;
          const waste = 1 - (target * s.x * s.y * s.z) / (W * D * H);
          // 팔레트 효율 (1100×1100 기준, 혼합방향 포함)
          const pl = ipsuFitPlan(PW, PD, W + 10, D + 10, 1.0);
          const tiers = Math.floor(PH / (H + 10));
          const palletBoxes = pl.count * tiers;
          const palUtil = (pl.count * (W + 10) * (D + 10)) / (PW * PD);
          const ratio = mx / Math.max(1, mn);
          cand.push({ W, D, H, cols: cols2, rows: rows2, layers, cap, waste: Math.round(waste * 100),
                      palletBoxes, palUtil: Math.round(palUtil * 100), ratio, rot });
        }
      }
    }
    // 팔레트 효율 우선 → 낭비 적은 순 → 정육면체에 가까운 순
    cand.sort((a, b) => b.palUtil - a.palUtil || a.waste - b.waste || a.ratio - b.ratio);
    const top = cand.filter(c => c.palletBoxes > 0).slice(0, 4);
    const el = document.getElementById('ipsu-reco');
    if (!top.length) { el.style.display = 'block'; el.textContent = '추천할 박스를 찾지 못했습니다.'; return; }
    el.style.display = 'block';
    el.innerHTML = `<b style="color:#6d28d9">📐 ${it.name} ${target}개 기준 추천 외박스</b>`
      + ` <span style="color:var(--text-3)">(팔레트 ${PW}×${PD}×${PH} 효율 우선)</span><br>`
      + top.map(c => `<span onclick="ipsuApplyBox(${c.W},${c.D},${c.H})" style="display:inline-block;margin:5px 6px 0 0;
          padding:5px 9px;border:1px solid #c4b5fd;border-radius:7px;cursor:pointer;background:#faf5ff">
          <b>${c.W + IPSU_WALL * 2}×${c.D + IPSU_WALL * 2}×${c.H + IPSU_WALL * 2}</b>
          <span style="color:var(--text-3)">외치수</span> · ${c.cols}×${c.rows}×${c.layers}층
          · 낭비 ${c.waste}% · 팔레트 ${c.palletBoxes}박스(면적 ${c.palUtil}%)</span>`).join('');
  }
  // 추천값은 필요한 '내부공간' → ③에는 외치수(+벽두께)로 넣고 ⑤까지 동기화
  function ipsuApplyBox(W, D, H) {
    const t = IPSU_WALL * 2;
    document.getElementById('ipsu-cw').value = Math.round(W + t);
    document.getElementById('ipsu-cd').value = Math.round(D + t);
    document.getElementById('ipsu-ch').value = Math.round(H + t);
    ipsuSyncOuter();
  }

  // ── 팔레트 적재 (외박스 → 팔레트) ──
  function ipsuPallet(perBoxTotal) {
    if (!_ipsuV3) return;
    const over = _ig('ipsu-over');
    // ③이 외치수이므로 그대로 사용 (⑤는 ③과 자동 연동)
    const bw = _ig('ipsu-bow') || _ig('ipsu-cw'),
          bd = _ig('ipsu-bod') || _ig('ipsu-cd'),
          bh = _ig('ipsu-boh') || _ig('ipsu-ch');
    const P = { W: _ig('ipsu-plw'), D: _ig('ipsu-pld'), H: _ig('ipsu-plh') };
    const area = { W: P.W + over * 2, D: P.D + over * 2, H: P.H };   // 오버행 허용분
    const plan = ipsuFitPlan(area.W, area.D, bw, bd, 1.0);
    const layers = bh > 0 ? Math.floor(area.H / bh) : 0;
    const total = plan.count * layers;

    const gap = 2;
    const box = (w, d) => { const g = new THREE.BoxGeometry(w - gap, bh - gap, d - gap);
      g.translate(w / 2, bh / 2, d / 2); return g; };
    const geoms = { n: box(bw, bd), s: box(bd, bw) };
    const blocks = plan.blocks.length ? plan.blocks
      : [{ x: 0, z: 0, cols: 0, rows: 0, rot: false, pitchX: bw, pitchZ: bd }];
    const items = [];
    for (let L = 0; L < layers; L++)
      for (const b of blocks)
        for (let r = 0; r < b.rows; r++)
          for (let c = 0; c < b.cols; c++)
            items.push({ x: b.x + c * b.pitchX, z: b.z + r * b.pitchZ, y: L * bh, sc: 1,
                         gk: b.rot ? 's' : 'n' });
    _ipsuV3.render({ W: area.W, H: area.H, D: area.D },
      { items, geoms, geom: geoms.n, isBox: true }, null, 150);   // 팔레트 위는 외박스

    document.getElementById('ipsu-s3cnt').textContent = total;
    const mix = blocks.length > 1
      ? ` · 혼합방향(${blocks.map(b => b.cols + '×' + b.rows + (b.rot ? '↻' : '')).join('+')})` : '';
    const useH = layers * bh;
    document.getElementById('ipsu-s3badge').textContent =
      total > 0 ? `${plan.count}박스/단 × ${layers}단 = ${total}박스${mix}`
                + ` · 높이 ${useH}/${P.H}mm · 박스외치수 ${bw}×${bd}×${bh}`
                + (perBoxTotal ? ` · 파우치 총 ${(total * perBoxTotal).toLocaleString()}개` : '')
                : '팔레트에 올라가지 않음 (박스 외치수 확인)';
    // PT적재량 실측 대조
    const el = document.getElementById('ipsu-pt');
    // ⓪ 완제품 선택이 우선, 없으면 '박스입수 조회'로 고른 것
    const picked = (_ipsuSelP && _ipsuSelP.pallet > 0) ? _ipsuSelP
      : _ipsuBoxes.find(b => document.getElementById('ipsu-c-q').value.startsWith(b.code));
    if (picked && picked.pallet > 0) {
      const diff = total - picked.pallet;
      el.innerHTML = `<b>${escapeHtml(picked.code)}</b> Monday PT적재량 <b>${picked.pallet}</b> vs 계산 <b>${total}</b>`
        + (diff === 0 ? ' · <span style="color:#059669">일치</span>'
                      : ` · <span style="color:#b45309">차이 ${diff > 0 ? '+' : ''}${diff}</span>`);
    } else {
      el.textContent = '완제품을 검색하면 실측 PT적재량과 비교됩니다';
    }
  }

  function _ig(id) { return parseFloat(document.getElementById(id).value) || 0; }
  const IPSU_WALL = 5;   // 골판지 편면 두께(mm) — 외치수 → 내부공간 환산
  // ③ 외박스 외치수 → 내부 적재공간
  function ipsuInnerC() {
    const w = _ig('ipsu-cw'), d = _ig('ipsu-cd'), h = _ig('ipsu-ch'), t = IPSU_WALL * 2;
    return { W: Math.max(1, w - t), D: Math.max(1, d - t), H: Math.max(1, h - t) };
  }
  // 입력 방식 전환 (배열 직접 입력 ↔ 입수 개수)
  function ipsuOnMode() {
    const grid = document.getElementById('ipsu-mode').value === 'grid';
    document.getElementById('ipsu-g1').style.display = grid ? 'contents' : 'none';
    document.getElementById('ipsu-c1').style.display = grid ? 'none' : 'contents';
    document.getElementById('ipsu-g2').style.display = grid ? 'contents' : 'none';
    document.getElementById('ipsu-c2').style.display = grid ? 'none' : 'contents';
    ipsuCalc();
  }
  function ipsuIsGrid() { return document.getElementById('ipsu-mode').value === 'grid'; }
  // ③ 입력 → ⑤ 팔레트용 외치수에 그대로 반영
  function ipsuSyncOuter() {
    document.getElementById('ipsu-bow').value = document.getElementById('ipsu-cw').value;
    document.getElementById('ipsu-bod').value = document.getElementById('ipsu-cd').value;
    document.getElementById('ipsu-boh').value = document.getElementById('ipsu-ch').value;
    ipsuCalc();
  }
  // 포장재비 — 선택한 규격의 단가(원) 기준
  function ipsuCost(n1, n2) {
    const el = document.getElementById('ipsu-cost');
    const pA = _ipsuSelA && _ipsuSelA.price > 0 ? _ipsuSelA.price : 0;
    const none = document.getElementById('ipsu-b-form').value === '없음';
    const pB = (!none && _ipsuSelB && _ipsuSelB.price > 0) ? _ipsuSelB.price : 0;
    if (!pA && !pB) {
      el.textContent = '※ 실측 입수 기반 시각화 — 규격을 검색 선택하면 포장재비도 계산됩니다';
      return;
    }
    const perBox = pA * n1 * n2 + pB * n2;          // 외박스 1개당 파우치 자재비
    const perUnit = perBox / Math.max(1, n1 * n2);  // 1차파우치 1개당
    const f = v => Math.round(v).toLocaleString();
    el.textContent = `포장재비 개당 ${f(perUnit)}원 · 박스당 ${f(perBox)}원`
      + (pA ? ` (1차 ${f(pA)}원` : '') + (pB ? ` + 2차 ${f(pB)}원` : '') + (pA ? ')' : '');
  }
  // 실측 최대입수 → 파우치 두께(_ipsuThinMul) 역산. 1차파우치가 담기는 컨테이너 기준.
  function ipsuCalibThickness() {
    const target = Math.round(_ig('ipsu-realmax'));
    const hint = document.getElementById('ipsu-realmax');
    if (!(target > 0)) { _ipsuThinMul = 1; hint.title = ''; ipsuCalc(); return; }
    const aForm = document.getElementById('ipsu-a-form').value;
    const aW = _ig('ipsu-aw'), aH = _ig('ipsu-ah');
    const aBase = aForm === '스탠드' ? _ig('ipsu-abase') : 0, aT = _ig('ipsu-at');
    const comp = Math.max(0, Math.min(0.8, _ig('ipsu-comp') / 100));
    const bForm = document.getElementById('ipsu-b-form').value;
    // 1차파우치가 실제로 담기는 컨테이너 + 배열
    let cont, pat;
    if (bForm === '없음') { cont = ipsuInnerC();
      pat = document.getElementById('ipsu-pat2').value; }
    else if (bForm === '단상자') { cont = { W: _ig('ipsu-bbw'), H: _ig('ipsu-bbh'), D: _ig('ipsu-bbd') };
      pat = document.getElementById('ipsu-pat1').value; }
    else { const bb = _ig('ipsu-bbase'), bt = _ig('ipsu-bt');
      cont = { W: _ig('ipsu-bw'), H: _ig('ipsu-bh'), D: bForm === '스탠드' ? Math.max(bb, bt) : bt };
      pat = document.getElementById('ipsu-pat1').value; }
    // 두께를 얇게(0.5)→두껍게(3.0) 늘려가며 최대입수가 target 이하로 떨어지는 첫 지점
    const save = _ipsuThinMul; let found = null;
    for (let m = 0.5; m <= 3.0; m += 0.01) {
      _ipsuThinMul = m;
      if (ipsuMaxFit(cont, aForm, aW, aH, aBase, aT, pat, comp) <= target) { found = m; break; }
    }
    _ipsuThinMul = found || save;
    const th = Math.max(aT, aBase, 4) * 1.215 * _ipsuThinMul;
    hint.title = `보정 두께 ≈ ${th.toFixed(1)}mm (배율 ${_ipsuThinMul.toFixed(2)})`;
    ipsuCalc();
  }

  function ipsuCalc() {
    if (!_ipsuV1) return;
    const aForm = document.getElementById('ipsu-a-form').value;
    const aW = _ig('ipsu-aw'), aH = _ig('ipsu-ah');
    const aBase = aForm === '스탠드' ? _ig('ipsu-abase') : 0, aT = _ig('ipsu-at');
    const bForm = document.getElementById('ipsu-b-form').value;
    const C = ipsuInnerC();   // ③은 외치수 → 내부공간으로 환산
    // 입수 칸이 비면(또는 0) 자동으로 최대치 계산
    const n1raw = document.getElementById('ipsu-n1').value.trim();
    const n2raw = document.getElementById('ipsu-n2').value.trim();
    const n1auto = !(parseFloat(n1raw) > 0), n2auto = !(parseFloat(n2raw) > 0);
    const pat1 = document.getElementById('ipsu-pat1').value, pat2 = document.getElementById('ipsu-pat2').value;
    const autoTag = ' · 자동 최대';
    const comp = Math.max(0, Math.min(0.8, _ig('ipsu-comp') / 100));   // 눌림(압축)
    const compTag = comp > 0 ? ` · 눌림 ${Math.round(comp * 100)}%` : '';
    const GRID = ipsuIsGrid();   // 배열 직접 입력 모드
    if (GRID) {   // 배열 → 개수 표시 갱신
      document.getElementById('ipsu-g1n').textContent =
        '=' + Math.max(1, Math.round(_ig('ipsu-g1c') * _ig('ipsu-g1r') * _ig('ipsu-g1l')));
      document.getElementById('ipsu-g2n').textContent =
        '=' + Math.max(1, Math.round(_ig('ipsu-g2c') * _ig('ipsu-g2r') * _ig('ipsu-g2l')));
    }

    // 내부용기 없음 — 1차파우치가 외박스로 직행 (단일 단계)
    if (bForm === '없음') {
      let layD, n2v, tag = '';
      if (GRID) {
        layD = ipsuGridLayout(C, aForm, aW, aH, aBase, aT, pat2, comp,
          _ig('ipsu-g2c'), _ig('ipsu-g2r'), _ig('ipsu-g2l'));
        n2v = layD.count;
      } else {
        const af = n2auto ? ipsuAutoFit(C, aForm, aW, aH, aBase, aT, pat2, comp) : null;
        n2v = n2auto ? Math.max(1, af.n) : Math.max(1, Math.round(parseFloat(n2raw)));
        layD = ipsuLayout(n2v, C, aForm, aW, aH, aBase, aT, n2auto ? af.pattern : pat2, comp, 0);
        tag = n2auto ? autoTag + af.note : '';
      }
      _ipsuV2.render(C, layD, null);
      document.getElementById('ipsu-s2cnt').textContent = n2v;
      document.getElementById('ipsu-s2badge').textContent =
        layD.label + tag + compTag + ipsuStats(layD, C, n2v);
      document.getElementById('ipsu-eq').textContent = '내부용기 없음 · 직행';
      document.getElementById('ipsu-total').innerHTML =
        n2v.toLocaleString() + ' <span style="font-size:13px;font-weight:600">개</span>';
      ipsuCost(1, n2v);
      ipsuPallet(n2v);
      return;
    }

    let Binner, Bshell = null, lay2, n2v, af2 = null, thB = 0, note2 = '';
    if (bForm === '단상자') {
      const W = _ig('ipsu-bbw'), D = _ig('ipsu-bbd'), H = _ig('ipsu-bbh');
      Binner = { W, H, D };
      n2v = GRID ? Math.max(1, Math.round(_ig('ipsu-g2c') * _ig('ipsu-g2r') * _ig('ipsu-g2l')))
        : (n2auto ? Math.max(1, ipsuMaxFitBox(C, W, D, H)) : Math.max(1, Math.round(parseFloat(n2raw))));
      lay2 = ipsuLayoutBox(n2v, C, W, D, H);
    } else {
      const W = _ig('ipsu-bw'), H = _ig('ipsu-bh');
      const base = bForm === '스탠드' ? _ig('ipsu-bbase') : 0, T = _ig('ipsu-bt');
      if (GRID) {
        lay2 = ipsuGridLayout(C, bForm, W, H, base, T, pat2, comp,
          _ig('ipsu-g2c'), _ig('ipsu-g2r'), _ig('ipsu-g2l'));
        n2v = lay2.count;
        thB = lay2.derivedT;   // 배열에서 산출된 2차 실제 두께
      } else {
        af2 = n2auto ? ipsuAutoFit(C, bForm, W, H, base, T, pat2, comp) : null;
        n2v = n2auto ? Math.max(1, af2.n) : Math.max(1, Math.round(parseFloat(n2raw)));
        // 두께는 고정(채움두께 T 기준), 자세만 결정 → 입수 늘리면 위로 쌓이고 초과 경고
        const sf2 = n2auto ? null : ipsuPickPattern(C, bForm, W, H, base, T, pat2, comp);
        const useP2 = n2auto ? af2.pattern : sf2.pattern;
        if (sf2) note2 = sf2.note;
        lay2 = ipsuLayout(n2v, C, bForm, W, H, base, T, useP2, comp, 0);
      }
      const bodyB = thB > 0 ? thB
        : (T > 0 ? T : Math.max(base, 4) * 1.215 * _ipsuThinMul);
      // 2차파우치 내부 캐비티 = 실제 두께 (봉투 최대용량이 아님)
      Binner = { W, H: Math.max(20, H - base * 0.5), D: bodyB * (1 - comp) };
      Bshell = ipsuOrient(ipsuPouchGeomC(bForm, W, H, base, T, comp, thB), '세움', false).geom;
    }
    let lay1, n1v, note1 = '', tag1 = '';
    if (GRID) {
      lay1 = ipsuGridLayout(Binner, aForm, aW, aH, aBase, aT, pat1, comp,
        _ig('ipsu-g1c'), _ig('ipsu-g1r'), _ig('ipsu-g1l'));
      n1v = lay1.count;
    } else {
      const af1 = n1auto ? ipsuAutoFit(Binner, aForm, aW, aH, aBase, aT, pat1, comp) : null;
      n1v = n1auto ? Math.max(1, af1.n) : Math.max(1, Math.round(parseFloat(n1raw)));
      const sf1 = n1auto ? null : ipsuPickPattern(Binner, aForm, aW, aH, aBase, aT, pat1, comp);
      note1 = n1auto ? af1.note : sf1.note;
      tag1 = n1auto ? autoTag : '';
      lay1 = ipsuLayout(n1v, Binner, aForm, aW, aH, aBase, aT, n1auto ? af1.pattern : sf1.pattern, comp, 0);
    }

    _ipsuV1.render(Binner, lay1, Bshell);
    _ipsuV2.render(C, lay2, null);
    const c1 = n1v, c2 = n2v;
    document.getElementById('ipsu-s1cnt').textContent = c1;
    document.getElementById('ipsu-s2cnt').textContent = c2;
    document.getElementById('ipsu-s1badge').textContent =
      lay1.label + tag1 + note1 + compTag + ipsuStats(lay1, Binner, c1);
    document.getElementById('ipsu-s2badge').textContent =
      lay2.label + (!GRID && n2auto ? autoTag + (af2 ? af2.note : '') : note2)
      + (bForm === '단상자' ? '' : compTag) + ipsuStats(lay2, C, c2);   // 단상자는 강체라 압축 없음
    document.getElementById('ipsu-eq').textContent = `${c1} × ${c2} =`;
    document.getElementById('ipsu-total').innerHTML =
      (c1 * c2).toLocaleString() + ' <span style="font-size:13px;font-weight:600">개</span>';
    ipsuCost(c1, c2);
    ipsuPallet(c1 * c2);
  }

  // ───── 부자재 규격 → 단가 계산기 연동 ─────
  function specLinkToCalc(cb) {
    // 단일 선택: 다른 체크 해제 + 행 하이라이트
    document.querySelectorAll('.spec-calc-cb').forEach(c => {
      c.checked = (c === cb && cb.checked);
      c.closest('.spec-row').classList.toggle('sc-linked', c === cb && cb.checked);
    });
    if (!cb.checked) {
      // 체크 해제 시 계산기 초기화
      resetPriceCalc();
      return;
    }

    const size = cb.dataset.size || '';
    const mat  = cb.dataset.mat  || '';
    const cat  = cb.dataset.cat  || '';
    _pcalcSizeRaw = size;  // 롤파우치 감지용 원본 사이즈 저장
    _pcalcMatRaw  = mat;   // 박스 토크나이저용 원본 재질 저장
    _pcalcActualPrice = +(cb.dataset.price || 0) || 0;  // 알고있는 품목의 실제 단가

    // 사이즈 파싱: "150*200+밑지70", "W70*H175", "70*175" 등
    let w = 0, h = 0, d = 0;
    const sm = size.match(/[A-Za-z]*\s*(\d+)\s*[A-Za-z]*\s*[*×xX]\s*[A-Za-z]*\s*(\d+)(?:[^0-9]*(\d+))?/);
    if (sm) { w = +sm[1]; h = +sm[2]; d = sm[3] ? +sm[3] : 0; }

    // 카테고리 탭 선택
    const tabCat = cat === '파우치' ? '파우치'
                 : cat === '단상자' ? '단상자'
                 : cat.indexOf('박스') >= 0 ? '박스' : '';
    _pcalcCat = tabCat;
    document.querySelectorAll('.pcalc-cat-tab').forEach(t =>
      t.classList.toggle('active', (t.dataset.cat || '') === tabCat));
    updatePcalcDepthLabel();

    // W / H / D 입력
    document.getElementById('pcalc-w').value = w || '';
    document.getElementById('pcalc-h').value = h || '';
    document.getElementById('pcalc-d').value = d || '';

    // 재질 정규화: ㎛ 제거, = 주변 공백 제거, 공백 제거, 소문자
    function normMat(s) {
      return s.replace(/㎛/g,'').replace(/\s*=\s*/g,'=').replace(/\s+/g,'').toLowerCase();
    }
    // "/" 기준으로만 분리 (괄호 안 "/" 무시, 콤마는 분리하지 않음)
    function splitMatBySlash(str) {
      const parts = []; let cur = '', depth = 0;
      for (const c of str) {
        if (c === '(') { depth++; cur += c; }
        else if (c === ')') { depth = Math.max(0, depth-1); cur += c; }
        else if (c === '/' && depth === 0) {
          if (cur.trim()) parts.push(cur.trim()); cur = '';
        } else { cur += c; }
      }
      if (cur.trim()) parts.push(cur.trim());
      return parts.filter(Boolean);
    }
    // DB 정규화 맵
    const dbNormMap = new Map();
    (_pcalcMaterials || []).forEach(m => { dbNormMap.set(normMat(m.name), m.name); });

    function tryMatch(seg) {
      const s = seg.replace(/㎛/g,'').replace(/\s*=\s*/g,'=').trim();
      // 1) 정규화 일치 (공백·㎛·= 정규화)
      const k1 = normMat(s);
      if (dbNormMap.has(k1)) return dbNormMap.get(k1);
      // 2) 콤마 제거 후 매칭 ("SC,K,K"→"SCKK", "LLD95=123M,지퍼스탠드"→"LLD95=123M지퍼스탠드")
      const k2 = k1.replace(/,/g,'');
      if (k2 !== k1 && dbNormMap.has(k2)) return dbNormMap.get(k2);
      // 3) 괄호 내용 제거 후 매칭 ("LLDPE110(D/R합지)"→"LLDPE110")
      const k3 = normMat(s.replace(/\([^)]*\)/g,''));
      if (k3 && k3 !== k1 && dbNormMap.has(k3)) return dbNormMap.get(k3);
      return null;
    }

    const segments = splitMatBySlash(mat);
    const firstMat = segments[0] || '';
    document.getElementById('pcalc-search').value = firstMat.replace(/㎛/g,'').trim();

    _pcalcSelected.clear();
    // 1) 서버 제공 토큰 우선 — 박스 '*' 재질(예: SK180*AK180*SQ155)·골 등 서버와 동일 기준이라 누락 없음
    let srvTokens = [];
    try { srvTokens = JSON.parse(cb.dataset.tokens || '[]'); } catch (e) {}
    if (srvTokens.length) {
      const known = new Set((_pcalcMaterials || []).map(m => m.name));
      srvTokens.forEach(t => { if (known.has(t)) _pcalcSelected.add(t); });
    }
    // 2) 폴백: 서버 토큰이 없거나 하나도 매칭 안 되면 기존 문자열 매칭
    if (_pcalcSelected.size === 0) {
      segments.forEach(seg => {
        const hit = tryMatch(seg);
        if (hit) { _pcalcSelected.add(hit); return; }
        // 콤마로 분리 후 각각 매칭
        seg.split(',').map(p => p.trim()).filter(Boolean).forEach(p => {
          const h = tryMatch(p);
          if (h) _pcalcSelected.add(h);
        });
      });
    }
    renderPriceCalcList();
    renderSelectedBar();
    // 기준 사이즈 저장 (비교바용) — price는 첫 estimate 완료 후 채워짐
    _pcalcBase = { price: null, w, h, d };
    const comparBar = document.getElementById('pcalc-compare-bar');
    if (comparBar) comparBar.style.display = 'none';
    updatePriceCalc();

    // 계산기로 스크롤
    const calc = document.querySelector('.pcalc-panel');
    if (calc) calc.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function renderSpecList() {
    const list = document.getElementById('spec-list');
    const cnt = document.getElementById('spec-count');
    const q = (document.getElementById('spec-search').value || '').toLowerCase().trim();
    const all = _specItems;
    const items = q
      ? all.filter(it => (it.code||'').toLowerCase().includes(q)
          || (it.name||'').toLowerCase().includes(q)
          || (it.material||'').toLowerCase().includes(q)
          || (it.vendor||'').toLowerCase().includes(q)
          || (it.div||'').toLowerCase().includes(q))
      : all;
    cnt.textContent = items.length + '/' + all.length + '건';
    if (!items.length) {
      list.innerHTML = '<div class="alert-empty">' + (q ? '검색 결과 없음' : '데이터 없음') + '</div>';
      return;
    }
    let html = '<div class="spec-head">'
      + '<div>품번</div><div>품명</div><div>구분</div><div>사이즈</div><div>재질</div>'
      + '<div class="h-check" title="단가 계산기 연동">↑</div>'
      + '<div class="h-moq">MOQ</div><div class="h-price">단가</div><div>업체</div>'
      + '</div>';
    html += items.map(it => {
      const catCls = it.category === '파우치' ? 'sc-cat-pouch' : (it.category === '단상자' ? 'sc-cat-단상자' : (it.category && it.category.indexOf('RRP') >= 0 ? 'sc-cat-rrp' : ''));
      const cbAttrs = 'data-code="' + escapeHtml(it.code||'') + '"'
        + ' data-size="' + escapeHtml(it.size||'') + '"'
        + ' data-mat="' + escapeHtml(it.material||'') + '"'
        + ' data-cat="' + escapeHtml(it.category||'') + '"'
        + ' data-price="' + (it.price || 0) + '"'
        + ' data-tokens="' + escapeHtml(JSON.stringify(it.tokens||[])) + '"';
      const divVal = it.div || '';
      const divCls = divVal === '쿠팡' ? 'sc-div-쿠팡'
        : (divVal.indexOf('홈플') >= 0 ? 'sc-div-홈플러스'
        : (divVal === '롯데' ? 'sc-div-롯데'
        : (divVal.indexOf('마트') >= 0 ? 'sc-div-이마트'
        : (divVal === '3P' ? 'sc-div-3P'
        : (divVal === '공용' ? 'sc-div-공용' : '')))));
      return '<div class="spec-row" onclick="openItemModal(\\'' + escapeHtml(it.code) + '\\')">'
        + '<span class="sc-code">' + escapeHtml(it.code || '-') + '</span>'
        + '<span class="sc-name" title="' + escapeHtml(it.name) + '">' + escapeHtml(it.name) + '</span>'
        + '<span class="sc-div ' + divCls + '" title="' + escapeHtml(divVal) + '">' + escapeHtml(divVal || '-') + '</span>'
        + '<span class="sc-size" title="' + escapeHtml(it.size || '') + '">' + escapeHtml(it.size || '-') + '</span>'
        + '<span class="sc-mat" title="' + escapeHtml(it.material) + '">' + escapeHtml(it.material) + '</span>'
        + '<span class="sc-check" onclick="event.stopPropagation()">'
        + '<input type="checkbox" class="spec-calc-cb" ' + cbAttrs + ' onchange="specLinkToCalc(this)" title="단가 계산기에 연동">'
        + '</span>'
        + '<span class="sc-moq">' + (it.moq ? fmtNum(it.moq) : '-') + '</span>'
        + '<span class="sc-price">' + (it.price ? fmtNum(it.price) + '원' : '-') + '</span>'
        + '<span class="sc-vendor ' + catCls + '" title="' + escapeHtml(it.vendor) + '">' + escapeHtml(it.vendor || '-') + '</span>'
        + '</div>';
    }).join('');
    list.innerHTML = html;
  }
</script>
</body>
</html>'''


# ────────────────────────────────────────────
# HTML 템플릿
# ────────────────────────────────────────────
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>매홍 L&F - 재고 조회 챗봇</title>
  <script>(function(){const f=window.fetch.bind(window);window.fetch=function(i,n){n=n||{};const h=new Headers(n.headers||{});if(!h.has('ngrok-skip-browser-warning'))h.set('ngrok-skip-browser-warning','true');n.headers=h;return f(i,n);};})();</script>
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
    const fbDb = firebase.firestore();   // shared(공개) 읽기 등 비인증 용도
    const SERVER_USER = {{ user_json|safe }};
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
    header p { font-size: 11px; color: #64748b; margin-top: 2px; }
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
      color: #64748b;
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

    .sender-name { color: #64748b; }
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
  <a href="/jasa" class="admin-link">자사</a>
  <!-- 사용자 정보 + 로그아웃 -->
  <div id="auth-area" style="margin-left:auto;display:flex;align-items:center;gap:8px;">
    <span id="user-name" style="font-size:12px;color:#64748b;display:none"></span>
    <button id="logout-btn" onclick="googleLogout()" style="display:none;font-size:11px;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:8px;padding:3px 10px;cursor:pointer;color:#64748b">로그아웃</button>
  </div>
</header>

<!-- 로그인 오버레이 제거됨 (대시보드에서 로그인 처리) -->

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
      <div style="font-size:11px;color:#64748b;margin-bottom:6px;font-weight:600">이전 대화</div>
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
      <input type="text"
        id="user-input"
        placeholder="질문을 입력하세요... (재고, 발주, 생산, BOM, 매출 등)"
        onkeydown="handleKeydown(event)"
        oninput="debounceSuggest(this.value)"
        autocomplete="off"
      />
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

  function handleKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      e.stopPropagation();
      if (typeof sendMessage === 'function') sendMessage();
      return false;
    }
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
      addMessage('bot', '로그인이 필요합니다. 페이지를 새로고침하고 Google 로그인을 해주세요.');
      return;
    }

    const input = document.getElementById('user-input');
    const message = input.value.trim();
    if (!message) return;

    // chatId 없으면 서버에서 자동 생성
    if (currentUser && !currentChatId) {
      try {
        const r = await fetch('/api/chats', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: message.substring(0, 30) }),
        });
        const d = await r.json();
        currentChatId = d.id;
      } catch(e) { console.log('chat create error', e); }
    }

    input.value = '';
    input.style.height = 'auto';
    isLoading = true;
    document.getElementById('send-btn').disabled = true;

    // Add user message
    addMessage('user', message);
    chatHistory.push({ role: 'user', content: message });
    // Firestore에 사용자 메시지 저장
    saveMessage('user', message);

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
        // Firestore에 봇 응답 저장
        saveMessage('assistant', data.message);
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

  function googleLogout() { location.href = '/auth/logout'; }

  // 서버 세션 기반 초기화 (게이트는 서버가 처리)
  (function initChatUser(){
    currentUser = SERVER_USER;
    const input = document.getElementById('user-input');
    const sendBtn = document.getElementById('send-btn');
    if (input) { input.disabled = false; input.placeholder = '질문을 입력하세요... (재고, 발주, 생산, BOM, 매출 등)'; }
    if (sendBtn) sendBtn.disabled = false;
    const nameEl = document.getElementById('user-name');
    const logoutBtn = document.getElementById('logout-btn');
    const shareBtn = document.getElementById('share-btn');
    if (SERVER_USER) {
      if (nameEl) { nameEl.style.display = 'inline'; nameEl.textContent = SERVER_USER.name || SERVER_USER.email; }
      if (logoutBtn) logoutBtn.style.display = 'inline';
      if (shareBtn) shareBtn.style.display = 'inline';
      loadHistory();
    } else {
      if (nameEl) { nameEl.style.display = 'inline'; nameEl.textContent = '게스트'; }
      if (logoutBtn) logoutBtn.style.display = 'inline';
      if (shareBtn) shareBtn.style.display = 'none';
    }
  })();

  // 새 대화
  function newChat() {
    chatHistory = [];
    document.getElementById('chat-box').innerHTML = '';
    currentChatId = null;
    if (currentUser) {
      fetch('/api/chats', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({title:'새 대화'})})
        .then(r=>r.json()).then(d=>{ currentChatId = d.id; }).catch(()=>{});
    }
  }

  // 대화 저장 (메시지 추가 시) — 서버 API
  function saveMessage(role, content) {
    if (!currentUser || !currentChatId) return;
    fetch('/api/chats/' + currentChatId + '/messages', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role, content }),
    }).catch(()=>{});
  }

  // (대화 저장은 sendMessage 내부에서 직접 처리)

  // 대화 기록 로드
  async function loadHistory() {
    if (!currentUser) return;
    let arr = [];
    try { arr = await (await fetch('/api/chats')).json(); } catch(e) { arr = []; }
    const list = document.getElementById('history-list');
    list.innerHTML = '';
    (Array.isArray(arr) ? arr : []).forEach(d => {
      const div = document.createElement('div');
      div.style.cssText = 'padding:4px 8px;margin-bottom:3px;background:white;border:1px solid #e5e7eb;border-radius:6px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
      div.textContent = d.title || '새 대화';
      div.onclick = () => loadChat(d.id);
      div.onmouseenter = () => div.style.background = '#e0e7ff';
      div.onmouseleave = () => div.style.background = 'white';
      list.appendChild(div);
    });
    if (!arr || !arr.length) {
      list.innerHTML = '<div style="color:#64748b;font-size:11px">아직 대화 기록이 없습니다</div>';
    }
  }

  // 이전 대화 불러오기
  async function loadChat(chatId) {
    const r = await fetch('/api/chats/' + chatId);
    if (!r.ok) return;
    const d = await r.json();
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
    const r = await fetch('/api/chats/' + currentChatId + '/share', { method: 'POST' });
    const sd = await r.json();
    if (!sd.shareId) { alert('공유 실패'); return; }
    const shareUrl = window.location.origin + '/shared/' + sd.shareId;
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
  <script>(function(){const f=window.fetch.bind(window);window.fetch=function(i,n){n=n||{};const h=new Headers(n.headers||{});if(!h.has('ngrok-skip-browser-warning'))h.set('ngrok-skip-browser-warning','true');n.headers=h;return f(i,n);};})();</script>
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
    header p { font-size: 11px; color: #64748b; margin-top: 2px; }
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
  <script>(function(){const f=window.fetch.bind(window);window.fetch=function(i,n){n=n||{};const h=new Headers(n.headers||{});if(!h.has('ngrok-skip-browser-warning'))h.set('ngrok-skip-browser-warning','true');n.headers=h;return f(i,n);};})();</script>
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
    """세션 로그인 사용자 우선 → user dict(uid/name/email). 없으면 Firebase ID 토큰 검증."""
    su = session.get('user')
    if su and su.get('uid'):
        return su
    auth_header = req.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None
    token = auth_header.split('Bearer ')[1]
    try:
        return fb_auth.verify_id_token(token)
    except Exception:
        return None


@app.route('/api/history', methods=['GET'])
def list_history():
    """대시보드 질의 기록 목록 (세션 사용자)."""
    user = current_user()
    if not user:
        return jsonify([])
    docs = (FIRESTORE_DB.collection('chats')
            .where('userId', '==', user['uid'])
            .limit(200).stream())
    items = []
    for doc in docs:
        d = doc.to_dict()
        if d.get('source') != 'dashboard':
            continue
        msgs = d.get('messages', [])
        q = next((m.get('content') for m in msgs if m.get('role') == 'user'), d.get('title', ''))
        a = next((m.get('content') for m in msgs if m.get('role') == 'assistant'), '')
        created = d.get('createdAt')
        items.append({
            'id': doc.id,
            'ts': str(created)[:19] if created else '',
            '_sort': str(created),
            'question': q,
            'answer': a,
        })
    items.sort(key=lambda x: x['_sort'], reverse=True)
    for it in items:
        it.pop('_sort', None)
    return jsonify(items)


@app.route('/api/history', methods=['POST'])
def save_history():
    """대시보드 질의 기록 저장 (세션 사용자)."""
    user = current_user()
    if not user:
        return jsonify({'ok': False, 'reason': 'guest'})
    data = request.json or {}
    q = (data.get('question') or '')[:2000]
    a = data.get('answer') or ''
    FIRESTORE_DB.collection('chats').add({
        'userId': user['uid'],
        'userName': user.get('name', ''),
        'title': q[:60],
        'source': 'dashboard',
        'messages': [
            {'role': 'user', 'content': q, 'timestamp': datetime.now().isoformat()},
            {'role': 'assistant', 'content': a, 'timestamp': datetime.now().isoformat()},
        ],
        'createdAt': fs_admin.SERVER_TIMESTAMP,
    })
    return jsonify({'ok': True})


@app.route('/api/history/<doc_id>', methods=['DELETE'])
def delete_history(doc_id):
    """대시보드 질의 기록 삭제 (본인 것만)."""
    user = current_user()
    if not user:
        return jsonify({'ok': False}), 401
    ref = FIRESTORE_DB.collection('chats').document(doc_id)
    doc = ref.get()
    if doc.exists and doc.to_dict().get('userId') == user['uid']:
        ref.delete()
        return jsonify({'ok': True})
    return jsonify({'ok': False}), 403


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


@app.route('/api/refresh_monday', methods=['POST'])
def api_refresh_monday():
    """대시보드 새로고침 버튼: 백그라운드로 4개 보드 재수집."""
    if os.environ.get('ENABLE_AUTO_FETCH', '1') != '1':
        return jsonify({'started': False, 'reason': 'disabled_on_cloud',
                        'message': '클라우드에서는 수집이 꺼져 있습니다(서버 보호). 데이터는 PC에서 자동 동기화되며, 즉시 반영은 "메모리 리로드"를 사용하세요.'}), 403
    if MONDAY_REFRESH_STATUS.get('running'):
        return jsonify({'started': False, 'reason': 'already_running'})
    threading.Thread(target=_refresh_monday_once, daemon=True).start()
    return jsonify({'started': True})


@app.route('/api/refresh_monday/status')
def api_refresh_monday_status():
    return jsonify(MONDAY_REFRESH_STATUS)


@app.route('/api/refresh_aramanth', methods=['POST'])
def api_refresh_aramanth():
    """대시보드 새로고침 버튼: 백그라운드로 fetch_all + fetch_bom 병렬 실행."""
    if os.environ.get('ENABLE_AUTO_FETCH', '1') != '1':
        return jsonify({'started': False, 'reason': 'disabled_on_cloud',
                        'message': '클라우드에서는 수집이 꺼져 있습니다(서버 보호). 데이터는 PC에서 자동 동기화되며, 즉시 반영은 "메모리 리로드"를 사용하세요.'}), 403
    if ARAMANTH_REFRESH_STATUS.get('running'):
        return jsonify({'started': False, 'reason': 'already_running'})
    threading.Thread(target=_refresh_aramanth_once, daemon=True).start()
    return jsonify({'started': True})


@app.route('/api/refresh_aramanth/status')
def api_refresh_aramanth_status():
    return jsonify(ARAMANTH_REFRESH_STATUS)


@app.route('/api/reload_dfs', methods=['POST'])
def api_reload_dfs():
    """fetch 없이 디스크의 최신 CSV들을 메모리에 다시 로드 (1~2초). 자동갱신 후 메모리 미반영 복구용."""
    global _DATA_VERSION, _DATA_SYNCED_AT
    try:
        result = _reload_aramanth_dfs()
        import time as _t
        _DATA_SYNCED_AT = int(_t.time())          # 표시용 리로드 시각
        # ★ 버전은 '내용 해시'. 파일명 날짜만 바뀌고 내용이 같으면 버전 불변
        #   → 데이터가 실제로 바뀐 리로드에서만 클라이언트가 새로고침한다.
        _DATA_VERSION = _compute_data_version()
        return jsonify({'ok': True, 'loaded': len(result['ok']), 'failed': result['fail'], 'version': _DATA_VERSION})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:200]}), 500


# ── Monday 실시간(웹훅) 반영 ──────────────────────────────
import time as _time_mod
import hashlib as _hashlib_dv
_MONDAY_DIRTY = 0.0                       # 마지막 변경 알림 시각 (0=없음)
_DATA_SYNCED_AT = int(_time_mod.time())   # 마지막 리로드 시각 (표시용)


def _compute_data_version():
    """로드되는 타입별 최신 CSV의 '내용'만 md5 → 데이터가 실제 바뀔 때만 값이 변한다.

    ⚠️ 이전엔 리로드 시각(int(time.time()))을 버전으로 써서, update_cloud가
    파일명 날짜(20260806_…)만 바뀐 '내용 동일' 파일을 재업로드→reload_dfs 할 때마다
    버전이 바뀌어 모든 접속자가 30분마다 강제 새로고침됐다. 내용 해시로 바꾸면
    실제 데이터 변경이 없는 리로드는 같은 버전 → 새로고침 안 함.
    """
    import glob as _g
    pats = ['재고일지', '단가', '부자재규격', '자사재고', '발주정보', '외주발주정보',
            '생산실적', '출하정보', '출고정보', 'BOM', '입고정보', '현재고',
            '생산지시', 'monday', '판매단가']
    h = _hashlib_dv.md5()
    for name in sorted(pats):
        files = sorted(_g.glob(f'{DATA_DIR}/*_{name}.csv'), reverse=True)
        if not files:
            h.update(('%s:none' % name).encode())
            continue
        try:
            with open(files[0], 'rb') as f:
                h.update(name.encode())
                h.update(f.read())
        except OSError:
            h.update(('%s:err' % name).encode())
    return h.hexdigest()[:16]


_DATA_VERSION = _compute_data_version()   # 데이터 버전 — 내용 해시(내용 동일=동일 버전)


@app.route('/api/monday_webhook', methods=['POST'])
def monday_webhook():
    """Monday.com 웹훅 수신. 변경 시 dirty 표시 → 로컬 PC가 폴링해 즉시 수집."""
    global _MONDAY_DIRTY
    data = request.get_json(silent=True) or {}
    if 'challenge' in data:               # Monday 웹훅 등록 핸드셰이크
        return jsonify({'challenge': data['challenge']})
    _MONDAY_DIRTY = _time_mod.time()
    print(f"[Monday webhook] 변경 감지 @ {_MONDAY_DIRTY}")
    return jsonify({'ok': True})


@app.route('/api/monday_dirty', methods=['GET'])
def monday_dirty():
    return jsonify({'dirty': _MONDAY_DIRTY > 0, 'since': _MONDAY_DIRTY})


@app.route('/api/monday_dirty/clear', methods=['POST'])
def monday_dirty_clear():
    global _MONDAY_DIRTY
    _MONDAY_DIRTY = 0
    return jsonify({'ok': True})


@app.route('/api/data_health', methods=['GET'])
@cached_api(ttl=300)
def api_data_health():
    """데이터 건강검진 — 거래데이터의 '내용 최신월'을 검사해 조용한 절단/고착을 탐지.

    파일 날짜는 최신인데 내용이 옛날 달에 멈춘 사례가 반복돼(입고·출고 절단 2회),
    파일 mtime이 아니라 **실제 로드된 DF의 날짜컬럼 최대 월**을 기준으로 판정한다.
    months_behind 0=정상, 1=주의(이번달 자료 없음), 2+=이상(절단 의심).
    """
    from datetime import datetime as _dt

    CHECKS = [
        ('발주정보',   'ORDER_DF',    '발주일자'),
        ('외주발주',   'WP_ORDER_DF', '발주일자'),
        ('생산실적',   'PROD_DF',     '실적일자'),
        ('생산지시',   'WO_DF',       '지시일자'),
        ('출하정보',   'SHIP_DF',     '출하일자'),
        ('출고정보',   'ISSUE_DF',    '출고일자'),
        ('입고정보',   'RCV_DF',      '입고일자'),
    ]
    now = _dt.now()
    cur_ym = now.strftime('%Y%m')

    items, worst = [], 0
    for label, dfname, datecol in CHECKS:
        df = globals().get(dfname)
        rec = {'label': label, 'date_col': datecol, 'rows': 0,
               'latest_ym': '', 'months_behind': None, 'status': 'unknown', 'msg': ''}
        if df is None or getattr(df, 'empty', True):
            rec['status'] = 'error'; rec['msg'] = '데이터 없음'
            worst = max(worst, 2)
            items.append(rec); continue
        if datecol not in df.columns:
            rec['status'] = 'unknown'; rec['msg'] = f'{datecol} 컬럼 없음'
            items.append(rec); continue

        # 일 단위 판정 — 월 단위는 월초마다 "이번달 자료 없음" 오탐이 나고,
        # 반대로 월중 절단(출고 8/11 고착 사례)은 같은 달이라 못 잡았음.
        s = (df[datecol].astype(str).str.replace('-', '', regex=False).str.strip().str[:8])
        s = s[s.str.match(r'^20\d{6}$', na=False)]
        rec['rows'] = int(len(df))
        if s.empty:
            rec['status'] = 'error'; rec['msg'] = '유효 날짜 없음'
            worst = max(worst, 2)
            items.append(rec); continue

        mx = s.max()
        rec['latest_ym'] = f'{mx[:4]}-{mx[4:6]}-{mx[6:8]}'   # 최신 내용일 (프론트 표시용)
        try:
            days = (now.date() - _dt.strptime(mx, '%Y%m%d').date()).days
        except ValueError:
            rec['status'] = 'error'; rec['msg'] = f'날짜 형식 이상({mx})'
            worst = max(worst, 2)
            items.append(rec); continue
        rec['days_behind'] = days
        # 주말·연휴(최대 1주)는 거래가 없어도 정상. 그 이상 조용하면 절단 의심.
        if days <= 7:
            rec['status'] = 'ok'; rec['msg'] = '정상' if days <= 1 else f'{days}일 전'
        elif days <= 20:
            rec['status'] = 'warn'; rec['msg'] = f'{days}일째 미갱신'
            worst = max(worst, 1)
        else:
            rec['status'] = 'error'; rec['msg'] = f'{days}일째 미갱신 — 수집 절단 의심'
            worst = max(worst, 2)
        items.append(rec)

    # ── 수동/보조 원천 점검 (2026-09-11 추가): 자동수집 밖에 있어 조용히 낡던 것들 ──
    def _aux(label, when, warn_d, err_d, note='', latest=''):
        rec = {'label': label, 'date_col': '', 'rows': 0, 'latest_ym': latest, 'months_behind': None,
               'status': 'unknown', 'msg': note}
        if when is None:
            rec['status'] = 'warn'; rec['msg'] = note or '원본 없음'
            items.append(rec); return 1
        days = (now.date() - when.date()).days
        rec['days_behind'] = days
        rec['latest_ym'] = latest or when.strftime('%Y-%m-%d')
        if days <= warn_d:
            rec['status'] = 'ok'; rec['msg'] = ('정상' if days <= 1 else f'{days}일 전') + (' · ' + note if note else '')
            items.append(rec); return 0
        if days <= err_d:
            rec['status'] = 'warn'; rec['msg'] = f'{days}일째 미갱신' + (' · ' + note if note else '')
            items.append(rec); return 1
        rec['status'] = 'error'; rec['msg'] = f'{days}일째 미갱신' + (' · ' + note if note else '')
        items.append(rec); return 2

    def _mtime(p):
        try:
            return _dt.fromtimestamp(os.path.getmtime(p)) if p and os.path.exists(p) else None
        except OSError:
            return None

    # 판매 CSV(수동, 월 1회): 최신 완결월이 전월이면 정상, 그 이전이면 갱신 필요
    try:
        if SALES_SOURCE.get('kind') == 'api':
            # 판매 API(일자별): 파일 수집일 기준 — 전날 자료는 매일 09시 이후 수집 (주말·휴일 감안 3일/7일)
            _sf = sorted(glob.glob(f'{DATA_DIR}/*_판매일별.csv'))
            worst = max(worst, _aux('판매(API 일자별)', _mtime(_sf[-1]) if _sf else None, 3, 7,
                                    f'온라인+오프라인 납품 · {SALES_SOURCE.get("file", "")}'))
        elif SALES_DF is not None and not SALES_DF.empty and 'ym' in SALES_DF.columns:
            mx = str(SALES_DF['ym'].astype(str).max())
            prev = (now.replace(day=1) - _timedelta(days=1)).strftime('%Y%m')
            lag = (int(prev[:4]) * 12 + int(prev[4:6])) - (int(mx[:4]) * 12 + int(mx[4:6]))
            rec = {'label': '판매CSV', 'date_col': 'ym', 'rows': int(len(SALES_DF)), 'latest_ym': f'{mx[:4]}-{mx[4:6]}',
                   'months_behind': lag, 'status': 'ok' if lag <= 0 else ('warn' if lag == 1 else 'error'),
                   'msg': '정상' if lag <= 0 else f'{mx[:4]}-{mx[4:6]}까지 반영 · {prev[:4]}-{prev[4:6]} 판매파일 필요'}
            items.append(rec); worst = max(worst, min(2, max(0, lag)))
    except Exception:
        pass
    # 판매 SKU 매핑(2026-09-17): 최신 판매 CSV에 매핑표에 없는 SKU가 있으면 경고(신제품 → 확정품번 지정 필요)
    try:
        if SALES_DF is not None:
            um = _sales_unmapped()
            if SALES_SOURCE.get('kind') == 'api':
                # 품번 정리 화면(/sku_review)의 남은 건수 = 자사코드 없는 SKU + 매핑표↔API 품번 불일치 미결정
                um = [{'SKU': x['sku'], '판매제품명': x['sale_name']} for x in _sku_review_data()['pending']]
            _mf = f'{BASE_DIR}/SKU매핑_확정.csv'
            items.append({'label': '판매 SKU매핑', 'date_col': '', 'rows': int(len(um)), 'months_behind': None,
                          'latest_ym': _dt.fromtimestamp(os.path.getmtime(_mf)).strftime('%Y-%m-%d') if os.path.exists(_mf) else '',
                          'status': 'warn' if um else 'ok',
                          'msg': ((f'품번 확인 필요 SKU {len(um)}건 · 아래 [판매 SKU 품번 정리]에서 클릭으로 정리'
                                   if SALES_SOURCE.get('kind') == 'api' else
                                   f'미매핑 SKU {len(um)}건 · SKU매핑_미매핑_추천.csv 확인 후 SKU매핑_확정.csv에 추가') if um
                                  else '정상 · 판매 SKU 전부 매핑됨')})
            worst = max(worst, 1 if um else 0)
    except Exception:
        pass
    # 판매단가 CSV(아마란스 기준단가, 시간별 수집)
    _spf = sorted(glob.glob(f'{DATA_DIR}/*_판매단가.csv'))
    worst = max(worst, _aux('판매단가', _mtime(_spf[-1]) if _spf else None, 3, 14, '아마란스 기준단가'))
    # OneDrive 원본 3종 — 로컬(호스트)에서만 의미 있음. 클라우드(GCP)는 CSV 동기화본이라 건너뜀
    if os.path.exists('C:/Users/jgkim/OneDrive'):
        _pf = _find_price_xlsx()
        # 단가 엑셀은 2026-09-11부터 보조 원천(발주단가·매입단가가 우선) → 느슨한 기준
        worst = max(worst, _aux('단가 엑셀(보조)', _mtime(_pf), 120, 240, os.path.basename(_pf) if _pf else 'NN년 원부자재 단가.xlsx 없음'))
        _if = CSV_PATH if isinstance(CSV_PATH, str) and CSV_PATH.lower().endswith('.xlsx') else _find_inventory_xlsx()
        worst = max(worst, _aux('재고일지 원본', _mtime(_if), 40, 70, os.path.basename(_if) if _if else '재고파악 파일 없음'))
        _jf = _find_jasa_xlsx()
        worst = max(worst, _aux('자사재고 원본', _mtime(_jf), 7, 21, os.path.basename(_jf) if _jf else '자사사용 부자재 파일 없음'))

    bad = [x for x in items if x['status'] == 'error']
    warn = [x for x in items if x['status'] == 'warn']
    overall = 'error' if bad else ('warn' if warn else 'ok')
    if bad:
        summary = '데이터 이상 ' + str(len(bad)) + '건: ' + ', '.join(x['label'] for x in bad)
    elif warn:
        summary = '확인 필요 ' + str(len(warn)) + '건: ' + ', '.join(x['label'] for x in warn)
    else:
        summary = '전체 정상'
    return jsonify({'overall': overall, 'summary': summary, 'cur_ym': f'{cur_ym[:4]}-{cur_ym[4:6]}',
                    'items': items, 'checked_at': now.strftime('%Y-%m-%d %H:%M')})


@app.route('/api/data_version', methods=['GET'])
def data_version():
    """데이터 버전 — 클라이언트가 폴링해 바뀌면 자동 새로고침."""
    return jsonify({'version': _DATA_VERSION})


@app.route('/api/export_source_csv', methods=['POST', 'GET'])
def export_source_csv():
    """OneDrive 기반 재고일지(DF)·자사재고(JASA_DF)를 오늘자 CSV로 내보냄.
    → 클라우드(OneDrive 없음)가 이 CSV를 읽어 최신 재고를 반영하게 함.
    OneDrive가 있는 로컬 PC에서만 동작 (클라우드에선 skip)."""
    if not _find_inventory_xlsx():
        return jsonify({'ok': False, 'reason': 'no_onedrive_source (클라우드/비-PC 환경)'}), 400
    out = {}
    today = datetime.now().strftime('%Y%m%d')
    try:
        if DF is not None and not DF.empty:
            DF.to_csv(f'{DATA_DIR}/{today}_재고일지.csv', index=False, encoding='utf-8-sig')
            out['재고일지'] = len(DF)
        if JASA_DF is not None and not JASA_DF.empty:
            JASA_DF.to_csv(f'{DATA_DIR}/{today}_자사재고.csv', index=False, encoding='utf-8-sig')
            out['자사재고'] = len(JASA_DF)
        # 월별 마감 자동 보관 — 이번 달 파일을 매번 덮어씀 → 달이 바뀌면 이전 달
        # 파일은 더 이상 안 건드려져 "말일 최종 상태"로 자연 동결된다.
        # (별도 마감 파일을 손으로 만들 필요가 없어짐 · prune 대상 아님)
        arch = f'{DATA_DIR}/마감'
        os.makedirs(arch, exist_ok=True)
        ym = today[:6]
        if DF is not None and not DF.empty:
            DF.to_csv(f'{arch}/{ym}_재고일지_마감.csv', index=False, encoding='utf-8-sig')
        if JASA_DF is not None and not JASA_DF.empty:
            JASA_DF.to_csv(f'{arch}/{ym}_자사재고_마감.csv', index=False, encoding='utf-8-sig')
        print(f"[export] OneDrive 소스 CSV 저장: {out} (+마감보관 {ym})")
        return jsonify({'ok': True, 'exported': out, 'date': today})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:200]}), 500


if __name__ == '__main__':
    print("\n" + "="*60)
    print("  매홍 L&F 통합 재고 관리 챗봇")
    print("  http://localhost:5000 에서 접속하세요")
    print("  http://localhost:5000/upload 에서 파일 업로드")
    print("  Firebase Auth + Firestore 연동")
    print("="*60 + "\n")
    # 자동수집 스케줄러: 메모리 작은 클라우드(1GB)에선 끔 (ENABLE_AUTO_FETCH=0)
    # fetch 서브프로세스가 pandas를 여러 개 띄워 1GB VM을 초과시키기 때문.
    if os.environ.get('ENABLE_AUTO_FETCH', '1') == '1':
        _start_monday_refresh_loop()
        _start_aramanth_refresh_loop()
        _start_jasa_watcher()
        _start_inventory_watcher()
        _start_vendor_pull()
        _start_price_watcher()
        _start_notify_scheduler()
        _start_sales_drop_watcher()   # 판매 CSV 드롭폴더 (2026-09-17)
        _start_partner_sales_loop()   # 판매 일자별 API (2026-09-23)
        _start_closing_auto()         # 마감 자동 반영 (2026-09-17)
    else:
        print('[자동수집] ENABLE_AUTO_FETCH=0 → 스케줄러/워처 비활성화 (데이터는 /upload 또는 수동 리로드)')
    _port = int(os.environ.get('PORT', '5000'))
    app.run(debug=False, port=_port, host='0.0.0.0')

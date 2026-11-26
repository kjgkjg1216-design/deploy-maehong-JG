# -*- coding: utf-8 -*-
"""완제품 재고 수집 — 온라인팀 파트너 API (media.maehong.top, 2026-11-26 연결 · 구매팀_API_안내.md 7절)
GET /api/partner/purchasing/stock  (최신 재고, 행 = 품목 × 보관 장소)
→ data/YYYYMMDD_완제품재고.csv      (self_code·sku·name·location_key·location·stock_qty·unit·pack_qty·as_of)
→ data/YYYYMMDD_완제품재고장소.csv  (장소별 기준 시각·자료 없음 여부·행 수·수집 시각)
보관 장소: 자사(랙 적치분만, 실시간) · 남양1p·남양3p·송림(하루 2번, 아침·저녁 6시 반 전후). 한성은 안 씀.
unit: EA = 낱개(자사·송림) / SKU = 남양 센터의 상품 단위(묶음 상품이면 묶음 수, 낱개 환산 안 함).
행이 없으면 그 장소에 재고 없음. 아마란스 현재고(전표 기준)와는 기준이 달라 수치가 다를 수 있음.
키: .env 의 PURCHASING_API_KEY (판매 일자별 수집과 같은 키). 키는 파일·로그에 남기지 않는다.
앱(호스트 PC)이 1시간마다 fetch()를 부름. 수동 실행: python fetch_partner_stock.py
"""
import os, sys, csv, json, time
from datetime import date, datetime
import urllib.request, urllib.parse, urllib.error

BASE_DIR = os.environ.get('APP_BASE_DIR', 'C:/Users/jgkim/maehong-JG')
DATA_DIR = os.environ.get('DATA_DIR', BASE_DIR + '/data')
API = os.environ.get('PURCHASING_API_BASE', 'https://media.maehong.top')
COLS = ['self_code', 'sku', 'name', 'location_key', 'location', 'stock_qty', 'unit', 'pack_qty', 'as_of']
LCOLS = ['location_key', 'location', 'as_of', 'missing', 'rows', 'checked_at']


def _key():
    k = os.environ.get('PURCHASING_API_KEY', '').strip()
    if k:
        return k
    try:
        for line in open(os.path.join(BASE_DIR, '.env'), encoding='utf-8'):
            line = line.strip()
            if line.startswith('PURCHASING_API_KEY='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ''


def _get(path, params, key):
    url = f'{API}{path}' + ('?' + urllib.parse.urlencode(params) if params else '')
    req = urllib.request.Request(url, headers={'X-API-Key': key, 'User-Agent': 'maehong-purchasing-dashboard'})
    err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404):
                raise RuntimeError(f'HTTP {e.code}: {e.read()[:200]!r}')
            err = e
        except Exception as e:
            err = e
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f'요청 실패: {err!r:.200}')


def _write(path, cols, rows):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow({c: ('' if r.get(c) is None else r.get(c)) for c in cols})
    os.replace(tmp, path)


def fetch():
    """최신 완제품 재고를 받아 오늘 날짜 파일 2개로 저장. 반환 (행 수, 재고 파일 경로). 실패 시 예외."""
    key = _key()
    if not key:
        raise RuntimeError('PURCHASING_API_KEY 없음 (.env에 추가 필요)')
    d = _get('/api/partner/purchasing/stock', {}, key)
    rows = d.get('rows')
    if rows is None:
        raise RuntimeError('응답에 rows 없음')
    meta = d.get('meta') or {}
    miss = {(m.get('key') if isinstance(m, dict) else str(m)) for m in (meta.get('missing_locations') or [])}
    cnt = {}
    for r in rows:
        cnt[r.get('location_key')] = cnt.get(r.get('location_key'), 0) + 1
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    locs = [{'location_key': l.get('key'), 'location': l.get('name'), 'as_of': l.get('as_of'),
             'missing': 1 if l.get('key') in miss else 0, 'rows': cnt.get(l.get('key'), 0), 'checked_at': now}
            for l in (meta.get('locations') or [])]
    for k in sorted(miss - {l['location_key'] for l in locs}):
        locs.append({'location_key': k, 'location': k, 'as_of': '', 'missing': 1, 'rows': 0, 'checked_at': now})
    os.makedirs(DATA_DIR, exist_ok=True)
    day = f'{date.today():%Y%m%d}'
    path = os.path.join(DATA_DIR, f'{day}_완제품재고.csv')
    _write(os.path.join(DATA_DIR, f'{day}_완제품재고장소.csv'), LCOLS, locs)
    _write(path, COLS, rows)
    return len(rows), path


def main():
    try:
        n, path = fetch()
    except Exception as e:
        print(f'[완제품재고] 실패: {e}')
        return 1
    print(f'[완제품재고] 저장 완료: {path} ({n}행)')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())

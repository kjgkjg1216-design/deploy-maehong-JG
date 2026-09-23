# -*- coding: utf-8 -*-
"""판매(납품) 일자별 자료 수집 — 온라인팀 파트너 API (media.maehong.top, 2026-09-23 연결)
GET /api/partner/purchasing/daily?from&to  (최대 92일/회, 행 = SKU×일자×채널)
→ data/YYYYMMDD_판매일별.csv  (앱 load_sales_data가 월별로 집계해 판매기반 재고경고·수급 플래너·발주 타이밍에 사용)

키: .env 의 PURCHASING_API_KEY  (없으면 환경변수)  ※ 키는 파일/로그에 남기지 않는다.
운영 권장: 하루 1~2회, 전날 자료는 09시 이후 (앱이 09:10·14:00에 호출).
"""
import os, sys, json, time
from datetime import date, timedelta
import urllib.request, urllib.parse

sys.stdout.reconfigure(encoding='utf-8')
BASE_DIR = os.environ.get('APP_BASE_DIR', 'C:/Users/jgkim/maehong-JG')
DATA_DIR = os.environ.get('DATA_DIR', BASE_DIR + '/data')
API = os.environ.get('PURCHASING_API_BASE', 'https://media.maehong.top')
START = date(2026, 1, 1)          # API 제공 시작월
MAX_DAYS = 90                      # API 상한 92일
COLS = ['date', 'channel', 'channel_name', 'channel_type', 'sku', 'sku_type', 'name', 'category', 'self_code',
        'pack_qty', 'stack_qty', 'delivery_qty', 'pos_qty', 'stock_qty', 'supply_amount', 'unit_supply_price']


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
    url = f'{API}{path}?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'X-API-Key': key, 'User-Agent': 'maehong-purchasing-dashboard'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                raise RuntimeError(f'HTTP {e.code}: {e.read()[:200]!r}')
            err = e
        except Exception as e:
            err = e
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f'요청 실패: {err!r:.200}')


def main():
    key = _key()
    if not key:
        print('[판매일별] PURCHASING_API_KEY 없음 (.env에 추가 필요) - 건너뜀')
        return 2
    end = date.today() - timedelta(days=1)
    rows, cur = [], START
    while cur <= end:
        to = min(cur + timedelta(days=MAX_DAYS - 1), end)
        d = _get('/api/partner/purchasing/daily', {'from': cur.isoformat(), 'to': to.isoformat()}, key)
        part = d.get('rows') or []
        rows.extend(part)
        print(f'[판매일별] {cur}~{to}: {len(part)}행')
        cur = to + timedelta(days=1)
    if not rows:
        print('[판매일별] 데이터 없음 - 저장 건너뜀')
        return 1
    import csv
    path = os.path.join(DATA_DIR, f'{date.today():%Y%m%d}_판매일별.csv')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow({c: ('' if r.get(c) is None else r.get(c)) for c in COLS})
    os.replace(tmp, path)
    nd = sum(1 for r in rows if r.get('delivery_qty'))
    print(f'[판매일별] 저장 완료: {path} ({len(rows)}행, 납품 {nd}행, {START}~{end})')
    return 0


if __name__ == '__main__':
    sys.exit(main())

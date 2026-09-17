"""대시보드용 Monday 보드만 재수집 → 기존 CSV의 해당 보드 행만 교체"""
import os, sys, glob, time
import pandas as pd
from datetime import datetime
BASE_DIR = os.environ.get('APP_BASE_DIR', 'C:/Users/jgkim/maehong-JG')
DATA_DIR = os.environ.get('DATA_DIR', BASE_DIR + '/data')
sys.path.insert(0, BASE_DIR)
from fetch_monday import gql, fetch_board_items

TARGETS = ['부자재 규격', '원/부자재 발주 요청', '원료 입고 일정', '외주 생산 요청', '2026년 매출 현황']
IMPORT_RAW_BOARD = '원재료 수입 현황'


def find_board_id(name):
    page = 1
    while True:
        q = f'{{boards(limit:50, page:{page}){{id name state}}}}'
        data = gql(q)
        if not data or not data.get('boards'):
            return None
        for b in data['boards']:
            if b.get('name') == name and b.get('state') != 'deleted':
                return b['id']
        if len(data['boards']) < 50:
            return None
        page += 1
        time.sleep(0.3)


def _fmt_updates(item):
    """말풍선(updates+replies)을 fetch_monday.py와 동일 포맷으로 직렬화.
    '[YYYY-MM-DD 작성자] 본문' 을 '\\n---\\n' 로 구분, 대댓글은 '└' 들여쓰기."""
    parts = []
    for u in (item.get('updates') or []):
        body = (u.get('text_body') or '').strip()
        if not body:
            continue
        when = (u.get('created_at') or '')[:10]
        who = (u.get('creator') or {}).get('name', '')
        header = f"[{when} {who}]" if when else ''
        parts.append(f"{header} {body}".strip())
        for rep in (u.get('replies') or []):
            rbody = (rep.get('text_body') or '').strip()
            if not rbody:
                continue
            rwhen = (rep.get('created_at') or '')[:10]
            rwho = (rep.get('creator') or {}).get('name', '')
            rheader = f"[{rwhen} {rwho}]" if rwhen else ''
            parts.append(f"  └ {rheader} {rbody}".rstrip())
    return '\n---\n'.join(parts)


def fetch_import_raw_with_subitems(board_id):
    """원재료 수입 현황 — 메인아이템 + 하위아이템 동시 수집.
    말풍선(updates)은 메인아이템에만 달리므로 메인에서 수집해 하위행에도 물려줌."""
    main_rows, sub_rows = [], []
    cursor = None
    # 말풍선 포함이라 complexity 고려해 페이지 20 (import 보드는 아이템 소수)
    _upd = 'updates(limit:8){text_body created_at creator{name} replies{text_body created_at creator{name}}}'
    while True:
        if cursor:
            q = f'''{{next_items_page(cursor:"{cursor}",limit:20){{cursor items{{
                id name group{{title}} column_values{{id column{{title}}text}} {_upd}
                subitems{{id name column_values{{id column{{title}}text}}}}
            }}}}}}'''
        else:
            q = f'''{{boards(ids:{board_id}){{items_page(limit:20){{cursor items{{
                id name group{{title}} column_values{{id column{{title}}text}} {_upd}
                subitems{{id name column_values{{id column{{title}}text}}}}
            }}}}}}}}'''
        data = gql(q)
        if not data:
            break
        page_data = data.get('next_items_page', {}) if cursor else (data.get('boards') or [{}])[0].get('items_page', {})
        batch = page_data.get('items', [])
        cursor = page_data.get('cursor')
        for item in batch:
            grp = item.get('group', {}).get('title', '')
            row = {'보드ID': board_id, '보드명': IMPORT_RAW_BOARD,
                   '아이템ID': item['id'], '아이템명': item['name'], '그룹': grp}
            for cv in item.get('column_values', []):
                t = cv.get('column', {}).get('title', '')
                v = cv.get('text', '')
                if t and v:
                    row[t] = v
            upd_text = _fmt_updates(item)   # 메인아이템 말풍선
            if upd_text:
                row['업데이트'] = upd_text
            main_rows.append(row)
            for sub in item.get('subitems', []):
                srow = {
                    '보드ID': board_id, '보드명': IMPORT_RAW_BOARD + '_하위',
                    '아이템ID': sub['id'], '아이템명': sub['name'], '그룹': grp,
                    '계약번호': item['name'], '부모아이템ID': item['id'],
                    '수입원': row.get('수입원', ''),
                    '입항일': row.get('입항일', ''),
                    '상태': row.get('상태', ''),
                    '업데이트': upd_text,   # 말풍선은 부모(메인)에만 → 하위행에 물려줌
                }
                for cv in sub.get('column_values', []):
                    t = cv.get('column', {}).get('title', '')
                    v = cv.get('text', '')
                    if t and v:
                        srow[t] = v
                sub_rows.append(srow)
        if not cursor or not batch:
            break
        time.sleep(0.3)
    return main_rows, sub_rows


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    today = datetime.now().strftime('%Y%m%d')
    path = f'{DATA_DIR}/{today}_monday.csv'
    if not os.path.exists(path):
        files = sorted(glob.glob(f'{DATA_DIR}/*_monday.csv'), reverse=True)
        path = files[0] if files else None
    if not path:
        print("Monday CSV 없음 — fetch_monday.py 전체 실행 필요"); return
    print(f"[기존 CSV] {path}")
    df = pd.read_csv(path, encoding='utf-8-sig', dtype=str).fillna('')
    print(f"  {len(df)}행 / {df['보드명'].nunique()}보드")

    new_rows = []
    fetched_boards = set()   # 실제로 새 데이터를 받은 보드만 교체 (0건이면 기존 행 보존)
    for tname in TARGETS:
        print(f"\n[수집] {tname}")
        bid = find_board_id(tname)
        if not bid:
            print(f"  보드 못 찾음 — 기존 행 유지(스킵)"); continue
        print(f"  ID: {bid}")
        items = fetch_board_items(bid, tname)
        print(f"  {len(items)}건 수집")
        if items:
            new_rows.extend(items)
            fetched_boards.add(tname)
        else:
            print(f"  ⚠ 0건 — 기존 '{tname}' 행을 지우지 않고 보존")
        time.sleep(0.5)

    # 원재료 수입 현황 (하위아이템 포함)
    print(f"\n[수집] {IMPORT_RAW_BOARD} (하위아이템 포함)")
    imp_bid = find_board_id(IMPORT_RAW_BOARD)
    if imp_bid:
        print(f"  ID: {imp_bid}")
        main_items, sub_items = fetch_import_raw_with_subitems(imp_bid)
        print(f"  {len(main_items)}건 메인 + {len(sub_items)}건 하위아이템")
        if main_items or sub_items:
            new_rows.extend(main_items)
            new_rows.extend(sub_items)
            fetched_boards.add(IMPORT_RAW_BOARD)
            fetched_boards.add(IMPORT_RAW_BOARD + '_하위')
        else:
            print(f"  ⚠ 0건 — 기존 '{IMPORT_RAW_BOARD}' 행 보존")
    else:
        print(f"  보드 못 찾음 — 기존 행 유지(스킵)")

    if not new_rows:
        print("새 데이터 없음 — 종료(기존 CSV 보존)"); return

    # 새 데이터를 받은 보드만 교체 (0건/실패 보드는 기존 행 그대로 유지)
    before = len(df)
    remove_boards = fetched_boards
    df = df[~df['보드명'].isin(remove_boards)].copy()
    print(f"\n[교체] 대상보드 {sorted(remove_boards)} | 기존 {before - len(df)}행 제거 → 새 {len(new_rows)}행 추가")

    out = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True, sort=False).fillna('')
    final_path = f'{DATA_DIR}/{today}_monday.csv'
    from _safe_csv import safe_to_csv
    safe_to_csv(out, final_path, label='monday-dashboard')
    print(f"[저장 시도] {final_path} ({len(out)}행, {len(out.columns)}열)")


if __name__ == '__main__':
    main()

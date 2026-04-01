"""
Monday.com 전체 보드 데이터 수집 → CSV 저장
"""
import requests, json, os, sys, time
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

load_dotenv('C:/Users/jgkim/maehong-JG/.env')

API_KEY = os.getenv('MONDAY_API_KEy')
URL = 'https://api.monday.com/v2'
HEADERS = {
    'Authorization': API_KEY,
    'Content-Type': 'application/json',
    'API-Version': '2024-10',
}


def gql(query, retries=3):
    for i in range(retries):
        try:
            r = requests.post(URL, json={'query': query}, headers=HEADERS, timeout=30)
            data = r.json()
            if 'errors' in data:
                print(f"  GQL 에러: {data['errors'][0].get('message','')[:80]}")
                if 'complexity' in str(data['errors']):
                    time.sleep(5)
                    continue
                return None
            return data.get('data')
        except Exception as e:
            print(f"  요청 실패({i+1}): {e}")
            time.sleep(2)
    return None


def fetch_all_boards():
    """전체 보드 목록 수집"""
    all_boards = []
    page = 1
    while True:
        q = f'{{boards(limit:50, page:{page}){{id name board_kind state items_count}}}}'
        data = gql(q)
        if not data or not data.get('boards'):
            break
        boards = data['boards']
        all_boards.extend(boards)
        print(f"  보드 페이지 {page}: {len(boards)}개 (누적 {len(all_boards)})")
        if len(boards) < 50:
            break
        page += 1
        time.sleep(0.5)
    return all_boards


def fetch_board_items(board_id, board_name):
    """특정 보드의 아이템+컬럼값 수집"""
    items = []
    cursor = None
    page_count = 0

    while True:
        if cursor:
            q = f'''{{
                next_items_page(cursor: "{cursor}", limit: 100) {{
                    cursor
                    items {{
                        id name created_at updated_at
                        group {{ title }}
                        column_values {{ id column {{ title }} text value }}
                    }}
                }}
            }}'''
        else:
            q = f'''{{
                boards(ids: {board_id}) {{
                    items_page(limit: 100) {{
                        cursor
                        items {{
                            id name created_at updated_at
                            group {{ title }}
                            column_values {{ id column {{ title }} text value }}
                        }}
                    }}
                }}
            }}'''

        data = gql(q)
        if not data:
            break

        if cursor:
            page_data = data.get('next_items_page', {})
        else:
            boards = data.get('boards', [{}])
            page_data = boards[0].get('items_page', {}) if boards else {}

        batch = page_data.get('items', [])
        cursor = page_data.get('cursor')
        page_count += 1

        for item in batch:
            row = {
                '보드ID': board_id,
                '보드명': board_name,
                '아이템ID': item['id'],
                '아이템명': item['name'],
                '그룹': item.get('group', {}).get('title', ''),
                '생성일': item.get('created_at', '')[:10],
                '수정일': item.get('updated_at', '')[:10],
            }
            for cv in item.get('column_values', []):
                col_name = cv.get('column', {}).get('title', cv.get('id', ''))
                val = cv.get('text', '')
                if val:
                    row[col_name] = val
            items.append(row)

        if not cursor or not batch:
            break
        time.sleep(0.3)

    return items


def main():
    today = datetime.now().strftime('%Y%m%d')
    output_path = f'C:/Users/jgkim/maehong-JG/data/{today}_monday.csv'

    print("=" * 60)
    print("  Monday.com 전체 데이터 수집")
    print("=" * 60)

    # 1) 전체 보드 목록
    print("\n[1단계] 보드 목록 수집...")
    boards = fetch_all_boards()
    active = [b for b in boards if b.get('state') != 'deleted' and b.get('items_count', 0) > 0]
    print(f"  전체 {len(boards)}개 → 아이템 있는 보드 {len(active)}개")

    # 2) 각 보드의 아이템 수집 (중간 저장 포함)
    print(f"\n[2단계] 아이템 수집 ({len(active)}개 보드)...")
    all_items = []
    for i, b in enumerate(active):
        bid = b['id']
        bname = b['name']
        cnt = b.get('items_count', 0)
        safe_name = bname.encode('ascii', 'replace').decode('ascii')[:30]
        sys.stdout.buffer.write(f"  [{i+1}/{len(active)}] {safe_name} ({cnt})...".encode('utf-8', 'replace'))
        sys.stdout.buffer.flush()
        try:
            items = fetch_board_items(bid, bname)
            all_items.extend(items)
            sys.stdout.buffer.write(f" -> {len(items)} (total {len(all_items)})\n".encode('utf-8'))
        except Exception as e:
            sys.stdout.buffer.write(f" ERROR: {str(e)[:50]}\n".encode('utf-8'))
        sys.stdout.buffer.flush()

        # 200건마다 중간 저장
        if (i + 1) % 200 == 0 and all_items:
            tmp_df = pd.DataFrame(all_items)
            tmp_df.to_csv(output_path, index=False, encoding='utf-8-sig')
            sys.stdout.buffer.write(f"  [중간저장] {len(all_items)}건 저장됨\n".encode('utf-8'))

        time.sleep(0.2)

    # 3) 최종 CSV 저장
    if all_items:
        df = pd.DataFrame(all_items)
        df.to_csv(output_path, index=False, encoding='utf-8-sig')
        print(f"\n[저장 완료] {output_path}")
        print(f"  총 {len(df)}건, {len(df.columns)}열")
        print(f"  보드: {df['보드명'].nunique()}개")
    else:
        print("\n수집된 데이터 없음")

    print("=" * 60)


if __name__ == '__main__':
    main()

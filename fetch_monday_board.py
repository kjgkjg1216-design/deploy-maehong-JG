"""특정 보드 1개만 재수집 → 기존 Monday CSV의 해당 보드 행만 교체"""
import os, sys, glob, time
import pandas as pd
from datetime import datetime
sys.path.insert(0, 'c:/Users/jgkim/maehong-JG')
from fetch_monday import gql, fetch_board_items, BOARDS_WITH_UPDATES

TARGET_BOARD = '외주 생산 요청'

def find_board_id(name):
    page = 1
    while True:
        q = f'{{boards(limit:50, page:{page}){{id name state items_count}}}}'
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


def main():
    if TARGET_BOARD not in BOARDS_WITH_UPDATES:
        print(f"경고: {TARGET_BOARD}가 BOARDS_WITH_UPDATES에 없음")
    sys.stdout.reconfigure(encoding='utf-8')
    print(f"[1] 보드 ID 찾기: {TARGET_BOARD}")
    bid = find_board_id(TARGET_BOARD)
    if not bid:
        print("보드를 찾을 수 없음"); return
    print(f"  ID: {bid}")

    print(f"[2] 보드 아이템 재수집 (replies 포함)...")
    items = fetch_board_items(bid, TARGET_BOARD)
    print(f"  {len(items)}건 수집 완료")

    # 기존 CSV 로드
    today = datetime.now().strftime('%Y%m%d')
    path = f'C:/Users/jgkim/maehong-JG/data/{today}_monday.csv'
    if not os.path.exists(path):
        files = sorted(glob.glob('C:/Users/jgkim/maehong-JG/data/*_monday.csv'), reverse=True)
        path = files[0] if files else None
    if not path:
        print("Monday CSV 없음")
        return
    print(f"[3] 기존 CSV 로드: {path}")
    df = pd.read_csv(path, encoding='utf-8-sig', dtype=str).fillna('')
    before = len(df)

    # 해당 보드 제거
    df = df[df['보드명'] != TARGET_BOARD].copy()
    removed = before - len(df)
    print(f"  기존 {TARGET_BOARD} 행 {removed}건 제거")

    # 새 행 추가 (신규 컬럼 자동 병합)
    new_df = pd.DataFrame(items)
    out = pd.concat([df, new_df], ignore_index=True, sort=False).fillna('')
    print(f"  최종 {len(out)}행 ({len(out.columns)}열)")

    # 오늘 날짜 CSV로 저장 (원본 덮어쓰기)
    out.to_csv(path, index=False, encoding='utf-8-sig')
    print(f"[저장 완료] {path}")


if __name__ == '__main__':
    main()

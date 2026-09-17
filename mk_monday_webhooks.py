# -*- coding: utf-8 -*-
"""Monday 대상 보드에 웹훅 등록 → 변경 시 클라우드로 즉시 알림.
중복 생성 방지(기존 웹훅 확인). 재실행 안전."""
import sys, time
sys.path.insert(0, 'C:/Users/jgkim/maehong-JG')
sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv
load_dotenv('C:/Users/jgkim/maehong-JG/.env')
from fetch_monday import gql

WEBHOOK_URL = 'https://8.235.41.127.sslip.io/api/monday_webhook'
TARGETS = ['부자재 규격', '원/부자재 발주 요청', '원료 입고 일정',
           '외주 생산 요청', '2026년 매출 현황', '원재료 수입 현황']
EVENTS = ['change_column_value', 'create_item']


def find_board_id(name):
    page = 1
    while True:
        data = gql(f'{{boards(limit:50, page:{page}){{id name state}}}}')
        if not data or not data.get('boards'):
            return None
        for b in data['boards']:
            if b.get('name') == name and b.get('state') != 'deleted':
                return b['id']
        if len(data['boards']) < 50:
            return None
        page += 1
        time.sleep(0.3)


def existing_events(board_id):
    data = gql(f'{{webhooks(board_id:{board_id}){{id event config}}}}')
    evs = set()
    for w in (data or {}).get('webhooks', []) or []:
        if WEBHOOK_URL in str(w.get('config', '')):
            evs.add(w.get('event'))
    return evs


created = skipped = failed = 0
for name in TARGETS:
    bid = find_board_id(name)
    if not bid:
        print(f"[보드 못찾음] {name}")
        continue
    have = existing_events(bid)
    for ev in EVENTS:
        if ev in have:
            print(f"  [이미있음] {name} / {ev}")
            skipped += 1
            continue
        m = f'mutation{{create_webhook(board_id:{bid}, url:"{WEBHOOK_URL}", event:{ev}){{id board_id}}}}'
        res = gql(m)
        if res and res.get('create_webhook'):
            print(f"  [생성] {name} / {ev} → id {res['create_webhook']['id']}")
            created += 1
        else:
            print(f"  [실패] {name} / {ev}")
            failed += 1
        time.sleep(0.5)

print(f"\n완료: 생성 {created} · 기존 {skipped} · 실패 {failed}")

"""Fly 볼륨 시드용 — data/ 에서 타입별 '최신' CSV만 _seed_data/ 로 복사.
1.8GB 전체가 아니라 앱이 실제로 읽는 최신 파일만 추려 업로드 부담을 줄인다.
사용:  python seed_latest.py
이후:  fly ssh sftp shell  →  put _seed_data/<파일> /data/<파일>  (또는 아래 절차서 참고)
"""
import os, glob, shutil

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')
OUT  = os.path.join(BASE, '_seed_data')

# 앱이 sorted(reverse=True)[0] 로 읽는 타입들
PATTERNS = [
    '*_재고일지.csv', '*_단가.csv', '*_부자재규격.csv', '*_자사재고.csv',
    '*_발주정보.csv', '*_외주발주정보.csv', '*_생산실적.csv', '*_출하정보.csv',
    '*_출고정보.csv', '*_BOM.csv', '*_입고정보.csv', '*_현재고.csv',
    '*_생산지시.csv', '*_monday.csv', '*_판매단가.csv', '*_판매일별.csv',
]

def main():
    os.makedirs(OUT, exist_ok=True)
    # 이전 실행 잔여 파일 제거 — 최신 1개씩만 남겨 업로드 md5 비교가 VM과 일치하도록
    # (안 비우면 옛 파일이 쌓여 VM 정리분이 매번 재업로드됨)
    for old in glob.glob(os.path.join(OUT, '*.csv')):
        try:
            os.remove(old)
        except OSError:
            pass
    total = 0
    for pat in PATTERNS:
        files = sorted(glob.glob(os.path.join(DATA, pat)), reverse=True)
        if not files:
            print(f'  (없음) {pat}')
            continue
        src = files[0]
        dst = os.path.join(OUT, os.path.basename(src))
        shutil.copy2(src, dst)
        sz = os.path.getsize(src) / 1024 / 1024
        total += sz
        print(f'  복사 {os.path.basename(src)}  ({sz:.1f}MB)')
    print(f'\n[완료] {OUT} 에 최신 파일 모음 ({total:.1f}MB)')

if __name__ == '__main__':
    main()

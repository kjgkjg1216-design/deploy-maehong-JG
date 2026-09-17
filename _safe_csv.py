"""원자적 CSV 저장 헬퍼.
.tmp로 먼저 쓰고, 직전 정상 파일의 50% 미만이면 거부 → os.replace로 교체.
fetch_all/fetch_bom/fetch_monday 등이 부분만 저장된 채 죽는 사고 방지."""
import os
import glob
import pandas as pd


def safe_to_csv(df, final_path, label='', min_ratio=0.5, encoding='utf-8-sig'):
    """df를 final_path에 원자적으로 저장.
    - .tmp로 우선 저장
    - 동일 베이스 이름의 직전 파일 대비 크기 < min_ratio이면 거부 (sanity check)
    - 통과하면 os.replace로 final_path로 교체
    - 실패하거나 거부되면 final_path는 손대지 않음 (= 직전 데이터 유지)
    """
    tmp_path = final_path + '.tmp'
    df.to_csv(tmp_path, index=False, encoding=encoding)
    try:
        new_size = os.path.getsize(tmp_path)
    except OSError:
        print(f"  [safe_csv] {label} tmp 크기 조회 실패 — 저장 보류")
        return False

    # 직전 정상 파일 찾기: 동일 베이스(_NAME.csv) 중 final_path가 아닌 최신
    dirpath, fname = os.path.split(final_path)
    # 베이스명: YYYYMMDD_NAME.csv → NAME 추출
    parts = fname.split('_', 1)
    base = parts[1] if len(parts) > 1 else fname
    prev_files = sorted(glob.glob(os.path.join(dirpath, f'*_{base}')), reverse=True)
    prev_files = [p for p in prev_files if os.path.normpath(p) != os.path.normpath(final_path)]
    if prev_files:
        try:
            prev_size = os.path.getsize(prev_files[0])
        except OSError:
            prev_size = 0
        if prev_size > 0 and new_size < prev_size * min_ratio:
            print(f"  [safe_csv] {label} 거부: 새 파일 {new_size:,}B < 직전 {prev_size:,}B × {min_ratio} ({prev_files[0]})")
            print(f"  [safe_csv]   원본 파일 유지, .tmp 보존: {tmp_path}")
            return False

    os.replace(tmp_path, final_path)
    print(f"  [safe_csv] {label} 저장 완료: {final_path} ({new_size:,}B)")
    return True

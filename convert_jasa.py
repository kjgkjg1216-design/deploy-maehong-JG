"""
자사사용 부자재 재고 변환 스크립트
입력: 자사사용 부자재_REV.260224_지우철_1.xlsx  (시트: 생산러닝 부자재, header=row2)
출력: data/YYYYMMDD_자사재고.csv (UTF-8-BOM)
"""
import os
import glob
import pandas as pd
from datetime import date

XLSX_PATH = 'C:/Users/jgkim/maehong-JG/자사사용 부자재_REV.260224_지우철_1.xlsx'
OUT_DIR   = 'C:/Users/jgkim/maehong-JG/data'
TODAY     = date.today().strftime('%Y%m%d')
OUT_PATH  = os.path.join(OUT_DIR, f'{TODAY}_자사재고.csv')

# ── 기존 파일 삭제 ──
for old in glob.glob(os.path.join(OUT_DIR, '*_자사재고.csv')):
    os.remove(old)
    print(f'[삭제] {old}')

# ── 읽기 (헤더 row2 = index 2) ──
xls = pd.ExcelFile(XLSX_PATH, engine='openpyxl')
df = pd.read_excel(xls, sheet_name=0, header=2, dtype=str)

# ── 정리 ──
df = df.fillna('')
df = df.drop(columns=[c for c in df.columns if str(c).startswith('Unnamed')], errors='ignore')
df = df[df['품번'].str.strip() != '']   # 품번 없는 행 제거

# 총재고 컬럼 중복 처리 (총재고 / 총재고.1)
if '총재고.1' in df.columns:
    df = df.rename(columns={'총재고.1': '총재고(최종)'})

# 공백 제거
for col in df.columns:
    if df[col].dtype == object:
        df[col] = df[col].str.strip()

os.makedirs(OUT_DIR, exist_ok=True)
df.to_csv(OUT_PATH, index=False, encoding='utf-8-sig')

print(f'[완료] {OUT_PATH}  ({len(df)}행 × {len(df.columns)}열)')
print(f'  컬럼: {list(df.columns)}')
print(f'  구분1 종류: {sorted(df["구분1"].unique())}')
print(f'  업체 종류: {sorted(df["업체"].unique())}')

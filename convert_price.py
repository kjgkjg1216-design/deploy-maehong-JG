"""
26년 원부자재 단가.xlsx → CSV 변환
- 부자재단가 시트 (시트0): 거래처, 품번, 품명, 규격, 최신단가
- 완제품단가 시트 (시트1): 거래처, 품번, 품명, 규격, 최신단가
- 가장 우측 년도 컬럼을 최신단가로 사용
"""
import pandas as pd
import glob
import os
import re
from datetime import datetime

def find_latest_year_col(df: pd.DataFrame) -> str:
    """헤더에서 년도가 포함된 컬럼 중 가장 오른쪽(최신) 컬럼명 반환
    Excel의 年(U+5E74) 및 한국어 년(U+B144) 모두 지원"""
    year_pattern = re.compile(r'\d{2}[\u5e74\ub144]')  # 年 또는 년
    year_cols = [c for c in df.columns if year_pattern.search(str(c))]
    if not year_cols:
        return None
    return year_cols[-1]  # 가장 우측 = 최신

def convert_price_excel():
    files = glob.glob('C:/Users/jgkim/maehong-JG/26년*.xlsx')
    if not files:
        raise FileNotFoundError("26년 원부자재 단가.xlsx 파일을 찾을 수 없습니다.")

    filepath = files[0]
    print(f"단가 파일: {filepath}")

    data_dir = 'C:/Users/jgkim/maehong-JG/data'
    os.makedirs(data_dir, exist_ok=True)

    all_rows = []

    # ─── 시트0: 부자재단가 ───────────────────────────────
    df0 = pd.read_excel(filepath, sheet_name=0, header=0, engine='openpyxl', dtype=str)
    df0 = df0.fillna('')

    latest_col0 = find_latest_year_col(df0)
    print(f"[부자재단가] 최신단가 컬럼: {latest_col0}")
    print(f"[부자재단가] 컬럼목록: {list(df0.columns)}")

    # 품번 컬럼 = index 1
    col_품번   = df0.columns[1]
    col_품명   = df0.columns[2]
    col_거래처 = df0.columns[0]
    col_규격   = df0.columns[3]

    for _, row in df0.iterrows():
        품번 = str(row[col_품번]).strip()
        품명 = str(row[col_품명]).strip()
        단가_str = str(row.get(latest_col0, '')).strip()
        if not 품번 or 품번 in ('nan', ''):
            continue
        try:
            단가 = float(단가_str) if 단가_str not in ('', 'nan') else None
        except ValueError:
            단가 = None

        all_rows.append({
            '시트종류': '부자재',
            '거래처': str(row[col_거래처]).strip(),
            '품번': 품번,
            '품명': 품명,
            '규격': str(row[col_규격]).strip(),
            '최신단가': 단가,
            '기준년월': latest_col0,
        })

    # ─── 시트1: 완제품단가 ───────────────────────────────
    df1 = pd.read_excel(filepath, sheet_name=1, header=0, engine='openpyxl', dtype=str)
    df1 = df1.fillna('')

    latest_col1 = find_latest_year_col(df1)
    print(f"[완제품단가] 최신단가 컬럼: {latest_col1}")
    print(f"[완제품단가] 컬럼목록: {list(df1.columns)}")

    # 품번 컬럼 = index 2 (시트1은 거래처, 품목군, 품번, 품명 순)
    col_품번1   = df1.columns[2]
    col_품명1   = df1.columns[3]
    col_거래처1 = df1.columns[0]
    col_규격1   = df1.columns[4]

    for _, row in df1.iterrows():
        품번 = str(row[col_품번1]).strip()
        품명 = str(row[col_품명1]).strip()
        단가_str = str(row.get(latest_col1, '')).strip()
        if not 품번 or 품번 in ('nan', ''):
            continue
        try:
            단가 = float(단가_str) if 단가_str not in ('', 'nan') else None
        except ValueError:
            단가 = None

        all_rows.append({
            '시트종류': '완제품',
            '거래처': str(row[col_거래처1]).strip(),
            '품번': 품번,
            '품명': 품명,
            '규격': str(row[col_규격1]).strip(),
            '최신단가': 단가,
            '기준년월': latest_col1,
        })

    price_df = pd.DataFrame(all_rows)
    price_df = price_df[price_df['품번'].str.strip() != '']
    price_df = price_df.drop_duplicates(subset=['품번'], keep='last')

    today = datetime.now().strftime('%Y%m%d')
    output_path = os.path.join(data_dir, f'{today}_단가.csv')
    price_df.to_csv(output_path, index=False, encoding='utf-8-sig')

    print(f"\nCSV 저장 완료: {output_path}")
    print(f"총 {len(price_df)}개 품번 (부자재+완제품)")
    print(f"단가 있는 항목: {price_df['최신단가'].notna().sum()}개")
    return output_path, price_df

if __name__ == '__main__':
    convert_price_excel()

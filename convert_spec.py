"""
부자재 규격.xlsx → CSV 변환
시트: 부자재 규격 (헤더: Row 3)
컬럼: Name(외주업체명), 납품채널, 품번, 납품처, 품명, 규격(사이즈), 재질, MOQ, 단가(원), 중량(g), 링크, 날짜
"""
import pandas as pd
import os
from datetime import datetime


def convert_spec_excel():
    filepath = 'C:/Users/jgkim/maehong-JG/부자재 규격.xlsx'
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {filepath}")

    print(f"부자재 규격 파일: {filepath}")

    data_dir = 'C:/Users/jgkim/maehong-JG/data'
    os.makedirs(data_dir, exist_ok=True)

    # header=2 → 0-indexed Row2 = 실제 3번째 행이 헤더
    df = pd.read_excel(filepath, sheet_name='부자재 규격', header=2, engine='openpyxl', dtype=str)
    df = df.fillna('')

    print(f"[부자재 규격] 원본 컬럼: {list(df.columns)}")
    print(f"[부자재 규격] 원본 행수: {len(df)}")

    # 컬럼명 정리 (앞뒤 공백 제거)
    df.columns = [c.strip() for c in df.columns]

    # Name 컬럼이 있는 행만 유지 (빈 행 제거)
    name_col = df.columns[0]  # 'Name' 또는 첫 번째 컬럼
    df = df[df[name_col].str.strip() != '']
    df = df[df[name_col].str.lower() != 'name']  # 헤더 중복 행 제거

    # 컬럼 표준화: 실제 컬럼명 → 표준 컬럼명
    col_map = {}
    for c in df.columns:
        c_lower = c.lower().strip()
        if c_lower == 'name':
            col_map[c] = '외주업체명'
        elif '납품채널' in c:
            col_map[c] = '납품채널'
        elif '품번' in c:
            col_map[c] = '품번'
        elif '납품처' in c:
            col_map[c] = '납품처'
        elif '품명' in c:
            col_map[c] = '품명'
        elif '규격' in c or '사이즈' in c:
            col_map[c] = '규격(사이즈)'
        elif '재질' in c:
            col_map[c] = '재질'
        elif 'moq' in c_lower:
            col_map[c] = 'MOQ'
        elif '단가' in c:
            col_map[c] = '단가(원)'
        elif '중량' in c:
            col_map[c] = '중량(g)'
        elif '링크' in c:
            col_map[c] = '링크'
        elif '날짜' in c:
            col_map[c] = '날짜'
        else:
            col_map[c] = c

    df = df.rename(columns=col_map)

    # 필수 컬럼만 유지
    keep_cols = ['외주업체명', '납품채널', '품번', '납품처', '품명', '규격(사이즈)', '재질', 'MOQ', '단가(원)', '중량(g)', '날짜']
    keep_cols = [c for c in keep_cols if c in df.columns]
    df = df[keep_cols]

    # 품번 있는 행만 (외주업체명만 있고 품번 없는 소계 행 제거)
    if '품번' in df.columns:
        df = df[df['품번'].str.strip() != '']

    today = datetime.now().strftime('%Y%m%d')
    output_path = os.path.join(data_dir, f'{today}_부자재규격.csv')
    df.to_csv(output_path, index=False, encoding='utf-8-sig')

    print(f"\nCSV 저장 완료: {output_path}")
    print(f"총 {len(df)}개 항목")
    print(f"외주업체명 목록: {df['외주업체명'].replace('', pd.NA).dropna().unique().tolist() if '외주업체명' in df.columns else 'N/A'}")
    return output_path, df


if __name__ == '__main__':
    convert_spec_excel()

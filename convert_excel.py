"""
엑셀 파일을 CSV로 변환 - daily _ 완제품 재고일지 시트
"""
import pandas as pd
import glob
import os
from datetime import datetime

def convert_excel_to_csv():
    # 엑셀 파일 찾기 (재고파악 최종본)
    files = glob.glob('C:/Users/jgkim/maehong-JG/원자재부자재 재고파악*최종본*.xlsx')
    if not files:
        files = glob.glob('C:/Users/jgkim/maehong-JG/원자재부자재*.xlsx')
    if not files:
        raise FileNotFoundError("원자재부자재 재고파악 엑셀 파일을 찾을 수 없습니다.")

    filepath = files[0]
    print(f"엑셀 파일: {filepath}")

    # data 폴더 생성
    data_dir = 'C:/Users/jgkim/maehong-JG/data'
    os.makedirs(data_dir, exist_ok=True)

    xls = pd.ExcelFile(filepath, engine='openpyxl')

    # 시트 3번 = daily _ 완제품 재고일지
    sheet_name = xls.sheet_names[3]
    print(f"시트명: {sheet_name}")

    # Row 5(0-indexed)가 헤더행
    df = pd.read_excel(filepath, sheet_name=sheet_name, header=5, engine='openpyxl')

    # 불필요한 unnamed 컬럼 제거 (빈 컬럼)
    df = df.loc[:, ~df.columns.str.startswith('Unnamed')]

    # 완전히 빈 행 제거
    df = df.dropna(how='all')

    # 품목처 컬럼이 있는 행만 유지 (헤더 반복 제거)
    first_col = df.columns[0]
    df = df[df[first_col].notna()]

    # 오늘 날짜로 파일명 생성
    today = datetime.now().strftime('%Y%m%d')
    output_path = os.path.join(data_dir, f'{today}_재고일지.csv')

    # UTF-8-BOM으로 저장 (Excel에서 한글 깨짐 방지)
    df.to_csv(output_path, index=False, encoding='utf-8-sig')

    print(f"CSV 저장 완료: {output_path}")
    print(f"총 {len(df)}행, {len(df.columns)}열")
    print(f"\n컬럼 목록:")
    for col in df.columns:
        print(f"  - {col}")

    return output_path, df

if __name__ == '__main__':
    output_path, df = convert_excel_to_csv()
    print(f"\n첫 3행 미리보기:")
    print(df.head(3).to_string())

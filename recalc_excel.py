# -*- coding: utf-8 -*-
"""Excel COM으로 워크북 전체 재계산 후 저장 (별도 프로세스용). 사용: python recalc_excel.py <xlsx>
openpyxl로 쓴 파일은 수식 캐시값이 없어 pandas/앱이 빈 값을 읽으므로, 마감 생성 후 반드시 실행."""
import sys, os
def main(path):
    import pythoncom, win32com.client as w32
    pythoncom.CoInitialize()
    xl = w32.DispatchEx('Excel.Application')
    xl.Visible = False; xl.DisplayAlerts = False
    try:
        wb = xl.Workbooks.Open(os.path.abspath(path), 0, False)   # UpdateLinks=0, ReadOnly=False
        xl.CalculateFullRebuild()
        wb.Save(); wb.Close(True)
        print('RECALC_OK')
    finally:
        xl.Quit()
if __name__ == '__main__':
    main(sys.argv[1])

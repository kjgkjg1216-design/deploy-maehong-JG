# -*- coding: utf-8 -*-
"""마감 워크북 생성 — Excel COM 전용 (별도 프로세스).
openpyxl로 저장하면 Excel이 파일 손상으로 판단해 열지 못하므로(조건부서식·대용량 시트),
원본을 Excel로 읽기전용 열기 → 셀 기입 → 전체 재계산 → 다른 이름으로 저장. 원형 100% 보존 + 수식 캐시값 생성.
사용: python closing_excel.py <spec.json>
spec: {baseline, target, mon, days:[{vendor,code,spec,day,val|null}], ins:[{vendor,code,date,qty,unit}]}
출력: JSON 한 줄 {ok, written, missing, inbound_rows, target}"""
import sys, os, json, datetime
sys.stdout.reconfigure(encoding='utf-8')   # app.py가 utf-8로 읽음 (기본 cp949면 한글 경로/품번이 깨짐)
sys.stderr.reconfigure(encoding='utf-8')

XL_UP = -4162
XL_OPENXML = 51


def main(spec_path):
    spec = json.load(open(spec_path, encoding='utf-8'))
    import pythoncom, win32com.client as w32
    pythoncom.CoInitialize()
    xl = w32.DispatchEx('Excel.Application')
    xl.Visible = False; xl.DisplayAlerts = False; xl.ScreenUpdating = False
    out = {'ok': False, 'written': 0, 'missing': [], 'inbound_rows': 0, 'target': spec['target']}
    try:
        inplace = bool(spec.get('inplace'))
        # inplace: 임시 작업본을 쓰기 모드로 열어 Save (app.py가 작업본을 실제 마감 경로로 복사). 아니면 읽기전용 열고 SaveAs.
        wb = xl.Workbooks.Open(os.path.normpath(spec['baseline']), 0, not inplace)
        ws = wb.Worksheets('daily _ 완제품 재고일지')
        hdr = str(ws.Cells(6, 11).Value or '')
        if not hdr.startswith(f"{spec['mon']}월"):
            out['error'] = f'기준 파일 일별 열 헤더가 {spec["mon"]}월이 아님: {hdr}'
            wb.Close(False); print(json.dumps(out, ensure_ascii=False)); return
        last = ws.Cells(ws.Rows.Count, 2).End(XL_UP).Row
        vals = ws.Range(ws.Cells(7, 1), ws.Cells(max(last, 7), 5)).Value
        if not isinstance(vals[0], (tuple, list)):
            vals = (vals,)
        rowmap = {}
        for i, v in enumerate(vals):
            code = str(v[1] or '').strip().upper(); sp = str(v[2] or '').strip(); vendor = str(v[4] or '').strip()
            if code and (vendor, code, sp) not in rowmap:
                rowmap[(vendor, code, sp)] = 7 + i
        for d in spec.get('days', []):
            r = rowmap.get((d['vendor'], d['code'].upper(), d['spec']))
            if not r:
                out['missing'].append(f"{d['vendor']}/{d['code']}/{d['spec']}"); continue
            c = ws.Cells(r, 10 + int(d['day']))
            if d.get('val') is None:
                c.ClearContents()
            else:
                c.Value = d['val']
            out['written'] += 1
        wi = wb.Worksheets('daily_원부자재 입고일지')
        li = wi.Cells(wi.Rows.Count, 2).End(XL_UP).Row
        if li < 2:
            li = 2
        # 기존 마감에 덧붙일 때는 이미 같은 (일자, 품번, 수량, 거래처) 행이 있으면 건너뜀 (재생성 시 중복 방지)
        existing = set()
        if spec.get('skip_existing_inbound') and li >= 3:
            vals = wi.Range(wi.Cells(3, 2), wi.Cells(li, 9)).Value
            if not isinstance(vals[0], (tuple, list)):
                vals = (vals,)
            for v in vals:
                try:
                    d = v[0]
                    dkey = d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)[:10]
                except Exception:
                    dkey = str(v[0])[:10]
                existing.add((dkey, str(v[1] or '').strip().upper(), float(v[5] or 0), str(v[7] or '').strip()))
        for e in spec.get('ins', []):
            if (e['date'], e['code'].upper(), float(e['qty']), e['vendor']) in existing:
                continue
            li += 1
            y, m, dd = [int(x) for x in e['date'].split('-')]
            # datetime을 그대로 넘기면 pywin32가 UTC로 변환해 KST 기준 하루가 밀림(9/2 → 9/1 15:00) → Excel 일련번호로 기입
            wi.Cells(li, 2).Value = float((datetime.date(y, m, dd) - datetime.date(1899, 12, 30)).days)
            wi.Cells(li, 2).NumberFormat = 'yyyy-mm-dd'
            wi.Cells(li, 3).Value = e['code']
            if not str(wi.Cells(li, 4).Formula or ''):
                wi.Cells(li, 4).Formula = f"=VLOOKUP(C{li},'daily _ 완제품 재고일지'!B:D,3,0)"
            if not str(wi.Cells(li, 5).Formula or ''):
                wi.Cells(li, 5).Formula = f"=VLOOKUP(C{li},'daily _ 완제품 재고일지'!B:D,2,0)"
            wi.Cells(li, 7).Value = e['qty']
            wi.Cells(li, 8).Value = e.get('unit', '')
            wi.Cells(li, 9).Value = e['vendor']
            if e.get('supplier'):
                wi.Cells(li, 6).Value = e['supplier']    # F 원/부자재 업체명
            if e.get('dest'):
                wi.Cells(li, 10).Value = e['dest']       # J 출고처
            if e.get('exp'):
                try:
                    ey, em, ed = [int(x) for x in e['exp'].split('-')]
                    wi.Cells(li, 11).Value = float((datetime.date(ey, em, ed) - datetime.date(1899, 12, 30)).days)
                    wi.Cells(li, 11).NumberFormat = 'yyyy-mm-dd'
                except Exception:
                    wi.Cells(li, 11).Value = e['exp']
            out['inbound_rows'] += 1
        xl.CalculateFullRebuild()
        if inplace:
            wb.Save()
            wb.Close(False)
        else:
            target = os.path.normpath(spec['target'])
            if os.path.exists(target):
                os.remove(target)
            wb.SaveAs(target, XL_OPENXML)
            wb.Close(False)
        out['ok'] = True
    except Exception as ex:
        out['error'] = str(ex)[:300]
    finally:
        try:
            xl.Quit()
        except Exception:
            pass
        pythoncom.CoUninitialize()
    print(json.dumps(out, ensure_ascii=False))


if __name__ == '__main__':
    main(sys.argv[1])

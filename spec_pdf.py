# -*- coding: utf-8 -*-
"""시방서(엑셀) → PDF 변환 — 별도 프로세스 (2026-10-28, 사용자 "시방서를 엑셀이 아닌 PDF로 내려받기").
Windows(로컬 PC): Excel COM ExportAsFixedFormat — 인쇄 영역·한 페이지 맞춤·그림 그대로.
Linux(GCP): LibreOffice headless(soffice --convert-to pdf). 한글 글꼴은 fonts-nanum 필요.
내용 없는 시트(예: 빈 '주문내역서')는 빈 페이지가 생기지 않게 빼고 변환한다.
사용: python spec_pdf.py <입력.xlsx> <출력.pdf>
출력: JSON 한 줄 {ok, engine, pages?, error?}"""
import sys, os, json, shutil, subprocess, tempfile
sys.stdout.reconfigure(encoding='utf-8')   # app.py가 utf-8로 읽음

_T0 = __import__('time').time()


def _dbg(m):
    if os.environ.get('SPEC_PDF_DEBUG'):
        print(f'{__import__("time").time() - _T0:6.2f}s {m}', file=sys.stderr, flush=True)


_KEEP = []   # 종료 직전까지 놓지 않을 COM 참조
XL_TYPE_PDF = 0
XL_SHEET_VISIBLE = -1
XL_SHEET_HIDDEN = 0


def _kill(pid):
    try:
        subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True, timeout=15)
    except Exception:
        pass


def _excel_pids():
    try:
        r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq EXCEL.EXE', '/FO', 'CSV', '/NH'], capture_output=True, text=True, timeout=15)
        return {int(x.split('","')[1]) for x in r.stdout.splitlines() if x.startswith('"') and '","' in x}
    except Exception:
        return set()


def _excel(src, dst, limit=75):
    import threading
    import pythoncom, win32com.client as w32, win32process
    pythoncom.CoInitialize()
    before = _excel_pids()
    _dbg('DispatchEx'); xl = w32.DispatchEx('Excel.Application'); _dbg('Excel 시작됨')
    # 이 변환용으로 띄운 Excel만 따로 잡아 둠 — 멈추면 이것만 강제 종료(사용자가 열어 둔 Excel 창은 건드리지 않음)
    pid = win32process.GetWindowThreadProcessId(xl.Hwnd)[1]
    if pid in before:   # 새 Excel이 아니라 이미 열려 있던 Excel에 붙었으면 손대지 않고 중단(숨기기·닫기 금지)
        _KEEP.append(xl)
        raise RuntimeError('변환용 Excel을 따로 띄우지 못함 — 열려 있는 Excel은 건드리지 않음')
    dog = threading.Timer(limit, lambda: (_kill(pid), print(json.dumps({'ok': False, 'error': f'Excel 응답 없음({limit}초)'}, ensure_ascii=False)), os._exit(2)))
    dog.daemon = True; dog.start()
    xl.Visible = False; xl.DisplayAlerts = False; xl.ScreenUpdating = False
    try:
        xl.AutomationSecurity = 3   # 매크로 실행 안 함 (msoAutomationSecurityForceDisable)
    except Exception:
        pass
    wb = None
    try:
        wb = xl.Workbooks.Open(os.path.abspath(src), 0, True)   # 링크 갱신 안 함, 읽기 전용 — 저장하지 않으므로 원본 영향 없음
        _dbg('열기 완료')
        shown = [ws for ws in wb.Worksheets if ws.Visible == XL_SHEET_VISIBLE]
        empty = [ws for ws in shown if xl.WorksheetFunction.CountA(ws.UsedRange) == 0 and ws.Shapes.Count == 0]
        if len(empty) == len(shown):
            raise RuntimeError('인쇄할 내용이 있는 시트가 없음')
        for ws in empty:
            try:
                ws.Visible = XL_SHEET_HIDDEN
            except Exception:
                pass   # 통합 문서 보호로 못 숨기면 빈 페이지가 하나 더 생길 뿐
        # ⚠️ Excel은 파일 이름이 .pdf로 끝나지 않으면 '.pdf'를 덧붙여 저장함 → dst는 반드시 .pdf로 끝나게 넘길 것
        wb.ExportAsFixedFormat(Type=XL_TYPE_PDF, Filename=os.path.abspath(dst), Quality=0,
                               IncludeDocProperties=True, IgnorePrintAreas=False, OpenAfterPublish=False)
        _dbg('PDF 저장 완료')
    finally:
        shown = empty = ws = None   # 시트·통합 문서 참조는 Excel이 살아 있을 때 놓음
        try:
            if wb is not None:
                wb.Close(False)
            wb = None
            xl.Quit(); _dbg('Quit')
        except Exception:
            pass
        # ⚠️ 닫히는 중인 Excel의 참조를 놓으면(xl = None) 그 호출이 60초 RPC 대기에 걸림(겪음, 두 번에 한 번꼴).
        # → 참조는 쥔 채로 이 변환용 Excel만 바로 강제 종료하고, main 끝에서 os._exit로 COM 정리 없이 끝냄.
        _KEEP.append(xl)
        dog.cancel()
        _kill(pid); _dbg('Excel 종료')


def _soffice(src, dst):
    exe = shutil.which('soffice') or shutil.which('libreoffice')
    if not exe:
        raise RuntimeError('LibreOffice(soffice)가 설치돼 있지 않음')
    out_dir = tempfile.mkdtemp(prefix='specpdf_')
    # 프로필을 고정 경로에 두면 두 번째부터 빠름(첫 실행 초기화 생략). 동시 실행은 app.py가 잠금으로 막음.
    prof = os.path.join(tempfile.gettempdir(), 'maehong_lo_profile')
    try:
        # 한국어 로캘 — 엑셀 '간단한 날짜' 서식이 10/15/2026(미국식)이 아닌 2026-10-15로 나오게
        env = dict(os.environ, LANG='ko_KR.UTF-8', LC_ALL='ko_KR.UTF-8', LANGUAGE='ko')
        r = subprocess.run([exe, '-env:UserInstallation=file://' + prof, '--headless', '--norestore', '--nolockcheck',
                            '--convert-to', 'pdf', '--outdir', out_dir, os.path.abspath(src)],
                           capture_output=True, text=True, timeout=170, env=env)
        pdfs = [f for f in os.listdir(out_dir) if f.lower().endswith('.pdf')]
        if not pdfs:
            raise RuntimeError('변환 실패: ' + ((r.stderr or '') + (r.stdout or ''))[-300:])
        shutil.move(os.path.join(out_dir, pdfs[0]), dst)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def main(src, dst):
    out = {'ok': False, 'engine': 'excel' if os.name == 'nt' else 'libreoffice'}
    try:
        (_excel if os.name == 'nt' else _soffice)(src, dst)
        if not os.path.exists(dst) or os.path.getsize(dst) < 100:
            raise RuntimeError('PDF가 만들어지지 않음')
        out['ok'] = True
    except Exception as e:
        out['error'] = str(e)[:300]
    print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
    os._exit(0)   # 남은 COM 프록시 정리를 건너뜀 (결과는 이미 출력) — 종료가 수십 초 늘어지는 것 방지

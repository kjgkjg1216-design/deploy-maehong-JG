# -*- coding: utf-8 -*-
"""부자재 시안(.ai/.pdf) 쪽별 JPG 미리보기 — 별도 프로세스 (2026-10-28, 사용자 "규격 패널 시안 아이콘 클릭 시 보기").
일러스트레이터 .ai는 'PDF 호환'으로 저장돼 PDF로 읽힌다(파일 앞부분 %PDF-1.x, 대지마다 한 쪽). 원본은 수십~수백 MB라
브라우저로 보내지 않고 긴 변 width px JPG(쪽당 수백 KB)로 바꿔 보여 준다.
Windows(로컬 PC): 내장 Windows.Data.Pdf(design_preview.ps1) / Linux(GCP): poppler pdftoppm.
사용: python design_preview.py <입력.pdf|.ai> <출력 접두어> [긴 변 px] [최대 쪽수]
  → <접두어>.jpg(1쪽), <접두어>-p2.jpg …
출력: JSON 한 줄 {ok, pages(전체 쪽수), rendered(만든 쪽수), error?}"""
import sys, os, json, re, glob, shutil, subprocess, tempfile
sys.stdout.reconfigure(encoding='utf-8')   # app.py가 utf-8로 읽음

BASE = os.path.dirname(os.path.abspath(__file__))


def _windows(src, prefix, width, max_pages):
    # WinRT PDF는 확장자가 .pdf여야 안전 → 임시 .pdf 이름으로 하드링크(안 되면 복사)
    tmp_dir = tempfile.mkdtemp(prefix='dsgprev_')
    try:
        pdf = os.path.join(tmp_dir, 'in.pdf')
        try:
            os.link(src, pdf)
        except OSError:
            shutil.copyfile(src, pdf)
        r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', os.path.join(BASE, 'design_preview.ps1'),
                            os.path.abspath(pdf), os.path.abspath(prefix), str(width), str(max_pages)],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=900)
        lines = [x for x in (r.stdout or '').splitlines() if x.strip().startswith('{')]
        res = json.loads(lines[-1]) if lines else {'ok': False, 'error': (r.stderr or r.stdout or '')[-250:]}
        if not res.get('ok'):
            raise RuntimeError(res.get('error') or '미리보기 실패')
        return res.get('pages'), res.get('rendered')
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _poppler(src, prefix, width, max_pages):
    pages = None
    try:
        info = subprocess.run(['pdfinfo', src], capture_output=True, text=True, errors='replace', timeout=60).stdout
        m = re.search(r'^Pages:\s+(\d+)', info, re.M)
        pages = int(m.group(1)) if m else None
    except Exception:
        pass
    last = min(pages or 1, max_pages)
    raw = prefix + '_raw'
    r = subprocess.run(['pdftoppm', '-jpeg', '-jpegopt', 'quality=88', '-scale-to', str(width), '-f', '1', '-l', str(last),
                        src, raw], capture_output=True, text=True, errors='replace', timeout=900)
    outs = sorted(glob.glob(glob.escape(raw) + '-*.jpg'), key=lambda p: int(re.search(r'-(\d+)\.jpg$', p).group(1)))
    if not outs:
        raise RuntimeError('미리보기 실패: ' + (r.stderr or '')[-250:])
    for i, p in enumerate(outs, 1):   # pdftoppm은 쪽 번호를 0으로 채움(-01, -001) → 우리 이름으로
        os.replace(p, prefix + '.jpg' if i == 1 else f'{prefix}-p{i}.jpg')
    return pages or len(outs), len(outs)


def main(src, prefix, width=2400, max_pages=8):
    out = {'ok': False}
    try:
        with open(src, 'rb') as f:
            head = f.read(1024)
        if b'%PDF' not in head:
            raise RuntimeError('PDF 호환으로 저장된 파일이 아니라 미리보기를 만들 수 없음')
        out['pages'], out['rendered'] = (_windows if os.name == 'nt' else _poppler)(src, prefix, int(width), int(max_pages))
        if not os.path.exists(prefix + '.jpg') or os.path.getsize(prefix + '.jpg') < 100:
            raise RuntimeError('미리보기 그림이 만들어지지 않음')
        out['ok'] = True
    except Exception as e:
        out['error'] = str(e)[:300]
    print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], *sys.argv[3:5])

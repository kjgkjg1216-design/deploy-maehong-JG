# -*- coding: utf-8 -*-
"""부자재 시안(.ai/.pdf) 쪽별 JPG 미리보기 — 별도 프로세스 (2026-10-28, 사용자 "규격 패널 시안 아이콘 클릭 시 보기").
일러스트레이터 .ai는 'PDF 호환'으로 저장돼 PDF로 읽힌다(파일 앞부분 %PDF-1.x, 대지마다 한 쪽). 원본은 수십~수백 MB라
브라우저로 보내지 않고 긴 변 width px JPG(쪽당 수백 KB)로 바꿔 보여 준다.
Windows(로컬 PC): PyMuPDF(없으면 내장 Windows.Data.Pdf, design_preview.ps1) / Linux(GCP): poppler pdftoppm.
사용: python design_preview.py <입력.pdf|.ai> <출력 접두어> [긴 변 px] [최대 쪽수]
  → <접두어>.jpg(1쪽), <접두어>-p2.jpg …
출력: JSON 한 줄 {ok, pages(전체 쪽수), rendered(만든 쪽수), chk?, nopdf?(안내문뿐인 .ai), conv?(Scribus로 변환해 그림),
      thumb?(1쪽=파일 속 썸네일 — 변환 못 했을 때만 씀), error?}
안내문 여부만: python design_preview.py --check <입력> [만든 쪽 수] [썸네일 저장 경로] → {ok, chk?, nopdf?, thumb?}"""
import sys, os, json, re, glob, shutil, subprocess, tempfile, base64, binascii, zlib
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


def _mupdf(src, prefix, width, max_pages):
    # PyMuPDF(2026-11-02): 무거운 시안도 몇 초 — WinRT는 Scribus PDF에서 15~30분+ 걸려 시간 초과
    import pymupdf
    with pymupdf.open(src, filetype='pdf') as doc:   # .ai도 PDF로
        n = min(doc.page_count, max_pages)
        for i in range(n):
            pg = doc[i]
            z = width / max(pg.rect.width, pg.rect.height)
            pg.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False).save(
                prefix + '.jpg' if i == 0 else f'{prefix}-p{i + 1}.jpg', jpg_quality=85)
        return doc.page_count, n


def _local(src, prefix, width, max_pages):
    # Windows: PyMuPDF 우선, 없거나 실패하면 내장 WinRT
    try:
        return _mupdf(src, prefix, width, max_pages)
    except Exception:
        return _windows(src, prefix, width, max_pages)


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


_PH_WORDS = {'adobe', 'illustrator', 'pdf'}


def placeholder_pages(src, last):
    """일러스트레이터에서 'PDF 호환 파일 만들기'를 끄고 저장한 .ai는 PDF 쪽에 그림 대신 안내문
    ('이 파일은 PDF 내용 없이 저장된 Adobe® Illustrator® 파일입니다…')만 들어 있다 → 1~last쪽 중 그런 쪽 번호 (2026-10-30).
    한글판 안내문의 영문 낱말은 Adobe·Illustrator·PDF뿐(GCP poppler엔 한글 자료가 없어 한글은 안 뽑힘), 영문판은 'without PDF Content'.
    pdftotext가 없으면(Windows) None."""
    try:
        t = subprocess.run(['pdftotext', '-raw', '-l', str(last), src, '-'], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=300).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    out = []
    for i, pg in enumerate(t.split('\f')[:int(last)], 1):   # pdftotext는 쪽마다 \f로 끊음
        low = pg.lower()
        if 'illustrator' in low and 'pdf' in low and (
                'without pdf content' in low or all(w in _PH_WORDS for w in re.findall(r'[a-z]+', low))):
            out.append(i)
    return out


def xmp_thumb(src):
    """일러스트레이터가 파일 맨 앞 XMP에 넣어 두는 썸네일(xmpGImg:image, base64 JPEG, 긴 변 256px) → JPEG bytes 또는 None.
    'PDF 호환' 없이 저장된 .ai에도 들어 있어 안내문 대신 작은 미리보기로 씀 (2026-10-30, 실측 7개 모두 앞 2KB 근처). 여러 개면 가장 큰 것."""
    with open(src, 'rb') as f:
        head = f.read(8 << 20)
    best = None
    for m in re.finditer(rb'<xmpGImg:image>([^<]+)</xmpGImg:image>', head):
        try:
            raw = base64.b64decode(re.sub(rb'&#x[aAdD];|\s', b'', m.group(1)))
        except (ValueError, binascii.Error):
            continue
        if raw[:2] == b'\xff\xd8' and (best is None or len(raw) > len(best)):
            best = raw
    return best


def check(src, last, thumb_out=None):
    """{chk: 확인함, nopdf: 만든 쪽이 모두 안내문뿐, thumb: 안내문뿐이라 썸네일을 thumb_out에 씀} — 확인 못 하면 {}."""
    ph = placeholder_pages(src, last)
    if ph is None:
        return {}
    out = {'chk': True, 'nopdf': int(last) > 0 and len(ph) >= int(last)}
    if out['nopdf'] and thumb_out:
        t = xmp_thumb(src)
        out['thumb'] = bool(t)
        if t:
            with open(thumb_out, 'wb') as f:
                f.write(t)
    return out


def ai_native(src, out):
    """PDF 형태 .ai에서 일러스트레이터 고유 데이터(1쪽 /PieceInfo/Illustrator/Private의 AIPrivateData 블록)를 꺼내 out에 씀.
    Scribus extractFromPDF와 같은 순서(블록이 하나면 1번, 아니면 2번부터). Scribus는 %PDF로 시작하는 파일엔 'PDF/AI 중 고르라'는
    창을 띄워 서버에서 멈추므로 미리 꺼내 넘김. 일러스트레이터는 객체 스트림 없이 저장(실측)."""
    with open(src, 'rb') as f:
        d = f.read()
    offs = {int(m.group(1)): m.end() for m in re.finditer(rb'(?:^|[\r\n\s])(\d+)\s+0\s+obj\b', d)}   # 뒤(증분 저장)가 최신

    def body(n):
        return d[offs[n]:d.find(b'endobj', offs[n])]

    def stream(n):
        b = body(n)
        k = b.find(b'stream')
        dic = b[:k]
        ln = re.search(rb'/Length\s+(\d+)(\s+0\s+R)?', dic)
        size = int(re.search(rb'\d+', body(int(ln.group(1)))).group()) if ln.group(2) else int(ln.group(1))
        p = offs[n] + k + len(b'stream')
        p += 2 if d[p:p + 2] == b'\r\n' else 1
        raw = d[p:p + size]
        return zlib.decompress(raw) if b'/FlateDecode' in dic else raw

    priv = body(int(re.search(rb'/Private\s+(\d+)\s+0\s+R', d).group(1)))
    refs = {int(a): int(b) for a, b in re.findall(rb'/AIPrivateData(\d+)\s+(\d+)\s+0\s+R', priv)}
    nb = re.search(rb'/NumBlock\s+(\d+)', priv)
    num = int(nb.group(1)) + 1 if nb else len(refs) + 1
    with open(out, 'wb') as f:
        for i in ([1] if num == 2 else range(2, num)):
            if i not in refs:
                break
            f.write(stream(refs[i]))


_CONV_MAX = 80 << 20   # 이보다 큰 원본은 변환 안 함(1GB VM 메모리 — 실측 41MB 원본에 Scribus 209MB, 191MB 원본은 1분 만에 200MB 넘고 스왑까지 써서 중단)


def scribus_pdf(src, pdf):
    """'PDF 호환' 없이 저장된 .ai → Scribus(무료, GCP apt)로 고유 데이터를 읽어 PDF로 (2026-10-30, 사용자 "PDF·JPG로 바꿔 보이게").
    실측 B0367 4MB 86초·B0364 41MB 173초, 사진·글자·치수선까지 원본과 같게 나옴. → 성공 여부.
    기본·고해상도를 따로 부르므로 결과(src.conv.pdf)·실패 표시(src.conv.fail)를 원본 옆에 남겨 한 번만 변환."""
    if os.path.exists(pdf):
        return True
    if os.path.exists(src + '.conv.fail') or os.path.getsize(src) > _CONV_MAX or not shutil.which('scribus'):
        return False
    native, ok = src + '.native.ai', False
    try:
        ai_native(src, native)
        r = subprocess.run(['scribus', '-g', '-ns', '-py', os.path.join(BASE, 'scribus_ai2pdf.py'), native, pdf],
                           capture_output=True, text=True, errors='replace', timeout=900,
                           env=dict(os.environ, QT_QPA_PLATFORM='offscreen'))
        ok = 'SCRIBUS_OK' in (r.stdout or '') and os.path.exists(pdf) and os.path.getsize(pdf) > 1000
    except Exception:
        ok = False
    finally:
        if os.path.exists(native):
            os.remove(native)
    if not ok:
        if os.path.exists(pdf):
            os.remove(pdf)
        open(src + '.conv.fail', 'w').close()
    return ok


def main(src, prefix, width=2400, max_pages=8):
    out = {'ok': False}
    try:
        with open(src, 'rb') as f:
            head = f.read(1024)
        if b'%PDF' not in head:
            raise RuntimeError('PDF 호환으로 저장된 파일이 아니라 미리보기를 만들 수 없음')
        out['pages'], out['rendered'] = (_local if os.name == 'nt' else _poppler)(src, prefix, int(width), int(max_pages))
        if not os.path.exists(prefix + '.jpg') or os.path.getsize(prefix + '.jpg') < 100:
            raise RuntimeError('미리보기 그림이 만들어지지 않음')
        out.update(check(src, out['rendered'], prefix + '-thumb.jpg'))
        if out.get('nopdf') and os.name != 'nt':   # 안내문뿐인 .ai → Scribus로 변환한 PDF를 다시 그림으로
            out['conv'] = scribus_pdf(src, src + '.conv.pdf')
            if out['conv']:
                for p in glob.glob(glob.escape(prefix) + '*.jpg'):
                    os.remove(p)
                out['pages'], out['rendered'] = _poppler(src + '.conv.pdf', prefix, int(width), int(max_pages))
        if out.get('thumb') and not out.get('conv'):   # 변환 못 하면 1쪽을 파일 속 썸네일로 바꿔 끼움
            for p in range(2, out['rendered'] + 1):
                if os.path.exists(f'{prefix}-p{p}.jpg'):
                    os.remove(f'{prefix}-p{p}.jpg')
            os.replace(prefix + '-thumb.jpg', prefix + '.jpg')
            out['rendered'] = 1
        out['ok'] = True
    except Exception as e:
        out['error'] = str(e)[:300]
    print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    if sys.argv[1] == '--check':   # 이미 만든 미리보기의 안내문 여부만: --check <원본> [만든 쪽 수] [썸네일 저장 경로]
        print(json.dumps(dict(ok=True, **check(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 1,
                                               sys.argv[4] if len(sys.argv) > 4 else None)), ensure_ascii=False), flush=True)
    else:
        main(sys.argv[1], sys.argv[2], *sys.argv[3:5])

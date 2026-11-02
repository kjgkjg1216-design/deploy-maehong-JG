# -*- coding: utf-8 -*-
"""Scribus 스크립터용 (2026-10-30): 일러스트레이터 고유 데이터(.ai, PDF 아님)를 열어 PDF로 내보냄.
'PDF 호환' 없이 저장된 .ai 미리보기용 — design_preview.py가 고유 데이터를 꺼내 넘김.
사용: QT_QPA_PLATFORM=offscreen scribus -g -ns -py scribus_ai2pdf.py <고유데이터.ai> <출력.pdf>"""
import os, sys, traceback
try:
    import scribus
    src, out = sys.argv[1], sys.argv[2]
    scribus.openDoc(src)
    pdf = scribus.PDFfile()
    pdf.file = out
    pdf.save()
    scribus.closeDoc()
    print('SCRIBUS_OK', flush=True)
except Exception:
    traceback.print_exc()
    sys.stdout.flush()
os._exit(0)   # GUI 없이 돌려도 Scribus가 스스로 끝나지 않을 수 있어서

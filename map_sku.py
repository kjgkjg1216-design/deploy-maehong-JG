"""판매 SKU → 아마란스 품번 자동 이름매칭.
- 제품마스터 = 단가마스터 ∪ BOM 모품번 (G/H/I)
- 채널 접두사/수식어 제거 후 정규화 → 유사도 + 용량토큰 일치 검사
- 결과: 자동확정 / 검토필요 / 실패 로 분류해 CSV 저장
"""
import glob, re, unicodedata
import pandas as pd
from difflib import SequenceMatcher

SALES = 'C:/Users/jgkim/maehong-JG/월간판매수량_최근6개월.csv'
OUT   = 'C:/Users/jgkim/maehong-JG/SKU매핑_초벌.csv'


def latest(t):
    fs = sorted(glob.glob(f'data/*_{t}.csv'))
    return fs[-1] if fs else None


# ── 제품마스터 (G/H/I) ──────────────────────────
def load_master():
    """제품마스터 = BOM 모품번 ∪ 단가 ∪ 현재고 ∪ 출하 (G/H/I).
    단가·BOM만 쓰면 I0156~158처럼 현재고/출하에만 있는 실판매 제품이 누락됨."""
    bom = pd.read_csv(latest('BOM'), dtype=str).fillna('')
    bom_parents = set(bom['모품번'].str.strip().str.upper())

    name, codes = {}, set(bom_parents)
    for _, r in bom.drop_duplicates('모품번').iterrows():
        name.setdefault(str(r['모품번']).strip().upper(), str(r['모품명']).strip())

    # 품번/품명 컬럼을 가진 소스들을 순서대로 병합 (뒤가 이름 우선)
    for t in ('현재고', '출하정보', '단가'):
        p = latest(t)
        if not p:
            continue
        d = pd.read_csv(p, dtype=str).fillna('')
        if '품번' not in d.columns or '품명' not in d.columns:
            continue
        for _, r in d.iterrows():
            c = str(r['품번']).strip().upper()
            if not c:
                continue
            codes.add(c)
            nm = str(r['품명']).strip()
            if nm:
                name[c] = nm

    rows = []
    for c in codes:
        if c[:1] not in ('G', 'H', 'I'):
            continue
        nm = name.get(c, '')
        if not nm or nm.lstrip().startswith('미사용'):   # 단종 제품은 매칭 대상 제외
            continue
        rows.append({'품번': c, '품명': nm, 'BOM': 'Y' if c in bom_parents else 'N'})
    return pd.DataFrame(rows)


# ── 정규화 ─────────────────────────────────────
_PREFIX = re.compile(r'\[[^\]]*\]|valuepack_?|미사용_?|예시\)|온라인\)|미도인\)|이마트\)', re.I)
_PAREN  = re.compile(r'\((?:봉|통|박스|외|주|청통|캔|팩|아라|MK/주문|주문)\)')


def norm(s):
    s = unicodedata.normalize('NFKC', str(s))
    s = _PREFIX.sub(' ', s)
    s = _PAREN.sub(' ', s)
    s = s.lower()
    s = re.sub(r'[^0-9a-z가-힣]', '', s)   # 공백/기호 제거
    return s


# 용량/수량 토큰: 숫자+단위 (g, kg, ml, l, 개, 봉, 포, 매, ea, 입)
_TOK = re.compile(r'(\d+(?:\.\d+)?)\s*(kg|g|ml|l|개입|개|봉|포|매|ea|입|번들|세트)', re.I)


def size_tokens(s):
    """무게(g/ml) 값 집합과 개수 값 집합을 분리 추출."""
    s = unicodedata.normalize('NFKC', str(s)).lower().replace('×', '*')
    weights, counts = set(), set()
    for num, unit in _TOK.findall(s):
        u, v = unit.lower(), float(num)
        if u == 'kg':
            v, u = v * 1000, 'g'
        elif u == 'l':
            v, u = v * 1000, 'ml'
        if u in ('g', 'ml'):
            weights.add(v)
        else:                      # 개/봉/포/매/ea/입/번들/세트
            counts.add(v)
    return weights, counts


def totals(sz):
    """총량 후보 = 개별 무게 ∪ (무게×개수).
    '330g(110g*3개)'와 '110g*3개'가 같은 제품임을 인식하기 위함."""
    w, c = sz
    out = set(w)
    for g in w:
        for n in c:
            out.add(g * n)
    return out


def size_verdict(a, b):
    """('match'|'conflict'|'unknown') — 1kg vs 3kg 오매칭 방지용."""
    ta, tb = totals(a), totals(b)
    if not ta or not tb:
        return 'unknown'
    return 'match' if (ta & tb) else 'conflict'


def score(a, b):
    return SequenceMatcher(None, a, b).ratio()


def main():
    sales = pd.read_csv(SALES, dtype=str, encoding='utf-8-sig').fillna('')
    master = load_master()
    master['n'] = master['품명'].map(norm)
    master['tok'] = master['품명'].map(size_tokens)

    skus = sales.drop_duplicates('SKU')[['SKU', '제품명']].copy()
    skus['n'] = skus['제품명'].map(norm)
    skus['tok'] = skus['제품명'].map(size_tokens)

    rows = []
    for _, s in skus.iterrows():
        cands = []
        for _, m in master.iterrows():
            sc = score(s['n'], m['n'])
            sv = size_verdict(s['tok'], m['tok'])
            # 용량 일치 가산 / 충돌(1kg vs 3kg) 강한 감점
            eff = sc + (0.15 if sv == 'match' else (-0.35 if sv == 'conflict' else 0.0))
            cands.append((eff, sc, sv, m))
        cands.sort(key=lambda x: -x[0])
        top = cands[:3]
        _, raw, best_sv, best = top[0]

        if raw >= 0.80 and best_sv == 'match':
            verdict = '자동확정'
        elif best_sv == 'conflict' or raw < 0.55:
            verdict = '실패'
        else:
            verdict = '검토필요'

        row = {
            'SKU': s['SKU'], '판매제품명': s['제품명'],
            '추천품번': best['품번'], '추천품명': best['품명'], 'BOM': best['BOM'],
            '유사도': round(raw, 3), '용량판정': best_sv, '판정': verdict,
            '확정품번': best['품번'] if verdict == '자동확정' else '',   # ← 사람이 채울 칸
        }
        for i, (_, sc2, sv2, m2) in enumerate(top, 1):   # 후보 1~3
            row[f'후보{i}'] = f"{m2['품번']} | {m2['품명']} | sim={sc2:.2f} size={sv2} BOM={m2['BOM']}"
        rows.append(row)

    out = pd.DataFrame(rows).sort_values(['판정', '유사도'], ascending=[True, False])
    out.to_csv(OUT, index=False, encoding='utf-8-sig')
    print('저장:', OUT)
    print(out['판정'].value_counts().to_string())
    return out


if __name__ == '__main__':
    main()

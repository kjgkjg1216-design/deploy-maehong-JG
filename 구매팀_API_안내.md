# 구매팀 데이터 API 안내

작성: 2026-09-23 · 대상: 구매팀 데이터를 가져다 쓰는 사내 프로그램 개발자

---

## 1. 접속 정보

| 항목 | 값 |
|---|---|
| Base URL (사내망) | `http://<대시보드PC>:5100` |
| Base URL (외부) | `https://media.maehong.top` |
| 인증 | 헤더 `X-API-Key: <발급키>` (또는 `?api_key=<발급키>`) |
| 형식 | JSON (`&format=csv` 로 CSV 도 가능) |
| 권한 | **읽기 전용**. 구매팀 키는 구매팀 리소스만 접근 가능 |

> 키는 별도로 전달받으세요. 이 문서에는 적지 않습니다.

연결 확인:
```
GET /api/partner/health
→ {"ok":true,"authenticated":true,"partner":"구매팀 (사내 프로그램)"}
```

---

## 2. 통합 일자별 데이터 (주 엔드포인트)

```
GET /api/partner/purchasing/daily?from=2026-09-01&to=2026-09-20
```

| 파라미터 | 필수 | 설명 |
|---|---|---|
| `from`, `to` | ✅ | `YYYY-MM-DD`. **최대 92일** (초과 시 400) |
| `channels` | | 콤마 구분. 미지정 시 전 채널. 예: `lotte_mart,coupang_rocket` |
| `format` | | `json`(기본) / `csv` |

**행 단위 = SKU × 일자 × 채널.** 한 행에 요청하신 5개 항목이 모두 들어 있습니다.

```json
{
  "date": "2026-09-01",
  "channel": "lotte_mart",
  "channel_name": "롯데마트",
  "channel_type": "offline",
  "sku": "8809478911724",
  "sku_type": "ean",
  "name": "푸짐한 누룽지(900G)",
  "category": "누룽지",
  "self_code": "I0102",
  "pack_qty": 9,
  "stack_qty": 30,
  "delivery_qty": 9,
  "pos_qty": 74,
  "stock_qty": 832,
  "supply_amount": 37350,
  "unit_supply_price": 4150
}
```

### 필드 설명
| 필드 | 의미 |
|---|---|
| `sku` / `sku_type` | 채널마다 키 체계가 다릅니다. **온라인=쿠팡 SKU번호**(`coupang_sku`), **오프라인=EAN 13자리**(`ean`) |
| `self_code` | 자사코드(아마란스 품번). **채널 간 동일 상품을 묶는 키**로 쓰세요 |
| `name`, `category` | 상품명·카테고리 (마스터 우선, 없으면 채널 원본명) |
| `pack_qty` / `stack_qty` | **박스당 입수** / 적재량 |
| `delivery_qty` | **납품(입고) 수량** — 필수 지표 |
| `pos_qty` | POS 판매수량 (채널이 제공할 때만) |
| `stock_qty` | 점재고(오프라인) / 센터재고(온라인) |
| `supply_amount` | 공급가 합계 (VAT 제외) |
| `unit_supply_price` | `supply_amount / delivery_qty` 반올림 = **단가** |

**값이 없으면 `null`** 입니다 (0이 아님). 채널별 수집 범위가 달라서 생기는 정상 상황입니다.

### 날짜 기준 (중요)
- **온라인**: `입고반출시각` = 실제 입고 시점
- **오프라인**: `납품일자` / `매입일자`

### 채널 코드
| 구분 | 코드 |
|---|---|
| 온라인 | `coupang_rocket`(쿠팡 로켓), `coupang_fresh`(쿠팡 프레시) |
| 오프라인 | `lotte_mart`, `lotte_super`, `lotte_max`, `emart`, `homeplus`, `homeplus_hyper`, `homeplus_express`, `gs`(GS편의점), `gs_super`, `costco` |

---

## 3. SKU 마스터 (신규 SKU 동기화용)

```
GET /api/partner/purchasing/skus
```
기간 없이 전체 SKU를 돌려줍니다 (현재 454건). 신규 상품이 생겼는지 확인하는 용도로 쓰세요.

```json
{"sku":"13256417","name":"자연다움 간편하고 든든하게 고구마스틱 400g(20g*20개입)",
 "category":"고구마","self_code":"I0110","pack_qty":9,"stack_qty":36}
```

---

## 4. 호출 예시

```bash
curl -H "X-API-Key: <키>" \
  "https://media.maehong.top/api/partner/purchasing/daily?from=2026-09-01&to=2026-09-20"
```

```python
import requests
r = requests.get("https://media.maehong.top/api/partner/purchasing/daily",
                 params={"from": "2026-09-01", "to": "2026-09-20"},
                 headers={"X-API-Key": "<키>"}, timeout=120)
rows = r.json()["rows"]
```

---

## 5. 운영 안내

- **호출 빈도**: 하루 1~2회 전제로 만들었습니다. 동일 조건 응답은 **10분간 캐시**되니 반복 호출해도 부담 없습니다.
- **응답 속도**: 20일·전채널 기준 약 2~3초 / 약 2,900행.
- **데이터 갱신 시각**: 오프라인은 매일 아침 수집(08:2x), 온라인은 06:00·수시. 전날 데이터는 **당일 오전 9시 이후** 조회를 권장합니다.
- **에러 코드**: `401` 키 없음/틀림, `403` 권한 밖 리소스, `400` 기간 오류(92일 초과 등), `503` 데이터 일시 불가.
- 문의: 매홍 온라인팀

---

## 6. 알려진 제약

| 항목 | 내용 |
|---|---|
| 코스트코 | POS 판매·재고 미수집 (납품만) |
| 홈플 하이퍼/익스프레스 | 점재고는 통합 `homeplus` 에만 존재 |
| GS | 데이터 공시가 늦어 최근 2~3일은 비어 있을 수 있음 |
| 이마트 재고 | 일자별 스냅샷이 부족해 최신 값이 반복될 수 있음 |
| 컬리·3P | 이번 범위에서 제외 (필요 시 추가 가능) |

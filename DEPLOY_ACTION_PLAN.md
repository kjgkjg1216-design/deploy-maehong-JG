# 매홍 L&F 챗봇 배포 액션플랜

## 현재 상태
- Firebase 프로젝트: `maehong-scm` (Blaze 요금제)
- Firebase Auth: Google 로그인 설정 완료
- Firestore: 데이터베이스 생성 + 보안규칙 배포 완료
- Firebase Admin SDK: 서비스 계정 키 발급 완료
- Flask 앱: Firebase Auth + Firestore 연동 코드 완료

## 남은 작업 (API 활성화 1건 + Claude 자동 작업)

### STEP 1: 2개 링크 클릭 (API 활성화 + gcloud 설치)

**1-A. Cloud Run API 활성화** — 링크 클릭 → "사용 설정" 버튼:
https://console.developers.google.com/apis/api/run.googleapis.com/overview?project=776997651051

**1-B. Google Cloud SDK 설치** — 다운로드 → 설치 → 재부팅:
https://cloud.google.com/sdk/docs/install?hl=ko
- "Windows용 Cloud SDK 설치 프로그램" 다운로드
- 설치 시 모든 기본값 OK
- 설치 완료 후 터미널에서 `gcloud --version` 확인

> 두 가지 완료 후 Claude에게 "완료" 알려주세요.

---

### STEP 2 이후: Claude가 자동 처리

"STEP 1 완료"라고 말하면 아래를 전부 자동으로 진행합니다:

| 순서 | 작업 | 설명 |
|------|------|------|
| 2 | Dockerfile 생성 | Flask 앱을 컨테이너로 패키징 |
| 3 | Cloud Run 배포 | `gcloud run deploy` 실행 |
| 4 | 환경변수 설정 | API 키들을 Cloud Run 환경변수로 |
| 5 | Firebase Hosting 연결 | 프론트 → Cloud Run 백엔드 연동 |
| 6 | 배포 테스트 | 실제 URL로 동작 확인 |
| 7 | GitHub 커밋 | 배포 설정 코드 푸시 |

---

## 배포 후 구조

```
사용자 (어디서든)
    │
    ▼
Firebase Hosting (정적 프론트엔드)
    │   https://maehong-scm.web.app
    │
    ├── Firebase Auth (Google 로그인)
    ├── Firestore (대화 히스토리, 공유)
    │
    └── Cloud Run (Flask 백엔드)
            │   https://maehong-chatbot-xxxxx.run.app
            │
            ├── OpenAI GPT-4o (RAG 응답)
            ├── 아마란스10 API (발주/생산/BOM)
            ├── Monday.com API (업무관리)
            └── 로컬 CSV 데이터
```

## 주의사항

### 아마란스 API IP 허용
Cloud Run은 IP가 유동적이므로, 아마란스 관리자에서 Cloud Run의 아웃바운드 IP를 허용해야 합니다.
- 방법1: Cloud NAT로 고정 IP 설정 (추가 비용 월 ~3천원)
- 방법2: 아마란스 데이터를 주기적으로 수집하여 Firestore에 저장 (IP 불필요)

### 예상 비용 (월간)
| 항목 | 비용 |
|------|------|
| Cloud Run | 무료 (월 200만 요청) |
| Firestore | 무료 (일 5만 읽기) |
| Firebase Auth | 무료 |
| Firebase Hosting | 무료 (10GB/월) |
| OpenAI GPT-4o | ~3만원 (사용량 따라) |
| **합계** | **~3만원/월** |

---

## 시작하기

**STEP 1 링크를 클릭하고 "완료"라고 말해주세요.**

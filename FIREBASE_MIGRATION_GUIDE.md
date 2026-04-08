# 매홍 L&F 챗봇 Firebase 마이그레이션 가이드

## 현재 구조 vs Firebase 구조

```
[현재 - 로컬 전용]
PC (python app.py) → ngrok → 외부접속
    └── 데이터: 로컬 CSV 파일
    └── 인증: 없음 (누구나 접속)
    └── 히스토리: 없음 (새로고침하면 사라짐)

[목표 - Firebase 클라우드]
사용자 → Firebase Hosting (프론트엔드)
           → Cloud Run (Flask 백엔드)
               → Firestore (대화 히스토리, 사용자 데이터)
               → Cloud Storage (엑셀 파일, CSV)
               → 아마란스 API / Monday API / OpenAI API
           → Firebase Auth (Google 로그인)
```

---

## 액션 플랜 (순서대로 실행)

### STEP 1: Firebase 프로젝트 생성

1. https://console.firebase.google.com/ 접속
2. **"프로젝트 추가"** 클릭
3. 프로젝트 이름: `maehong-chatbot` 입력
4. Google Analytics: **사용 설정** (선택)
5. 프로젝트 생성 완료

> 참고: https://firebase.google.com/docs/web/setup?hl=ko

---

### STEP 2: Firebase 요금제 변경 (Blaze)

Cloud Run을 사용하려면 **Blaze 요금제** (종량제)가 필요합니다.

1. Firebase 콘솔 → 좌측 하단 **"Spark"** 클릭
2. **"Blaze 요금제로 업그레이드"** 선택
3. 결제 계정 연결 (신용카드 등록)

> ⚠️ 무료 범위가 넉넉하므로 소규모 사용 시 비용 거의 없음
> - Firestore: 일 5만건 읽기/2만건 쓰기 무료
> - Cloud Run: 월 200만 요청, 36만 CPU-초 무료
> - Storage: 5GB 무료
> - Auth: 무료
>
> 참고: https://firebase.google.com/pricing?hl=ko

---

### STEP 3: Firebase CLI 설치

**PowerShell (관리자 권한)**에서 실행:

```powershell
npm install -g firebase-tools
```

Node.js가 없으면 먼저 설치:
- https://nodejs.org/ko (LTS 버전 다운로드 → 설치)

설치 확인:
```powershell
firebase --version
```

> 참고: https://firebase.google.com/docs/cli?hl=ko

---

### STEP 4: Firebase 로그인

```powershell
firebase login
```

브라우저가 열리면 Google 계정으로 로그인 → 권한 허용

---

### STEP 5: Firebase Authentication (Google 로그인) 설정

1. Firebase 콘솔 → **Authentication** → **"시작하기"**
2. **Sign-in method** 탭 → **Google** 클릭
3. **"사용 설정"** 토글 ON
4. 프로젝트 지원 이메일: 본인 이메일 입력
5. **저장**

> 참고: https://firebase.google.com/docs/auth/web/google-signin?hl=ko

---

### STEP 6: Firestore Database 생성

1. Firebase 콘솔 → **Firestore Database** → **"데이터베이스 만들기"**
2. 위치: **asia-northeast3 (서울)** 선택
3. 보안 규칙: **"프로덕션 모드에서 시작"** 선택
4. **만들기**

사용할 컬렉션 구조:
```
users/
  {userId}/
    displayName: "김지광"
    email: "kjgkjg1216@gmail.com"
    lastLogin: timestamp

chats/
  {chatId}/
    userId: "xxx"
    title: "C0018 재고 조회"
    createdAt: timestamp
    messages: [
      {role: "user", content: "C0018 재고수량 알려줘", timestamp: ...},
      {role: "assistant", content: "...", timestamp: ...}
    ]

shared/
  {shareId}/
    chatId: "xxx"
    sharedBy: "userId"
    sharedAt: timestamp
    expiresAt: timestamp (선택)
```

> 참고: https://firebase.google.com/docs/firestore/quickstart?hl=ko

---

### STEP 7: Cloud Storage 설정

1. Firebase 콘솔 → **Storage** → **"시작하기"**
2. 보안 규칙: 기본값 사용
3. 위치: **asia-northeast3 (서울)**

엑셀 파일과 CSV를 여기에 저장합니다.

> 참고: https://firebase.google.com/docs/storage/web/start?hl=ko

---

### STEP 8: 프로젝트 구조 변경

현재 프로젝트를 Firebase 구조로 재구성합니다.

```
maehong-JG/
├── frontend/               ← Firebase Hosting (프론트엔드)
│   ├── public/
│   │   ├── index.html      ← 챗봇 UI
│   │   ├── admin.html      ← 관리자 대시보드
│   │   ├── login.html      ← Google 로그인 페이지
│   │   └── js/
│   │       ├── app.js      ← 챗봇 로직
│   │       ├── auth.js     ← Firebase Auth
│   │       └── history.js  ← 대화 히스토리
│   └── firebase.json
│
├── backend/                ← Cloud Run (Flask 백엔드)
│   ├── app.py              ← 기존 Flask 앱 (API만)
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env                ← 환경변수 (Cloud Run 환경변수로 이동)
│
├── functions/              ← Cloud Functions (선택)
│   └── index.js            ← 공유 링크 생성 등
│
├── firebase.json
├── .firebaserc
└── Dockerfile
```

> 이 단계는 Claude가 자동으로 코드를 변환합니다.
> "STEP 8 진행해줘"라고 말하면 됩니다.

---

### STEP 9: Docker 설치 (Cloud Run 배포용)

1. https://www.docker.com/products/docker-desktop/ 접속
2. **Docker Desktop for Windows** 다운로드 → 설치
3. 재부팅 후 Docker Desktop 실행
4. 확인:
```powershell
docker --version
```

> 참고: https://docs.docker.com/desktop/setup/install/windows-install/

---

### STEP 10: Cloud Run 배포

```powershell
# 프로젝트 폴더에서
cd C:\Users\jgkim\maehong-JG

# Google Cloud SDK 설치 (없으면)
# https://cloud.google.com/sdk/docs/install?hl=ko

# 로그인
gcloud auth login

# 프로젝트 설정
gcloud config set project maehong-chatbot

# Cloud Run에 배포
gcloud run deploy maehong-chatbot \
  --source . \
  --region asia-northeast3 \
  --allow-unauthenticated \
  --set-env-vars "OPENAI_API_KEY=xxx,AMARANTH_ACCESS_TOKEN=xxx,..."
```

> ⚠️ 아마란스 API는 Cloud Run의 고정 IP가 필요합니다.
> Cloud Run에 VPC 커넥터 + Cloud NAT로 고정 IP 설정이 필요합니다.
> 이 부분은 STEP 10에서 상세히 안내합니다.
>
> 참고: https://cloud.google.com/run/docs/quickstarts/build-and-deploy?hl=ko

---

### STEP 11: Firebase Hosting 배포 (프론트엔드)

```powershell
cd C:\Users\jgkim\maehong-JG

# Firebase 초기화
firebase init hosting

# 배포
firebase deploy --only hosting
```

> 참고: https://firebase.google.com/docs/hosting/quickstart?hl=ko

---

### STEP 12: 기능 구현

#### 12-1. Google 로그인
- Firebase Auth SDK로 로그인 버튼 구현
- 로그인한 사용자만 챗봇 사용 가능
- 회사 도메인(@maehong.kr) 제한 가능

#### 12-2. 대화 히스토리 저장
- 각 질의응답을 Firestore에 자동 저장
- 좌측 사이드바에 과거 대화 목록 표시
- 대화 클릭 시 이전 대화 불러오기

#### 12-3. 대화 공유
- "공유" 버튼 클릭 → 공유 링크 생성
- 링크를 받은 사람은 읽기 전용으로 대화 확인
- Firestore `shared` 컬렉션에 저장

#### 12-4. 엑셀 업로드 → Cloud Storage
- 업로드된 파일을 Cloud Storage에 저장
- Cloud Run에서 Storage의 파일을 읽어서 처리

---

### STEP 13: 도메인 연결 (선택)

회사 도메인(예: chatbot.maehong.kr)을 Firebase Hosting에 연결:

1. Firebase 콘솔 → Hosting → **커스텀 도메인**
2. `chatbot.maehong.kr` 입력
3. DNS에 TXT/A 레코드 추가 (도메인 관리자에서)

> 참고: https://firebase.google.com/docs/hosting/custom-domain?hl=ko

---

## 예상 비용 (월간)

| 항목 | 무료 범위 | 예상 사용량 | 비용 |
|------|----------|-----------|------|
| Cloud Run | 200만 요청 | ~5만 요청 | **무료** |
| Firestore | 5만 읽기/일 | ~1만/일 | **무료** |
| Storage | 5GB | ~1GB | **무료** |
| Auth | 무제한 | ~50명 | **무료** |
| Hosting | 10GB/월 | ~1GB | **무료** |
| OpenAI GPT-4o | - | ~3만 토큰/일 | **~3만원/월** |
| **합계** | | | **~3만원/월** (OpenAI만) |

---

## 진행 순서 요약

| 순서 | 작업 | 소요시간 | 비고 |
|------|------|---------|------|
| 1 | Firebase 프로젝트 생성 | 5분 | 웹 콘솔 |
| 2 | Blaze 요금제 업그레이드 | 5분 | 신용카드 필요 |
| 3 | Firebase CLI 설치 | 10분 | Node.js 필요 |
| 4 | Firebase 로그인 | 2분 | |
| 5 | Google 로그인 설정 | 5분 | 웹 콘솔 |
| 6 | Firestore 생성 | 3분 | 웹 콘솔 |
| 7 | Cloud Storage 설정 | 3분 | 웹 콘솔 |
| 8 | 코드 구조 변경 | Claude 작업 | "STEP 8 진행해줘" |
| 9 | Docker 설치 | 15분 | |
| 10 | Cloud Run 배포 | Claude 작업 | "STEP 10 진행해줘" |
| 11 | Hosting 배포 | Claude 작업 | "STEP 11 진행해줘" |
| 12 | 기능 구현 | Claude 작업 | "STEP 12 진행해줘" |
| 13 | 도메인 연결 (선택) | 10분 | DNS 설정 |

---

## 시작하기

**STEP 1~7**은 직접 웹 콘솔에서 진행해 주세요.
완료 후 **"STEP 8 진행해줘"**라고 말하면 코드 변환을 시작합니다.

각 STEP이 끝날 때마다 알려주시면 다음 단계를 안내해 드리겠습니다.

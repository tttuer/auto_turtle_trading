# Auto Turtle Trading Bot 🐢

한국투자증권(KIS) OpenAPI와 Python(`uv`)을 활용하여 리눅스/Mac 환경에서 24시간 돌아가는 **터틀 트레이딩(System 1)** 자동매매 봇입니다.

## 📈 매매 전략 (Turtle Trading - System 1)
- **진입**: 최근 20일 최고가 돌파 시 매수 (1 Unit)
- **피라미딩(불타기)**: 진입 후 가격이 `0.5N(변동성)` 상승할 때마다 1 Unit씩 추가 매수 (최대 4 Unit)
- **손절매**: 마지막 진입가 대비 `2N` 하락 시 전량 매도
- **청산**: 최근 10일 최저가 이탈 시 전량 매도
- **자금 관리**: 현재 총 평가 자산(잔액+주식평가액)의 1% Risk에 맞춰 매일 1 Unit 수량을 동적으로 재계산

## 🚀 다른 PC에서 설치 및 실행 방법

### 1. 사전 준비
이 프로젝트는 매우 빠르고 가벼운 파이썬 패키지 매니저인 `uv`를 사용합니다.
- [uv 설치 가이드](https://docs.astral.sh/uv/)에 따라 `uv`를 먼저 설치해 주세요.

### 2. 저장소 복제 (Clone)
```bash
git clone https://github.com/tttuer/auto_turtle_trading.git
cd auto_turtle_trading
```

### 3. 환경 변수 세팅 (API Key)
`.env.example` 파일을 복사하여 `.env` 파일을 생성하고, 한국투자증권에서 발급받은 API 키를 입력합니다.
> **주의**: `.env` 파일은 Github에 올라가지 않도록 `.gitignore`에 등록되어 있습니다. 다른 PC에서 받을 때마다 이 작업을 해주어야 합니다.

```bash
cp .env.example .env
nano .env  # 본인의 App Key, App Secret, 계좌번호 입력
```

```dotenv
# .env 파일 예시
KIS_APP_KEY=본인의_앱키
KIS_APP_SECRET=본인의_앱시크릿
KIS_ACCOUNT_NO=12345678-01
MODE=VIRTUAL  # 실전 투자는 REAL 로 변경
LOG_LEVEL=INFO
```

### 4. 프로그램 실행
`uv`를 사용하면 별도의 가상환경 세팅 없이 아래 명령어 한 줄로 의존성 패키지가 자동 설치되고 봇이 실행됩니다.

```bash
uv run main.py
```

- 리눅스 서버 등에서 24시간 백그라운드로 돌리실 경우 `tmux`나 `nohup uv run main.py &` 방식을 권장합니다.

## 📂 주요 파일 설명
- `main.py`: 프로그램 진입점. 스케줄러 및 실시간 웹소켓(현재가 수신) 데몬.
- `strategy.py`: 터틀 트레이딩의 핵심 알고리즘(N값, Unit 사이즈, 피라미딩/손절/청산 로직).
- `kis_api.py`: 한국투자증권 REST API 통신 모듈.
- `config.py`: 환경 변수 로드 및 설정 관리.
- `universe.json`: 봇이 감시하고 거래할 대상 주식/ETF 종목 코드 목록.

## ⚠️ 주의사항
- **모의투자 운영 시간**: 모의투자 서버도 실제 주식 시장 시간(평일 09:00 ~ 15:30)과 똑같이 동작합니다. 장이 열리지 않는 밤이나 주말에는 가격 데이터가 수신되지 않습니다.
- **API 호출 제한 (TPS)**: 한국투자증권 API는 1분에 1회만 토큰 발급이 가능하며, 초당 호출 횟수(TPS) 제한이 있습니다. 에러(`EGW00133` 등)가 발생하면 1분 뒤에 다시 실행해 주세요.
- **잔고 기록**: 매 1시간마다 터미널과 `balance_history.csv` 파일에 현재 계좌 잔고가 기록되어 추이를 추적할 수 있습니다.

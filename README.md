# TradingView 기반 자동매매 서버

TradingView 전략 시그널로만 진입하고, 익절/손절은 서버가 판단하는 FastAPI 기반 자동매매 서버입니다.  
단일 포지션만 허용하며, Upbit 현물 시장만 지원합니다.

## 주요 기능
- TradingView 웹훅 수신 및 signal_id 중복 방지
- Upbit 시장가 진입/청산 (체결 확인 포함)
- TP/SL 감시 루프
- 잔고 조회 API 및 간단한 모니터링 UI 제공
- 오류/재시도 로직 및 로그 출력

## 요구사항
- Python 3.11+
- Upbit API 키 (Access/Secret)

## 설치 및 실행
```bash
python3.11 -m pip install -r requirements.txt
python3.11 -m uvicorn main:app --reload
```

## Docker 실행
```bash
docker build -t pine-slave .
docker run --rm -p 8000:8000 --env-file .env pine-slave
```

## Docker Compose 실행 (대안)
```bash
docker compose up --build
```

## 환경 변수 (.env)
`.env` 파일에 아래 키를 설정하세요.

```ini
UP_BIT_ACCESS_KEY=...
UP_BIT_SECRET_KEY=...
```

선택 설정:
- `MIN_ORDER_KRW` (기본 5000)
- `PRICE_POLL_SEC` (기본 1.0)
- `ORDER_RETRY_ATTEMPTS` (기본 3)
- `RECOVERY_MARKET` 예: `KRW-BTC` (기존 보유를 복구하고 싶을 때만 사용)
- `RECOVERY_TP`, `RECOVERY_SL` (복구 포지션용)
- `RECOVERY_SKIP=1` (복구 무조건 스킵)

## 엔드포인트
- `POST /webhook/tradingview` TradingView 웹훅
- `GET /status` 서버/포지션 상태
- `GET /account/balances` Upbit 자산 조회 (성공조건 확인용)
- `GET /` 모니터링 UI

## TradingView 웹훅 JSON 예시
```json
{
  "market": "KRW-BTC",
  "action": "BUY",
  "price": 10000,
  "tp": 0.015,
  "sl": 0.01,
  "signal_id": "{{strategy.order.id}}",
  "timeframe": "15m",
  "sent_at": "{{timenow}}"
}
```

필수 필드: `market`, `action`, `price`, `tp`, `sl`, `signal_id`

## 운영 메모
- 단일 포지션만 허용합니다.
- 기본 동작은 **기존 보유 포지션을 무시**합니다.
  - 복구가 필요하면 `RECOVERY_MARKET`/`RECOVERY_TP`/`RECOVERY_SL`을 설정하세요.
- 주문/청산/체결/에러 로그는 서버 로그에서 확인합니다.

## 확인 방법
- UI: `http://127.0.0.1:8000/`
- 자산 조회: `http://127.0.0.1:8000/account/balances`

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

X(트위터) 데이터 수집 시스템 - Nitter 인스턴스를 활용하여 특정 계정의 트윗을 자동 수집합니다.
- 구조: 2개 소스 파일 (crawler.py + main.py)
- 주기: 12시간 간격 자동 실행 또는 수동 1회 실행
- 환경: Python 3.10+, Windows/Linux 호환

## Architecture

### 통합 모듈 구조 (crawler.py)

```
crawler.py 구성 (순서대로):
1. 전역 설정 (환경변수 기반, 기본값 내장)
   - TARGET_ACCOUNTS, SEARCH_MODE, MAX_TWEETS 등
   - 환경변수가 없으면 코드 내 기본값 사용

2. 재시도 (지수 백오프)
   - withExponentialBackoff(): 최소 3회 재시도 (5s→10s→20s)
   - RetryError: 모든 재시도 실패 시 발생

3. Anti-bot (세션·헤더·딜레이)
   - AntiBot: 랜덤 UA, 브라우저 호환 헤더, 요청 간 딜레이
   - randomDelay(): 1~3초 랜덤 딜레이
   - rotateIdentity(): UA/쿠키 교체 (인스턴스 실패 시 호출)

4. Nitter 인스턴스 관리
   - InstanceManager: 인스턴스 조회/헬스체크/우선순위 관리
   - refresh(): 매 실행 시 인스턴스 목록 갱신
   - _checkAllHealth(): 응답 속도순 정렬 (빠른 순부터)
   - reportFailure(): 실패 인스턴스 기록 (다음 사이클에 제외)

5. HTML 파싱
   - TweetParser: Nitter HTML → 트윗 데이터 추출
   - parse_timeline(): 트윗/스레드 분류
   - _extractTweet(): BeautifulSoup으로 트윗 정보 파싱
     (작성자, 본문, 날짜, 미디어, 통계 등)

6. 저장
   - Storage: JSON 파일 저장
   - 경로: ./data/{username}/{username}_{YYYYMMDD_HHMMSS}.json

7. 크롤링 핵심
   - XCrawler: 인스턴스 순차 시도 → 지수 백오프 → 3회 반복
   - crawl(): 특정 계정 크롤링
   - _crawlSingleInstance(): ntscraper 시도 → 빈 결과 시 Playwright 폴백
   - _crawlWithNtscraper(): ntscraper로 크롤링
   - _crawlWithPlaywright(): JS 렌더링 + Anubis 챌린지 감지/해결
```

### 흐름 (main.py)

```
main.py 실행 흐름:
1. setupLogging(): 파일 + 콘솔 로깅 (Windows cp949 인코딩 문제 회피)
2. runCrawlJob(): 크롤링 1회 실행
   ├─ instanceManager.refresh(): 활성 인스턴스 갱신
   ├─ 각 계정별:
   │  ├─ crawler.crawl(account): 크롤링
   │  ├─ storage.save(account, result): 저장
   │  └─ 통계 로깅
   └─ 최종 통계 출력
3. 실행 모드:
   ├─ --once: 1회만 실행 후 종료
   └─ (기본): CrawlScheduler로 12시간 간격 반복
```

## Running

### 설정

환경변수는 코드에 기본값이 내장되어 있으므로 필수가 아님.
필요시 shell에서 오버라이드:

```bash
# 수집 대상 계정
export TARGET_ACCOUNTS="account1,account2"

# 크롤링 설정
export MAX_TWEETS=50
export PLAYWRIGHT_WAIT_SEC=10

# 스케줄 간격
export SCHEDULE_INTERVAL_HOURS=6

# 재시도/Anti-bot
export RETRY_MAX_ATTEMPTS=5
export REQUEST_DELAY_MIN=2.0
export REQUEST_DELAY_MAX=5.0

# 저장 경로
export DATA_DIR=/custom/data/path
export LOG_DIR=/custom/log/path
```

### 명령어

```bash
# 1회만 실행 후 종료
python main.py --once

# 1회만 실행 (특정 계정)
python main.py --once --accounts account1 account2

# 스케줄러 모드 (12시간 간격, 즉시 1회 실행)
python main.py

# 스케줄러 모드 (특정 계정만)
python main.py --accounts account1 account2
```

### 의존성 설치

```bash
pip install -r requirements.txt
playwright install chromium firefox  # Playwright 브라우저 설치 필수
```

## Key Design Decisions

### 1. 단일 모듈 통합 (crawler.py)
- 원래 config/ + src/ (11개 파일) → 2개 파일 (crawler.py + main.py)
- 이유: 수집 시스템의 일부로 통합되므로 관리 편의성 우선
- 환경변수는 .env 파일 대신 os.getenv()로 직접 처리

### 2. 인스턴스 헬스체크 기반 필터링
- SKIP/JS_REQUIRED 설정 제거 (dead code)
- 헬스체크에서 비정상 인스턴스 자동 제외
- 응답 속도순 정렬로 우선순위 결정

### 3. ntscraper → Playwright 폴백
- ntscraper: 빠르지만 Nitter 봇 방지에 취약
- Playwright: JS 렌더링 + Anubis 챌린지 감지/해결
- 빈 결과 시 Playwright로 자동 재시도

### 4. 지수 백오프 재시도
- 전체 인스턴스 순환을 3회 반복
- 각 시도마다 5s → 10s → 20s 대기
- 한 인스턴스 실패 시 다음 인스턴스 즉시 시도

### 5. Anti-bot 전략
- 랜덤 User-Agent + 요청 간 딜레이 (1~3초)
- 인스턴스 실패 시 세션 교체 (새로운 UA/쿠키)
- 검색 엔드포인트만 사용 (프로필 페이지는 Anubis 차단)

## 로그 해석

```
크롤링 성공:
[crawler] - 활성 인스턴스 3개 확인
[crawler] - 'account' 크롤링 시작 (인스턴스 3개)
[crawler] -   [1/3] https://... 시도 중...
[main] - 'account' 수집 완료: 트윗 20개, 스레드 0개

ntscraper 빈 결과 → Playwright 폴백:
[root] WARNING - Empty page on https://...
[crawler] WARNING -     ntscraper 빈 결과 - Playwright 검색 모드로 폴백
[crawler] INFO -     Playwright 요청: https://...
[crawler] INFO -     Anubis 봇 방지 감지 - 챌린지 해결 대기...

모든 인스턴스 실패:
[main] ERROR - 'account' 실패 (전체 재시도 소진): ...
```

## Common Tasks

### 수집 대상 계정 변경
```python
# crawler.py 상단 설정 직접 수정 또는
export TARGET_ACCOUNTS="newaccount1,newaccount2"
python main.py --once
```

### 최대 트윗 수 증가
```python
# crawler.py
MAX_TWEETS: int = int(os.getenv("MAX_TWEETS", "50"))  # 기본값 50으로 변경
```

### Nitter 인스턴스 추가
```python
# crawler.py FALLBACK_INSTANCES에 URL 추가
FALLBACK_INSTANCES: list[str] = [
  "https://nitter.new-instance.com",
  # ...
]
```

### 로그 레벨 변경 (DEBUG 모드)
```bash
export LOG_LEVEL=DEBUG
python main.py --once
```

## Important Notes

1. **Windows 콘솔 인코딩**
   - cp949 인코딩 문제 해결: `io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")`
   - 로그 파일은 UTF-8, 콘솔은 UTF-8 fallback with replace

2. **Playwright 설치**
   - `playwright install chromium firefox` 필수
   - Chromium 실패 시 Firefox로 자동 재시도

3. **Nitter 인스턴스 현황 (2026-02-09)**
   - 대부분 403 반환 (봇 방지 강화됨)
   - `nitter.tiekoetter.com`, `nitter.privacyredirect.com` 정도만 200 응답
   - LibRedirect API에서 동적으로 조회 (폴백 목록 병행)

4. **데이터 저장 구조**
   - 경로: `./data/{username}/{username}_{YYYYMMDD_HHMMSS}.json`
   - 한 파일에 tweets, threads, meta 통합 저장
   - 계정별 폴더로 자동 구분

5. **메모리 누수 주의**
   - requestSession.close()를 명시적으로 호출해야 함 (rotateIdentity에서 수행)
   - Playwright 페이지는 browser.close()로 정리

"""
X(트위터) 데이터 수집 시스템
Nitter 인스턴스를 활용하여 특정 계정의 트윗을 자동 수집합니다.

구조: 전역 설정 → 재시도 → Anti-bot → 인스턴스 관리 → HTML 파싱 → 저장 → 크롤링 핵심
실행: python x_crawler.py [--once] [--accounts account1 account2 ...]
"""

import argparse
import io
import json
import logging
import os
import random
import signal
import sys
import time
from base64 import b64decode
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import quote, unquote, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from fake_useragent import UserAgent
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

# ntscraper 임포트 전에 stderr 리디렉트
_oldStderr = sys.stderr
sys.stderr = io.StringIO()
try:
  from ntscraper import Nitter
finally:
  sys.stderr = _oldStderr


# ============================================================
# 전역 설정 (환경변수로 오버라이드 가능)
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent

# === 수집 대상 계정 ===
TARGET_ACCOUNTS: list[str] = [
  a.strip().strip("'\"")
  for a in os.getenv("TARGET_ACCOUNTS", "HalcyonAi,DarkWebInformer,MonThreat,DailyDarkWeb,FalconFeedsio,H4ckManac").split(",")
  if a.strip()
]

# === 크롤링 설정 ===
SEARCH_MODE: str = os.getenv("SEARCH_MODE", "user")
MAX_TWEETS: int = int(os.getenv("MAX_TWEETS", "20"))
PLAYWRIGHT_WAIT_SEC: int = int(os.getenv("PLAYWRIGHT_WAIT_SEC", "8"))

# === 스케줄링 설정 ===
SCHEDULE_INTERVAL_HOURS: float = float(os.getenv("SCHEDULE_INTERVAL_HOURS", "12"))

# === 재시도 설정 ===
RETRY_MAX_ATTEMPTS: int = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY: float = float(os.getenv("RETRY_BASE_DELAY", "5"))

# === Anti-bot 설정 ===
REQUEST_DELAY_MIN: float = float(os.getenv("REQUEST_DELAY_MIN", "1.0"))
REQUEST_DELAY_MAX: float = float(os.getenv("REQUEST_DELAY_MAX", "3.0"))

# === 저장 경로 ===
DATA_DIR: Path = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")))
LOG_DIR: Path = Path(os.getenv("LOG_DIR", str(PROJECT_ROOT / "logs")))

# === 로깅 설정 ===
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# === Nitter 인스턴스 설정 ===
INSTANCES_API: str = (
  "https://raw.githubusercontent.com/libredirect/instances/main/data.json"
)

FALLBACK_INSTANCES: list[str] = [
  "https://xcancel.com",
  "https://nitter.tiekoetter.com",
  "https://nitter.poast.org",
  "https://nitter.space",
  "https://nuku.trabun.org",
  "https://nitter.catsarch.com",
]

INSTANCE_HEALTH_TIMEOUT: int = int(os.getenv("INSTANCE_HEALTH_TIMEOUT", "5"))

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ============================================================
# 재시도 (지수 백오프)
# ============================================================

@dataclass
class AttemptRecord:
  """개별 재시도 기록."""
  attemptNumber: int
  error: Exception
  delay: float


class RetryError(Exception):
  """모든 재시도가 실패했을 때 발생하는 예외."""

  def __init__(self, message: str, attempts: list[AttemptRecord]):
    self.attempts = attempts
    super().__init__(
      f"{message} (총 {len(attempts)}회 시도, "
      f"마지막 오류: {attempts[-1].error if attempts else 'N/A'})"
    )


def withExponentialBackoff(
  func: Callable[..., T],
  maxAttempts: int = RETRY_MAX_ATTEMPTS,
  baseDelay: float = RETRY_BASE_DELAY,
) -> T:
  """
  지수 백오프로 함수를 재시도합니다.

  Args:
    func: 실행할 함수 (인자 없이 호출)
    maxAttempts: 최대 재시도 횟수 (최소 3)
    baseDelay: 초기 대기 시간 (초)

  Returns:
    func의 반환값

  Raises:
    RetryError: 모든 재시도 실패 시
  """
  maxAttempts = max(maxAttempts, 3)
  attempts: list[AttemptRecord] = []

  for attempt in range(1, maxAttempts + 1):
    try:
      result = func()
      return result
    except Exception as e:
      delay = baseDelay * (2 ** (attempt - 1))
      record = AttemptRecord(
        attemptNumber=attempt,
        error=e,
        delay=delay,
      )
      attempts.append(record)

      if attempt < maxAttempts:
        time.sleep(delay)

  raise RetryError("모든 재시도 실패", attempts)


# ============================================================
# Anti-bot (세션·헤더·딜레이 관리)
# ============================================================

class AntiBot:
  """Bot 탐지 우회를 위한 세션·헤더·딜레이 관리 클래스."""

  def __init__(
    self,
    delayMin: float = REQUEST_DELAY_MIN,
    delayMax: float = REQUEST_DELAY_MAX,
  ):
    self._delayMin = delayMin
    self._delayMax = delayMax
    self._ua = UserAgent()
    self._session = self._createSession()

  @property
  def userAgent(self) -> str:
    """현재 세션의 User-Agent를 반환합니다."""
    return self._session.headers.get("User-Agent", "")

  def _createSession(self) -> requests.Session:
    """랜덤 UA와 브라우저 호환 헤더를 갖춘 세션을 생성합니다."""
    session = requests.Session()
    ua = self._ua.random
    session.headers.update({
      "User-Agent": ua,
      "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/webp,*/*;q=0.8"
      ),
      "Accept-Language": "en-US,en;q=0.9",
      "Accept-Encoding": "gzip, deflate, br",
      "Connection": "keep-alive",
      "Upgrade-Insecure-Requests": "1",
      "Cache-Control": "max-age=0",
    })
    return session

  def randomDelay(self) -> None:
    """요청 간 랜덤 딜레이를 적용합니다."""
    delay = random.uniform(self._delayMin, self._delayMax)
    time.sleep(delay)

  def rotateIdentity(self) -> None:
    """UA와 쿠키를 교체하여 새로운 세션을 생성합니다."""
    self._session.close()
    self._session = self._createSession()


# ============================================================
# Nitter 인스턴스 관리
# ============================================================

@dataclass
class InstanceInfo:
  """Nitter 인스턴스 상태 정보."""
  url: str
  isHealthy: bool = False
  responseTime: float = float("inf")


class InstanceManager:
  """Nitter 인스턴스 조회, 헬스체크, 우선순위 관리 클래스."""

  def __init__(self):
    self._instances: list[InstanceInfo] = []
    self._failedInstances: set[str] = set()

  @property
  def workingInstances(self) -> list[str]:
    """활성 인스턴스 URL 목록 (응답 속도순)."""
    return [
      inst.url
      for inst in self._instances
      if inst.isHealthy and inst.url not in self._failedInstances
    ]

  def refresh(self) -> list[str]:
    """
    인스턴스 목록을 갱신하고 헬스체크를 수행합니다.

    Returns:
      활성 인스턴스 URL 목록
    """
    self._failedInstances.clear()

    rawInstances = self._fetchInstanceList()
    self._instances = self._checkAllHealth(rawInstances)

    working = self.workingInstances
    logger.info("활성 인스턴스: %d개", len(working))
    return working

  def reportFailure(self, instanceUrl: str) -> None:
    """인스턴스 실패를 기록합니다."""
    self._failedInstances.add(instanceUrl)

  def _fetchInstanceList(self) -> list[str]:
    """LibRedirect API에서 인스턴스 목록을 가져옵니다."""
    try:
      r = requests.get(INSTANCES_API, timeout=10)
      if r.ok:
        data = r.json()
        instances = data.get("nitter", {}).get("clearnet", [])
        if instances:
          logger.info("LibRedirect API에서 %d개 인스턴스 조회", len(instances))
          return instances
    except Exception as e:
      logger.warning("인스턴스 API 조회 실패: %s - 폴백 목록 사용", e)

    return FALLBACK_INSTANCES.copy()

  def _checkAllHealth(self, instanceUrls: list[str]) -> list[InstanceInfo]:
    """모든 인스턴스의 헬스체크를 수행하고 응답 속도순으로 정렬합니다."""
    results: list[InstanceInfo] = []

    for url in instanceUrls:
      info = self._checkHealth(url)
      if info.isHealthy:
        results.append(info)

    results.sort(key=lambda x: x.responseTime)
    return results

  def _checkHealth(self, instanceUrl: str) -> InstanceInfo:
    """단일 인스턴스 헬스체크 + 응답 시간 측정."""
    info = InstanceInfo(url=instanceUrl)
    try:
      start = time.time()
      r = requests.get(
        f"{instanceUrl}/x",
        timeout=INSTANCE_HEALTH_TIMEOUT,
        headers={
          "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "Gecko/20100101 Firefox/129.0"
          )
        },
      )
      elapsed = time.time() - start
      info.isHealthy = r.ok
      info.responseTime = elapsed
    except Exception:
      info.isHealthy = False

    return info


# ============================================================
# HTML 파싱 (트윗 데이터 추출)
# ============================================================

class TweetParser:
  """Nitter HTML에서 트윗 데이터를 파싱하는 클래스."""

  def parse_timeline(
    self, html: str, maxTweets: int = -1
  ) -> tuple[list[dict], list[dict]]:
    """
    타임라인 HTML에서 트윗과 스레드를 추출합니다.

    Args:
      html: 렌더링된 HTML 문자열
      maxTweets: 최대 트윗 수 (-1이면 제한 없음)

    Returns:
      (tweets, threads) 튜플
    """
    soup = BeautifulSoup(html, "lxml")
    timelineItems = soup.find_all("div", class_="timeline-item")

    tweets: list[dict] = []
    threads: list[dict] = []
    isEncrypted = "/enc/" in html

    for item in timelineItems:
      classes = item.get("class", [])
      if len(classes) == 1:
        tweet = self._extractTweet(item, isEncrypted)
        if tweet:
          tweets.append(tweet)
          if maxTweets > 0 and len(tweets) >= maxTweets:
            break
      elif "thread" in classes and len(classes) == 3:
        tweet = self._extractTweet(item, isEncrypted)
        if tweet:
          threads.append([tweet])

    return tweets, threads

  def _extractTweet(
    self, tweetDiv: Tag, isEncrypted: bool = False
  ) -> dict | None:
    """BeautifulSoup 트윗 div에서 트윗 데이터를 추출합니다."""

    def safeText(el: Tag | None, default: str = "") -> str:
      return el.text.strip() if el else default

    def safeAttr(el: Tag | None, attr: str, default: str = "") -> str:
      return el.get(attr, default) if el and el.has_attr(attr) else default

    try:
      fullname = tweetDiv.find("a", class_="fullname")
      username = tweetDiv.find("a", class_="username")
      if not fullname or not username:
        return None

      avatarTag = tweetDiv.find("img", class_="avatar")
      avatar = (
        "https://abs.twimg.com/sticky/default_profile_images/"
        "default_profile_normal.png"
      )
      if avatarTag and avatarTag.has_attr("src"):
        try:
          if isEncrypted and "/enc/" in avatarTag["src"]:
            avatar = (
              "https://pbs.twimg.com/"
              + b64decode(
                avatarTag["src"].split("/")[-1].encode("utf-8")
              ).decode("utf-8").split("?")[0]
            )
          else:
            avatar = unquote(avatarTag["src"])
        except Exception:
          pass

      profileId = (
        avatar.split("/profile_images/")[1].split("/")[0]
        if "profile_images" in avatar
        else "unknown"
      )

      content = tweetDiv.find("div", class_="tweet-content")
      if content:
        textEl = content.find("div", class_="media-body")
        text = safeText(textEl or content)
      else:
        quoteText = tweetDiv.find("div", class_="quote-text")
        text = safeText(quoteText)
      text = text.replace("\n", " ").strip()

      dateSpan = tweetDiv.find("span", class_="tweet-date")
      link = ""
      dateStr = ""
      if dateSpan and dateSpan.find("a"):
        a = dateSpan.find("a")
        link = "https://twitter.com" + safeAttr(a, "href")
        dateStr = safeAttr(a, "title")

      tweetId = urlparse(link).path.rsplit("/", 1)[-1] if link else ""

      statsDivs = tweetDiv.find_all("span", class_="tweet-stat")

      def statVal(i: int, default: int = 0) -> int:
        if i < len(statsDivs):
          d = statsDivs[i].find("div")
          if d:
            try:
              return int(d.text.strip().replace(",", "") or 0)
            except ValueError:
              pass
        return default

      pictures: list[str] = []
      videos: list[str] = []
      gifs: list[str] = []
      attachments = tweetDiv.find("div", class_="tweet-body")
      if attachments:
        attachments = attachments.find(
          "div", class_="attachments", recursive=False
        )
      if attachments:
        for img in attachments.find_all("img", recursive=False):
          if img.has_attr("src"):
            try:
              src = img["src"]
              if isEncrypted and "/enc/" in src:
                picsUrl = (
                  "https://pbs.twimg.com/"
                  + b64decode(src.split("/")[-1].encode("utf-8"))
                  .decode("utf-8")
                  .split("?")[0]
                )
              elif "/pic" in src:
                picsUrl = (
                  "https://pbs.twimg.com"
                  + unquote(src.split("/pic")[1]).split("?")[0]
                )
              else:
                picsUrl = (
                  unquote(src)
                  if src.startswith("http")
                  else ("https:" + unquote(src))
                )
              pictures.append(picsUrl)
            except Exception:
              pass

        for v in attachments.find_all("video", class_="gif"):
          if v.find("source") and v.find("source").has_attr("src"):
            try:
              src = v.find("source")["src"]
              if isEncrypted and "/enc/" in src:
                gifs.append(
                  "https://"
                  + b64decode(src.split("/")[-1].encode("utf-8")).decode(
                    "utf-8"
                  )
                )
              else:
                gifs.append(unquote("https://" + src.split("/pic/")[1]))
            except Exception:
              pass

        for v in attachments.find_all("video", class_=""):
          if v.has_attr("data-url"):
            try:
              url = unquote("https" + v["data-url"].split("https")[1])
              videos.append(url)
            except Exception:
              pass
          elif v.find("source") and v.find("source").has_attr("src"):
            try:
              videos.append(unquote(v.find("source")["src"]))
            except Exception:
              pass

      replying: list[str] = []
      repDiv = tweetDiv.find("div", class_="replying-to")
      if repDiv:
        replying = [a.text.strip() for a in repDiv.find_all("a")]

      return {
        "id": tweetId,
        "link": link,
        "text": text,
        "user": {
          "name": safeText(fullname),
          "username": safeText(username),
          "profile_id": profileId,
          "avatar": avatar,
        },
        "date": dateStr,
        "is-retweet": tweetDiv.find("div", class_="retweet-header") is not None,
        "is-pinned": tweetDiv.find("div", class_="pinned") is not None,
        "external-link": safeAttr(
          tweetDiv.find("a", class_="card-container"), "href"
        ),
        "replying-to": replying,
        "quoted-post": {},
        "stats": {
          "comments": statVal(0),
          "retweets": statVal(1),
          "quotes": statVal(2),
          "likes": statVal(3),
        },
        "pictures": pictures,
        "videos": videos,
        "gifs": gifs,
      }
    except Exception as e:
      logger.debug("트윗 파싱 실패: %s", e)
      return None


# ============================================================
# 데이터 저장
# ============================================================

class Storage:
  """수집 데이터를 JSON 파일로 저장하는 클래스."""

  def __init__(self, baseDir: Path | None = None):
    self._baseDir = baseDir or DATA_DIR

  def save(self, username: str, data: dict) -> Path:
    """
    수집 결과를 JSON 파일로 저장합니다.

    Args:
      username: 수집 대상 계정명
      data: 저장할 데이터 딕셔너리

    Returns:
      저장된 파일 경로
    """
    accountDir = self._baseDir / username
    accountDir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{username}_{timestamp}.json"
    filepath = accountDir / filename

    with open(filepath, "w", encoding="utf-8") as f:
      json.dump(data, f, ensure_ascii=False, indent=2)

    return filepath


# ============================================================
# 크롤링 핵심 (예외, 결과, XCrawler)
# ============================================================

class CrawlError(Exception):
  """크롤링 관련 기본 예외."""


class NoInstanceError(CrawlError):
  """사용 가능한 인스턴스가 없을 때 발생."""


class AllInstancesFailedError(CrawlError):
  """모든 인스턴스에서 크롤링이 실패했을 때 발생."""


@dataclass
class CrawlResult:
  """크롤링 결과를 구조화하는 데이터 클래스."""
  username: str
  tweets: list[dict] = field(default_factory=list)
  threads: list[dict] = field(default_factory=list)
  meta: dict = field(default_factory=dict)

  @property
  def tweetCount(self) -> int:
    return len(self.tweets)

  @property
  def isEmpty(self) -> bool:
    return self.tweetCount == 0 and len(self.threads) == 0

  def toDict(self) -> dict:
    """저장용 딕셔너리로 변환합니다."""
    return {
      "username": self.username,
      "tweets": self.tweets,
      "threads": self.threads,
      "meta": self.meta,
    }


class XCrawler:
  """
  X(트위터) 크롤러 핵심 클래스.
  인스턴스 순차 시도 -> 모두 실패 시 지수 백오프 -> 3회 반복.
  """

  def __init__(
    self,
    instanceManager: InstanceManager,
    antiBot: AntiBot,
    parser: TweetParser,
    maxTweets: int = MAX_TWEETS,
    searchMode: str = SEARCH_MODE,
  ):
    self._instanceMgr = instanceManager
    self._antiBot = antiBot
    self._parser = parser
    self._maxTweets = maxTweets
    self._searchMode = searchMode

  def crawl(self, username: str) -> CrawlResult:
    """
    특정 계정의 트윗을 수집합니다.
    지수 백오프로 전체 인스턴스 순환을 최대 3회 반복합니다.

    Args:
      username: 수집 대상 X 계정명

    Returns:
      CrawlResult

    Raises:
      NoInstanceError: 활성 인스턴스가 없을 때
      RetryError: 3회 재시도 모두 실패 시
    """
    instances = self._instanceMgr.workingInstances
    if not instances:
      raise NoInstanceError("사용 가능한 Nitter 인스턴스가 없습니다")

    def attemptAllInstances() -> CrawlResult:
      return self._tryAllInstances(username, instances)

    return withExponentialBackoff(func=attemptAllInstances)

  def _tryAllInstances(
    self, username: str, instances: list[str]
  ) -> CrawlResult:
    """모든 인스턴스를 순차적으로 시도합니다."""
    lastError: Exception | None = None

    for idx, instance in enumerate(instances, 1):
      try:
        result = self._crawlSingleInstance(username, instance)

        if result.isEmpty and idx < len(instances):
          continue

        result.meta.update({
          "instance_tried": idx,
          "total_instances": len(instances),
        })
        return result

      except Exception as e:
        lastError = e
        self._instanceMgr.reportFailure(instance)
        self._antiBot.rotateIdentity()
        continue

    raise AllInstancesFailedError(
      f"'{username}': 모든 인스턴스 실패"
    )

  def _crawlSingleInstance(
    self, username: str, instance: str
  ) -> CrawlResult:
    """단일 인스턴스에서 크롤링을 수행합니다."""
    result = self._crawlWithNtscraper(username, instance)
    if not result.isEmpty:
      return result

    return self._crawlWithPlaywright(username, instance)

  def _crawlWithNtscraper(
    self, username: str, instance: str
  ) -> CrawlResult:
    """ntscraper를 사용하여 크롤링합니다."""
    oldStderr = sys.stderr
    logging.getLogger("ntscraper").setLevel(logging.ERROR)

    try:
      sys.stderr = io.StringIO()

      scraper = Nitter(
        instances=[instance],
        log_level=0,
        skip_instance_check=True,
      )

      rawResult = scraper.get_tweets(
        terms=username,
        mode=self._searchMode,
        number=self._maxTweets,
        instance=instance,
      )

      tweets = rawResult.get("tweets", [])
      threads = rawResult.get("threads", [])

      return CrawlResult(
        username=username,
        tweets=tweets,
        threads=threads,
        meta={
          "crawled_at": datetime.now().isoformat(),
          "instance_used": instance,
          "method": "ntscraper",
          "tweet_count": len(tweets),
          "thread_count": len(threads),
        },
      )
    finally:
      sys.stderr = oldStderr
      logging.getLogger("ntscraper").setLevel(logging.ERROR)

  def _crawlWithPlaywright(
    self, username: str, instance: str
  ) -> CrawlResult:
    """Playwright를 사용하여 JS 렌더링 후 크롤링합니다."""
    try:
      from playwright.sync_api import sync_playwright
    except ImportError:
      raise CrawlError(
        "Playwright 미설치. "
        "pip install playwright && playwright install chromium"
      )

    if self._searchMode == "hashtag":
      endpoint = (
        f"/search?f=tweets&q=%23{quote(username)}&scroll=false"
      )
    else:
      endpoint = (
        f"/search?f=tweets&q={quote(username)}&scroll=false"
      )

    requestUrl = instance.rstrip("/") + endpoint

    def fetchWithBrowser(browserType):
      args = (
        ["--disable-http2"]
        if "chromium" in str(browserType).lower()
        else []
      )
      browser = browserType.launch(headless=True, args=args)
      page = browser.new_page(
        user_agent=self._antiBot.userAgent
      )
      page.set_default_timeout(60000)
      page.goto(requestUrl, wait_until="domcontentloaded")

      deadline = time.time() + 45
      while time.time() < deadline:
        try:
          title = page.title() or ""
        except Exception:
          page.wait_for_timeout(2000)
          continue
        if "Making sure" not in title and "bot" not in title.lower():
          break
        page.wait_for_timeout(2000)

      try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
      except Exception:
        pass
      page.wait_for_timeout(PLAYWRIGHT_WAIT_SEC * 1000)

      try:
        page.wait_for_selector(".timeline-item", timeout=15000)
      except Exception:
        pass
      html = page.content()
      browser.close()
      return html

    with sync_playwright() as p:
      try:
        html = fetchWithBrowser(p.chromium)
      except Exception as e:
        if "ERR_HTTP2" in str(e) or "PROTOCOL_ERROR" in str(e):
          try:
            html = fetchWithBrowser(p.firefox)
          except Exception as fe:
            raise CrawlError(f"Playwright 실패: {fe}") from fe
        else:
          raise CrawlError(f"Playwright 실패: {e}") from e

    tweets, threads = self._parser.parse_timeline(
      html, self._maxTweets
    )

    return CrawlResult(
      username=username,
      tweets=tweets,
      threads=threads,
      meta={
        "crawled_at": datetime.now().isoformat(),
        "instance_used": instance,
        "method": "playwright",
        "tweet_count": len(tweets),
        "thread_count": len(threads),
      },
    )


# ============================================================
# 로깅 설정
# ============================================================

class _SuppressFilter(logging.Filter):
  """특정 메시지를 억제하는 필터."""

  def filter(self, record: logging.LogRecord) -> bool:
    """Empty page 메시지 억제."""
    return "Empty page" not in record.getMessage()


def setupLogging() -> None:
  """파일 + 콘솔 듀얼 핸들러 로깅을 초기화합니다."""
  LOG_DIR.mkdir(parents=True, exist_ok=True)

  logFilename = f"crawler_{datetime.now().strftime('%Y%m%d')}.log"
  logPath = LOG_DIR / logFilename

  fileFormatter = logging.Formatter(
    "[%(asctime)s] %(levelname)-8s %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
  )

  consoleFormatter = logging.Formatter(
    "%(message)s"
  )

  fileHandler = logging.FileHandler(logPath, encoding="utf-8")
  fileHandler.setLevel(logging.DEBUG)
  fileHandler.setFormatter(fileFormatter)

  consoleStream = io.TextIOWrapper(
    sys.stdout.buffer, encoding="utf-8", errors="replace"
  )
  consoleHandler = logging.StreamHandler(consoleStream)
  consoleHandler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
  consoleHandler.setFormatter(consoleFormatter)
  consoleHandler.addFilter(_SuppressFilter())

  rootLogger = logging.getLogger()
  rootLogger.setLevel(logging.DEBUG)

  for handler in rootLogger.handlers[:]:
    rootLogger.removeHandler(handler)

  rootLogger.addHandler(fileHandler)
  rootLogger.addHandler(consoleHandler)

  for appLogger in ["__main__", "__name__"]:
    logging.getLogger(appLogger).setLevel(logging.DEBUG)
    logging.getLogger(appLogger).propagate = True

  for libName in [
    "ntscraper", "urllib3", "requests", "apscheduler",
    "selenium", "playwright", "asyncio", "aiohttp",
    "paramiko", "PIL", "http.client"
  ]:
    logging.getLogger(libName).setLevel(logging.ERROR)
    logging.getLogger(libName).propagate = False

  rootLogger.setLevel(logging.ERROR)


# ============================================================
# 스케줄러
# ============================================================

class CrawlScheduler:
  """크롤링 작업을 주기적으로 실행하는 스케줄러."""

  def __init__(
    self,
    job: Callable[[], None],
    intervalHours: float = SCHEDULE_INTERVAL_HOURS,
    runImmediately: bool = True,
  ):
    self._job = job
    self._intervalHours = intervalHours
    self._runImmediately = runImmediately
    self._scheduler = BlockingScheduler()
    self._setupSignalHandlers()

  def start(self) -> None:
    """스케줄러를 시작합니다."""
    logger.info(
      "스케줄러 시작: %.1f시간 간격으로 실행", self._intervalHours
    )

    if self._runImmediately:
      try:
        self._job()
      except Exception as e:
        logger.error("초기 실행 실패: %s", e)

    self._scheduler.add_job(
      self._scheduleWrapper,
      trigger=IntervalTrigger(hours=self._intervalHours),
      id="crawl_job",
      name="X 크롤링 작업",
      max_instances=1,
    )

    try:
      self._scheduler.start()
    except (KeyboardInterrupt, SystemExit):
      logger.info("스케줄러 종료 요청 수신")

  def _scheduleWrapper(self) -> None:
    """실행 후 다음 예정시간을 표시합니다."""
    self._job()
    try:
      job = self._scheduler.get_job("crawl_job")
      if job and job.next_run_time:
        logger.info(
          "다음 실행 예정: %s",
          job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
        )
    except Exception:
      pass

  def _setupSignalHandlers(self) -> None:
    """Ctrl+C 시그널을 처리하여 graceful shutdown합니다."""

    def handleShutdown(signum, frame):
      logger.info("종료 시그널 수신 (signal=%d) - 스케줄러 종료 중...", signum)
      self._scheduler.shutdown(wait=False)
      sys.exit(0)

    signal.signal(signal.SIGINT, handleShutdown)
    signal.signal(signal.SIGTERM, handleShutdown)


# ============================================================
# 공유 컴포넌트 + 크롤링 작업
# ============================================================

instanceManager = InstanceManager()
antiBot = AntiBot()
parser = TweetParser()
crawler = XCrawler(instanceManager, antiBot, parser)
storage = Storage()


def runCrawlJob(accounts: list[str] | None = None) -> None:
  """
  크롤링 작업을 1회 실행합니다.
  인스턴스 갱신 -> 계정별 크롤링 -> 저장 -> 통계 로깅.
  """
  targetAccounts = accounts or TARGET_ACCOUNTS
  startTime = datetime.now()

  logger.info("크롤링 작업 시작 (%d개 계정)", len(targetAccounts))

  working = instanceManager.refresh()
  if not working:
    logger.error("활성 Nitter 인스턴스가 없습니다. 작업 중단.")
    return

  successCount = 0
  failCount = 0

  for account in targetAccounts:
    instanceManager._failedInstances.clear()

    try:
      result = crawler.crawl(account)
      savedPath = storage.save(account, result.toDict())

      logger.info(
        "  ✓ %s 완료: 트윗 %d개 / 스레드 %d개",
        account,
        result.tweetCount,
        len(result.threads),
      )

      successCount += 1

    except NoInstanceError as e:
      logger.error("  ✗ %s 실패 (인스턴스 없음): %s", account, str(e)[:50])
      failCount += 1
    except (AllInstancesFailedError, RetryError) as e:
      logger.error("  ✗ %s 실패 (재시도 소진): %s", account, str(e)[:50])
      failCount += 1
    except CrawlError as e:
      logger.error("  ✗ %s 크롤링 오류: %s", account, str(e)[:50])
      failCount += 1
    except Exception as e:
      logger.error("  ✗ %s 오류: %s", account, str(e)[:50])
      failCount += 1

  elapsed = (datetime.now() - startTime).total_seconds()
  logger.info(
    "작업 완료: %d/%d 성공 (%.0f초)",
    successCount,
    len(targetAccounts),
    elapsed,
  )


# ============================================================
# CLI 진입점
# ============================================================

def main() -> None:
  """CLI 진입점."""
  argParser = argparse.ArgumentParser(
    description="X(트위터) 데이터 수집 시스템"
  )
  argParser.add_argument(
    "--once",
    action="store_true",
    help="1회만 실행 후 종료",
  )
  argParser.add_argument(
    "--accounts",
    nargs="+",
    help="수집 대상 계정 오버라이드 (공백 또는 쉼표 구분)",
  )
  args = argParser.parse_args()

  setupLogging()

  # 계정 파싱 (쉼표/공백 모두 지원)
  if args.accounts:
    accountStr = " ".join(args.accounts)
    accounts = [a.strip() for a in accountStr.replace(",", " ").split() if a.strip()]
  else:
    accounts = None

  appLogger = logging.getLogger(__name__)
  appLogger.info("X(트위터) 데이터 수집 시스템 시작")
  appLogger.info("대상 계정: %s", ", ".join(accounts or TARGET_ACCOUNTS))

  if args.once:
    runCrawlJob(accounts=accounts)
  else:
    appLogger.info("스케줄러 모드: %.2f시간 간격", SCHEDULE_INTERVAL_HOURS)
    scheduler = CrawlScheduler(
      job=lambda: runCrawlJob(accounts=accounts),
      runImmediately=True,
    )
    scheduler.start()


if __name__ == "__main__":
  main()

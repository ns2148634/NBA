"""全域參數設定。

所有可調參數集中在此模組：

* 檔案路徑與時區
* 30 支球隊的基本資料與隊名比對表
* 基礎戰力分、傷病扣分與賽果預測的模型係數
* 網路請求與快取設定
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# --------------------------------------------------------------------------
# 版本與路徑
# --------------------------------------------------------------------------
SCHEMA_VERSION = 1

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = BASE_DIR / ".cache"

TEAM_RATINGS_PATH = DATA_DIR / "team_ratings.json"
DAILY_PREDICTIONS_PATH = DATA_DIR / "daily_predictions.json"
HISTORY_LOG_PATH = DATA_DIR / "history_log.csv"


# --------------------------------------------------------------------------
# 時區（Windows 需安裝 tzdata，見 requirements.txt）
# --------------------------------------------------------------------------
def _load_timezone(name: str, fallback_hours: float) -> timezone | ZoneInfo:
    """載入 IANA 時區；若系統缺少 tzdata 則退回固定位移。"""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:  # pragma: no cover - 僅在缺少 tzdata 時觸發
        return timezone(timedelta(hours=fallback_hours))


TZ_ET = _load_timezone("America/New_York", -4)
TZ_TAIPEI = _load_timezone("Asia/Taipei", 8)


def now_utc() -> datetime:
    """回傳目前 UTC 時間（timezone-aware）。"""
    return datetime.now(timezone.utc)


def to_iso(moment: datetime | None) -> str | None:
    """將 datetime 轉為 UTC ISO-8601 字串。"""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def to_taipei_iso(moment: datetime | None) -> str | None:
    """將 datetime 轉為台北時區 ISO-8601 字串。"""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(TZ_TAIPEI).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 賽季
# --------------------------------------------------------------------------
SEASON_TYPE = "Regular Season"
PLAYOFF_SEASON_TYPE = "Playoffs"


def resolve_season(moment: datetime | date | None = None) -> str:
    """依日期推算 NBA 賽季字串，例如 2026-11-17 -> ``2026-27``。"""
    if moment is None:
        moment = now_utc().astimezone(TZ_ET)
    if isinstance(moment, datetime):
        moment = moment.astimezone(TZ_ET).date()
    start_year = moment.year if moment.month >= 10 else moment.year - 1
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def previous_season(season: str) -> str:
    """回傳上一個賽季字串。"""
    start_year = int(str(season).split("-")[0]) - 1
    return f"{start_year}-{(start_year + 1) % 100:02d}"


# --------------------------------------------------------------------------
# 30 支球隊對照表
# aliases 只需列出「自動組合以外」的別名；比對時會自動納入
# 縮寫、城市、隊名（暱稱）與全名。
# --------------------------------------------------------------------------
TEAMS: tuple[dict, ...] = (
    {"id": 1610612737, "abbreviation": "ATL", "city": "Atlanta", "name": "Hawks", "full_name": "Atlanta Hawks", "aliases": ("atl",)},
    {"id": 1610612738, "abbreviation": "BOS", "city": "Boston", "name": "Celtics", "full_name": "Boston Celtics", "aliases": ("bos",)},
    {"id": 1610612751, "abbreviation": "BKN", "city": "Brooklyn", "name": "Nets", "full_name": "Brooklyn Nets", "aliases": ("bkn", "brooklyn nets")},
    {"id": 1610612766, "abbreviation": "CHA", "city": "Charlotte", "name": "Hornets", "full_name": "Charlotte Hornets", "aliases": ("cha",)},
    {"id": 1610612741, "abbreviation": "CHI", "city": "Chicago", "name": "Bulls", "full_name": "Chicago Bulls", "aliases": ("chi",)},
    {"id": 1610612739, "abbreviation": "CLE", "city": "Cleveland", "name": "Cavaliers", "full_name": "Cleveland Cavaliers", "aliases": ("cle", "cavs")},
    {"id": 1610612742, "abbreviation": "DAL", "city": "Dallas", "name": "Mavericks", "full_name": "Dallas Mavericks", "aliases": ("dal", "mavs")},
    {"id": 1610612743, "abbreviation": "DEN", "city": "Denver", "name": "Nuggets", "full_name": "Denver Nuggets", "aliases": ("den",)},
    {"id": 1610612765, "abbreviation": "DET", "city": "Detroit", "name": "Pistons", "full_name": "Detroit Pistons", "aliases": ("det",)},
    {"id": 1610612744, "abbreviation": "GSW", "city": "Golden State", "name": "Warriors", "full_name": "Golden State Warriors", "aliases": ("gsw", "dubs")},
    {"id": 1610612745, "abbreviation": "HOU", "city": "Houston", "name": "Rockets", "full_name": "Houston Rockets", "aliases": ("hou",)},
    {"id": 1610612754, "abbreviation": "IND", "city": "Indiana", "name": "Pacers", "full_name": "Indiana Pacers", "aliases": ("ind",)},
    {"id": 1610612746, "abbreviation": "LAC", "city": "LA", "name": "Clippers", "full_name": "LA Clippers", "aliases": ("lac", "los angeles clippers", "la clippers", "clips")},
    {"id": 1610612747, "abbreviation": "LAL", "city": "Los Angeles", "name": "Lakers", "full_name": "Los Angeles Lakers", "aliases": ("lal",)},
    {"id": 1610612763, "abbreviation": "MEM", "city": "Memphis", "name": "Grizzlies", "full_name": "Memphis Grizzlies", "aliases": ("mem",)},
    {"id": 1610612748, "abbreviation": "MIA", "city": "Miami", "name": "Heat", "full_name": "Miami Heat", "aliases": ("mia",)},
    {"id": 1610612749, "abbreviation": "MIL", "city": "Milwaukee", "name": "Bucks", "full_name": "Milwaukee Bucks", "aliases": ("mil",)},
    {"id": 1610612750, "abbreviation": "MIN", "city": "Minnesota", "name": "Timberwolves", "full_name": "Minnesota Timberwolves", "aliases": ("min", "wolves")},
    {"id": 1610612740, "abbreviation": "NOP", "city": "New Orleans", "name": "Pelicans", "full_name": "New Orleans Pelicans", "aliases": ("nop", "nola")},
    {"id": 1610612752, "abbreviation": "NYK", "city": "New York", "name": "Knicks", "full_name": "New York Knicks", "aliases": ("nyk",)},
    {"id": 1610612760, "abbreviation": "OKC", "city": "Oklahoma City", "name": "Thunder", "full_name": "Oklahoma City Thunder", "aliases": ("okc",)},
    {"id": 1610612753, "abbreviation": "ORL", "city": "Orlando", "name": "Magic", "full_name": "Orlando Magic", "aliases": ("orl",)},
    {"id": 1610612755, "abbreviation": "PHI", "city": "Philadelphia", "name": "76ers", "full_name": "Philadelphia 76ers", "aliases": ("phi", "sixers")},
    {"id": 1610612756, "abbreviation": "PHX", "city": "Phoenix", "name": "Suns", "full_name": "Phoenix Suns", "aliases": ("phx", "pho")},
    {"id": 1610612757, "abbreviation": "POR", "city": "Portland", "name": "Trail Blazers", "full_name": "Portland Trail Blazers", "aliases": ("por", "blazers")},
    {"id": 1610612758, "abbreviation": "SAC", "city": "Sacramento", "name": "Kings", "full_name": "Sacramento Kings", "aliases": ("sac",)},
    {"id": 1610612759, "abbreviation": "SAS", "city": "San Antonio", "name": "Spurs", "full_name": "San Antonio Spurs", "aliases": ("sas",)},
    {"id": 1610612761, "abbreviation": "TOR", "city": "Toronto", "name": "Raptors", "full_name": "Toronto Raptors", "aliases": ("tor",)},
    {"id": 1610612762, "abbreviation": "UTA", "city": "Utah", "name": "Jazz", "full_name": "Utah Jazz", "aliases": ("uta",)},
    {"id": 1610612764, "abbreviation": "WAS", "city": "Washington", "name": "Wizards", "full_name": "Washington Wizards", "aliases": ("was", "wsh")},
)


def normalize_team_text(value: str) -> str:
    """正規化隊名：去重音、轉小寫、只保留英數字並壓縮空白。"""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = "".join(ch if ch.isalnum() else " " for ch in text)
    return " ".join(text.split())


TEAM_BY_ID: dict[int, dict] = {int(team["id"]): team for team in TEAMS}
TEAM_BY_ABBREV: dict[str, dict] = {team["abbreviation"]: team for team in TEAMS}

_TEAM_ALIAS_INDEX: dict[str, str] = {}
for _team in TEAMS:
    _values = {
        _team["abbreviation"],
        _team["city"],
        _team["name"],
        _team["full_name"],
        *(_team.get("aliases") or ()),
    }
    for _value in _values:
        _TEAM_ALIAS_INDEX.setdefault(normalize_team_text(_value), _team["abbreviation"])


def resolve_team(value: str | None) -> str | None:
    """把任意隊名／縮寫轉為官方縮寫，找不到回傳 ``None``。"""
    if not value:
        return None
    text = str(value).strip()
    if text.upper() in TEAM_BY_ABBREV:
        return text.upper()
    return _TEAM_ALIAS_INDEX.get(normalize_team_text(text))


def team_display_name(abbreviation: str) -> str:
    """回傳球隊全名，找不到時回傳原字串。"""
    team = TEAM_BY_ABBREV.get(str(abbreviation).upper())
    return team["full_name"] if team else str(abbreviation)


# --------------------------------------------------------------------------
# 基礎戰力分模型
# --------------------------------------------------------------------------
#: 主場優勢（分）
HOME_COURT_ADVANTAGE = 2.6
#: 中立場地（例如季中錦標賽決賽）主場優勢
NEUTRAL_COURT_ADVANTAGE = 0.0
#: 背靠背（B2B）扣分
B2B_PENALTY = 1.2
#: 每多休一天的分數補償（基準為休 1 天）
REST_ADVANTAGE_PER_DAY = 0.25
#: 休息天數補償上限
REST_ADVANTAGE_CAP = 1.0

#: 逐場分差指數移動平均係數（0 < alpha <= 1，越大越看重近期）
EMA_ALPHA = 0.20
#: 基礎戰力分混合權重，總和必須為 1.0
RATING_WEIGHTS = {"season": 0.40, "recent": 0.35, "ema": 0.25}
#: 近 N 場進階數據視窗
RECENT_GAMES_WINDOW = 10
#: 近 N 場至少要有幾場才採用近期 Net Rating
MIN_RECENT_GAMES = 5
#: 少於此場數時對球季 Net Rating 做線性收縮
MIN_GAMES_FOR_FULL_WEIGHT = 10

#: 基礎戰力分基準（50 = 聯盟平均，分數差 ≈ 預期分差）
RATING_BASE = 50.0
RATING_MIN = 25.0
RATING_MAX = 75.0

#: 勝率換算：預期分差服從標準差 12 分的常態分布
WIN_PROB_SIGMA = 12.0
WIN_PROB_MIN = 0.01
WIN_PROB_MAX = 0.99
#: 信心標籤門檻（絕對預期分差）
CONFIDENCE_HIGH_MARGIN = 8.0
CONFIDENCE_MEDIUM_MARGIN = 4.0

#: 預測總分合理區間
TOTAL_MIN = 190.0
TOTAL_MAX = 270.0

#: 聯盟平均備援值（球季剛開始、資料不足時使用）
LEAGUE_AVERAGES_FALLBACK = {
    "avg_off_rating": 114.0,
    "avg_def_rating": 114.0,
    "avg_net_rating": 0.0,
    "avg_pace": 99.0,
}

#: team_ratings.json 超過此小時數即視為過期
RATING_MAX_AGE_HOURS = 26


# --------------------------------------------------------------------------
# 傷病扣分模型
# --------------------------------------------------------------------------
#: 傷病狀態 -> 嚴重度權重（0 = 不扣分，1 = 視同缺賽）
INJURY_STATUS_WEIGHTS: dict[str, float] = {
    "out": 1.00,
    "doubtful": 0.75,
    "questionable": 0.45,
    "day-to-day": 0.35,
    "probable": 0.15,
    "available": 0.00,
    "game-time decision": 0.50,
    "suspended": 1.00,
    "not with team": 0.90,
    "g league": 0.15,
    "return to competition reconditioning": 0.20,
    "rest": 0.60,
    "unknown": 0.35,
}

#: 球員影響分公式：以 PerGame 數據加權後再乘上 MIN/36
PLAYER_IMPACT_WEIGHTS: dict[str, float] = {
    "pts": 1.0,
    "ast": 1.2,
    "reb": 0.9,
    "stl": 1.5,
    "blk": 1.5,
    "tov": -1.0,
}
#: 球員影響分的出賽時間基準
PLAYER_IMPACT_MINUTE_BASE = 36.0
#: 場均上場時間低於此值不納入影響分計算
PLAYER_MIN_MINUTES = 1.0
#: 球員姓名模糊比對門檻
PLAYER_NAME_MATCH_THRESHOLD = 0.90
#: 傷兵影響佔比換算成扣分的基準分
INJURY_IMPACT_SCALE = 16.0
#: 單隊傷病扣分上限
INJURY_MAX_TEAM_DEDUCTION = 12.0
#: 替補只能補回部分產出，因此傷病扣分再乘上此係數
INJURY_REPLACEMENT_FACTOR = 0.65
#: 查無球員數據時的預設影響佔比
DEFAULT_UNKNOWN_PLAYER_SHARE = 0.03
#: 傷兵對預測總分的下修係數（球隊少得分、對手也少了轉換機會）
INJURY_TOTAL_SCORING_FACTOR = 0.5


# --------------------------------------------------------------------------
# 網路請求與快取
# --------------------------------------------------------------------------
REQUEST_TIMEOUT = 60
REQUEST_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5.0

INJURY_REQUEST_TIMEOUT = 20
#: 傷病報告整體抓取上限（秒），避免 nbainjuries 內部無 timeout 卡死 CI
INJURY_FETCH_TIMEOUT = 240
#: 傷病報告回溯範圍與步進（2025-12-22 起官網改為 15 分鐘粒度）
INJURY_LOOKBACK_HOURS = 24
INJURY_STEP_MINUTES = 15
INJURY_MAX_PROBES = INJURY_LOOKBACK_HOURS * 60 // INJURY_STEP_MINUTES
INJURY_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
}

#: API 原始回應快取（存放於 .cache/，不進版控）
ENABLE_CACHE = True
CACHE_TTL_HOURS = 6

#: stats.nba.com 可用性探測：GitHub Actions 的雲端 IP 常被 Akamai 封鎖，
#: 因此先用短 timeout 探測，失敗就整趟改用 ESPN 備援，避免 3 次 60 秒的等待。
SOURCE_PROBE_TIMEOUT = 12
#: 來源健康狀態在記憶體中的快取秒數（單一 process 內有效）
SOURCE_HEALTH_TTL_SECONDS = 1800
#: stats.nba.com 不可用時是否改用 ESPN 公開 API 備援
ESPN_FALLBACK_ENABLED = True
#: ESPN 備援缺少 ORtg/DRtg/Pace，改用「聯盟平均每隊每場得分」作為換算基準
ESPN_FALLBACK_PACE = 114.0

#: 預設 log 等級（可被 --log-level 覆寫）
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

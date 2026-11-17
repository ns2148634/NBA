"""ESPN 公開 API 備援資料層。

stats.nba.com 在 GitHub Actions 等雲端 IP 常被 Akamai 封鎖（API 路徑回 0 bytes
timeout），cdn.nba.com 則直接回 403；ESPN 的 ``site.api.espn.com`` 在 runner 與
本機皆正常，因此作為備援來源，輸出與 :mod:`src.fetch_stats` 完全相同的資料結構：

* 當日賽程：scoreboard → 主客隊、ET 時間、比分、狀態、休息天數
* 球季數據：各隊逐場比分 → 自行換算 ORtg / DRtg / Pace 代理值與近 10 場
* 團隊逐場 log：供 EMA 與歷史賽果回填使用

Proxy 說明：ESPN 不提供 ORtg/DRtg/Pace，因此以「聯盟平均每隊每場得分」
（``config.ESPN_FALLBACK_PACE`` 為球季初期的備援值）為基準換算：

* ``off_rating = 100 × 每場得分 / 聯盟平均每場得分``
* ``def_rating = 100 × 每場失分 / 聯盟平均每場得分``
* ``pace`` 使用同一基準，確保 rating_engine 的「分差 ↔ 每 100 回合」換算一致。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from . import config

LOGGER = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_TEAMS_URL = f"{ESPN_BASE}/teams"
ESPN_SCOREBOARD_URL = f"{ESPN_BASE}/scoreboard"
ESPN_TEAM_SCHEDULE_URL = f"{ESPN_BASE}/teams/{{team}}/schedule"

#: ESPN 縮寫與 nba_api 縮寫不同之處
ESPN_ABBREVIATION_ALIASES = {
    "NY": "NYK",
    "GS": "GSW",
    "NO": "NOP",
    "SA": "SAS",
    "WSH": "WAS",
    "UTAH": "UTA",
    "PHO": "PHX",
    "NJ": "BKN",
}

ESPN_TIMEOUT = min(config.REQUEST_TIMEOUT, 30)
ESPN_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_SESSION: requests.Session | None = None
#: 同一 process 內重複使用各隊賽程（predict 模式會呼叫多次）
_SCHEDULE_CACHE: dict[tuple[str, str], dict[str, list[dict]]] = {}


class EspnFetchError(RuntimeError):
    """ESPN 備援資料抓取失敗。"""


# --------------------------------------------------------------------------
# HTTP 與快取
# --------------------------------------------------------------------------
def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        session = requests.Session()
        session.headers.update(ESPN_HTTP_HEADERS)
        _SESSION = session
    return _SESSION


def _cache_path(url: str) -> Path:
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return config.CACHE_DIR / "espn" / f"{key}.json"


def _cache_read(url: str, ttl_hours: float) -> dict | None:
    path = _cache_path(url)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        body = payload["body"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    age_hours = (config.now_utc() - fetched_at).total_seconds() / 3600.0
    if age_hours > ttl_hours or not isinstance(body, dict):
        return None
    LOGGER.info("使用 ESPN 快取：%s", url)
    return body


def _cache_write(url: str, body: dict) -> None:
    path = _cache_path(url)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {"fetched_at": config.to_iso(config.now_utc()), "body": body},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:  # pragma: no cover - 磁碟問題
        LOGGER.debug("ESPN 快取寫入失敗（%s）：%s", path, exc)


def _get_json(url: str, ttl_hours: float | None = None) -> dict:
    """取得 ESPN JSON（含 6 小時檔案快取與 3 次指數退避重試）。"""
    use_cache = bool(config.ENABLE_CACHE)
    ttl = config.CACHE_TTL_HOURS if ttl_hours is None else ttl_hours
    if use_cache:
        cached = _cache_read(url, ttl)
        if cached is not None:
            return cached

    last_error: Exception | None = None
    payload: Any = None
    for attempt in range(1, config.REQUEST_RETRIES + 1):
        try:
            response = _session().get(url, timeout=ESPN_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            break
        except Exception as exc:  # requests / JSON 例外種類繁多
            last_error = exc
            LOGGER.warning(
                "ESPN 請求失敗（第 %d/%d 次）%s：%s",
                attempt,
                config.REQUEST_RETRIES,
                url,
                exc,
            )
            if attempt < config.REQUEST_RETRIES:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
    else:
        raise EspnFetchError(
            f"ESPN 連續 {config.REQUEST_RETRIES} 次失敗：{url}（{last_error}）"
        ) from last_error

    if not isinstance(payload, dict):
        raise EspnFetchError(f"ESPN 回應格式非預期：{url}")
    if use_cache:
        _cache_write(url, payload)
    return payload


# --------------------------------------------------------------------------
# 解析工具
# --------------------------------------------------------------------------
def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _mean(values: Iterable[float | None]) -> float | None:
    cleaned = [value for value in values if value is not None]
    if not cleaned:
        return None
    return sum(cleaned) / len(cleaned)


def espn_season_year(season: str) -> int:
    """``2026-27`` -> ``2027``（ESPN 的 season 參數使用球季結束年份）。"""
    start = int(str(season).split("-")[0][:4])
    return start + 1


def _parse_utc(value: Any) -> datetime | None:
    """解析 ESPN 的 UTC 時間字串（例：``2026-11-18T00:30Z``）。"""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _resolve_team(team: dict) -> str | None:
    """把 ESPN 的隊伍物件轉成官方縮寫。"""
    abbreviation = str((team or {}).get("abbreviation") or "").strip().upper()
    if abbreviation:
        resolved = config.resolve_team(
            ESPN_ABBREVIATION_ALIASES.get(abbreviation, abbreviation)
        )
        if resolved:
            return resolved
    for key in ("displayName", "shortDisplayName", "name", "location"):
        resolved = config.resolve_team((team or {}).get(key))
        if resolved:
            return resolved
    return None


def _season_type_matches(event: dict, season_type: str) -> bool:
    """判斷 ESPN 事件是否屬於指定賽事類型。"""
    label = str(((event.get("seasonType") or {}).get("name")) or "").strip().lower()
    if label:
        if season_type == config.PLAYOFF_SEASON_TYPE:
            return "playoff" in label or "postseason" in label
        return "regular" in label
    # scoreboard 只有 season.type（2 = 例行賽、3 = 季後賽）
    season = event.get("season") or {}
    expected = 3 if season_type == config.PLAYOFF_SEASON_TYPE else 2
    return int(_to_float(season.get("type")) or 2) == expected


def _competitors(competition: dict) -> tuple[dict | None, dict | None]:
    """取出 (主隊, 客隊) 的 competitor 物件。"""
    home: dict | None = None
    away: dict | None = None
    for competitor in competition.get("competitors") or []:
        side = str(competitor.get("homeAway") or "").lower()
        if side == "home":
            home = competitor
        elif side == "away":
            away = competitor
    if home is not None and away is not None:
        return home, away
    ordered = sorted(
        competition.get("competitors") or [],
        key=lambda item: _to_float(item.get("order")) or 0.0,
    )
    if len(ordered) >= 2:
        return ordered[0], ordered[1]
    return None, None


def _score(competitor: dict | None) -> float | None:
    if not competitor:
        return None
    score = competitor.get("score")
    if isinstance(score, dict):
        score = score.get("value")
    return _to_float(score)


def _status_code(competition: dict, event: dict) -> int:
    """ESPN 狀態 -> 1 未開賽 / 2 進行中 / 3 已結束。"""
    status = competition.get("status") or event.get("status") or {}
    type_block = status.get("type") or {}
    state = str(type_block.get("state") or "").lower()
    if type_block.get("completed") or state == "post":
        return 3
    if state == "in":
        return 2
    return 1


def _status_text(competition: dict, event: dict) -> str:
    status = competition.get("status") or event.get("status") or {}
    type_block = status.get("type") or {}
    return str(type_block.get("detail") or type_block.get("shortDetail") or "").strip()


# --------------------------------------------------------------------------
# 球隊與賽程
# --------------------------------------------------------------------------
def fetch_teams() -> dict[str, dict]:
    """回傳 ``{官方縮寫: {"espn_id": ..., "name": ...}}``。"""
    payload = _get_json(ESPN_TEAMS_URL)
    sports = payload.get("sports") or [{}]
    leagues = (sports[0] or {}).get("leagues") or [{}]
    teams: dict[str, dict] = {}
    for entry in (leagues[0] or {}).get("teams") or []:
        team = entry.get("team") or {}
        abbreviation = _resolve_team(team)
        espn_id = str(team.get("id") or "").strip()
        if not abbreviation or not espn_id:
            continue
        teams[abbreviation] = {
            "espn_id": espn_id,
            "name": config.team_display_name(abbreviation),
        }
    if len(teams) < 20:
        raise EspnFetchError(f"ESPN 隊伍清單不完整（僅 {len(teams)} 隊）。")
    return teams


def _team_schedule_games(
    season: str,
    season_type: str,
    espn_id: str,
) -> list[dict]:
    """取得單隊已完成的逐場賽事（含比分、ET 時間）。"""
    url = ESPN_TEAM_SCHEDULE_URL.format(team=espn_id)
    payload = _get_json(f"{url}?season={espn_season_year(season)}")
    games: list[dict] = []
    for event in payload.get("events") or []:
        game = normalize_event(event, season_type)
        if game is None or game.get("status") != 3:
            continue
        games.append(game)
    games.sort(
        key=lambda item: (item["game_date"], item["game_time_et"], item["game_id"])
    )
    return games


def fetch_all_team_games(
    season: str,
    season_type: str = config.SEASON_TYPE,
    refresh: bool = False,
) -> dict[str, list[dict]]:
    """取得 30 隊逐場賽事（同一 process 內快取，避免重複 30 次請求）。"""
    key = (season, season_type)
    if not refresh and key in _SCHEDULE_CACHE:
        return _SCHEDULE_CACHE[key]
    teams = fetch_teams()
    games_by_team: dict[str, list[dict]] = {}
    for abbreviation, meta in teams.items():
        try:
            games_by_team[abbreviation] = _team_schedule_games(
                season, season_type, meta["espn_id"]
            )
        except EspnFetchError as exc:
            LOGGER.warning("%s 賽程取得失敗：%s", abbreviation, exc)
    if len(games_by_team) < 20:
        raise EspnFetchError(
            f"ESPN 賽程取得失敗（僅 {len(games_by_team)} 隊成功）。"
        )
    _SCHEDULE_CACHE[key] = games_by_team
    return games_by_team


def normalize_event(event: dict, season_type: str) -> dict | None:
    """把 ESPN 賽事轉成與 ``fetch_stats._normalize_schedule_row`` 相同格式。"""
    if not _season_type_matches(event, season_type):
        return None
    competition = (event.get("competitions") or [{}])[0] or {}
    home, away = _competitors(competition)
    home_team = _resolve_team((home or {}).get("team") or {})
    away_team = _resolve_team((away or {}).get("team") or {})
    if not home_team or not away_team or home_team == away_team:
        return None

    utc_dt = _parse_utc(competition.get("date") or event.get("date"))
    if utc_dt is None:
        return None
    et_dt = utc_dt.astimezone(config.TZ_ET)

    status = _status_code(competition, event)
    home_score = _score(home)
    away_score = _score(away)
    if status != 3:
        home_score = None
        away_score = None

    return {
        "game_id": str(event.get("id") or competition.get("id") or ""),
        "season_type": season_type,
        "game_date": et_dt.date().isoformat(),
        "game_time_et": et_dt.strftime("%H:%M"),
        "game_time_utc": config.to_iso(utc_dt),
        "game_time_taipei": et_dt.astimezone(config.TZ_TAIPEI).isoformat(
            timespec="minutes"
        ),
        "home_team": home_team,
        "away_team": away_team,
        "home_team_name": config.team_display_name(home_team),
        "away_team_name": config.team_display_name(away_team),
        "home_team_id": int(config.TEAM_BY_ABBREV[home_team]["id"]),
        "away_team_id": int(config.TEAM_BY_ABBREV[away_team]["id"]),
        "home_score": home_score,
        "away_score": away_score,
        "status": status,
        "status_text": _status_text(competition, event),
        "venue": str((competition.get("venue") or {}).get("fullName") or ""),
        "is_neutral": bool(competition.get("neutralSite")),
        "home_rest_days": None,
        "away_rest_days": None,
        "home_b2b": None,
        "away_b2b": None,
    }


# --------------------------------------------------------------------------
# 由比分換算數據（純函式，方便離線測試）
# --------------------------------------------------------------------------
def build_team_log(game: dict) -> list[dict]:
    """單場賽事 -> 兩隊各一筆逐場 log（與 nba_api LeagueGameLog 精簡格式一致）。"""
    rows: list[dict] = []
    for side, opposite in (("home", "away"), ("away", "home")):
        team = game.get(f"{side}_team")
        own = _to_float(game.get(f"{side}_score"))
        other = _to_float(game.get(f"{opposite}_score"))
        if not team or own is None or other is None:
            continue
        rows.append(
            {
                "game_id": game.get("game_id"),
                "team_id": int(config.TEAM_BY_ABBREV[team]["id"]),
                "team_abbrev": team,
                "game_date": game.get("game_date"),
                "plus_minus": float(own - other),
                "points": float(own),
                "wl": "W" if own > other else "L",
            }
        )
    return rows


def build_game_log(games_by_team: dict[str, list[dict]]) -> list[dict]:
    """把各隊逐場賽事合併成正規化的逐場 log（依 game_id 去重）。"""
    games: dict[str, dict] = {}
    for team_games in games_by_team.values():
        for game in team_games:
            game_id = game.get("game_id")
            if game_id:
                games[str(game_id)] = game
    log: list[dict] = []
    for game in sorted(
        games.values(),
        key=lambda item: (item.get("game_date") or "", item.get("game_id") or ""),
    ):
        log.extend(build_team_log(game))
    log.sort(
        key=lambda row: (row["game_date"] or "", row["game_id"] or "", row["team_abbrev"])
    )
    return log


def _proxy_block(
    points_for: float | None,
    points_against: float | None,
    pace: float,
) -> dict:
    """以聯盟平均得分為基準，換算 ORtg / DRtg / Net Rating / Pace 代理值。"""
    if points_for is None or points_against is None:
        return {
            "off_rating": None,
            "def_rating": None,
            "net_rating": None,
            "pace": None,
        }
    off_rating = 100.0 * points_for / pace
    def_rating = 100.0 * points_against / pace
    return {
        "off_rating": round(off_rating, 4),
        "def_rating": round(def_rating, 4),
        "net_rating": round(off_rating - def_rating, 4),
        "pace": round(pace, 4),
    }


def _team_totals(games: list[dict], team: str) -> list[dict]:
    """單隊逐場的 (得分, 失分)。"""
    totals: list[dict] = []
    for game in games:
        if game.get("home_team") == team:
            own = _to_float(game.get("home_score"))
            other = _to_float(game.get("away_score"))
        else:
            own = _to_float(game.get("away_score"))
            other = _to_float(game.get("home_score"))
        if own is None or other is None:
            continue
        totals.append({"points": own, "opp_points": other})
    return totals


def build_team_stats(
    games_by_team: dict[str, list[dict]],
    league_pace: float | None = None,
) -> tuple[dict, dict]:
    """由各隊逐場比分計算球隊與聯盟數據（含近 10 場）。"""
    per_team = {
        team: _team_totals(games, team) for team, games in games_by_team.items()
    }
    league_avg = _mean(
        sum(row["points"] for row in rows) / len(rows)
        for rows in per_team.values()
        if rows
    )
    if league_avg is None:
        league_avg = float(league_pace or config.ESPN_FALLBACK_PACE)
    league_avg = max(1.0, league_avg)

    teams: dict[str, dict] = {}
    for team, rows in per_team.items():
        games_played = len(rows)
        if games_played:
            points_for = sum(row["points"] for row in rows) / games_played
            points_against = sum(row["opp_points"] for row in rows) / games_played
            wins = sum(1 for row in rows if row["points"] > row["opp_points"])
            recent = rows[-config.RECENT_GAMES_WINDOW :]
            recent_for = sum(row["points"] for row in recent) / len(recent)
            recent_against = sum(row["opp_points"] for row in recent) / len(recent)
        else:
            points_for = points_against = recent_for = recent_against = None
            wins = 0
        season_block = _proxy_block(points_for, points_against, league_avg)
        recent_block = _proxy_block(recent_for, recent_against, league_avg)
        recent_block["games"] = min(games_played, config.RECENT_GAMES_WINDOW)
        teams[team] = {
            "team_id": int(config.TEAM_BY_ABBREV[team]["id"]),
            "abbreviation": team,
            "name": config.team_display_name(team),
            "games_played": games_played,
            "wins": wins,
            "losses": games_played - wins,
            "season": season_block,
            "recent": recent_block,
        }

    league = {
        "avg_off_rating": _mean(
            block["season"]["off_rating"] for block in teams.values()
        ),
        "avg_def_rating": _mean(
            block["season"]["def_rating"] for block in teams.values()
        ),
        "avg_net_rating": _mean(
            block["season"]["net_rating"] for block in teams.values()
        ),
        "avg_pace": _mean(block["season"]["pace"] for block in teams.values()),
    }
    for key, fallback in config.LEAGUE_AVERAGES_FALLBACK.items():
        if league.get(key) is None:
            league[key] = fallback
    return teams, league


def _rest_days(previous_dates: list[date], target: date) -> int | None:
    """休息天數：0 代表 B2B、1 代表休一天（基準）。"""
    earlier = [moment for moment in previous_dates if moment < target]
    if not earlier:
        return None
    return max(0, (target - earlier[-1]).days - 1)


def annotate_rest_days(
    games: list[dict],
    games_by_team: dict[str, list[dict]],
) -> list[dict]:
    """依各隊逐場賽程標註休息天數與 B2B。"""
    dates_by_team: dict[str, list[date]] = {}
    for team, team_games in games_by_team.items():
        dates: set[date] = set()
        for game in team_games:
            try:
                dates.add(date.fromisoformat(str(game.get("game_date"))))
            except (TypeError, ValueError):
                continue
        dates_by_team[team] = sorted(dates)

    for game in games:
        try:
            target = date.fromisoformat(str(game.get("game_date")))
        except (TypeError, ValueError):
            continue
        for side in ("home", "away"):
            team = game.get(f"{side}_team")
            rest = _rest_days(dates_by_team.get(team, []), target)
            game[f"{side}_rest_days"] = rest
            game[f"{side}_b2b"] = None if rest is None else rest == 0
    return games


# --------------------------------------------------------------------------
# 對外介面（與 fetch_stats 相同資料契約）
# --------------------------------------------------------------------------
def fetch_team_game_log(
    season: str,
    season_type: str = config.SEASON_TYPE,
) -> list[dict]:
    """全聯盟團隊逐場 log（每隊每場一列，供 EMA 與賽果回填）。"""
    games_by_team = fetch_all_team_games(season, season_type)
    return build_game_log(games_by_team)


def fetch_all_team_stats(
    season: str,
    season_type: str = config.SEASON_TYPE,
) -> dict:
    """球季數據（結構與 ``fetch_stats.fetch_all_team_stats`` 相同）。"""
    games_by_team = fetch_all_team_games(season, season_type)
    teams, league = build_team_stats(games_by_team)
    if not teams:
        raise EspnFetchError(f"ESPN 查無 {season} {season_type} 的賽果。")
    game_log = build_game_log(games_by_team)
    warnings = [
        "資料來源：ESPN 備援（stats.nba.com 不可用）；"
        "ORtg / DRtg / Pace 為逐場比分換算的代理值。"
    ]
    if not game_log:
        warnings.append("ESPN 逐場資料為空，EMA 與預測總分改用代理值推估。")
    missing = [team for team, block in teams.items() if not block["games_played"]]
    if missing:
        warnings.append(f"以下球隊在 ESPN 尚無已完成賽事：{', '.join(sorted(missing))}")
    return {
        "source": "espn",
        "season": season,
        "season_type": season_type,
        "fetched_at": config.to_iso(config.now_utc()),
        "teams": teams,
        "league": league,
        "game_log": game_log,
        "warnings": warnings,
    }


def fetch_games_for_date(
    season: str,
    target_date: date,
    season_type: str = config.SEASON_TYPE,
) -> list[dict]:
    """取得指定日期（美東時間）的賽事，並標註休息天數。"""
    url = f"{ESPN_SCOREBOARD_URL}?dates={target_date.strftime('%Y%m%d')}"
    payload = _get_json(url)
    iso_date = target_date.isoformat()

    games: list[dict] = []
    for event in payload.get("events") or []:
        game = normalize_event(event, season_type)
        if game is None or game.get("game_date") != iso_date:
            continue
        games.append(game)
    if not games:
        return []

    games_by_team = fetch_all_team_games(season, season_type)
    involved = {
        game[f"{side}_team"] for game in games for side in ("home", "away")
    }
    annotate_rest_days(
        games, {team: games_by_team.get(team, []) for team in involved}
    )
    games.sort(key=lambda game: (game.get("game_time_et") or "", game["game_id"]))
    LOGGER.info("ESPN 賽程：%s 共 %d 場", iso_date, len(games))
    return games

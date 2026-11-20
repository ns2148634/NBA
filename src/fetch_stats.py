"""nba_api 資料抓取層。

負責：

* 全聯盟 30 隊的球季與近 10 場進階數據（Net Rating / ORtg / DRtg / Pace）
* 全聯盟團隊逐場 box log（供 EMA、近況與賽果回填使用）
* 球季賽程與當日對戰組合（含休息天數 / B2B 判定）
* 球員場均數據（供傷病影響分使用）

所有 API 回應都會經過快取（``.cache/``）與重試，並統一回傳純 dict/list，
方便直接寫入 JSON。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import math
import time
from datetime import date, datetime, timezone
from typing import Any, Callable

from . import config, fetch_espn

LOGGER = logging.getLogger(__name__)

_ENDPOINT_MODULE = "nba_api.stats.endpoints"


class StatsFetchError(RuntimeError):
    """nba_api 資料抓取失敗。"""


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------
def _endpoint(name: str):
    """延遲匯入 nba_api 端點，缺少套件時給出可讀訊息。"""
    try:
        module = importlib.import_module(_ENDPOINT_MODULE)
    except ImportError as exc:  # pragma: no cover - 依賴未安裝時
        raise StatsFetchError(
            "找不到 nba_api，請先執行 `pip install -r requirements.txt`。"
        ) from exc
    endpoint = getattr(module, name, None)
    if endpoint is None:  # pragma: no cover - 舊版 nba_api
        raise StatsFetchError(
            f"目前的 nba_api 版本不支援 {name} 端點，請升級 nba_api。"
        )
    return endpoint


# --------------------------------------------------------------------------
# 資料來源健康狀態（stats.nba.com 在雲端 CI 常被封鎖）
# --------------------------------------------------------------------------
_SOURCE_HEALTH: dict[str, Any] = {"checked_at": 0.0, "available": None}


def nba_api_available(force: bool = False) -> bool:
    """以短 timeout 探測 stats.nba.com 是否可用（結果在 process 內快取）。

    雲端 IP（例如 GitHub Actions）常被 Akamai 封鎖：TCP 可連線，但 API 路徑
    會 0 bytes timeout。若照原本 3 次 60 秒重試會浪費好幾分鐘，因此先探測一次，
    失敗就整趟改用 ESPN 備援。
    """
    if not force and _SOURCE_HEALTH["available"] is not None:
        elapsed = time.time() - float(_SOURCE_HEALTH["checked_at"] or 0.0)
        if elapsed < config.SOURCE_HEALTH_TTL_SECONDS:
            return bool(_SOURCE_HEALTH["available"])

    available = False
    try:
        importlib.import_module("pandas")  # nba_api 解析資料必需
        endpoint = _endpoint("ScoreboardV3")
        endpoint(
            game_date=config.now_utc().astimezone(config.TZ_ET).date().isoformat(),
            timeout=config.SOURCE_PROBE_TIMEOUT,
        )
        available = True
    except Exception as exc:  # pragma: no cover - 需要網路
        LOGGER.warning(
            "stats.nba.com 探測失敗（timeout=%ss）：%s",
            config.SOURCE_PROBE_TIMEOUT,
            exc,
        )

    _SOURCE_HEALTH["checked_at"] = time.time()
    _SOURCE_HEALTH["available"] = available
    LOGGER.info("stats.nba.com 可用：%s", available)
    return available


def _is_source_broken(exc: BaseException) -> bool:
    """判斷是否為「環境層級」故障（重試也沒用），例如 pandas/numpy 被系統封鎖。"""
    text = str(exc).lower()
    return isinstance(exc, ImportError) or "dll load failed" in text or (
        "no module named" in text
    )


def _mark_source_unavailable(exc: BaseException) -> None:
    """把資料來源標記為不可用，後續呼叫直接走 ESPN 備援。"""
    if _SOURCE_HEALTH["available"] is False:
        return
    LOGGER.warning("nba_api 環境不可用，本次改用 ESPN 備援：%s", exc)
    _SOURCE_HEALTH["checked_at"] = time.time()
    _SOURCE_HEALTH["available"] = False


def _clean_scalar(value: Any) -> Any:
    """把 pandas / numpy 純量轉成可 JSON 序列化的 Python 型別。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return _clean_scalar(value.item())
        except (ValueError, AttributeError):  # pragma: no cover
            pass
    if isinstance(value, (list, tuple)):
        return [_clean_scalar(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _clean_scalar(item) for key, item in value.items()}
    return str(value)


def _frame_to_records(frame) -> list[dict]:
    """DataFrame -> list[dict]（欄位名稱保持原樣並清理純量）。"""
    if frame is None:
        return []
    try:
        if frame.empty:
            return []
    except AttributeError:  # pragma: no cover - 非 DataFrame
        pass
    frame = frame.copy()
    frame.columns = [str(column) for column in frame.columns]
    return [
        {str(key): _clean_scalar(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _cache_key(namespace: str, params: dict) -> str:
    payload = json.dumps(
        {"namespace": namespace, "params": params},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _cache_read(key: str, ttl_hours: float) -> list[dict] | None:
    path = config.CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    age_seconds = (config.now_utc() - fetched_at).total_seconds()
    if age_seconds > ttl_hours * 3600:
        return None
    records = payload.get("records")
    return records if isinstance(records, list) else None


def _cache_write(key: str, records: list[dict]) -> None:
    path = config.CACHE_DIR / f"{key}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "fetched_at": config.to_iso(config.now_utc()),
                    "records": records,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:  # pragma: no cover - 磁碟問題
        LOGGER.debug("快取寫入失敗（%s）：%s", path, exc)


def _with_retry(label: str, producer: Callable[[], list[dict]]) -> list[dict]:
    """執行 producer 並在失敗時指數退避重試。"""
    last_error: Exception | None = None
    for attempt in range(1, config.REQUEST_RETRIES + 1):
        try:
            return producer()
        except StatsFetchError:
            raise
        except Exception as exc:  # nba_api / requests 例外種類繁多
            if _is_source_broken(exc):
                _mark_source_unavailable(exc)
                raise StatsFetchError(f"{label} 資料來源不可用：{exc}") from exc
            last_error = exc
            LOGGER.warning(
                "%s 請求失敗（第 %d/%d 次）：%s",
                label,
                attempt,
                config.REQUEST_RETRIES,
                exc,
            )
            if attempt < config.REQUEST_RETRIES:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
    raise StatsFetchError(
        f"{label} 連續 {config.REQUEST_RETRIES} 次失敗：{last_error}"
    ) from last_error


def _cached_records(
    namespace: str,
    params: dict,
    producer: Callable[[], list[dict]],
) -> list[dict]:
    """帶快取的 API 呼叫；producer 必須回傳 JSON 可序列化的 list[dict]。"""
    use_cache = bool(config.ENABLE_CACHE)
    key = _cache_key(namespace, params)
    if use_cache:
        cached = _cache_read(key, config.CACHE_TTL_HOURS)
        if cached is not None:
            LOGGER.info("使用快取資料：%s %s", namespace, params)
            return cached
    records = producer()
    if use_cache:
        _cache_write(key, records)
    return records


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _first_float(record: dict, *keys: str) -> float | None:
    for key in keys:
        if key in record:
            value = _to_float(record.get(key))
            if value is not None:
                return value
    return None


def _first_text(record: dict, *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _team_from_record(record: dict) -> dict | None:
    """從 API 資料列解析出球隊基本資料。"""
    team_id = _first_float(record, "TEAM_ID", "teamId")
    if team_id is not None:
        team = config.TEAM_BY_ID.get(int(team_id))
        if team:
            return team
    abbreviation = _first_text(
        record, "TEAM_ABBREVIATION", "TEAM_TRICODE", "teamTricode"
    )
    if abbreviation:
        team = config.TEAM_BY_ABBREV.get(abbreviation.upper())
        if team:
            return team
    name = _first_text(record, "TEAM_NAME", "TEAM_CITY_NAME", "teamName")
    if name:
        resolved = config.resolve_team(name)
        if resolved:
            return config.TEAM_BY_ABBREV[resolved]
    return None


def _parse_date(value: Any) -> date | None:
    """解析多種日期字串格式（ISO、美式、NBA 賽程格式）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%Y/%m/%d", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_datetime(value: Any) -> datetime | None:
    """解析 UTC 時間字串；無時區資訊時視為 UTC。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
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


def _parse_et_datetime(value: Any) -> datetime | None:
    """解析美東時間字串（無時區資訊時視為 ET）並轉為 UTC。"""
    if value is None or value == "":
        return None
    text = str(value).strip().replace("Z", "")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        return moment.astimezone(timezone.utc)
    return moment.replace(tzinfo=config.TZ_ET).astimezone(timezone.utc)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


# --------------------------------------------------------------------------
# 進階團隊數據
# --------------------------------------------------------------------------
def fetch_team_advanced(
    season: str,
    season_type: str = config.SEASON_TYPE,
    last_n_games: int = 0,
) -> list[dict]:
    """抓取 LeagueDashTeamStats（Advanced / PerGame）原始資料列。"""
    if not nba_api_available():
        raise StatsFetchError("stats.nba.com 不可用，無法取得球隊進階數據。")
    endpoint = _endpoint("LeagueDashTeamStats")
    api_params = {
        "season": season,
        "season_type_all_star": season_type,
        "measure_type_detailed_defense": "Advanced",
        "per_mode_detailed": "PerGame",
        "last_n_games": int(last_n_games),
    }

    def request() -> list[dict]:
        response = endpoint(**api_params, timeout=config.REQUEST_TIMEOUT)
        frames = response.get_data_frames()
        return _frame_to_records(frames[0] if frames else None)

    return _cached_records(
        "LeagueDashTeamStats.Advanced",
        api_params,
        lambda: _with_retry("LeagueDashTeamStats", request),
    )


def _fetch_team_game_log_nba(
    season: str,
    season_type: str = config.SEASON_TYPE,
) -> list[dict]:
    """抓取全聯盟團隊逐場 box log（已正規化為精簡欄位）。"""
    endpoint = _endpoint("LeagueGameLog")
    api_params = {
        "counter": 0,
        "direction": "ASC",
        "league_id": "00",
        "player_or_team_abbreviation": "T",
        "season": season,
        "season_type_all_star": season_type,
        "sorter": "DATE",
    }

    def request() -> list[dict]:
        response = endpoint(**api_params, timeout=config.REQUEST_TIMEOUT)
        frames = response.get_data_frames()
        return _frame_to_records(frames[0] if frames else None)

    rows = _cached_records(
        "LeagueGameLog.Team",
        api_params,
        lambda: _with_retry("LeagueGameLog", request),
    )

    normalized: list[dict] = []
    for row in rows:
        team = _team_from_record(row)
        game_date = _parse_date(row.get("GAME_DATE"))
        if team is None or game_date is None:
            continue
        normalized.append(
            {
                "game_id": str(row.get("GAME_ID") or ""),
                "team_id": int(team["id"]),
                "team_abbrev": team["abbreviation"],
                "game_date": game_date.isoformat(),
                "plus_minus": _first_float(row, "PLUS_MINUS"),
                "points": _first_float(row, "PTS"),
                "wl": row.get("WL"),
            }
        )
    normalized.sort(key=lambda item: (item["game_date"], item["game_id"]))
    return normalized


# --------------------------------------------------------------------------
# 賽程
# --------------------------------------------------------------------------
def _normalize_schedule_row(row: dict, season_type: str) -> dict | None:
    """把 ScheduleLeagueV2 的資料列轉成統一格式。"""
    game_id = str(row.get("gameId") or row.get("gameID") or "").strip()
    if not game_id:
        return None
    # 002 = 例行賽、004 = 季後賽，過濾熱身賽與明星賽
    if season_type == config.SEASON_TYPE and not game_id.startswith("002"):
        return None
    if season_type == config.PLAYOFF_SEASON_TYPE and not game_id.startswith("004"):
        return None

    home_team = config.resolve_team(
        row.get("homeTeam_teamTricode")
    ) or config.resolve_team(row.get("homeTeam_teamName"))
    away_team = config.resolve_team(
        row.get("awayTeam_teamTricode")
    ) or config.resolve_team(row.get("awayTeam_teamName"))
    if not home_team or not away_team or home_team == away_team:
        return None

    utc_dt = _parse_datetime(row.get("gameDateTimeUTC")) or _parse_et_datetime(
        row.get("gameDateTimeEst")
    )
    et_dt = utc_dt.astimezone(config.TZ_ET) if utc_dt else None
    game_date = et_dt.date() if et_dt else _parse_date(row.get("gameDate"))
    if game_date is None:
        return None

    status = int(_first_float(row, "gameStatus", "gameStatusId") or 0)
    home_score = _first_float(row, "homeTeam_score", "homeTeamScore")
    away_score = _first_float(row, "awayTeam_score", "awayTeamScore")
    if status != 3:
        home_score = None
        away_score = None

    return {
        "game_id": game_id,
        "season_type": season_type,
        "game_date": game_date.isoformat(),
        "game_time_et": et_dt.strftime("%H:%M") if et_dt else "",
        "game_time_utc": config.to_iso(utc_dt),
        "game_time_taipei": et_dt.astimezone(config.TZ_TAIPEI).isoformat(timespec="minutes")
        if et_dt
        else None,
        "home_team": home_team,
        "away_team": away_team,
        "home_team_name": config.team_display_name(home_team),
        "away_team_name": config.team_display_name(away_team),
        "home_team_id": int(config.TEAM_BY_ABBREV[home_team]["id"]),
        "away_team_id": int(config.TEAM_BY_ABBREV[away_team]["id"]),
        "home_score": home_score,
        "away_score": away_score,
        "status": status,
        "status_text": _first_text(row, "gameStatusText", "gameStatus") or "",
        "venue": _first_text(row, "arenaName", "arena") or "",
        "is_neutral": _to_bool(row.get("isNeutral")),
        "home_rest_days": None,
        "away_rest_days": None,
        "home_b2b": None,
        "away_b2b": None,
    }


def fetch_schedule(
    season: str,
    season_type: str = config.SEASON_TYPE,
) -> list[dict]:
    """抓取整季賽程並正規化（含主客隊、UTC/ET 時間、比分）。"""
    if not nba_api_available():
        raise StatsFetchError("stats.nba.com 不可用，無法取得整季賽程。")
    endpoint = _endpoint("ScheduleLeagueV2")
    api_params = {"season": season, "league_id": "00"}

    def request() -> list[dict]:
        response = endpoint(**api_params, timeout=config.REQUEST_TIMEOUT)
        frame = response.season_games.get_data_frame()
        return _frame_to_records(frame)

    rows = _cached_records(
        "ScheduleLeagueV2",
        api_params,
        lambda: _with_retry("ScheduleLeagueV2", request),
    )

    games: dict[str, dict] = {}
    for row in rows:
        normalized = _normalize_schedule_row(row, season_type)
        if normalized is not None:
            games[normalized["game_id"]] = normalized
    return sorted(
        games.values(),
        key=lambda game: (game["game_date"], game["game_time_et"], game["game_id"]),
    )


def _rest_days(previous_dates: list[date], target: date | None) -> int | None:
    """計算休息天數：0 代表 B2B、1 代表休一天（基準）。"""
    if target is None or not previous_dates:
        return None
    earlier = [moment for moment in previous_dates if moment < target]
    if not earlier:
        return None
    return max(0, (target - earlier[-1]).days - 1)


def annotate_rest_days(games: list[dict], schedule: list[dict]) -> list[dict]:
    """依整季賽程標註每場比賽的休息天數與 B2B。"""
    team_dates: dict[str, set[date]] = {}
    for row in schedule:
        game_date = _parse_date(row.get("game_date"))
        if game_date is None:
            continue
        for key in ("home_team", "away_team"):
            team = row.get(key)
            if team:
                team_dates.setdefault(team, set()).add(game_date)
    ordered = {team: sorted(dates) for team, dates in team_dates.items()}

    for game in games:
        target = _parse_date(game.get("game_date"))
        for side in ("home", "away"):
            team = game.get(f"{side}_team")
            rest = _rest_days(ordered.get(team, []), target)
            game[f"{side}_rest_days"] = rest
            game[f"{side}_b2b"] = None if rest is None else rest == 0
    return games


def _build_scoreboard_games(
    headers: list[dict],
    lines: list[dict],
    target_date: date,
    season_type: str,
) -> list[dict]:
    """由 ScoreboardV3 的 game_header + line_score 組出賽程。"""
    by_game: dict[str, list[dict]] = {}
    for row in lines:
        by_game.setdefault(str(row.get("gameId") or ""), []).append(row)

    games: list[dict] = []
    for header in headers:
        game_id = str(header.get("gameId") or "").strip()
        if not game_id:
            continue
        if season_type == config.SEASON_TYPE and not game_id.startswith("002"):
            continue
        if season_type == config.PLAYOFF_SEASON_TYPE and not game_id.startswith("004"):
            continue
        rows = by_game.get(game_id, [])
        if len(rows) < 2:
            continue

        # 先由 gameCode（例：20251117/BOSNYK）判斷主客，再退回 line_score 順序
        away_team = home_team = None
        game_code = str(header.get("gameCode") or "")
        if "/" in game_code:
            codes = game_code.rsplit("/", 1)[-1]
            if len(codes) >= 6:
                away_team = config.resolve_team(codes[:3])
                home_team = config.resolve_team(codes[3:6])
        if not home_team or not away_team:
            # ScoreboardV3 的 line_score 順序為「客隊在前、主隊在後」
            away_team = config.resolve_team(rows[0].get("teamTricode"))
            home_team = config.resolve_team(rows[1].get("teamTricode"))
        if not home_team or not away_team or home_team == away_team:
            continue

        rows_by_team = {
            config.resolve_team(row.get("teamTricode")): row for row in rows
        }
        away_row = rows_by_team.get(away_team) or rows[0]
        home_row = rows_by_team.get(home_team) or rows[-1]

        utc_dt = _parse_datetime(header.get("gameTimeUTC"))
        et_dt = utc_dt.astimezone(config.TZ_ET) if utc_dt else None
        status = int(_first_float(header, "gameStatus", "gameStatusId") or 0)
        home_score = _first_float(home_row, "score", "points", "teamPoints")
        away_score = _first_float(away_row, "score", "points", "teamPoints")
        if status != 3:
            home_score = None
            away_score = None

        games.append(
            {
                "game_id": game_id,
                "season_type": season_type,
                "game_date": (et_dt.date() if et_dt else target_date).isoformat(),
                "game_time_et": et_dt.strftime("%H:%M") if et_dt else "",
                "game_time_utc": config.to_iso(utc_dt),
                "game_time_taipei": et_dt.astimezone(config.TZ_TAIPEI).isoformat(
                    timespec="minutes"
                )
                if et_dt
                else None,
                "home_team": home_team,
                "away_team": away_team,
                "home_team_name": config.team_display_name(home_team),
                "away_team_name": config.team_display_name(away_team),
                "home_team_id": int(config.TEAM_BY_ABBREV[home_team]["id"]),
                "away_team_id": int(config.TEAM_BY_ABBREV[away_team]["id"]),
                "home_score": home_score,
                "away_score": away_score,
                "status": status,
                "status_text": _first_text(header, "gameStatusText") or "",
                "venue": _first_text(header, "arenaName", "arena") or "",
                "is_neutral": _to_bool(header.get("isNeutral")),
                "home_rest_days": None,
                "away_rest_days": None,
                "home_b2b": None,
                "away_b2b": None,
            }
        )

    games.sort(key=lambda game: (game["game_time_et"], game["game_id"]))
    return games


def _fetch_scoreboard_games(
    target_date: date,
    season_type: str = config.SEASON_TYPE,
) -> list[dict]:
    """ScoreboardV3 備援：ScheduleLeagueV2 失效時仍能取得當日賽事。"""
    endpoint = _endpoint("ScoreboardV3")
    last_error: Exception | None = None
    success = False
    for date_arg in (target_date.isoformat(), target_date.strftime("%m/%d/%Y")):
        try:
            response = endpoint(game_date=date_arg, timeout=config.REQUEST_TIMEOUT)
            headers = _frame_to_records(response.game_header.get_data_frame())
            lines = _frame_to_records(response.line_score.get_data_frame())
            success = True
            if not headers:
                continue
            games = _build_scoreboard_games(headers, lines, target_date, season_type)
            if games:
                return games
        except Exception as exc:
            last_error = exc
            LOGGER.warning("ScoreboardV3(%s) 失敗：%s", date_arg, exc)
            if _is_source_broken(exc):
                _mark_source_unavailable(exc)
                break
    if success:
        return []
    raise StatsFetchError(
        f"ScoreboardV3 無法取得 {target_date} 賽程：{last_error}"
    ) from last_error


def _fetch_games_for_date_nba(
    season: str,
    target_date: date,
    season_type: str = config.SEASON_TYPE,
    schedule: list[dict] | None = None,
) -> dict:
    """取得指定日期（美東時間）的賽事，必要時改用 ScoreboardV3。"""
    warnings: list[str] = []

    if schedule is None:
        try:
            schedule = fetch_schedule(season, season_type)
        except StatsFetchError as exc:
            warnings.append(f"賽程抓取失敗，改用 ScoreboardV3：{exc}")
            games = _fetch_scoreboard_games(target_date, season_type)
            return {"games": games, "schedule": [], "warnings": warnings}

    iso_date = target_date.isoformat()
    games = [dict(game) for game in schedule if game.get("game_date") == iso_date]

    if not games:
        try:
            fallback_games = _fetch_scoreboard_games(target_date, season_type)
        except StatsFetchError as exc:
            warnings.append(str(exc))
            fallback_games = []
        if fallback_games:
            warnings.append("ScheduleLeagueV2 查無當日賽事，已改用 ScoreboardV3。")
            games = fallback_games

    annotate_rest_days(games, schedule)
    games.sort(key=lambda game: (game.get("game_time_et") or "", game["game_id"]))
    return {"games": games, "schedule": schedule, "warnings": warnings}


# --------------------------------------------------------------------------
# 球員數據（傷病影響分）
# --------------------------------------------------------------------------
def _fetch_player_season_stats_nba(
    season: str,
    season_type: str = config.SEASON_TYPE,
) -> list[dict]:
    """以 stats.nba.com 抓取全聯盟球員場均數據並精簡欄位。"""
    endpoint = _endpoint("LeagueDashPlayerStats")
    api_params = {
        "season": season,
        "season_type_all_star": season_type,
        "measure_type_detailed_defense": "Base",
        "per_mode_detailed": "PerGame",
    }

    def request() -> list[dict]:
        response = endpoint(**api_params, timeout=config.REQUEST_TIMEOUT)
        frames = response.get_data_frames()
        return _frame_to_records(frames[0] if frames else None)

    rows = _cached_records(
        "LeagueDashPlayerStats.Base",
        api_params,
        lambda: _with_retry("LeagueDashPlayerStats", request),
    )

    normalized: list[dict] = []
    for row in rows:
        team = _team_from_record(row)
        name = _first_text(row, "PLAYER_NAME", "PLAYER")
        if team is None or not name:
            continue
        normalized.append(
            {
                "player_id": int(_first_float(row, "PLAYER_ID") or 0),
                "name": name,
                "team_abbrev": team["abbreviation"],
                "gp": int(_first_float(row, "GP") or 0),
                "min": _first_float(row, "MIN") or 0.0,
                "pts": _first_float(row, "PTS") or 0.0,
                "reb": _first_float(row, "REB") or 0.0,
                "ast": _first_float(row, "AST") or 0.0,
                "stl": _first_float(row, "STL") or 0.0,
                "blk": _first_float(row, "BLK") or 0.0,
                "tov": _first_float(row, "TOV") or 0.0,
            }
        )
    return normalized


def _espn_player_season_stats(
    season: str,
    season_type: str,
    warnings: list[str] | None = None,
) -> list[dict]:
    """改用 ESPN byathlete 端點取得球員場均數據（stats.nba.com 的替代來源）。"""
    if not config.ESPN_PLAYER_STATS_FALLBACK_ENABLED:
        raise StatsFetchError(
            "stats.nba.com 不可用，且已停用 ESPN 球員數據備援"
            "（config.ESPN_PLAYER_STATS_FALLBACK_ENABLED）。"
        )
    try:
        rows = fetch_espn.fetch_player_season_stats(season, season_type)
    except fetch_espn.EspnFetchError as exc:
        raise StatsFetchError(
            f"stats.nba.com 與 ESPN 皆無法取得球員數據：{exc}"
        ) from exc
    message = f"stats.nba.com 不可用，球員數據改用 ESPN 場均數據（{len(rows)} 人）。"
    LOGGER.info(message)
    if warnings is not None:
        warnings.append(message)
    return rows


def fetch_player_season_stats(
    season: str,
    season_type: str = config.SEASON_TYPE,
    warnings: list[str] | None = None,
) -> list[dict]:
    """球員場均數據：優先 nba_api，不可用或失敗時改用 ESPN byathlete。"""
    if nba_api_available():
        try:
            return _fetch_player_season_stats_nba(season, season_type)
        except StatsFetchError as exc:
            if not config.ESPN_PLAYER_STATS_FALLBACK_ENABLED:
                raise
            LOGGER.warning("nba_api 球員數據失敗，改用 ESPN 備援：%s", exc)
            if warnings is not None:
                warnings.append(f"nba_api 球員數據失敗：{exc}")
    return _espn_player_season_stats(season, season_type, warnings)


# --------------------------------------------------------------------------
# 一次取得基礎戰力分所需資料
# --------------------------------------------------------------------------
def _average(values) -> float | None:
    cleaned = [value for value in values if value is not None]
    if not cleaned:
        return None
    return sum(cleaned) / len(cleaned)


def _fetch_all_team_stats_nba(
    season: str,
    season_type: str = config.SEASON_TYPE,
) -> dict:
    """組合球季、近況與逐場資料，供 rating_engine 計算基礎戰力分。"""
    warnings: list[str] = []

    season_rows = fetch_team_advanced(season, season_type, last_n_games=0)
    if not season_rows:
        raise StatsFetchError(f"{season} 查無球隊進階數據，無法計算戰力分。")

    try:
        recent_rows = fetch_team_advanced(
            season, season_type, last_n_games=config.RECENT_GAMES_WINDOW
        )
        if not recent_rows:
            warnings.append("近 10 場進階數據為空，近期權重改用球季數據。")
    except StatsFetchError as exc:
        warnings.append(f"近 10 場進階數據抓取失敗，近期權重改用球季數據：{exc}")
        recent_rows = []

    try:
        game_log = fetch_team_game_log(season, season_type)
        if not game_log:
            warnings.append("團隊逐場資料為空，EMA 與總分改用進階數據推估。")
    except StatsFetchError as exc:
        warnings.append(f"團隊逐場資料抓取失敗，EMA 與總分改用進階數據推估：{exc}")
        game_log = []

    teams: dict[str, dict] = {}
    for row in season_rows:
        team = _team_from_record(row)
        if team is None:
            continue
        off_rating = _first_float(row, "OFF_RATING", "E_OFF_RATING")
        def_rating = _first_float(row, "DEF_RATING", "E_DEF_RATING")
        pace = _first_float(row, "PACE", "E_PACE")
        net_rating = _first_float(row, "NET_RATING", "E_NET_RATING")
        if net_rating is None and off_rating is not None and def_rating is not None:
            net_rating = off_rating - def_rating

        teams[team["abbreviation"]] = {
            "team_id": int(team["id"]),
            "abbreviation": team["abbreviation"],
            "name": team["full_name"],
            "games_played": int(_first_float(row, "GP") or 0),
            "wins": int(_first_float(row, "W") or 0),
            "losses": int(_first_float(row, "L") or 0),
            "season": {
                "off_rating": off_rating,
                "def_rating": def_rating,
                "net_rating": net_rating,
                "pace": pace,
            },
            "recent": {
                "off_rating": None,
                "def_rating": None,
                "net_rating": None,
                "pace": None,
                "games": 0,
            },
        }

    for row in recent_rows:
        team = _team_from_record(row)
        if team is None or team["abbreviation"] not in teams:
            continue
        off_rating = _first_float(row, "OFF_RATING", "E_OFF_RATING")
        def_rating = _first_float(row, "DEF_RATING", "E_DEF_RATING")
        pace = _first_float(row, "PACE", "E_PACE")
        net_rating = _first_float(row, "NET_RATING", "E_NET_RATING")
        if net_rating is None and off_rating is not None and def_rating is not None:
            net_rating = off_rating - def_rating
        teams[team["abbreviation"]]["recent"] = {
            "off_rating": off_rating,
            "def_rating": def_rating,
            "net_rating": net_rating,
            "pace": pace,
            "games": int(_first_float(row, "GP") or 0),
        }

    league = {
        "avg_off_rating": _average(
            team["season"]["off_rating"] for team in teams.values()
        ),
        "avg_def_rating": _average(
            team["season"]["def_rating"] for team in teams.values()
        ),
        "avg_net_rating": _average(
            team["season"]["net_rating"] for team in teams.values()
        ),
        "avg_pace": _average(team["season"]["pace"] for team in teams.values()),
    }
    for key, fallback in config.LEAGUE_AVERAGES_FALLBACK.items():
        if league.get(key) is None:
            league[key] = fallback

    return {
        "source": "nba_api",
        "season": season,
        "season_type": season_type,
        "fetched_at": config.to_iso(config.now_utc()),
        "teams": teams,
        "league": league,
        "game_log": game_log,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# 對外介面：nba_api 優先；不可用或失敗時自動改用 ESPN 備援
# --------------------------------------------------------------------------
def _espn_team_stats(season: str, season_type: str) -> dict:
    """取得 ESPN 備援的球季數據，失敗時轉成 StatsFetchError。"""
    if not config.ESPN_FALLBACK_ENABLED:
        raise StatsFetchError(
            "stats.nba.com 不可用，且已停用 ESPN 備援（config.ESPN_FALLBACK_ENABLED）。"
        )
    try:
        return fetch_espn.fetch_all_team_stats(season, season_type)
    except fetch_espn.EspnFetchError as exc:
        raise StatsFetchError(
            f"stats.nba.com 與 ESPN 皆無法取得球季數據：{exc}"
        ) from exc


def _espn_games_for_date(
    season: str,
    target_date: date,
    season_type: str,
    warnings: list[str],
) -> list[dict]:
    """取得 ESPN 備援的當日賽程，失敗時只留下警告。"""
    if not config.ESPN_FALLBACK_ENABLED:
        return []
    try:
        return fetch_espn.fetch_games_for_date(season, target_date, season_type)
    except fetch_espn.EspnFetchError as exc:
        LOGGER.warning("ESPN 賽程備援失敗：%s", exc)
        warnings.append(f"ESPN 賽程備援失敗：{exc}")
        return []


def fetch_all_team_stats(
    season: str,
    season_type: str = config.SEASON_TYPE,
) -> dict:
    """球季數據：優先 nba_api，不可用或失敗時改用 ESPN 備援。"""
    if not nba_api_available():
        return _espn_team_stats(season, season_type)
    try:
        return _fetch_all_team_stats_nba(season, season_type)
    except StatsFetchError as exc:
        if not config.ESPN_FALLBACK_ENABLED:
            raise
        LOGGER.warning("nba_api 球季數據失敗，改用 ESPN 備援：%s", exc)
        payload = _espn_team_stats(season, season_type)
        payload.setdefault("warnings", []).insert(
            0, f"nba_api 球季數據失敗，已改用 ESPN 備援：{exc}"
        )
        return payload


def fetch_team_game_log(
    season: str,
    season_type: str = config.SEASON_TYPE,
) -> list[dict]:
    """逐場 log：優先 nba_api，不可用或失敗時改用 ESPN 備援。"""
    if nba_api_available():
        try:
            return _fetch_team_game_log_nba(season, season_type)
        except StatsFetchError as exc:
            if not config.ESPN_FALLBACK_ENABLED:
                raise
            LOGGER.warning("nba_api 逐場資料失敗，改用 ESPN 備援：%s", exc)
    if not config.ESPN_FALLBACK_ENABLED:
        raise StatsFetchError("stats.nba.com 不可用，且已停用 ESPN 備援。")
    try:
        return fetch_espn.fetch_team_game_log(season, season_type)
    except fetch_espn.EspnFetchError as exc:
        raise StatsFetchError(f"ESPN 逐場資料也無法取得：{exc}") from exc


def fetch_games_for_date(
    season: str,
    target_date: date,
    season_type: str = config.SEASON_TYPE,
    schedule: list[dict] | None = None,
) -> dict:
    """當日賽程：優先 nba_api，查無資料或不可用時改用 ESPN 備援。"""
    if nba_api_available():
        try:
            payload = _fetch_games_for_date_nba(
                season, target_date, season_type, schedule
            )
        except StatsFetchError as exc:
            LOGGER.warning("nba_api 賽程抓取失敗，改用 ESPN 備援：%s", exc)
            payload = {
                "games": [],
                "schedule": [],
                "warnings": [f"nba_api 賽程抓取失敗：{exc}"],
            }
        if payload["games"]:
            return payload
        espn_games = _espn_games_for_date(
            season, target_date, season_type, payload["warnings"]
        )
        if espn_games:
            payload["games"] = espn_games
            payload["warnings"].append(
                "stats.nba.com 查無當日賽事，已改用 ESPN 備援。"
            )
        return payload

    warnings = ["stats.nba.com 不可用，賽程改用 ESPN 備援。"]
    games = _espn_games_for_date(season, target_date, season_type, warnings)
    return {"games": games, "schedule": [], "warnings": warnings}

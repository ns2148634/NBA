"""傷病報告抓取層（封裝 nbainjuries）。

重點：

* 2025-12-22 起 NBA 官網傷病報告改為 **15 分鐘粒度**，因此以 15 分鐘為步進
  往前探測最近一份可下載的 PDF（最多回溯 24 小時）。
* nbainjuries 內部的 ``requests.get`` 沒有 timeout，這裡先用自帶 timeout 的
  HTTP 檢查確認 PDF 存在，再用 daemon thread + watchdog 限制整體耗時。
* 只保留「當日出賽球隊」的傷兵，並把狀態正規化為嚴重度權重。
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

import requests

from . import config, fetch_espn
from .rating_engine import normalize_person_name

LOGGER = logging.getLogger(__name__)


class InjuryFetchError(RuntimeError):
    """傷病報告抓取失敗。"""


# 依序比對，越前面的規則優先（例如 "Out for Season" 要先於 "Out"）。
_STATUS_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("out for season", "out"),
    ("out indefinitely", "out"),
    ("not with team", "not with team"),
    ("return to competition", "return to competition reconditioning"),
    ("game-time decision", "game-time decision"),
    ("game time decision", "game-time decision"),
    ("day-to-day", "day-to-day"),
    ("day to day", "day-to-day"),
    ("suspended", "suspended"),
    ("suspension", "suspended"),
    ("doubtful", "doubtful"),
    ("questionable", "questionable"),
    ("probable", "probable"),
    ("available", "available"),
    ("g league", "g league"),
    ("nwt", "not with team"),
    ("out", "out"),
    ("rest", "rest"),
)

_REPORT_DATE_KEYS = ("game date", "game_date", "gamedate", "date")
_REPORT_TEAM_KEYS = ("team", "team name", "team_name", "team abbreviation")
_REPORT_PLAYER_KEYS = ("player name", "player_name", "player")
_REPORT_STATUS_KEYS = ("current status", "current_status", "status", "game status")
_REPORT_REASON_KEYS = ("reason", "injury", "notes")
_REPORT_MATCHUP_KEYS = ("matchup", "game", "game matchup")


def normalize_status(raw_value: Any) -> tuple[str, float]:
    """把官網狀態字串正規化為 (狀態代碼, 嚴重度權重)。"""
    text = unicodedata.normalize("NFKD", str(raw_value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = text.replace("/", " ").replace("_", " ")
    text = re.sub(r"[^a-z0-9\- ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "unknown", config.INJURY_STATUS_WEIGHTS["unknown"]
    for pattern, canonical in _STATUS_KEYWORDS:
        if pattern in text:
            weight = config.INJURY_STATUS_WEIGHTS.get(
                canonical, config.INJURY_STATUS_WEIGHTS["unknown"]
            )
            return canonical, weight
    return "unknown", config.INJURY_STATUS_WEIGHTS["unknown"]


#: 「策略性輪休」的判定樣式（用字界避免把 minutes restriction 誤判成 rest）
_REST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brest\b"),
    re.compile(r"load management"),
)


def injury_category(status: Any, status_raw: Any = None, reason: Any = None) -> str:
    """區分「策略性輪休」（rest）與一般傷病（injury）。

    官方報告有時只寫 ``Reason: Rest`` 而沒有傷勢，若不拆開會讓輪休看起來像受傷；
    分類只用於標示與統計，嚴重度仍由 :func:`normalize_status` 決定。
    """
    text = " ".join(str(value or "") for value in (status, status_raw, reason)).lower()
    if any(pattern.search(text) for pattern in _REST_PATTERNS):
        return "rest"
    return "injury"


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip().lower() in {"", "nan", "none", "nat", "na", "<na>"}


def _value(row: dict, candidates: tuple[str, ...]) -> Any:
    for key in candidates:
        if key in row and not _is_missing(row[key]):
            return row[key]
    return None


def _parse_report_date(value: Any) -> date | None:
    if _is_missing(value):
        return None
    text = str(value).strip()
    tokens = text.split()
    token = tokens[0] if tokens else text
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(token, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _url_available(url: str) -> bool:
    """用自帶 timeout 的請求檢查 PDF 是否存在。"""
    try:
        response = requests.get(
            url,
            headers=config.INJURY_HTTP_HEADERS,
            timeout=config.INJURY_REQUEST_TIMEOUT,
            stream=True,
        )
        try:
            return response.status_code == 200
        finally:
            response.close()
    except requests.RequestException as exc:
        LOGGER.debug("傷病報告 URL 檢查失敗（%s）：%s", url, exc)
        return False


def _find_latest_report(now_et: datetime, max_probes: int | None = None) -> dict:
    """以 15 分鐘步進往前找最近一份可下載且可解析的傷病報告。"""
    try:
        from nbainjuries import injury
    except ImportError as exc:  # pragma: no cover - 依賴未安裝時
        raise InjuryFetchError(
            "找不到 nbainjuries，請先執行 `pip install -r requirements.txt`。"
        ) from exc

    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=config.TZ_ET)

    probes = config.INJURY_MAX_PROBES if max_probes is None else int(max_probes)
    step = config.INJURY_STEP_MINUTES
    candidate = now_et.replace(second=0, microsecond=0)
    candidate -= timedelta(minutes=candidate.minute % step)
    earliest = now_et - timedelta(hours=config.INJURY_LOOKBACK_HOURS)

    for probe in range(probes):
        timestamp = candidate - timedelta(minutes=probe * step)
        if timestamp < earliest:
            break
        # nbainjuries 內部會與 naive datetime 比較，必須傳入不帶時區的 ET 時間
        naive_timestamp = timestamp.replace(tzinfo=None)
        try:
            url = injury.gen_url(naive_timestamp)
        except Exception as exc:  # 時間格式錯誤等
            LOGGER.debug("產生傷病報告 URL 失敗（%s）：%s", timestamp, exc)
            continue
        if not _url_available(url):
            continue
        try:
            dataframe = injury.get_reportdata(naive_timestamp, return_df=True)
        except Exception as exc:
            LOGGER.warning("解析傷病報告失敗（%s）：%s", timestamp.isoformat(), exc)
            continue
        if dataframe is None or getattr(dataframe, "empty", True):
            continue
        LOGGER.info(
            "找到傷病報告：%s ET（%s）",
            timestamp.strftime("%Y-%m-%d %H:%M"),
            url,
        )
        return {"timestamp": timestamp, "url": url, "dataframe": dataframe}

    raise InjuryFetchError(
        f"近 {config.INJURY_LOOKBACK_HOURS} 小時內找不到可用的傷病報告。"
    )


def latest_report(
    now_et: datetime | None = None,
    timeout_seconds: float | None = None,
) -> dict:
    """在 watchdog 保護下取得最近一份傷病報告。"""
    moment = now_et or config.now_utc().astimezone(config.TZ_ET)
    timeout = config.INJURY_FETCH_TIMEOUT if timeout_seconds is None else timeout_seconds

    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["report"] = _find_latest_report(moment)
        except BaseException as exc:  # noqa: BLE001 - 需把例外傳回主執行緒
            outcome["error"] = exc

    thread = threading.Thread(target=worker, name="injury-report-fetch", daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        raise InjuryFetchError(
            f"傷病報告抓取逾時（超過 {timeout:.0f} 秒），本次不套用傷病扣分。"
        )
    if "report" in outcome:
        return outcome["report"]

    error = outcome.get("error")
    raise InjuryFetchError(f"傷病報告抓取失敗：{error}") from (
        error if isinstance(error, BaseException) else None
    )


def parse_report(
    dataframe: Any,
    games: list[dict],
    target_date: date,
) -> tuple[list[dict], list[str]]:
    """把傷害報告 DataFrame 轉為傷兵清單（僅保留當日出賽球隊）。"""
    warnings: list[str] = []
    if dataframe is None or getattr(dataframe, "empty", True):
        return [], ["傷病報告內容為空。"]

    frame = dataframe.copy()
    frame.columns = [str(column) for column in frame.columns]
    raw_rows = frame.to_dict(orient="records")

    today_teams = {game["home_team"] for game in games} | {
        game["away_team"] for game in games
    }

    parsed: list[dict] = []
    for raw in raw_rows:
        row = {str(key).strip().lower(): value for key, value in raw.items()}
        team = config.resolve_team(_value(row, _REPORT_TEAM_KEYS))
        if team is None:
            continue
        if today_teams and team not in today_teams:
            continue
        player = _value(row, _REPORT_PLAYER_KEYS)
        if _is_missing(player):
            continue
        status_raw = _value(row, _REPORT_STATUS_KEYS)
        status, severity = normalize_status(status_raw)
        if severity <= 0:
            continue
        game_date = _parse_report_date(_value(row, _REPORT_DATE_KEYS))
        reason = str(_value(row, _REPORT_REASON_KEYS) or "").strip()
        parsed.append(
            {
                "team": team,
                "team_name": config.team_display_name(team),
                "player": str(player).strip(),
                "status": status,
                "status_raw": str(status_raw or "").strip(),
                "severity": severity,
                "reason": reason,
                "game_date": game_date.isoformat() if game_date else None,
                "matchup": str(_value(row, _REPORT_MATCHUP_KEYS) or "").strip(),
                "category": injury_category(status, status_raw, reason),
                "source": "nbainjuries",
            }
        )

    target_iso = target_date.isoformat()
    matched_dates = {row["game_date"] for row in parsed if row["game_date"]}
    if target_iso in matched_dates:
        before = len(parsed)
        parsed = [row for row in parsed if row["game_date"] == target_iso]
        LOGGER.info("傷病報告依比賽日期 %s 過濾：%d -> %d 筆", target_iso, before, len(parsed))
    elif matched_dates:
        other_dates = ", ".join(sorted(matched_dates))
        warnings.append(
            f"最新傷病報告只有其他比賽日期（{other_dates}），沒有 {target_iso}；"
            f"仍保留 {len(parsed)} 筆當日出賽球隊的傷兵，請留意狀態可能已過時。"
        )

    # 同一隊同一球員若重複出現，只保留最嚴重的狀態
    deduped: dict[tuple[str, str], dict] = {}
    for row in parsed:
        key = (row["team"], normalize_person_name(row["player"]))
        existing = deduped.get(key)
        if existing is None or row["severity"] > existing["severity"]:
            deduped[key] = row

    injuries = sorted(
        deduped.values(),
        key=lambda item: (item["team"], -item["severity"], item["player"]),
    )
    return injuries, warnings


def parse_espn_injuries(
    rows: list[dict] | None,
    games: list[dict],
    target_date: date,
    teams_filter: set[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """把 ESPN 傷兵名單轉成與官方報告相同的結構（僅保留當日出賽球隊）。"""
    warnings: list[str] = []
    today_teams = {game["home_team"] for game in games} | {
        game["away_team"] for game in games
    }
    if teams_filter is not None:
        today_teams &= {team for team in teams_filter if team}
        if not today_teams:
            return [], ["ESPN 傷兵名單與指定球隊沒有交集。"]

    deduped: dict[tuple[str, str], dict] = {}
    for raw in rows or []:
        team = config.resolve_team(raw.get("team"))
        if team is None or team not in today_teams:
            continue
        player = str(raw.get("player") or "").strip()
        if not player:
            continue
        status_raw = raw.get("status")
        status, severity = normalize_status(status_raw)
        if severity <= 0:
            continue
        reason = str(raw.get("reason") or "").strip()
        entry = {
            "team": team,
            "team_name": config.team_display_name(team),
            "player": player,
            "status": status,
            "status_raw": str(status_raw or "").strip(),
            "severity": severity,
            "reason": reason,
            "game_date": target_date.isoformat(),
            "matchup": "",
            "category": injury_category(status, status_raw, reason),
            "source": "espn",
            "updated_at": raw.get("updated_at"),
        }
        key = (team, normalize_person_name(player))
        existing = deduped.get(key)
        if existing is None or entry["severity"] > existing["severity"]:
            deduped[key] = entry

    injuries = sorted(
        deduped.values(),
        key=lambda item: (item["team"], -item["severity"], item["player"]),
    )
    if not injuries:
        warnings.append("ESPN 傷兵名單沒有當日出賽球隊的資料。")
    return injuries, warnings


def _merge_espn_injuries(
    injuries: list[dict],
    games: list[dict],
    target_date: date,
    payload: dict,
    official_available: bool,
) -> tuple[list[dict], str]:
    """官方報告不可用（或啟用補漏）時，改用／併用 ESPN 傷兵名單。"""
    playing = {game["home_team"] for game in games} | {
        game["away_team"] for game in games
    }
    covered = {row["team"] for row in injuries}
    missing = playing - covered

    if official_available:
        if not config.ESPN_INJURY_FILL_MISSING_TEAMS or not missing:
            return injuries, "nbainjuries"
    elif not config.ESPN_INJURY_FALLBACK_ENABLED:
        return injuries, "nbainjuries"

    try:
        rows = fetch_espn.fetch_injuries()
    except Exception as exc:  # noqa: BLE001 - ESPN 失敗只記警告
        LOGGER.warning("ESPN 傷兵名單不可用：%s", exc)
        payload["warnings"].append(f"ESPN 傷兵名單無法取得：{exc}")
        return injuries, "nbainjuries"

    teams_filter = missing if official_available else None
    espn_rows, warnings = parse_espn_injuries(
        rows, games, target_date, teams_filter=teams_filter
    )
    payload["warnings"].extend(warnings)
    if official_available:
        existing = {
            (row["team"], normalize_person_name(row.get("player"))) for row in injuries
        }
        added = [
            row
            for row in espn_rows
            if (row["team"], normalize_person_name(row.get("player"))) not in existing
        ]
    else:
        added = espn_rows
    if not added:
        return injuries, "nbainjuries"

    payload["espn_timestamp_utc"] = config.to_iso(config.now_utc())
    if official_available:
        payload["warnings"].append(
            f"官方傷病報告缺少 {len(missing)} 支當日出賽球隊的資料，"
            f"已用 ESPN 補上 {len(added)} 筆傷兵。"
        )
        source = "nbainjuries+espn"
    else:
        payload["warnings"].append(
            f"官方傷病報告不可用，已改用 ESPN 傷兵名單（{len(added)} 筆）。"
        )
        source = "espn"

    merged = injuries + added
    merged.sort(key=lambda item: (item["team"], -item["severity"], item["player"]))
    return merged, source


def _save_snapshot(injuries: list[dict], timestamp_et: datetime) -> None:
    """把解析後的傷兵清單存到 .cache/ 方便除錯（不進版控）。"""
    if not config.ENABLE_CACHE:
        return
    path = config.CACHE_DIR / f"injury_report_{timestamp_et.strftime('%Y%m%d_%H%M')}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(injuries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - 磁碟問題
        LOGGER.debug("傷病快照寫入失敗（%s）：%s", path, exc)
    except Exception as exc:  # noqa: BLE001 - 快照只是除錯用途，不可影響主流程
        LOGGER.debug("傷病快照序列化失敗（%s）：%s", path, exc)


def _save_snapshot_guarded(injuries: list[dict], timestamp_et: datetime) -> None:
    """呼叫 :func:`_save_snapshot`；任何例外都只記錄，不影響預測流程。"""
    try:
        _save_snapshot(injuries, timestamp_et)
    except Exception as exc:  # noqa: BLE001 - 快照僅供除錯
        LOGGER.warning("傷病快照寫入失敗（不影響預測）：%s", exc)


def fetch_injuries(
    games: list[dict],
    target_date: date,
    now_et: datetime | None = None,
) -> dict:
    """取得當日傷兵資料；任何失敗都只回報警告，不中斷預測流程。

    來源優先序：官方傷病報告 PDF（nbainjuries）→ ESPN 傷兵名單。

    * 官方報告完全抓不到時，改用 ESPN（``config.ESPN_INJURY_FALLBACK_ENABLED``）。
    * 官方報告可用時預設不混用；若開啟
      ``config.ESPN_INJURY_FILL_MISSING_TEAMS``，才用 ESPN 補「當日出賽但報告
      完全沒有傷兵資料」的球隊。
    """
    payload: dict = {
        "source": "nbainjuries",
        "available": False,
        "report_timestamp_et": None,
        "report_url": None,
        "espn_timestamp_utc": None,
        "warnings": [],
        "injuries": [],
        "by_team": {},
    }

    report: dict | None = None
    try:
        report = latest_report(now_et)
    except InjuryFetchError as exc:
        LOGGER.warning("傷病資料不可用：%s", exc)
        payload["warnings"].append(str(exc))

    injuries: list[dict] = []
    if report is not None:
        injuries, warnings = parse_report(report["dataframe"], games, target_date)
        payload.update(
            {
                "available": True,
                "report_timestamp_et": report["timestamp"].isoformat(
                    timespec="minutes"
                ),
                "report_url": report["url"],
                "warnings": warnings,
            }
        )

    injuries, source = _merge_espn_injuries(
        injuries,
        games,
        target_date,
        payload,
        official_available=report is not None,
    )
    payload["source"] = source
    payload["injuries"] = injuries
    payload["available"] = bool(report is not None or injuries)

    by_team: dict[str, list[dict]] = {}
    for injury in injuries:
        by_team.setdefault(injury["team"], []).append(injury)
    payload["by_team"] = by_team

    if injuries:
        snapshot_time = (
            report["timestamp"]
            if report is not None
            else config.now_utc().astimezone(config.TZ_ET)
        )
        _save_snapshot_guarded(injuries, snapshot_time)

    LOGGER.info(
        "傷病資料解析完成：%d 筆（%d 支球隊，來源 %s）",
        len(injuries),
        len(by_team),
        source,
    )
    return payload

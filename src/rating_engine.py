"""評分與預測引擎（純函式，方便離線測試）。

模組責任：

* 基礎戰力分：球季 Net Rating（.40）+ 近 10 場 Net Rating（.35）
  + 逐場分差 EMA（.25），換算成 25~75 的分數。
* 傷病扣分：球員影響佔比 × 狀態嚴重度 × 16 × 0.65，單隊上限 12 分。
* 賽果預測：主場優勢、B2B、休息天數、傷病扣分 → 預期分差 / 勝率 / 總分。
* 檔案 IO：原子寫入 JSON、history_log.csv 的 upsert 與賽果回填。
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import unicodedata
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config

LOGGER = logging.getLogger(__name__)

#: history_log.csv 欄位順序（同時作為 upsert 的白名單）
HISTORY_FIELDS: tuple[str, ...] = (
    "game_date",
    "game_time_et",
    "game_id",
    "home_team",
    "away_team",
    "home_rating",
    "away_rating",
    "home_injury_deduction",
    "away_injury_deduction",
    "home_b2b",
    "away_b2b",
    "home_rest_days",
    "away_rest_days",
    "predicted_home_score",
    "predicted_away_score",
    "predicted_margin",
    "predicted_total",
    "home_win_prob",
    "away_win_prob",
    "predicted_winner",
    "injury_details_json",
    "predicted_at",
    "post_game",
    "actual_home_score",
    "actual_away_score",
    "actual_winner",
    "result_correct",
)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def clamp(value: float, minimum: float, maximum: float) -> float:
    """把數值限制在 [minimum, maximum]。"""
    return max(minimum, min(maximum, value))


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


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def _parse_utc_iso(value: Any) -> datetime | None:
    """解析 ``2026-11-18T00:30:00+00:00`` 這類時間字串（失敗回傳 ``None``）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def compute_ema(values: Sequence[float], alpha: float) -> float | None:
    """計算指數移動平均；空序列回傳 ``None``。"""
    if not values:
        return None
    ema: float | None = None
    for value in values:
        if value is None:
            continue
        ema = float(value) if ema is None else alpha * float(value) + (1.0 - alpha) * ema
    return ema


def normalize_person_name(value: str | None) -> str:
    """正規化球員姓名：去重音、處理 "Last, First"、只留英數字。"""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = text.replace("&", " and ")
    if "," in text:
        last, first = (part.strip() for part in text.split(",", 1))
        text = f"{first} {last}"
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# 基礎戰力分
# --------------------------------------------------------------------------
def _league_averages(payload_league: dict | None) -> dict:
    league = dict(config.LEAGUE_AVERAGES_FALLBACK)
    for key, value in (payload_league or {}).items():
        number = _to_float(value)
        if number is not None:
            league[key] = number
    return league


def _mean(values: Iterable[float]) -> float | None:
    cleaned = [value for value in values if value is not None]
    if not cleaned:
        return None
    return sum(cleaned) / len(cleaned)


def compute_team_ratings(stats_payload: dict) -> dict:
    """由 fetch_stats 的輸出計算 30 隊基礎戰力分。"""
    season = stats_payload.get("season")
    season_type = stats_payload.get("season_type", config.SEASON_TYPE)
    teams_payload = stats_payload.get("teams") or {}
    league = _league_averages(stats_payload.get("league"))
    game_log = stats_payload.get("game_log") or []

    log_by_team: dict[str, list[dict]] = {}
    for entry in game_log:
        abbreviation = entry.get("team_abbrev")
        if abbreviation in teams_payload:
            log_by_team.setdefault(abbreviation, []).append(entry)
    for entries in log_by_team.values():
        entries.sort(
            key=lambda item: (item.get("game_date") or "", item.get("game_id") or "")
        )

    weights = config.RATING_WEIGHTS
    ratings: dict[str, dict] = {}

    for abbreviation in sorted(teams_payload):
        team = teams_payload[abbreviation]
        season_block = team.get("season") or {}
        recent_block = team.get("recent") or {}

        games_played = int(team.get("games_played") or 0)
        season_net = _to_float(season_block.get("net_rating"))
        season_off = _to_float(season_block.get("off_rating"))
        season_def = _to_float(season_block.get("def_rating"))
        season_pace = _to_float(season_block.get("pace")) or league["avg_pace"]

        entries = log_by_team.get(abbreviation, [])
        margins = [
            value
            for value in (_to_float(entry.get("plus_minus")) for entry in entries)
            if value is not None
        ]
        points_for = [
            value
            for value in (_to_float(entry.get("points")) for entry in entries)
            if value is not None
        ]
        points_against = []
        for entry in entries:
            points = _to_float(entry.get("points"))
            margin = _to_float(entry.get("plus_minus"))
            if points is not None and margin is not None:
                points_against.append(points - margin)

        shrink = (
            min(1.0, games_played / config.MIN_GAMES_FOR_FULL_WEIGHT)
            if config.MIN_GAMES_FOR_FULL_WEIGHT > 0
            else 1.0
        )
        ema_margin = compute_ema(margins, config.EMA_ALPHA)
        ema_net = (
            ema_margin / season_pace * 100.0
            if ema_margin is not None and season_pace
            else None
        )

        recent_net = _to_float(recent_block.get("net_rating"))
        recent_games = int(recent_block.get("games") or 0)
        if recent_net is None or recent_games < config.MIN_RECENT_GAMES:
            recent_net = (season_net * shrink) if season_net is not None else None

        components: dict[str, float] = {}
        if season_net is not None:
            components["season"] = season_net * shrink
        if recent_net is not None:
            components["recent"] = recent_net
        if ema_net is not None:
            components["ema"] = ema_net

        total_weight = sum(weights[key] for key in components)
        if total_weight > 0:
            blended_net = (
                sum(weights[key] * value for key, value in components.items())
                / total_weight
            )
        else:
            blended_net = 0.0

        expected_margin = blended_net * season_pace / 100.0
        base_rating = clamp(
            config.RATING_BASE + expected_margin,
            config.RATING_MIN,
            config.RATING_MAX,
        )

        if games_played >= config.MIN_GAMES_FOR_FULL_WEIGHT and components:
            data_quality = "ok"
        elif games_played > 0:
            data_quality = "partial"
        else:
            data_quality = "no_games"

        ratings[abbreviation] = {
            "team_id": int(
                team.get("team_id") or config.TEAM_BY_ABBREV[abbreviation]["id"]
            ),
            "abbreviation": abbreviation,
            "name": team.get("name") or config.team_display_name(abbreviation),
            "games_played": games_played,
            "wins": int(team.get("wins") or 0),
            "losses": int(team.get("losses") or 0),
            "base_rating": _round(base_rating),
            "expected_margin": _round(expected_margin),
            "off_rating": _round(
                season_off if season_off is not None else league["avg_off_rating"]
            ),
            "def_rating": _round(
                season_def if season_def is not None else league["avg_def_rating"]
            ),
            "net_rating": _round(season_net if season_net is not None else 0.0),
            "pace": _round(season_pace),
            "blended_net_rating": _round(blended_net),
            "ema_margin": _round(ema_margin),
            "ema_net_rating": _round(ema_net),
            "recent_net_rating": _round(recent_net),
            "points_for": _round(
                _mean(points_for)
                if points_for
                else (season_off or league["avg_off_rating"]) * season_pace / 100.0
            ),
            "points_against": _round(
                _mean(points_against)
                if points_against
                else (season_def or league["avg_def_rating"]) * season_pace / 100.0
            ),
            "last_game_date": entries[-1]["game_date"] if entries else None,
            "data_quality": data_quality,
        }

    generated_at = config.now_utc()
    return {
        "schema_version": config.SCHEMA_VERSION,
        "season": season,
        "season_type": season_type,
        "data_source": stats_payload.get("source") or "nba_api",
        "generated_at": config.to_iso(generated_at),
        "generated_at_taipei": config.to_taipei_iso(generated_at),
        "league": {
            "avg_off_rating": _round(league["avg_off_rating"]),
            "avg_def_rating": _round(league["avg_def_rating"]),
            "avg_net_rating": _round(league["avg_net_rating"]),
            "avg_pace": _round(league["avg_pace"]),
            "teams_count": len(ratings),
        },
        "weights": {
            "season": weights["season"],
            "recent": weights["recent"],
            "ema": weights["ema"],
            "ema_alpha": config.EMA_ALPHA,
            "min_games_for_full_weight": config.MIN_GAMES_FOR_FULL_WEIGHT,
            "recent_games_window": config.RECENT_GAMES_WINDOW,
        },
        "warnings": list(stats_payload.get("warnings") or []),
        "teams": ratings,
    }


# --------------------------------------------------------------------------
# 傷病扣分
# --------------------------------------------------------------------------
def compute_player_impacts(player_rows: list[dict] | None) -> dict:
    """計算每位球員的影響分與球隊內的影響佔比。"""
    by_team: dict[str, list[dict]] = {}

    for row in player_rows or []:
        team = row.get("team_abbrev")
        name = row.get("name")
        minutes = _to_float(row.get("min")) or 0.0
        if not team or not name or minutes < config.PLAYER_MIN_MINUTES:
            continue
        value = sum(
            weight * (_to_float(row.get(key)) or 0.0)
            for key, weight in config.PLAYER_IMPACT_WEIGHTS.items()
        )
        if value <= 0:
            continue
        impact = value * (minutes / config.PLAYER_IMPACT_MINUTE_BASE)
        by_team.setdefault(team, []).append(
            {
                "player_id": row.get("player_id"),
                "name": str(name),
                "name_key": normalize_person_name(name),
                "gp": int(row.get("gp") or 0),
                "min": round(minutes, 2),
                "raw_impact": impact,
            }
        )

    result: dict[str, dict] = {}
    for team, players in by_team.items():
        total = sum(player["raw_impact"] for player in players)
        if total <= 0:
            continue
        for player in players:
            player["impact_share"] = round(player.pop("raw_impact") / total, 6)
        players.sort(key=lambda player: player["impact_share"], reverse=True)
        result[team] = {"total_impact": round(total, 3), "players": players}
    return result


def match_player(
    name: str | None,
    team: str | None,
    impacts: dict,
) -> tuple[dict | None, float]:
    """把傷兵姓名對應到球員數據，回傳 (球員, 相似度)。"""
    key = normalize_person_name(name)
    if not key or not team:
        return None, 0.0

    team_players = (impacts.get(team) or {}).get("players", [])
    for player in team_players:
        if player["name_key"] == key:
            return player, 1.0

    key_tokens = key.split()
    surname = key_tokens[-1]
    same_surname = [
        player for player in team_players if player["name_key"].split()[-1] == surname
    ]

    if len(same_surname) == 1:
        candidate = same_surname[0]
        candidate_tokens = candidate["name_key"].split()
        if len(key_tokens) > 1 and len(candidate_tokens) > 1:
            first_key, first_candidate = key_tokens[0], candidate_tokens[0]
            if (
                first_key == first_candidate
                or first_key.startswith(first_candidate)
                or first_candidate.startswith(first_key)
            ):
                return candidate, 0.97
            if SequenceMatcher(None, first_key, first_candidate).ratio() >= 0.8:
                return candidate, 0.93
        return candidate, 0.90

    if len(same_surname) > 1:
        best = max(
            same_surname,
            key=lambda player: SequenceMatcher(None, key, player["name_key"]).ratio(),
        )
        score = SequenceMatcher(None, key, best["name_key"]).ratio()
        if score >= 0.95:
            return best, round(score, 3)
        return None, round(score, 3)

    # 全聯盟唯一且完整同名（例如剛被交易，球員數據仍在舊隊）
    league_matches = [
        player
        for data in impacts.values()
        for player in data.get("players", [])
        if player["name_key"] == key
    ]
    if len(league_matches) == 1:
        return league_matches[0], 1.0
    return None, 0.0


def compute_injury_deductions(
    injuries_by_team: dict | None,
    impacts: dict | None,
) -> dict:
    """把傷兵清單換算成每隊的扣分（含明細，單隊上限 12 分）。"""
    impacts = impacts or {}
    summary: dict[str, dict] = {}

    for team, injuries in (injuries_by_team or {}).items():
        seen: set[str] = set()
        details: list[dict] = []
        total_raw = 0.0

        ordered = sorted(
            injuries or [],
            key=lambda item: _to_float(item.get("severity")) or 0.0,
            reverse=True,
        )
        for injury in ordered:
            player_name = injury.get("player") or ""
            key = normalize_person_name(player_name)
            severity = _to_float(injury.get("severity")) or 0.0
            if not key or key in seen or severity <= 0:
                continue
            seen.add(key)

            player, score = match_player(player_name, team, impacts)
            if player is not None:
                share = _to_float(player.get("impact_share")) or 0.0
            else:
                share = config.DEFAULT_UNKNOWN_PLAYER_SHARE

            deduction = (
                share
                * severity
                * config.INJURY_IMPACT_SCALE
                * config.INJURY_REPLACEMENT_FACTOR
            )
            total_raw += deduction
            details.append(
                {
                    "player": player_name,
                    "status": injury.get("status"),
                    "status_raw": injury.get("status_raw"),
                    "reason": injury.get("reason"),
                    "category": injury.get("category") or "injury",
                    "source": injury.get("source") or "nbainjuries",
                    "severity": round(severity, 2),
                    "impact_share": round(share, 4),
                    "matched": player is not None,
                    "match_score": round(score, 3),
                    "deduction": round(deduction, 2),
                }
            )

        capped = min(total_raw, config.INJURY_MAX_TEAM_DEDUCTION)
        details.sort(key=lambda item: item["deduction"], reverse=True)
        summary[team] = {
            "deduction": round(capped, 2),
            "raw_deduction": round(total_raw, 2),
            "capped": total_raw > config.INJURY_MAX_TEAM_DEDUCTION,
            "injuries": details,
        }

    return summary


# --------------------------------------------------------------------------
# 賽果預測
# --------------------------------------------------------------------------
def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _rest_adjustment(rest_days: Any) -> float:
    """休息天數補償：休 1 天為基準，每多休 1 天 +0.25（上限 1.0）。"""
    days = _to_float(rest_days)
    if days is None:
        return 0.0
    return min(
        config.REST_ADVANTAGE_CAP,
        max(0.0, days - 1.0) * config.REST_ADVANTAGE_PER_DAY,
    )


def _estimate_points(team: dict, opponent: dict, league: dict, pace: float) -> float:
    league_def = _to_float(league.get("avg_def_rating")) or config.LEAGUE_AVERAGES_FALLBACK[
        "avg_def_rating"
    ]
    off_rating = _to_float(team.get("off_rating")) or league_def
    opp_def_rating = _to_float(opponent.get("def_rating")) or league_def
    return off_rating * opp_def_rating / league_def * pace / 100.0


def predict_game(
    game: dict,
    team_ratings: dict,
    injury_summary: dict | None = None,
    generated_at: datetime | None = None,
) -> dict | None:
    """預測單場比賽；缺少任一方戰力分時回傳 ``None``。"""
    teams = team_ratings.get("teams") or {}
    home = teams.get(game.get("home_team"))
    away = teams.get(game.get("away_team"))
    if home is None or away is None:
        return None

    league = team_ratings.get("league") or {}
    home_abbrev = home["abbreviation"]
    away_abbrev = away["abbreviation"]
    injury_summary = injury_summary or {}
    home_injury = injury_summary.get(home_abbrev) or {}
    away_injury = injury_summary.get(away_abbrev) or {}

    home_rating = _to_float(home.get("base_rating")) or config.RATING_BASE
    away_rating = _to_float(away.get("base_rating")) or config.RATING_BASE
    home_deduction = _to_float(home_injury.get("deduction")) or 0.0
    away_deduction = _to_float(away_injury.get("deduction")) or 0.0

    home_effective = home_rating - home_deduction
    away_effective = away_rating - away_deduction

    hca = (
        config.NEUTRAL_COURT_ADVANTAGE
        if game.get("is_neutral")
        else config.HOME_COURT_ADVANTAGE
    )
    home_adjustment = (
        hca
        + _rest_adjustment(game.get("home_rest_days"))
        - (config.B2B_PENALTY if game.get("home_b2b") else 0.0)
    )
    away_adjustment = _rest_adjustment(game.get("away_rest_days")) - (
        config.B2B_PENALTY if game.get("away_b2b") else 0.0
    )

    margin = (home_effective - away_effective) + home_adjustment - away_adjustment

    home_pace = _to_float(home.get("pace")) or _to_float(league.get("avg_pace")) or 99.0
    away_pace = _to_float(away.get("pace")) or home_pace
    game_pace = (home_pace + away_pace) / 2.0

    home_points = _estimate_points(home, away, league, game_pace)
    away_points = _estimate_points(away, home, league, game_pace)
    total = home_points + away_points - (
        config.INJURY_TOTAL_SCORING_FACTOR * (home_deduction + away_deduction)
    )
    total = clamp(total, config.TOTAL_MIN, config.TOTAL_MAX)

    predicted_home_score = (total + margin) / 2.0
    predicted_away_score = total - predicted_home_score

    home_win_prob = clamp(
        _normal_cdf(margin / config.WIN_PROB_SIGMA),
        config.WIN_PROB_MIN,
        config.WIN_PROB_MAX,
    )

    absolute_margin = abs(margin)
    if absolute_margin >= config.CONFIDENCE_HIGH_MARGIN:
        confidence = "high"
    elif absolute_margin >= config.CONFIDENCE_MEDIUM_MARGIN:
        confidence = "medium"
    else:
        confidence = "low"

    predicted_winner = home_abbrev if margin >= 0 else away_abbrev
    predicted_winner_name = home.get("name") if margin >= 0 else away.get("name")

    moment = generated_at or config.now_utc()
    moment_utc = moment.astimezone(timezone.utc)
    tipoff = _parse_utc_iso(game.get("game_time_utc"))
    post_game = bool(tipoff is not None and moment_utc > tipoff)
    lead_time_minutes = (
        int(round((tipoff - moment_utc).total_seconds() / 60)) if tipoff else None
    )

    return {
        "game_id": game.get("game_id"),
        "game_date": game.get("game_date"),
        "game_time_et": game.get("game_time_et"),
        "game_time_taipei": game.get("game_time_taipei"),
        "status": game.get("status"),
        "status_text": game.get("status_text"),
        "venue": game.get("venue"),
        "is_neutral": bool(game.get("is_neutral")),
        "home_team": home_abbrev,
        "home_team_name": home.get("name"),
        "away_team": away_abbrev,
        "away_team_name": away.get("name"),
        "home_rating": _round(home_rating),
        "away_rating": _round(away_rating),
        "home_injury_deduction": _round(home_deduction),
        "away_injury_deduction": _round(away_deduction),
        "home_effective_rating": _round(home_effective),
        "away_effective_rating": _round(away_effective),
        "home_rest_days": game.get("home_rest_days"),
        "away_rest_days": game.get("away_rest_days"),
        "home_b2b": game.get("home_b2b"),
        "away_b2b": game.get("away_b2b"),
        "predicted_margin": _round(margin),
        "predicted_home_score": _round(predicted_home_score, 1),
        "predicted_away_score": _round(predicted_away_score, 1),
        "predicted_total": _round(total, 1),
        "home_win_prob": _round(home_win_prob, 4),
        "away_win_prob": _round(1.0 - home_win_prob, 4),
        "predicted_winner": predicted_winner,
        "predicted_winner_name": predicted_winner_name,
        "confidence": confidence,
        "home_injuries": list(home_injury.get("injuries") or []),
        "away_injuries": list(away_injury.get("injuries") or []),
        "factors": {
            "home_court_advantage": _round(hca),
            "home_rest_adjustment": _round(home_adjustment),
            "away_rest_adjustment": _round(away_adjustment),
            "pace": _round(game_pace),
        },
        "generated_at": config.to_iso(moment),
        "post_game": post_game,
        "lead_time_minutes": lead_time_minutes,
    }


def build_predictions(
    games: list[dict],
    team_ratings: dict,
    injuries: dict | None,
    player_rows: list[dict] | None,
    season: str | None = None,
    target_date: date | None = None,
    generated_at: datetime | None = None,
) -> dict:
    """組出 daily_predictions.json 的完整內容。"""
    injuries = injuries or {}
    impacts = compute_player_impacts(player_rows or [])
    injury_summary = compute_injury_deductions(injuries.get("by_team") or {}, impacts)

    moment = generated_at or config.now_utc()
    predictions: list[dict] = []
    warnings: list[str] = []

    for game in games:
        prediction = predict_game(
            game, team_ratings, injury_summary, generated_at=moment
        )
        if prediction is None:
            warnings.append(
                f"{game.get('away_team')} @ {game.get('home_team')} 缺少戰力分資料，已略過。"
            )
            continue
        predictions.append(prediction)

    predictions.sort(
        key=lambda item: (item.get("game_time_et") or "", item.get("game_id") or "")
    )

    return {
        "schema_version": config.SCHEMA_VERSION,
        "season": season or team_ratings.get("season"),
        "data_source": team_ratings.get("data_source"),
        "game_date": target_date.isoformat()
        if isinstance(target_date, date)
        else target_date,
        "generated_at": config.to_iso(moment),
        "generated_at_taipei": config.to_taipei_iso(moment),
        "ratings_generated_at": team_ratings.get("generated_at"),
        "injury_report": {
            "source": injuries.get("source", "nbainjuries"),
            "available": bool(injuries.get("available")),
            "report_timestamp_et": injuries.get("report_timestamp_et"),
            "report_url": injuries.get("report_url"),
            "warnings": list(injuries.get("warnings") or []),
        },
        "games_count": len(predictions),
        "warnings": warnings + list(injuries.get("warnings") or []),
        "predictions": predictions,
    }


# --------------------------------------------------------------------------
# 檔案 IO
# --------------------------------------------------------------------------
#: 賽果欄位；upsert 時一律保留既有值，避免重跑預測覆蓋已回填的結果
_ACTUAL_FIELDS: tuple[str, ...] = (
    "actual_home_score",
    "actual_away_score",
    "actual_winner",
    "result_correct",
)


def write_json(path: str | Path, payload: dict) -> Path:
    """原子寫入 JSON（先寫 .tmp 再 os.replace）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, target)
    return target


def read_json(path: str | Path) -> Any:
    target = Path(path)
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("讀取 JSON 失敗（%s）：%s", target, exc)
        return None


def load_team_ratings(path: str | Path | None = None) -> dict | None:
    """讀取 team_ratings.json；內容不完整時回傳 ``None``。"""
    payload = read_json(path or config.TEAM_RATINGS_PATH)
    if not isinstance(payload, dict):
        return None
    teams = payload.get("teams")
    if not isinstance(teams, dict) or not teams:
        return None
    return payload


def ratings_age_hours(payload: dict | None) -> float | None:
    """回傳戰力分距離現在幾小時；無法解析時回傳 ``None``。"""
    if not isinstance(payload, dict):
        return None
    generated = payload.get("generated_at")
    if not generated:
        return None
    try:
        moment = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (config.now_utc() - moment.astimezone(timezone.utc)).total_seconds() / 3600.0


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def prediction_to_history_row(prediction: dict) -> dict:
    """把單筆預測攤平成 history_log.csv 的一列。"""
    injury_details = {
        "home": prediction.get("home_injuries") or [],
        "away": prediction.get("away_injuries") or [],
    }
    return {
        "game_date": prediction.get("game_date"),
        "game_time_et": prediction.get("game_time_et"),
        "game_id": prediction.get("game_id"),
        "home_team": prediction.get("home_team"),
        "away_team": prediction.get("away_team"),
        "home_rating": prediction.get("home_rating"),
        "away_rating": prediction.get("away_rating"),
        "home_injury_deduction": prediction.get("home_injury_deduction"),
        "away_injury_deduction": prediction.get("away_injury_deduction"),
        "home_b2b": prediction.get("home_b2b"),
        "away_b2b": prediction.get("away_b2b"),
        "home_rest_days": prediction.get("home_rest_days"),
        "away_rest_days": prediction.get("away_rest_days"),
        "predicted_home_score": prediction.get("predicted_home_score"),
        "predicted_away_score": prediction.get("predicted_away_score"),
        "predicted_margin": prediction.get("predicted_margin"),
        "predicted_total": prediction.get("predicted_total"),
        "home_win_prob": prediction.get("home_win_prob"),
        "away_win_prob": prediction.get("away_win_prob"),
        "predicted_winner": prediction.get("predicted_winner"),
        "injury_details_json": json.dumps(
            injury_details, ensure_ascii=False, separators=(",", ":")
        ),
        "predicted_at": prediction.get("generated_at"),
        "post_game": "true" if prediction.get("post_game") else "false",
    }


def build_history_rows(predictions_payload: dict) -> list[dict]:
    """把 daily_predictions.json 轉成 history 資料列。"""
    return [
        prediction_to_history_row(prediction)
        for prediction in predictions_payload.get("predictions") or []
    ]


def read_history(path: str | Path | None = None) -> list[dict]:
    """讀取 history_log.csv（不存在時回傳空 list）。"""
    target = Path(path or config.HISTORY_LOG_PATH)
    if not target.exists():
        return []
    try:
        with target.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError as exc:
        LOGGER.warning("讀取 history_log.csv 失敗：%s", exc)
        return []


def write_history(path: str | Path | None, rows: list[dict]) -> Path:
    """以固定欄位順序覆寫 history_log.csv。"""
    target = Path(path or config.HISTORY_LOG_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(HISTORY_FIELDS), lineterminator="\n"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {field: _stringify(row.get(field, "")) for field in HISTORY_FIELDS}
            )
    os.replace(temporary, target)
    return target


#: ``post_game`` 已明確標記時的字串（不再重複回寫）
_POST_GAME_VALUES: tuple[str, ...] = ("true", "false", "1", "0", "yes", "no")


def is_predicted_after_tipoff(
    predicted_at: Any,
    game_date: Any,
    game_time_et: Any,
) -> bool:
    """``predicted_at`` 是否晚於「比賽日 + 美東開球時間」。

    缺欄位或格式錯誤一律視為賽前（回傳 ``False``），避免把舊資料誤判成賽後預測。
    """
    date_text = str(game_date or "").strip()
    time_text = str(game_time_et or "").strip()
    predicted = _parse_utc_iso(predicted_at)
    if predicted is None or not date_text or not time_text:
        return False
    tipoff: datetime | None = None
    for candidate in (f"{date_text}T{time_text}", f"{date_text}T{time_text}:00"):
        try:
            tipoff = datetime.fromisoformat(candidate).replace(tzinfo=config.TZ_ET)
            break
        except ValueError:
            continue
    if tipoff is None:
        return False
    return predicted > tipoff.astimezone(timezone.utc)


def fill_missing_post_game_flags(path: str | Path | None = None) -> int:
    """把舊資料缺漏的 ``post_game`` 依 ``predicted_at`` 補寫回 CSV，回傳補寫列數。

    舊版 ``history_log.csv`` 沒有 ``post_game`` 欄位，統計命中率時每次都得即時推算；
    補寫後結果就固定下來，日後即使時區規則調整也不會改動歷史賽事的判定。
    """
    target = Path(path or config.HISTORY_LOG_PATH)
    rows = read_history(target)
    if not rows:
        return 0
    updated = 0
    for row in rows:
        flag = str(row.get("post_game") or "").strip().lower()
        if flag in _POST_GAME_VALUES:
            continue
        row["post_game"] = (
            "true"
            if is_predicted_after_tipoff(
                row.get("predicted_at"),
                row.get("game_date"),
                row.get("game_time_et"),
            )
            else "false"
        )
        updated += 1
    if updated:
        write_history(target, rows)
        LOGGER.info("已補寫 %d 筆 history_log.csv 的 post_game 標記。", updated)
    return updated


def upsert_history(
    path: str | Path | None,
    rows: list[dict],
) -> tuple[int, int]:
    """以 (game_date, game_id) 為鍵 upsert，回傳 (總列數, 本次寫入列數)。"""
    target = Path(path or config.HISTORY_LOG_PATH)
    existing = read_history(target)
    merged: dict[tuple[str, str], dict] = {
        (str(row.get("game_date") or ""), str(row.get("game_id") or "")): row
        for row in existing
    }

    written = 0
    for row in rows:
        game_id = str(row.get("game_id") or "")
        if not game_id:
            continue
        key = (str(row.get("game_date") or ""), game_id)
        current = merged.get(key, {})
        combined = {field: current.get(field, "") for field in HISTORY_FIELDS}
        for field in HISTORY_FIELDS:
            value = row.get(field)
            if value not in (None, ""):
                combined[field] = value
        # 既有賽果永遠優先，避免重跑預測時把結果洗掉
        for field in _ACTUAL_FIELDS:
            if current.get(field):
                combined[field] = current[field]
        merged[key] = combined
        written += 1

    ordered = sorted(
        merged.values(),
        key=lambda item: (
            str(item.get("game_date") or ""),
            str(item.get("game_id") or ""),
        ),
    )
    write_history(target, ordered)
    return len(ordered), written


def settle_history(
    path: str | Path | None,
    game_log_rows: list[dict],
    through_date: date | str | None = None,
) -> int:
    """用團隊逐場 box log 回填 history_log.csv 的實際比分，回傳更新筆數。"""
    target = Path(path or config.HISTORY_LOG_PATH)
    history = read_history(target)
    if not history:
        return 0

    scores: dict[str, dict[str, float]] = {}
    for row in game_log_rows or []:
        game_id = str(row.get("game_id") or "")
        team = row.get("team_abbrev")
        points = _to_float(row.get("points"))
        if not game_id or not team or points is None:
            continue
        scores.setdefault(game_id, {})[team] = points
    if not scores:
        return 0

    cutoff = (
        through_date.isoformat() if isinstance(through_date, date) else str(through_date)
    ) if through_date is not None else None

    updated = 0
    for row in history:
        if row.get("actual_home_score") and row.get("actual_away_score"):
            continue
        if cutoff and str(row.get("game_date") or "") > cutoff:
            continue
        game_scores = scores.get(str(row.get("game_id") or ""))
        if not game_scores:
            continue
        home = row.get("home_team")
        away = row.get("away_team")
        home_points = game_scores.get(home)
        away_points = game_scores.get(away)
        if home_points is None or away_points is None:
            continue

        home_points = int(round(home_points))
        away_points = int(round(away_points))
        actual_winner = home if home_points > away_points else away
        row["actual_home_score"] = home_points
        row["actual_away_score"] = away_points
        row["actual_winner"] = actual_winner
        row["result_correct"] = (
            "true" if str(row.get("predicted_winner") or "") == actual_winner else "false"
        )
        updated += 1

    if updated:
        write_history(target, history)
    return updated

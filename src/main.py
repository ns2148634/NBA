"""指令列入口：``python -m src.main``。

範例：

    python -m src.main --mode base
    python -m src.main --mode predict --date 2026-11-17
    python -m src.main --mode both --skip-injuries --dry-run
    python -m src.main --mode settle
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

from . import config, fetch_injuries, fetch_stats, rating_engine

LOGGER = logging.getLogger("nba.main")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="NBA 每日球隊戰力評分與賽果預測（資料來源：nba_api + nbainjuries）",
    )
    parser.add_argument(
        "--mode",
        choices=("base", "predict", "both", "settle"),
        default="both",
        help="base=只更新戰力分、predict=產生當日預測、both=兩者、settle=回填歷史賽果",
    )
    parser.add_argument(
        "--date",
        dest="game_date",
        default=None,
        help="比賽日期（美東時間 YYYY-MM-DD），預設為今天",
    )
    parser.add_argument(
        "--season",
        default=None,
        help="賽季字串（例如 2026-27），預設依日期自動推算",
    )
    parser.add_argument(
        "--season-type",
        default=config.SEASON_TYPE,
        choices=(config.SEASON_TYPE, config.PLAYOFF_SEASON_TYPE),
        help="球季類型",
    )
    parser.add_argument(
        "--skip-injuries",
        action="store_true",
        help="不抓取傷病報告（不套用傷病扣分）",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="忽略 .cache/ 的 API 快取，強制重新抓取",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只計算並印出結果，不寫入 data/",
    )
    parser.add_argument(
        "--log-level",
        default=config.LOG_LEVEL,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="log 等級",
    )
    return parser.parse_args(argv)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format=config.LOG_FORMAT,
        datefmt=config.LOG_DATEFMT,
    )


def parse_target_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"日期格式錯誤：{value}（應為 YYYY-MM-DD）") from exc


def _log_rating_table(ratings: dict) -> None:
    teams = sorted(
        (ratings.get("teams") or {}).values(),
        key=lambda item: item.get("base_rating") or 0.0,
        reverse=True,
    )
    LOGGER.info("戰力分排名（賽季 %s，共 %d 隊）", ratings.get("season"), len(teams))
    for rank, team in enumerate(teams, start=1):
        LOGGER.info(
            "#%2d %-4s %-24s 評分 %5.2f | ORtg %5.1f DRtg %5.1f Pace %5.1f | GP %2d %s",
            rank,
            team.get("abbreviation"),
            team.get("name"),
            team.get("base_rating") or 0.0,
            team.get("off_rating") or 0.0,
            team.get("def_rating") or 0.0,
            team.get("pace") or 0.0,
            team.get("games_played") or 0,
            team.get("data_quality"),
        )


def _log_predictions(payload: dict) -> None:
    predictions = payload.get("predictions") or []
    if not predictions:
        LOGGER.warning("%s（ET）沒有可預測的賽事。", payload.get("game_date"))
        return
    status = "可用" if (payload.get("injury_report") or {}).get("available") else "不可用"
    LOGGER.info(
        "%s（ET）共 %d 場預測，傷病資料：%s",
        payload.get("game_date"),
        len(predictions),
        status,
    )
    for item in predictions:
        if item.get("predicted_winner") == item.get("home_team"):
            winner, winner_score = item.get("home_team"), item.get("predicted_home_score")
            loser, loser_score = item.get("away_team"), item.get("predicted_away_score")
        else:
            winner, winner_score = item.get("away_team"), item.get("predicted_away_score")
            loser, loser_score = item.get("home_team"), item.get("predicted_home_score")
        LOGGER.info(
            "%s @ %s（%s）預測勝隊 %s %s - %s %s | 分差 %+.1f 總分 %.1f | 主勝率 %.1f%% | 信心 %s",
            item.get("away_team"),
            item.get("home_team"),
            item.get("game_time_et") or "--:--",
            winner,
            winner_score,
            loser,
            loser_score,
            item.get("predicted_margin") or 0.0,
            item.get("predicted_total") or 0.0,
            (item.get("home_win_prob") or 0.0) * 100.0,
            item.get("confidence"),
        )


def run_base(args: argparse.Namespace, season: str) -> dict:
    """更新 data/team_ratings.json。"""
    LOGGER.info("開始更新基礎戰力分（%s / %s）", season, args.season_type)
    stats_payload = fetch_stats.fetch_all_team_stats(season, args.season_type)
    for message in stats_payload.get("warnings") or []:
        LOGGER.warning("資料警告：%s", message)

    ratings = rating_engine.compute_team_ratings(stats_payload)
    if not ratings.get("teams"):
        raise fetch_stats.StatsFetchError("戰力分計算結果為空，請確認賽季參數。")

    if args.dry_run:
        LOGGER.info("--dry-run：不寫入 %s", config.TEAM_RATINGS_PATH)
    else:
        rating_engine.write_json(config.TEAM_RATINGS_PATH, ratings)
        LOGGER.info("已寫入 %s", config.TEAM_RATINGS_PATH)

    _log_rating_table(ratings)
    return ratings


def load_or_refresh_ratings(args: argparse.Namespace, season: str) -> dict:
    """優先沿用 26 小時內且賽季相符的戰力分，否則重新計算。"""
    ratings = rating_engine.load_team_ratings()
    if ratings is not None:
        age = rating_engine.ratings_age_hours(ratings)
        same_season = ratings.get("season") == season
        if same_season and age is not None and age <= config.RATING_MAX_AGE_HOURS:
            LOGGER.info(
                "沿用既有戰力分（產生於 %s，距今 %.1f 小時）",
                ratings.get("generated_at"),
                age,
            )
            return ratings
        LOGGER.info(
            "既有戰力分需要更新（season=%s，age=%s 小時）",
            ratings.get("season"),
            f"{age:.1f}" if age is not None else "未知",
        )
    else:
        LOGGER.info("找不到可用的 team_ratings.json，先計算基礎戰力分。")
    return run_base(args, season)


def _persist_predictions(args: argparse.Namespace, payload: dict) -> None:
    if args.dry_run:
        LOGGER.info(
            "--dry-run：不寫入 %s 與 %s",
            config.DAILY_PREDICTIONS_PATH,
            config.HISTORY_LOG_PATH,
        )
        return
    rating_engine.write_json(config.DAILY_PREDICTIONS_PATH, payload)
    history_rows = rating_engine.build_history_rows(payload)
    total_rows, written_rows = rating_engine.upsert_history(
        config.HISTORY_LOG_PATH, history_rows
    )
    LOGGER.info(
        "已寫入 %s（%d 場）與 history_log.csv（總計 %d 列，本次寫入 %d 列）",
        config.DAILY_PREDICTIONS_PATH,
        payload.get("games_count") or 0,
        total_rows,
        written_rows,
    )


def _settle_history(args: argparse.Namespace, season: str, target_date: date) -> int:
    if args.dry_run:
        return 0
    try:
        game_log = fetch_stats.fetch_team_game_log(season, args.season_type)
    except fetch_stats.StatsFetchError as exc:
        LOGGER.warning("無法取得逐場資料回填賽果：%s", exc)
        return 0
    updated = rating_engine.settle_history(
        config.HISTORY_LOG_PATH, game_log, through_date=target_date
    )
    if updated:
        LOGGER.info("已回填 %d 筆歷史賽果。", updated)
    else:
        LOGGER.info("沒有需要回填的歷史賽果。")
    return updated


def run_predict(args: argparse.Namespace, season: str, target_date: date) -> dict:
    """產生 data/daily_predictions.json 並 upsert history_log.csv。"""
    ratings = load_or_refresh_ratings(args, season)

    try:
        games_payload = fetch_stats.fetch_games_for_date(
            season, target_date, args.season_type
        )
    except fetch_stats.StatsFetchError as exc:
        LOGGER.error("賽程抓取失敗：%s", exc)
        payload = rating_engine.build_predictions(
            [],
            ratings,
            {"available": False, "warnings": [f"賽程資料不可用：{exc}"]},
            [],
            season=season,
            target_date=target_date,
        )
        payload["warnings"].append("賽程資料不可用，本次未產生任何預測。")
        _persist_predictions(args, payload)
        LOGGER.warning("本次不產生預測，結束時仍視為成功。")
        return payload

    for message in games_payload.get("warnings") or []:
        LOGGER.warning("賽程警告：%s", message)

    games = games_payload.get("games") or []
    LOGGER.info("%s（ET）共有 %d 場比賽。", target_date, len(games))

    if not games:
        injuries = {
            "source": "nbainjuries",
            "available": False,
            "warnings": ["當日沒有賽事，未抓取傷病報告。"],
            "by_team": {},
        }
        payload = rating_engine.build_predictions(
            [],
            ratings,
            injuries,
            [],
            season=season,
            target_date=target_date,
        )
        payload["warnings"].append("當日沒有賽事。")
        _persist_predictions(args, payload)
        _log_predictions(payload)
        _settle_history(args, season, target_date)
        return payload

    if args.skip_injuries:
        injuries: dict = {
            "source": "disabled",
            "available": False,
            "warnings": ["--skip-injuries 已啟用，本次不套用傷病扣分。"],
            "by_team": {},
        }
    else:
        injuries = fetch_injuries.fetch_injuries(games, target_date)
    for message in injuries.get("warnings") or []:
        LOGGER.warning("傷病警告：%s", message)

    player_rows: list[dict] = []
    if injuries.get("available"):
        try:
            player_rows = fetch_stats.fetch_player_season_stats(
                season, args.season_type
            )
        except fetch_stats.StatsFetchError as exc:
            LOGGER.warning("球員數據抓取失敗，傷兵改用預設影響佔比：%s", exc)
            injuries.setdefault("warnings", []).append(str(exc))

    payload = rating_engine.build_predictions(
        games,
        ratings,
        injuries,
        player_rows,
        season=season,
        target_date=target_date,
    )
    _persist_predictions(args, payload)
    _log_predictions(payload)
    _settle_history(args, season, target_date)
    return payload


def run_settle(args: argparse.Namespace, season: str, target_date: date) -> int:
    """只回填 history_log.csv 的賽果。"""
    if args.dry_run:
        LOGGER.info("--dry-run：不寫入 %s", config.HISTORY_LOG_PATH)
        return 0
    try:
        game_log = fetch_stats.fetch_team_game_log(season, args.season_type)
    except fetch_stats.StatsFetchError as exc:
        raise RuntimeError(f"無法取得逐場資料：{exc}") from exc
    updated = rating_engine.settle_history(
        config.HISTORY_LOG_PATH, game_log, through_date=target_date
    )
    LOGGER.info("賽果回填完成：更新 %d 筆。", updated)
    return updated


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)
    if args.no_cache:
        config.ENABLE_CACHE = False

    try:
        target_date = parse_target_date(args.game_date) or config.now_utc().astimezone(
            config.TZ_ET
        ).date()
        season = args.season or config.resolve_season(target_date)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 2

    LOGGER.info(
        "執行模式=%s 日期=%s（ET） 賽季=%s 球季類型=%s",
        args.mode,
        target_date,
        season,
        args.season_type,
    )

    try:
        if args.mode in ("base", "both"):
            run_base(args, season)
        if args.mode == "settle":
            run_settle(args, season, target_date)
        if args.mode in ("predict", "both"):
            run_predict(args, season, target_date)
    except (fetch_stats.StatsFetchError, fetch_injuries.InjuryFetchError) as exc:
        LOGGER.error("資料抓取失敗：%s", exc)
        return 1
    except RuntimeError as exc:
        LOGGER.error("執行失敗：%s", exc)
        return 1
    except Exception:  # noqa: BLE001 - CLI 需要統一 exit code
        LOGGER.exception("未預期的錯誤")
        return 1

    LOGGER.info("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""離線單元測試（不需網路，使用合成資料）。

執行：``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from src import config, fetch_espn, fetch_injuries, fetch_stats, rating_engine
from src import build_site as build_site_module
from src import main as main_module


class FakeFrame:
    """模擬 pandas.DataFrame 的最小介面，避免測試相依 pandas。"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = [dict(row) for row in rows]
        self.columns = list(rows[0].keys()) if rows else []
        self.empty = not rows

    def copy(self) -> "FakeFrame":
        return FakeFrame(self._rows)

    def to_dict(self, orient: str = "records") -> list[dict]:
        if orient != "records":
            raise ValueError(orient)
        return [dict(row) for row in self._rows]

    def __setitem__(self, key: str, value) -> None:
        for row in self._rows:
            row[key] = value


def _team_payload(abbreviation: str, net_rating: float, pace: float = 100.0, games: int = 12) -> dict:
    meta = config.TEAM_BY_ABBREV[abbreviation]
    return {
        "team_id": meta["id"],
        "abbreviation": abbreviation,
        "name": meta["full_name"],
        "games_played": games,
        "wins": games // 2,
        "losses": games - games // 2,
        "season": {
            "off_rating": 114.0 + net_rating / 2.0,
            "def_rating": 114.0 - net_rating / 2.0,
            "net_rating": net_rating,
            "pace": pace,
        },
        "recent": {
            "off_rating": 114.0 + net_rating / 2.0,
            "def_rating": 114.0 - net_rating / 2.0,
            "net_rating": net_rating,
            "pace": pace,
            "games": games,
        },
    }


def _stats_payload() -> dict:
    teams = {
        "BOS": _team_payload("BOS", 8.0),
        "NYK": _team_payload("NYK", -6.0),
    }
    game_log: list[dict] = []
    for index in range(12):
        game_date = f"2026-11-{index + 1:02d}"
        game_log.append(
            {
                "game_id": f"0022600{index:03d}",
                "team_id": config.TEAM_BY_ABBREV["BOS"]["id"],
                "team_abbrev": "BOS",
                "game_date": game_date,
                "plus_minus": 8.0,
                "points": 115.0,
                "wl": "W",
            }
        )
        game_log.append(
            {
                "game_id": f"0022600{index:03d}",
                "team_id": config.TEAM_BY_ABBREV["NYK"]["id"],
                "team_abbrev": "NYK",
                "game_date": game_date,
                "plus_minus": -6.0,
                "points": 105.0,
                "wl": "L",
            }
        )
    return {
        "season": "2026-27",
        "season_type": config.SEASON_TYPE,
        "fetched_at": "2026-11-17T00:00:00+00:00",
        "teams": teams,
        "league": {
            "avg_off_rating": 114.0,
            "avg_def_rating": 114.0,
            "avg_net_rating": 0.0,
            "avg_pace": 100.0,
        },
        "game_log": game_log,
        "warnings": [],
    }


class EmaAndRatingTests(unittest.TestCase):
    def test_compute_ema(self) -> None:
        self.assertIsNone(rating_engine.compute_ema([], 0.2))
        self.assertAlmostEqual(rating_engine.compute_ema([10.0, 10.0], 0.2), 10.0)
        self.assertAlmostEqual(rating_engine.compute_ema([0.0, 10.0], 0.2), 2.0)

    def test_base_rating_equals_50_plus_expected_margin(self) -> None:
        ratings = rating_engine.compute_team_ratings(_stats_payload())
        boston = ratings["teams"]["BOS"]
        new_york = ratings["teams"]["NYK"]

        self.assertAlmostEqual(boston["expected_margin"], 8.0, places=2)
        self.assertAlmostEqual(boston["base_rating"], 58.0, places=2)
        self.assertAlmostEqual(new_york["expected_margin"], -6.0, places=2)
        self.assertAlmostEqual(new_york["base_rating"], 44.0, places=2)
        self.assertEqual(boston["data_quality"], "ok")
        self.assertEqual(ratings["league"]["teams_count"], 2)

    def test_rating_is_clamped_and_shrunk(self) -> None:
        payload = _stats_payload()
        payload["teams"] = {
            "BOS": _team_payload("BOS", 30.0, pace=120.0),
            "NYK": _team_payload("NYK", -30.0, pace=120.0, games=4),
        }
        payload["game_log"] = []
        ratings = rating_engine.compute_team_ratings(payload)["teams"]
        self.assertEqual(ratings["BOS"]["base_rating"], config.RATING_MAX)
        # NYK 只有 4 場，Net Rating 會先乘上 4/10 再進入混合
        self.assertAlmostEqual(ratings["NYK"]["blended_net_rating"], -12.0, places=2)
        self.assertEqual(ratings["NYK"]["data_quality"], "partial")

    def test_no_games_team_defaults_to_league_average(self) -> None:
        payload = _stats_payload()
        payload["teams"] = {"BOS": _team_payload("BOS", 0.0, games=0)}
        payload["teams"]["BOS"]["season"]["net_rating"] = None
        payload["teams"]["BOS"]["recent"] = {"net_rating": None, "games": 0}
        payload["game_log"] = []
        team = rating_engine.compute_team_ratings(payload)["teams"]["BOS"]
        self.assertEqual(team["base_rating"], config.RATING_BASE)
        self.assertEqual(team["data_quality"], "no_games")


def _player_rows() -> list[dict]:
    return [
        {
            "player_id": 1,
            "name": "Nikola Jokić",
            "team_abbrev": "DEN",
            "gp": 15,
            "min": 36.0,
            "pts": 30.0,
            "reb": 12.0,
            "ast": 9.0,
            "stl": 1.5,
            "blk": 1.0,
            "tov": 3.0,
        },
        {
            "player_id": 2,
            "name": "Jamal Murray",
            "team_abbrev": "DEN",
            "gp": 15,
            "min": 33.0,
            "pts": 21.0,
            "reb": 4.0,
            "ast": 6.0,
            "stl": 1.0,
            "blk": 0.3,
            "tov": 2.0,
        },
        {
            "player_id": 3,
            "name": "Jalen Williams",
            "team_abbrev": "OKC",
            "gp": 15,
            "min": 32.0,
            "pts": 21.0,
            "reb": 5.0,
            "ast": 5.0,
            "stl": 1.5,
            "blk": 0.7,
            "tov": 2.0,
        },
        {
            "player_id": 4,
            "name": "Jaylin Williams",
            "team_abbrev": "OKC",
            "gp": 15,
            "min": 15.0,
            "pts": 6.0,
            "reb": 5.0,
            "ast": 1.5,
            "stl": 0.5,
            "blk": 0.5,
            "tov": 0.8,
        },
    ]


class InjuryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.impacts = rating_engine.compute_player_impacts(_player_rows())

    def test_impact_shares_sum_to_one(self) -> None:
        total = sum(player["impact_share"] for player in self.impacts["DEN"]["players"])
        self.assertAlmostEqual(total, 1.0, places=4)
        self.assertEqual(self.impacts["DEN"]["players"][0]["name"], "Nikola Jokić")

    def test_name_matching_handles_accents_and_same_surname(self) -> None:
        player, score = rating_engine.match_player("Nikola Jokic", "DEN", self.impacts)
        self.assertIsNotNone(player)
        self.assertEqual(player["name"], "Nikola Jokić")
        self.assertEqual(score, 1.0)

        player, _ = rating_engine.match_player("Jalen Williams", "OKC", self.impacts)
        self.assertIsNotNone(player)
        self.assertEqual(player["name"], "Jalen Williams")

        # 只有姓氏時無法區分同隊同姓球員，應視為未匹配
        player, score = rating_engine.match_player("J. Williams", "OKC", self.impacts)
        self.assertIsNone(player)
        self.assertLess(score, 0.95)

        player, score = rating_engine.match_player("Nobody Here", "DEN", self.impacts)
        self.assertIsNone(player)
        self.assertEqual(score, 0.0)

    def test_deduction_uses_share_severity_and_default_share(self) -> None:
        injuries = {
            "DEN": [
                {"player": "Nikola Jokic", "status": "out", "severity": 1.0},
                {"player": "Unknown Reserve", "status": "questionable", "severity": 0.45},
                {"player": "Nikola Jokic", "status": "questionable", "severity": 0.45},
            ]
        }
        summary = rating_engine.compute_injury_deductions(injuries, self.impacts)
        details = summary["DEN"]["injuries"]

        self.assertEqual(len(details), 2)  # 重複的 Jokic 只保留最嚴重
        jokic = next(item for item in details if item["player"] == "Nikola Jokic")
        expected = (
            jokic["impact_share"]
            * 1.0
            * config.INJURY_IMPACT_SCALE
            * config.INJURY_REPLACEMENT_FACTOR
        )
        self.assertAlmostEqual(jokic["deduction"], expected, places=2)
        unknown = next(item for item in details if item["player"] == "Unknown Reserve")
        self.assertFalse(unknown["matched"])
        self.assertAlmostEqual(unknown["impact_share"], config.DEFAULT_UNKNOWN_PLAYER_SHARE)

    def test_deduction_cap_and_zero_severity(self) -> None:
        injuries = {
            "DEN": [
                {"player": "Nikola Jokic", "status": "out", "severity": 1.0},
                {"player": "Jamal Murray", "status": "available", "severity": 0.0},
            ]
        }
        with mock.patch.object(config, "INJURY_MAX_TEAM_DEDUCTION", 1.0):
            summary = rating_engine.compute_injury_deductions(injuries, self.impacts)
        self.assertEqual(summary["DEN"]["deduction"], 1.0)
        self.assertTrue(summary["DEN"]["capped"])
        self.assertEqual(len(summary["DEN"]["injuries"]), 1)


class ReportParsingTests(unittest.TestCase):
    def _frame(self) -> FakeFrame:
        return FakeFrame(
            [
                {
                    "Game Date": "11/17/2026",
                    "Matchup": "BOS@NYK",
                    "Team": "Boston Celtics",
                    "Player Name": "Jayson Tatum",
                    "Current Status": "Out",
                    "Reason": "Ankle",
                },
                {
                    "Game Date": "11/17/2026",
                    "Matchup": "BOS@NYK",
                    "Team": "Boston Celtics",
                    "Player Name": "Jaylen Brown",
                    "Current Status": "Available",
                    "Reason": "",
                },
                {
                    "Game Date": "11/17/2026",
                    "Matchup": "LAL@DEN",
                    "Team": "Los Angeles Lakers",
                    "Player Name": "LeBron James",
                    "Current Status": "Out",
                    "Reason": "Foot",
                },
                {
                    "Game Date": "11/16/2026",
                    "Matchup": "NYK@BOS",
                    "Team": "New York Knicks",
                    "Player Name": "Jalen Brunson",
                    "Current Status": "Questionable",
                    "Reason": "Knee",
                },
                {
                    "Game Date": "11/17/2026",
                    "Matchup": "BOS@NYK",
                    "Team": "New York Knicks",
                    "Player Name": "Jalen Brunson",
                    "Current Status": "Questionable",
                    "Reason": "Knee",
                },
                {
                    "Game Date": "11/17/2026",
                    "Matchup": "BOS@NYK",
                    "Team": "New York Knicks",
                    "Player Name": "Jalen Brunson",
                    "Current Status": "Out",
                    "Reason": "Knee",
                },
            ]
        )

    def test_filters_by_team_date_and_available(self) -> None:
        games = [{"home_team": "NYK", "away_team": "BOS"}]
        injuries, warnings = fetch_injuries.parse_report(
            self._frame(), games, date(2026, 11, 17)
        )
        self.assertEqual(warnings, [])
        self.assertEqual({row["team"] for row in injuries}, {"BOS", "NYK"})
        self.assertEqual(len(injuries), 2)
        self.assertNotIn("Jaylen Brown", {row["player"] for row in injuries})
        brunson = next(row for row in injuries if row["player"] == "Jalen Brunson")
        self.assertEqual(brunson["status"], "out")  # 同球員取最嚴重狀態

    def test_keeps_all_rows_when_date_mismatch(self) -> None:
        games = [{"home_team": "NYK", "away_team": "BOS"}]
        frame = self._frame()
        frame["Game Date"] = "11/16/2026"
        injuries, warnings = fetch_injuries.parse_report(frame, games, date(2026, 11, 17))
        self.assertTrue(warnings)
        self.assertEqual(len(injuries), 2)


class PredictionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ratings = rating_engine.compute_team_ratings(_stats_payload())
        self.game = {
            "game_id": "0022600999",
            "game_date": "2026-11-17",
            "game_time_et": "19:30",
            "game_time_taipei": "2026-11-18T08:30+08:00",
            "home_team": "BOS",
            "away_team": "NYK",
            "home_rest_days": 1,
            "away_rest_days": 1,
            "home_b2b": False,
            "away_b2b": False,
            "is_neutral": False,
            "status": 1,
        }

    def _predict(self, **overrides) -> dict:
        game = dict(self.game)
        game.update(overrides)
        prediction = rating_engine.predict_game(game, self.ratings)
        self.assertIsNotNone(prediction)
        return prediction

    def test_margin_total_and_probability_invariants(self) -> None:
        prediction = self._predict()
        self.assertAlmostEqual(prediction["predicted_margin"], 16.6, places=2)
        self.assertEqual(prediction["predicted_winner"], "BOS")
        self.assertGreater(prediction["home_win_prob"], 0.5)
        self.assertLessEqual(prediction["home_win_prob"], config.WIN_PROB_MAX)
        self.assertAlmostEqual(
            prediction["home_win_prob"] + prediction["away_win_prob"], 1.0, places=3
        )
        self.assertGreaterEqual(prediction["predicted_total"], config.TOTAL_MIN)
        self.assertLessEqual(prediction["predicted_total"], config.TOTAL_MAX)
        total_gap = (
            prediction["predicted_home_score"]
            + prediction["predicted_away_score"]
            - prediction["predicted_total"]
        )
        self.assertLessEqual(abs(total_gap), 0.2)
        margin_gap = (
            prediction["predicted_home_score"]
            - prediction["predicted_away_score"]
            - prediction["predicted_margin"]
        )
        self.assertLessEqual(abs(margin_gap), 0.2)
        self.assertEqual(prediction["confidence"], "high")

    def test_home_court_b2b_and_rest_adjustments(self) -> None:
        baseline = self._predict()
        neutral = self._predict(is_neutral=True)
        self.assertAlmostEqual(
            neutral["predicted_margin"],
            baseline["predicted_margin"] - config.HOME_COURT_ADVANTAGE,
            places=2,
        )

        away_b2b = self._predict(away_b2b=True, away_rest_days=0)
        self.assertAlmostEqual(
            away_b2b["predicted_margin"],
            baseline["predicted_margin"] + config.B2B_PENALTY,
            places=2,
        )

        home_b2b = self._predict(home_b2b=True, home_rest_days=0)
        self.assertAlmostEqual(
            home_b2b["predicted_margin"],
            baseline["predicted_margin"] - config.B2B_PENALTY,
            places=2,
        )

        # 客隊多休 3 天（rest_days=4）→ 3 × 0.25 = 0.75 分補償
        rested = self._predict(away_rest_days=4)
        self.assertAlmostEqual(
            rested["predicted_margin"],
            baseline["predicted_margin"] - 3 * config.REST_ADVANTAGE_PER_DAY,
            places=2,
        )

    def test_injury_deduction_reduces_margin_and_total(self) -> None:
        baseline = self._predict()
        injuries = {"BOS": {"deduction": 5.0, "injuries": []}}
        hurt = rating_engine.predict_game(self.game, self.ratings, injuries)
        self.assertAlmostEqual(
            hurt["predicted_margin"], baseline["predicted_margin"] - 5.0, places=2
        )
        self.assertAlmostEqual(
            baseline["predicted_total"] - hurt["predicted_total"],
            config.INJURY_TOTAL_SCORING_FACTOR * 5.0,
            places=1,
        )
        self.assertEqual(hurt["home_injury_deduction"], 5.0)

    def test_missing_team_returns_none(self) -> None:
        self.assertIsNone(
            rating_engine.predict_game({"home_team": "BOS", "away_team": "SEA"}, self.ratings)
        )


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ratings = rating_engine.compute_team_ratings(_stats_payload())
        self.game = {
            "game_id": "0022600999",
            "game_date": "2026-11-17",
            "game_time_et": "19:30",
            "home_team": "BOS",
            "away_team": "NYK",
            "home_rest_days": 1,
            "away_rest_days": 1,
            "home_b2b": False,
            "away_b2b": False,
            "is_neutral": False,
        }

    def _payload(self) -> dict:
        return rating_engine.build_predictions(
            [self.game],
            self.ratings,
            {"available": False, "warnings": []},
            [],
            season="2026-27",
            target_date=date(2026, 11, 17),
        )

    def test_upsert_and_settle_preserve_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history_log.csv"
            payload = self._payload()
            rows = rating_engine.build_history_rows(payload)
            self.assertEqual(len(rows), 1)

            self.assertEqual(rating_engine.upsert_history(path, rows), (1, 1))
            self.assertEqual(rating_engine.upsert_history(path, rows), (1, 1))

            game_log = [
                {
                    "game_id": rows[0]["game_id"],
                    "team_abbrev": "BOS",
                    "game_date": "2026-11-17",
                    "points": 118.0,
                    "plus_minus": 8.0,
                },
                {
                    "game_id": rows[0]["game_id"],
                    "team_abbrev": "NYK",
                    "game_date": "2026-11-17",
                    "points": 110.0,
                    "plus_minus": -8.0,
                },
            ]
            self.assertEqual(rating_engine.settle_history(path, game_log), 1)
            record = rating_engine.read_history(path)[0]
            self.assertEqual(record["actual_home_score"], "118")
            self.assertEqual(record["actual_away_score"], "110")
            self.assertEqual(record["actual_winner"], "BOS")
            self.assertEqual(record["result_correct"], "true")
            self.assertEqual(rating_engine.settle_history(path, game_log), 0)

            # 重跑預測（分差改變）不得覆蓋已回填的賽果
            rows[0]["predicted_margin"] = 1.0
            rating_engine.upsert_history(path, rows)
            record = rating_engine.read_history(path)[0]
            self.assertEqual(record["predicted_margin"], "1")
            self.assertEqual(record["actual_home_score"], "118")
            self.assertEqual(record["result_correct"], "true")

    def test_predictions_payload_shape(self) -> None:
        payload = self._payload()
        self.assertEqual(payload["games_count"], 1)
        self.assertEqual(payload["game_date"], "2026-11-17")
        self.assertEqual(payload["season"], "2026-27")
        self.assertFalse(payload["injury_report"]["available"])
        self.assertEqual(len(payload["predictions"]), 1)
        json.dumps(payload, ensure_ascii=False)  # 必須可序列化


class ScheduleParsingTests(unittest.TestCase):
    def test_schedule_row_is_normalized_to_et(self) -> None:
        row = {
            "gameId": "0022500123",
            "gameDateTimeUTC": "2026-11-18T00:30:00Z",
            "gameStatus": 1,
            "gameStatusText": "7:30 pm ET",
            "homeTeam_teamTricode": "NYK",
            "awayTeam_teamTricode": "BOS",
            "homeTeam_score": "",
            "awayTeam_score": "",
            "arenaName": "Madison Square Garden",
            "isNeutral": False,
        }
        game = fetch_stats._normalize_schedule_row(row, config.SEASON_TYPE)
        self.assertIsNotNone(game)
        self.assertEqual(game["game_date"], "2026-11-17")
        self.assertEqual(game["game_time_et"], "19:30")
        self.assertEqual(game["home_team"], "NYK")
        self.assertEqual(game["away_team"], "BOS")
        self.assertIsNone(game["home_score"])
        self.assertIsNone(game["away_score"])

    def test_preseason_and_allstar_rows_are_filtered(self) -> None:
        row = {
            "gameId": "0012500001",
            "gameDateTimeUTC": "2026-10-06T23:00:00Z",
            "homeTeam_teamTricode": "BOS",
            "awayTeam_teamTricode": "NYK",
        }
        self.assertIsNone(fetch_stats._normalize_schedule_row(row, config.SEASON_TYPE))

    def test_rest_days_and_b2b(self) -> None:
        schedule = [
            {"game_date": "2026-11-14", "home_team": "BOS", "away_team": "NYK"},
            {"game_date": "2026-11-15", "home_team": "NYK", "away_team": "BOS"},
        ]
        games = [
            {
                "game_id": "0022500999",
                "game_date": "2026-11-17",
                "home_team": "BOS",
                "away_team": "NYK",
            }
        ]
        fetch_stats.annotate_rest_days(games, schedule)
        self.assertEqual(games[0]["home_rest_days"], 1)
        self.assertEqual(games[0]["away_rest_days"], 1)
        self.assertFalse(games[0]["home_b2b"])
        self.assertFalse(games[0]["away_b2b"])

    def test_scoreboard_fallback_maps_gamecode_and_score_field(self) -> None:
        headers = [
            {
                "gameId": "0022500123",
                "gameCode": "20251117/BOSNYK",
                "gameStatus": 3,
                "gameStatusText": "Final",
                "gameTimeUTC": "2026-11-18T00:30:00Z",
            }
        ]
        # 刻意把主隊排在前面，驗證會用 gameCode 對應而非列順序
        lines = [
            {"gameId": "0022500123", "teamTricode": "NYK", "score": 110},
            {"gameId": "0022500123", "teamTricode": "BOS", "score": 118},
        ]
        games = fetch_stats._build_scoreboard_games(
            headers, lines, date(2026, 11, 17), config.SEASON_TYPE
        )
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["home_team"], "NYK")
        self.assertEqual(games[0]["away_team"], "BOS")
        self.assertEqual(games[0]["home_score"], 110.0)
        self.assertEqual(games[0]["away_score"], 118.0)
        self.assertEqual(games[0]["game_date"], "2026-11-17")


class InjuryProbeTests(unittest.TestCase):
    def test_probe_uses_naive_et_timestamps(self) -> None:
        received: dict = {}

        def fake_gen_url(timestamp):
            received["gen"] = timestamp
            return "https://example.com/Injury-Report.pdf"

        def fake_get_reportdata(timestamp, return_df=False):
            received["parse"] = timestamp
            return FakeFrame([{"Team": "Boston Celtics"}])

        fake_injury = types.SimpleNamespace(
            gen_url=fake_gen_url, get_reportdata=fake_get_reportdata
        )
        fake_module = types.ModuleType("nbainjuries")
        fake_module.injury = fake_injury

        now_et = datetime(2026, 11, 17, 17, 42, tzinfo=config.TZ_ET)
        with mock.patch.dict(sys.modules, {"nbainjuries": fake_module}), mock.patch.object(
            fetch_injuries, "_url_available", return_value=True
        ):
            report = fetch_injuries._find_latest_report(now_et, max_probes=3)

        # nbainjuries 會與 naive datetime 比較，因此傳入的必須是 naive ET 時間
        self.assertIsNone(received["gen"].tzinfo)
        self.assertIsNone(received["parse"].tzinfo)
        self.assertEqual(received["gen"], datetime(2026, 11, 17, 17, 30))
        self.assertEqual(report["timestamp"].utcoffset(), now_et.utcoffset())


class MainFlowTests(unittest.TestCase):
    def test_both_mode_writes_ratings_predictions_and_history(self) -> None:
        game = {
            "game_id": "0022600999",
            "game_date": "2026-11-17",
            "game_time_et": "19:30",
            "game_time_taipei": "2026-11-18T08:30+08:00",
            "home_team": "BOS",
            "away_team": "NYK",
            "home_rest_days": 1,
            "away_rest_days": 1,
            "home_b2b": False,
            "away_b2b": False,
            "is_neutral": False,
            "status": 1,
        }
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ratings_path = directory / "team_ratings.json"
            predictions_path = directory / "daily_predictions.json"
            history_path = directory / "history_log.csv"

            with mock.patch.object(config, "TEAM_RATINGS_PATH", ratings_path), mock.patch.object(
                config, "DAILY_PREDICTIONS_PATH", predictions_path
            ), mock.patch.object(config, "HISTORY_LOG_PATH", history_path), mock.patch.object(
                fetch_stats, "fetch_all_team_stats", return_value=_stats_payload()
            ), mock.patch.object(
                fetch_stats,
                "fetch_games_for_date",
                return_value={"games": [game], "schedule": [], "warnings": []},
            ), mock.patch.object(
                fetch_stats, "fetch_team_game_log", return_value=[]
            ), mock.patch.object(
                fetch_injuries,
                "fetch_injuries",
                return_value={
                    "source": "test",
                    "available": False,
                    "warnings": [],
                    "by_team": {},
                },
            ):
                exit_code = main_module.main(
                    [
                        "--mode",
                        "both",
                        "--date",
                        "2026-11-17",
                        "--season",
                        "2026-27",
                        "--log-level",
                        "ERROR",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(ratings_path.exists())
            self.assertTrue(predictions_path.exists())
            self.assertTrue(history_path.exists())

            ratings = json.loads(ratings_path.read_text(encoding="utf-8"))
            self.assertEqual(ratings["league"]["teams_count"], 2)
            predictions = json.loads(predictions_path.read_text(encoding="utf-8"))
            self.assertEqual(predictions["games_count"], 1)
            self.assertEqual(predictions["predictions"][0]["home_team"], "BOS")
            history = rating_engine.read_history(history_path)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["game_id"], "0022600999")

    def test_predict_applies_injuries_without_player_stats(self) -> None:
        game = {
            "game_id": "0022600998",
            "game_date": "2026-11-17",
            "game_time_et": "19:30",
            "game_time_taipei": "2026-11-18T08:30+08:00",
            "home_team": "BOS",
            "away_team": "NYK",
            "home_rest_days": 1,
            "away_rest_days": 1,
            "home_b2b": False,
            "away_b2b": False,
            "is_neutral": False,
            "status": 1,
        }
        injury_entry = {
            "team": "BOS",
            "team_name": "Boston Celtics",
            "player": "Jayson Tatum",
            "status": "out",
            "status_raw": "Out",
            "severity": 1.0,
            "reason": "Ankle",
            "game_date": "2026-11-17",
            "matchup": "",
        }
        injuries = {
            "source": "test",
            "available": True,
            "report_timestamp_et": "2026-11-17T17:30",
            "report_url": "https://example.com/report.pdf",
            "warnings": [],
            "injuries": [injury_entry],
            "by_team": {"BOS": [injury_entry]},
        }
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            predictions_path = directory / "daily_predictions.json"
            with mock.patch.object(
                config, "TEAM_RATINGS_PATH", directory / "team_ratings.json"
            ), mock.patch.object(
                config, "DAILY_PREDICTIONS_PATH", predictions_path
            ), mock.patch.object(
                config, "HISTORY_LOG_PATH", directory / "history_log.csv"
            ), mock.patch.object(
                fetch_stats, "fetch_all_team_stats", return_value=_stats_payload()
            ), mock.patch.object(
                fetch_stats,
                "fetch_games_for_date",
                return_value={"games": [game], "schedule": [], "warnings": []},
            ), mock.patch.object(
                fetch_stats, "fetch_team_game_log", return_value=[]
            ), mock.patch.object(
                fetch_stats,
                "fetch_player_season_stats",
                side_effect=fetch_stats.StatsFetchError("pandas 不可用"),
            ), mock.patch.object(
                fetch_injuries, "fetch_injuries", return_value=injuries
            ):
                exit_code = main_module.main(
                    [
                        "--mode",
                        "predict",
                        "--date",
                        "2026-11-17",
                        "--season",
                        "2026-27",
                        "--log-level",
                        "ERROR",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(predictions_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["injury_report"]["available"])
            prediction = payload["predictions"][0]
            self.assertGreater(prediction["home_injury_deduction"], 0)
            self.assertEqual(len(prediction["home_injuries"]), 1)

    def test_post_game_rerun_keeps_existing_pre_game_predictions(self) -> None:
        game = {
            "game_id": "0022600997",
            "game_date": "2026-11-17",
            "game_time_et": "19:30",
            "game_time_utc": "2026-11-18T00:30:00+00:00",
            "game_time_taipei": "2026-11-18T08:30+08:00",
            "home_team": "BOS",
            "away_team": "NYK",
            "home_rest_days": 1,
            "away_rest_days": 1,
            "home_b2b": False,
            "away_b2b": False,
            "is_neutral": False,
            "status": 1,
        }
        pre_game = {
            "schema_version": 1,
            "season": "2026-27",
            "game_date": "2026-11-17",
            "generated_at": "2026-11-17T12:00:00+00:00",
            "games_count": 1,
            "warnings": [],
            "predictions": [
                {
                    "game_id": "0022600997",
                    "game_date": "2026-11-17",
                    "game_time_et": "19:30",
                    "home_team": "BOS",
                    "away_team": "NYK",
                    "predicted_winner": "NYK",
                    "post_game": False,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            predictions_path = directory / "daily_predictions.json"
            history_path = directory / "history_log.csv"
            predictions_path.write_text(
                json.dumps(pre_game, ensure_ascii=False), encoding="utf-8"
            )
            with mock.patch.object(
                config, "TEAM_RATINGS_PATH", directory / "team_ratings.json"
            ), mock.patch.object(
                config, "DAILY_PREDICTIONS_PATH", predictions_path
            ), mock.patch.object(
                config, "HISTORY_LOG_PATH", history_path
            ), mock.patch.object(
                fetch_stats, "fetch_all_team_stats", return_value=_stats_payload()
            ), mock.patch.object(
                fetch_stats,
                "fetch_games_for_date",
                return_value={"games": [game], "schedule": [], "warnings": []},
            ), mock.patch.object(
                fetch_stats, "fetch_team_game_log", return_value=[]
            ), mock.patch.object(
                config,
                "now_utc",
                return_value=datetime(2026, 11, 18, 4, 0, tzinfo=config.TZ_ET),
            ), mock.patch.object(
                fetch_injuries,
                "fetch_injuries",
                return_value={
                    "source": "test",
                    "available": False,
                    "warnings": [],
                    "by_team": {},
                },
            ):
                exit_code = main_module.main(
                    [
                        "--mode",
                        "predict",
                        "--date",
                        "2026-11-17",
                        "--season",
                        "2026-27",
                        "--log-level",
                        "ERROR",
                    ]
                )

            self.assertEqual(exit_code, 0)
            kept = json.loads(predictions_path.read_text(encoding="utf-8"))
            self.assertEqual(kept["predictions"][0]["predicted_winner"], "NYK")
            self.assertTrue(
                any("保留賽前預測" in message for message in kept["warnings"])
            )
            history = rating_engine.read_history(history_path)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["post_game"], "false")

    def test_mixed_run_keeps_pre_game_rows_for_started_games(self) -> None:
        early = {
            "game_id": "0022600996",
            "game_date": "2026-11-17",
            "game_time_et": "19:30",
            "game_time_utc": "2026-11-18T00:30:00+00:00",
            "game_time_taipei": "2026-11-18T08:30+08:00",
            "home_team": "BOS",
            "away_team": "NYK",
            "home_rest_days": 1,
            "away_rest_days": 1,
            "home_b2b": False,
            "away_b2b": False,
            "is_neutral": False,
            "status": 1,
        }
        late = dict(
            early,
            game_id="0022600995",
            game_time_et="22:00",
            game_time_utc="2026-11-18T03:00:00+00:00",
            game_time_taipei="2026-11-18T11:00+08:00",
        )
        pre_game = {
            "schema_version": 1,
            "season": "2026-27",
            "game_date": "2026-11-17",
            "generated_at": "2026-11-17T11:00:00+00:00",
            "games_count": 2,
            "warnings": [],
            "predictions": [
                {
                    "game_id": "0022600996",
                    "game_date": "2026-11-17",
                    "game_time_et": "19:30",
                    "home_team": "BOS",
                    "away_team": "NYK",
                    "predicted_winner": "NYK",
                    "generated_at": "2026-11-17T11:00:00+00:00",
                    "post_game": False,
                },
                {
                    "game_id": "0022600995",
                    "game_date": "2026-11-17",
                    "game_time_et": "22:00",
                    "home_team": "BOS",
                    "away_team": "NYK",
                    "predicted_winner": "LAL",
                    "generated_at": "2026-11-17T11:00:00+00:00",
                    "post_game": False,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            predictions_path = directory / "daily_predictions.json"
            history_path = directory / "history_log.csv"
            predictions_path.write_text(
                json.dumps(pre_game, ensure_ascii=False), encoding="utf-8"
            )
            with mock.patch.object(
                config, "TEAM_RATINGS_PATH", directory / "team_ratings.json"
            ), mock.patch.object(
                config, "DAILY_PREDICTIONS_PATH", predictions_path
            ), mock.patch.object(
                config, "HISTORY_LOG_PATH", history_path
            ), mock.patch.object(
                config,
                "now_utc",
                return_value=datetime(2026, 11, 17, 21, 0, tzinfo=config.TZ_ET),
            ), mock.patch.object(
                fetch_stats, "fetch_all_team_stats", return_value=_stats_payload()
            ), mock.patch.object(
                fetch_stats,
                "fetch_games_for_date",
                return_value={"games": [early, late], "schedule": [], "warnings": []},
            ), mock.patch.object(
                fetch_stats, "fetch_team_game_log", return_value=[]
            ), mock.patch.object(
                fetch_injuries,
                "fetch_injuries",
                return_value={
                    "source": "test",
                    "available": False,
                    "warnings": [],
                    "by_team": {},
                },
            ):
                exit_code = main_module.main(
                    [
                        "--mode",
                        "predict",
                        "--date",
                        "2026-11-17",
                        "--season",
                        "2026-27",
                        "--log-level",
                        "ERROR",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(predictions_path.read_text(encoding="utf-8"))
            started, upcoming = payload["predictions"]
            # 已開打的場次保留賽前預測（含原本的產生時間）
            self.assertEqual(started["game_id"], "0022600996")
            self.assertEqual(started["predicted_winner"], "NYK")
            self.assertEqual(started["generated_at"], "2026-11-17T11:00:00+00:00")
            self.assertFalse(started["post_game"])
            # 還沒開打的場次採用本次結果
            self.assertEqual(upcoming["game_id"], "0022600995")
            self.assertNotEqual(upcoming["predicted_winner"], "LAL")
            self.assertEqual(upcoming["generated_at"], "2026-11-18T02:00:00+00:00")
            self.assertTrue(
                any("保留先前的賽前預測" in message for message in payload["warnings"])
            )

            history = {
                row["game_id"]: row for row in rating_engine.read_history(history_path)
            }
            self.assertEqual(len(history), 2)
            self.assertEqual(history["0022600996"]["post_game"], "false")
            self.assertEqual(
                history["0022600996"]["predicted_at"], "2026-11-17T11:00:00+00:00"
            )
            self.assertEqual(history["0022600995"]["post_game"], "false")


def _espn_team(abbreviation: str) -> dict:
    return {
        "id": "999",
        "abbreviation": abbreviation,
        "displayName": config.team_display_name(abbreviation),
    }


def _espn_event(
    game_id: str,
    date_utc: str,
    away: str,
    home: str,
    away_score: float | None = None,
    home_score: float | None = None,
    state: str = "pre",
    season_type_name: str | None = None,
):
    competitors = [
        {
            "homeAway": "home",
            "order": 0,
            "team": _espn_team(home),
            "score": {"value": home_score} if home_score is not None else None,
        },
        {
            "homeAway": "away",
            "order": 1,
            "team": _espn_team(away),
            "score": {"value": away_score} if away_score is not None else None,
        },
    ]
    event = {
        "id": game_id,
        "date": date_utc,
        "season": {"year": 2027, "type": 2},
        "competitions": [
            {
                "date": date_utc,
                "neutralSite": False,
                "venue": {"fullName": "TD Garden"},
                "status": {
                    "type": {
                        "state": state,
                        "completed": state == "post",
                        "detail": "Final" if state == "post" else "7:30 PM ET",
                    }
                },
                "competitors": competitors,
            }
        ],
    }
    if season_type_name:
        event["seasonType"] = {"name": season_type_name}
    return event


def _espn_games_by_team() -> dict:
    first = fetch_espn.normalize_event(
        _espn_event("401", "2026-11-02T00:30Z", "NYK", "BOS", 100, 110, state="post"),
        config.SEASON_TYPE,
    )
    second = fetch_espn.normalize_event(
        _espn_event("402", "2026-11-04T00:30Z", "BOS", "NYK", 105, 95, state="post"),
        config.SEASON_TYPE,
    )
    return {"BOS": [first, second], "NYK": [first, second]}


class EspnFallbackTests(unittest.TestCase):
    def test_abbreviation_aliases_resolve_to_official_codes(self) -> None:
        pairs = {
            "NY": "NYK",
            "GS": "GSW",
            "NO": "NOP",
            "SA": "SAS",
            "WSH": "WAS",
            "UTAH": "UTA",
            "BOS": "BOS",
        }
        for espn_abbrev, official in pairs.items():
            self.assertEqual(
                fetch_espn._resolve_team({"abbreviation": espn_abbrev}), official
            )
        self.assertEqual(
            fetch_espn._resolve_team({"displayName": "Golden State Warriors"}), "GSW"
        )
        self.assertIsNone(fetch_espn._resolve_team({"abbreviation": "XYZ"}))

    def test_season_year_uses_end_year(self) -> None:
        self.assertEqual(fetch_espn.espn_season_year("2026-27"), 2027)

    def test_normalize_event_converts_to_et_and_drops_unknown_scores(self) -> None:
        event = _espn_event("401", "2026-11-18T00:30Z", "NYK", "BOS", 0, 0, state="pre")
        game = fetch_espn.normalize_event(event, config.SEASON_TYPE)
        self.assertIsNotNone(game)
        self.assertEqual(game["home_team"], "BOS")
        self.assertEqual(game["away_team"], "NYK")
        self.assertEqual(game["game_date"], "2026-11-17")
        self.assertEqual(game["game_time_et"], "19:30")
        self.assertEqual(game["status"], 1)
        self.assertIsNone(game["home_score"])
        self.assertIsNone(game["away_score"])

    def test_normalize_event_keeps_final_scores_and_filters_preseason(self) -> None:
        event = _espn_event(
            "401",
            "2026-11-18T00:30Z",
            "NYK",
            "BOS",
            100,
            110,
            state="post",
            season_type_name="Regular Season",
        )
        game = fetch_espn.normalize_event(event, config.SEASON_TYPE)
        self.assertEqual(game["status"], 3)
        self.assertEqual(game["home_score"], 110.0)
        self.assertEqual(game["away_score"], 100.0)

        preseason = _espn_event(
            "900",
            "2026-10-06T23:00Z",
            "NYK",
            "BOS",
            90,
            95,
            state="post",
            season_type_name="Preseason",
        )
        self.assertIsNone(fetch_espn.normalize_event(preseason, config.SEASON_TYPE))

    def test_build_team_stats_computes_proxy_ratings(self) -> None:
        teams, league = fetch_espn.build_team_stats(_espn_games_by_team())
        self.assertEqual(teams["BOS"]["games_played"], 2)
        self.assertEqual(teams["BOS"]["wins"], 2)
        self.assertEqual(teams["BOS"]["losses"], 0)
        self.assertEqual(teams["NYK"]["wins"], 0)
        self.assertAlmostEqual(teams["BOS"]["season"]["off_rating"], 104.878, places=3)
        self.assertAlmostEqual(teams["BOS"]["season"]["def_rating"], 95.122, places=3)
        self.assertGreater(teams["BOS"]["season"]["net_rating"], 0)
        self.assertLess(teams["NYK"]["season"]["net_rating"], 0)
        self.assertEqual(teams["BOS"]["recent"]["games"], 2)
        # pace 代理值 = 聯盟平均每隊每場得分
        self.assertAlmostEqual(league["avg_pace"], 102.5, places=3)
        self.assertAlmostEqual(league["avg_net_rating"], 0.0, places=3)

    def test_build_game_log_deduplicates_shared_games(self) -> None:
        log = fetch_espn.build_game_log(_espn_games_by_team())
        self.assertEqual(len(log), 4)  # 2 場 × 2 隊
        self.assertEqual(
            sorted(row["team_abbrev"] for row in log), ["BOS", "BOS", "NYK", "NYK"]
        )
        bos_rows = [row for row in log if row["team_abbrev"] == "BOS"]
        self.assertEqual(bos_rows[0]["plus_minus"], 10.0)
        self.assertEqual(bos_rows[0]["wl"], "W")
        self.assertEqual(bos_rows[0]["team_id"], config.TEAM_BY_ABBREV["BOS"]["id"])

    def test_annotate_rest_days_marks_back_to_back(self) -> None:
        games_by_team = _espn_games_by_team()
        rested = [
            {
                "game_id": "403",
                "game_date": "2026-11-06",
                "home_team": "BOS",
                "away_team": "NYK",
            }
        ]
        fetch_espn.annotate_rest_days(rested, games_by_team)
        # 前兩場 ET 日期為 11-01 與 11-03；11-06 開打代表休 2 天
        self.assertEqual(rested[0]["home_rest_days"], 2)
        self.assertFalse(rested[0]["home_b2b"])

        back_to_back = [
            {
                "game_id": "404",
                "game_date": "2026-11-04",
                "home_team": "BOS",
                "away_team": "NYK",
            }
        ]
        fetch_espn.annotate_rest_days(back_to_back, games_by_team)
        self.assertEqual(back_to_back[0]["away_rest_days"], 0)
        self.assertTrue(back_to_back[0]["away_b2b"])

    def test_fetch_all_team_stats_uses_espn_when_nba_api_unavailable(self) -> None:
        payload = {
            "source": "espn",
            "season": "2026-27",
            "season_type": config.SEASON_TYPE,
            "teams": {},
            "league": {},
            "game_log": [],
            "warnings": [],
        }
        with mock.patch.object(
            fetch_stats, "nba_api_available", return_value=False
        ), mock.patch.object(
            fetch_espn, "fetch_all_team_stats", return_value=payload
        ) as espn_mock:
            result = fetch_stats.fetch_all_team_stats("2026-27")
        self.assertIs(result, payload)
        espn_mock.assert_called_once_with("2026-27", config.SEASON_TYPE)

    def test_fetch_all_team_stats_falls_back_when_nba_api_raises(self) -> None:
        payload = {
            "source": "espn",
            "season": "2026-27",
            "season_type": config.SEASON_TYPE,
            "teams": {},
            "league": {},
            "game_log": [],
            "warnings": [],
        }
        with mock.patch.object(
            fetch_stats, "nba_api_available", return_value=True
        ), mock.patch.object(
            fetch_stats,
            "_fetch_all_team_stats_nba",
            side_effect=fetch_stats.StatsFetchError("boom"),
        ), mock.patch.object(
            fetch_espn, "fetch_all_team_stats", return_value=payload
        ):
            result = fetch_stats.fetch_all_team_stats("2026-27")
        self.assertEqual(result["source"], "espn")
        self.assertIn("nba_api 球季數據失敗", result["warnings"][0])

    def test_fetch_games_for_date_uses_espn_when_nba_unavailable(self) -> None:
        game = {
            "game_id": "401",
            "game_date": "2026-11-17",
            "home_team": "BOS",
            "away_team": "NYK",
        }
        with mock.patch.object(
            fetch_stats, "nba_api_available", return_value=False
        ), mock.patch.object(
            fetch_espn, "fetch_games_for_date", return_value=[game]
        ) as espn_mock:
            payload = fetch_stats.fetch_games_for_date("2026-27", date(2026, 11, 17))
        self.assertEqual(payload["games"], [game])
        self.assertTrue(any("ESPN" in warning for warning in payload["warnings"]))
        espn_mock.assert_called_once_with(
            "2026-27", date(2026, 11, 17), config.SEASON_TYPE
        )

    def test_espn_payload_produces_sane_ratings_and_predictions(self) -> None:
        games_by_team = _espn_games_by_team()
        teams, league = fetch_espn.build_team_stats(games_by_team)
        stats_payload = {
            "source": "espn",
            "season": "2026-27",
            "season_type": config.SEASON_TYPE,
            "teams": teams,
            "league": league,
            "game_log": fetch_espn.build_game_log(games_by_team),
            "warnings": [],
        }
        ratings = rating_engine.compute_team_ratings(stats_payload)
        self.assertEqual(ratings["data_source"], "espn")
        self.assertGreater(ratings["teams"]["BOS"]["base_rating"], 50.0)
        self.assertLess(ratings["teams"]["NYK"]["base_rating"], 50.0)

        prediction = rating_engine.predict_game(
            {
                "game_id": "403",
                "game_date": "2026-11-17",
                "game_time_et": "19:30",
                "home_team": "BOS",
                "away_team": "NYK",
                "is_neutral": False,
                "status": 1,
            },
            ratings,
        )
        self.assertIsNotNone(prediction)
        self.assertEqual(prediction["predicted_winner"], "BOS")
        self.assertGreater(prediction["predicted_total"], config.TOTAL_MIN)
        self.assertLess(prediction["predicted_total"], config.TOTAL_MAX)


class EspnPlayerStatsTests(unittest.TestCase):
    """ESPN byathlete 端點（stats.nba.com 不可用時的球員數據替代來源）。"""

    def _payload(self) -> dict:
        return {
            "categories": [
                {"name": "general", "names": ["gamesPlayed", "avgMinutes"]},
                {"name": "offensive", "names": ["avgPoints", "avgAssists", "avgTurnovers"]},
                {"name": "defensive", "names": ["avgSteals", "avgBlocks"]},
            ],
            "pagination": {"pages": 1},
            "athletes": [
                {
                    "athlete": {
                        "id": "111",
                        "displayName": "Keldon Johnson",
                        "teamShortName": "SA",
                    },
                    "categories": [
                        {"name": "general", "values": [15, 28.5]},
                        {"name": "offensive", "values": [17.2, 3.1, 1.4]},
                        {"name": "defensive", "values": [0.8, 0.3]},
                    ],
                },
                {
                    "athlete": {"id": "222", "displayName": "No Team Guy"},
                    "categories": [{"name": "general", "values": [3, 12.0]}],
                },
            ],
        }

    def test_byathlete_rows_are_normalized(self) -> None:
        with mock.patch.object(
            fetch_espn, "_get_json", return_value=self._payload()
        ) as get_json:
            rows = fetch_espn.fetch_player_season_stats("2026-27")

        self.assertEqual(len(rows), 1)  # 沒有球隊的球員會被略過
        row = rows[0]
        self.assertEqual(row["player_id"], 111)
        self.assertEqual(row["name"], "Keldon Johnson")
        self.assertEqual(row["team_abbrev"], "SAS")  # ESPN 的 SA -> 官方 SAS
        self.assertEqual(row["gp"], 15)
        self.assertAlmostEqual(row["min"], 28.5)
        self.assertAlmostEqual(row["pts"], 17.2)
        self.assertAlmostEqual(row["tov"], 1.4)
        self.assertAlmostEqual(row["blk"], 0.3)
        # 參數需帶 ESPN 的球季結束年份與例行賽代碼
        self.assertIn("season=2027", get_json.call_args[0][0])
        self.assertIn("seasontype=2", get_json.call_args[0][0])

    def test_empty_payload_raises(self) -> None:
        with mock.patch.object(
            fetch_espn, "_get_json", return_value={"categories": [], "athletes": []}
        ):
            with self.assertRaises(fetch_espn.EspnFetchError):
                fetch_espn.fetch_player_season_stats("2026-27")


class PlayerStatsFallbackTests(unittest.TestCase):
    """fetch_stats.fetch_player_season_stats 的 ESPN 備援流程。"""

    ROWS = [
        {
            "player_id": 1,
            "name": "Jayson Tatum",
            "team_abbrev": "BOS",
            "gp": 12,
            "min": 34.0,
            "pts": 27.0,
            "reb": 8.0,
            "ast": 5.0,
            "stl": 1.0,
            "blk": 0.6,
            "tov": 2.5,
        }
    ]

    def test_nba_unavailable_uses_espn(self) -> None:
        warnings: list[str] = []
        with mock.patch.object(
            fetch_stats, "nba_api_available", return_value=False
        ), mock.patch.object(
            fetch_stats.fetch_espn, "fetch_player_season_stats", return_value=self.ROWS
        ):
            rows = fetch_stats.fetch_player_season_stats("2026-27", warnings=warnings)

        self.assertEqual(rows, self.ROWS)
        self.assertTrue(any("ESPN" in message for message in warnings))

    def test_nba_failure_falls_back_to_espn(self) -> None:
        with mock.patch.object(
            fetch_stats, "nba_api_available", return_value=True
        ), mock.patch.object(
            fetch_stats,
            "_fetch_player_season_stats_nba",
            side_effect=fetch_stats.StatsFetchError("boom"),
        ), mock.patch.object(
            fetch_stats.fetch_espn, "fetch_player_season_stats", return_value=self.ROWS
        ):
            self.assertEqual(fetch_stats.fetch_player_season_stats("2026-27"), self.ROWS)

    def test_disabled_flag_raises(self) -> None:
        with mock.patch.object(
            fetch_stats, "nba_api_available", return_value=False
        ), mock.patch.object(config, "ESPN_PLAYER_STATS_FALLBACK_ENABLED", False):
            with self.assertRaises(fetch_stats.StatsFetchError):
                fetch_stats.fetch_player_season_stats("2026-27")


class EspnInjuryTests(unittest.TestCase):
    """ESPN 傷兵名單：官方報告不可用時的替代來源。"""

    GAMES = [{"home_team": "MEM", "away_team": "GSW"}]
    ESPN_ROWS = [
        {
            "team": "GSW",
            "player": "Jimmy Butler III",
            "status": "Out",
            "reason": "Right Knee Surgery",
            "updated_at": "2026-11-18T17:12:00+00:00",
        },
        {
            "team": "MEM",
            "player": "Zach Edey",
            "status": "Day-To-Day",
            "reason": "Ankle",
            "updated_at": None,
        },
        {
            "team": "BOS",
            "player": "Jayson Tatum",
            "status": "Out",
            "reason": "Achilles",
            "updated_at": None,
        },
        {
            "team": "MEM",
            "player": "Ja Morant",
            "status": "Available",
            "reason": "",
            "updated_at": None,
        },
    ]

    def test_parse_espn_injuries_filters_and_normalizes(self) -> None:
        injuries, warnings = fetch_injuries.parse_espn_injuries(
            self.ESPN_ROWS, self.GAMES, date(2026, 11, 19)
        )
        self.assertEqual(warnings, [])
        # 非當日出賽球隊（BOS）與 Available 都應被剔除
        self.assertEqual({row["team"] for row in injuries}, {"GSW", "MEM"})
        self.assertEqual(len(injuries), 2)
        self.assertTrue(all(row["source"] == "espn" for row in injuries))
        edey = next(row for row in injuries if row["player"] == "Zach Edey")
        self.assertEqual(edey["status"], "day-to-day")
        self.assertAlmostEqual(
            edey["severity"], config.INJURY_STATUS_WEIGHTS["day-to-day"]
        )
        self.assertEqual(edey["game_date"], "2026-11-19")

    def test_parse_espn_injuries_respects_team_filter(self) -> None:
        injuries, _ = fetch_injuries.parse_espn_injuries(
            self.ESPN_ROWS, self.GAMES, date(2026, 11, 19), teams_filter={"MEM"}
        )
        self.assertEqual({row["team"] for row in injuries}, {"MEM"})

    def test_uses_espn_when_official_report_unavailable(self) -> None:
        with mock.patch.object(
            fetch_injuries,
            "latest_report",
            side_effect=fetch_injuries.InjuryFetchError("找不到傷病報告"),
        ), mock.patch.object(
            fetch_injuries.fetch_espn, "fetch_injuries", return_value=self.ESPN_ROWS
        ), mock.patch.object(
            fetch_injuries, "_save_snapshot"
        ):
            payload = fetch_injuries.fetch_injuries(self.GAMES, date(2026, 11, 19))

        self.assertTrue(payload["available"])
        self.assertEqual(payload["source"], "espn")
        self.assertEqual(len(payload["injuries"]), 2)
        self.assertIn("GSW", payload["by_team"])
        self.assertTrue(any("ESPN" in message for message in payload["warnings"]))
        # 官方報告不可用時，ESPN 是唯一來源，嚴重度仍需正確換算
        self.assertAlmostEqual(payload["injuries"][0]["severity"], 1.0)

    def test_espn_failure_keeps_empty_payload(self) -> None:
        with mock.patch.object(
            fetch_injuries,
            "latest_report",
            side_effect=fetch_injuries.InjuryFetchError("找不到傷病報告"),
        ), mock.patch.object(
            fetch_injuries.fetch_espn,
            "fetch_injuries",
            side_effect=fetch_espn.EspnFetchError("500"),
        ):
            payload = fetch_injuries.fetch_injuries(self.GAMES, date(2026, 11, 19))

        self.assertFalse(payload["available"])
        self.assertEqual(payload["injuries"], [])
        self.assertTrue(
            any("ESPN 傷兵名單無法取得" in msg for msg in payload["warnings"])
        )


class OfficialReportPriorityTests(unittest.TestCase):
    """官方報告可用時，預設不呼叫 ESPN；開啟補漏才併用。"""

    GAMES = [{"home_team": "MEM", "away_team": "GSW"}]

    def _report(self) -> dict:
        return {
            "timestamp": datetime(2026, 11, 19, 17, 30, tzinfo=config.TZ_ET),
            "url": "https://example.com/Injury-Report.pdf",
            "dataframe": FakeFrame(
                [
                    {
                        "Team": "Golden State Warriors",
                        "Player Name": "Jimmy Butler III",
                        "Current Status": "Out",
                        "Reason": "Knee",
                        "Game Date": "11/19/2026",
                    }
                ]
            ),
        }

    def test_official_report_wins_without_calling_espn(self) -> None:
        with mock.patch.object(
            fetch_injuries, "latest_report", return_value=self._report()
        ), mock.patch.object(fetch_injuries, "fetch_espn") as espn_module, mock.patch.object(
            fetch_injuries, "_save_snapshot"
        ):
            payload = fetch_injuries.fetch_injuries(self.GAMES, date(2026, 11, 19))

        espn_module.fetch_injuries.assert_not_called()
        self.assertEqual(payload["source"], "nbainjuries")
        self.assertEqual(len(payload["injuries"]), 1)
        self.assertEqual(payload["injuries"][0]["source"], "nbainjuries")

    def test_fill_missing_teams_merges_espn_when_enabled(self) -> None:
        espn_rows = [
            {
                "team": "MEM",
                "player": "Zach Edey",
                "status": "Out",
                "reason": "Ankle",
                "updated_at": None,
            },
            {
                "team": "GSW",
                "player": "Jimmy Butler III",
                "status": "Out",
                "reason": "Knee",
                "updated_at": None,
            },
            {
                "team": "BOS",
                "player": "Jayson Tatum",
                "status": "Out",
                "reason": "Achilles",
                "updated_at": None,
            },
        ]
        with mock.patch.object(
            fetch_injuries, "latest_report", return_value=self._report()
        ), mock.patch.object(
            config, "ESPN_INJURY_FILL_MISSING_TEAMS", True
        ), mock.patch.object(
            fetch_injuries.fetch_espn, "fetch_injuries", return_value=espn_rows
        ), mock.patch.object(
            fetch_injuries, "_save_snapshot"
        ):
            payload = fetch_injuries.fetch_injuries(self.GAMES, date(2026, 11, 19))

        self.assertEqual(payload["source"], "nbainjuries+espn")
        self.assertEqual({row["team"] for row in payload["injuries"]}, {"GSW", "MEM"})
        self.assertTrue(payload["espn_timestamp_utc"])
        butler = next(
            row for row in payload["injuries"] if row["player"] == "Jimmy Butler III"
        )
        self.assertEqual(butler["source"], "nbainjuries")  # 官方優先，不被 ESPN 覆蓋


class RestClassificationTests(unittest.TestCase):
    """輪休（rest）與傷病要分開標示。"""

    def test_rest_reason_is_classified_as_rest(self) -> None:
        frame = FakeFrame(
            [
                {
                    "Team": "Boston Celtics",
                    "Player Name": "Al Horford",
                    "Current Status": "Out",
                    "Reason": "Rest",
                    "Game Date": "11/19/2026",
                }
            ]
        )
        injuries, _ = fetch_injuries.parse_report(
            frame, [{"home_team": "BOS", "away_team": "NYK"}], date(2026, 11, 19)
        )
        self.assertEqual(injuries[0]["category"], "rest")
        self.assertEqual(injuries[0]["status"], "out")
        self.assertEqual(injuries[0]["source"], "nbainjuries")

    def test_minutes_restriction_is_not_rest(self) -> None:
        self.assertEqual(
            fetch_injuries.injury_category("out", "Out", "Minutes restriction"),
            "injury",
        )
        self.assertEqual(
            fetch_injuries.injury_category("out", "Out", "Load Management"), "rest"
        )
        self.assertEqual(fetch_injuries.injury_category("out", "Out", "Ankle"), "injury")

    def test_deduction_details_keep_category_and_site_marks_rest(self) -> None:
        injuries = {
            "BOS": [
                {
                    "player": "Al Horford",
                    "status": "out",
                    "status_raw": "Out",
                    "reason": "Rest",
                    "severity": 1.0,
                    "category": "rest",
                }
            ]
        }
        summary = rating_engine.compute_injury_deductions(injuries, {})
        detail = summary["BOS"]["injuries"][0]
        self.assertEqual(detail["category"], "rest")

        rest_html = build_site_module._injury_summary(
            [{"player": "Al Horford", "deduction": 1.2, "category": "rest"}]
        )
        self.assertIn("輪休", rest_html)
        injury_html = build_site_module._injury_summary(
            [{"player": "Jayson Tatum", "deduction": 1.2, "category": "injury"}]
        )
        self.assertNotIn("輪休", injury_html)


class InjurySnapshotTests(unittest.TestCase):
    def test_save_snapshot_writes_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with mock.patch.object(config, "CACHE_DIR", directory), mock.patch.object(
                config, "ENABLE_CACHE", True
            ):
                fetch_injuries._save_snapshot(
                    [{"team": "BOS", "player": "Jayson Tatum"}],
                    datetime(2026, 11, 17, 19, 30, tzinfo=config.TZ_ET),
                )
            files = sorted(directory.glob("injury_report_*.json"))
            self.assertEqual(len(files), 1)
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload[0]["team"], "BOS")

    def test_snapshot_failure_does_not_break_injuries(self) -> None:
        dataframe = FakeFrame(
            [
                {
                    "Team": "Boston Celtics",
                    "Player Name": "Jayson Tatum",
                    "Current Status": "Out",
                    "Reason": "Ankle",
                    "Game Date": "11/17/2026",
                }
            ]
        )
        report = {
            "timestamp": datetime(2026, 11, 17, 17, 30, tzinfo=config.TZ_ET),
            "url": "https://example.com/Injury-Report.pdf",
            "dataframe": dataframe,
        }
        with mock.patch.object(
            fetch_injuries, "latest_report", return_value=report
        ), mock.patch.object(
            fetch_injuries, "_save_snapshot", side_effect=RuntimeError("disk full")
        ):
            payload = fetch_injuries.fetch_injuries(
                [{"home_team": "BOS", "away_team": "NYK"}], date(2026, 11, 17)
            )
        self.assertTrue(payload["available"])
        self.assertEqual(len(payload["injuries"]), 1)
        self.assertEqual(payload["injuries"][0]["team"], "BOS")


def _site_ratings_payload(team_name: str = "Boston Celtics") -> dict:
    return {
        "schema_version": config.SCHEMA_VERSION,
        "season": "2026-27",
        "season_type": config.SEASON_TYPE,
        "data_source": "espn",
        "generated_at": "2026-11-17T00:35:00+00:00",
        "warnings": [],
        "teams": {
            "BOS": {
                "team_id": config.TEAM_BY_ABBREV["BOS"]["id"],
                "abbreviation": "BOS",
                "name": team_name,
                "games_played": 14,
                "wins": 10,
                "losses": 4,
                "base_rating": 64.4,
                "net_rating": 8.2,
                "blended_net_rating": 8.2,
                "recent_net_rating": 6.1,
                "pace": 113.7,
                "data_quality": "ok",
            },
            "ORL": {
                "team_id": config.TEAM_BY_ABBREV["ORL"]["id"],
                "abbreviation": "ORL",
                "name": "Orlando Magic",
                "games_played": 14,
                "wins": 6,
                "losses": 8,
                "base_rating": 47.5,
                "net_rating": -2.5,
                "blended_net_rating": -2.1,
                "recent_net_rating": -1.4,
                "pace": 113.7,
                "data_quality": "ok",
            },
        },
    }


def _site_predictions_payload(away_name: str = "Orlando Magic") -> dict:
    return {
        "schema_version": config.SCHEMA_VERSION,
        "season": "2026-27",
        "data_source": "espn",
        "game_date": "2026-11-17",
        "generated_at": "2026-11-17T00:54:58+00:00",
        "generated_at_taipei": "2026-11-17T08:54:58+08:00",
        "injury_report": {
            "source": "nbainjuries",
            "available": True,
            "report_timestamp_et": "2026-11-16T19:45-05:00",
            "warnings": [],
        },
        "games_count": 1,
        "warnings": ["stats.nba.com 不可用，略過球員數據（傷兵改用預設佔比）。"],
        "predictions": [
            {
                "game_id": "401909994",
                "game_date": "2026-11-17",
                "game_time_et": "19:00",
                "game_time_taipei": "2026-11-17T08:00+08:00",
                "status": 1,
                "status_text": "Scheduled",
                "home_team": "BOS",
                "home_team_name": "Boston Celtics",
                "away_team": "ORL",
                "away_team_name": away_name,
                "home_win_prob": 0.9437,
                "away_win_prob": 0.0563,
                "predicted_winner": "BOS",
                "predicted_home_score": 115.1,
                "predicted_away_score": 96.1,
                "predicted_total": 211.2,
                "confidence": "high",
                "home_injury_deduction": 0.31,
                "away_injury_deduction": 1.25,
                "home_injuries": [
                    {"player": "Queta, Neemias", "status": "out", "deduction": 0.31}
                ],
                "away_injuries": [
                    {"player": "Banchero, Paolo", "status": "out", "deduction": 0.31},
                    {"player": "Isaac, Jonathan", "status": "out", "deduction": 0.31},
                    {"player": "Nelson, Izaiyah", "status": "out", "deduction": 0.31},
                    {"player": "Penda, Noah", "status": "out", "deduction": 0.31},
                ],
            }
        ],
    }


class SiteBuildTests(unittest.TestCase):
    def test_build_site_renders_predictions_and_ratings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ratings_path = directory / "team_ratings.json"
            predictions_path = directory / "daily_predictions.json"
            index_path = directory / "index.html"
            ratings_path.write_text(
                json.dumps(_site_ratings_payload(), ensure_ascii=False), encoding="utf-8"
            )
            predictions_path.write_text(
                json.dumps(_site_predictions_payload(), ensure_ascii=False), encoding="utf-8"
            )

            target = build_site_module.build_site(
                index_path,
                ratings_path=ratings_path,
                predictions_path=predictions_path,
                history_path=directory / "history_log.csv",
                generated_at=datetime(2026, 11, 17, 1, 0, tzinfo=config.TZ_ET),
            )

            html = target.read_text(encoding="utf-8")
            self.assertEqual(target, index_path)
            self.assertIn("Boston Celtics", html)
            self.assertIn("Orlando Magic", html)
            self.assertIn("94.4%", html)
            self.assertIn("espn 備援", html)
            self.assertIn("08:00（台北）", html)
            self.assertIn("Banchero, Paolo", html)
            self.assertIn("資料提醒", html)
            self.assertIn('id="nba-data"', html)
            self.assertIn("尚無已回填賽果", html)

    def test_build_site_adds_realtime_refresh_button(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            generated_at = datetime(2026, 11, 17, 1, 0, tzinfo=config.TZ_ET)
            html = build_site_module.build_site(
                directory / "index.html",
                ratings_path=directory / "missing_ratings.json",
                predictions_path=directory / "missing_predictions.json",
                history_path=directory / "missing_history.csv",
                generated_at=generated_at,
            ).read_text(encoding="utf-8")

        self.assertIn('id="refresh-btn"', html)
        self.assertIn("即時更新", html)
        self.assertIn('id="refresh-status"', html)
        self.assertIn('id="refresh-script"', html)
        # 前端必須用 cache-busting + page_generated_at 判斷是否有新版。
        self.assertIn("no-store", html)
        self.assertIn("searchParams.set", html)
        embedded = html.split('id="nba-data">', 1)[1].split("</script>", 1)[0]
        payload = json.loads(embedded)
        self.assertEqual(
            payload["meta"]["page_generated_at"], config.to_iso(generated_at)
        )


    def test_build_site_marks_espn_injury_fallback_and_rest_players(self) -> None:
        payload = _site_predictions_payload()
        payload["injury_report"] = {
            "source": "espn",
            "available": True,
            "report_timestamp_et": None,
            "warnings": ["官方傷病報告不可用，已改用 ESPN 傷兵名單（2 筆）。"],
        }
        payload["predictions"][0]["home_injuries"] = [
            {
                "player": "Horford, Al",
                "status": "out",
                "deduction": 1.2,
                "category": "rest",
                "source": "espn",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            predictions_path = directory / "daily_predictions.json"
            predictions_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            html = build_site_module.build_site(
                directory / "index.html",
                ratings_path=directory / "missing_ratings.json",
                predictions_path=predictions_path,
                history_path=directory / "missing_history.csv",
            ).read_text(encoding="utf-8")

        self.assertIn("傷兵名單：ESPN 備援", html)
        self.assertIn("輪休", html)
        self.assertNotIn("傷病報告：已取得", html)

    def test_build_site_handles_missing_data_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = build_site_module.build_site(
                directory / "index.html",
                ratings_path=directory / "missing_ratings.json",
                predictions_path=directory / "missing_predictions.json",
                history_path=directory / "missing_history.csv",
            )
            html = target.read_text(encoding="utf-8")
            self.assertIn("尚無預測資料", html)
            self.assertIn("尚無球隊戰力資料", html)
            self.assertIn("unknown", html)

    def test_build_site_escapes_untrusted_names(self) -> None:
        hostile = "<script>alert('x')</script>"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ratings_path = directory / "team_ratings.json"
            predictions_path = directory / "daily_predictions.json"
            ratings_path.write_text(
                json.dumps(_site_ratings_payload(team_name=hostile), ensure_ascii=False),
                encoding="utf-8",
            )
            predictions_path.write_text(
                json.dumps(
                    _site_predictions_payload(away_name=hostile), ensure_ascii=False
                ),
                encoding="utf-8",
            )
            html = build_site_module.build_site(
                directory / "index.html",
                ratings_path=ratings_path,
                predictions_path=predictions_path,
                history_path=directory / "history_log.csv",
            ).read_text(encoding="utf-8")

            self.assertNotIn("<script>alert", html)
            self.assertNotIn("</script><", html)
            self.assertIn("&lt;script&gt;", html)


    @staticmethod
    def _write_history_fixture(path: Path) -> None:
        """兩天份的賽果：一天有賽前＋賽後、一天全數賽後。"""
        rating_engine.write_history(
            path,
            [
                {
                    "game_date": "2026-11-16",
                    "game_time_et": "19:00",
                    "game_id": "1",
                    "home_team": "BOS",
                    "away_team": "NYK",
                    "predicted_winner": "BOS",
                    "actual_home_score": "110",
                    "actual_away_score": "100",
                    "actual_winner": "BOS",
                    "result_correct": "true",
                    "post_game": "false",
                },
                {
                    "game_date": "2026-11-16",
                    "game_time_et": "21:00",
                    "game_id": "2",
                    "home_team": "LAL",
                    "away_team": "GSW",
                    "predicted_winner": "LAL",
                    "actual_home_score": "101",
                    "actual_away_score": "110",
                    "actual_winner": "GSW",
                    "result_correct": "true",
                    "post_game": "false",
                },
                {
                    "game_date": "2026-11-16",
                    "game_time_et": "22:00",
                    "game_id": "3",
                    "home_team": "MIA",
                    "away_team": "ATL",
                    "predicted_winner": "MIA",
                    "actual_home_score": "95",
                    "actual_away_score": "99",
                    "actual_winner": "ATL",
                    "result_correct": "false",
                    "post_game": "true",
                },
                {
                    "game_date": "2026-11-16",
                    "game_time_et": "22:30",
                    "game_id": "4",
                    "home_team": "DAL",
                    "away_team": "PHX",
                    "predicted_winner": "DAL",
                    "actual_home_score": "120",
                    "actual_away_score": "118",
                    "actual_winner": "DAL",
                    "result_correct": "false",
                    "post_game": "true",
                },
                {
                    "game_date": "2026-11-17",
                    "game_time_et": "19:30",
                    "game_id": "5",
                    "home_team": "DEN",
                    "away_team": "UTA",
                    "predicted_winner": "DEN",
                    "actual_home_score": "118",
                    "actual_away_score": "110",
                    "actual_winner": "DEN",
                    "result_correct": "true",
                    "post_game": "true",
                },
            ],
        )

    def test_build_site_reports_pre_game_and_all_accuracy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            history_path = directory / "history_log.csv"
            self._write_history_fixture(history_path)
            html = build_site_module.build_site(
                directory / "index.html",
                ratings_path=directory / "missing_ratings.json",
                predictions_path=directory / "missing_predictions.json",
                history_path=history_path,
            ).read_text(encoding="utf-8")

        # 已回填 5 場，只有 2 場是賽前預測（2/2 命中）；含賽後則是 3/5 命中。
        self.assertIn("5</b>已回填場次", html)
        self.assertIn("2</b>計入命中率", html)
        self.assertIn("100.0%</b>賽前預測命中率", html)
        self.assertIn("60.0%</b>全部命中率", html)
        self.assertIn("近 2 個賽事日命中率", html)
        self.assertIn("賽後排除", html)
        self.assertIn("全數賽後", html)

    def test_build_site_contains_no_merge_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            history_path = directory / "history_log.csv"
            self._write_history_fixture(history_path)
            html = build_site_module.build_site(
                directory / "index.html",
                ratings_path=directory / "missing_ratings.json",
                predictions_path=directory / "missing_predictions.json",
                history_path=history_path,
            ).read_text(encoding="utf-8")

        # 產出的頁面會直接 commit 進 repo，混入衝突標記或重複區塊就會壞掉。
        for marker in ("<<<<<<<", "=======", ">>>>>>>"):
            self.assertNotIn(marker, html)
        self.assertEqual(html.count('id="nba-data"'), 1)
        self.assertEqual(html.count("<footer"), 1)


class PostGameGuardTests(unittest.TestCase):
    """賽前／賽後預測標記與命中率防護。"""

    def setUp(self) -> None:
        self.ratings = rating_engine.compute_team_ratings(_stats_payload())
        self.game = {
            "game_id": "0022600999",
            "game_date": "2026-11-17",
            "game_time_et": "19:30",
            "game_time_utc": "2026-11-18T00:30:00+00:00",
            "home_team": "BOS",
            "away_team": "NYK",
        }

    def test_prediction_before_tipoff_is_marked_pre_game(self) -> None:
        prediction = rating_engine.predict_game(
            self.game,
            self.ratings,
            generated_at=datetime(2026, 11, 17, 12, 0, tzinfo=config.TZ_ET),
        )
        self.assertFalse(prediction["post_game"])
        self.assertEqual(prediction["lead_time_minutes"], 450)
        row = rating_engine.prediction_to_history_row(prediction)
        self.assertEqual(row["post_game"], "false")

    def test_prediction_after_tipoff_is_marked_post_game(self) -> None:
        prediction = rating_engine.predict_game(
            self.game,
            self.ratings,
            generated_at=datetime(2026, 11, 18, 3, 0, tzinfo=config.TZ_ET),
        )
        self.assertTrue(prediction["post_game"])
        self.assertLess(prediction["lead_time_minutes"], 0)
        row = rating_engine.prediction_to_history_row(prediction)
        self.assertEqual(row["post_game"], "true")

    def test_summarize_history_excludes_post_game_rows(self) -> None:
        rows = [
            {
                "game_date": "2026-11-17",
                "game_time_et": "19:30",
                "actual_home_score": "110",
                "actual_away_score": "100",
                "result_correct": "true",
                "post_game": "false",
            },
            {
                "game_date": "2026-11-17",
                "game_time_et": "20:00",
                "actual_home_score": "90",
                "actual_away_score": "99",
                "result_correct": "true",
                "post_game": "true",
            },
        ]
        summary = build_site_module.summarize_history(rows)
        self.assertEqual(summary["total_settled"], 1)
        self.assertEqual(summary["post_game_excluded"], 1)
        self.assertAlmostEqual(summary["accuracy"], 1.0)

    def test_legacy_rows_without_flag_use_predicted_at(self) -> None:
        rows = [
            {
                "game_date": "2026-11-17",
                "game_time_et": "19:30",
                "predicted_at": "2026-11-18T03:10:00+00:00",  # 開賽後
                "actual_home_score": "110",
                "actual_away_score": "100",
                "result_correct": "true",
            },
            {
                "game_date": "2026-11-17",
                "game_time_et": "22:00",
                "predicted_at": "2026-11-17T13:00:00+00:00",  # 開賽前
                "actual_home_score": "90",
                "actual_away_score": "99",
                "result_correct": "false",
            },
        ]
        summary = build_site_module.summarize_history(rows)
        self.assertEqual(summary["post_game_excluded"], 1)
        self.assertEqual(summary["total_settled"], 1)
        self.assertAlmostEqual(summary["accuracy"], 0.0)

    def test_is_predicted_after_tipoff_handles_missing_fields(self) -> None:
        self.assertTrue(
            rating_engine.is_predicted_after_tipoff(
                "2026-11-18T03:10:00+00:00", "2026-11-17", "19:30"
            )
        )
        self.assertFalse(
            rating_engine.is_predicted_after_tipoff(
                "2026-11-17T13:00:00+00:00", "2026-11-17", "22:00"
            )
        )
        self.assertFalse(rating_engine.is_predicted_after_tipoff("", "2026-11-17", "19:30"))
        self.assertFalse(
            rating_engine.is_predicted_after_tipoff("not-a-time", "2026-11-17", "19:30")
        )

    def test_fill_missing_post_game_flags_backfills_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history_log.csv"
            rating_engine.write_history(
                path,
                [
                    {
                        "game_date": "2026-11-17",
                        "game_time_et": "19:30",
                        "game_id": "1",
                        "predicted_at": "2026-11-18T03:10:00+00:00",
                    },
                    {
                        "game_date": "2026-11-17",
                        "game_time_et": "22:00",
                        "game_id": "2",
                        "predicted_at": "2026-11-17T13:00:00+00:00",
                        "post_game": "false",
                    },
                ],
            )
            self.assertEqual(rating_engine.fill_missing_post_game_flags(path), 1)
            rows = {row["game_id"]: row for row in rating_engine.read_history(path)}
            self.assertEqual(rows["1"]["post_game"], "true")
            self.assertEqual(rows["2"]["post_game"], "false")
            # 已有標記的列不再改寫（idempotent）
            self.assertEqual(rating_engine.fill_missing_post_game_flags(path), 0)

    def test_should_keep_pre_game_when_new_run_is_post_game(self) -> None:
        existing = {
            "game_date": "2026-11-17",
            "predictions": [{"game_id": "1", "post_game": False}],
        }
        payload = {
            "game_date": "2026-11-17",
            "predictions": [{"game_id": "1", "post_game": True}],
        }
        self.assertTrue(main_module._should_keep_pre_game(existing, payload))
        payload["predictions"][0]["post_game"] = False
        self.assertFalse(main_module._should_keep_pre_game(existing, payload))

    def test_merge_keeps_previous_rows_for_started_games(self) -> None:
        existing = {
            "game_date": "2026-11-17",
            "predictions": [
                {"game_id": "1", "post_game": False, "predicted_winner": "NYK"},
                {"game_id": "2", "post_game": False, "predicted_winner": "LAL"},
            ],
        }
        payload = {
            "game_date": "2026-11-17",
            "games_count": 2,
            "predictions": [
                {"game_id": "1", "post_game": True, "predicted_winner": "BOS"},
                {"game_id": "2", "post_game": False, "predicted_winner": "NYK"},
            ],
        }
        kept = main_module._merge_pre_game_rows(existing, payload)
        self.assertEqual(kept, ["1"])
        self.assertEqual(payload["games_count"], 2)
        self.assertEqual(payload["predictions"][0]["predicted_winner"], "NYK")
        self.assertFalse(payload["predictions"][0]["post_game"])
        self.assertEqual(payload["predictions"][1]["predicted_winner"], "NYK")

    def test_merge_ignores_other_dates_and_unknown_games(self) -> None:
        existing = {
            "game_date": "2026-11-16",
            "predictions": [{"game_id": "1", "post_game": False, "predicted_winner": "NYK"}],
        }
        payload = {
            "game_date": "2026-11-17",
            "games_count": 1,
            "predictions": [{"game_id": "1", "post_game": True, "predicted_winner": "BOS"}],
        }
        self.assertEqual(main_module._merge_pre_game_rows(existing, payload), [])
        self.assertEqual(payload["predictions"][0]["predicted_winner"], "BOS")

        same_date = {
            "game_date": "2026-11-16",
            "games_count": 1,
            "predictions": [{"game_id": "9", "post_game": True, "predicted_winner": "BOS"}],
        }
        self.assertEqual(main_module._merge_pre_game_rows(existing, same_date), [])
        self.assertEqual(same_date["predictions"][0]["predicted_winner"], "BOS")


class HistorySummaryTests(unittest.TestCase):
    def test_summarize_history_counts_only_settled_games(self) -> None:
        rows = [
            {
                "game_date": "2026-11-16",
                "actual_home_score": "115",
                "actual_away_score": "96",
                "result_correct": "true",
            },
            {
                "game_date": "2026-11-16",
                "actual_home_score": "101",
                "actual_away_score": "110",
                "result_correct": "false",
            },
            {"game_date": "2026-11-17", "actual_home_score": "", "actual_away_score": ""},
        ]
        summary = build_site_module.summarize_history(rows)
        self.assertEqual(summary["total_settled"], 2)
        self.assertEqual(summary["total_correct"], 1)
        self.assertAlmostEqual(summary["accuracy"], 0.5, places=4)
        self.assertEqual(len(summary["days"]), 1)
        self.assertEqual(summary["days"][0]["date"], "2026-11-16")
        self.assertEqual(summary["days"][0]["games"], 2)


    def test_summarize_history_recomputes_missing_result_correct(self) -> None:
        rows = [
            {
                "game_date": "2026-11-20",
                "home_team": "BOS",
                "away_team": "NYK",
                "predicted_winner": "BOS",
                "actual_home_score": "110",
                "actual_away_score": "100",
                "post_game": "false",
            },
            {
                "game_date": "2026-11-20",
                "home_team": "BOS",
                "away_team": "NYK",
                "predicted_winner": "BOS",
                "actual_home_score": "90",
                "actual_away_score": "99",
                "actual_winner": "NYK",
                "post_game": "false",
            },
            {
                "game_date": "2026-11-20",
                "predicted_winner": "BOS",
                "actual_home_score": "101",
                "actual_away_score": "101",
                "post_game": "false",
            },
        ]
        summary = build_site_module.summarize_history(rows)
        self.assertEqual(summary["total_settled"], 3)
        self.assertEqual(summary["total_correct"], 1)
        self.assertAlmostEqual(summary["accuracy"], 1 / 3, places=4)

    def test_summarize_history_reports_all_and_excluded_totals(self) -> None:
        rows = [
            {
                "game_date": "2026-11-16",
                "actual_home_score": "110",
                "actual_away_score": "100",
                "result_correct": "true",
                "post_game": "false",
            },
            {
                "game_date": "2026-11-16",
                "actual_home_score": "90",
                "actual_away_score": "99",
                "result_correct": "false",
                "post_game": "true",
            },
            {
                "game_date": "2026-11-17",
                "actual_home_score": "88",
                "actual_away_score": "99",
                "result_correct": "false",
                "post_game": "true",
            },
        ]
        summary = build_site_module.summarize_history(rows)
        self.assertEqual(summary["settled_rows"], 3)
        self.assertEqual(summary["all_correct"], 1)
        self.assertAlmostEqual(summary["all_accuracy"], 1 / 3, places=4)
        self.assertEqual(summary["total_settled"], 1)
        self.assertEqual(summary["total_correct"], 1)
        self.assertEqual(summary["post_game_excluded"], 2)
        self.assertEqual(summary["excluded_correct"], 0)
        # 整日都是賽後預測的日子只出現在 days_all，不會被當成命中率分母
        self.assertEqual([day["date"] for day in summary["days"]], ["2026-11-16"])
        self.assertEqual(
            [day["date"] for day in summary["days_all"]], ["2026-11-17", "2026-11-16"]
        )
        self.assertEqual(summary["days_all"][0]["games"], 0)
        self.assertEqual(summary["days_all"][0]["excluded"], 1)


    def test_summarize_history_ignores_rows_without_predicted_winner(self) -> None:
        rows = [
            {
                "game_date": "2026-11-20",
                "home_team": "BOS",
                "predicted_winner": "",
                "actual_home_score": "110",
                "actual_away_score": "100",
                "result_correct": "",
                "post_game": "false",
            },
            {
                "game_date": "2026-11-20",
                "home_team": "BOS",
                "predicted_winner": "BOS",
                "actual_home_score": "110",
                "actual_away_score": "100",
                "result_correct": "true",
                "post_game": "false",
            },
        ]
        summary = build_site_module.summarize_history(rows)
        # 沒有預測勝負的列無法計分，不能被當成「沒中」而拉低命中率。
        self.assertEqual(summary["settled_rows"], 1)
        self.assertEqual(summary["total_settled"], 1)
        self.assertEqual(summary["total_correct"], 1)
        self.assertAlmostEqual(summary["accuracy"], 1.0)


    def test_render_history_handles_all_post_game_days(self) -> None:
        rows = [
            {
                "game_date": "2026-11-18",
                "predicted_winner": "BOS",
                "actual_home_score": "110",
                "actual_away_score": "100",
                "result_correct": "true",
                "post_game": "true",
            }
        ]
        summary = build_site_module.summarize_history(rows)
        html = build_site_module._render_history(summary)
        self.assertIn("0</b>計入命中率", html)
        self.assertIn("全數賽後", html)
        # 沒有可計分場次時不能吐出「（0/0）」這種看不懂的數字。
        self.assertNotIn("（0/0）", html)


if __name__ == "__main__":
    unittest.main()

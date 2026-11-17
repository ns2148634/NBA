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

from src import config, fetch_injuries, fetch_stats, rating_engine
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


if __name__ == "__main__":
    unittest.main()

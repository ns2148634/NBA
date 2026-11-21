"""產生 ``docs/index.html`` 靜態戰報（給 GitHub Pages 用）。

只讀 ``data/`` 既有產出、不連網、不依賴 pandas：

* ``data/daily_predictions.json`` -> 今日預測
* ``data/team_ratings.json``      -> 30 隊戰力排行
* ``data/history_log.csv``        -> 近期命中率（重用 :func:`rating_engine.read_history`）

用法::

    python -m src.build_site
    python -m src.build_site --index docs/index.html

設計取捨：整頁資料在產生時就渲染成 HTML，並額外內嵌一份精簡 JSON
（``<script type="application/json" id="nba-data">``），因此開頁時不需要
瀏覽器端 fetch，也就沒有 CORS / 相對路徑問題，單一檔案即可分享。

頁面上的「即時更新」按鈕只在使用者點擊時才連網：以 cache-busting 參數
（``?_=時間戳``）取回自身頁面、比對內嵌 JSON 的 ``meta.page_generated_at``，
有新版才重新載入，藉此繞過 GitHub Pages CDN 約 10 分鐘的快取。
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import config, rating_engine

LOGGER = logging.getLogger(__name__)

#: 靜態頁面輸出位置
DOCS_DIR = config.BASE_DIR / "docs"
SITE_INDEX_PATH = DOCS_DIR / "index.html"

#: 近期命中率表格顯示的賽事日數量
RECENT_DAYS_WINDOW = 7

#: 傷兵摘要每隊最多列出的球員數
INJURY_NAMES_LIMIT = 3

_CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}
_SETTLED_FIELDS = ("actual_home_score", "actual_away_score")
_SITE_TITLE = "NBA 每日預測"


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _esc(value: Any) -> str:
    """HTML 轉義（``None`` 視為空字串）。"""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _num(value: Any, digits: int = 1, default: str = "—") -> str:
    """數值格式化；非數字或 NaN / inf 時回傳 ``default``。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return f"{number:.{digits}f}"


def _pct(value: Any, digits: int = 1) -> str:
    """把 0~1 的比例格式化成百分比字串。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number != number:
        return "—"
    return f"{number * 100:.{digits}f}%"


def _load_json(path: Path) -> dict | None:
    """讀取 JSON；檔案不存在、格式錯誤或非物件時回傳 ``None``。"""
    if not path.exists():
        LOGGER.warning("找不到資料檔：%s", path)
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        LOGGER.warning("讀取 %s 失敗：%s", path, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_write(target: Path, text: str) -> Path:
    """先寫暫存檔再 ``os.replace``，避免中斷時留下半個檔案。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(temporary, target)
    return target


def _signed(value: Any, digits: int = 1, default: str = "—") -> str:
    """帶正負號的數值（例如 ``+3.2``）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return f"{number:+.{digits}f}"


# --------------------------------------------------------------------------
# history_log.csv 統計
# --------------------------------------------------------------------------
def _has_result(row: dict) -> bool:
    """該列是否已回填實際比分。"""
    return all(
        str(row.get(field) or "").strip() not in ("", "nan", "None")
        for field in _SETTLED_FIELDS
    )


def _is_correct(row: dict) -> bool:
    """該列預測勝負是否命中。"""
    return str(row.get("result_correct") or "").strip().lower() in ("true", "1", "yes")


def _row_post_game(row: dict) -> bool:
    """該筆預測是否在開賽後才產生（賽後預測不列入命中率）。

    舊版 ``history_log.csv`` 沒有 ``post_game`` 欄位，此時用
    ``predicted_at`` 與「比賽日 + 美東開賽時間」推算。
    """
    flag = str(row.get("post_game") or "").strip().lower()
    if flag in ("true", "1", "yes"):
        return True
    if flag in ("false", "0", "no"):
        return False
    predicted_at = str(row.get("predicted_at") or "").strip()
    game_date = str(row.get("game_date") or "").strip()
    game_time = str(row.get("game_time_et") or "").strip()
    if not predicted_at or not game_date or not game_time:
        return False
    try:
        tipoff = datetime.fromisoformat(f"{game_date}T{game_time}:00").replace(
            tzinfo=config.TZ_ET
        )
        predicted = datetime.fromisoformat(predicted_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if predicted.tzinfo is None:
        predicted = predicted.replace(tzinfo=timezone.utc)
    return predicted > tipoff


def summarize_history(
    rows: Sequence[dict] | None, window: int = RECENT_DAYS_WINDOW
) -> dict:
    """統計已回填賽果的整體與逐日命中率（排除賽後才產生的預測）。"""
    settled = [dict(row) for row in (rows or []) if _has_result(row)]
    settled.sort(
        key=lambda row: (
            str(row.get("game_date") or ""),
            str(row.get("game_time_et") or ""),
        )
    )
    excluded = sum(1 for row in settled if _row_post_game(row))
    settled = [row for row in settled if not _row_post_game(row)]
    correct = sum(1 for row in settled if _is_correct(row))

    buckets: dict[str, dict] = {}
    for row in settled:
        day = str(row.get("game_date") or "")
        bucket = buckets.setdefault(day, {"date": day, "games": 0, "correct": 0})
        bucket["games"] += 1
        if _is_correct(row):
            bucket["correct"] += 1

    days = sorted(buckets.values(), key=lambda item: item["date"], reverse=True)
    days = days[: max(int(window), 1)]
    for bucket in days:
        bucket["accuracy"] = bucket["correct"] / bucket["games"] if bucket["games"] else 0.0

    recent_games = sum(bucket["games"] for bucket in days)
    recent_correct = sum(bucket["correct"] for bucket in days)
    return {
        "total_settled": len(settled),
        "post_game_excluded": excluded,
        "total_correct": correct,
        "accuracy": (correct / len(settled)) if settled else 0.0,
        "recent_games": recent_games,
        "recent_correct": recent_correct,
        "recent_accuracy": (recent_correct / recent_games) if recent_games else 0.0,
        "days": days,
    }


# --------------------------------------------------------------------------
# 各區塊渲染
# --------------------------------------------------------------------------
def _empty_panel(message: str) -> str:
    return f'<div class="panel"><p class="empty">{_esc(message)}</p></div>'


def _source_badge(source: Any) -> str:
    text = str(source or "unknown").strip().lower()
    if text == "espn":
        return '<span class="badge badge-warn">espn 備援（代理指標）</span>'
    if text in ("nba_api", "stats.nba.com"):
        return '<span class="badge badge-ok">stats.nba.com</span>'
    return f'<span class="badge">{_esc(text)}</span>'


def _short_time(value: Any) -> str:
    """``2026-11-17T08:00+08:00`` -> ``08:00``。"""
    text = str(value or "").strip()
    if "T" in text:
        return text.split("T", 1)[1][:5]
    return text


def _short_timestamp(value: Any) -> str:
    """``2026-11-17T08:00+08:00`` -> ``2026-11-17 08:00``。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" in text:
        date_part, rest = text.split("T", 1)
        return f"{date_part} {rest[:5]}"
    return text


def _team_list(ratings: dict | None) -> list[dict]:
    """``team_ratings.json`` 的 ``teams`` 可能是 dict（以縮寫為鍵）或 list。"""
    teams = (ratings or {}).get("teams") or []
    values = list(teams.values()) if isinstance(teams, dict) else list(teams)
    return [dict(team) for team in values if isinstance(team, dict)]


def _render_ratings(ratings: dict | None) -> str:
    teams = _team_list(ratings)
    if not teams:
        return _empty_panel("尚無球隊戰力資料（data/team_ratings.json）。")

    def sort_key(team: dict) -> float:
        try:
            return float(team.get("base_rating") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    teams.sort(key=sort_key, reverse=True)
    body: list[str] = []
    for rank, team in enumerate(teams, start=1):
        wins = team.get("wins")
        losses = team.get("losses")
        record = "—"
        if wins is not None and losses is not None:
            record = f"{_esc(wins)}-{_esc(losses)}"
        quality = str(team.get("data_quality") or "").strip()
        if quality in ("", "ok"):
            quality = "正常"
        body.append(
            "        <tr>"
            f'<td class="num">{rank}</td>'
            f"<td><b>{_esc(team.get('abbreviation'))}</b>"
            f'<br><span class="sub">{_esc(team.get("name"))}</span></td>'
            f'<td class="num">{record}</td>'
            f'<td class="num"><b>{_num(team.get("base_rating"), 1)}</b></td>'
            f'<td class="num">{_signed(team.get("blended_net_rating"), 2)}</td>'
            f'<td class="num">{_signed(team.get("recent_net_rating"), 2)}</td>'
            f'<td class="num">{_num(team.get("pace"), 1)}</td>'
            f"<td>{_esc(quality)}</td>"
            "</tr>"
        )

    return "\n".join(
        [
            '<div class="scroll">',
            '  <table id="ratings-table">',
            "    <thead><tr>"
            '<th class="num">#</th><th>球隊</th><th class="num">戰績</th>'
            '<th class="num">戰力分</th><th class="num">混合 NetRtg</th>'
            '<th class="num">近 10 場</th><th class="num">Pace</th><th>資料</th>'
            "</tr></thead>",
            "    <tbody>",
            *body,
            "    </tbody>",
            "  </table>",
            "</div>",
        ]
    )



def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _injury_summary(injuries: Sequence[dict] | None, limit: int = INJURY_NAMES_LIMIT) -> str:
    """列出傷兵（依扣分排序，最多 ``limit`` 人）；輪休另加標記。"""
    entries = [entry for entry in (injuries or []) if entry.get("player")]
    if not entries:
        return '<span class="sub">無</span>'
    entries.sort(key=lambda entry: _as_float(entry.get("deduction")), reverse=True)
    parts = [
        f'{_esc(entry.get("player"))}（{_num(entry.get("deduction"), 2)}）'
        f'{"·輪休" if str(entry.get("category") or "").lower() == "rest" else ""}'
        for entry in entries[: max(int(limit), 1)]
    ]
    if len(entries) > limit:
        parts.append(f"…共 {len(entries)} 人")
    return "<br>".join(parts)


def _winner_probability(game: dict) -> str:
    winner = str(game.get("predicted_winner") or "")
    home = str(game.get("home_team") or "")
    value = game.get("home_win_prob") if winner == home else game.get("away_win_prob")
    return _pct(value)


def _render_predictions(predictions: dict | None) -> str:
    if not predictions:
        return _empty_panel("尚無預測資料（data/daily_predictions.json）。")

    games = [dict(game) for game in (predictions.get("predictions") or [])]
    game_date = str(predictions.get("game_date") or "")
    if not games:
        return _empty_panel(f"{game_date}（ET）當日沒有 NBA 賽事。")

    games.sort(
        key=lambda game: (
            str(game.get("game_time_et") or ""),
            str(game.get("game_id") or ""),
        )
    )

    injury_report = predictions.get("injury_report") or {}
    source = str(injury_report.get("source") or "").strip()
    report_bits: list[str] = []
    if injury_report.get("available"):
        report_bits.append(
            "傷兵名單：ESPN 備援" if source == "espn" else "傷病報告：已取得"
        )
        if injury_report.get("report_timestamp_et"):
            report_bits.append(f"報告時間 {_esc(injury_report.get('report_timestamp_et'))}")
    else:
        report_bits.append("傷病報告：不適用")
    if source:
        report_bits.append(f"來源 {_esc(source)}")
    if any(game.get("post_game") for game in games):
        report_bits.append("部分預測在開賽後才產生（不列入命中率）")

    body: list[str] = []
    for game in games:
        home = _esc(game.get("home_team"))
        away = _esc(game.get("away_team"))
        winner = _esc(game.get("predicted_winner"))
        confidence = _CONFIDENCE_LABELS.get(
            str(game.get("confidence") or "").strip().lower(),
            _esc(game.get("confidence")),
        )
        status = str(game.get("status") or "").strip().lower()
        status_note = ""
        if status in ("in", "post") and game.get("status_text"):
            status_note = f'<br><span class="sub">{_esc(game.get("status_text"))}</span>'
        body.append(
            "        <tr>"
            f'<td>{_esc(game.get("game_time_et"))}<br>'
            f'<span class="sub">{_esc(_short_time(game.get("game_time_taipei")))}（台北）</span>{status_note}</td>'
            f'<td><b>{away}</b> @ <b class="host">{home}</b><br>'
            f'<span class="sub">{_esc(game.get("away_team_name"))} @ '
            f'{_esc(game.get("home_team_name"))}</span></td>'
            f'<td class="num">{_num(game.get("predicted_away_score"), 1)} - '
            f'{_num(game.get("predicted_home_score"), 1)}'
            f'<br><span class="sub">總分 {_num(game.get("predicted_total"), 1)}</span></td>'
            f'<td class="num"><span class="win">{winner}</span> '
            f'{_winner_probability(game)}</td>'
            f"<td>{confidence}</td>"
            f'<td class="num">{_num(game.get("home_injury_deduction"), 2)} / '
            f'{_num(game.get("away_injury_deduction"), 2)}</td>'
            f'<td>{_injury_summary(game.get("home_injuries"))}'
            f'<br><span class="sub">{_injury_summary(game.get("away_injuries"))}</span></td>'
            "</tr>"
        )

    return "\n".join(
        [
            f'<p class="meta">{" · ".join(report_bits)}</p>',
            '<div class="scroll">',
            '  <table id="predictions-table">',
            "    <thead><tr>"
            '<th>開賽（ET / 台北）</th><th>對戰（客 @ 主）</th>'
            '<th class="num">預測比分</th><th class="num">預測贏家</th><th>信心</th>'
            '<th class="num">傷病扣分（主 / 客）</th><th>傷兵（主 / 客）</th>'
            "</tr></thead>",
            "    <tbody>",
            *body,
            "    </tbody>",
            "  </table>",
            "</div>",
        ]
    )


def _render_warnings(warnings: Sequence[Any] | None) -> str:
    items = [str(item).strip() for item in (warnings or []) if str(item).strip()]
    if not items:
        return ""
    bullets = "\n".join(f"      <li>{_esc(item)}</li>" for item in items)
    return (
        '<div class="panel warn">\n'
        "  <b>資料提醒</b>\n"
        '  <ul class="warn-list">\n'
        f"{bullets}\n"
        "  </ul>\n"
        "</div>"
    )


def _render_history(summary: dict) -> str:
    if not summary.get("total_settled") and not summary.get("post_game_excluded"):
        return _empty_panel("尚無已回填賽果（history_log.csv 的 actual_* 仍為空）。")

    stats = "\n".join(
        [
            '<div class="stats">',
            f'  <span class="stat"><b>{summary["total_settled"]}</b>已回填場次</span>',
            f'  <span class="stat"><b>{summary["total_correct"]}</b>命中</span>',
            f'  <span class="stat"><b>{_pct(summary["accuracy"])}</b>累計命中率</span>',
            f'  <span class="stat"><b>{_pct(summary["recent_accuracy"])}</b>'
            f'近 {len(summary.get("days") or [])} 日命中率</span>',
            "</div>",
        ]
    )
    excluded = int(summary.get("post_game_excluded") or 0)
    if excluded:
        stats += (
            f'\n<p class="sub">另有 {excluded} 場是開賽後才產生的預測，已排除在命中率之外。</p>'
        )

    rows = [
        "        <tr>"
        f"<td>{_esc(day.get('date'))}</td>"
        f'<td class="num">{_esc(day.get("games"))}</td>'
        f'<td class="num">{_esc(day.get("correct"))}</td>'
        f'<td class="num">{_pct(day.get("accuracy"))}</td>'
        "</tr>"
        for day in (summary.get("days") or [])
    ]

    return "\n".join(
        [
            stats,
            '<div class="scroll">',
            '  <table id="history-table">',
            '    <thead><tr><th>賽事日（ET）</th><th class="num">已回填</th>'
            '<th class="num">命中</th><th class="num">命中率</th></tr></thead>',
            "    <tbody>",
            *rows,
            "    </tbody>",
            "  </table>",
            "</div>",
        ]
    )



_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; background: #0b1020; color: #e8ecf7; line-height: 1.5;
  font-family: system-ui, -apple-system, "Segoe UI", "Noto Sans TC", "Microsoft JhengHei", sans-serif;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 28px 16px 48px; }
h1 { font-size: 1.55rem; margin: 0 0 6px; }
h2 { font-size: 1.05rem; margin: 34px 0 10px; padding-bottom: 6px; border-bottom: 1px solid #22304d; }
.meta { margin: 3px 0; color: #93a4c4; font-size: .88rem; }
.badge { display: inline-block; padding: 1px 9px; border-radius: 999px; background: #1b2a45; color: #b9c9e6; font-size: .78rem; }
.badge-warn { background: #4a2f12; color: #ffcc80; }
.badge-ok { background: #123a2a; color: #7ee2b8; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #1b2740; vertical-align: top; }
th { color: #93a4c4; font-weight: 600; font-size: .78rem; letter-spacing: .04em; white-space: nowrap; }
tbody tr:hover { background: #111a2e; }
td.num, th.num { text-align: right; white-space: nowrap; }
.host { color: #8fd0ff; }
.sub { color: #7f8fae; font-size: .78rem; }
.panel { background: #111a2e; border: 1px solid #1b2740; border-radius: 12px; padding: 12px 16px; margin: 14px 0; }
.warn { background: #2a1f10; border-color: #5a3d17; color: #ffd9a3; }
ul.warn-list { margin: 6px 0 0; padding-left: 20px; }
ul.warn-list li { margin: 2px 0; }
.stats { display: flex; flex-wrap: wrap; gap: 22px; margin: 6px 0 14px; }
.stat { color: #93a4c4; font-size: .8rem; }
.stat b { display: block; color: #e8ecf7; font-size: 1.3rem; }
.empty { color: #7f8fae; font-style: italic; margin: 0; }
.win { color: #7ee2b8; font-weight: 600; }
footer { margin-top: 40px; color: #7f8fae; font-size: .8rem; border-top: 1px solid #1b2740; padding-top: 14px; }
footer a { color: #8fd0ff; text-decoration: none; }
footer a:hover { text-decoration: underline; }
.refresh-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin: 10px 0 0; }
.refresh-btn {
  padding: 6px 16px; border-radius: 999px; border: 1px solid #2c3f63;
  background: #1b2a45; color: #d5e2f8; font: inherit; font-size: .84rem;
  cursor: pointer; transition: background .15s ease, border-color .15s ease;
}
.refresh-btn:hover:not(:disabled) { background: #24395e; border-color: #3a548a; }
.refresh-btn:disabled { opacity: .55; cursor: progress; }
.refresh-btn:focus-visible { outline: 2px solid #8fd0ff; outline-offset: 2px; }
.refresh-status { color: #93a4c4; font-size: .8rem; }
.refresh-status.ok { color: #7ee2b8; }
.refresh-status.warn { color: #ffcc80; }
"""


#: 「即時更新」按鈕的前端邏輯（純前端、無外部相依、只在使用者點擊時連網）。
#: 以 cache-busting 參數取回自身頁面，比對內嵌 JSON 的
#: ``meta.page_generated_at``；有新版才用同一個 URL 重新載入。
_REFRESH_JS = """
(function () {
  "use strict";

  var button = document.getElementById("refresh-btn");
  var status = document.getElementById("refresh-status");
  if (!button || !status) { return; }

  var currentGenerated = readGeneratedAt(document);

  function readGeneratedAt(doc) {
    var node = doc.getElementById("nba-data");
    if (!node) { return ""; }
    try {
      var payload = JSON.parse(node.textContent || "{}");
      var meta = payload.meta || {};
      return String(meta.page_generated_at || meta.generated_at || "");
    } catch (error) {
      return "";
    }
  }

  function say(message, tone) {
    status.textContent = message;
    status.className = "refresh-status" + (tone ? " " + tone : "");
  }

  function describe(value) {
    var moment = new Date(value);
    if (isNaN(moment.getTime())) { return value; }
    return moment.toLocaleString("zh-TW", {
      timeZone: "Asia/Taipei",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false
    });
  }

  function isNewer(fresh, current) {
    var left = new Date(fresh);
    var right = new Date(current);
    if (isNaN(left.getTime())) { return false; }
    if (isNaN(right.getTime())) { return true; }
    return left.getTime() > right.getTime();
  }

  function cacheBusted(url) {
    var target = new URL(url, window.location.href);
    target.searchParams.set("_", String(Date.now()));
    return target.toString();
  }

  function stopBusy() {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }

  function refresh() {
    if (!window.fetch || !window.DOMParser || !window.URL) {
      say("此瀏覽器不支援即時更新，改用重新載入。", "warn");
      window.location.reload();
      return;
    }

    var pageUrl = cacheBusted(window.location.href);
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    say("檢查最新資料中…", "");

    window.fetch(pageUrl, { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) { throw new Error("HTTP " + response.status); }
        return response.text();
      })
      .then(function (html) {
        var fresh = new DOMParser().parseFromString(html, "text/html");
        var freshGenerated = readGeneratedAt(fresh);
        if (!freshGenerated || !isNewer(freshGenerated, currentGenerated)) {
          var note = currentGenerated ? "（頁面產生於 " + describe(currentGenerated) + "）" : "";
          say("已是最新資料" + note + "。", "ok");
          stopBusy();
          return;
        }
        say("發現新版本（頁面產生於 " + describe(freshGenerated) + "），重新載入中…", "ok");
        window.location.replace(pageUrl);
      })
      .catch(function (error) {
        stopBusy();
        var reason = error && error.message ? error.message : "網路異常";
        say("無法取得最新資料（" + reason + "），請稍後再試。", "warn");
      });
  }

  button.addEventListener("click", refresh);
})();
"""


def _refresh_script() -> str:
    """回傳「即時更新」按鈕用的 inline script。"""
    return f'<script id="refresh-script">\n{_REFRESH_JS.strip()}\n</script>'


def _repo_slug() -> str:
    """GitHub repo 的 ``owner/name``（CI 內自動取得，本機用預設值）。"""
    return os.environ.get("GITHUB_REPOSITORY", "ns2148634/NBA").strip() or "ns2148634/NBA"


def _render_header(predictions: dict | None, ratings: dict | None, moment: datetime) -> str:
    season = (predictions or {}).get("season") or (ratings or {}).get("season") or "—"
    game_date = (predictions or {}).get("game_date") or "—"
    games_count = (predictions or {}).get("games_count")
    generated = config.to_taipei_iso(moment) or ""
    source = (predictions or {}).get("data_source") or (ratings or {}).get("data_source")

    entries = [
        f"賽季 {_esc(season)}",
        f"比賽日 {_esc(game_date)}（ET）",
    ]
    if games_count is not None:
        entries.append(f"賽事 {_esc(games_count)} 場")
    if generated:
        entries.append(f"產生於 {_esc(_short_timestamp(generated))}（台北）")

    return "\n".join(
        [
            "<header>",
            f"  <h1>{_esc(_SITE_TITLE)}</h1>",
            f'  <p class="meta">{" · ".join(entries)}</p>',
            f'  <p class="meta">資料來源：{_source_badge(source)}</p>',
            '  <div class="refresh-bar">',
            '    <button type="button" id="refresh-btn" class="refresh-btn"'
            ' title="略過 CDN 快取，重新抓取最新版本的頁面">即時更新</button>',
            '    <span id="refresh-status" class="refresh-status" role="status"'
            ' aria-live="polite"></span>',
            "  </div>",
            "</header>",
        ]
    )


def _render_footer(moment: datetime) -> str:
    slug = _repo_slug()
    raw_base = f"https://raw.githubusercontent.com/{slug}/main/data"
    generated = config.to_iso(moment) or ""
    links = " · ".join(
        f'<a href="{raw_base}/{name}">{name}</a>'
        for name in ("daily_predictions.json", "team_ratings.json", "history_log.csv")
    )
    return "\n".join(
        [
            "<footer>",
            f"  <p>原始資料：{links}</p>",
            f'  <p>原始碼：<a href="https://github.com/{slug}">{_esc(slug)}</a>'
            " · 由 GitHub Actions 每日自動更新"
            f" · 本頁產生時間 {_esc(_short_timestamp(generated))}（UTC）</p>",
            "  <p>頁面由 <code>python -m src.build_site</code> 產生，請勿手動編輯。"
            " GitHub Pages 有約 10 分鐘快取，可按頁面上方的「即時更新」"
            "重新抓取最新版本。</p>",
            "</footer>",
        ]
    )



def _embed_json(predictions: dict | None, ratings: dict | None, moment: datetime) -> str:
    """內嵌精簡 JSON，方便用程式取用（不含傷兵明細，控制檔案大小）。"""
    prediction_fields = (
        "game_id",
        "game_time_et",
        "home_team",
        "away_team",
        "predicted_home_score",
        "predicted_away_score",
        "predicted_total",
        "home_win_prob",
        "away_win_prob",
        "predicted_winner",
        "confidence",
    )
    team_fields = (
        "abbreviation",
        "name",
        "wins",
        "losses",
        "games_played",
        "base_rating",
        "net_rating",
        "blended_net_rating",
        "pace",
    )
    payload = {
        "meta": {
            "season": (predictions or {}).get("season") or (ratings or {}).get("season"),
            "game_date": (predictions or {}).get("game_date"),
            "data_source": (predictions or {}).get("data_source")
            or (ratings or {}).get("data_source"),
            "generated_at": (predictions or {}).get("generated_at"),
            # 本頁產生時間（UTC）：「即時更新」按鈕用來判斷抓回來的頁面是否較新。
            "page_generated_at": config.to_iso(moment),
            "schema_version": config.SCHEMA_VERSION,
        },
        "predictions": [
            {field: game.get(field) for field in prediction_fields}
            for game in (predictions or {}).get("predictions") or []
        ],
        "teams": [
            {field: team.get(field) for field in team_fields}
            for team in _team_list(ratings)
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 內嵌在 <script> 裡：把所有 < > & 轉成 \u003c 等 JSON 跳脫，避免提前結束標籤
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f'<script type="application/json" id="nba-data">{blob}</script>'


def _render_html(
    ratings: dict | None,
    predictions: dict | None,
    history_rows: Sequence[dict] | None,
    moment: datetime,
) -> str:
    summary = summarize_history(history_rows)

    warnings: list[Any] = []
    for source in ((predictions or {}).get("warnings"), (ratings or {}).get("warnings")):
        for item in source or []:
            if str(item).strip() and item not in warnings:
                warnings.append(item)

    sections = "\n".join(
        [
            _render_header(predictions, ratings, moment),
            _render_warnings(warnings),
            "<h2>今日預測</h2>",
            _render_predictions(predictions),
            "<h2>球隊戰力排行</h2>",
            _render_ratings(ratings),
            "<h2>預測命中率</h2>",
            _render_history(summary),
            _render_footer(moment),
        ]
    )

    return "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="zh-Hant">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            f"  <title>{_esc(_SITE_TITLE)}</title>",
            "  <style>",
            _CSS.strip(),
            "  </style>",
            "</head>",
            "<body>",
            '  <main class="wrap">',
            sections,
            "  </main>",
            _embed_json(predictions, ratings, moment),
            _refresh_script(),
            "</body>",
            "</html>",
        ]
    ) + "\n"


def build_site(
    index_path: str | Path | None = None,
    *,
    ratings_path: str | Path | None = None,
    predictions_path: str | Path | None = None,
    history_path: str | Path | None = None,
    generated_at: datetime | None = None,
) -> Path:
    """讀取 ``data/`` 後寫出靜態頁面，回傳輸出路徑。"""
    ratings = _load_json(Path(ratings_path or config.TEAM_RATINGS_PATH))
    predictions = _load_json(Path(predictions_path or config.DAILY_PREDICTIONS_PATH))
    history_rows = rating_engine.read_history(Path(history_path or config.HISTORY_LOG_PATH))
    moment = generated_at or config.now_utc()

    document = _render_html(ratings, predictions, history_rows, moment)
    target = Path(index_path or SITE_INDEX_PATH)
    _atomic_write(target, document)

    LOGGER.info(
        "靜態頁面已更新：%s（%d 隊 / %d 場 / %.1f KB）",
        target,
        len(_team_list(ratings)),
        len((predictions or {}).get("predictions") or []),
        len(document.encode("utf-8")) / 1024.0,
    )
    return target


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.build_site",
        description="由 data/ 的產出檔產生 docs/index.html 靜態戰報",
    )
    parser.add_argument("--index", default=None, help="輸出的 index.html 路徑（預設 docs/index.html）")
    parser.add_argument("--ratings", default=None, help="team_ratings.json 路徑")
    parser.add_argument("--predictions", default=None, help="daily_predictions.json 路徑")
    parser.add_argument("--history", default=None, help="history_log.csv 路徑")
    parser.add_argument(
        "--log-level",
        default=config.LOG_LEVEL,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="log 等級",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format=config.LOG_FORMAT,
        datefmt=config.LOG_DATEFMT,
    )
    try:
        build_site(
            args.index,
            ratings_path=args.ratings,
            predictions_path=args.predictions,
            history_path=args.history,
        )
    except OSError as exc:
        LOGGER.error("寫入靜態頁面失敗：%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# NBA 每日球隊戰力評分與賽果預測

以「不架資料庫、只靠 JSON/CSV + GitHub Actions」為前提打造的 NBA 預測系統：

1. 用 [`nba_api`](https://github.com/swar/nba_api) 抓取 30 隊球季／近 10 場進階數據與逐場比分；若 `stats.nba.com` 不可用（雲端 CI 常被 Akamai 封鎖），自動改用 ESPN 公開 API 備援。
2. 計算 0–100 制的**基礎戰力分**（50 = 聯盟平均，分數差 ≈ 預期分差）。
3. 用 [`nbainjuries`](https://github.com/maobvious/nbainjuries) 抓取當日官方傷病報告，換算**傷病扣分**；官方報告抓不到時改用 ESPN 傷兵名單。
4. 產出當日每場比賽的**勝負、比分、勝率、總分**預測，寫入 `data/`。
5. 之後用實際比分**回填歷史命中率**，形成可追蹤的預測紀錄。

所有結果都直接 commit 回倉庫，不需要任何外部資料庫或伺服器。

**線上版（每日自動更新）**：[https://ns2148634.github.io/NBA/](https://ns2148634.github.io/NBA/) — 由 `python -m src.build_site` 產生的靜態頁面，顯示今日預測、30 隊戰力排行與近期命中率。

---

## 1. 檔案結構

```text
NBA/
├─ .github/workflows/
│  ├─ update_base_rating.yml      # 排程 A：每天更新基礎戰力分
│  ├─ daily_prediction.yml        # 排程 B：抓傷病 + 產生當日預測
│  └─ probe_sources.yml           # 手動診斷：印出各資料來源的可用性
├─ data/                          # 會被 GitHub Actions commit 的產出
│  ├─ team_ratings.json           # 30 隊基礎戰力分
│  ├─ daily_predictions.json      # 當日預測（每日覆寫）
│  └─ history_log.csv             # 歷史預測 + 賽果回填（累積）
├─ docs/
│  └─ index.html                  # GitHub Pages 靜態戰報（自動產生，勿手改）
├─ src/
│  ├─ __init__.py
│  ├─ config.py                   # 參數、30 隊對照表、時區、路徑
│  ├─ fetch_stats.py              # nba_api 抓取層（快取 / 重試 / 正規化 / ESPN 備援切換）
│  ├─ fetch_espn.py               # ESPN 公開 API 備援（賽程、逐場比分、球員場均、傷兵名單）
│  ├─ fetch_injuries.py           # nbainjuries 抓取層（15 分鐘粒度探測）
│  ├─ rating_engine.py            # 評分、傷病扣分、預測、JSON/CSV IO
│  ├─ build_site.py               # 產生 docs/index.html（純本地渲染，不連網）
│  └─ main.py                     # CLI 入口（base / predict / both / settle）
├─ tests/
│  ├─ __init__.py
│  └─ test_offline.py             # 80 個離線測試（合成資料，不需網路）
├─ requirements.txt
└─ README.md
```

`.cache/` 會存放 API 原始回應與傷病快照，已在 `.gitignore` 中排除；`data/` 一定要進版控。

---

## 2. 安裝與執行

```bash
pip install -r requirements.txt

# 更新 30 隊基礎戰力分
python -m src.main --mode base

# 產生當日預測（含傷病扣分）
python -m src.main --mode predict

# 提前產生「隔日」預測（晚班／CI 用；目標日期 = 指定日期 + 1 天，ET）
python -m src.main --mode predict --next-day

# 一次做完兩件事
python -m src.main --mode both

# 只回填歷史賽果
python -m src.main --mode settle

# 由 data/ 產生 GitHub Pages 靜態頁（docs/index.html）
python -m src.build_site
```

### CLI 參數

| 參數 | 說明 |
| --- | --- |
| `--mode base\|predict\|both\|settle` | 執行模式，預設 `both` |
| `--date YYYY-MM-DD` | 比賽日期（**美東時間 ET**），預設今天 |
| `--next-day` | 目標日期往後推一天（「下一個美東賽事日」），只適用 `predict` / `both` |
| `--season 2026-27` | 賽季字串，預設依日期推算（10 月起算新賽季） |
| `--season-type "Regular Season"\|"Playoffs"` | 球季類型，預設例行賽 |
| `--skip-injuries` | 不抓傷病報告、不套用傷病扣分 |
| `--no-cache` | 忽略 `.cache/`，強制重新抓取 |
| `--dry-run` | 只計算並印出，不寫入 `data/` |
| `--log-level DEBUG\|INFO\|WARNING\|ERROR` | log 等級 |

### 結束碼

* `0`：成功；或「當天沒有比賽 / 傷病報告不可用 / 賽程暫時失效」等可容忍情況（會留下 warning）。
* `1`：資料抓取失敗且無法產出結果（例如沒有任何可用戰力分，且 nba_api 也失效）。
* `2`：參數錯誤（例如日期格式不對）。

### 資料來源與備援（ESPN fallback）

系統會依下列順序取資料，並把實際來源寫進輸出的 `data_source` 欄位：

1. **nba_api（`stats.nba.com`）**：首選來源，提供真實的 `ORtg / DRtg / Pace`。
2. **ESPN 公開 API（`site.api.espn.com` / `site.web.api.espn.com`）**：自動備援，提供當日賽程、30 隊逐場比分、球員場均數據與傷兵名單。

切換時機會由 `fetch_stats.nba_api_available()` 判斷（單一 process 內快取 30 分鐘）：

* 以 12 秒 timeout 探測一次；本機／雲端的 `stats.nba.com` API 路徑若被 Akamai 封鎖
  （常見表現是 TCP 可連、API 回 0 bytes timeout），直接改走 ESPN，不會浪費 3 次 60 秒重試。
* 先確認 `pandas` 可 import；若系統政策封鎖 numpy DLL（Windows 常見），同樣直接走 ESPN。
* 執行途中若出現環境層級錯誤（`ImportError` / `DLL load failed`），立即標記來源不可用並切換。

ESPN 備援的計算方式（`src/fetch_espn.py`）：

| 項目 | 做法 |
| --- | --- |
| 當日賽程 | `scoreboard?dates=YYYYMMDD`，轉成 ET 日期／時間、狀態碼 1/2/3、比分 |
| 球季數據 | 逐隊 `teams/{id}/schedule?season=2027`（30 次請求，6 小時快取） |
| 休息天數 / B2B | 由逐場賽程自行比對前一場日期 |
| `pace`（代理） | 聯盟平均「每隊每場得分」（約 113–114） |
| `off_rating` / `def_rating`（代理） | `100 × 每場得分 ÷ pace`、`100 × 每場失分 ÷ pace` |
| 近 10 場 | 各隊最近 10 場得分／失分，用同一基準換算 |
| 球員場均數據 | `statistics/byathlete`（1 次請求、約 470–550 人），取代 `LeagueDashPlayerStats` |
| 傷兵名單 | `site.api.espn.com/.../nba/injuries`（1 次請求，1 小時快取） |

因為 ESPN 沒有回合數，`ORtg / DRtg / Pace` 是**代理值**，`warnings` 會明確標示；
但「分差 ↔ 每 100 回合」的換算前後一致，所以 `base_rating`、預測比分與總分仍然可用。

傷兵與球員數據的備援由三個開關控制：

| 設定 | 預設 | 說明 |
| --- | --- | --- |
| `ESPN_PLAYER_STATS_FALLBACK_ENABLED` | `True` | `stats.nba.com` 抓不到球員場均數據時，改用 ESPN `byathlete`；傷病扣分仍依真實影響佔比計算（不再一律 0.03） |
| `ESPN_INJURY_FALLBACK_ENABLED` | `True` | 官方傷病報告 PDF 完全抓不到時，改用 ESPN 傷兵名單（`Out` / `Day-To-Day` 對應同一組 severity） |
| `ESPN_INJURY_FILL_MISSING_TEAMS` | `False` | 官方報告可用時，是否再用 ESPN 補「當日出賽但報告完全沒有傷兵」的球隊；預設關閉，避免把長期傷兵算進來 |

`injury_report.source` 會標示實際來源（`nbainjuries` / `nbainjuries+espn` / `espn`），
每一筆傷兵也有自己的 `source` 欄位，相關說明會寫進 `injury_report.warnings`。

想強制停用備援（例如要確認 nba_api 是否真的可用），把 `src/config.py` 的
`ESPN_FALLBACK_ENABLED` 設為 `False` 即可。需要診斷網路時，可手動執行
`Actions → Probe data sources → Run workflow`，它會列出 `stats.nba.com`、
`cdn.nba.com`、ESPN 各端點的 HTTP 狀態碼。

---

## 3. 評分模型

### 3.1 基礎戰力分

以三種「每 100 回合淨勝分」訊號加權混合：

| 訊號 | 權重 | 說明 |
| --- | --- | --- |
| 球季 Net Rating | 0.40 | `LeagueDashTeamStats`（Advanced / PerGame） |
| 近 10 場 Net Rating | 0.35 | `last_n_games=10` |
| 逐場分差 EMA | 0.25 | `α = 0.20`，以 `LeagueGameLog` 的 `PLUS_MINUS` 計算 |

```
shrink          = min(1, GP / 10)
season_adj      = season_net × shrink
recent_net      = 近 10 場 Net Rating（不足 5 場時改用 season_adj）
ema_net         = EMA(逐場分差) ÷ Pace × 100
blended_net     = 加權平均（只納入有資料的訊號並重新歸一化）

expected_margin = blended_net × Pace ÷ 100
base_rating     = clamp(50 + expected_margin, 25, 75)
```

* 少於 10 場會對球季 Net Rating 做線性收縮，避免開季極端值。
* 完全沒有資料的球隊 → 50 分、`data_quality = "no_games"`。

### 3.2 傷病扣分

```
impact(player) = (PTS×1.0 + AST×1.2 + REB×0.9 + STL×1.5 + BLK×1.5 − TOV×1.0) × MIN/36
share(player)  = impact ÷ 全隊 impact 總和

deduction = share × severity × 16.0 × 0.65     # 單隊上限 12 分
```

| 狀態 | severity |
| --- | --- |
| Out / Suspended | 1.00 |
| Not With Team | 0.90 |
| Doubtful | 0.75 |
| Rest | 0.60 |
| Game-Time Decision | 0.50 |
| Questionable | 0.45 |
| Day-To-Day | 0.35 |
| Probable | 0.15 |
| Available | 0.00（不扣分） |

* 姓名比對：去重音、處理 `Last, First`、同隊同姓需完整姓名相似度 ≥ 0.95；查無資料的傷兵以預設佔比 0.03 計算。
* 傷病報告只保留**當日出賽球隊**，同一球員重複出現時取最嚴重狀態。
* 每筆傷兵都會標記 `category`：`rest`（`Reason: Rest` / `Load Management`）或 `injury`；
  靜態頁會在輪休球員後面加上「·輪休」。分類只影響標示，severity 仍由狀態決定
  （`Out` + `Rest` 代表該場確定不打，仍以 1.00 計算）。
* 資料來源優先序：官方傷病報告 →（報告不可用時）ESPN 傷兵名單；官方報告可用時
  預設不混用，可用 `ESPN_INJURY_FILL_MISSING_TEAMS` 開啟補漏。
* 傷病也會輕微下修總分：`total −= 0.5 × (兩隊扣分合計)`。

### 3.3 賽果預測

```
home_eff = home_rating − home_injury_deduction
away_eff = away_rating − away_injury_deduction

margin = (home_eff − away_eff)
       + 主場優勢（2.6，中立場 0）
       + 休息補償（每多休 1 天 +0.25，上限 +1.0）
       − 背靠背扣分（1.2）

主勝率 = Φ(margin / 12)，限制在 1%–99%

game_pace   = (兩隊 Pace 平均)
home_points = ORtg_home × DRtg_away ÷ 聯盟平均 DRtg × game_pace ÷ 100
away_points = ORtg_away × DRtg_home ÷ 聯盟平均 DRtg × game_pace ÷ 100
total       = clamp(home_points + away_points − 傷病調整, 190, 270)

預測比分 = total/2 ± margin/2
```

信心標籤：`|margin| ≥ 8` → `high`、`≥ 4` → `medium`、其餘 `low`。

---

## 4. 資料格式

### 4.1 `data/team_ratings.json`

```json
{
  "schema_version": 1,
  "season": "2026-27",
  "season_type": "Regular Season",
  "data_source": "nba_api",
  "generated_at": "2026-11-17T07:00:12+00:00",
  "generated_at_taipei": "2026-11-17T15:00:12+08:00",
  "league": {
    "avg_off_rating": 114.21,
    "avg_def_rating": 114.21,
    "avg_net_rating": 0.0,
    "avg_pace": 99.4,
    "teams_count": 30
  },
  "weights": {
    "season": 0.4,
    "recent": 0.35,
    "ema": 0.25,
    "ema_alpha": 0.2,
    "min_games_for_full_weight": 10,
    "recent_games_window": 10
  },
  "warnings": [],
  "teams": {
    "BOS": {
      "team_id": 1610612738,
      "abbreviation": "BOS",
      "name": "Boston Celtics",
      "games_played": 15,
      "wins": 11,
      "losses": 4,
      "base_rating": 58.42,
      "expected_margin": 8.42,
      "off_rating": 118.1,
      "def_rating": 109.7,
      "net_rating": 8.4,
      "pace": 100.2,
      "blended_net_rating": 8.41,
      "ema_margin": 7.9,
      "ema_net_rating": 7.88,
      "recent_net_rating": 9.1,
      "points_for": 116.8,
      "points_against": 108.4,
      "last_game_date": "2026-11-15",
      "data_quality": "ok"
    }
  }
}
```

`data_quality`：`ok`（場次足夠）、`partial`（場次不足 → 已收縮）、`no_games`（無資料 → 50 分）。

### 4.2 `data/daily_predictions.json`

```json
{
  "schema_version": 1,
  "season": "2026-27",
  "data_source": "nba_api",
  "game_date": "2026-11-17",
  "generated_at": "2026-11-17T22:31:05+00:00",
  "generated_at_taipei": "2026-11-18T06:31:05+08:00",
  "ratings_generated_at": "2026-11-17T07:00:12+00:00",
  "injury_report": {
    "source": "nbainjuries",
    "available": true,
    "report_timestamp_et": "2026-11-17T17:30-05:00",
    "report_url": "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-17_05_30PM.pdf",
    "warnings": []
  },
  "games_count": 1,
  "warnings": [],
  "predictions": [
    {
      "game_id": "0022600123",
      "game_date": "2026-11-17",
      "game_time_et": "19:30",
      "game_time_taipei": "2026-11-18T08:30+08:00",
      "post_game": false,
      "lead_time_minutes": 450,
      "home_team": "BOS",
      "away_team": "NYK",
      "home_rating": 58.42,
      "away_rating": 49.13,
      "home_injury_deduction": 2.84,
      "away_injury_deduction": 0.0,
      "home_rest_days": 1,
      "away_rest_days": 0,
      "home_b2b": false,
      "away_b2b": true,
      "predicted_margin": 9.85,
      "predicted_home_score": 116.9,
      "predicted_away_score": 107.1,
      "predicted_total": 224.0,
      "home_win_prob": 0.7939,
      "away_win_prob": 0.2061,
      "predicted_winner": "BOS",
      "confidence": "high",
      "home_injuries": [
        {
          "player": "Jayson Tatum",
          "status": "questionable",
          "severity": 0.45,
          "impact_share": 0.2213,
          "matched": true,
          "match_score": 1.0,
          "deduction": 2.07
        }
      ],
      "away_injuries": [],
      "factors": {
        "home_court_advantage": 2.6,
        "home_rest_adjustment": 2.6,
        "away_rest_adjustment": -1.2,
        "pace": 99.8
      }
    }
  ]
}
```

`post_game`：預測產生時間晚於實際開球時間（`game_time_utc`）時為 `true`；
`lead_time_minutes` 是距離開球的提前分鐘數（負值代表已開打）。

> 賽後才產生的預測**不會覆蓋**同日既有的賽前預測：
> 整份都是賽後時沿用舊檔（`main._should_keep_pre_game`），
> 只有部分場次開打時則逐場保留（`main._merge_pre_game_rows`），
> 被保留的場次連 `generated_at` 都維持原值，因此 `predicted_at` 不會變成賽後時間。
> 賽後預測不計入「賽前預測命中率」，靜態頁會標示「部分預測在開賽後才產生（不列入命中率）」，
> 並同時公布把賽後場次一起計入的「全部命中率」，避免只看單一數字誤判模型準度。

### 4.3 `data/history_log.csv`

固定欄位順序，以 `(game_date, game_id)` 為唯一鍵 upsert：

| 欄位 | 說明 |
| --- | --- |
| `game_date`, `game_time_et`, `game_id` | 比賽識別 |
| `home_team`, `away_team` | 官方縮寫 |
| `home_rating`, `away_rating` | 賽前基礎戰力分 |
| `home_injury_deduction`, `away_injury_deduction` | 傷病扣分 |
| `home_b2b`, `away_b2b`, `home_rest_days`, `away_rest_days` | 賽程情境 |
| `predicted_home_score`, `predicted_away_score`, `predicted_margin`, `predicted_total` | 預測比分 |
| `home_win_prob`, `away_win_prob`, `predicted_winner` | 預測勝負 |
| `injury_details_json` | 傷兵明細（compact JSON） |
| `predicted_at` | 預測產生時間 |
| `post_game` | `true` = 開賽後才產生的預測（不計入命中率） |
| `actual_home_score`, `actual_away_score`, `actual_winner`, `result_correct` | 賽後回填 |

* 重跑預測**不會**覆蓋已回填的 `actual_*` / `result_correct`。
* `settle` 與每次 `predict` 結束時都會嘗試回填，重複執行為 idempotent。
* 舊資料沒有 `post_game` 欄位時，會用 `predicted_at` 與「比賽日 + `game_time_et`」推算
  （`rating_engine.is_predicted_after_tipoff`）；`build_site` 產生頁面時會把推算結果
  補寫回 CSV（idempotent），之後判定就固定下來。
* 命中率有兩種算法：`賽前預測命中率` 只算開賽前產生的預測（正式數字），
  `全部命中率` 連賽後才產生的預測一起算（僅供參考）；兩者都公布，避免分母被誤解。
* 靜態頁另外公布「近 7 個賽事日賽前預測比例」（`recent_pre_game_ratio`）：
  ≥ 80% 顯示綠色、≥ 40% 黃色、其餘紅色，並提示可能是 GitHub 排程延遲，
  用來一眼判斷命中率的分母是否健康（門檻見 `build_site.PRE_GAME_RATIO_WARN`）。

---

## 5. GitHub Actions 自動化

| 工作流 | 排程（UTC） | 台灣時間 | 內容 |
| --- | --- | --- | --- |
| `A - Update Base Ratings` | `0 7 * * *` | 15:00 | 更新 `team_ratings.json` + 重建靜態頁 |
| `B - Daily Injury & Prediction` | `0 6,9,12 * * *` | 14:00 / 17:00 / 20:00 | 早班：傷病 + 賽前預測 + 賽果回填 + 重建靜態頁 |
| `B - Daily Injury & Prediction` | `0 11 * * *` | 19:00 | 主執行：同上（美東約 06:00） |
| `B - Daily Injury & Prediction` | `30 22 * * *` | 隔日 06:30 | 備援／回填：若當天已存在賽前預測則不覆蓋，並用 `--next-day` 產生隔日預測 |

* GitHub 排程常有 4–10 小時延遲，因此一天排多趟（美東約 01:00 / 04:00 / 06:00 / 07:00）；
  只要有任何一趟在開打前成功，`history_log.csv` 就會留下賽前預測，命中率的分母才不會失真。
  真的延遲到開打後才跑，程式會標記 `post_game` 並保留既有的賽前預測
  （整份賽後→沿用舊檔；只開打部分場次→逐場保留該場次的賽前預測）。
* 每次執行先 `--mode predict`，再用 `--mode settle` 獨立回填賽果（`if: always()`，
  預測步驟失敗也會補賽果），最後才重建靜態頁並 commit。
* 晚班（`30 22 * * *`）會再用 `--mode predict --next-day` 產生**隔日**預測：即使隔天早班
  延遲到開打後，`history_log.csv` 也已經有一筆賽前預測可以計分（`continue-on-error`，
  失敗不影響當天資料的 commit）。手動觸發時可勾選 `next_day` 重現同樣行為。
* `--mode predict` 如果在開賽後才落地，log 會印出「n/m 場在開賽後才產生預測（最晚落後 k 分鐘）」，
  並在 Actions 上以 `::warning::` 標註（`main._warn_late_predictions`），排程延遲不必等到看頁面才發現。
* 兩者共用 `concurrency: nba-data-write`，避免同時改寫 `data/`。
* `permissions: contents: write`，commit 身分為 `github-actions[bot]`。
* 兩支工作流都會在資料更新後執行 `python -m src.build_site`，並以 `git add data/ docs/` 一起提交，讓 Pages 內容與資料同步。
* 只在資料有變動時才 commit；推送前先 `git pull --rebase --autostash`。
* 工作流 B 會安裝 **Temurin 17**（`nbainjuries` 透過 tabula-py 解析 PDF，需要 JRE）。
* 兩者都支援 `workflow_dispatch`，可手動指定 `season` / `date`。

### GitHub Pages

* 靜態頁面：`docs/index.html`，由 `python -m src.build_site` 產生（純本地渲染，不連網、不依賴 pandas）。
* Pages 設定：Source = `Deploy from a branch` → `main` / `/docs`；首次啟用可用：

  ```bash
  gh api -X POST repos/ns2148634/NBA/pages \
    -f 'source[branch]=main' -f 'source[path]=/docs'
  ```

* 網址：`https://ns2148634.github.io/NBA/`。
* 頁面同時內嵌精簡 JSON（`<script type="application/json" id="nba-data">`），方便程式取用；完整資料仍在 `data/*.json`。
* GitHub Pages CDN 約有 10 分鐘快取，頁面上會顯示資料產生時間。
* 頁面上方有「即時更新」按鈕：點擊後以 `?_=時間戳`（cache-busting）取回自身頁面，
  比對內嵌 JSON 的 `meta.page_generated_at`；有新版才重新載入，否則顯示「已是最新資料」，
  抓取失敗則在按鈕旁顯示原因。

---

## 6. 失敗處理策略

| 情境 | 行為 |
| --- | --- |
| 傷病報告抓不到 / 解析失敗 / 逾時（240 秒） | 記錄 warning，不套用傷病扣分，其餘照常預測 |
| 球員場均數據失敗 | 傷兵改用預設影響佔比 0.03 |
| `ScheduleLeagueV2` 失敗 | 自動改用 `ScoreboardV3` |
| 當日查無賽事 | 寫出 `games_count = 0` 的預測檔，exit 0 |
| `team_ratings.json` 過期（> 26 小時）或賽季不符 | 自動先執行 `base` 再預測 |
| `data/` 完全沒有可用戰力分且 nba_api 也失效 | exit 1，讓 Actions 標記失敗 |

---

## 7. 測試

```bash
python -m unittest discover -s tests -t . -v
```

80 個離線測試（不需網路、不需 pandas）涵蓋：

* EMA、收縮、25–75 夾限、`base_rating = 50 + expected_margin`
* 傷病影響佔比、姓名正規化與同姓球員比對、狀態嚴重度、12 分上限
* 傷病報告的球隊／日期過濾、`Available` 不計入、重複傷兵取最嚴重
* 主場優勢、B2B、休息補償、傷病對分差與總分的影響
* 預測總分區間、比分加總與分差一致性、勝率範圍與加總
* 賽程正規化（UTC → ET）、熱身賽過濾、休息天數、`ScoreboardV3` 備援欄位
* ESPN 備援：縮寫別名對照（`NY/GS/NO/SA/WSH`）、賽事正規化、`pace` 代理換算、逐場 log 去重、休息天數、自動切換與警告
* ESPN 球員場均（`byathlete`）欄位對齊與參數、空回應報錯、`fetch_player_season_stats` 的 nba_api → ESPN 備援與停用開關
* ESPN 傷兵名單：球隊／日期過濾、狀態正規化、官方報告優先（不呼叫 ESPN）、`ESPN_INJURY_FILL_MISSING_TEAMS` 補漏與去重
* 輪休分類（`Rest` / `Load Management` 為 `rest`、`Minutes restriction` 不算）與靜態頁「·輪休」標記
* `history_log.csv` upsert 冪等性、賽果回填、重跑不覆蓋賽果
* 賽前／賽後判定（`post_game`、`lead_time_minutes`）、賽後預測不覆蓋賽前預測（整份沿用＋逐場保留）、命中率排除賽後預測（含舊資料以 `predicted_at` 推算、缺 `result_correct` 時用比分重算、缺 `predicted_winner` 且缺 `result_correct` 的列不計分）、`post_game` 補寫回 CSV 的冪等性
* 傷病快照寫入、快照失敗不影響預測、`--mode predict` 在「有傷兵但無球員數據」時仍能產出扣分
* 靜態頁產生：預測／戰力表渲染、缺檔容錯、HTML 跳脫與 `</script>` 注入防護、命中率統計（賽前／全部雙指標、逐日「賽後排除」欄）、產出頁面不含合併衝突標記且 `id="nba-data"` 只出現一次、「即時更新」按鈕與 `page_generated_at`
* `--next-day` 的日期位移（`settle` 模式會被拒絕）、預測在開賽後落地時的 log 警告與 `::warning::` 標註、
  「近 N 個賽事日賽前預測比例」的統計與顏色門檻、預測區塊標題在內容不是當天時改顯示日期

---

## 8. 注意事項

* **日期以美東時間（ET）為準**：`--date` 與 `game_date` 都是 ET；台北時間請自行 +13 小時（夏令時 +12）。
* Windows 需要 `tzdata`（已列入 `requirements.txt`）。若系統缺少時區資料，程式會退回固定 UTC 位移，但跨日光節約時間可能不準。
* `stats.nba.com` 有速率限制：內建 3 次重試與 5/10 秒退避；API 回應快取 6 小時（`.cache/`）。
* `stats.nba.com` 若被 Akamai 封鎖（GitHub Actions 常見），系統會自動改用 ESPN 備援，並在 `data_source` 標成 `espn`；此時 `ORtg / DRtg / Pace` 是逐場比分換算的代理值。
* `docs/index.html` 是 **產生檔**，請修改 `src/build_site.py` 後重新執行 `python -m src.build_site`，不要手動編輯。
* ESPN 備援模式下，球員場均數據也改由 ESPN `byathlete` 取得，傷病扣分仍依球星影響佔比計算；只有 ESPN 也失敗時才退回 `DEFAULT_UNKNOWN_PLAYER_SHARE`（每位傷兵固定佔比），並在 `injury_report.warnings` 標示。靜態頁會顯示「espn 備援（代理指標）」徽章。
* `ScoreboardV3` 備援依賴 `gameCode`（`YYYYMMDD/AWAYHOME`）判斷主客；若官方格式變動，會退回 `line_score` 的「客隊在前、主隊在後」順序。
* `python -m src.main` 在結束時會 flush log 後呼叫 `os._exit()`：`nbainjuries` / `tabula-py` 會留下非 daemon 執行緒與 Java 子程序，若不強制結束，CI 會卡在 interpreter shutdown（直接呼叫 `main()` 的單元測試不受影響）。
* 若在本機 Windows 看到 `DLL load failed while importing _common: 應用程式控制原則已封鎖此檔案`，代表系統的應用程式控制（WDAC / Smart App Control）封鎖了 numpy 的 DLL；此為環境限制，請改用 GitHub Actions（Ubuntu）或調整系統政策。本專案的核心模組與測試不依賴 pandas，可在受限環境下正常執行。

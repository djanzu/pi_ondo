# disp_pi_ondo — ラズパイの温度 DB 管理

Raspberry Pi で収集した温度を SQLite にためて、1 画面で数値と折れ線グラフを表示する Django アプリ。
(`docs/memo.md` の仕様どおり、docker は使わない / ログイン不要 / 待ち受けポート **8181**)

- Python 3.12+ / Django 5.2 / SQLite / uv / Chart.js (vendored)
- 画面は 1 画面のみ。ログイン不要で `http://<ラズパイのIP>:8181/` にアクセスするだけで見られる

## 必要もの

```bash
# uv が入っていない場合 (公式スクリプト)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## セットアップ

```bash
cd disp_pi_ondo
uv sync                        # .venv を作る (Django が入る)
uv run python manage.py migrate
uv run python manage.py runserver 0.0.0.0:8181
```

ブラウザで `http://127.0.0.1:8181/` を開く。

`Chart.js` は `static/js/chart.umd.min.js` として同梱している (`jsdelivr` 版 4.4.9) ため、
ネットワークが無い環境でもグラフが描画される。

### 動作確認用のデータを入れる

1 年前のデータも一緒に作れる。まず `--year-ago` を実行して 1 年前側を作り、
そのあと通常の 12 時間ぶんを追加投入するのが最短。

```bash
uv run python manage.py seed_temps --year-ago            # 1年前の12時間ぶん
uv run python manage.py seed_temps                       # 現在の12時間ぶん
uv run python manage.py seed_temps --clear --year-ago    # 作り直す
```

主なオプション: `--hours` (既定 12) / `--interval` (既定 10 分) /
`--missing 0.05` (1年前側で欠測させる割合) / `--clear` (全削除してから作成) /
`--seed N` / `--base 26.0` (現在の平均温度) / `--amp 5.0` (変動幅) / `--year-base 24.0`。

### 温度記録をクリアする

`clear_temps` で temps テーブルのレコードを消す。対話では確認プロンプトが出るので、
スクリプトや cron から叩くときは `--yes` を付ける (`--yes` 無しで確認できない環境からは
エラーにして削除しない)。

```bash
uv run python manage.py clear_temps                       # 全件削除 (y/N 確認あり)
uv run python manage.py clear_temps --yes                 # 確認せず全件削除
uv run python manage.py clear_temps --dry-run             # 削除せずに対象件数だけ確認
uv run python manage.py clear_temps --days 30             # 30日より前だけ (定期メンテナンス向け)
uv run python manage.py clear_temps --year-ago            # 1年前の同じ月日時分まわりだけ
uv run python manage.py clear_temps --before "2026-09-01 00:00" --after "2026-01-01"  # 期間指定
uv run python manage.py clear_temps --backup backup.json --yes   # 退避してから削除
```

`--before` / `--after` / `--days` / `--year-ago` を組み合わせると AND 条件になる
(`--after` 以上 `--before` 未満 AND 1年前まわり)。日時書式は API と同じで
`YYYY-MM-DD [HH:MM[:SS]]` (`/` 区切りも可、TZ 指定が無ければ Asia/Tokyo)。
`--backup` で書き出した JSON は `uv run python manage.py loaddata backup.json` で復元できる。

## 画面

`docs/img/fig1.png` のレイアウトを再現した 1 画面。

| 領域 | 内容 |
| --- | --- |
| 現在 | 過去 12 時間ぶん (最大 72 件) の月日時分と温度。新しい順。**最大**を青、**最小**を緑で行ハイライト |
| 1年前 | 同じ月日時分まわりの温度。対応する分が無い場合は ±30 分以内で最も近い計測値を対応づける。**最小**をピンクでハイライト |
| グラフ | 上記 2系列の折れ線。1年前は時刻軸に合わせるため +1 年シフトして現在と同じ目盛りに載せる |

- 表の日時は `26/09/29 16:16`、グラフの目盛りは `9/29 16:10` 形式 (日付は日付が変わる目盛りのみ表示)
- 自動更新: 5 分ごとに `api/dashboard` を取って表とグラフを描き替える。手動リロードは不要

## API

`dt` は一意キー。同じ `dt` で再送すれば上書きされるので、計測側のリトライにそのまま使える。

```bash
# 記録 (Create) — {"dt": "...", "temp": "..."}
curl -s -X POST http://127.0.0.1:8181/api/temps \
  -H 'Content-Type: application/json' \
  -d '{"dt":"2026-09-29 16:10:00","temp":"29.1"}'
# 新規 dt なら HTTP 201 / 同じ dt の再送なら HTTP 200 (上書き)
# -> {"ok":true,"created":true,"id":12,"dt":"2026-09-29 16:10:00","temp":29.1}

# dt を省略するとサーバー時刻で記録する
curl -s -X POST http://127.0.0.1:8181/api/temps -H 'Content-Type: application/json' -d '{"temp":"28.7"}'

# 取得 (Read) — count 件 (既定 72 件) を新しい順に
curl -s "http://127.0.0.1:8181/api/temps/?count=72"

# 期間・系列の絞り込み
curl -s "http://127.0.0.1:8181/api/temps/?count=200&hours=12"          # 過去 12 時間から最大 200 件
curl -s "http://127.0.0.1:8181/api/temps/?count=200&start=2026-09-29%2009:00&end=2026-09-29%2012:00"
curl -s "http://127.0.0.1:8181/api/temps/?count=72&hours=12&year_ago=1" # 1年前の同じ月日時分まわり

# 画面と同じ内容の JSON / 疎通確認
curl -s "http://127.0.0.1:8181/api/dashboard"
curl -s "http://127.0.0.1:8181/api/health"
```

受けつける日時フォーマット: `YYYY-MM-DD HH:MM[:SS]`、`YYYY-MM-DDTHH:MM[:SS[.ffffff]]`、`YYYY-MM-DD`
(`/` 区切りも可、タイムゾーン指定が無ければ Asia/Tokyo)。温度は 0.1 刻み `-999.9〜999.9`。
不正な値は HTTP 400 + `{"ok": false, "error": "..."}` を返す。

URL 一覧:

| URL | 用途 |
| --- | --- |
| `/` | ダッシュボード 1 画面 (`?hours=` / `?count=` で期間・件数を変更) |
| `POST /api/temps` | 1 件登録 (`{"dt","temp"}`) |
| `GET /api/temps/` | `count` 件取得 (`hours` / `start` / `end` / `year_ago` / `order`) |
| `GET /api/dashboard` | 画面相当の JSON |
| `GET /api/health` | レコード数と最新日時 |
| `/django-admin/` | 管理サイト (任意。手入れ用) |

## 運用メモ

- **SECRET_KEY**: 環境変数 `DJANGO_SECRET_KEY` を使えばそのまま、無ければ初回に生成して `.secret_key` (600) に保存する
- **db.sqlite3** がデータベース本体。バックアップはこのファイルをコピーするだけでよい
- `settings.py` は `DEBUG = True` / `ALLOWED_HOSTS = ["*"]`。家庭内 LAN 向けの既定なので、
  外部に公開する場合は `DEBUG = False` と hosts 指定、リバースプロキシ経由の HTTPS を用意する
- 常時稼働させるなら (例: systemd)

```ini
# /etc/systemd/system/ondo.service
[Unit]
Description=disp_pi_ondo dashboard
After=network.target

[Service]
WorkingDirectory=/home/pi/disp_pi_ondo
ExecStart=/home/pi/.local/bin/uv run python manage.py runserver 0.0.0.0:8181 --noreload
Restart=always

[Install]
WantedBy=multi-user.target
```

## テスト

```bash
uv run python manage.py test temps   # 36 tests
```

入力解析・重複上書き・件数/期間の絞り込み・1年前の対応づけ (許容範囲外は除外)・空 DB 表示、
`clear_temps` の確認挙動 (中止 / 対話不可時に削除しない) ・期間指定・退避と復元などを検証。

## 未実装 (memo.md の「カスタマイズ」)

- **表示期間の指定**: memo.md では TBD。画面で期間をピッカー選ぶUIは未実装。
  差し当たり読み取り専用の口として `/?hours=24&count=200` のような URL パラメータだけを付けてある
  (既定は 12 時間 / 72 件のまま)。UI 化が必要ならここを拡張する。

## 構成

```
manage.py            # Django の入口 (リポジトリ直下)
config/              # settings / urls
temps/               # モデル・ビュー・集計・テスト
  models.py          # temps テーブル (dt / temp)
  services.py        # 表示用の集計 (期間取得, 1年前の対応づけ)
  views.py           # 画面 + API
  management/commands/seed_temps.py, clear_temps.py
templates/temps/dashboard.html
static/css/dashboard.css
static/js/dashboard.js, chart.umd.min.js
docs/memo.md, docs/img/fig1.png
```

# android-tab-harvest

ADB経由でAndroid端末のChromeタブを吸い上げて管理するWebアプリ。
Inoreaderのように既読/未読、タグ付け、後で読む機能を備える。

## 動機

スマホで気になる記事をタブで開くが、画面が小さくて読みきれずタブが溜まる。
PCの大画面で未読記事を消化するためのツール。

## 構成

```
Ubuntu24 (Ryzen7 9700X, 64GB RAM, RTX4070super)
├── collector.py         # ADB → Chrome DevTools → SQLite に収集
├── server.py            # FastAPI + Jinja2 + HTMX で閲覧UI
├── summarizer.py        # LM Studio + trafilatura で本文要約 + タグ自動提案
├── tabs.db              # SQLite
└── sync.sh              # (将来) rclone でGoogle Driveにバックアップ
```

## 技術スタック

- Python 3.12+
- FastAPI + Uvicorn
- Jinja2 テンプレート
- HTMX (JSほぼ不要でSPA風操作)
- SQLite (aiosqlite)
- ADB + Chrome DevTools Protocol

## セットアップ

```bash
cd ~/Projects/android-tab-harvest
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 環境変数（端末シリアル等）。.env は gitignore 対象
cp .env.example .env
# .env を編集して実値を入れる
```

## 使い方

```bash
# タブ収集（手動 or cron/systemd timer）
python collector.py

# Web UI起動
python server.py
# → http://localhost:8765
```

## 記事要約（LM Studio）

`summarizer.py` は LM Studio の OpenAI 互換 API に「3行要約 + タグ提案最大3」を依頼し、
`tabs.summary` と `tab_tags` を更新する。本文取得は trafilatura（広告/ナビを除いた記事本文）。

### LM Studio 側のセットアップ

1. 任意のモデルをロード（軽さ重視なら `gemma-4-e4b`、品質重視なら `qwen3.5-9b` 等）
2. 左メニュー **Developer** タブ → **Local Server** → **Status: Running** にする
3. ポートはデフォルト **1234**。別マシンから叩くなら **Serve on Local Network** を ON

### .env

```bash
# 同一マシンの場合（デフォルト）
LM_STUDIO_URL=http://localhost:1234/v1/chat/completions

# 別マシンから叩く場合の例
# LM_STUDIO_URL=http://192.168.1.10:1234/v1/chat/completions
```

### CLI

```bash
# 未要約タブを最大10件まとめて要約（既に summary がある行はスキップ）
python summarizer.py --max 10

# 特定タブだけ要約
python summarizer.py --tab 42
```

### Web UI から

- 各タブ行右の **✨ 要約** ボタン → 1件処理、結果は HTMX で行に差し替え
- ヘッダの **✨ 要約バッチ** → 未要約を最大10件、結果メッセージ付きで戻る

### 自動タグ

| 状況 | 付与されるタグ |
|------|---------------|
| HTTP 404 | `404` + `dead-link` |
| その他のエラー (4xx/5xx/タイムアウト) | `dead-link` |
| 本文抽出失敗（SPA等） | `empty` |
| 正常 | LLM 提案タグを最大3個（既存は重複スキップ） |

## 定期実行のセットアップ

`collector.py` を一定間隔で動かすことで、タブの新規検出・閉じ済判定の sighting を継続的に蓄積できる。
ADB は端末がUSB接続されている時だけ成功するので、未接続時もエラーで落ちず graceful にスキップされる。

### systemd timer（推奨）

ユーザー単位で動かすので sudo 不要。`~/.config/systemd/user/` 配下に作成する。

```ini
# ~/.config/systemd/user/tab-harvest.service
[Unit]
Description=android-tab-harvest collector

[Service]
Type=oneshot
WorkingDirectory=%h/Projects/android-tab-harvest
ExecStart=%h/Projects/android-tab-harvest/.venv/bin/python collector.py
StandardOutput=append:%h/.local/state/tab-harvest.log
StandardError=append:%h/.local/state/tab-harvest.log
```

```ini
# ~/.config/systemd/user/tab-harvest.timer
[Unit]
Description=Run android-tab-harvest collector hourly

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
Persistent=true

[Install]
WantedBy=timers.target
```

有効化:
```bash
mkdir -p ~/.local/state
systemctl --user daemon-reload
systemctl --user enable --now tab-harvest.timer

# 状態確認
systemctl --user list-timers | grep tab-harvest
journalctl --user -u tab-harvest.service -n 50
```

ログオフ後も動かしたい場合は `loginctl enable-linger $USER` を一度実行する。

### cron 版（systemd を使わない場合）

```cron
# crontab -e
0 * * * * cd /home/USER/Projects/android-tab-harvest && .venv/bin/python collector.py >> /tmp/tab-harvest.log 2>&1
```

## 将来の拡張

- rclone で Google Drive に tabs.db バックアップ
- PWA化でスマホからも閲覧

## License

[MIT](LICENSE) © velaciela-ringWing2007

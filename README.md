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
├── tabs.db              # SQLite
├── summarizer.py        # (将来) LM Studio連携で記事要約
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

- LM Studio (localhost:1234) 連携で記事要約
- rclone でGoogle Driveにtabs.dbバックアップ
- PWA化でスマホからも閲覧

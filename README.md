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

## 将来の拡張

- LM Studio (localhost:1234) 連携で記事要約
- rclone でGoogle Driveにtabs.dbバックアップ
- PWA化でスマホからも閲覧

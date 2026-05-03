# CLAUDE.md

## プロジェクト

android-tab-harvest: ADB経由でAndroid端末のChromeタブを吸い上げてWeb UIで管理するツール。

## 必ず読むドキュメント

- SPEC.md: DBスキーマ、URL正規化ルール、Collector仕様、Web UI仕様
- DEVELOPMENT_GUIDE.md: 実装フェーズ、ディレクトリ構成、開発ルール

## 技術スタック

- Python 3.12+, FastAPI, Jinja2, HTMX, aiosqlite, httpx
- SQLite (tabs.db)
- ADB + Chrome DevTools Protocol

## 開発ルール

- TDD: 機能実装前にテストを書く
- conventional commit: `feat:`, `test:`, `fix:`, `refactor:`, `docs:`
- 全関数に型アノテーション
- collector.py は同期処理、server.py は async
- エラーは握りつぶさず、graceful に処理してログ出力

## 機密情報の扱い

- 端末シリアル・APIキー・個別エンドポイントなどは `.env` に書く（gitignore対象）
- 公開してよいテンプレは `.env.example` に置く
- ソースコード・テスト・ドキュメントには実シリアルや実エンドポイントを直書きしない
  （テスト用は `FAKE_SERIAL_001` のようなダミー値を使う）

## セットアップ

```bash
cp .env.example .env
# .env を編集して実値を入れる
```

## テスト

```bash
source .venv/bin/activate
pytest tests/ -v
```

## 起動

```bash
# 収集
python collector.py

# Web UI
python server.py
# → http://localhost:8765
```

## コミットタイミング
- 機能単位でこまめにコミット（動く状態でコミット、壊れた状態でコミットしない）

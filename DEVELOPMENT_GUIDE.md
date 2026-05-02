# DEVELOPMENT_GUIDE.md - Claude Code 実装ガイド

## プロジェクト概要

ADB経由でAndroid端末のChromeタブを吸い上げてSQLiteに保存し、
FastAPI + HTMX のWeb UIで管理するツール。
詳細仕様は SPEC.md を参照。

## 環境

- OS: Ubuntu 24 (Ryzen7 9700X, 64GB RAM)
- Python: 3.12+
- ADB: 事前にインストール済み前提
- エディタ: 任意
- Git: conventional commit形式

## ディレクトリ構成

```
android-tab-harvest/
├── README.md
├── SPEC.md
├── DEVELOPMENT_GUIDE.md
├── requirements.txt
├── collector.py          # ADBタブ収集
├── server.py             # FastAPI + Uvicorn起動
├── db.py                 # DB初期化・マイグレーション・ヘルパー
├── models.py             # データクラス (dataclass or Pydantic)
├── url_utils.py          # URL正規化・ハッシュ
├── templates/
│   ├── base.html         # ベーステンプレート (HTMX/CSS読み込み)
│   ├── index.html        # メイン画面
│   └── partials/
│       ├── tab_list.html # タブ一覧 (HTMXパーシャル)
│       ├── tab_row.html  # タブ1行 (HTMXパーシャル)
│       └── filters.html  # フィルタバー
├── static/
│   └── style.css         # 最小限のCSS
├── tests/
│   ├── test_url_utils.py
│   ├── test_collector.py
│   └── test_server.py
└── tabs.db               # SQLite (gitignore対象)
```

## 実装フェーズ

### Phase 1: 基盤 (まずここを完成させる)

1. **url_utils.py** - URL正規化とハッシュ
   - SPEC.md の正規化ルール通りに実装
   - テスト: UTMパラメータ除去、末尾スラッシュ統一、大小文字、等
   
2. **db.py** - SQLite初期化
   - SPEC.md のスキーマでテーブル作成
   - aiosqlite使用
   - `get_db()` コンテキストマネージャ

3. **models.py** - データクラス
   ```python
   @dataclass
   class Device:
       id: int | None
       serial: str
       nickname: str | None
       model: str | None
       added_at: int

   @dataclass
   class Tab:
       id: int | None
       url: str
       url_hash: str
       title: str | None
       status: str  # unread/read/later/archived
       note: str | None
       summary: str | None
       summarized_at: int | None
       created_at: int
       updated_at: int
       # UI表示用 (JOINで取得)
       devices: list[str] | None = None     # 端末名リスト
       sighting_count: int = 0
       first_seen: int | None = None
       still_open: bool = False
   ```

4. **テスト** - url_utils, db のユニットテスト

### Phase 2: Collector

5. **collector.py** - ADBタブ収集
   - `adb devices` パース → 接続端末列挙
   - `adb forward` → `http://localhost:{port}/json/list` でタブ取得
   - type=="page" かつ chrome:// / about: 以外をフィルタ
   - URL正規化 → INSERT OR IGNORE + sighting追加
   - 複数端末対応 (ポート9222〜)
   - forward後のクリーンアップ
   - CLI: `python collector.py` で即実行
   - 端末未接続時は graceful にスキップ (エラーで落ちない)

6. **テスト** - collectorのモック付きテスト
   - adb/httpのレスポンスをモックして正常系・異常系テスト

### Phase 3: Web UI

7. **server.py** - FastAPIアプリ
   - SPEC.md のエンドポイント通り
   - Jinja2 テンプレート
   - 静的ファイル配信
   - Uvicorn起動 (port=8765)

8. **templates/** - Jinja2 + HTMX
   - base.html: HTMX CDN読み込み、最小CSS
   - index.html: フィルタバー + タブ一覧
   - partials/tab_list.html: タブ一覧 (hx-get="/tabs" で取得)
   - partials/tab_row.html: タブ1行 (ステータス変更で差し替え)
   - CSS: HTMX CDN (https://unpkg.com/htmx.org)

9. **テスト** - APIエンドポイントのテスト (httpx AsyncClient)

### Phase 4: 仕上げ

10. 一括操作 (チェックボックス選択 → まとめて既読)
11. 検索 (title/url/note のFTS)
12. ページネーション
13. systemd timerまたはcron設定の手順をREADMEに追記

## 開発ルール

- **TDD**: 各機能にユニットテスト必須
- **コミット**: conventional commit形式
  - `feat: add url normalization`
  - `test: add url_utils tests`
  - `fix: handle empty device list`
- **コミットタイミング**: Phase内の各番号完了ごと
- **エラーハンドリング**: ADB未接続、端末なし、Chrome未起動 → 全てgraceful処理
- **型ヒント**: 全関数に型アノテーション

## 注意事項

- collector.pyは **同期処理** でOK (asyncにする必要なし、ADB操作がブロッキング)
- server.pyは **async** (FastAPI + aiosqlite)
- tabs.db は .gitignore に入れる
- Chrome DevTools Protocolの疎通は開発者が事前に手動確認する
  (ADB接続やChromeのUSBデバッグ有効化はアプリ側では制御できない)
- HTMX CDNは unpkg.com を使用。ネットワーク制限がある場合は
  ローカルにダウンロードして static/ に配置

## ADB疎通確認手順 (開発者が手動で行う)

```bash
# 1. Android端末をUSB接続、USBデバッグON確認
adb devices
# → デバイスが表示されること

# 2. Chromeのデバッグソケット確認
adb shell cat /proc/net/unix | grep chrome_devtools_remote
# → localabstract:chrome_devtools_remote が表示されること
# 表示されない場合: Chrome > 設定 > 開発者向けオプション確認
# または chrome://flags/#enable-remote-debugging を確認

# 3. ポートフォワード
adb forward tcp:9222 localabstract:chrome_devtools_remote

# 4. タブ一覧取得
curl -s http://localhost:9222/json/list | python -m json.tool
# → タブ情報のJSONが返ること

# 5. クリーンアップ
adb forward --remove tcp:9222
```

上記が全て通れば、collector.py の実装に進める。

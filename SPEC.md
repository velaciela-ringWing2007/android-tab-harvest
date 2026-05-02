# SPEC.md - 詳細仕様

## 1. データベーススキーマ

```sql
CREATE TABLE devices (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  serial   TEXT NOT NULL UNIQUE,   -- adb serial (例: 1A2B3C4D)
  nickname TEXT,                   -- ユーザーが付ける表示名
  model    TEXT,                   -- ro.product.model の値
  added_at INTEGER NOT NULL        -- unix秒
);

CREATE TABLE tabs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  url           TEXT NOT NULL,
  url_hash      TEXT NOT NULL UNIQUE,  -- 正規化URLのSHA1 (重複排除キー)
  title         TEXT,
  status        TEXT NOT NULL DEFAULT 'unread',  -- unread / read / later / archived
  note          TEXT,                  -- ユーザーメモ
  summary       TEXT,                  -- LLM要約 (将来用、当面NULL)
  summarized_at INTEGER,               -- 要約実行日時 (将来用)
  created_at    INTEGER NOT NULL,      -- 初回発見日時
  updated_at    INTEGER NOT NULL       -- 最終更新日時
);

CREATE TABLE tab_sightings (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  tab_id     INTEGER NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
  device_id  INTEGER NOT NULL REFERENCES devices(id),
  seen_at    INTEGER NOT NULL,         -- 吸い上げ実行日時
  tab_active INTEGER NOT NULL DEFAULT 0,  -- 1=その時点でまだ開いていた
  UNIQUE(tab_id, device_id, seen_at)
);

CREATE TABLE tags (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE tab_tags (
  tab_id INTEGER NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (tab_id, tag_id)
);

CREATE INDEX idx_tabs_status ON tabs(status);
CREATE INDEX idx_tabs_updated ON tabs(updated_at DESC);
CREATE INDEX idx_sightings_tab ON tab_sightings(tab_id);
CREATE INDEX idx_sightings_device ON tab_sightings(device_id);
```

## 2. URL正規化ルール

重複排除のため、URLを正規化してからSHA1ハッシュを取る。

1. scheme, host を小文字化
2. トラッキングパラメータを除去:
   - utm_source, utm_medium, utm_campaign, utm_term, utm_content
   - fbclid, gclid, yclid, mc_cid, mc_eid
   - ref, source (これは要検討、サイトによっては意味あり)
3. フラグメント (#...) を除去
4. 末尾スラッシュを統一 (付ける側に統一)
5. クエリパラメータをアルファベット順にソート
6. 空のクエリストリング (?のみ) を除去

```python
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import hashlib

TRACKING_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'fbclid', 'gclid', 'yclid', 'mc_cid', 'mc_eid',
}

def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()
    path = parsed.path.rstrip('/') + '/'
    
    # トラッキングパラメータ除去
    params = parse_qs(parsed.query, keep_blank_values=False)
    filtered = {k: v for k, v in params.items() if k.lower() not in TRACKING_PARAMS}
    query = urlencode(sorted(filtered.items()), doseq=True)
    
    return urlunparse((scheme, host, path, '', query, ''))

def url_hash(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode()).hexdigest()
```

## 3. Collector仕様 (collector.py)

### ADB経由のタブ取得フロー

```
1. adb devices で接続中のAndroid端末一覧を取得
2. 各端末に対して:
   a. adb -s {serial} shell cat /proc/net/unix | grep _devtools_remote
      → Chrome/WebViewのデバッグソケット名を列挙
   b. chrome_devtools_remote がChromeのソケット
   c. adb -s {serial} forward tcp:{port} localabstract:chrome_devtools_remote
   d. GET http://localhost:{port}/json/list → タブ一覧JSON
3. 各タブの url, title を取得
4. URL正規化 → url_hash計算
5. INSERT OR IGNORE INTO tabs (新規なら追加)
6. INSERT INTO tab_sightings (検出履歴を追加)
7. 端末のタブ一覧にないurl → tab_active=0 として記録
```

### Chrome DevTools Protocol レスポンス例

```json
[
  {
    "description": "",
    "devtoolsFrontendUrl": "...",
    "id": "ABC123",
    "title": "Example Article - Site Name",
    "type": "page",
    "url": "https://example.com/article?utm_source=twitter",
    "webSocketDebuggerUrl": "ws://..."
  }
]
```

- `type: "page"` のみ対象 (background_page, service_worker等は除外)
- `url` が chrome:// や about: で始まるものは除外

### ポート管理

複数端末を同時に処理する場合、forwardポートが衝突しないよう
端末ごとに異なるポート (9222, 9223, ...) を使う。処理後に
`adb forward --remove tcp:{port}` でクリーンアップ。

## 4. Web UI仕様 (server.py)

### エンドポイント

```
GET  /                  → メイン画面 (タブ一覧)
GET  /tabs              → タブ一覧 (HTMXパーシャル、フィルタ対応)
POST /tabs/{id}/status  → ステータス変更 (unread/read/later/archived)
POST /tabs/{id}/tags    → タグ追加
DELETE /tabs/{id}/tags/{tag_id}  → タグ削除
POST /tabs/{id}/note    → メモ更新
DELETE /tabs/{id}       → タブ削除
GET  /devices           → 端末一覧
POST /devices/{id}/nickname → 端末ニックネーム設定
POST /collect           → 手動収集トリガー
```

### フィルタ (クエリパラメータ)

```
?status=unread          → ステータスフィルタ
?device=3               → 端末フィルタ (device_id)
?tag=tech               → タグフィルタ
?q=検索ワード            → title/url/note の部分一致検索
?sort=created|updated|sightings  → ソート
?page=1&per_page=50     → ページネーション
```

### UI構成

```
┌──────────────────────────────────────────────┐
│ 🔍 android-tab-harvest        [📱収集] [⚙️]  │
├──────────────────────────────────────────────┤
│ 端末: [全部] [Pixel 9] [Pixel 7a]            │
│ 状態: [全て] [未読 23] [後で読む 5] [既読] [🗄️]│
│ タグ: [all] [tech] [読み物] [+タグ追加]       │
│ 🔍 [検索ボックス                         ]    │
├──────────────────────────────────────────────┤
│ ☐ 記事タイトルがここに表示される              │
│   example.com                                │
│   📱 Pixel 9 · 初検出 4/28 · 5回検出(開いてる)│
│   [✓既読] [📌後で] [🏷️タグ] [📝メモ] [🗑️]    │
│ ─────────────────────────────────────────── │
│ ☐ 別の記事タイトル                           │
│   another-site.dev                           │
│   📱 Pixel 9, Pixel 7a · 初検出 4/25 · 閉じ済│
│   [✓既読] [📌後で] [🏷️タグ] [📝メモ] [🗑️]    │
├──────────────────────────────────────────────┤
│ ← 1 2 3 ... →                     50件/ページ│
└──────────────────────────────────────────────┘
```

### HTMX操作例

既読ボタン:
```html
<button hx-post="/tabs/42/status"
        hx-vals='{"status":"read"}'
        hx-target="#tab-42"
        hx-swap="outerHTML">
  ✓既読
</button>
```
→ サーバーがtab-42の行HTMLだけ返す → その行だけ差し替わる
→ ページ全体リロード不要

## 5. 将来拡張

### LM Studio 要約 (summarizer.py)

- LM Studio の OpenAI互換API (localhost:1234) を使用
- 未要約のタブに対してバッチ実行
- 本文取得: httpxでURL fetch → HTMLからテキスト抽出 (readability or trafilatura)
- プロンプト: 「以下のWebページを3行で日本語要約して」
- 結果を tabs.summary, tabs.summarized_at に書き込み
- UIに「要約」ボタン → 個別に要約実行も可能に

### Google Drive バックアップ (sync.sh)

```bash
#!/bin/bash
rclone copy ~/Projects/android-tab-harvest/tabs.db gdrive:tab-manager/
```
cronで1日1回実行。双方向同期は不要 (Ubuntu機のみが書き込む)。

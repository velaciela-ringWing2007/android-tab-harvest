"""URL正規化と重複排除用ハッシュ。

仕様は SPEC.md セクション2「URL正規化ルール」を参照。
"""

import hashlib
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "yclid",
        "mc_cid",
        "mc_eid",
    }
)


def normalize_url(url: str) -> str:
    """URLを正規化する。

    - scheme/host を小文字化
    - フラグメント除去
    - トラッキングパラメータ除去
    - クエリをアルファベット順にソート
    - 末尾スラッシュ統一（必ず付ける）
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/") + "/"

    pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS
    ]
    pairs.sort()
    query = urlencode(pairs)

    return urlunparse((scheme, host, path, "", query, ""))


def url_hash(url: str) -> str:
    """正規化URLのSHA1ハッシュ（重複排除キー）。"""
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()


def extract_host(url: str) -> str:
    """URLからホスト名を取り出す（lowercase、port除去、`www.` プレフィックス除去）。

    `blog.example.com` などのサブドメインは保持。パース失敗時は空文字を返す。
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host

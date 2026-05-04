"""URL正規化とハッシュのユニットテスト。

SPEC.md セクション2「URL正規化ルール」に基づく。
"""

from url_utils import extract_host, normalize_url, url_hash


class TestNormalizeUrl:
    def test_lowercases_scheme(self) -> None:
        assert normalize_url("HTTPS://example.com/") == "https://example.com/"

    def test_lowercases_host(self) -> None:
        assert normalize_url("https://Example.COM/path/") == "https://example.com/path/"

    def test_preserves_path_case(self) -> None:
        # パスは大小文字を保持する（サーバー次第で意味が変わるため）
        assert (
            normalize_url("https://example.com/Path/Article/")
            == "https://example.com/Path/Article/"
        )

    def test_strips_fragment(self) -> None:
        assert (
            normalize_url("https://example.com/page/#section1")
            == "https://example.com/page/"
        )

    def test_removes_utm_params(self) -> None:
        assert (
            normalize_url(
                "https://example.com/article?utm_source=twitter&utm_medium=social"
            )
            == "https://example.com/article/"
        )

    def test_removes_all_tracking_params(self) -> None:
        url = (
            "https://example.com/x?"
            "utm_source=a&utm_medium=b&utm_campaign=c&utm_term=d&utm_content=e&"
            "fbclid=f&gclid=g&yclid=y&mc_cid=m1&mc_eid=m2"
        )
        assert normalize_url(url) == "https://example.com/x/"

    def test_keeps_non_tracking_params(self) -> None:
        assert (
            normalize_url("https://example.com/search?q=python&page=2")
            == "https://example.com/search/?page=2&q=python"
        )

    def test_keeps_ref_and_source(self) -> None:
        # ref/source はサイトによって意味があるため保持する
        assert (
            normalize_url("https://example.com/a?ref=abc&source=xyz")
            == "https://example.com/a/?ref=abc&source=xyz"
        )

    def test_sorts_query_params_alphabetically(self) -> None:
        assert (
            normalize_url("https://example.com/?z=1&a=2&m=3")
            == "https://example.com/?a=2&m=3&z=1"
        )

    def test_appends_trailing_slash(self) -> None:
        assert normalize_url("https://example.com/article") == "https://example.com/article/"

    def test_collapses_multiple_trailing_slashes(self) -> None:
        assert (
            normalize_url("https://example.com/article///")
            == "https://example.com/article/"
        )

    def test_empty_path_becomes_root_slash(self) -> None:
        assert normalize_url("https://example.com") == "https://example.com/"

    def test_strips_empty_query_string(self) -> None:
        # ?だけ付いている場合は除去
        assert normalize_url("https://example.com/x?") == "https://example.com/x/"

    def test_combined_normalization(self) -> None:
        url = "HTTPS://Example.COM/Article?utm_source=tw&z=1&a=2#frag"
        assert normalize_url(url) == "https://example.com/Article/?a=2&z=1"

    def test_handles_http_scheme(self) -> None:
        assert normalize_url("http://example.com/x") == "http://example.com/x/"


class TestUrlHash:
    def test_same_url_produces_same_hash(self) -> None:
        h1 = url_hash("https://example.com/article")
        h2 = url_hash("https://example.com/article")
        assert h1 == h2

    def test_normalized_equivalents_share_hash(self) -> None:
        # 異なる表記でも正規化結果が同じならハッシュ一致
        h1 = url_hash("https://Example.COM/article?utm_source=tw#top")
        h2 = url_hash("https://example.com/article/")
        assert h1 == h2

    def test_different_urls_produce_different_hashes(self) -> None:
        h1 = url_hash("https://example.com/a")
        h2 = url_hash("https://example.com/b")
        assert h1 != h2

    def test_returns_sha1_hex_string(self) -> None:
        h = url_hash("https://example.com/")
        assert isinstance(h, str)
        assert len(h) == 40
        assert all(c in "0123456789abcdef" for c in h)


class TestExtractHost:
    def test_basic(self) -> None:
        assert extract_host("https://example.com/x") == "example.com"

    def test_strips_www_prefix(self) -> None:
        assert extract_host("https://www.example.com/") == "example.com"

    def test_keeps_other_subdomains(self) -> None:
        assert extract_host("https://blog.example.com/") == "blog.example.com"
        assert extract_host("https://www2.example.com/") == "www2.example.com"

    def test_lowercases(self) -> None:
        assert extract_host("https://EXAMPLE.com/x") == "example.com"

    def test_strips_port(self) -> None:
        assert extract_host("http://localhost:8000/x") == "localhost"

    def test_handles_japanese_domain(self) -> None:
        assert extract_host("https://www.example.co.jp/x") == "example.co.jp"

    def test_invalid_url_returns_empty(self) -> None:
        assert extract_host("") == ""
        assert extract_host("not a url") == ""

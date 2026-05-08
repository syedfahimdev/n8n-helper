import asyncio

import pytest

from lib.jina_fetcher import fetch_jd, _build_jina_url


def test_build_jina_url_prefixes_correctly():
    assert (
        _build_jina_url("https://example.com/job/123")
        == "https://r.jina.ai/https://example.com/job/123"
    )


def test_rejects_non_http_url():
    with pytest.raises(ValueError):
        asyncio.run(fetch_jd("ftp://example.com"))


def test_rejects_missing_scheme():
    with pytest.raises(ValueError):
        asyncio.run(fetch_jd("example.com"))

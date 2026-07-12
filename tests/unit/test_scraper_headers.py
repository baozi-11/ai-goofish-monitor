from src.scraper import (
    _build_extra_headers,
    _get_detail_api_timeout_ms,
    _is_navigation_aborted_error,
)


def test_build_extra_headers_filters_browser_controlled_headers():
    raw_headers = {
        "Accept": "text/html",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.goofish.com/",
        "Sec-CH-UA": '"Chromium";v="131"',
        "Sec-CH-UA-Mobile": "?1",
        "Sec-CH-UA-Platform": '"Android"',
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Origin": "https://www.goofish.com",
        "Cache-Control": "max-age=0",
        "Pragma": "no-cache",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

    assert _build_extra_headers(raw_headers) == {
        "Accept-Language": "zh-CN,zh;q=0.9"
    }


def test_is_navigation_aborted_error_detects_playwright_goto_abort():
    error = Exception(
        "Page.goto: net::ERR_ABORTED at https://www.goofish.com/search?q=test"
    )

    assert _is_navigation_aborted_error(error) is True


def test_is_navigation_aborted_error_ignores_other_errors():
    assert _is_navigation_aborted_error(Exception("Page.goto: net::ERR_TIMED_OUT")) is False


def test_get_detail_api_timeout_ms_defaults_to_45000(monkeypatch):
    monkeypatch.delenv("DETAIL_API_TIMEOUT_MS", raising=False)

    assert _get_detail_api_timeout_ms() == 45000


def test_get_detail_api_timeout_ms_uses_env_value(monkeypatch):
    monkeypatch.setenv("DETAIL_API_TIMEOUT_MS", "25000")

    assert _get_detail_api_timeout_ms() == 25000


def test_get_detail_api_timeout_ms_falls_back_for_invalid_value(monkeypatch):
    monkeypatch.setenv("DETAIL_API_TIMEOUT_MS", "invalid")

    assert _get_detail_api_timeout_ms() == 45000

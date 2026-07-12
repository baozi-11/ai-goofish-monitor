from src.scraper import _build_extra_headers


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

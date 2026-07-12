from src.scraper import (
    QUICK_NOTIFY_REASON,
    _build_extra_headers,
    _build_search_list_result_record,
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


def test_build_search_list_result_record_marks_quick_notify_recommended():
    item_data = {
        "商品标题": "DIM 十字绣",
        "当前售价": "100",
        "商品链接": "https://www.goofish.com/item?id=1",
        "商品ID": "1",
        "商品主图链接": "https://img.example/item.jpg",
        "商品图片列表": ["https://img.example/item.jpg"],
        "发货地区": "上海",
        "卖家昵称": "baozi",
        "发布时间": "2026-07-12 19:00",
        "商品标签": ["包邮"],
        "“想要”人数": "3",
    }

    record = _build_search_list_result_record(
        item_data=item_data,
        keyword="dim十字绣",
        task_name="dim十字绣",
        scraped_at="2026-07-12T19:00:00",
    )

    assert record["爬取时间"] == "2026-07-12T19:00:00"
    assert record["搜索关键字"] == "dim十字绣"
    assert record["任务名称"] == "dim十字绣"
    assert record["商品信息"] is item_data
    assert record["卖家信息"] == {}
    assert record["ai_analysis"] == {
        "analysis_source": "quick_notify",
        "is_recommended": True,
        "reason": QUICK_NOTIFY_REASON,
        "keyword_hit_count": 0,
    }

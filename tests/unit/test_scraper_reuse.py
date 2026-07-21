from datetime import datetime
import asyncio

from src.scraper import (
    ReusableSearchSession,
    NEW_PUBLISH_POPUP_SELECTOR,
    _build_task_filter_signature,
    _can_reuse_search_session,
    _click_new_publish_option_in_open_filter,
    _open_new_publish_filter,
    _requires_confirmed_filter_response,
    _select_latest_ok_search_response,
    _select_search_response_for_processing,
)


def test_can_reuse_search_session_when_successful_session_matches_filters():
    task_config = {
        "keyword": "dim十字绣",
        "new_publish_option": "最新",
        "personal_only": True,
        "free_shipping": True,
        "region": "江苏/南京/全南京",
        "min_price": "10",
        "max_price": "100",
        "max_pages": 1,
    }
    session = ReusableSearchSession(
        filter_signature=_build_task_filter_signature(task_config),
        state_file="state/baozi-175.json",
        proxy_server=None,
        last_success_at=datetime(2026, 7, 21, 23, 15, 44),
    )

    assert _can_reuse_search_session(
        session,
        task_config,
        state_file="state/baozi-175.json",
        proxy_server=None,
    ) is True


def test_can_reuse_search_session_rejects_filter_changes():
    session = ReusableSearchSession(
        filter_signature=_build_task_filter_signature(
            {"keyword": "dim十字绣", "new_publish_option": "最新"}
        ),
        state_file="state/baozi-175.json",
        proxy_server=None,
        last_success_at=datetime(2026, 7, 21, 23, 15, 44),
    )

    assert _can_reuse_search_session(
        session,
        {"keyword": "dim十字绣", "new_publish_option": "1天内"},
        state_file="state/baozi-175.json",
        proxy_server=None,
    ) is False


class _FakeResponse:
    def __init__(self, ok: bool):
        self.ok = ok


def test_select_search_response_requires_filter_response_when_publish_filter_configured():
    initial_response = _FakeResponse(ok=True)

    assert _requires_confirmed_filter_response({"new_publish_option": "最新"}) is True
    assert _select_search_response_for_processing(
        initial_response=initial_response,
        final_response=None,
        requires_filter_response=True,
    ) is None


def test_select_search_response_allows_initial_response_without_publish_filter():
    initial_response = _FakeResponse(ok=True)

    assert _requires_confirmed_filter_response({"new_publish_option": ""}) is False
    assert _select_search_response_for_processing(
        initial_response=initial_response,
        final_response=None,
        requires_filter_response=False,
    ) is initial_response


def test_select_latest_ok_search_response_uses_last_successful_response():
    first_response = _FakeResponse(ok=True)
    stale_response = _FakeResponse(ok=True)
    latest_response = _FakeResponse(ok=True)

    assert _select_latest_ok_search_response(
        first_response,
        [first_response, stale_response, latest_response],
    ) is latest_response


class _FakeLocator:
    def __init__(self, page, name: str, count: int = 1):
        self.page = page
        self.name = name
        self._count = count
        self.clicks = 0
        self.wait_calls = []
        self.first = self
        self.last = self

    async def count(self):
        return self._count

    async def click(self):
        self.clicks += 1

    async def wait_for(self, **kwargs):
        self.wait_calls.append(kwargs)

    def filter(self, **kwargs):
        self.page.filter_calls.append(kwargs)
        return self

    def get_by_text(self, text: str, exact: bool = False):
        self.page.locator_text_calls.append((self.name, text, exact))
        return self.page.option_locator


class _FakePage:
    def __init__(self):
        self.trigger_locator = _FakeLocator(self, "trigger")
        self.popup_locator = _FakeLocator(self, "popup")
        self.option_locator = _FakeLocator(self, "option")
        self.page_clicks = []
        self.text_calls = []
        self.locator_calls = []
        self.locator_text_calls = []
        self.filter_calls = []

    def get_by_text(self, text: str, exact: bool = False):
        self.text_calls.append((text, exact))
        if text == "新发布":
            return self.trigger_locator
        return self.option_locator

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        return self.popup_locator

    async def click(self, selector: str):
        self.page_clicks.append(selector)


def test_click_new_publish_option_uses_scoped_popup_locator():
    page = _FakePage()

    asyncio.run(_open_new_publish_filter(page))
    asyncio.run(_click_new_publish_option_in_open_filter(page, "最新"))

    assert page.trigger_locator.clicks == 1
    assert page.locator_calls == [NEW_PUBLISH_POPUP_SELECTOR]
    assert page.locator_text_calls == [("popup", "最新", True)]
    assert page.option_locator.clicks == 1
    assert page.page_clicks == []

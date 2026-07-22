from datetime import datetime
import asyncio

from src.scraper import (
    ReusableSearchSession,
    NEW_PUBLISH_POPUP_SELECTOR,
    NewPublishOptionNotFoundError,
    NewPublishPopupNotFoundError,
    PlaywrightTimeoutError,
    _build_task_filter_signature,
    _can_reuse_search_session,
    _click_new_publish_option_in_open_filter,
    _capture_search_response_after_action,
    _find_new_publish_option_in_open_filter,
    _open_new_publish_filter,
    _requires_confirmed_filter_response,
    _search_response_stage_for_log,
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
    other_filter_response = _FakeResponse(ok=True)

    assert _requires_confirmed_filter_response({"new_publish_option": "最新"}) is True
    assert _select_search_response_for_processing(
        initial_response=initial_response,
        final_response=other_filter_response,
        publish_response=None,
        requires_filter_response=True,
    ) is None


def test_select_search_response_allows_initial_response_without_publish_filter():
    initial_response = _FakeResponse(ok=True)

    assert _requires_confirmed_filter_response({"new_publish_option": ""}) is False
    assert _select_search_response_for_processing(
        initial_response=initial_response,
        final_response=None,
        publish_response=None,
        requires_filter_response=False,
    ) is initial_response


def test_select_search_response_keeps_publish_confirmation_after_later_filter():
    initial_response = _FakeResponse(ok=True)
    publish_response = _FakeResponse(ok=True)
    later_filter_response = _FakeResponse(ok=True)

    assert _select_search_response_for_processing(
        initial_response=initial_response,
        final_response=later_filter_response,
        publish_response=publish_response,
        requires_filter_response=True,
    ) is later_filter_response


def test_select_search_response_uses_publish_response_when_no_later_filter():
    initial_response = _FakeResponse(ok=True)
    publish_response = _FakeResponse(ok=True)

    assert _select_search_response_for_processing(
        initial_response=initial_response,
        final_response=None,
        publish_response=publish_response,
        requires_filter_response=True,
    ) is publish_response


def test_search_response_stage_prefers_new_publish_when_response_is_shared():
    initial_response = _FakeResponse(ok=True)
    publish_response = _FakeResponse(ok=True)

    assert _search_response_stage_for_log(
        selected_response=publish_response,
        initial_response=initial_response,
        publish_response=publish_response,
        final_response=publish_response,
    ) == "new_publish"


def test_select_latest_ok_search_response_uses_last_successful_response():
    first_response = _FakeResponse(ok=True)
    stale_response = _FakeResponse(ok=True)
    latest_response = _FakeResponse(ok=True)

    assert _select_latest_ok_search_response(
        first_response,
        [first_response, stale_response, latest_response],
    ) is latest_response


class _FakeLocator:
    def __init__(self, page, name: str, count: int = 1, visible: bool = True):
        self.page = page
        self.name = name
        self._count = count
        self.visible = visible
        self.clicks = 0
        self.wait_calls = []
        self.first = self
        self.last = self

    async def count(self):
        return self._count

    async def click(self):
        self.clicks += 1

    async def is_visible(self):
        return self.visible

    async def wait_for(self, **kwargs):
        self.wait_calls.append(kwargs)

    def nth(self, index: int):
        self.page.nth_calls.append((self.name, index))
        return self

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
        self.nth_calls = []

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


def test_find_new_publish_option_reports_missing_popup_before_response_wait():
    page = _FakePage()
    page.popup_locator._count = 0
    page.option_locator._count = 1

    try:
        asyncio.run(_find_new_publish_option_in_open_filter(page, "最新"))
    except NewPublishPopupNotFoundError as exc:
        assert str(exc) == "新发布筛选弹层未出现"
        assert page.option_locator.clicks == 0
        assert page.nth_calls == []
    else:
        raise AssertionError("expected NewPublishPopupNotFoundError")


def test_find_new_publish_option_reports_missing_option_before_response_wait():
    page = _FakePage()
    page.option_locator._count = 0

    try:
        asyncio.run(_find_new_publish_option_in_open_filter(page, "最新"))
    except NewPublishOptionNotFoundError as exc:
        assert str(exc) == "新发布选项 '最新' 未找到"
    else:
        raise AssertionError("expected NewPublishOptionNotFoundError")


class _FakeTimeoutExpectResponse:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is None:
            raise PlaywrightTimeoutError("no search response")
        return False


class _FakeNoResponsePage:
    def __init__(self):
        self.expect_response_calls = 0
        self.listener_events = []

    def on(self, event: str, callback):
        self.listener_events.append(("on", event))

    def remove_listener(self, event: str, callback):
        self.listener_events.append(("remove", event))

    def expect_response(self, predicate, timeout: int):
        self.expect_response_calls += 1
        self.expect_response_timeout = timeout
        return _FakeTimeoutExpectResponse()


def test_capture_search_response_timeout_happens_after_successful_click_action():
    page = _FakeNoResponsePage()
    action_calls = []

    async def action():
        action_calls.append("clicked")

    try:
        asyncio.run(
            _capture_search_response_after_action(
                page=page,
                action=action,
                timeout_ms=20000,
                settle_min_seconds=0,
                settle_max_seconds=0,
            )
        )
    except PlaywrightTimeoutError:
        assert action_calls == ["clicked"]
        assert page.expect_response_calls == 1
        assert page.expect_response_timeout == 20000
        assert page.listener_events == [("on", "response"), ("remove", "response")]
    else:
        raise AssertionError("expected PlaywrightTimeoutError")

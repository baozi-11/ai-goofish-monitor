from datetime import datetime
import asyncio

from src.scraper import (
    ReusableSearchSession,
    SearchRequestTemplate,
    NEW_PUBLISH_POPUP_SELECTOR,
    NewPublishOptionNotFoundError,
    NewPublishPopupNotFoundError,
    LoginRequiredError,
    PlaywrightTimeoutError,
    _build_search_request_template_from_response,
    _build_task_filter_signature,
    _can_replay_search_request,
    _can_reuse_search_session,
    _click_new_publish_option_in_open_filter,
    _capture_search_response_after_action,
    _find_new_publish_option_in_open_filter,
    _is_login_modal_visible,
    _open_new_publish_filter,
    _retain_successful_search_session,
    _replay_search_request_from_session,
    _requires_confirmed_filter_response,
    _search_response_stage_for_log,
    _search_request_template_is_trusted,
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


def test_can_reuse_search_session_allows_filter_changes_for_rescreening():
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
    ) is True


def test_retain_successful_search_session_marks_zero_new_item_sync_success():
    session = ReusableSearchSession()
    task_config = {"keyword": "dim十字绣", "new_publish_option": "最新"}
    page = object()
    context = object()
    browser = object()
    playwright = object()
    template = SearchRequestTemplate(
        url="https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/",
        post_data='data={"pageNumber":1,"keyword":"dim十字绣","fromFilter":true,"sortField":"create","sortValue":"desc"}',
        headers={},
        filter_signature=_build_task_filter_signature(task_config),
        new_publish_option="最新",
    )

    _retain_successful_search_session(
        session=session,
        task_config=task_config,
        state_file="state/baozi-166.json",
        proxy_server=None,
        playwright=playwright,
        browser=browser,
        context=context,
        page=page,
        search_request_template=template,
    )

    assert session.filter_signature == _build_task_filter_signature(task_config)
    assert session.state_file == "state/baozi-166.json"
    assert session.proxy_server is None
    assert session.last_success_at is not None
    assert session.playwright is playwright
    assert session.browser is browser
    assert session.context is context
    assert session.page is page
    assert session.search_request_template is template


def test_can_replay_search_request_with_matching_confirmed_template():
    task_config = {"keyword": "dim十字绣", "new_publish_option": "最新"}
    session = ReusableSearchSession(
        filter_signature=_build_task_filter_signature(task_config),
        state_file="state/baozi-166.json",
        proxy_server=None,
        last_success_at=datetime(2026, 7, 22, 14, 57, 31),
        search_request_template=SearchRequestTemplate(
            url="https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/",
            post_data='data={"pageNumber":1,"keyword":"dim十字绣","fromFilter":true,"sortField":"create","sortValue":"desc"}',
            headers={"content-type": "application/x-www-form-urlencoded"},
            filter_signature=_build_task_filter_signature(task_config),
            new_publish_option="最新",
        ),
    )

    assert _can_replay_search_request(
        session,
        task_config=task_config,
    ) is True


def test_can_replay_search_request_rejects_filter_signature_change():
    original_config = {"keyword": "dim十字绣", "new_publish_option": "最新"}
    session = ReusableSearchSession(
        filter_signature=_build_task_filter_signature(original_config),
        state_file="state/baozi-166.json",
        proxy_server=None,
        last_success_at=datetime(2026, 7, 22, 14, 57, 31),
        search_request_template=SearchRequestTemplate(
            url="https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/",
            post_data='data={"pageNumber":1,"keyword":"dim十字绣","fromFilter":true,"sortField":"create","sortValue":"desc"}',
            headers={},
            filter_signature=_build_task_filter_signature(original_config),
            new_publish_option="最新",
        ),
    )

    assert _can_replay_search_request(
        session,
        task_config={"keyword": "dim十字绣", "new_publish_option": "1天内"},
    ) is False


class _FakeRequest:
    def __init__(self, post_data: str = "", headers=None):
        self.post_data = post_data
        self.headers = headers or {}

    async def all_headers(self):
        return self.headers


class _FakeResponse:
    def __init__(self, ok: bool, post_data: str = "", headers=None):
        self.ok = ok
        self.url = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
        self.request = _FakeRequest(post_data, headers=headers)

    async def json(self):
        return {"data": {"resultList": []}}


def test_build_search_request_template_from_response_keeps_confirmed_request():
    task_config = {"keyword": "dim十字绣", "new_publish_option": "最新"}
    response = _FakeResponse(
        ok=True,
        post_data='data={"pageNumber":1,"keyword":"dim十字绣","fromFilter":true,"sortField":"create","sortValue":"desc"}',
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "accept": "application/json",
            "accept-language": "zh-CN,zh;q=0.9",
            "accept-encoding": "zstd, br, gzip",
            "user-agent": "Mozilla/5.0",
            ":authority": "h5api.m.goofish.com",
            ":method": "POST",
            ":path": "/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/",
            ":scheme": "https",
            "bad header": "invalid",
            "cookie": "redacted",
            "content-length": "123",
        },
    )

    template = asyncio.run(
        _build_search_request_template_from_response(
            response=response,
            task_config=task_config,
            new_publish_option="最新",
        )
    )

    assert template.url == response.url
    assert template.post_data == response.request.post_data
    assert template.filter_signature == _build_task_filter_signature(task_config)
    assert template.new_publish_option == "最新"
    assert template.headers == {
        "content-type": "application/x-www-form-urlencoded",
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9",
        "user-agent": "Mozilla/5.0",
    }


def test_search_request_template_rejects_missing_publish_sort():
    task_config = {"keyword": "dim十字绣", "new_publish_option": "最新"}
    template = SearchRequestTemplate(
        url="https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/",
        post_data='data={"pageNumber":1,"keyword":"dim十字绣","fromFilter":true}',
        headers={},
        filter_signature=_build_task_filter_signature(task_config),
        new_publish_option="最新",
    )

    assert _search_request_template_is_trusted(template, task_config) is False


class _FakeApiResponse:
    def __init__(self, ok=True, json_error=None, body=b"\xb5bad", headers=None):
        self.ok = ok
        self.status = 200 if ok else 500
        self.url = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
        self._json_error = json_error
        self._body = body
        self.headers = headers or {}

    async def json(self):
        if self._json_error is not None:
            raise self._json_error
        return {"data": {"resultList": []}}

    async def body(self):
        return self._body


class _FakeRequestContext:
    def __init__(self, api_response=None):
        self.post_calls = []
        self.api_response = api_response or _FakeApiResponse(ok=True)

    async def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.api_response


class _FakeBrowserContext:
    def __init__(self, api_response=None):
        self.request = _FakeRequestContext(api_response=api_response)


class _FakeReplayPage:
    def __init__(self, api_response=None):
        self.context = _FakeBrowserContext(api_response=api_response)


def test_replay_search_request_uses_saved_template_without_dom():
    task_config = {"keyword": "dim十字绣", "new_publish_option": "最新"}
    template = SearchRequestTemplate(
        url="https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/",
        post_data='data={"pageNumber":1,"keyword":"dim十字绣","fromFilter":true,"sortField":"create","sortValue":"desc"}',
        headers={"content-type": "application/x-www-form-urlencoded"},
        filter_signature=_build_task_filter_signature(task_config),
        new_publish_option="最新",
    )
    session = ReusableSearchSession(
        filter_signature=_build_task_filter_signature(task_config),
        state_file="state/baozi-166.json",
        proxy_server=None,
        last_success_at=datetime(2026, 7, 22, 14, 57, 31),
        search_request_template=template,
    )
    page = _FakeReplayPage()

    response = asyncio.run(
        _replay_search_request_from_session(
            page=page,
            session=session,
            task_config=task_config,
            timeout_ms=20000,
        )
    )

    assert response.ok is True
    assert page.context.request.post_calls == [
        (
            template.url,
            {
                "data": template.post_data,
                "headers": {
                    "content-type": "application/x-www-form-urlencoded",
                    "accept": "application/json, text/plain, */*",
                },
                "timeout": 20000,
            },
        )
    ]


def test_replay_search_request_reports_non_json_response_details():
    task_config = {"keyword": "dim十字绣", "new_publish_option": "最新"}
    template = SearchRequestTemplate(
        url="https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/",
        post_data='data={"pageNumber":1,"keyword":"dim十字绣","fromFilter":true,"sortField":"create","sortValue":"desc"}',
        headers={"content-type": "application/x-www-form-urlencoded"},
        filter_signature=_build_task_filter_signature(task_config),
        new_publish_option="最新",
    )
    session = ReusableSearchSession(
        filter_signature=_build_task_filter_signature(task_config),
        state_file="state/baozi-166.json",
        proxy_server=None,
        last_success_at=datetime(2026, 7, 22, 14, 57, 31),
        search_request_template=template,
    )
    page = _FakeReplayPage(
        api_response=_FakeApiResponse(
            ok=True,
            json_error=UnicodeDecodeError("utf-8", b"\xb5bad", 0, 1, "invalid"),
            body=b"\xb5bad",
            headers={"content-type": "application/octet-stream"},
        )
    )

    try:
        asyncio.run(
            _replay_search_request_from_session(
                page=page,
                session=session,
                task_config=task_config,
                timeout_ms=20000,
            )
        )
    except Exception as exc:
        assert "content-type=application/octet-stream" in str(exc)
        assert "body_hex=b5626164" in str(exc)
    else:
        raise AssertionError("expected replay failure with response diagnostics")


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


def test_select_search_response_rejects_later_filter_that_loses_latest_sort():
    initial_response = _FakeResponse(ok=True)
    publish_response = _FakeResponse(
        ok=True,
        post_data='data={"fromFilter":true,"sortValue":"desc","sortField":"create"}',
    )
    later_filter_response = _FakeResponse(ok=True)

    assert _select_search_response_for_processing(
        initial_response=initial_response,
        final_response=later_filter_response,
        publish_response=publish_response,
        requires_filter_response=True,
    ) is None


def test_select_search_response_allows_later_filter_that_keeps_latest_sort():
    initial_response = _FakeResponse(ok=True)
    publish_response = _FakeResponse(
        ok=True,
        post_data='data={"fromFilter":true,"sortValue":"desc","sortField":"create"}',
    )
    later_filter_response = _FakeResponse(
        ok=True,
        post_data='data={"fromFilter":true,"sortValue":"desc","sortField":"create","extraFilterValue":"{}"}',
    )

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
    def __init__(
        self,
        page,
        name: str,
        count: int = 1,
        visible: bool = True,
        click_error=None,
    ):
        self.page = page
        self.name = name
        self._count = count
        self.visible = visible
        self.click_error = click_error
        self.clicks = 0
        self.wait_calls = []
        self.first = self
        self.last = self

    async def count(self):
        return self._count

    async def click(self):
        self.clicks += 1
        if self.click_error is not None:
            raise self.click_error

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
        return self.page.option_locators.get(text, self.page.empty_locator)


class _FakePage:
    def __init__(self):
        self.trigger_locator = _FakeLocator(self, "trigger")
        self.popup_locator = _FakeLocator(self, "popup")
        self.content_menu_locator = _FakeLocator(self, "content-menu")
        self.login_modal_locator = _FakeLocator(self, "login-modal", count=0)
        self.empty_locator = _FakeLocator(self, "empty", count=0)
        self.option_locators = {
            "最新": _FakeLocator(self, "option:最新"),
            "1天内": _FakeLocator(self, "option:1天内"),
            "3天内": _FakeLocator(self, "option:3天内"),
            "7天内": _FakeLocator(self, "option:7天内"),
            "14天内": _FakeLocator(self, "option:14天内"),
        }
        self.page_clicks = []
        self.text_calls = []
        self.locator_calls = []
        self.locator_text_calls = []
        self.filter_calls = []
        self.nth_calls = []
        self.content_menu_info = None

    def get_by_text(self, text: str, exact: bool = False):
        self.text_calls.append((text, exact))
        if text == "新发布":
            return self.trigger_locator
        return self.option_locators.get(text, self.empty_locator)

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        if "login-modal-wrap" in selector:
            return self.login_modal_locator
        if "data-goofish-new-publish-menu" in selector:
            return self.content_menu_locator
        return self.popup_locator

    async def click(self, selector: str):
        self.page_clicks.append(selector)

    async def evaluate(self, script, arg=None):
        self.evaluate_calls = getattr(self, "evaluate_calls", [])
        self.evaluate_calls.append((script, arg))
        return self.content_menu_info


def test_click_new_publish_option_uses_scoped_popup_locator():
    page = _FakePage()

    asyncio.run(_open_new_publish_filter(page))
    asyncio.run(_click_new_publish_option_in_open_filter(page, "最新"))

    assert page.trigger_locator.clicks == 1
    assert page.locator_calls == [NEW_PUBLISH_POPUP_SELECTOR]
    assert page.locator_text_calls == [("popup", "最新", True)]
    assert page.option_locators["最新"].clicks == 1
    assert page.page_clicks == []


def test_open_new_publish_filter_raises_login_required_when_modal_blocks_click():
    page = _FakePage()
    page.trigger_locator.click_error = PlaywrightTimeoutError(
        "login-modal-wrap intercepts pointer events"
    )
    page.login_modal_locator._count = 1
    page.login_modal_locator.visible = True

    try:
        asyncio.run(_open_new_publish_filter(page))
    except LoginRequiredError as exc:
        assert "login modal" in str(exc)
    else:
        raise AssertionError("expected LoginRequiredError")


def test_is_login_modal_visible_detects_visible_login_overlay():
    page = _FakePage()
    page.login_modal_locator._count = 1
    page.login_modal_locator.visible = True

    assert asyncio.run(_is_login_modal_visible(page)) is True


def test_click_new_publish_option_uses_task_configured_option():
    page = _FakePage()

    asyncio.run(_open_new_publish_filter(page))
    asyncio.run(_click_new_publish_option_in_open_filter(page, "1天内"))

    assert page.option_locators["1天内"].clicks == 1
    assert page.option_locators["最新"].clicks == 0


def test_find_new_publish_option_recognizes_content_menu_without_fixed_popup():
    page = _FakePage()
    page.popup_locator._count = 0
    page.content_menu_info = {
        "matched_options": ["最新", "1天内", "3天内"],
        "text": "最新 1天内 3天内 7天内 14天内",
    }

    option = asyncio.run(_find_new_publish_option_in_open_filter(page, "3天内"))

    assert option is page.option_locators["3天内"]
    assert any("data-goofish-new-publish-menu" in s for s in page.locator_calls)


def test_find_new_publish_option_reports_missing_popup_before_response_wait():
    page = _FakePage()
    page.popup_locator._count = 0
    page.option_locators["最新"]._count = 1

    try:
        asyncio.run(_find_new_publish_option_in_open_filter(page, "最新"))
    except NewPublishPopupNotFoundError as exc:
        assert str(exc) == "新发布筛选弹层未出现"
        assert page.option_locators["最新"].clicks == 0
        assert page.nth_calls == []
    else:
        raise AssertionError("expected NewPublishPopupNotFoundError")


def test_find_new_publish_option_reports_missing_option_before_response_wait():
    page = _FakePage()
    page.option_locators["最新"]._count = 0

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

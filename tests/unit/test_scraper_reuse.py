from datetime import datetime

from src.scraper import (
    ReusableSearchSession,
    _build_task_filter_signature,
    _can_reuse_search_session,
    _requires_confirmed_filter_response,
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

import asyncio
import json
import os
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode

from playwright.async_api import (
    Error as PlaywrightError,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from src.ai_handler import (
    send_ntfy_notification,
    cleanup_task_images,
)
from src.config import (
    LOGIN_IS_EDGE,
    RUN_HEADLESS,
    RUNNING_IN_DOCKER,
    STATE_FILE,
)
from src.parsers import (
    _parse_search_results_json,
    _parse_user_items_data,
    calculate_reputation_from_ratings,
    parse_ratings_data,
    parse_user_head_data,
)
from src.keyword_rule_engine import (
    build_search_text,
    evaluate_keyword_rules,
    extract_keywords_from_alert_rules,
)
from src.utils import (
    get_link_unique_key,
    log_time,
    random_sleep,
    safe_get,
    save_to_jsonl,
)
from src.rotation import RotationPool, load_state_files, parse_proxy_pool, RotationItem
from src.failure_guard import FailureGuard
from src.services.account_strategy_service import resolve_account_runtime_plan
from src.infrastructure.persistence.storage_names import build_result_filename
from src.services.price_history_service import (
    load_price_snapshots,
    parse_price_value,
    record_market_snapshots,
)
from src.services.result_storage_service import load_processed_link_keys
from src.services.search_pagination import (
    advance_search_page,
    is_search_results_response,
)


class RiskControlError(Exception):
    pass


class LoginRequiredError(Exception):
    """Raised when Goofish redirects to the passport/mini_login flow."""


class NewPublishFilterError(Exception):
    """新发布筛选的业务异常，用于区分页面定位失败和接口响应超时。"""


class NewPublishPopupNotFoundError(NewPublishFilterError):
    """点击“新发布”后没有找到筛选弹层，通常是页面结构或风控状态变化。"""


class NewPublishOptionNotFoundError(NewPublishFilterError):
    """筛选弹层已出现，但没有找到任务配置对应的发布时间选项。"""


class SearchRequestReplayError(Exception):
    """复用筛选请求模板重新拉取商品时的可恢复异常。"""


@dataclass
class SearchRequestTemplate:
    """保存一次已确认筛选搜索接口请求，复用轮用它直接重新拉取列表。"""

    url: str
    post_data: str
    headers: dict
    filter_signature: tuple
    new_publish_option: str


@dataclass
class ReusableSearchSession:
    """保存定时监控可复用的浏览器页面和筛选状态。"""

    filter_signature: Optional[tuple] = None
    state_file: Optional[str] = None
    proxy_server: Optional[str] = None
    last_success_at: Optional[datetime] = None
    playwright: Optional[Any] = None
    browser: Optional[Any] = None
    context: Optional[Any] = None
    page: Optional[Any] = None
    search_url: Optional[str] = None
    search_request_template: Optional[SearchRequestTemplate] = None

    async def close(self) -> None:
        """关闭浏览器资源，并清空会话状态，保证下轮从首页冷启动。"""
        for resource in (self.page, self.context, self.browser):
            if resource is None:
                continue
            try:
                await resource.close()
            except Exception:
                pass
        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self.filter_signature = None
        self.state_file = None
        self.proxy_server = None
        self.last_success_at = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.search_url = None
        self.search_request_template = None


FAILURE_GUARD = FailureGuard()
EDGE_DOCKER_WARNING_PRINTED = False
INITIAL_SEARCH_RESPONSE_TIMEOUT_MS = 30_000
INITIAL_SEARCH_RESPONSE_RETRY_COUNT = 2
INITIAL_SEARCH_RESPONSE_RETRY_DELAY_SECONDS = 5
QUICK_NOTIFY_REASON = "搜索列表发现新商品"
NEW_PUBLISH_OPTIONS = ("最新", "1天内", "3天内", "7天内", "14天内")
NEW_PUBLISH_MENU_MARKER = "data-goofish-new-publish-menu"
NEW_PUBLISH_POPUP_SELECTOR = (
    "div.ant-popover, div.ant-select-dropdown, div.ant-dropdown, "
    "div[role='tooltip'], div[role='menu'], "
    "div[class*='Popover'], div[class*='popover'], "
    "div[class*='Dropdown'], div[class*='dropdown']"
)
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _normalize_new_publish_option(task_config: dict) -> str:
    raw_new_publish = task_config.get("new_publish_option") or ""
    new_publish_option = str(raw_new_publish).strip()
    return "" if new_publish_option == "__none__" else new_publish_option


def _build_task_filter_signature(task_config: dict) -> tuple:
    return (
        str(task_config.get("keyword") or "").strip(),
        _normalize_new_publish_option(task_config),
        bool(task_config.get("personal_only", False)),
        bool(task_config.get("free_shipping", False)),
        str(task_config.get("region") or "").strip(),
        str(task_config.get("min_price") or "").strip(),
        str(task_config.get("max_price") or "").strip(),
        int(task_config.get("max_pages", 1) or 1),
    )


def _can_reuse_search_session(
    session: Optional[ReusableSearchSession],
    task_config: dict,
    *,
    state_file: str,
    proxy_server: Optional[str],
) -> bool:
    if session is None or session.last_success_at is None:
        return False
    if session.state_file != state_file or session.proxy_server != proxy_server:
        return False
    return True


def _retain_successful_search_session(
    *,
    session: ReusableSearchSession,
    task_config: dict,
    state_file: str,
    proxy_server: Optional[str],
    playwright: Optional[Any],
    browser: Optional[Any],
    context: Optional[Any],
    page: Optional[Any],
    search_request_template: Optional[SearchRequestTemplate] = None,
) -> None:
    """记录一次完整筛选同步成功后的浏览器会话，供下一轮定时任务复用。"""
    session.filter_signature = _build_task_filter_signature(task_config)
    session.state_file = state_file
    session.proxy_server = proxy_server
    session.last_success_at = datetime.now()
    session.playwright = playwright
    session.browser = browser
    session.context = context
    session.page = page
    session.search_url = None
    session.search_request_template = search_request_template


def _requires_confirmed_filter_response(task_config: dict) -> bool:
    return bool(_normalize_new_publish_option(task_config))


def _select_search_response_for_processing(
    *,
    initial_response: Optional[Any],
    final_response: Optional[Any],
    publish_response: Optional[Any],
    requires_filter_response: bool,
) -> Optional[Any]:
    if requires_filter_response and not (
        publish_response and getattr(publish_response, "ok", False)
    ):
        return None
    if final_response and final_response.ok:
        if requires_filter_response and publish_response is not final_response:
            if not _response_preserves_publish_sort(final_response, publish_response):
                log_time(
                    "搜索响应诊断[selected]: 后续筛选响应未保留新发布排序参数，"
                    "本轮跳过，避免通知未按新发布范围筛选的商品。"
                )
                return None
        return final_response
    if publish_response and getattr(publish_response, "ok", False):
        return publish_response
    return initial_response


def _get_response_post_data(response: Optional[Any]) -> str:
    request = getattr(response, "request", None) if response is not None else None
    post_data = getattr(request, "post_data", "") if request is not None else ""
    if callable(post_data):
        try:
            post_data = post_data()
        except Exception:
            post_data = ""
    if isinstance(post_data, bytes):
        return post_data.decode("utf-8", errors="replace")
    return str(post_data or "")


def _decode_search_post_data(post_data: str) -> dict:
    post_data = str(post_data or "").strip()
    if not post_data:
        return {}
    try:
        if post_data.startswith("{"):
            return json.loads(post_data)
        parsed_form = parse_qs(post_data, keep_blank_values=True)
        data_values = parsed_form.get("data") or []
        if not data_values:
            return {}
        data_value = data_values[0]
        if isinstance(data_value, str) and data_value.strip().startswith("{"):
            return json.loads(data_value)
    except Exception:
        return {}
    return {}


def _decode_search_post_payload(response: Optional[Any]) -> dict:
    return _decode_search_post_data(_get_response_post_data(response))


def _response_preserves_publish_sort(
    final_response: Optional[Any],
    publish_response: Optional[Any],
) -> bool:
    publish_payload = _decode_search_post_payload(publish_response)
    final_payload = _decode_search_post_payload(final_response)
    publish_sort_field = str(publish_payload.get("sortField") or "").strip()
    publish_sort_value = str(publish_payload.get("sortValue") or "").strip()
    if not publish_sort_field and not publish_sort_value:
        return False
    return (
        str(final_payload.get("sortField") or "").strip() == publish_sort_field
        and str(final_payload.get("sortValue") or "").strip() == publish_sort_value
    )


def _search_request_template_is_trusted(
    template: Optional[SearchRequestTemplate],
    task_config: dict,
) -> bool:
    """校验复用模板仍对应当前任务，且保留“新发布=最新”的排序参数。"""
    if template is None:
        return False
    if template.filter_signature != _build_task_filter_signature(task_config):
        return False
    if template.new_publish_option != _normalize_new_publish_option(task_config):
        return False
    payload = _decode_search_post_data(template.post_data)
    if template.new_publish_option == "最新":
        return (
            str(payload.get("sortField") or "").strip() == "create"
            and str(payload.get("sortValue") or "").strip() == "desc"
        )
    return bool(payload.get("fromFilter"))


def _can_replay_search_request(
    session: Optional[ReusableSearchSession],
    *,
    task_config: dict,
) -> bool:
    """判断复用轮是否可以跳过 DOM，直接重放上轮已确认筛选接口请求。"""
    if session is None or session.last_success_at is None:
        return False
    return _search_request_template_is_trusted(
        session.search_request_template,
        task_config,
    )


def _sanitize_search_request_headers(headers: Optional[dict]) -> dict:
    """保留 replay 所需的普通请求头，去掉容易过期或由浏览器上下文托管的头。"""
    if not isinstance(headers, dict):
        return {}
    excluded = {
        "cookie",
        "content-length",
        "host",
        "origin",
        "referer",
        "connection",
    }
    return {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).lower() not in excluded and value is not None
    }


async def _build_search_request_template_from_response(
    *,
    response: Any,
    task_config: dict,
    new_publish_option: str,
) -> SearchRequestTemplate:
    """从已确认用于解析的搜索响应中提取可复用的接口请求模板。"""
    request = getattr(response, "request", None)
    headers = {}
    if request is not None:
        all_headers = getattr(request, "all_headers", None)
        if callable(all_headers):
            try:
                headers = await all_headers()
            except Exception:
                headers = getattr(request, "headers", {}) or {}
        else:
            headers = getattr(request, "headers", {}) or {}
    return SearchRequestTemplate(
        url=str(getattr(response, "url", "") or ""),
        post_data=_get_response_post_data(response),
        headers=_sanitize_search_request_headers(headers),
        filter_signature=_build_task_filter_signature(task_config),
        new_publish_option=new_publish_option,
    )


class _ReplaySearchRequest:
    """让 API replay 响应保留 request.post_data，供诊断和筛选校验复用。"""

    def __init__(self, post_data: str, headers: dict):
        self.post_data = post_data
        self.headers = headers


class _ReplaySearchResponse:
    """包装 Playwright APIResponse，使其兼容现有搜索结果解析流程。"""

    def __init__(self, api_response: Any, template: SearchRequestTemplate):
        self._api_response = api_response
        self._json_cache = None
        self.ok = bool(getattr(api_response, "ok", False))
        self.status = getattr(api_response, "status", None)
        self.url = str(getattr(api_response, "url", "") or template.url)
        self.request = _ReplaySearchRequest(template.post_data, template.headers)

    async def json(self) -> Any:
        if self._json_cache is None:
            self._json_cache = await self._api_response.json()
        return self._json_cache


async def _replay_search_request_from_session(
    *,
    page: Any,
    session: ReusableSearchSession,
    task_config: dict,
    timeout_ms: int = 20000,
) -> _ReplaySearchResponse:
    """使用当前浏览器上下文重放已确认筛选请求，避免复用轮再依赖页面 DOM。"""
    template = session.search_request_template
    if not _search_request_template_is_trusted(template, task_config):
        raise SearchRequestReplayError("筛选请求模板不存在或与当前任务配置不匹配")
    request_context = getattr(getattr(page, "context", None), "request", None)
    if request_context is None:
        raise SearchRequestReplayError("当前页面缺少可用的 API 请求上下文")
    try:
        api_response = await request_context.post(
            template.url,
            data=template.post_data,
            headers=template.headers,
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError as exc:
        raise SearchRequestReplayError("复用筛选接口请求超时") from exc
    except Exception as exc:
        raise SearchRequestReplayError(f"复用筛选接口请求失败: {exc}") from exc
    replay_response = _ReplaySearchResponse(api_response, template)
    if not replay_response.ok:
        raise SearchRequestReplayError(
            f"复用筛选接口返回异常状态: {replay_response.status}"
        )
    try:
        await replay_response.json()
    except Exception as exc:
        raise SearchRequestReplayError(f"复用筛选接口返回非 JSON 数据: {exc}") from exc
    return replay_response


def _search_response_stage_for_log(
    *,
    selected_response: Optional[Any],
    initial_response: Optional[Any],
    publish_response: Optional[Any],
    final_response: Optional[Any],
) -> str:
    if selected_response is None:
        return "none"
    if selected_response is publish_response:
        return "new_publish"
    if selected_response is final_response:
        return "final"
    if selected_response is initial_response:
        return "initial"
    return "unknown"


def _summarize_response_post_data(response: Optional[Any], max_length: int = 500) -> str:
    if response is None:
        return "-"
    request = getattr(response, "request", None)
    post_data = getattr(request, "post_data", "") if request is not None else ""
    if callable(post_data):
        try:
            post_data = post_data()
        except Exception:
            post_data = ""
    if isinstance(post_data, bytes):
        post_data = post_data.decode("utf-8", errors="replace")
    post_data = " ".join(str(post_data or "").split())
    if len(post_data) > max_length:
        return post_data[:max_length] + "...(truncated)"
    return post_data or "-"


def _log_search_response_trace(
    stage: str,
    response: Optional[Any],
    logger: Any = log_time,
) -> None:
    """记录搜索接口响应的阶段化诊断信息，方便确认最终解析来源。"""
    if response is None:
        logger(f"搜索响应诊断[{stage}]: 无响应")
        return
    response_url = getattr(response, "url", "")
    response_ok = getattr(response, "ok", None)
    post_summary = _summarize_response_post_data(response)
    logger(
        f"搜索响应诊断[{stage}]: ok={response_ok}, "
        f"url={response_url}, post={post_summary}"
    )


def _select_latest_ok_search_response(
    first_response: Optional[Any],
    captured_responses: list[Any],
) -> Optional[Any]:
    """从同一次筛选动作捕获到的响应中选择最后一个成功结果，避免旧请求抢先返回。"""
    for response in reversed(captured_responses):
        if getattr(response, "ok", False):
            return response
    return first_response


def _has_live_page(session: Optional[ReusableSearchSession]) -> bool:
    page = session.page if session is not None else None
    if page is None:
        return False
    is_closed = getattr(page, "is_closed", None)
    if callable(is_closed):
        try:
            return not bool(is_closed())
        except Exception:
            return False
    return True


async def _open_new_publish_filter(page: Any) -> None:
    """打开“新发布”筛选弹层，优先使用精确文本避免点到相似入口。"""
    trigger = page.get_by_text("新发布", exact=True).first
    try:
        if await trigger.count():
            await trigger.click()
            return
        await page.click("text=新发布")
    except PlaywrightTimeoutError as e:
        if await _is_login_modal_visible(page):
            raise LoginRequiredError(
                "Login required: login modal blocks new publish filter click"
            ) from e
        raise NewPublishPopupNotFoundError("新发布筛选入口未找到") from e


async def _is_login_modal_visible(page: Any) -> bool:
    """识别闲鱼页面层登录弹窗，避免把登录态失效误报成筛选入口缺失。"""
    login_modal = page.locator(
        ".login-modal-wrap, div[class*='login-modal-wrap'], "
        ".ant-modal-wrap[class*='login-modal-wrap']"
    ).first
    try:
        return bool(await login_modal.count()) and bool(await login_modal.is_visible())
    except Exception:
        return False


async def _first_visible_locator(locator: Any, max_candidates: int = 20) -> Optional[Any]:
    """从候选定位器中挑选第一个可见节点，兼容弹层类名变化后的兜底查找。"""
    count = await locator.count()
    for index in range(min(count, max_candidates)):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


async def _mark_new_publish_menu_by_content(
    page: Any,
    required_option: str,
    logger: Any = log_time,
) -> Optional[dict]:
    """按发布时间选项组识别菜单容器，避免依赖闲鱼频繁变化的弹层类名。"""
    marker = NEW_PUBLISH_MENU_MARKER
    options = list(NEW_PUBLISH_OPTIONS)
    menu_info = await page.evaluate(
        """
        ({ marker, options, requiredOption }) => {
            const normalize = (value) => String(value || '').replace(/\\s+/g, '');
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && style.opacity !== '0'
                    && rect.width > 0
                    && rect.height > 0;
            };

            document.querySelectorAll(`[${marker}]`).forEach((element) => {
                element.removeAttribute(marker);
            });

            const candidates = Array.from(document.querySelectorAll('body *'))
                .filter(isVisible)
                .map((element) => {
                    const text = normalize(element.innerText || element.textContent || '');
                    const matchedOptions = options.filter((option) => text.includes(option));
                    const rect = element.getBoundingClientRect();
                    return {
                        element,
                        text,
                        matchedOptions,
                        area: rect.width * rect.height,
                    };
                })
                .filter((candidate) => {
                    return candidate.matchedOptions.length >= 2
                        && candidate.matchedOptions.includes(requiredOption);
                });

            if (!candidates.length) {
                return null;
            }

            candidates.sort((left, right) => {
                if (left.text.length !== right.text.length) {
                    return left.text.length - right.text.length;
                }
                if (left.area !== right.area) {
                    return left.area - right.area;
                }
                return right.matchedOptions.length - left.matchedOptions.length;
            });

            const selected = candidates[0];
            selected.element.setAttribute(marker, '1');
            return {
                matched_options: selected.matchedOptions,
                text: selected.text.slice(0, 120),
            };
        }
        """,
        {
            "marker": marker,
            "options": options,
            "requiredOption": required_option,
        },
    )
    if menu_info:
        logger(
            "新发布筛选菜单识别: "
            f"options={menu_info.get('matched_options')}, "
            f"text={menu_info.get('text')}"
        )
    return menu_info


async def _find_new_publish_menu_container(
    page: Any,
    required_option: str,
) -> Optional[Any]:
    """查找包含发布时间选项组的菜单容器，先用固定弹层，再用内容识别兜底。"""
    fixed_popup = page.locator(NEW_PUBLISH_POPUP_SELECTOR).filter(
        has=page.get_by_text(required_option, exact=True)
    ).last
    if await fixed_popup.count():
        try:
            await fixed_popup.wait_for(state="visible", timeout=5000)
            return fixed_popup
        except PlaywrightTimeoutError:
            pass

    menu_info = await _mark_new_publish_menu_by_content(page, required_option)
    if not menu_info:
        return None

    content_menu = page.locator(f"[{NEW_PUBLISH_MENU_MARKER}='1']").first
    if await content_menu.count():
        return content_menu
    return None


async def _find_new_publish_option_in_open_filter(
    page: Any,
    new_publish_option: str,
) -> Any:
    """定位新发布筛选选项，区分弹层缺失和选项缺失两类页面状态。"""
    option_text = str(new_publish_option).strip()
    if option_text not in NEW_PUBLISH_OPTIONS:
        raise NewPublishOptionNotFoundError(f"新发布选项 '{option_text}' 未找到")

    menu_container = await _find_new_publish_menu_container(page, option_text)
    if menu_container is None:
        raise NewPublishPopupNotFoundError("新发布筛选弹层未出现")
    option = menu_container.get_by_text(option_text, exact=True)
    visible_option = await _first_visible_locator(option)
    if visible_option is not None:
        return visible_option
    raise NewPublishOptionNotFoundError(f"新发布选项 '{option_text}' 未找到")


async def _click_new_publish_option_in_open_filter(
    page: Any,
    new_publish_option: str,
) -> None:
    """只点击已确认可见的发布时间范围选项，避免误报接口超时。"""
    option = await _find_new_publish_option_in_open_filter(
        page,
        new_publish_option,
    )
    await option.click()


async def _capture_search_response_after_action(
    *,
    page: Any,
    action: Any,
    timeout_ms: int,
    settle_min_seconds: float,
    settle_max_seconds: float,
) -> Optional[Any]:
    """执行筛选动作并收集其后的搜索响应，返回页面稳定前最后一个成功响应。"""
    captured_responses: list[Any] = []

    def _capture_response(response: Any) -> None:
        if is_search_results_response(response):
            captured_responses.append(response)

    page.on("response", _capture_response)
    try:
        async with page.expect_response(
            is_search_results_response,
            timeout=timeout_ms,
        ) as response_info:
            await action()
        first_response = await response_info.value
        await random_sleep(settle_min_seconds, settle_max_seconds)
        return _select_latest_ok_search_response(first_response, captured_responses)
    finally:
        remove_listener = getattr(page, "remove_listener", None)
        if callable(remove_listener):
            remove_listener("response", _capture_response)
        else:
            off = getattr(page, "off", None)
            if callable(off):
                off("response", _capture_response)


def _is_login_url(url: str) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return "passport.goofish.com" in lowered or "mini_login" in lowered


def _is_navigation_aborted_error(error: Exception) -> bool:
    return "net::ERR_ABORTED" in str(error)


def _build_search_list_result_record(
    *, item_data: dict, keyword: str, task_name: str, scraped_at: Optional[str] = None
) -> dict:
    return {
        "爬取时间": scraped_at or datetime.now().isoformat(),
        "搜索关键字": keyword,
        "任务名称": task_name,
        "商品信息": item_data,
        "卖家信息": {},
        "ai_analysis": {
            "analysis_source": "quick_notify",
            "is_recommended": True,
            "reason": QUICK_NOTIFY_REASON,
            "keyword_hit_count": 0,
        },
    }


async def _is_locator_visible(locator, timeout_ms: int) -> bool:
    try:
        await locator.wait_for(state="visible", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


async def _raise_if_security_challenge(page, keyword: str, timeout_ms: int = 2000) -> None:
    baxia_dialog = page.locator("div.baxia-dialog-mask")
    if await _is_locator_visible(baxia_dialog, timeout_ms):
        print("\n==================== CRITICAL BLOCK DETECTED ====================")
        print("检测到闲鱼反爬虫验证弹窗 (baxia-dialog)，无法继续操作。")
        print("这通常是因为操作过于频繁或被识别为机器人。")
        print("建议：")
        print("1. 停止脚本一段时间再试。")
        print(
            "2. (推荐) 在 .env 文件中设置 RUN_HEADLESS=false，"
            "以非无头模式运行，这有助于绕过检测。"
        )
        print(f"任务 '{keyword}' 将在此处中止。")
        print("===================================================================")
        raise RiskControlError("baxia-dialog")

    middleware_widget = page.locator("div.J_MIDDLEWARE_FRAME_WIDGET")
    if await _is_locator_visible(middleware_widget, timeout_ms):
        print("\n==================== CRITICAL BLOCK DETECTED ====================")
        print("检测到闲鱼反爬虫验证弹窗 (J_MIDDLEWARE_FRAME_WIDGET)，无法继续操作。")
        print("这通常是因为操作过于频繁或被识别为机器人。")
        print("建议：")
        print("1. 停止脚本一段时间再试。")
        print("2. (推荐) 更新登录状态文件，确保登录状态有效。")
        print("3. 降低任务执行频率，避免被识别为机器人。")
        print(f"任务 '{keyword}' 将在此处中止。")
        print("===================================================================")
        raise RiskControlError("J_MIDDLEWARE_FRAME_WIDGET")


def _resolve_browser_channel() -> str:
    global EDGE_DOCKER_WARNING_PRINTED
    if RUNNING_IN_DOCKER:
        if LOGIN_IS_EDGE and not EDGE_DOCKER_WARNING_PRINTED:
            print(
                "检测到 LOGIN_IS_EDGE=true，但 Docker 镜像未内置 Edge，"
                "任务运行时将改用 Chromium。"
            )
            EDGE_DOCKER_WARNING_PRINTED = True
        return "chromium"
    return "msedge" if LOGIN_IS_EDGE else "chrome"


def _build_browser_env_without_global_proxy() -> dict:
    browser_env = dict(os.environ)
    for key in PROXY_ENV_KEYS:
        browser_env.pop(key, None)
    return browser_env


def _format_failure_reason(reason: str, limit: int = 500) -> str:
    if not reason:
        return "未知错误"
    cleaned = " ".join(str(reason).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


async def _notify_task_failure(
    task_config: dict, reason: str, *, cookie_path: Optional[str]
) -> None:
    task_name = task_config.get("task_name", "未命名任务")
    keyword = task_config.get("keyword", "")
    formatted_reason = _format_failure_reason(reason)

    # Some failures are deterministic misconfiguration and should pause/notify immediately.
    pause_immediately = any(
        marker in formatted_reason
        for marker in (
            "未找到可用的代理地址",
            "未找到可用的登录状态文件",
        )
    )

    guard_result = FAILURE_GUARD.record_failure(
        task_name,
        formatted_reason,
        cookie_path=cookie_path,
        min_failures_to_pause=1 if pause_immediately else None,
    )

    if not guard_result.get("should_notify"):
        print(
            f"[FailureGuard] 任务 '{task_name}' 失败计数 {guard_result.get('consecutive_failures')}/{FAILURE_GUARD.threshold}，暂不通知。"
        )
        return

    paused_until = guard_result.get("paused_until")
    paused_until_str = (
        paused_until.strftime("%Y-%m-%d %H:%M:%S") if paused_until else "N/A"
    )

    product_data = {
        "商品标题": f"[任务异常] {task_name}",
        "当前售价": "N/A",
        "商品链接": "#",
    }
    notify_reason = (
        f"任务运行失败(已连续 {guard_result.get('consecutive_failures')}/{FAILURE_GUARD.threshold} 次): {formatted_reason}"
        f"\n任务: {task_name}"
        f"\n关键词: {keyword or 'N/A'}"
        f"\n已自动暂停重试，暂停到: {paused_until_str}"
        f"\n修复后(更新登录态/cookies文件)将自动恢复。"
    )

    try:
        await send_ntfy_notification(product_data, notify_reason)
    except Exception as e:
        print(f"发送任务异常通知失败: {e}")


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_rotation_settings(task_config: dict) -> dict:
    account_cfg = task_config.get("account_rotation") or {}
    proxy_cfg = task_config.get("proxy_rotation") or {}

    account_enabled = _as_bool(
        account_cfg.get("enabled"),
        _as_bool(os.getenv("ACCOUNT_ROTATION_ENABLED"), False),
    )
    account_mode = (
        account_cfg.get("mode") or os.getenv("ACCOUNT_ROTATION_MODE", "per_task")
    ).lower()
    account_state_dir = account_cfg.get("state_dir") or os.getenv(
        "ACCOUNT_STATE_DIR", "state"
    )
    account_retry_limit = _as_int(
        account_cfg.get("retry_limit"),
        _as_int(os.getenv("ACCOUNT_ROTATION_RETRY_LIMIT"), 2),
    )
    account_blacklist_ttl = _as_int(
        account_cfg.get("blacklist_ttl_sec"),
        _as_int(os.getenv("ACCOUNT_BLACKLIST_TTL"), 300),
    )

    proxy_enabled = _as_bool(
        proxy_cfg.get("enabled"), _as_bool(os.getenv("PROXY_ROTATION_ENABLED"), False)
    )
    proxy_mode = (
        proxy_cfg.get("mode") or os.getenv("PROXY_ROTATION_MODE", "per_task")
    ).lower()
    proxy_pool = proxy_cfg.get("proxy_pool") or os.getenv("PROXY_POOL", "")
    proxy_retry_limit = _as_int(
        proxy_cfg.get("retry_limit"),
        _as_int(os.getenv("PROXY_ROTATION_RETRY_LIMIT"), 2),
    )
    proxy_blacklist_ttl = _as_int(
        proxy_cfg.get("blacklist_ttl_sec"),
        _as_int(os.getenv("PROXY_BLACKLIST_TTL"), 300),
    )

    return {
        "account_enabled": account_enabled,
        "account_mode": account_mode,
        "account_state_dir": account_state_dir,
        "account_retry_limit": max(1, account_retry_limit),
        "account_blacklist_ttl": max(0, account_blacklist_ttl),
        "proxy_enabled": proxy_enabled,
        "proxy_mode": proxy_mode,
        "proxy_pool": proxy_pool,
        "proxy_retry_limit": max(1, proxy_retry_limit),
        "proxy_blacklist_ttl": max(0, proxy_blacklist_ttl),
    }


def _get_item_snapshot_keys(item: dict) -> set[str]:
    keys = set()
    item_id = str(item.get("商品ID") or "").strip()
    link = str(item.get("商品链接") or "").strip()
    if item_id:
        keys.add(item_id)
    if link:
        keys.add(link)
        keys.add(get_link_unique_key(link))
    return keys


def _find_latest_price_snapshot(item: dict, snapshots: list[dict]) -> Optional[dict]:
    keys = _get_item_snapshot_keys(item)
    if not keys:
        return None
    for snapshot in reversed(snapshots or []):
        snapshot_keys = {
            str(snapshot.get("item_id") or "").strip(),
            str(snapshot.get("link") or "").strip(),
        }
        snapshot_keys = {key for key in snapshot_keys if key}
        snapshot_keys.update(get_link_unique_key(key) for key in list(snapshot_keys))
        if keys & snapshot_keys:
            return snapshot
    return None


def _build_duplicate_drop_reason(
    *,
    previous_price: float,
    current_price: float,
    matched_keywords: list[str],
) -> str:
    drop_amount = round(previous_price - current_price, 2)
    drop_percent = (
        round(drop_amount / previous_price * 100, 2)
        if previous_price > 0
        else None
    )
    percent_text = f"，降幅 {drop_percent:g}%" if drop_percent is not None else ""
    return (
        f"重复商品降价：¥{previous_price:g} -> ¥{current_price:g}"
        f"，降低 ¥{drop_amount:g}{percent_text}；"
        f"命中关键词：{', '.join(matched_keywords)}"
    )


async def _notify_duplicate_price_drop_if_needed(
    *,
    item_data: dict,
    previous_snapshots: list[dict],
    keyword_rules: list[str],
    keyword_alert_rules: list[dict],
    decision_mode: str,
) -> bool:
    if decision_mode != "keyword":
        return False

    current_price = parse_price_value(item_data.get("当前售价"))
    if current_price is None:
        return False
    previous_snapshot = _find_latest_price_snapshot(item_data, previous_snapshots)
    if not previous_snapshot:
        return False
    previous_price = parse_price_value(previous_snapshot.get("price"))
    if previous_price is None or current_price >= previous_price:
        return False

    match_keywords = (
        extract_keywords_from_alert_rules(keyword_alert_rules)
        or list(keyword_rules or [])
    )
    analysis = evaluate_keyword_rules(
        match_keywords,
        build_search_text({"商品信息": item_data}),
    )
    if not analysis.get("is_recommended"):
        return False

    await send_ntfy_notification(
        item_data,
        _build_duplicate_drop_reason(
            previous_price=previous_price,
            current_price=current_price,
            matched_keywords=analysis.get("matched_keywords") or [],
        ),
    )
    return True


def _default_context_options() -> dict:
    return {
        "user_agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "permissions": ["geolocation"],
        "geolocation": {"longitude": 121.4737, "latitude": 31.2304},
        "color_scheme": "light",
    }


def _clean_kwargs(options: dict) -> dict:
    return {k: v for k, v in options.items() if v is not None}


def _looks_like_mobile(ua: str) -> Optional[bool]:
    if not ua:
        return None
    ua_lower = ua.lower()
    if "mobile" in ua_lower or "android" in ua_lower or "iphone" in ua_lower:
        return True
    if "windows" in ua_lower or "macintosh" in ua_lower:
        return False
    return None


def _build_context_overrides(snapshot: dict) -> dict:
    env = snapshot.get("env") or {}
    headers = snapshot.get("headers") or {}
    navigator = env.get("navigator") or {}
    screen = env.get("screen") or {}
    intl = env.get("intl") or {}

    overrides = {}

    ua = (
        headers.get("User-Agent")
        or headers.get("user-agent")
        or navigator.get("userAgent")
    )
    if ua:
        overrides["user_agent"] = ua

    accept_language = headers.get("Accept-Language") or headers.get("accept-language")
    locale = None
    if accept_language:
        locale = accept_language.split(",")[0].strip()
    elif navigator.get("language"):
        locale = navigator["language"]
    if locale:
        overrides["locale"] = locale

    tz = intl.get("timeZone")
    if tz:
        overrides["timezone_id"] = tz

    width = screen.get("width")
    height = screen.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        overrides["viewport"] = {"width": int(width), "height": int(height)}

    dpr = screen.get("devicePixelRatio")
    if isinstance(dpr, (int, float)):
        overrides["device_scale_factor"] = float(dpr)

    touch_points = navigator.get("maxTouchPoints")
    if isinstance(touch_points, (int, float)):
        overrides["has_touch"] = touch_points > 0

    mobile_flag = _looks_like_mobile(ua or "")
    if mobile_flag is not None:
        overrides["is_mobile"] = mobile_flag

    return _clean_kwargs(overrides)


def _build_extra_headers(raw_headers: Optional[dict]) -> dict:
    if not raw_headers:
        return {}
    allowed = {"accept-language"}
    headers = {}
    for key, value in raw_headers.items():
        normalized_key = str(key).strip().lower() if key else ""
        if normalized_key not in allowed or value is None:
            continue
        headers[key] = value
    return headers


async def scrape_user_profile(context, user_id: str) -> dict:
    """
    【新版】访问指定用户的个人主页，按顺序采集其摘要信息、完整的商品列表和完整的评价列表。
    """
    print(f"   -> 开始采集用户ID: {user_id} 的完整信息...")
    profile_data = {}
    page = await context.new_page()

    # 为各项异步任务准备Future和数据容器
    head_api_future = asyncio.get_event_loop().create_future()

    all_items, all_ratings = [], []
    stop_item_scrolling, stop_rating_scrolling = asyncio.Event(), asyncio.Event()

    async def handle_response(response: Response):
        # 捕获头部摘要API
        if (
            "mtop.idle.web.user.page.head" in response.url
            and not head_api_future.done()
        ):
            try:
                head_api_future.set_result(await response.json())
                print(f"      [API捕获] 用户头部信息... 成功")
            except Exception as e:
                if not head_api_future.done():
                    head_api_future.set_exception(e)

        # 捕获商品列表API
        elif "mtop.idle.web.xyh.item.list" in response.url:
            try:
                data = await response.json()
                all_items.extend(data.get("data", {}).get("cardList", []))
                print(f"      [API捕获] 商品列表... 当前已捕获 {len(all_items)} 件")
                if not data.get("data", {}).get("nextPage", True):
                    stop_item_scrolling.set()
            except Exception as e:
                stop_item_scrolling.set()

        # 捕获评价列表API
        elif "mtop.idle.web.trade.rate.list" in response.url:
            try:
                data = await response.json()
                all_ratings.extend(data.get("data", {}).get("cardList", []))
                print(f"      [API捕获] 评价列表... 当前已捕获 {len(all_ratings)} 条")
                if not data.get("data", {}).get("nextPage", True):
                    stop_rating_scrolling.set()
            except Exception as e:
                stop_rating_scrolling.set()

    page.on("response", handle_response)

    try:
        # --- 任务1: 导航并采集头部信息 ---
        await page.goto(
            f"https://www.goofish.com/personal?userId={user_id}",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        head_data = await asyncio.wait_for(head_api_future, timeout=15)
        profile_data = await parse_user_head_data(head_data)

        # --- 任务2: 滚动加载所有商品 (默认页面) ---
        print("      [采集阶段] 开始采集该用户的商品列表...")
        await random_sleep(2, 4)  # 等待第一页商品API完成
        while not stop_item_scrolling.is_set():
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            try:
                await asyncio.wait_for(stop_item_scrolling.wait(), timeout=8)
            except asyncio.TimeoutError:
                print("      [滚动超时] 商品列表可能已加载完毕。")
                break
        profile_data["卖家发布的商品列表"] = await _parse_user_items_data(all_items)

        # --- 任务3: 点击并采集所有评价 ---
        print("      [采集阶段] 开始采集该用户的评价列表...")
        rating_tab_locator = page.locator("//div[text()='信用及评价']/ancestor::li")
        if await rating_tab_locator.count() > 0:
            await rating_tab_locator.click()
            await random_sleep(3, 5)  # 等待第一页评价API完成

            while not stop_rating_scrolling.is_set():
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                try:
                    await asyncio.wait_for(stop_rating_scrolling.wait(), timeout=8)
                except asyncio.TimeoutError:
                    print("      [滚动超时] 评价列表可能已加载完毕。")
                    break

            profile_data["卖家收到的评价列表"] = await parse_ratings_data(all_ratings)
            reputation_stats = await calculate_reputation_from_ratings(all_ratings)
            profile_data.update(reputation_stats)
        else:
            print("      [警告] 未找到评价选项卡，跳过评价采集。")

    except Exception as e:
        print(f"   [错误] 采集用户 {user_id} 信息时发生错误: {e}")
    finally:
        page.remove_listener("response", handle_response)
        await page.close()
        print(f"   -> 用户 {user_id} 信息采集完成。")

    return profile_data


async def scrape_xianyu(
    task_config: dict,
    debug_limit: int = 0,
    reusable_session: Optional[ReusableSearchSession] = None,
):
    """
    【核心执行器】
    根据单个任务配置，异步爬取闲鱼商品数据，并对每个新发现的商品进行实时的、独立的AI分析和通知。
    """
    keyword = task_config["keyword"]
    max_pages = task_config.get("max_pages", 1)
    personal_only = task_config.get("personal_only", False)
    min_price = task_config.get("min_price")
    max_price = task_config.get("max_price")
    decision_mode = str(task_config.get("decision_mode", "ai")).strip().lower()
    if decision_mode not in {"ai", "keyword"}:
        decision_mode = "ai"
    keyword_rules = task_config.get("keyword_rules") or []
    keyword_alert_rules = task_config.get("keyword_alert_rules") or []
    if not keyword_alert_rules and keyword_rules:
        keyword_alert_rules = [
            {"keyword": keyword_rule, "max_price": None}
            for keyword_rule in keyword_rules
        ]
    free_shipping = task_config.get("free_shipping", False)
    new_publish_option = _normalize_new_publish_option(task_config)
    is_latest_mode = new_publish_option == "最新"
    region_filter = (task_config.get("region") or "").strip()

    processed_links = set()
    history_run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    history_seen_item_ids: set[str] = set()
    historical_snapshots = load_price_snapshots(keyword)
    result_filename = build_result_filename(keyword)
    processed_links = load_processed_link_keys(keyword)
    if processed_links:
        print(f"LOG: 发现已存在结果集 {result_filename}，已加载 {len(processed_links)} 个历史商品用于去重。")
    else:
        print(f"LOG: 结果集 {result_filename} 当前为空，将写入新记录。")

    rotation_settings = _get_rotation_settings(task_config)
    account_items = load_state_files(rotation_settings["account_state_dir"])
    runtime_plan = resolve_account_runtime_plan(
        strategy=task_config.get("account_strategy"),
        account_state_file=task_config.get("account_state_file"),
        has_root_state_file=os.path.exists(STATE_FILE),
        available_account_files=account_items,
    )
    forced_account = runtime_plan["forced_account"]
    if runtime_plan["prefer_root_state"]:
        account_items = [STATE_FILE]
        rotation_settings["account_enabled"] = False
    elif runtime_plan["use_account_pool"]:
        rotation_settings["account_enabled"] = True
    else:
        rotation_settings["account_enabled"] = False

    account_pool = RotationPool(
        account_items, rotation_settings["account_blacklist_ttl"], "account"
    )
    proxy_pool = RotationPool(
        parse_proxy_pool(rotation_settings["proxy_pool"]),
        rotation_settings["proxy_blacklist_ttl"],
        "proxy",
    )

    selected_account: Optional[RotationItem] = None
    selected_proxy: Optional[RotationItem] = None

    def _select_account(force_new: bool = False) -> Optional[RotationItem]:
        nonlocal selected_account
        if forced_account:
            return RotationItem(value=forced_account)
        if (
            reusable_session is not None
            and reusable_session.state_file
            and not force_new
        ):
            return RotationItem(value=reusable_session.state_file)
        if not rotation_settings["account_enabled"]:
            if os.path.exists(STATE_FILE):
                return RotationItem(value=STATE_FILE)
            return None
        if (
            rotation_settings["account_mode"] == "per_task"
            and selected_account
            and not force_new
        ):
            return selected_account
        picked = account_pool.pick_random()
        return picked or selected_account

    def _select_proxy(force_new: bool = False) -> Optional[RotationItem]:
        nonlocal selected_proxy
        if not rotation_settings["proxy_enabled"]:
            return None
        if (
            reusable_session is not None
            and reusable_session.proxy_server
            and not force_new
        ):
            return RotationItem(value=reusable_session.proxy_server)
        if (
            rotation_settings["proxy_mode"] == "per_task"
            and selected_proxy
            and not force_new
        ):
            return selected_proxy
        picked = proxy_pool.pick_random()
        return picked or selected_proxy

    async def _run_scrape_attempt(state_file: str, proxy_server: Optional[str]) -> int:
        processed_item_count = 0
        stop_scraping = False

        if not os.path.exists(state_file):
            raise FileNotFoundError(f"登录状态文件不存在: {state_file}")

        snapshot_data = None
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                snapshot_data = json.load(f)
        except Exception as e:
            print(f"警告：读取登录状态文件失败，将直接按路径使用: {e}")

        session_has_live_page = _has_live_page(reusable_session)
        session_can_keep_browser = (
            reusable_session is not None
            and session_has_live_page
            and reusable_session.state_file == state_file
            and reusable_session.proxy_server == proxy_server
        )
        keep_session_after_success = reusable_session is not None
        attempt_success = False
        close_session_reason = "本轮未完成筛选同步"
        page = None
        context = None
        browser = None
        playwright_manager = None

        if session_can_keep_browser:
            page = reusable_session.page
            context = reusable_session.context
            browser = reusable_session.browser
            playwright_manager = reusable_session.playwright
        else:
            if (
                reusable_session is not None
                and (
                    reusable_session.page is not None
                    or reusable_session.context is not None
                    or reusable_session.browser is not None
                    or reusable_session.playwright is not None
                )
            ):
                await reusable_session.close()

            playwright_manager = await async_playwright().start()
            # 反检测启动参数
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ]

            launch_kwargs = {
                "headless": RUN_HEADLESS,
                "args": launch_args,
                "env": _build_browser_env_without_global_proxy(),
            }
            if proxy_server:
                launch_kwargs["proxy"] = {"server": proxy_server}

            launch_kwargs["channel"] = _resolve_browser_channel()

            browser = await playwright_manager.chromium.launch(**launch_kwargs)

            context_kwargs = _default_context_options()
            storage_state_arg = state_file

            if isinstance(snapshot_data, dict):
                # 新版扩展导出的增强快照，包含环境和Header
                if any(
                    key in snapshot_data
                    for key in ("env", "headers", "page", "storage")
                ):
                    print(f"检测到增强浏览器快照，应用环境参数: {state_file}")
                    storage_state_arg = {"cookies": snapshot_data.get("cookies", [])}
                    context_kwargs.update(_build_context_overrides(snapshot_data))
                    extra_headers = _build_extra_headers(snapshot_data.get("headers"))
                    if extra_headers:
                        context_kwargs["extra_http_headers"] = extra_headers
                else:
                    storage_state_arg = snapshot_data

            context_kwargs = _clean_kwargs(context_kwargs)
            context = await browser.new_context(
                storage_state=storage_state_arg, **context_kwargs
            )

            # 增强反检测脚本（模拟真实移动设备）
            await context.add_init_script("""
                // 移除webdriver标识
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

                // 模拟真实移动设备的navigator属性
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en-US', 'en']});

                // 添加chrome对象
                window.chrome = {runtime: {}, loadTimes: function() {}, csi: function() {}};

                // 模拟触摸支持
                Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 5});

                // 覆盖permissions查询（避免暴露自动化）
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({state: Notification.permission}) :
                        originalQuery(parameters)
                );
            """)

            page = await context.new_page()

        if reusable_session is not None:
            reusable_session.state_file = state_file
            reusable_session.proxy_server = proxy_server
            reusable_session.playwright = playwright_manager
            reusable_session.browser = browser
            reusable_session.context = context
            reusable_session.page = page

        replay_confirmed_search_request = (
            session_can_keep_browser
            and _can_replay_search_request(
                reusable_session,
                task_config=task_config,
            )
        )

        try:
            if (
                session_can_keep_browser
                and new_publish_option
                and not replay_confirmed_search_request
            ):
                close_session_reason = "复用筛选请求模板不存在或与当前配置不匹配，关闭复用会话"
                log_time(
                    "复用筛选请求模板不存在或与当前任务配置不匹配，"
                    "本轮跳过并关闭复用会话，下轮从步骤 0 冷启动。"
                )
                return 0

            if not session_can_keep_browser:
                # 步骤 0 - 模拟真实用户：先访问首页（重要的反检测措施）
                log_time("步骤 0 - 模拟真实用户访问首页...")
                await page.goto(
                    "https://www.goofish.com/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                log_time("[反爬] 在首页停留，模拟浏览...")
                await random_sleep(1, 2)

                # 模拟随机滚动（移动设备的触摸滚动）
                await page.evaluate(
                    "window.scrollBy(0, Math.random() * 500 + 200)"
                )
                await random_sleep(1, 2)
            else:
                if replay_confirmed_search_request:
                    log_time("复用浏览器跳过首页，直接使用上轮筛选接口模板查询...")
                else:
                    log_time("复用浏览器跳过首页，重新查询并应用筛选...")

            # 使用 'q' 参数构建正确的搜索URL，并进行URL编码
            params = {"q": keyword}
            search_url = f"https://www.goofish.com/search?{urlencode(params)}"

            initial_response = None
            final_response = None
            new_publish_response = None
            if replay_confirmed_search_request:
                log_time("步骤 1 - 复用筛选接口模板，跳过搜索页导航和 DOM 筛选。")
                log_time(f"当前页面URL: {page.url}")
                try:
                    new_publish_response = await _replay_search_request_from_session(
                        page=page,
                        session=reusable_session,
                        task_config=task_config,
                        timeout_ms=20000,
                    )
                    final_response = new_publish_response
                    _log_search_response_trace(
                        "new_publish:replay",
                        new_publish_response,
                    )
                except SearchRequestReplayError as e:
                    close_session_reason = f"{e}，关闭复用会话"
                    log_time(f"{e}，本轮跳过并关闭复用会话，下轮从步骤 0 冷启动。")
                    return 0
            else:
                log_time("步骤 1 - 导航到搜索结果页...")
                log_time(f"目标URL: {search_url}")
                for retry_index in range(INITIAL_SEARCH_RESPONSE_RETRY_COUNT):
                    try:
                        # 先监听搜索接口响应，再执行导航，避免错过首次请求
                        async with page.expect_response(
                            is_search_results_response,
                            timeout=INITIAL_SEARCH_RESPONSE_TIMEOUT_MS,
                        ) as initial_response_info:
                            await page.goto(
                                search_url,
                                wait_until="domcontentloaded",
                                timeout=60000,
                            )
                        if _is_login_url(page.url):
                            raise LoginRequiredError(
                                f"Login required: redirected to {page.url} (cookies/state likely expired)"
                            )

                        # 捕获初始搜索的API数据
                        initial_response = await initial_response_info.value
                        _log_search_response_trace("initial", initial_response)
                        break
                    except PlaywrightTimeoutError:
                        if _is_login_url(page.url):
                            raise LoginRequiredError(
                                f"Login required: redirected to {page.url} (cookies/state likely expired)"
                            )
                        await _raise_if_security_challenge(
                            page, keyword, timeout_ms=1000
                        )
                        if retry_index < INITIAL_SEARCH_RESPONSE_RETRY_COUNT - 1:
                            log_time(
                                "等待初始搜索响应超时，"
                                f"{INITIAL_SEARCH_RESPONSE_RETRY_DELAY_SECONDS}秒后重试..."
                            )
                            await asyncio.sleep(
                                INITIAL_SEARCH_RESPONSE_RETRY_DELAY_SECONDS
                            )
                            continue
                        log_time(
                            "等待初始搜索响应超时，本轮任务跳过，"
                            "不计为失败保护。"
                        )
                        close_session_reason = "等待初始搜索响应超时，关闭复用会话"
                        return 0
                    except PlaywrightError as e:
                        if _is_login_url(page.url):
                            raise LoginRequiredError(
                                f"Login required: redirected to {page.url} (cookies/state likely expired)"
                            ) from e
                        if not _is_navigation_aborted_error(e):
                            raise
                        await _raise_if_security_challenge(
                            page, keyword, timeout_ms=1000
                        )
                        if retry_index < INITIAL_SEARCH_RESPONSE_RETRY_COUNT - 1:
                            log_time(
                                "初始搜索页导航被浏览器取消(net::ERR_ABORTED)，"
                                f"{INITIAL_SEARCH_RESPONSE_RETRY_DELAY_SECONDS}秒后重试..."
                            )
                            await asyncio.sleep(
                                INITIAL_SEARCH_RESPONSE_RETRY_DELAY_SECONDS
                            )
                            continue
                        raise

            # 冷启动路径需要确认筛选栏可见；复用 replay 路径不再依赖页面 DOM。
            if not replay_confirmed_search_request:
                try:
                    await page.wait_for_selector("text=新发布", timeout=15000)
                except PlaywrightTimeoutError as e:
                    if _is_login_url(page.url):
                        raise LoginRequiredError(
                            f"Login required: redirected to {page.url} (cookies/state likely expired)"
                        ) from e
                    raise

            if not replay_confirmed_search_request:
                # 模拟真实用户行为：页面加载后的初始停留和浏览
                log_time("[反爬] 模拟用户查看页面...")
                await random_sleep(1, 3)

                # --- 新增：检查是否存在验证弹窗 ---
                baxia_dialog = page.locator("div.baxia-dialog-mask")
                middleware_widget = page.locator("div.J_MIDDLEWARE_FRAME_WIDGET")
                try:
                    # 等待弹窗在2秒内出现。如果出现，则执行块内代码。
                    await baxia_dialog.wait_for(state="visible", timeout=2000)
                    print(
                        "\n==================== CRITICAL BLOCK DETECTED ===================="
                    )
                    print("检测到闲鱼反爬虫验证弹窗 (baxia-dialog)，无法继续操作。")
                    print("这通常是因为操作过于频繁或被识别为机器人。")
                    print("建议：")
                    print("1. 停止脚本一段时间再试。")
                    print(
                        "2. (推荐) 在 .env 文件中设置 RUN_HEADLESS=false，以非无头模式运行，这有助于绕过检测。"
                    )
                    print(f"任务 '{keyword}' 将在此处中止。")
                    print(
                        "==================================================================="
                    )
                    raise RiskControlError("baxia-dialog")
                except PlaywrightTimeoutError:
                    # 2秒内弹窗未出现，这是正常情况，继续执行
                    pass

                # 检查是否有J_MIDDLEWARE_FRAME_WIDGET覆盖层
                try:
                    await middleware_widget.wait_for(state="visible", timeout=2000)
                    print(
                        "\n==================== CRITICAL BLOCK DETECTED ===================="
                    )
                    print(
                        "检测到闲鱼反爬虫验证弹窗 (J_MIDDLEWARE_FRAME_WIDGET)，无法继续操作。"
                    )
                    print("这通常是因为操作过于频繁或被识别为机器人。")
                    print("建议：")
                    print("1. 停止脚本一段时间再试。")
                    print("2. (推荐) 更新登录状态文件，确保登录状态有效。")
                    print("3. 降低任务执行频率，避免被识别为机器人。")
                    print(f"任务 '{keyword}' 将在此处中止。")
                    print(
                        "==================================================================="
                    )
                    raise RiskControlError("J_MIDDLEWARE_FRAME_WIDGET")
                except PlaywrightTimeoutError:
                    # 2秒内弹窗未出现，这是正常情况，继续执行
                    pass
                # --- 结束新增 ---

                try:
                    await page.click("div[class*='closeIconBg']", timeout=3000)
                    print("LOG: 已关闭广告弹窗。")
                except PlaywrightTimeoutError:
                    print("LOG: 未检测到广告弹窗。")

            if replay_confirmed_search_request:
                log_time("步骤 2 - 已复用上轮筛选接口模板，跳过 DOM 筛选。")
            else:
                final_response = None
                new_publish_response = None
                log_time("步骤 2 - 应用筛选条件...")
            if new_publish_option and not replay_confirmed_search_request:
                try:
                    log_time(f"新发布筛选: {new_publish_option}")
                    await _open_new_publish_filter(page)
                    await random_sleep(1, 2)  # 原来是 (1.5, 2.5)
                    new_publish_option_locator = (
                        await _find_new_publish_option_in_open_filter(
                            page,
                            new_publish_option,
                        )
                    )

                    async def _click_new_publish_option() -> None:
                        await new_publish_option_locator.click()

                    new_publish_response = await _capture_search_response_after_action(
                        page=page,
                        action=_click_new_publish_option,
                        timeout_ms=20000,
                        settle_min_seconds=2,
                        settle_max_seconds=4,
                    )
                    final_response = new_publish_response
                    _log_search_response_trace(
                        "new_publish",
                        new_publish_response,
                    )
                except LoginRequiredError:
                    close_session_reason = "登录弹窗遮挡新发布筛选，关闭复用会话"
                    raise
                except NewPublishPopupNotFoundError as e:
                    close_session_reason = f"{e}，关闭复用会话"
                    log_time(
                        f"{e}，无法应用新发布筛选 '{new_publish_option}'，"
                        "本轮跳过并关闭复用会话。"
                    )
                    return 0
                except NewPublishOptionNotFoundError as e:
                    close_session_reason = f"{e}，关闭复用会话"
                    log_time(f"{e}，本轮跳过并关闭复用会话。")
                    return 0
                except PlaywrightTimeoutError:
                    close_session_reason = (
                        f"新发布筛选 '{new_publish_option}' 接口响应超时，关闭复用会话"
                    )
                    log_time(
                        f"新发布筛选 '{new_publish_option}' 接口响应超时(20秒)，"
                        "本轮跳过并关闭复用会话。"
                    )
                    return 0
                except Exception as e:
                    print(f"LOG: 应用新发布筛选失败: {e}")
                    return 0

            if personal_only and not replay_confirmed_search_request:
                try:
                    async with page.expect_response(
                        is_search_results_response, timeout=20000
                    ) as response_info:
                        await page.click("text=个人闲置")
                        # --- 修改: 将固定等待改为随机等待，并加长 ---
                        await random_sleep(2, 4)  # 原来是 asyncio.sleep(5)
                    final_response = await response_info.value
                    _log_search_response_trace("final:personal_only", final_response)
                except PlaywrightTimeoutError:
                    log_time("个人闲置筛选请求超时，继续执行。")
                except Exception as e:
                    print(f"LOG: 应用个人闲置筛选失败: {e}")

            if free_shipping and not replay_confirmed_search_request:
                try:
                    async with page.expect_response(
                        is_search_results_response, timeout=20000
                    ) as response_info:
                        await page.click("text=包邮")
                        await random_sleep(2, 4)
                    final_response = await response_info.value
                    _log_search_response_trace("final:free_shipping", final_response)
                except PlaywrightTimeoutError:
                    log_time("包邮筛选请求超时，继续执行。")
                except Exception as e:
                    print(f"LOG: 应用包邮筛选失败: {e}")

            if region_filter and not replay_confirmed_search_request:
                try:
                    area_trigger = page.get_by_text("区域", exact=True)
                    if await area_trigger.count():
                        await area_trigger.first.click()
                        await random_sleep(1.5, 2)
                        popover_candidates = page.locator("div.ant-popover")
                        popover = popover_candidates.filter(
                            has=page.locator(
                                ".areaWrap--FaZHsn8E, [class*='areaWrap']"
                            )
                        ).last
                        if not await popover.count():
                            popover = popover_candidates.filter(
                                has=page.get_by_text("重新定位")
                            ).last
                        if not await popover.count():
                            popover = popover_candidates.filter(
                                has=page.get_by_text("查看")
                            ).last
                        if not await popover.count():
                            print("LOG: 未找到区域弹窗，跳过区域筛选。")
                            raise PlaywrightTimeoutError("region-popover-not-found")
                        await popover.wait_for(state="visible", timeout=5000)

                        # 列表容器：第一层 children 即省/市/区三列，不再强依赖具体类名，提升鲁棒性
                        area_wrap = popover.locator(
                            ".areaWrap--FaZHsn8E, [class*='areaWrap']"
                        ).first
                        await area_wrap.wait_for(state="visible", timeout=3000)
                        columns = area_wrap.locator(":scope > div")
                        col_prov = columns.nth(0)
                        col_city = columns.nth(1)
                        col_dist = columns.nth(2)

                        region_parts = [
                            p.strip() for p in region_filter.split("/") if p.strip()
                        ]

                        async def _click_in_column(
                            column_locator, text_value: str, desc: str
                        ) -> None:
                            option = column_locator.locator(
                                ".provItem--QAdOx8nD", has_text=text_value
                            ).first
                            if await option.count():
                                await option.click()
                                await random_sleep(1.5, 2)
                                try:
                                    await option.wait_for(
                                        state="attached", timeout=1500
                                    )
                                    await option.wait_for(
                                        state="visible", timeout=1500
                                    )
                                except PlaywrightTimeoutError:
                                    pass
                            else:
                                print(f"LOG: 未找到{desc} '{text_value}'，跳过。")

                        if len(region_parts) >= 1:
                            await _click_in_column(
                                col_prov, region_parts[0], "省份"
                            )
                            await random_sleep(1, 2)
                        if len(region_parts) >= 2:
                            await _click_in_column(
                                col_city, region_parts[1], "城市"
                            )
                            await random_sleep(1, 2)
                        if len(region_parts) >= 3:
                            await _click_in_column(
                                col_dist, region_parts[2], "区/县"
                            )
                            await random_sleep(1, 2)

                        search_btn = popover.locator(
                            "div.searchBtn--Ic6RKcAb"
                        ).first
                        if await search_btn.count():
                            try:
                                async with page.expect_response(
                                    is_search_results_response,
                                    timeout=20000,
                                ) as response_info:
                                    await search_btn.click()
                                    await random_sleep(2, 3)
                                final_response = await response_info.value
                                _log_search_response_trace(
                                    "final:region",
                                    final_response,
                                )
                            except PlaywrightTimeoutError:
                                log_time("区域筛选提交超时，继续执行。")
                        else:
                            print(
                                "LOG: 未找到区域弹窗的“查看XX件宝贝”按钮，跳过提交。"
                            )
                    else:
                        print("LOG: 未找到区域筛选触发器。")
                except PlaywrightTimeoutError:
                    log_time(f"区域筛选 '{region_filter}' 请求超时，继续执行。")
                except Exception as e:
                    print(f"LOG: 应用区域筛选 '{region_filter}' 失败: {e}")

            if (min_price or max_price) and not replay_confirmed_search_request:
                try:
                    price_container = page.locator(
                        'div[class*="search-price-input-container"]'
                    ).first
                    if await price_container.is_visible():
                        if min_price:
                            await price_container.get_by_placeholder("¥").first.fill(
                                min_price
                            )
                            # --- 修改: 将固定等待改为随机等待 ---
                            await random_sleep(1, 2.5)  # 原来是 asyncio.sleep(5)
                        if max_price:
                            await (
                                price_container.get_by_placeholder("¥")
                                .nth(1)
                                .fill(max_price)
                            )
                            # --- 修改: 将固定等待改为随机等待 ---
                            await random_sleep(1, 2.5)  # 原来是 asyncio.sleep(5)

                        async with page.expect_response(
                            is_search_results_response, timeout=20000
                        ) as response_info:
                            await page.keyboard.press("Tab")
                            # --- 修改: 增加确认价格后的等待时间 ---
                            await random_sleep(2, 4)  # 原来是 asyncio.sleep(5)
                        final_response = await response_info.value
                        _log_search_response_trace("final:price", final_response)
                    else:
                        print("LOG: 警告 - 未找到价格输入容器。")
                except PlaywrightTimeoutError:
                    log_time("价格筛选请求超时，继续执行。")
                except Exception as e:
                    print(f"LOG: 应用价格筛选失败: {e}")

            log_time("所有筛选已完成，开始处理商品列表...")

            requires_filter_response = _requires_confirmed_filter_response(
                task_config
            )
            current_response = _select_search_response_for_processing(
                initial_response=initial_response,
                final_response=final_response,
                publish_response=new_publish_response,
                requires_filter_response=requires_filter_response,
            )
            _log_search_response_trace("final", final_response)
            selected_response_stage = _search_response_stage_for_log(
                selected_response=current_response,
                initial_response=initial_response,
                publish_response=new_publish_response,
                final_response=final_response,
            )
            log_time(f"搜索响应诊断[selected]: stage={selected_response_stage}")
            if current_response is None:
                close_session_reason = "筛选响应未确认或不可信，关闭复用会话"
                log_time(
                    "已配置新发布筛选，但未获得筛选后的搜索响应，"
                    "本轮跳过并关闭复用会话。"
                )
                return 0
            selected_first_page_response = current_response
            retained_search_request_template = None
            if requires_filter_response:
                retained_search_request_template = (
                    await _build_search_request_template_from_response(
                        response=selected_first_page_response,
                        task_config=task_config,
                        new_publish_option=new_publish_option,
                    )
                )
                if not _search_request_template_is_trusted(
                    retained_search_request_template,
                    task_config,
                ):
                    close_session_reason = "筛选接口请求模板不可信，关闭复用会话"
                    log_time(
                        "筛选接口请求模板缺少当前任务的新发布确认参数，"
                        "本轮跳过并关闭复用会话。"
                    )
                    return 0
            for page_num in range(1, max_pages + 1):
                if stop_scraping:
                    break
                log_time(f"开始处理第 {page_num}/{max_pages} 页 ...")

                if page_num > 1:
                    page_advance_result = await advance_search_page(
                        page=page,
                        page_num=page_num,
                    )
                    if not page_advance_result.advanced:
                        break
                    current_response = page_advance_result.response

                if not (current_response and current_response.ok):
                    log_time(f"第 {page_num} 页响应无效，跳过。")
                    continue

                basic_items = await _parse_search_results_json(
                    await current_response.json(), f"第 {page_num} 页"
                )
                if not basic_items:
                    break
                previous_snapshots = list(historical_snapshots)
                historical_snapshots.extend(
                    record_market_snapshots(
                        keyword=keyword,
                        task_name=task_config.get("task_name", "Untitled Task"),
                        items=basic_items,
                        run_id=history_run_id,
                        snapshot_time=datetime.now().isoformat(),
                        seen_item_ids=history_seen_item_ids,
                    )
                )

                total_items_on_page = len(basic_items)
                for i, item_data in enumerate(basic_items, 1):
                    if debug_limit > 0 and processed_item_count >= debug_limit:
                        log_time(
                            f"已达到调试上限 ({debug_limit})，停止获取新商品。"
                        )
                        stop_scraping = True
                        break

                    unique_key = get_link_unique_key(item_data["商品链接"])
                    if unique_key in processed_links:
                        notified_drop = await _notify_duplicate_price_drop_if_needed(
                            item_data=item_data,
                            previous_snapshots=previous_snapshots,
                            keyword_rules=keyword_rules,
                            keyword_alert_rules=keyword_alert_rules,
                            decision_mode=decision_mode,
                        )
                        log_time(
                            f"[页内进度 {i}/{total_items_on_page}] 商品 '{item_data['商品标题'][:20]}...' 已存在，"
                            f"{'已发送降价提醒。' if notified_drop else '跳过。'}"
                        )
                        if is_latest_mode:
                            log_time(
                                "最新模式遇到历史商品，停止继续同步，"
                                "不再拉取后续详情或分页。"
                            )
                            stop_scraping = True
                            break
                        continue

                    log_time(
                        f"[页内进度 {i}/{total_items_on_page}] 发现新商品，保存并通知: {item_data['商品标题'][:30]}..."
                    )
                    final_record = _build_search_list_result_record(
                        item_data=item_data,
                        keyword=keyword,
                        task_name=task_config.get("task_name", "Untitled Task"),
                    )
                    if await save_to_jsonl(final_record, keyword):
                        processed_links.add(unique_key)
                        processed_item_count += 1
                        log_time(
                            f"商品已保存。累计处理 {processed_item_count} 个新商品。"
                        )
                        try:
                            await send_ntfy_notification(
                                item_data, QUICK_NOTIFY_REASON
                            )
                        except Exception as e:
                            print(f"   [通知] 发送失败: {e}")
                    else:
                        print("   错误: 保存搜索列表基础记录失败，跳过通知。")

                # --- 新增: 在处理完一页所有商品后，翻页前，增加一个更长的“休息”时间 ---
                if not stop_scraping and page_num < max_pages:
                    print(
                        f"--- 第 {page_num} 页处理完毕，准备翻页。执行一次页面间的长时休息... ---"
                    )
                    await random_sleep(10, 15)

            if keep_session_after_success and reusable_session is not None:
                _retain_successful_search_session(
                    session=reusable_session,
                    task_config=task_config,
                    state_file=state_file,
                    proxy_server=proxy_server,
                    playwright=playwright_manager,
                    browser=browser,
                    context=context,
                    page=page,
                    search_request_template=retained_search_request_template,
                )
            attempt_success = True
            close_session_reason = ""

        except PlaywrightTimeoutError as e:
            close_session_reason = "页面元素或网络响应超时，关闭复用会话"
            if _is_login_url(page.url):
                raise LoginRequiredError(
                    f"Login required: redirected to {page.url} (cookies/state likely expired)"
                ) from e
            print(f"\n操作超时错误: 页面元素或网络响应未在规定时间内出现。\n{e}")
            raise
        except asyncio.CancelledError:
            close_session_reason = "任务被取消，关闭复用会话"
            log_time("收到取消信号，正在终止当前爬虫任务...")
            raise
        except Exception as e:
            if type(e).__name__ == "TargetClosedError":
                close_session_reason = "浏览器页面已关闭，关闭复用会话"
                log_time("浏览器已关闭，忽略后续异常（可能是任务被停止）。")
                return processed_item_count
            if "passport.goofish.com" in str(e):
                close_session_reason = "跳转登录流程，关闭复用会话"
                raise LoginRequiredError(
                    f"Login required: redirected to passport flow ({e})"
                ) from e
            close_session_reason = f"爬取过程异常，关闭复用会话: {e}"
            print(f"\n爬取过程中发生未知错误: {e}")
            raise
        finally:
            if keep_session_after_success and attempt_success:
                log_time("本轮筛选同步成功，页面已保留供下轮复用。")
            else:
                if reusable_session is not None and close_session_reason:
                    log_time(close_session_reason)
                log_time("任务执行完毕，浏览器将在5秒后自动关闭...")
                await asyncio.sleep(5)
                if debug_limit:
                    input("按回车键关闭浏览器...")
                if reusable_session is not None:
                    await reusable_session.close()
                else:
                    if browser is not None:
                        await browser.close()
                    if playwright_manager is not None:
                        await playwright_manager.stop()

        return processed_item_count

    processed_item_count = 0
    attempt_limit = max(
        rotation_settings["account_retry_limit"],
        rotation_settings["proxy_retry_limit"],
        1,
    )
    last_error = ""
    last_state_path: Optional[str] = None

    # If this task is already in a paused state, skip immediately.
    task_name_for_guard = task_config.get("task_name", "未命名任务")
    pause_cookie_path = None
    if (
        isinstance(task_config.get("account_state_file"), str)
        and task_config.get("account_state_file").strip()
    ):
        pause_cookie_path = task_config.get("account_state_file").strip()
    elif os.path.exists(STATE_FILE):
        pause_cookie_path = STATE_FILE

    decision = FAILURE_GUARD.should_skip_start(
        task_name_for_guard, cookie_path=pause_cookie_path
    )
    if decision.skip:
        print(
            f"[FailureGuard] 任务 '{task_name_for_guard}' 已暂停重试 (连续失败 {decision.consecutive_failures}/{FAILURE_GUARD.threshold})"
        )
        if decision.should_notify:
            try:
                await send_ntfy_notification(
                    {
                        "商品标题": f"[任务暂停] {task_name_for_guard}",
                        "当前售价": "N/A",
                        "商品链接": "#",
                    },
                    "任务处于暂停状态，将跳过执行。\n"
                    f"原因: {decision.reason}\n"
                    f"连续失败: {decision.consecutive_failures}/{FAILURE_GUARD.threshold}\n"
                    f"暂停到: {decision.paused_until.strftime('%Y-%m-%d %H:%M:%S') if decision.paused_until else 'N/A'}\n"
                    "修复方法: 更新登录态/cookies文件后会自动恢复。",
                )
            except Exception as e:
                print(f"发送任务暂停通知失败: {e}")

        cleanup_task_images(task_config.get("task_name", "default"))
        return 0

    for attempt in range(1, attempt_limit + 1):
        if attempt == 1:
            selected_account = _select_account()
            selected_proxy = _select_proxy()
        else:
            if (
                rotation_settings["account_enabled"]
                and rotation_settings["account_mode"] == "on_failure"
            ):
                account_pool.mark_bad(selected_account, last_error)
                selected_account = _select_account(force_new=True)
            if (
                rotation_settings["proxy_enabled"]
                and rotation_settings["proxy_mode"] == "on_failure"
            ):
                proxy_pool.mark_bad(selected_proxy, last_error)
                selected_proxy = _select_proxy(force_new=True)

        if rotation_settings["account_enabled"] and not selected_account:
            last_error = "未找到可用的登录状态文件，无法继续执行任务。"
            print(last_error)
            break
        if not rotation_settings["account_enabled"] and not selected_account:
            last_error = "未找到可用的登录状态文件，无法继续执行任务。"
            print(last_error)
            break
        if rotation_settings["proxy_enabled"] and not selected_proxy:
            last_error = "未找到可用的代理地址，无法继续执行任务。"
            print(last_error)
            break

        state_path = selected_account.value if selected_account else STATE_FILE
        last_state_path = state_path
        proxy_server = selected_proxy.value if selected_proxy else None
        if rotation_settings["account_enabled"]:
            print(f"账号轮换：使用登录状态 {state_path}")
        if rotation_settings["proxy_enabled"] and proxy_server:
            print(f"IP 轮换：使用代理 {proxy_server}")

        try:
            processed_item_count += await _run_scrape_attempt(state_path, proxy_server)
            last_error = ""
            FAILURE_GUARD.record_success(task_name_for_guard)
            break
        except LoginRequiredError as e:
            last_error = str(e)
            print(f"检测到登录失效/重定向: {e}")
            break
        except RiskControlError as e:
            last_error = str(e)
            print(f"检测到风控或验证触发: {e}")
            # 风控验证通常不是简单轮换能解决的，避免无意义重试。
            break
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            print(f"本次尝试失败: {last_error}")
            if attempt < attempt_limit:
                print("将尝试轮换账号/IP 后重试...")

    if last_error:
        await _notify_task_failure(task_config, last_error, cookie_path=last_state_path)

    # 清理任务图片目录
    cleanup_task_images(task_config.get("task_name", "default"))

    return processed_item_count

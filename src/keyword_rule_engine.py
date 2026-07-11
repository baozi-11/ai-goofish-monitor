"""
关键词判断引擎：单组 OR 逻辑，命中任意关键词即推荐。
纯英数字关键词按完整词匹配，避免 Q1 误命中 Q1R5。
"""
import re
from typing import Any, Dict, Iterable, List, Optional

from src.services.price_history_service import parse_price_value


_ASCII_TOKEN_KEYWORD_PATTERN = re.compile(r"^[a-z0-9 ]+$")
_ASCII_TOKEN_BOUNDARY = r"[a-z0-9]"


def normalize_text(value: str) -> str:
    return " ".join((value or "").lower().split())


def _collect_text_fragments(value: Any, bucket: List[str]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if text:
            bucket.append(text)
        return
    if isinstance(value, (int, float, bool)):
        bucket.append(str(value))
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_text_fragments(item, bucket)
        return
    if isinstance(value, list):
        for item in value:
            _collect_text_fragments(item, bucket)


def build_search_text(record: Dict[str, Any]) -> str:
    fragments: List[str] = []
    product_info = record.get("商品信息", {})

    _collect_text_fragments(product_info.get("商品标题"), fragments)
    _collect_text_fragments(product_info, fragments)

    return normalize_text(" ".join(fragments))


def _normalize_keywords(values: Iterable[str]) -> List[str]:
    normalized: List[str] = []
    seen = set()
    for raw in values or []:
        text = normalize_text(str(raw).strip())
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _uses_ascii_token_match(keyword: str) -> bool:
    return bool(keyword) and _ASCII_TOKEN_KEYWORD_PATTERN.fullmatch(keyword) is not None


def _keyword_matches(keyword: str, normalized_text: str) -> bool:
    if not _uses_ascii_token_match(keyword):
        return keyword in normalized_text
    pattern = rf"(?<!{_ASCII_TOKEN_BOUNDARY}){re.escape(keyword)}(?!{_ASCII_TOKEN_BOUNDARY})"
    return re.search(pattern, normalized_text) is not None


def evaluate_keyword_rules(keywords: List[str], search_text: str) -> Dict[str, Any]:
    normalized_text = normalize_text(search_text)
    normalized_keywords = _normalize_keywords(keywords)

    if not normalized_text:
        return {
            "analysis_source": "keyword",
            "is_recommended": False,
            "reason": "可匹配文本为空，关键词规则无法执行。",
            "matched_keywords": [],
            "keyword_hit_count": 0,
        }

    if not normalized_keywords:
        return {
            "analysis_source": "keyword",
            "is_recommended": False,
            "reason": "未配置关键词规则。",
            "matched_keywords": [],
            "keyword_hit_count": 0,
        }

    matched_keywords = [kw for kw in normalized_keywords if _keyword_matches(kw, normalized_text)]
    hit_count = len(matched_keywords)
    is_recommended = hit_count > 0

    if is_recommended:
        reason = f"命中 {hit_count} 个关键词：{', '.join(matched_keywords)}"
    else:
        reason = "未命中任何关键词。"

    return {
        "analysis_source": "keyword",
        "is_recommended": is_recommended,
        "reason": reason,
        "matched_keywords": matched_keywords,
        "keyword_hit_count": hit_count,
    }


def _normalize_alert_rules(rules: Iterable[Any]) -> List[Dict[str, Optional[str]]]:
    normalized: List[Dict[str, Optional[str]]] = []
    seen = set()
    for raw in rules or []:
        if isinstance(raw, dict):
            keyword = str(raw.get("keyword") or "").strip()
            max_price = raw.get("max_price")
        else:
            keyword = str(raw or "").strip()
            max_price = None
        if not keyword:
            continue
        key = normalize_text(keyword)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "keyword": keyword,
                "max_price": None if max_price in (None, "", "null", "undefined") else str(max_price).strip(),
            }
        )
    return normalized


def extract_keywords_from_alert_rules(rules: Iterable[Any]) -> List[str]:
    return [rule["keyword"] for rule in _normalize_alert_rules(rules)]


def evaluate_keyword_alert_rules(
    rules: List[Dict[str, Optional[str]]],
    search_text: str,
    current_price: Any,
) -> Dict[str, Any]:
    alert_rules = _normalize_alert_rules(rules)
    base_result = evaluate_keyword_rules(
        [rule["keyword"] for rule in alert_rules],
        search_text,
    )
    base_result["matched_keyword_rules"] = []

    if not base_result.get("is_recommended"):
        return base_result

    price_value = parse_price_value(current_price)
    matched_keywords = set(base_result.get("matched_keywords") or [])
    matched_rules = [
        rule for rule in alert_rules
        if normalize_text(rule["keyword"]) in matched_keywords
    ]
    base_result["matched_keyword_rules"] = matched_rules

    rules_without_price = [rule for rule in matched_rules if rule.get("max_price") is None]
    if rules_without_price:
        base_result["reason"] = (
            f"命中 {len(matched_rules)} 个关键词："
            f"{', '.join(rule['keyword'] for rule in matched_rules)}"
        )
        return base_result

    if price_value is None:
        base_result["is_recommended"] = False
        base_result["reason"] = "命中关键词，但当前价格无法解析，跳过价格提醒。"
        return base_result

    price_hits = []
    for rule in matched_rules:
        threshold = parse_price_value(rule.get("max_price"))
        if threshold is not None and price_value <= threshold:
            price_hits.append((rule, threshold))

    if not price_hits:
        thresholds = [
            f"{rule['keyword']}≤{rule.get('max_price')}"
            for rule in matched_rules
            if rule.get("max_price") is not None
        ]
        base_result["is_recommended"] = False
        base_result["reason"] = (
            f"命中关键词，但当前价 {current_price} 高于提醒价："
            f"{', '.join(thresholds)}"
        )
        return base_result

    base_result["matched_keyword_rules"] = [rule for rule, _ in price_hits]
    base_result["matched_keywords"] = [rule["keyword"] for rule, _ in price_hits]
    base_result["keyword_hit_count"] = len(price_hits)
    price_hit_labels = [
        f"{rule['keyword']}≤{threshold:g}" for rule, threshold in price_hits
    ]
    base_result["reason"] = (
        f"命中 {len(price_hits)} 个价格提醒关键词："
        f"{', '.join(price_hit_labels)}，当前价 {price_value:g}"
    )
    return base_result
    return base_result

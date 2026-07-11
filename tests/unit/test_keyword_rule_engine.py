from src.keyword_rule_engine import (
    build_search_text,
    evaluate_keyword_alert_rules,
    evaluate_keyword_rules,
)


def _sample_record():
    return {
        "商品信息": {
            "商品标题": "Sony A7M4 全画幅相机",
            "当前售价": "10000",
            "商品标签": ["验货宝", "包邮"],
        },
        "卖家信息": {
            "卖家昵称": "摄影器材店",
            "卖家个性签名": "可验机，支持同城面交",
        },
    }


def test_build_search_text_contains_product_fields_only():
    text = build_search_text(_sample_record())
    assert "sony a7m4" in text
    assert "验货宝" in text
    assert "摄影器材店" not in text
    assert "支持同城面交" not in text


def test_keyword_rules_or_match_any_keyword():
    text = build_search_text(_sample_record())
    result = evaluate_keyword_rules(["a7m4", "佳能"], text)
    assert result["is_recommended"] is True
    assert result["analysis_source"] == "keyword"
    assert result["keyword_hit_count"] == 1
    assert result["matched_keywords"] == ["a7m4"]


def test_keyword_rules_count_multiple_hits():
    text = build_search_text(_sample_record())
    result = evaluate_keyword_rules(["a7m4", "验货宝", "摄影器材店"], text)
    assert result["is_recommended"] is True
    assert result["keyword_hit_count"] == 2


def test_keyword_rules_case_insensitive_contains():
    text = build_search_text(_sample_record())
    result = evaluate_keyword_rules(["SONY", "A7M4"], text)
    assert result["is_recommended"] is True
    assert result["keyword_hit_count"] == 2


def test_keyword_rules_no_match():
    text = build_search_text(_sample_record())
    result = evaluate_keyword_rules(["佳能", "单反"], text)
    assert result["is_recommended"] is False
    assert result["keyword_hit_count"] == 0


def test_keyword_rules_do_not_partially_match_alphanumeric_prefixes():
    result = evaluate_keyword_rules(["q1"], "富士 q1r5 旗舰相机")
    assert result["is_recommended"] is False
    assert result["keyword_hit_count"] == 0


def test_keyword_rules_still_match_full_alphanumeric_token():
    result = evaluate_keyword_rules(["q1r5"], "富士 q1r5 旗舰相机")
    assert result["is_recommended"] is True
    assert result["keyword_hit_count"] == 1


def test_keyword_alert_rules_without_price_threshold_recommend():
    text = build_search_text(_sample_record())
    result = evaluate_keyword_alert_rules(
        [{"keyword": "a7m4", "max_price": None}],
        text,
        "10000",
    )
    assert result["is_recommended"] is True
    assert result["matched_keywords"] == ["a7m4"]


def test_keyword_alert_rules_respect_max_price_threshold():
    text = build_search_text(_sample_record())
    high = evaluate_keyword_alert_rules(
        [{"keyword": "a7m4", "max_price": "9000"}],
        text,
        "10000",
    )
    low = evaluate_keyword_alert_rules(
        [{"keyword": "a7m4", "max_price": "10000"}],
        text,
        "10000",
    )

    assert high["is_recommended"] is False
    assert low["is_recommended"] is True
    assert low["keyword_hit_count"] == 1


def test_keyword_alert_rules_skip_unparseable_price_when_threshold_required():
    text = build_search_text(_sample_record())
    result = evaluate_keyword_alert_rules(
        [{"keyword": "a7m4", "max_price": "9000"}],
        text,
        "价格异常",
    )
    assert result["is_recommended"] is False
    assert "价格无法解析" in result["reason"]

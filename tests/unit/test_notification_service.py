import asyncio

from src.infrastructure.external.notification_clients.base import NotificationClient
from src.infrastructure.external.notification_clients.ntfy_client import NtfyClient
from src.infrastructure.external.notification_clients.webhook_client import WebhookClient
from src.services.notification_service import NotificationService


class _OkClient(NotificationClient):
    channel_key = "ok"
    display_name = "OK"

    async def send(self, product_data, reason):
        return None


class _FailClient(NotificationClient):
    channel_key = "fail"
    display_name = "FAIL"

    async def send(self, product_data, reason):
        raise RuntimeError("boom")


def test_notification_service_collects_success_and_failure_results():
    service = NotificationService([_OkClient(enabled=True), _FailClient(enabled=True)])

    results = asyncio.run(
        service.send_notification({"商品标题": "Sony A7M4"}, "价格合适")
    )

    assert results["ok"]["success"] is True
    assert results["ok"]["message"] == "发送成功"
    assert results["fail"]["success"] is False
    assert results["fail"]["message"] == "boom"


def test_webhook_client_renders_json_templates(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

    def _fake_post(url, headers=None, json=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["data"] = data
        return _FakeResponse()

    monkeypatch.setattr("requests.post", _fake_post)

    client = WebhookClient(
        webhook_url="https://hooks.example.com/notify",
        webhook_method="POST",
        webhook_headers='{"Authorization":"Bearer token"}',
        webhook_content_type="JSON",
        webhook_query_parameters='{"task":"{{title}}"}',
        webhook_body='{"message":"{{content}}","link":"{{desktop_link}}"}',
        pcurl_to_mobile=False,
    )

    asyncio.run(
        client.send(
            {
                "商品标题": "Sony A7M4",
                "当前售价": "9999",
                "商品链接": "https://www.goofish.com/item/123",
            },
            "价格合适",
        )
    )

    assert "task=%F0%9F%9A%A8+%E6%96%B0%E6%8E%A8%E8%8D%90%21+Sony+A7M4" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer token"
    assert captured["json"]["message"].startswith("价格: 9999")
    assert captured["json"]["link"] == "https://www.goofish.com/item/123"
    assert captured["data"] is None


def test_ntfy_client_uses_compact_body_and_attaches_image(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

    def _fake_post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("requests.post", _fake_post)

    client = NtfyClient(
        topic_url="https://ntfy.sh/demo-topic",
        pcurl_to_mobile=True,
    )

    asyncio.run(
        client.send(
            {
                "商品标题": "Sony A7M4",
                "当前售价": "9999",
                "商品链接": "https://www.goofish.com/item?id=123",
                "商品主图链接": "https://img.example.com/item.jpg",
            },
            "价格合适",
        )
    )

    body = captured["data"].decode("utf-8")
    assert captured["url"] == "https://ntfy.sh/demo-topic"
    assert body.startswith("价格: 9999\n原因: 价格合适")
    assert "🚨 新推荐! Sony A7M4" not in body
    assert "手机端链接: https://pages.goofish.com/sharexy?" in body
    assert captured["headers"]["Title"] == "🚨 新推荐! Sony A7M4".encode("utf-8")
    assert captured["headers"]["Priority"] == "urgent"
    assert captured["headers"]["Tags"] == "bell,vibration"
    assert captured["headers"]["Attach"] == "https://img.example.com/item.jpg"
    assert captured["timeout"] == 10


def test_ntfy_client_omits_attach_header_when_image_missing(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

    def _fake_post(url, data=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse()

    monkeypatch.setattr("requests.post", _fake_post)

    client = NtfyClient(
        topic_url="https://ntfy.sh/demo-topic",
        pcurl_to_mobile=False,
    )

    asyncio.run(
        client.send(
            {
                "商品标题": "Sony A7M4",
                "当前售价": "9999",
                "商品链接": "https://www.goofish.com/item?id=123",
            },
            "价格合适",
        )
    )

    assert "Attach" not in captured["headers"]

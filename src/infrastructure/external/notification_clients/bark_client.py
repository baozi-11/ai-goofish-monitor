"""
Bark 通知客户端
"""
import asyncio
import requests
from typing import Dict
from urllib.parse import quote

from .base import NotificationClient


class BarkClient(NotificationClient):
    """Bark 通知客户端"""

    channel_key = "bark"
    display_name = "Bark"

    def __init__(self, bark_url: str = None, pcurl_to_mobile: bool = True):
        super().__init__(enabled=bool(bark_url), pcurl_to_mobile=pcurl_to_mobile)
        self.bark_url = bark_url

    async def send(self, product_data: Dict, reason: str) -> None:
        """发送 Bark 通知"""
        if not self.is_enabled():
            raise RuntimeError("Bark 未启用")

        message = self._build_message(product_data, reason)
        content_lines = [
            message.notification_title,
            "",
            f"价格: {message.price}",
            f"原因: {message.reason}",
        ]
        if message.mobile_link:
            content_lines.append(f"手机端链接: {message.mobile_link}")

        bark_message = "\n".join(content_lines)
        bark_url = f"{self.bark_url.rstrip('/')}/{quote(bark_message, safe='')}"
        bark_params = {
            "level": "timeSensitive",
            "group": "闲鱼监控",
        }

        if message.image_url:
            bark_params["image"] = message.image_url

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.get(
                bark_url,
                params=bark_params,
                timeout=10,
            )
        )
        response.raise_for_status()

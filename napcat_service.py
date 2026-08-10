from __future__ import annotations

from typing import Any

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

try:
    from .models import Quote
    from .utils import parse_cq_message
except ImportError:  # pragma: no cover
    from models import Quote
    from utils import parse_cq_message


class NapcatService:
    async def fetch_onebot_message(self, event: Any, message_id: str | None) -> dict[str, Any]:
        if not message_id or event.get_platform_name() != "aiocqhttp":
            return {}
        client = getattr(event, "bot", None)
        if client is None:
            return {}

        errors: list[str] = []
        for candidate in self._message_id_candidates(message_id):
            try:
                response = await client.api.call_action("get_msg", message_id=candidate)
                payload = self._unwrap_action_payload(response)
                if payload:
                    return payload
            except Exception as exc:
                errors.append(str(exc))

        if errors:
            logger.info(f"get_msg 失败: {' | '.join(errors)}")
        return {}

    async def fetch_forward_messages(
        self,
        event: Any,
        *,
        forward_id: str | None = None,
        forward_payload: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        messages = self._extract_messages_from_forward_data(forward_payload)
        if messages:
            return messages

        if not forward_id or event.get_platform_name() != "aiocqhttp":
            return []

        client = getattr(event, "bot", None)
        if client is None:
            return []

        for params in ({"id": str(forward_id)}, {"message_id": str(forward_id)}):
            try:
                response = await client.api.call_action("get_forward_msg", **params)
                payload = self._unwrap_action_payload(response)
                messages = self._extract_messages_from_forward_data(payload)
                if messages:
                    return messages
            except Exception as exc:
                logger.info(f"get_forward_msg 失败({params}): {exc}")
        return []

    def extract_forward_reference(self, message_obj: Any) -> tuple[str | None, dict[str, Any] | None]:
        if isinstance(message_obj, str):
            # get_msg 在 message_format=string 的实现上返回 CQ 码字符串
            message_obj = parse_cq_message(message_obj)
        return (
            self._extract_forward_id_from_message_obj(message_obj),
            self._extract_forward_payload_from_message_obj(message_obj),
        )

    async def resolve_user_name(self, event: Any, qq: str) -> str:
        if not qq:
            return ""
        if event.get_platform_name() != "aiocqhttp":
            return qq
        try:
            client = event.bot
            group_id = event.get_group_id()
            if group_id:
                response = await client.api.call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(qq),
                    no_cache=True,
                )
                payload = self._unwrap_action_payload(response)
                card = str(payload.get("card") or "").strip()
                nickname = str(payload.get("nickname") or "").strip()
                if card or nickname:
                    return card or nickname
            response = await client.api.call_action(
                "get_stranger_info",
                user_id=int(qq),
                no_cache=True,
            )
            payload = self._unwrap_action_payload(response)
            nickname = str(payload.get("nickname") or "").strip()
            if nickname:
                return nickname
        except Exception as exc:
            logger.info(f"读取 Napcat 用户信息失败，回退：{exc}")
        return qq

    async def resolve_signature_name(self, event: Any, quote: Quote, use_group_signature: bool) -> str:
        if not use_group_signature:
            return quote.name
        if event.get_platform_name() != "aiocqhttp":
            return quote.name
        group_id = str(quote.group or "").strip()
        qq = str(quote.qq or "").strip()
        if not (group_id.isdigit() and qq.isdigit()):
            return quote.name
        try:
            client = event.bot
            response = await client.api.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(qq),
                no_cache=True,
            )
            payload = self._unwrap_action_payload(response)
            card = str(payload.get("card") or "").strip()
            nickname = str(payload.get("nickname") or "").strip()
            return card or nickname or quote.name
        except Exception as exc:
            logger.info(f"读取 Napcat 群名片失败，回退：{exc}")
            return quote.name

    def _unwrap_action_payload(self, response: Any) -> dict[str, Any]:
        if not isinstance(response, dict):
            return {}
        if "message" in response or "messages" in response:
            return response
        data = response.get("data")
        if isinstance(data, dict):
            return data
        return response

    def _message_id_candidates(self, message_id: str | None) -> list[Any]:
        value = str(message_id or "").strip()
        if not value:
            return []
        candidates: list[Any] = [value]
        if value.isdigit():
            candidates.insert(0, int(value))
        return candidates

    def _extract_messages_from_forward_data(self, forward_data: Any) -> list[dict[str, Any]]:
        if isinstance(forward_data, list):
            return [item for item in forward_data if isinstance(item, dict)]

        if not isinstance(forward_data, dict):
            return []

        for key in ("messages", "message"):
            value = forward_data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

        data_obj = forward_data.get("data")
        if isinstance(data_obj, dict):
            for key in ("messages", "message"):
                value = data_obj.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def _extract_forward_id_from_segment_data(self, seg_data: Any) -> str | None:
        if not isinstance(seg_data, dict):
            return None
        for key in ("id", "resid", "forward_id"):
            value = seg_data.get(key)
            if value:
                return str(value)
        return None

    def _extract_forward_id_from_message_obj(self, message_obj: Any) -> str | None:
        if isinstance(message_obj, list):
            for item in message_obj:
                found = self._extract_forward_id_from_message_obj(item)
                if found:
                    return found
            return None

        if not isinstance(message_obj, dict):
            return None

        seg_type = message_obj.get("type")
        if seg_type == "forward":
            found = self._extract_forward_id_from_segment_data(message_obj.get("data", {}))
            if found:
                return found
            found = self._extract_forward_id_from_segment_data(message_obj)
            if found:
                return found

        for key in ("message", "messages", "content", "data"):
            found = self._extract_forward_id_from_message_obj(message_obj.get(key))
            if found:
                return found
        return None

    def _extract_forward_payload_from_message_obj(self, message_obj: Any) -> dict[str, Any] | None:
        if isinstance(message_obj, list):
            for item in message_obj:
                found = self._extract_forward_payload_from_message_obj(item)
                if found:
                    return found
            return None

        if not isinstance(message_obj, dict):
            return None

        seg_type = message_obj.get("type")
        if seg_type == "forward":
            seg_data = message_obj.get("data", {})
            if isinstance(seg_data, dict) and isinstance(seg_data.get("messages"), list):
                return {"messages": seg_data.get("messages", [])}
            if isinstance(message_obj.get("messages"), list):
                return {"messages": message_obj.get("messages", [])}

        for key in ("message", "messages", "content", "data"):
            found = self._extract_forward_payload_from_message_obj(message_obj.get(key))
            if found:
                return found
        return None

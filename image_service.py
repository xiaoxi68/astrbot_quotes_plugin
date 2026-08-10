from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    from astrbot.api import logger
    import astrbot.api.message_components as Comp
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)
    Comp = None  # type: ignore

try:
    from .models import (
        ImageCollection,
        PendingForwardNode,
        PendingForwardSegment,
        PendingQuoteSegment,
        PreparedImage,
        PreparedMedia,
    )
    from .utils import parse_cq_message, prepare_image, prepare_media
except ImportError:  # pragma: no cover
    from models import (
        ImageCollection,
        PendingForwardNode,
        PendingForwardSegment,
        PendingQuoteSegment,
        PreparedImage,
        PreparedMedia,
    )
    from utils import parse_cq_message, prepare_image, prepare_media


ForwardMessageLoader = Callable[[Any, str | None, dict[str, Any] | None], Awaitable[list[dict[str, Any]]]]


class ImageService:
    def __init__(
        self,
        http_client: Any | None = None,
        *,
        wake_prefixes: tuple[str, ...] | list[str] | None = None,
    ):
        self.http_client = http_client
        prefixes = [str(item) for item in (wake_prefixes or ()) if str(item)]
        self.wake_prefixes = tuple(
            sorted(
                dict.fromkeys(prefixes),
                key=len,
                reverse=True,
            )
        )

    async def collect_images(
        self,
        event: Any,
        reply_message: Any,
    ) -> ImageCollection:
        reply_images = await self._collect_from_onebot_message(event, reply_message)
        current_images = await self._collect_from_segments(event)
        return ImageCollection(reply_images=reply_images, current_images=current_images)

    async def build_reply_segments(self, event: Any, message: Any) -> list[PendingQuoteSegment]:
        if isinstance(message, str):
            # get_msg 在 message_format=string 的实现上返回 CQ 码字符串
            message = parse_cq_message(message)
        elif isinstance(message, dict):
            message = [message]
        if not isinstance(message, list):
            return []
        segments: list[PendingQuoteSegment] = []
        for segment in message:
            try:
                seg_type = str(segment.get("type") or "").lower()
                if seg_type in {"text", "plain"}:
                    text = str((segment.get("data") or {}).get("text") or "")
                    if text:
                        segments.append(PendingQuoteSegment(type="text", text=text))
                    continue
                if seg_type != "image":
                    continue
                data = segment.get("data") or {}
                prepared = await self._prepare_image_from_onebot_data(event, data)
                if prepared is not None:
                    segments.append(PendingQuoteSegment(type="image", image=prepared))
            except Exception as exc:
                logger.warning(f"构建回复消息段失败: {exc}")
        return segments

    async def build_current_segments(
        self,
        event: Any,
        *,
        command_name: str,
        explicit_qq: str = "",
    ) -> list[PendingQuoteSegment]:
        segments: list[PendingQuoteSegment] = []
        first_plain_consumed = False
        # AstrBot 唤醒阶段已剥离真实生效前缀，message_str 此时以指令开头
        event_message_str = ""
        get_message_str = getattr(event, "get_message_str", None)
        if callable(get_message_str):
            try:
                event_message_str = str(get_message_str() or "")
            except Exception:
                event_message_str = ""
        try:
            message_segments = list(event.get_messages())
        except Exception as exc:
            logger.warning(f"读取当前消息链失败: {exc}")
            return segments

        for raw_segment in message_segments:
            try:
                if Comp is not None and isinstance(raw_segment, Comp.Reply):
                    continue
                if Comp is not None and isinstance(raw_segment, Comp.At):
                    continue
                if Comp is not None and isinstance(raw_segment, Comp.Plain):
                    text = str(getattr(raw_segment, "text", "") or "")
                    if not first_plain_consumed:
                        text = self._strip_command_invocation(
                            text, command_name, event_message_str
                        )
                        if explicit_qq and text.startswith(explicit_qq):
                            text = text[len(explicit_qq) :].strip()
                        first_plain_consumed = True
                    if text:
                        segments.append(PendingQuoteSegment(type="text", text=text))
                    continue
                if Comp is not None and isinstance(raw_segment, Comp.Image):
                    prepared = await self._prepare_image_from_component(event, raw_segment)
                    if prepared is not None:
                        segments.append(PendingQuoteSegment(type="image", image=prepared))
            except Exception as exc:
                logger.warning(f"构建当前消息段失败: {exc}")
        return segments

    async def build_forward_nodes(
        self,
        event: Any,
        *,
        forward_id: str | None,
        forward_payload: dict[str, Any] | None,
        forward_loader: ForwardMessageLoader,
        max_depth: int = 3,
    ) -> list[PendingForwardNode]:
        messages = await forward_loader(
            event,
            forward_id=forward_id,
            forward_payload=forward_payload,
        )
        if not messages:
            return []
        return await self._build_forward_nodes_from_messages(
            event,
            messages,
            forward_loader=forward_loader,
            current_depth=0,
            max_depth=max_depth,
        )

    async def _build_forward_nodes_from_messages(
        self,
        event: Any,
        messages: list[dict[str, Any]],
        *,
        forward_loader: ForwardMessageLoader,
        current_depth: int,
        max_depth: int,
    ) -> list[PendingForwardNode]:
        nodes: list[PendingForwardNode] = []
        for raw_node in messages:
            sender_obj = raw_node.get("sender", {}) if isinstance(raw_node, dict) else {}
            sender_uin = str(
                sender_obj.get("user_id")
                or sender_obj.get("uin")
                or raw_node.get("user_id")
                or raw_node.get("uin")
                or ""
            )
            sender_name = str(
                sender_obj.get("card")
                or sender_obj.get("nickname")
                or raw_node.get("nickname")
                or raw_node.get("name")
                or sender_uin
                or "未知用户"
            )
            content_chain = []
            if isinstance(raw_node, dict):
                content_chain = (
                    raw_node.get("content")
                    or raw_node.get("message")
                    or raw_node.get("raw_message")
                    or []
                )
            segments = await self._build_forward_segments_from_chain(
                event,
                content_chain,
                forward_loader=forward_loader,
                current_depth=current_depth,
                max_depth=max_depth,
            )
            if not segments:
                segments = [PendingForwardSegment(type="text", text="[空消息]")]
            nodes.append(
                PendingForwardNode(
                    sender_uin=sender_uin,
                    sender_name=sender_name,
                    segments=segments,
                )
            )
        return nodes

    async def _build_forward_segments_from_chain(
        self,
        event: Any,
        content_chain: Any,
        *,
        forward_loader: ForwardMessageLoader,
        current_depth: int,
        max_depth: int,
    ) -> list[PendingForwardSegment]:
        if isinstance(content_chain, str):
            if "[CQ:" in content_chain:
                # 节点内容为 CQ 码字符串时先解析（message_format=string 的实现）
                content_chain = parse_cq_message(content_chain)
            else:
                return [PendingForwardSegment(type="text", text=content_chain)] if content_chain else []
        if isinstance(content_chain, dict):
            content_chain = [content_chain]
        elif not isinstance(content_chain, list):
            content_chain = [content_chain] if content_chain else []

        segments: list[PendingForwardSegment] = []
        for item in content_chain:
            if isinstance(item, str):
                if item:
                    segments.append(PendingForwardSegment(type="text", text=item))
                continue

            seg_type = ""
            seg_data: dict[str, Any] = {}
            payload: dict[str, Any] | None = None
            if isinstance(item, dict):
                seg_type = str(item.get("type") or "").lower()
                seg_data = item.get("data", {}) or {}
                payload = item
            else:
                seg_type = str(getattr(item, "type", "") or "").lower()
                seg_data = getattr(item, "data", {}) or {}

            if seg_type in {"text", "plain"}:
                text = str(seg_data.get("text") or "")
                if text:
                    segments.append(PendingForwardSegment(type="text", text=text))
                continue

            if seg_type == "image":
                image = await self._prepare_image_from_onebot_data(event, seg_data)
                if image is not None:
                    segments.append(PendingForwardSegment(type="image", image=image))
                else:
                    segments.append(PendingForwardSegment(type="text", text="[图片]"))
                continue

            if seg_type in {"record", "video", "file"}:
                media = await self._prepare_media_from_onebot_data(event, seg_data, seg_type)
                if media is not None:
                    segments.append(PendingForwardSegment(type=seg_type, media=media))
                else:
                    segments.append(PendingForwardSegment(type="text", text=self._placeholder_for_media(seg_type)))
                continue

            if seg_type == "face":
                face_id = int(seg_data.get("id") or payload.get("id") or 0) if isinstance(payload, dict) else 0
                if face_id:
                    segments.append(PendingForwardSegment(type="face", face_id=face_id))
                continue

            if seg_type == "at":
                qq = str(seg_data.get("qq") or seg_data.get("user_id") or payload.get("qq") or "")
                name = str(seg_data.get("name") or payload.get("name") or "")
                if qq:
                    segments.append(PendingForwardSegment(type="at", qq=qq, name=name))
                continue

            if seg_type == "forward":
                nested_nodes = await self._prepare_nested_forward_nodes(
                    event,
                    seg_data,
                    payload or {},
                    forward_loader=forward_loader,
                    current_depth=current_depth,
                    max_depth=max_depth,
                )
                if nested_nodes:
                    segments.append(PendingForwardSegment(type="nodes", nodes=nested_nodes))
                else:
                    segments.append(PendingForwardSegment(type="text", text="[聊天记录]"))
                continue

            placeholder = self._placeholder_for_unknown(seg_type)
            if placeholder:
                segments.append(PendingForwardSegment(type="text", text=placeholder))

        return segments

    async def _prepare_nested_forward_nodes(
        self,
        event: Any,
        seg_data: dict[str, Any],
        payload: dict[str, Any],
        *,
        forward_loader: ForwardMessageLoader,
        current_depth: int,
        max_depth: int,
    ) -> list[PendingForwardNode]:
        if current_depth >= max_depth:
            return []

        nested_payload = None
        if isinstance(seg_data, dict) and isinstance(seg_data.get("messages"), list):
            nested_payload = {"messages": seg_data.get("messages", [])}
        elif isinstance(payload.get("messages"), list):
            nested_payload = {"messages": payload.get("messages", [])}

        nested_id = ""
        for key in ("id", "resid", "forward_id"):
            value = seg_data.get(key) or payload.get(key)
            if value:
                nested_id = str(value)
                break

        messages = await forward_loader(
            event,
            forward_id=nested_id or None,
            forward_payload=nested_payload,
        )
        if not messages:
            return []
        return await self._build_forward_nodes_from_messages(
            event,
            messages,
            forward_loader=forward_loader,
            current_depth=current_depth + 1,
            max_depth=max_depth,
        )

    async def _collect_from_onebot_message(self, event: Any, message: Any) -> list[PreparedImage]:
        if not isinstance(message, list):
            return []
        images: list[PreparedImage] = []
        for segment in message:
            try:
                if (segment.get("type") or "").lower() != "image":
                    continue
                data = segment.get("data") or {}
                prepared = await self._prepare_image_from_onebot_data(event, data)
                if prepared is not None:
                    images.append(prepared)
            except Exception as exc:
                logger.warning(f"处理回复图片失败: {exc}")
        return images

    async def _collect_from_segments(self, event: Any) -> list[PreparedImage]:
        images: list[PreparedImage] = []
        try:
            segments = list(event.get_messages())
        except Exception as exc:
            logger.warning(f"读取消息链失败: {exc}")
            return images

        for segment in segments:
            try:
                if Comp is None or not isinstance(segment, Comp.Image):
                    continue
                prepared = await self._prepare_image_from_component(event, segment)
                if prepared is not None:
                    images.append(prepared)
            except Exception as exc:
                logger.warning(f"处理当前消息图片失败: {exc}")
        return images

    async def _prepare_image_from_component(self, event: Any, segment: Any) -> PreparedImage | None:
        url = getattr(segment, "url", None)
        file_or_path = getattr(segment, "file", None) or getattr(segment, "path", None)
        if url and str(url).startswith(("http://", "https://")):
            return await self._prepare_image_from_url(str(url))
        if file_or_path:
            prepared = await self._prepare_image_from_fs(str(file_or_path))
            if prepared is not None:
                return prepared
            if event.get_platform_name() == "aiocqhttp":
                local_path = await self._resolve_napcat_image_path(event, str(file_or_path))
                if local_path:
                    return await self._prepare_image_from_fs(local_path)
        return None

    async def _prepare_image_from_onebot_data(self, event: Any, data: dict[str, Any]) -> PreparedImage | None:
        url = data.get("url") or data.get("image_url")
        if url and str(url).startswith(("http://", "https://")):
            prepared = await self._prepare_image_from_url(str(url))
            if prepared is not None:
                return prepared

        file_or_path = data.get("file") or data.get("path")
        if not file_or_path:
            return None

        prepared = await self._prepare_image_from_fs(str(file_or_path))
        if prepared is not None:
            return prepared

        local_path = await self._resolve_napcat_image_path(event, str(file_or_path))
        if local_path:
            return await self._prepare_image_from_fs(local_path)
        return None

    async def _prepare_media_from_onebot_data(
        self,
        event: Any,
        data: dict[str, Any],
        media_type: str,
    ) -> PreparedMedia | None:
        display_name = str(data.get("name") or data.get("fileName") or "") or f"{media_type}"
        url = self._extract_http_media_url(data)
        if url:
            prepared = await self._prepare_media_from_url(
                url,
                media_type=media_type,
                display_name=display_name,
            )
            if prepared is not None:
                return prepared

        file_or_path = data.get("file") or data.get("path")
        if not file_or_path:
            return None
        return await self._prepare_media_from_fs(
            str(file_or_path),
            media_type=media_type,
            display_name=display_name,
        )

    def _extract_http_media_url(self, data: dict[str, Any]) -> str:
        for key in ("url", "source_url", "src", "origin", "origin_url", "file"):
            value = data.get(key)
            if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
                return value.strip()
        return ""

    async def _prepare_image_from_url(self, url: str) -> PreparedImage | None:
        try:
            content, content_type = await self._fetch_bytes(url)
            return prepare_image(content, source=url, content_type=content_type)
        except Exception as exc:
            logger.warning(f"下载图片失败: {exc}")
            return None

    async def _prepare_image_from_fs(self, path: str) -> PreparedImage | None:
        try:
            file_path = self._normalize_path(path)
            if not file_path.exists():
                return None
            return prepare_image(file_path.read_bytes(), source=file_path.name)
        except Exception as exc:
            logger.warning(f"读取本地图片失败: {exc}")
            return None

    async def _prepare_media_from_url(
        self,
        url: str,
        *,
        media_type: str,
        display_name: str,
    ) -> PreparedMedia | None:
        try:
            content, content_type = await self._fetch_bytes(url)
            return prepare_media(
                content,
                media_type=media_type,
                source=url,
                content_type=content_type,
                display_name=display_name,
            )
        except Exception as exc:
            logger.warning(f"下载媒体失败({media_type}): {exc}")
            return None

    async def _prepare_media_from_fs(
        self,
        path: str,
        *,
        media_type: str,
        display_name: str,
    ) -> PreparedMedia | None:
        try:
            file_path = self._normalize_path(path)
            if not file_path.exists():
                return None
            return prepare_media(
                file_path.read_bytes(),
                media_type=media_type,
                source=file_path.name,
                display_name=display_name or file_path.name,
            )
        except Exception as exc:
            logger.warning(f"读取本地媒体失败({media_type}): {exc}")
            return None

    async def _fetch_bytes(self, url: str) -> tuple[bytes, str]:
        if self.http_client is not None:
            response = await self.http_client.get(url)
        else:
            import httpx

            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(url)
        if getattr(response, "status_code", 200) >= 400:
            raise ValueError(f"HTTP {getattr(response, 'status_code', 'unknown')}")
        headers = getattr(response, "headers", None) or {}
        content_type = str(headers.get("Content-Type") or "")
        return bytes(response.content), content_type

    def _normalize_path(self, path: str) -> Path:
        normalized = str(path or "")
        if normalized.startswith("file:///"):
            normalized = normalized[8:]
        elif normalized.startswith("file://"):
            normalized = normalized[7:]
        return Path(normalized)

    async def _resolve_napcat_image_path(self, event: Any, file_id: str) -> str | None:
        if event.get_platform_name() != "aiocqhttp":
            return None
        try:
            client = event.bot
            response = await client.api.call_action("get_image", file=str(file_id))
            payload = response.get("data") if isinstance(response, dict) and isinstance(response.get("data"), dict) else response
            return str(payload.get("file") or payload.get("path") or payload.get("file_path") or "") or None
        except Exception as exc:
            logger.info(f"get_image 回退失败: {exc}")
            return None

    def _strip_command_invocation(
        self, text: str, command_name: str, event_message_str: str = ""
    ) -> str:
        text = text.strip()
        if not text or not command_name:
            return text

        # 优先用 AstrBot 已剥离前缀的 message_str 定位指令，兼容任意唤醒形式
        if event_message_str.strip().startswith(command_name):
            idx = text.find(command_name)
            if idx >= 0:
                return text[idx + len(command_name) :].strip()

        if text.startswith(command_name):
            return text[len(command_name) :].strip()

        for prefix in self.wake_prefixes:
            if text.startswith(f"{prefix}{command_name}"):
                return text[len(prefix) + len(command_name) :].strip()
        return text

    def _placeholder_for_media(self, media_type: str) -> str:
        return {
            "record": "[语音]",
            "video": "[视频]",
            "file": "[文件]",
        }.get(media_type, "[附件]")

    def _placeholder_for_unknown(self, seg_type: str) -> str:
        if not seg_type:
            return ""
        return {
            "json": "[卡片消息]",
            "share": "[分享]",
            "reply": "[回复]",
            "music": "[音乐]",
        }.get(seg_type, f"[{seg_type}]")

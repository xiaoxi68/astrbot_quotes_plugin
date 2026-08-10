from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
    import astrbot.api.message_components as Comp
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)
    Comp = None  # type: ignore

try:
    from .constants import DUPLICATE_IMAGE_MESSAGE, DUPLICATE_QUOTE_MESSAGE
    from .models import CommandResponse, ForwardNode, ForwardSegment, ImageSignature, Quote
    from .napcat_service import NapcatService
    from .renderer import QuoteRenderer
    from .store import QuoteRepository
    from .utils import (
        is_valid_qq,
        make_session_key,
        normalize_quote_text,
        random_id,
        sha256_bytes,
    )
except ImportError:  # pragma: no cover
    from constants import DUPLICATE_IMAGE_MESSAGE, DUPLICATE_QUOTE_MESSAGE
    from models import CommandResponse, ForwardNode, ForwardSegment, ImageSignature, Quote
    from napcat_service import NapcatService
    from renderer import QuoteRenderer
    from store import QuoteRepository
    from utils import (
        is_valid_qq,
        make_session_key,
        normalize_quote_text,
        random_id,
        sha256_bytes,
    )


class QuoteService:
    def __init__(
        self,
        repository: QuoteRepository,
        image_service: Any,
        napcat_service: NapcatService,
        renderer: QuoteRenderer,
        http_client: Any | None,
        *,
        global_mode: bool,
        text_mode: bool,
        render_cache: bool,
        image_signature_use_group: bool,
        blacklist: set[str],
    ):
        self.repository = repository
        self.image_service = image_service
        self.napcat_service = napcat_service
        self.renderer = renderer
        self.http_client = http_client
        self.global_mode = global_mode
        self.text_mode = text_mode
        self.render_cache = render_cache
        self.image_signature_use_group = image_signature_use_group
        self.blacklist = blacklist

    async def add_quote(self, event: Any, uid: str = "") -> CommandResponse:
        session_key = make_session_key(event.get_group_id(), event.get_sender_id())
        reply_message_id = self.get_reply_message_id(event)
        reply_payload = await self.napcat_service.fetch_onebot_message(event, reply_message_id)
        reply_sender = reply_payload.get("sender") or {}
        reply_qq = str(reply_sender.get("user_id") or reply_sender.get("qq") or "")
        explicit_qq = uid.strip() if is_valid_qq(uid) else ""
        mention_qq = self.extract_at_qq(event) or ""

        forward_id, forward_payload = self.napcat_service.extract_forward_reference(reply_payload.get("message"))
        if forward_id or forward_payload:
            return await self._add_forward_quote(
                event,
                session_key=session_key,
                reply_qq=reply_qq,
                explicit_qq=explicit_qq,
                mention_qq=mention_qq,
                forward_id=forward_id,
                forward_payload=forward_payload,
            )

        reply_segments = await self.image_service.build_reply_segments(event, reply_payload.get("message"))
        current_segments = await self.image_service.build_current_segments(
            event,
            command_name="上传",
            explicit_qq=explicit_qq,
        )
        all_segments = self._normalize_pending_segments([*reply_segments, *current_segments])
        if not all_segments:
            logger.info(f"上传语录失败: 未获取到可收录内容, session={session_key}, sender={event.get_sender_id()}")
            return CommandResponse(kind="plain", text="未获取到被回复消息内容或图片，请确认已正确回复对方的消息或附带图片。")

        if explicit_qq:
            target_qq = explicit_qq
        elif mention_qq:
            target_qq = mention_qq
        elif reply_qq:
            target_qq = reply_qq
        elif current_segments:
            target_qq = str(event.get_sender_id())
        else:
            target_qq = ""

        if target_qq and target_qq in self.blacklist:
            logger.info(f"上传语录已忽略: 目标用户在黑名单中, session={session_key}, target_qq={target_qq}")
            return CommandResponse(kind="plain", text="该用户在语录黑名单中，本次语录已忽略。")

        target_name = await self.napcat_service.resolve_user_name(event, target_qq) if target_qq else ""
        if not target_name:
            target_name = target_qq or "未知用户"

        from time import time

        duplicate_fingerprint = self._fingerprint_pending_standard_segments(all_segments)
        if target_qq and duplicate_fingerprint and self._has_duplicate_quote(
            session_key,
            target_qq=target_qq,
            fingerprint=duplicate_fingerprint,
        ):
            logger.info(f"上传语录被拒绝: 内容重复, session={session_key}, target_qq={target_qq}")
            return CommandResponse(kind="plain", text=DUPLICATE_QUOTE_MESSAGE)

        quote = Quote(
            id=random_id("q_"),
            qq=str(target_qq or ""),
            name=str(target_name),
            text=self._plain_text_from_pending_segments(all_segments),
            created_by=str(event.get_sender_id()),
            created_at=time(),
            group=session_key,
        )
        result = await self.repository.create_quote_with_segments(session_key, quote, all_segments)
        if result.duplicate:
            logger.info(f"上传语录被拒绝: 图片重复, session={session_key}, target_qq={target_qq}")
            return CommandResponse(kind="plain", text=result.message or DUPLICATE_IMAGE_MESSAGE)

        image_count = len([segment for segment in all_segments if segment.type == "image"])
        logger.info(
            "上传语录成功: "
            f"quote_id={quote.id}, session={session_key}, target_qq={target_qq}, "
            f"segments={len(all_segments)}, images={image_count}"
        )
        if image_count:
            return CommandResponse(kind="plain", text=f"已收录 {quote.name} 的语录，并保存 {image_count} 张图片。")
        return CommandResponse(kind="plain", text=f"已收录 {quote.name} 的语录：{quote.text}")

    async def _add_forward_quote(
        self,
        event: Any,
        *,
        session_key: str,
        reply_qq: str,
        explicit_qq: str,
        mention_qq: str,
        forward_id: str | None,
        forward_payload: dict[str, Any] | None,
    ) -> CommandResponse:
        nodes = await self.image_service.build_forward_nodes(
            event,
            forward_id=forward_id,
            forward_payload=forward_payload,
            forward_loader=self.napcat_service.fetch_forward_messages,
        )
        if not nodes:
            logger.info(f"上传聊天记录语录失败: 未获取到 forward 节点, session={session_key}, sender={event.get_sender_id()}")
            return CommandResponse(kind="plain", text="未获取到可用的聊天记录内容，请确认回复的是 QQ 合并转发消息。")

        if explicit_qq:
            target_qq = explicit_qq
        elif mention_qq:
            target_qq = mention_qq
        elif reply_qq:
            target_qq = reply_qq
        else:
            target_qq = str(event.get_sender_id())

        if target_qq and target_qq in self.blacklist:
            logger.info(f"上传聊天记录语录已忽略: 目标用户在黑名单中, session={session_key}, target_qq={target_qq}")
            return CommandResponse(kind="plain", text="该用户在语录黑名单中，本次语录已忽略。")

        target_name = await self.napcat_service.resolve_user_name(event, target_qq) if target_qq else ""
        if not target_name:
            target_name = target_qq or "未知用户"

        from time import time

        duplicate_fingerprint = self._fingerprint_pending_forward_nodes(nodes)
        if target_qq and duplicate_fingerprint and self._has_duplicate_quote(
            session_key,
            target_qq=target_qq,
            fingerprint=duplicate_fingerprint,
        ):
            logger.info(f"上传聊天记录语录被拒绝: 内容重复, session={session_key}, target_qq={target_qq}")
            return CommandResponse(kind="plain", text=DUPLICATE_QUOTE_MESSAGE)

        quote = Quote(
            id=random_id("q_"),
            qq=str(target_qq or ""),
            name=str(target_name),
            text=self._flatten_forward_nodes(nodes),
            created_by=str(event.get_sender_id()),
            created_at=time(),
            group=session_key,
            kind="forward",
        )
        result = await self.repository.create_quote_with_forward_nodes(session_key, quote, nodes)
        if result.duplicate:
            logger.info(f"上传聊天记录语录被拒绝: 图片重复, session={session_key}, target_qq={target_qq}")
            return CommandResponse(kind="plain", text=result.message or DUPLICATE_IMAGE_MESSAGE)

        message_count = self._count_forward_messages(nodes)
        logger.info(
            "上传聊天记录语录成功: "
            f"quote_id={quote.id}, session={session_key}, target_qq={target_qq}, messages={message_count}"
        )
        return CommandResponse(
            kind="plain",
            text=f"已收录 {quote.name} 的聊天记录语录，共 {message_count} 条消息。",
        )

    async def random_quote(self, event: Any, uid: str = "", silent_if_empty: bool = False) -> CommandResponse | None:
        session_key = make_session_key(event.get_group_id(), event.get_sender_id())
        target_session = None if self.global_mode else session_key
        explicit_qq = uid.strip() if is_valid_qq(uid) else ""
        only_qq = explicit_qq or (self.extract_at_qq(event) or "")
        quote = await self.repository.random_quote(target_session, qq=only_qq or None)
        if quote is None:
            if not silent_if_empty:
                logger.info(
                    "随机语录未命中: "
                    f"session={target_session or 'global'}, target_qq={only_qq or '*'}"
                )
            if silent_if_empty:
                return None
            if only_qq:
                text = "这个用户还没有语录哦~" if self.global_mode else "这个用户在本会话还没有语录哦~"
            else:
                text = "还没有语录，先用 上传 保存一条吧~" if self.global_mode else "本会话还没有语录，先用 上传 保存一条吧~"
            return CommandResponse(kind="plain", text=text)

        logger.info(f"随机语录命中: quote_id={quote.id}, session={quote.group}, target_qq={only_qq or '*'}")
        chain = self.build_quote_chain(quote)
        if chain:
            return CommandResponse(
                kind="chain",
                chain=chain,
                quote_id=quote.id,
                delete_fingerprint=await self.build_delete_fingerprint(quote, chain=chain),
                delete_image_signatures=self.build_delete_image_signatures(quote),
            )

        if self.text_mode or quote.kind == "forward" or self.renderer.should_fallback_to_plain(quote):
            text = self._quote_plain_fallback(quote)
            return CommandResponse(
                kind="plain",
                text=text,
                quote_id=quote.id,
                delete_fingerprint=self._fingerprint_plain_text(text),
            )

        store = self.repository.get_store(quote.group)
        cache_path = store.cache_path(quote.id)
        if self.render_cache and cache_path.exists():
            return CommandResponse(
                kind="image_path",
                path=str(cache_path),
                quote_id=quote.id,
                delete_fingerprint=await self._fingerprint_image_path(cache_path),
            )

        signature = await self.napcat_service.resolve_signature_name(
            event,
            quote,
            use_group_signature=self.image_signature_use_group,
        )
        rendered_url = await self.renderer.render_quote_image(quote, signature)
        cached = await self.cache_rendered_result(rendered_url, cache_path)
        if cached:
            return CommandResponse(
                kind="image_path",
                path=str(cache_path),
                quote_id=quote.id,
                delete_fingerprint=await self._fingerprint_image_path(cache_path),
            )
        return CommandResponse(
            kind="image_url",
            url=rendered_url,
            quote_id=quote.id,
            delete_fingerprint=await self._fingerprint_image_url(rendered_url),
        )

    async def delete_quote(self, quote_id: str) -> bool:
        return await self.repository.delete_quote(quote_id)

    async def resolve_delete_target(self, event: Any) -> str | None:
        session_key = make_session_key(event.get_group_id(), event.get_sender_id())
        reply_message_id = self.get_reply_message_id(event)
        if not reply_message_id:
            logger.info("删除语录定位失败: 当前消息没有 Reply 段。")
            return None

        reply_payload = await self.napcat_service.fetch_onebot_message(event, reply_message_id)
        if not reply_payload:
            logger.info(f"删除语录定位失败: get_msg 未返回被回复消息, message_id={reply_message_id}")
            return None

        sender = reply_payload.get("sender") or {}
        sender_id = str(sender.get("user_id") or sender.get("qq") or "")
        self_id = self._self_id_of_event(event)
        if self_id and sender_id and sender_id != self_id:
            logger.info(
                "删除语录定位失败: 被回复消息不是机器人发送。"
                f" sender_id={sender_id}, self_id={self_id}, message_id={reply_message_id}"
            )
            return None

        fingerprints, image = await self._fingerprint_from_reply_payload(event, reply_payload)
        if not fingerprints:
            logger.info(f"删除语录定位失败: 无法从被回复消息计算指纹, message_id={reply_message_id}")
            return None

        replied_at = float(reply_payload.get("time") or 0)
        for fingerprint in fingerprints:
            quote_id = self.repository.find_sent_quote_id(
                session_key,
                fingerprint=fingerprint,
                replied_at=replied_at,
            )
            if quote_id:
                logger.info(f"删除语录定位成功: 精确指纹匹配 quote_id={quote_id}, session={session_key}")
                return quote_id
        if image is not None:
            legacy_quote_id = self.repository.find_sent_quote_id(
                session_key,
                fingerprint=self._fingerprint_image_chain_sha(image.sha256),
                replied_at=replied_at,
            )
            if legacy_quote_id:
                logger.info(
                    "删除语录定位成功: 兼容旧版纯图片链式指纹匹配 "
                    f"quote_id={legacy_quote_id}, session={session_key}"
                )
                return legacy_quote_id
            near_quote_id = self.repository.find_sent_quote_id_by_image_signature(
                session_key,
                image=image,
                replied_at=replied_at,
            )
            if near_quote_id:
                logger.info(
                    "删除语录定位成功: 纯图片感知哈希近似匹配 "
                    f"quote_id={near_quote_id}, session={session_key}"
                )
                return near_quote_id
            logger.info(
                "删除语录定位失败: 单图消息未匹配 sent_index。"
                f" session={session_key}, sha256={image.sha256[:12]}, dhash={image.dhash[:12]}"
            )
            return None
        logger.info(f"删除语录定位失败: 指纹未匹配 sent_index, session={session_key}")
        return None

    def build_quote_chain(self, quote: Quote) -> list[Any]:
        if Comp is None:
            return []
        if quote.kind == "forward":
            return self.build_forward_quote_chain(quote)
        return self.build_standard_quote_chain(quote)

    def build_standard_quote_chain(self, quote: Quote) -> list[Any]:
        if not quote.segments:
            return []
        has_image = any(segment.type == "image" and segment.asset_id for segment in quote.segments)
        if not has_image:
            return []

        chain: list[Any] = []
        for segment in quote.segments:
            if segment.type == "text":
                text = str(segment.text or "").strip()
                if text:
                    chain.append(Comp.Plain(text))
                continue
            if segment.type != "image" or not segment.asset_id:
                continue
            asset = self.repository.find_asset(quote.group, segment.asset_id)
            if asset is None:
                continue
            abs_path = self.repository.root / asset.rel_path
            if abs_path.exists():
                chain.append(Comp.Image.fromFileSystem(str(abs_path)))
        return chain

    def build_forward_quote_chain(self, quote: Quote) -> list[Any]:
        if not quote.forward_nodes:
            return []
        nodes = [self._build_forward_node_component(quote.group, node) for node in quote.forward_nodes]
        nodes = [node for node in nodes if node is not None]
        if not nodes:
            return []
        return [Comp.Nodes(nodes=nodes)]

    def _build_forward_node_component(self, session_key: str, node: ForwardNode) -> Any | None:
        content = self._build_forward_segment_components(session_key, node.segments)
        if not content:
            content = [Comp.Plain("[空消息]")]
        try:
            return Comp.Node(
                uin=str(node.sender_uin or "0"),
                name=str(node.sender_name or node.sender_uin or "未知用户"),
                content=content,
            )
        except Exception as exc:
            logger.info(f"构造 forward 节点失败: {exc}")
            return None

    def _build_forward_segment_components(self, session_key: str, segments: list[ForwardSegment]) -> list[Any]:
        content: list[Any] = []
        for segment in segments:
            if segment.type == "text":
                text = str(segment.text or "")
                if text:
                    content.append(Comp.Plain(text))
                continue

            if segment.type == "image" and segment.asset_id:
                asset = self.repository.find_asset(session_key, segment.asset_id)
                if asset is not None:
                    abs_path = self.repository.root / asset.rel_path
                    if abs_path.exists():
                        content.append(Comp.Image.fromFileSystem(str(abs_path)))
                        continue
                content.append(Comp.Plain("[图片]"))
                continue

            if segment.type in {"record", "video", "file"} and segment.asset_id:
                media_asset = self.repository.find_media_asset(session_key, segment.asset_id)
                if media_asset is None:
                    content.append(Comp.Plain(self._placeholder_for_media(segment.type)))
                    continue
                abs_path = self.repository.root / media_asset.rel_path
                if not abs_path.exists():
                    content.append(Comp.Plain(self._placeholder_for_media(segment.type)))
                    continue
                component = self._build_media_component(segment.type, abs_path, media_asset.display_name)
                if component is None:
                    content.append(Comp.Plain(self._placeholder_for_media(segment.type)))
                else:
                    content.append(component)
                continue

            if segment.type == "face" and segment.face_id:
                try:
                    content.append(Comp.Face(id=segment.face_id))
                except Exception:
                    content.append(Comp.Plain("[表情]"))
                continue

            if segment.type == "at" and segment.qq:
                try:
                    content.append(Comp.At(qq=segment.qq, name=segment.name or ""))
                except Exception:
                    content.append(Comp.Plain(f"@{segment.name or segment.qq}"))
                continue

            if segment.type == "nodes":
                nested_nodes = [self._build_forward_node_component(session_key, node) for node in segment.nodes]
                nested_nodes = [node for node in nested_nodes if node is not None]
                if nested_nodes:
                    content.append(Comp.Nodes(nodes=nested_nodes))
                else:
                    content.append(Comp.Plain("[聊天记录]"))
                continue

            placeholder = self._placeholder_for_unknown(segment.type)
            if placeholder:
                content.append(Comp.Plain(placeholder))
        return content

    def _build_media_component(self, media_type: str, path: Path, display_name: str) -> Any | None:
        try:
            if media_type == "record":
                return Comp.Record.fromFileSystem(str(path))
            if media_type == "video":
                return Comp.Video.fromFileSystem(str(path))
            if media_type == "file":
                return Comp.File(file=str(path), name=display_name or path.name)
        except Exception as exc:
            logger.info(f"构造媒体组件失败({media_type}): {exc}")
        return None

    async def cache_rendered_result(self, rendered_url: str, cache_path: Path) -> bool:
        if not self.render_cache:
            return False
        try:
            if rendered_url.startswith("file://"):
                from urllib.parse import unquote, urlparse

                parsed = urlparse(rendered_url)
                local_path = Path(unquote(parsed.path))
                if local_path.exists():
                    cache_path.write_bytes(local_path.read_bytes())
                    return True
            elif rendered_url.startswith("http") and self.http_client is not None:
                response = await self.http_client.get(rendered_url)
                if getattr(response, "status_code", 200) < 400:
                    cache_path.write_bytes(bytes(response.content))
                    return True
        except Exception as exc:
            logger.info(f"渲染缓存落盘失败: {exc}")
        return False

    def extract_at_qq(self, event: Any) -> str | None:
        try:
            for segment in event.get_messages():
                if Comp is not None and isinstance(segment, Comp.At):
                    for field in ("qq", "target", "uin", "user_id", "id"):
                        value = getattr(segment, field, None)
                        if value:
                            return str(value)
        except Exception as exc:
            logger.warning(f"解析 @ 失败: {exc}")
        return None

    def get_reply_message_id(self, event: Any) -> str | None:
        try:
            for segment in event.get_messages():
                if Comp is not None and isinstance(segment, Comp.Reply):
                    value = (
                        getattr(segment, "message_id", None)
                        or getattr(segment, "id", None)
                        or getattr(segment, "reply", None)
                        or getattr(segment, "msgId", None)
                    )
                    if value:
                        return str(value)
        except Exception as exc:
            logger.warning(f"解析 Reply 段失败: {exc}")
        return None

    def _normalize_pending_segments(self, segments: list[Any]) -> list[Any]:
        normalized: list[Any] = []
        for segment in segments:
            if segment.type == "text":
                text = normalize_quote_text(str(segment.text or ""))
                if text:
                    segment.text = text
                    normalized.append(segment)
                continue
            if segment.type == "image" and getattr(segment, "image", None) is not None:
                normalized.append(segment)
        return normalized

    def _plain_text_from_pending_segments(self, segments: list[Any]) -> str:
        parts = [str(segment.text or "") for segment in segments if segment.type == "text" and str(segment.text or "").strip()]
        return " ".join(parts).strip()

    def _flatten_forward_nodes(self, nodes: list[Any]) -> str:
        lines: list[str] = []
        for node in nodes:
            sender = str(getattr(node, "sender_name", "") or getattr(node, "sender_uin", "") or "未知用户")
            parts: list[str] = []
            for segment in getattr(node, "segments", []):
                if segment.type == "text" and segment.text:
                    parts.append(str(segment.text))
                elif segment.type == "image":
                    parts.append("[图片]")
                elif segment.type == "record":
                    parts.append("[语音]")
                elif segment.type == "video":
                    parts.append("[视频]")
                elif segment.type == "file":
                    parts.append("[文件]")
                elif segment.type == "face" and getattr(segment, "face_id", 0):
                    parts.append("[表情]")
                elif segment.type == "at" and getattr(segment, "qq", ""):
                    parts.append(f"@{segment.name or segment.qq}")
                elif segment.type == "nodes":
                    parts.append("[聊天记录]")
            content = "".join(parts).strip()
            if content:
                lines.append(f"{sender}：{content}")
        return "\n".join(lines).strip()

    def _count_forward_messages(self, nodes: list[Any]) -> int:
        total = 0
        for node in nodes:
            total += 1
            for segment in getattr(node, "segments", []):
                if segment.type == "nodes":
                    total += self._count_forward_messages(segment.nodes)
        return total

    def _quote_plain_fallback(self, quote: Quote) -> str:
        if quote.kind == "forward":
            return quote.text or f"{quote.name} 的聊天记录语录"
        return f"「{quote.text}」 — {quote.name}"

    async def build_delete_fingerprint(self, quote: Quote, *, chain: list[Any] | None = None) -> str:
        if quote.kind == "forward":
            return self._fingerprint_forward_nodes(quote.group, quote.forward_nodes)

        single_image = self._single_standard_image_signature(quote)
        if single_image is not None:
            logger.info(f"语录删除指纹: 使用纯图片指纹 quote_id={quote.id}, sha256={single_image.sha256[:12]}")
            return self._fingerprint_image_sha(single_image.sha256)

        if chain:
            fingerprint = await self._fingerprint_standard_chain(chain)
            if fingerprint:
                return fingerprint

        return self._fingerprint_standard_quote(quote)

    def build_delete_image_signatures(self, quote: Quote) -> list[ImageSignature]:
        signature = self._single_standard_image_signature(quote)
        return [signature] if signature is not None else []

    async def _fingerprint_from_reply_payload(
        self,
        event: Any,
        reply_payload: dict[str, Any],
    ) -> tuple[list[str], Any | None]:
        message = reply_payload.get("message")
        forward_id, forward_payload = self.napcat_service.extract_forward_reference(message)
        if forward_id or forward_payload:
            nodes = await self.image_service.build_forward_nodes(
                event,
                forward_id=forward_id,
                forward_payload=forward_payload,
                forward_loader=self.napcat_service.fetch_forward_messages,
            )
            if nodes:
                return self._forward_delete_fingerprints(nodes)

        segments = await self.image_service.build_reply_segments(event, message)
        normalized = self._normalize_pending_segments(segments)
        if not normalized:
            return [], None

        if len(normalized) == 1 and normalized[0].type == "image" and normalized[0].image is not None:
            return [self._fingerprint_image_sha(normalized[0].image.sha256)], normalized[0].image

        return [
            self._hash_payload(
                {
                    "kind": "chain",
                    "parts": [
                        self._pending_segment_payload(segment)
                        for segment in normalized
                    ],
                }
            )
        ], None

    def _forward_delete_fingerprints(self, nodes: list[Any]) -> tuple[list[str], Any | None]:
        # 长文本/图文语录会被 AstrBot 自动包成合并转发（AstrNa 可能再拆成多节点），
        # 删除定位需按发送时的指纹方案重建候选，而不是只算 forward 指纹。
        # 注意：相邻文本段必须按原始文本先拼接再统一规整——逐段 strip 会在
        # AstrNa 的切分边界丢字符（实测 node1/node2 首字符是空格），导致指纹不匹配。
        parts: list[dict[str, Any]] = []
        images: list[Any] = []
        raw_texts: list[str] = []
        text_buf: list[str] = []

        def flush_text() -> None:
            if not text_buf:
                return
            merged = self._canonical_text("".join(text_buf))
            if merged:
                parts.append({"type": "text", "text": merged})
            text_buf.clear()

        for node in nodes:
            for segment in getattr(node, "segments", []) or []:
                if segment.type == "text":
                    raw_texts.append(str(segment.text or ""))
                    text_buf.append(str(segment.text or ""))
                    continue
                flush_text()
                payload = self._pending_forward_segment_payload(segment)
                if payload is not None:
                    parts.append(payload)
                if segment.type == "image" and getattr(segment, "image", None) is not None:
                    images.append(segment.image)
        flush_text()

        candidates: list[str] = []
        if parts:
            # 图文链语录发送时的 chain 指纹
            candidates.append(self._hash_payload({"kind": "chain", "parts": parts}))
        if parts and all(part.get("type") == "text" for part in parts):
            # 纯文本兜底语录发送时指纹（按原始文本拼接，保持与发送内容逐字节一致）
            plain = self._fingerprint_plain_text("".join(raw_texts))
            if plain:
                candidates.append(plain)
        for image in images:
            # 单图语录发送时的 image 指纹
            candidates.append(self._fingerprint_image_sha(image.sha256))
        forward_fingerprint = self._fingerprint_pending_forward_nodes(nodes)
        if forward_fingerprint:
            # kind == "forward" 的聊天记录语录保持原方案
            candidates.append(forward_fingerprint)

        unique = list(dict.fromkeys(item for item in candidates if item))
        return unique, (images[0] if images else None)

    def _fingerprint_plain_text(self, text: str) -> str:
        normalized = self._canonical_text(normalize_quote_text(text))
        if not normalized:
            return ""
        return self._hash_payload(
            {"kind": "chain", "parts": [{"type": "text", "text": normalized}]}
        )

    def _fingerprint_standard_quote(self, quote: Quote) -> str:
        if not quote.segments:
            return ""
        parts = [self._stored_standard_segment_payload(quote.group, segment) for segment in quote.segments]
        parts = [item for item in parts if item is not None]
        if not parts:
            return ""
        return self._hash_payload({"kind": "chain", "parts": parts})

    def _fingerprint_pending_standard_segments(self, segments: list[Any]) -> str:
        parts = [self._pending_segment_payload(segment) for segment in segments]
        parts = [item for item in parts if item is not None]
        if not parts:
            return ""
        return self._hash_payload({"kind": "chain", "parts": parts})

    async def _fingerprint_standard_chain(self, chain: list[Any]) -> str:
        parts: list[dict[str, Any]] = []
        for component in chain:
            payload = await self._component_payload(component)
            if payload is not None:
                parts.append(payload)
        if not parts:
            return ""
        return self._hash_payload({"kind": "chain", "parts": parts})

    async def _fingerprint_image_path(self, path: Path) -> str:
        try:
            if path.exists():
                return self._fingerprint_image_sha(sha256_bytes(path.read_bytes()))
        except Exception as exc:
            logger.info(f"计算语录图片指纹失败: {exc}")
        return ""

    async def _fingerprint_image_url(self, url: str) -> str:
        if not url:
            return ""
        try:
            if url.startswith("file://"):
                from urllib.parse import unquote, urlparse

                parsed = urlparse(url)
                local_path = Path(unquote(parsed.path))
                return await self._fingerprint_image_path(local_path)

            if url.startswith("http") and self.http_client is not None:
                response = await self.http_client.get(url)
                if getattr(response, "status_code", 200) < 400:
                    return self._fingerprint_image_sha(sha256_bytes(bytes(response.content)))
        except Exception as exc:
            logger.info(f"计算语录图片 URL 指纹失败: {exc}")
        return ""

    async def _component_payload(self, component: Any) -> dict[str, Any] | None:
        if Comp is None:
            return None

        if isinstance(component, Comp.Plain):
            text = self._canonical_text(str(getattr(component, "text", "") or ""))
            return {"type": "text", "text": text} if text else None

        if isinstance(component, Comp.Image):
            image_hash = await self._hash_component_file(component)
            return {"type": "image", "sha256": image_hash} if image_hash else None

        if isinstance(component, Comp.Record):
            media_hash = await self._hash_component_file(component)
            return {"type": "record", "sha256": media_hash} if media_hash else {"type": "record", "text": "[语音]"}

        if isinstance(component, Comp.Video):
            media_hash = await self._hash_component_file(component)
            return {"type": "video", "sha256": media_hash} if media_hash else {"type": "video", "text": "[视频]"}

        if isinstance(component, Comp.File):
            media_hash = await self._hash_component_file(component)
            if media_hash:
                return {"type": "file", "sha256": media_hash}
            return {"type": "file", "name": str(getattr(component, "name", "") or "")}

        if isinstance(component, Comp.At):
            return {
                "type": "at",
                "qq": str(getattr(component, "qq", "") or ""),
                "name": str(getattr(component, "name", "") or ""),
            }

        if isinstance(component, Comp.Face):
            return {"type": "face", "face_id": int(getattr(component, "id", 0) or 0)}

        if isinstance(component, Comp.Node):
            nested = await self._fingerprint_node_component(component)
            return nested

        if isinstance(component, Comp.Nodes):
            nested_nodes = []
            for node in list(getattr(component, "nodes", []) or []):
                node_payload = await self._fingerprint_node_component(node)
                if node_payload is not None:
                    nested_nodes.append(node_payload)
            return {"type": "nodes", "nodes": nested_nodes} if nested_nodes else None

        return None

    async def _fingerprint_node_component(self, node: Any) -> dict[str, Any] | None:
        content = []
        for component in list(getattr(node, "content", []) or []):
            payload = await self._component_payload(component)
            if payload is not None:
                content.append(payload)
        return {
            "sender_uin": str(getattr(node, "uin", "") or ""),
            "sender_name": str(getattr(node, "name", "") or ""),
            "segments": content,
        }

    async def _hash_component_file(self, component: Any) -> str:
        for attr in ("path", "file"):
            raw_value = getattr(component, attr, None)
            path_hash = await self._hash_local_or_remote_file(raw_value)
            if path_hash:
                return path_hash
        return ""

    async def _hash_local_or_remote_file(self, raw_value: Any) -> str:
        value = str(raw_value or "").strip()
        if not value:
            return ""
        try:
            if value.startswith("file:///"):
                value = value[8:]
            elif value.startswith("file://"):
                value = value[7:]
            path = Path(value)
            if path.exists():
                return sha256_bytes(path.read_bytes())
            if value.startswith("http") and self.http_client is not None:
                response = await self.http_client.get(value)
                if getattr(response, "status_code", 200) < 400:
                    return sha256_bytes(bytes(response.content))
        except Exception as exc:
            logger.info(f"计算媒体文件指纹失败: {exc}")
        return ""

    def _fingerprint_image_sha(self, sha_value: str) -> str:
        return self._hash_payload({"kind": "image", "sha256": str(sha_value or "")})

    def _fingerprint_image_chain_sha(self, sha_value: str) -> str:
        return self._hash_payload(
            {
                "kind": "chain",
                "parts": [{"type": "image", "sha256": str(sha_value or "")}],
            }
        )

    def _single_standard_image_signature(self, quote: Quote) -> ImageSignature | None:
        if quote.kind != "standard" or len(quote.segments) != 1:
            return None
        segment = quote.segments[0]
        if segment.type != "image" or not segment.asset_id:
            return None
        asset = self.repository.find_asset(quote.group, segment.asset_id)
        if asset is None or not asset.sha256:
            return None
        return ImageSignature(
            sha256=asset.sha256,
            dhash=asset.dhash,
            width=asset.width,
            height=asset.height,
        )

    def _stored_standard_segment_payload(self, session_key: str, segment: Any) -> dict[str, Any] | None:
        if segment.type == "text":
            text = self._canonical_text(str(segment.text or ""))
            return {"type": "text", "text": text} if text else None
        if segment.type == "image" and segment.asset_id:
            asset = self.repository.find_asset(session_key, segment.asset_id)
            if asset is not None and asset.sha256:
                return {"type": "image", "sha256": asset.sha256}
            return {"type": "image", "asset_id": segment.asset_id}
        return None

    def _pending_segment_payload(self, segment: Any) -> dict[str, Any] | None:
        if segment.type == "text":
            text = self._canonical_text(str(segment.text or ""))
            return {"type": "text", "text": text} if text else None
        if segment.type == "image" and getattr(segment, "image", None) is not None:
            return {"type": "image", "sha256": str(segment.image.sha256 or "")}
        return None

    def _fingerprint_forward_nodes(self, session_key: str, nodes: list[ForwardNode]) -> str:
        payload = [self._stored_forward_node_payload(session_key, node) for node in nodes]
        payload = [item for item in payload if item is not None]
        if not payload:
            return ""
        return self._hash_payload({"kind": "forward", "nodes": payload})

    def _fingerprint_pending_forward_nodes(self, nodes: list[Any]) -> str:
        payload = [self._pending_forward_node_payload(node) for node in nodes]
        payload = [item for item in payload if item is not None]
        if not payload:
            return ""
        return self._hash_payload({"kind": "forward", "nodes": payload})

    def _stored_forward_node_payload(self, session_key: str, node: ForwardNode) -> dict[str, Any] | None:
        segments = [self._stored_forward_segment_payload(session_key, segment) for segment in node.segments]
        segments = [item for item in segments if item is not None]
        return {
            "sender_uin": str(node.sender_uin or ""),
            "sender_name": str(node.sender_name or ""),
            "segments": segments,
        }

    def _stored_forward_segment_payload(self, session_key: str, segment: ForwardSegment) -> dict[str, Any] | None:
        if segment.type == "text":
            text = self._canonical_text(str(segment.text or ""))
            return {"type": "text", "text": text} if text else None
        if segment.type == "image" and segment.asset_id:
            asset = self.repository.find_asset(session_key, segment.asset_id)
            if asset is not None and asset.sha256:
                return {"type": "image", "sha256": asset.sha256}
            return {"type": "image", "asset_id": segment.asset_id}
        if segment.type in {"record", "video", "file"} and segment.asset_id:
            asset = self.repository.find_media_asset(session_key, segment.asset_id)
            if asset is not None:
                abs_path = self.repository.root / asset.rel_path
                if abs_path.exists():
                    return {"type": segment.type, "sha256": sha256_bytes(abs_path.read_bytes())}
            return {"type": segment.type, "asset_id": segment.asset_id}
        if segment.type == "face" and segment.face_id:
            return {"type": "face", "face_id": int(segment.face_id)}
        if segment.type == "at" and segment.qq:
            return {"type": "at", "qq": str(segment.qq), "name": str(segment.name or "")}
        if segment.type == "nodes":
            nested = [self._stored_forward_node_payload(session_key, node) for node in segment.nodes]
            nested = [item for item in nested if item is not None]
            return {"type": "nodes", "nodes": nested} if nested else {"type": "text", "text": "[聊天记录]"}
        placeholder = self._placeholder_for_unknown(segment.type)
        return {"type": "text", "text": placeholder} if placeholder else None

    def _pending_forward_node_payload(self, node: Any) -> dict[str, Any] | None:
        segments = [self._pending_forward_segment_payload(segment) for segment in getattr(node, "segments", [])]
        segments = [item for item in segments if item is not None]
        return {
            "sender_uin": str(getattr(node, "sender_uin", "") or ""),
            "sender_name": str(getattr(node, "sender_name", "") or ""),
            "segments": segments,
        }

    def _pending_forward_segment_payload(self, segment: Any) -> dict[str, Any] | None:
        if segment.type == "text":
            text = self._canonical_text(str(segment.text or ""))
            return {"type": "text", "text": text} if text else None
        if segment.type == "image" and getattr(segment, "image", None) is not None:
            return {"type": "image", "sha256": str(segment.image.sha256 or "")}
        if segment.type in {"record", "video", "file"} and getattr(segment, "media", None) is not None:
            return {"type": segment.type, "sha256": sha256_bytes(segment.media.content)}
        if segment.type == "face" and getattr(segment, "face_id", 0):
            return {"type": "face", "face_id": int(segment.face_id)}
        if segment.type == "at" and getattr(segment, "qq", ""):
            return {"type": "at", "qq": str(segment.qq), "name": str(segment.name or "")}
        if segment.type == "nodes":
            nested = [self._pending_forward_node_payload(node) for node in getattr(segment, "nodes", [])]
            nested = [item for item in nested if item is not None]
            return {"type": "nodes", "nodes": nested} if nested else {"type": "text", "text": "[聊天记录]"}
        placeholder = self._placeholder_for_unknown(segment.type)
        return {"type": "text", "text": placeholder} if placeholder else None

    def _canonical_text(self, text: str) -> str:
        return str(text or "").replace("\r\n", "\n").strip()

    def _hash_payload(self, payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256_bytes(canonical.encode("utf-8"))

    def _has_duplicate_quote(self, session_key: str, *, target_qq: str, fingerprint: str) -> bool:
        if not target_qq or not fingerprint:
            return False
        for quote in self.repository.list_quotes(session_key):
            if str(quote.qq or "") != str(target_qq):
                continue
            existing_fingerprint = self._stored_quote_fingerprint(quote)
            if existing_fingerprint and existing_fingerprint == fingerprint:
                return True
        return False

    def _stored_quote_fingerprint(self, quote: Quote) -> str:
        if quote.kind == "forward":
            return self._fingerprint_forward_nodes(quote.group, quote.forward_nodes)
        return self._fingerprint_standard_quote(quote)

    def _self_id_of_event(self, event: Any) -> str:
        for getter in (
            lambda: getattr(event, "get_self_id", lambda: "")(),
            lambda: getattr(getattr(event, "message_obj", None), "self_id", None),
            lambda: getattr(event, "self_id", None),
            lambda: (getattr(event, "raw_event", None) or {}).get("self_id")
            if isinstance(getattr(event, "raw_event", None), dict)
            else None,
        ):
            try:
                value = getter()
            except Exception:
                value = None
            if value:
                return str(value)
        return ""

    def _placeholder_for_media(self, media_type: str) -> str:
        return {
            "record": "[语音]",
            "video": "[视频]",
            "file": "[文件]",
        }.get(media_type, "[附件]")

    def _placeholder_for_unknown(self, seg_type: str) -> str:
        if not seg_type:
            return ""
        return f"[{seg_type}]"

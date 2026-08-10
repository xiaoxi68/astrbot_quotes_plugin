from __future__ import annotations

import html
from typing import Any, Awaitable, Callable

try:
    from .models import Quote
    from .utils import normalize_quote_text
except ImportError:  # pragma: no cover
    from models import Quote
    from utils import normalize_quote_text


class QuoteRenderer:
    MAX_RENDER_TEXT_LENGTH = 260
    PLAIN_FALLBACK_TEXT_LENGTH = 1000

    def __init__(
        self,
        html_render: Callable[[str, dict[str, Any], bool, dict[str, Any] | None], Awaitable[str]],
        image_config: dict[str, Any],
    ):
        self._html_render = html_render
        self.image_config = image_config or {}

    async def _render_template(
        self,
        template: str,
        data: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> str:
        return await self._html_render(
            template,
            data,
            True,
            options,
        )

    async def warmup(self) -> None:
        minimal = '<div style="width:320px;height:120px;background:#000;color:#fff">init</div>'
        try:
            await self._render_template(
                minimal,
                {},
                {"full_page": False, "clip": {"x": 0, "y": 0, "width": 320, "height": 120}},
            )
        except Exception:
            return

    def should_fallback_to_plain(self, quote: Quote) -> bool:
        if quote.kind != "standard":
            return False
        return len(normalize_quote_text(quote.text)) > self.PLAIN_FALLBACK_TEXT_LENGTH

    def _resolve_text_layout(self, text: str) -> dict[str, Any]:
        normalized = normalize_quote_text(text)
        if len(normalized) > self.MAX_RENDER_TEXT_LENGTH:
            clipped = normalized[: self.MAX_RENDER_TEXT_LENGTH].rstrip()
            clipped = clipped.rstrip("，。！？；：、,.!?;: ")
            normalized = f"{clipped}……" if clipped else "……"

        length = len(normalized)
        if length <= 48:
            return {
                "text": normalized,
                "font_size": 38,
                "line_height": 1.6,
                "padding_x": 80,
                "signature_size": 22,
                "quote_gap": 14,
            }
        if length <= 100:
            return {
                "text": normalized,
                "font_size": 32,
                "line_height": 1.55,
                "padding_x": 72,
                "signature_size": 20,
                "quote_gap": 12,
            }
        if length <= 180:
            return {
                "text": normalized,
                "font_size": 26,
                "line_height": 1.5,
                "padding_x": 64,
                "signature_size": 18,
                "quote_gap": 10,
            }
        return {
            "text": normalized,
            "font_size": 22,
            "line_height": 1.45,
            "padding_x": 56,
            "signature_size": 17,
            "quote_gap": 8,
        }

    async def render_quote_image(self, quote: Quote, signature: str) -> str:
        width = int(self.image_config.get("width", 1280))
        height = int(self.image_config.get("height", 427))
        bg_color = self.image_config.get("bg_color", "#000")
        text_color = self.image_config.get("text_color", "#fff")
        font_family = self.image_config.get(
            "font_family",
            "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'WenQuanYi Micro Hei', Arial, sans-serif",
        )
        avatar = f"https://q1.qlogo.cn/g?b=qq&nk={quote.qq}&s=640"
        layout = self._resolve_text_layout(quote.text)
        safe_text = html.escape(layout["text"])
        signature = html.escape(signature or quote.name)
        grad_width = max(200, int(width * 0.26))
        grad_left = int(width * 0.36) - int(grad_width * 0.7)
        padding_x = int(layout["padding_x"])
        signature_size = int(layout["signature_size"])
        font_size = int(layout["font_size"])
        quote_gap = int(layout["quote_gap"])
        max_text_width = max(200, int(width * 0.64) - padding_x * 2)

        template = f"""
        <html>
        <head>
            <meta charset='utf-8' />
            <style>
                * {{ box-sizing: border-box; }}
                html, body {{ margin:0; padding:0; width:{width}px; min-height:{height}px; height:auto; background:{bg_color}; }}
                .root {{ position:relative; width:{width}px; min-height:{height}px; height:auto; background:{bg_color}; font-family:{font_family}; overflow:hidden; }}
                .left {{ position:absolute; left:0; top:0; bottom:0; width:{int(width * 0.36)}px; overflow:hidden; z-index:0; }}
                .left img {{ width:100%; height:100%; object-fit:cover; display:block; }}
                .left .left-shade {{ position:absolute; inset:0; background: linear-gradient(to right, rgba(0,0,0,0) 0%, rgba(0,0,0,0.28) 58%, rgba(0,0,0,0.55) 100%); }}
                .right {{ position:relative; margin-left:{int(width * 0.36)}px; width:{int(width * 0.64)}px; min-height:{height}px; padding:36px 0; background:{bg_color}; display:flex; align-items:center; justify-content:center; text-align:center; z-index:2; }}
                .text {{
                    color:{text_color};
                    font-size:{font_size}px;
                    line-height:{layout["line_height"]};
                    padding:0 {padding_x}px;
                    max-width:{max_text_width}px;
                    display:flex;
                    align-items:center;
                    justify-content:center;
                    gap:{quote_gap}px;
                    text-align:center;
                }}
                .text-content {{
                    display:block;
                    max-width:100%;
                    white-space:pre-wrap;
                    word-break:break-word;
                    overflow-wrap:anywhere;
                }}
                .signature {{ position:absolute; right:44px; bottom:28px; color:rgba(255,255,255,0.82); font-size:{signature_size}px; font-weight:300; z-index:3; }}
                .quote-mark {{ color:{text_color}; opacity:0.8; flex:0 0 auto; }}
                .fade-overlay {{
                    position:absolute;
                    top:0; bottom:0;
                    left:{grad_left}px;
                    width:{grad_width}px;
                    pointer-events:none;
                    z-index:1;
                    background: linear-gradient(
                        to right,
                        rgba(0,0,0,0.00) 0%,
                        rgba(0,0,0,0.35) 38%,
                        rgba(0,0,0,0.70) 70%,
                        {bg_color} 100%
                    );
                }}
            </style>
        </head>
        <body>
            <div class="root">
                <div class="left"><img src="{avatar}" /><div class="left-shade"></div></div>
                <div class="right">
                    <div class="text">
                        <span class="quote-mark">「</span>
                        <div class="text-content">{safe_text}</div>
                        <span class="quote-mark">」</span>
                    </div>
                </div>
                <div class="fade-overlay"></div>
                <div class="signature">— {signature}</div>
            </div>
        </body>
        </html>
        """
        return await self._render_template(
            template,
            {},
            {
                "full_page": True,
                "omit_background": False,
                "viewport_width": width,
                "viewport_height": height,
            },
        )

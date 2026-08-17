from __future__ import annotations

import json

from astrbot.api.all import logger
from astrbot.api.event import AstrMessageEvent


LOG_PREFIX = "[GetPx]"


class LlmMixin:
    """大模型相关的回复生成、图片描述、标签优化与对话历史写入。"""

    def _get_image_caption_provider_id(self) -> str:
        """读取 AstrBot 配置的默认图片转述模型 provider_id。"""
        try:
            context_obj = getattr(self, "context", None)
            if context_obj is None:
                return ""
            get_config = getattr(context_obj, "get_config", None)
            if not callable(get_config):
                return ""
            cfg = get_config() or {}
            return str(
                (cfg.get("provider_settings") or {}).get(
                    "default_image_caption_provider_id"
                )
                or ""
            )
        except Exception:
            return ""

    async def _llm_generate_text(
        self,
        event: AstrMessageEvent,
        prompt: str,
        image_urls: list[str] | None = None,
    ) -> str:
        """调用大模型生成文本；无可用的模型或调用失败返回空串。

        带图片（image_urls）时，优先使用 AstrBot 配置的「默认图片转述模型」，
        避免会话的默认对话模型不支持多模态而失败；未配置则回退当前会话聊天模型。
        """
        try:
            context_obj = getattr(self, "context", None)
            if context_obj is None:
                return ""
            get_provider = getattr(context_obj, "get_current_chat_provider_id", None)
            llm_generate = getattr(context_obj, "llm_generate", None)
            if get_provider is None or llm_generate is None:
                return ""
            provider_id = await get_provider(event.unified_msg_origin)
            if not provider_id:
                return ""
            if image_urls:
                caption_provider_id = self._get_image_caption_provider_id()
                if caption_provider_id:
                    provider_id = caption_provider_id
            llm_resp = await llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                image_urls=image_urls or None,
            )
            return (getattr(llm_resp, "completion_text", "") or "").strip()
        except Exception:
            return ""

    async def _record_message_pair(
        self, event: AstrMessageEvent, user_text: str, assistant_text: str
    ) -> None:
        """把一组「用户请求 / 助手回复」写入 AstrBot 对话历史（无会话则自动新建）。"""
        if not self._cfg_bool("record_message_to_conversation", True):
            return
        try:
            conversation_manager = getattr(self, "context", None)
            if conversation_manager is None:
                return
            conv_mgr = getattr(conversation_manager, "conversation_manager", None)
            if conv_mgr is None:
                return
            umo = event.unified_msg_origin
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                try:
                    cid = await conv_mgr.new_conversation(umo)
                except Exception:
                    return
            from astrbot.core.agent.message import (
                AssistantMessageSegment,
                TextPart,
                UserMessageSegment,
            )

            await conv_mgr.add_message_pair(
                cid=cid,
                user_message=UserMessageSegment(content=[TextPart(text=user_text)]),
                assistant_message=AssistantMessageSegment(
                    content=[TextPart(text=assistant_text)]
                ),
            )
        except Exception:
            return

    async def _record_conversation(
        self,
        event: AstrMessageEvent,
        tag: str,
        count: int,
        sent_illust_ids: set[str],
        downloaded: list[tuple[dict, str, str, int]],
        description: str = "",
    ) -> None:
        """把本次发图结果（含图片描述）写入 AstrBot 对话历史。"""
        user_text = f"请求发图（标签：{tag or '随机'}，数量：{count}）"
        sent_ids = sorted(sent_illust_ids)
        assistant_text = (
            f"已发送 {len(sent_ids)} 张图片（ID：{', '.join(sent_ids) or '-'}）"
        )
        if description:
            assistant_text += f"\n图片内容描述：{description}"
        await self._record_message_pair(event, user_text, assistant_text)
        logger.info(
            f"{LOG_PREFIX} 已记录发图消息到对话历史: "
            f"sent_count={len(sent_ids)} "
            f"has_description={'yes' if description else 'no'}"
        )

    async def _push_to_agent(self, event: AstrMessageEvent, note: str) -> str:
        """把场景推送给 AstrBot 主 Agent，让其按人格与上下文回复并写入对话历史。

        返回最终回复文本；无模型或异常时返回空串，由调用方用默认文案兜底。
        """
        try:
            from astrbot.core.astr_main_agent import (
                MainAgentBuildConfig,
                _get_session_conv,
                build_main_agent,
            )
            from astrbot.core.cron.events import CronMessageEvent
            from astrbot.core.platform.message_session import MessageSession
            from astrbot.core.provider.entities import ProviderRequest, ProviderType

            ctx = getattr(self, "context", None)
            if ctx is None:
                return ""
            get_config = getattr(ctx, "get_config", None)
            if not callable(get_config):
                return ""
            umo = event.unified_msg_origin
            cfg = get_config(umo=umo) or {}
            provider_settings = cfg.get("provider_settings") or {}
            provider_manager = getattr(ctx, "provider_manager", None)
            if provider_manager is None:
                return ""
            # 无可用对话模型时直接兜底（不触发 Agent）
            if (
                provider_manager.get_using_provider(
                    provider_type=ProviderType.CHAT_COMPLETION, umo=umo
                )
                is None
            ):
                return ""

            session = MessageSession.from_str(umo)
            cron_event = CronMessageEvent(
                context=ctx,
                session=session,
                message=note,
                message_type=session.message_type,
            )
            config = MainAgentBuildConfig(
                tool_call_timeout=120,
                streaming_response=provider_settings.get("stream", False),
                provider_settings=provider_settings,
            )
            conv = await _get_session_conv(event=cron_event, plugin_context=ctx)
            before_history = conv.history
            req = ProviderRequest()
            req.conversation = conv
            req.contexts = json.loads(conv.history) if conv.history else []
            req.prompt = note
            req.image_urls = []
            req.audio_urls = []
            req.func_tool = None  # 状态提示不调用工具，避免递归

            result = await build_main_agent(
                event=cron_event, plugin_context=ctx, config=config, req=req
            )
            if not result:
                return ""
            runner = result.agent_runner
            async for _ in runner.step_until_done(30):
                pass
            llm_resp = runner.get_final_llm_resp()
            text = (getattr(llm_resp, "completion_text", "") or "").strip()
            if not text:
                return ""
            # 若 Agent 未把本轮写入对话历史，则由插件补齐，保证回复被记忆
            if conv.history == before_history:
                await self._record_message_pair(event, note, text)
            return text
        except Exception as exc:
            logger.debug(
                f"{LOG_PREFIX} 推送主 Agent 回复失败，使用默认文案: "
                f"error_type={type(exc).__name__}"
            )
            return ""

    async def _reply(
        self,
        event: AstrMessageEvent,
        fallback: str,
        context: str = "",
        push: bool = True,
    ):
        """产出状态回复（async generator）。

        命令路径（push=True）推送给 AstrBot 主 Agent 按人格自然回复；工具路径（push=False）
        直接返回默认文案，由其工具结果交回外层 Agent 按人格自然回应（避免在 Agent 内部
        嵌套再跑 Agent）。两条路径都会确保回复写入对话历史（受 record_message_to_conversation 控制）。
        """
        if push and self._cfg_bool("llm_natural_replies", True):
            note = fallback
            if context:
                note += f"\n背景：{context}"
            text = await self._push_to_agent(event, note)
            if text:
                yield event.plain_result(text)
                return
        await self._record_message_pair(event, context or "发图请求", fallback)
        if push:
            yield event.plain_result(fallback)
        else:
            yield fallback

    async def _optimize_search_tag(self, event: AstrMessageEvent, tag: str) -> str:
        """让大模型优化/改写搜索标签（如翻译为日语）；失败返回空串。"""
        if not tag:
            return ""
        prompt = (
            f"插画搜索标签「{tag}」在图片源上没有结果。"
            f"请给出一个更可能命中结果的替换标签：优先翻译成日语标签，"
            f"或用更常见的英文/日语词表达同一个主题。"
            f"只输出替换后的标签本身，不要引号、解释、编号或多余文字。"
        )
        text = await self._llm_generate_text(event, prompt)
        text = text.strip().strip("\"'“”‘’《》【】[]()")
        text = " ".join(text.split())
        return text[:60]

    async def _describe_images_with_llm(
        self,
        event: AstrMessageEvent,
        tag: str,
        downloaded: list[tuple[dict, str, str, int]],
    ) -> str:
        """调用多模态大模型查看已发送图片，返回绑定搜索标签的内容描述。"""
        image_paths = [path for _illust, path, _q, _s in downloaded if path]
        if not image_paths:
            return ""
        base_prompt = self._cfg_str(
            "llm_describe_prompt",
            "请简要描述这张图片的内容、构图与氛围，用中文，不超过 80 字。",
        )
        tag_clean = str(tag or "").strip()
        if tag_clean:
            prompt = (
                f"{base_prompt}\n"
                f"这些图片是用户按标签「{tag_clean}」搜索并发送的插画，"
                f"请结合该标签客观描述图片内容，并判断图片是否与标签相符。"
            )
        else:
            prompt = base_prompt
        text = await self._llm_generate_text(event, prompt, image_urls=image_paths)
        if not text:
            logger.warning(
                f"{LOG_PREFIX} 大模型未返回图片描述，请确认所用模型支持图片输入（多模态）"
            )
            return ""
        logger.info(
            f"{LOG_PREFIX} 大模型已查看图片: image_count={len(image_paths)} "
            f"tag_configured={'yes' if tag_clean else 'no'} "
            f"description_len={len(text)}"
        )
        return text

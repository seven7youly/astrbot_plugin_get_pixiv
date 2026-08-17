from __future__ import annotations

import asyncio

from astrbot.api.all import Image, Plain, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Node, Nodes

from .constants import AIOCQHTTP_PLATFORM, MAX_IMAGE_COUNT
from .downloader import cleanup


LOG_PREFIX = "[GetPx]"
DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB = 3.0


def _search_quality_label(value: object) -> str:
    return {
        "original": "原图",
        "large": "大图",
        "medium": "中图",
        "square_medium": "方形缩略图",
    }.get(str(value or ""), str(value or ""))


def _search_source_label(value: object) -> str:
    source = str(value or "")
    if source.startswith("lolicon"):
        return "Lolicon"
    if source.startswith("pixiv"):
        return "Pixiv"
    return source


class SearchMixin:
    """Search and source-fallback flows."""

    @staticmethod
    def _should_use_forward(
        *, downloaded_count: int, threshold: int, platform_name: str
    ) -> bool:
        return platform_name == AIOCQHTTP_PLATFORM and downloaded_count > threshold

    def _ensure_client_or_error(self, event: AstrMessageEvent) -> bool:
        lolicon_client = getattr(self, "lolicon_client", None)
        if lolicon_client and lolicon_client.available:
            return True
        if getattr(self, "client", None) and self.client.api:
            return True
        self._init_client()
        return bool(
            (
                getattr(self, "lolicon_client", None)
                and self.lolicon_client.available
            )
            or getattr(self, "client", None)
        )

    @staticmethod
    def _event_scope(event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if group_id:
            return f"group:{group_id}"
        return f"private:{event.get_sender_id() or ''}"

    @staticmethod
    def _source_key(tag: str, source: str) -> str:
        prefix = "search" if tag.strip() else "random"
        return f"{source}:{prefix}:{tag.strip().casefold()}" if tag.strip() else f"{source}:random"

    async def _fetch_source_candidates(
        self,
        event: AstrMessageEvent,
        tag: str,
        *,
        count: int = 20,
        offset: int = 0,
        aspect_ratio: str = "",
        use_page_cursor: bool = True,
        allow_r18: bool | None = None,
        source_key_suffix: str = "",
    ) -> tuple[list[dict], int, str]:
        """优先请求 Lolicon，失败后按有无标签回退 Pixiv。"""
        lolicon_client = getattr(self, "lolicon_client", None)
        if lolicon_client and lolicon_client.available:
            try:
                if tag:
                    illusts = await lolicon_client.search(
                        tag,
                        count=count,
                        aspect_ratio=aspect_ratio,
                        allow_r18=allow_r18,
                    )
                    source_key = self._source_key(tag, "lolicon") + source_key_suffix
                else:
                    illusts = await lolicon_client.random(
                        count=count, aspect_ratio=aspect_ratio, allow_r18=allow_r18
                    )
                    source_key = "lolicon:random" + source_key_suffix
                if illusts:
                    return illusts, len(illusts), source_key
            except Exception as exc:
                logger.info(
                    f"{LOG_PREFIX} Lolicon 请求失败，尝试 Pixiv 回退: "
                    f"tag_configured={'yes' if tag else 'no'} "
                    f"error_type={type(exc).__name__}"
                )

        pixiv_source_key = (
            self._source_key(tag, "pixiv") if tag else "pixiv:recommended"
        ) + source_key_suffix
        page_offset = offset
        if use_page_cursor and page_offset == 0 and self.image_index is not None:
            try:
                page_offset = await self.image_index.get_page_offset(
                    self._event_scope(event), pixiv_source_key
                )
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 读取 Pixiv 回退分页游标失败: "
                    f"error_type={type(exc).__name__}"
                )

        if self.client is None:
            self._init_client()
        if self.client is None:
            return [], 0, pixiv_source_key
        try:
            if tag:
                illusts = await self.client.search(tag, offset=page_offset)
                source_key = pixiv_source_key
            else:
                illusts = await self.client.recommended(offset=page_offset)
                source_key = pixiv_source_key
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} Pixiv 回退请求失败: "
                f"tag_configured={'yes' if tag else 'no'} "
                f"error_type={type(exc).__name__}"
            )
            return [], 0, pixiv_source_key
        return illusts, len(illusts), source_key

    @staticmethod
    def _artwork_pid(illust: dict, illust_id: str) -> str:
        """提取作品在 Pixiv 上的纯数字 ID（Lolicon 的 id 形如 pid:page）。"""
        pid = str(illust.get("pid") or "").strip()
        if pid:
            return pid
        return str(illust_id or "").split(":")[0]

    async def _record_image_usage(
        self,
        event: AstrMessageEvent,
        source_key: str,
        illust: dict,
        *,
        feature: str,
        user_id: str = "",
    ) -> None:
        if self.image_index is None or not source_key:
            return
        illust_id = str(illust.get("id") or "")
        if not illust_id:
            return
        try:
            await self.image_index.record_usage(
                scope=self._event_scope(event),
                source_key=source_key,
                illust_id=illust_id,
                feature=feature,
                user_id=user_id,
            )
        except Exception:
            pass

    async def _describe_images_with_llm(
        self,
        event: AstrMessageEvent,
        tag: str,
        downloaded: list[tuple[dict, str, str, int]],
    ) -> str:
        """调用当前会话的多模态大模型查看已发送图片，返回内容描述。

        描述会绑定本次发图所用的搜索标签，让大模型结合标签客观评判图片内容；
        无标签（随机取图）时仅使用 llm_describe_prompt 基础提示词。
        直接复用发图时下载的临时文件路径，不新增磁盘占用；读取后由大模型
        侧编码传输，临时文件仍由调用方在 finally 中清理。
        """
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

    async def _record_conversation(
        self,
        event: AstrMessageEvent,
        tag: str,
        count: int,
        sent_illust_ids: set[str],
        downloaded: list[tuple[dict, str, str, int]],
        description: str = "",
    ) -> None:
        """把本次发图写入 AstrBot 对话历史，让大模型/AI 能看到插件发送的消息。

        若该会话尚无对话记录，会自动新建一条对话，保证发送结果与图片描述
        总能持久写入（如无会话记录则大模型后续无法“看到”图片内容）。
        """
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
                    logger.debug(
                        f"{LOG_PREFIX} 会话无对话记录，已自动新建对话: "
                        f"cid={cid}"
                    )
                except Exception as exc:
                    logger.debug(
                        f"{LOG_PREFIX} 新建对话失败，跳过记录: "
                        f"error_type={type(exc).__name__}"
                    )
                    return
            from astrbot.core.agent.message import (
                AssistantMessageSegment,
                TextPart,
                UserMessageSegment,
            )

            user_text = f"请求发图（标签：{tag or '随机'}，数量：{count}）"
            sent_ids = sorted(sent_illust_ids)
            assistant_text = (
                f"已发送 {len(sent_ids)} 张图片（ID：{', '.join(sent_ids) or '-'}）"
            )
            if description:
                assistant_text += f"\n图片内容描述：{description}"
            await conv_mgr.add_message_pair(
                cid=cid,
                user_message=UserMessageSegment(content=[TextPart(text=user_text)]),
                assistant_message=AssistantMessageSegment(
                    content=[TextPart(text=assistant_text)]
                ),
            )
            logger.info(
                f"{LOG_PREFIX} 已记录发图消息到对话历史: cid={cid} "
                f"sent_count={len(sent_ids)} "
                f"has_description={'yes' if description else 'no'}"
            )
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 记录发图消息到对话历史失败: "
                f"error_type={type(exc).__name__}"
            )

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

    async def _record_reply(
        self, event: AstrMessageEvent, user_text: str, reply_text: str
    ) -> None:
        """把插件提示/回复写入 AstrBot 对话历史，让大模型/AI 能看到。"""
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
                    content=[TextPart(text=reply_text)]
                ),
            )
        except Exception:
            return

    async def _reply(
        self,
        event: AstrMessageEvent,
        fallback: str,
        context: str = "",
        user_text: str = "",
        record: bool = True,
    ) -> str:
        """按当前会话大模型的人格改写回复；未开启或调用失败时返回默认文案。

        开启记录时（record=True），改写后的回复会一并写入 AstrBot 对话历史。
        """
        text = fallback
        if self._cfg_bool("llm_natural_replies", True):
            prompt = (
                "你是当前会话的机器人角色。下面是一条本应由插件输出的功能提示，"
                "请用符合你人格设定的自然口语改写为一句（不超过 30 字），保留原意，"
                "不要解释、引号、Markdown 或多余文字。\n"
                f"提示：{fallback}\n"
                f"背景：{context or '发图相关提示'}"
            )
            text = (await self._llm_generate_text(event, prompt)) or fallback
        if record and user_text and text:
            await self._record_reply(event, user_text, text)
        return text

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

    async def _fetch_search_candidates(
        self,
        event: AstrMessageEvent,
        tag: str,
        *,
        count: int,
        allow_r18: bool,
        source_key_suffix: str,
        tag_retry_enabled: bool = False,
        tag_retry_limit: int = 0,
    ) -> tuple[list[dict], str, int, str, str]:
        """获取并过滤候选作品；无结果时（开启标签重试）让大模型更换标签重试。

        返回 (illusts, source_key, raw_count, search_tag, reason)。
        reason 为空表示成功；否则为失败原因代码（blocked/safety_error/
        no_results/filtered_manga/filtered_blacklist）。
        """
        filter_manga = self._cfg_bool("filter_manga", True)
        current_tag = tag
        reason = ""
        for attempt in range(tag_retry_limit + 1):
            if attempt > 0:
                new_tag = await self._optimize_search_tag(event, current_tag)
                if not new_tag:
                    reason = "no_results"
                    break
                current_tag = new_tag
                logger.info(
                    f"{LOG_PREFIX} 标签重试 {attempt}/{tag_retry_limit}: "
                    f"已更换标签 {tag!r} -> {current_tag!r}"
                )

            try:
                if current_tag and await self._blocked_query_term(current_tag):
                    reason = "blocked"
                    if not tag_retry_enabled:
                        break
                    continue
            except RuntimeError:
                return [], "", 0, current_tag, "safety_error"

            illusts, raw_count, source_key = await self._fetch_source_candidates(
                event,
                current_tag,
                count=count,
                allow_r18=allow_r18,
                source_key_suffix=source_key_suffix,
            )
            if not illusts:
                reason = "no_results"
                if not tag_retry_enabled:
                    break
                continue

            if filter_manga:
                illusts = self._filter_manga(illusts)
                if not illusts:
                    reason = "filtered_manga"
                    if not tag_retry_enabled:
                        break
                    continue

            try:
                illusts = await self._filter_blacklisted_illusts(
                    illusts, allow_r18=allow_r18
                )
            except RuntimeError:
                return [], "", 0, current_tag, "safety_error"
            if not illusts:
                reason = "filtered_blacklist"
                if not tag_retry_enabled:
                    break
                continue

            return illusts, source_key, raw_count, current_tag, ""

        return [], "", 0, current_tag, reason

    @staticmethod
    def _search_failure_message(reason: str) -> str:
        return {
            "blocked": "🚫 搜索词不符合内容安全要求",
            "safety_error": "🚫 内容安全服务暂不可用，本次请求已拒绝",
            "no_results": "❌ 图片源请求失败或无结果，换个标签试试",
            "filtered_manga": "😶 过滤漫画后没有可用作品，可关闭漫画过滤后重试",
            "filtered_blacklist": "😶 可用作品都被内容安全策略过滤了，换个标签后再试",
        }.get(reason, "❌ 图片源请求失败或无结果，换个标签试试")

    async def _handle_search(
        self,
        event: AstrMessageEvent,
        tag: str,
        count_str: str,
        *,
        allow_r18_override: bool = False,
        record_conversation: bool = True,
        tag_retry_enabled: bool = False,
    ):
        """搜索并发送图片；Lolicon 失败时按需回退 Pixiv。"""
        # 频率限制
        wait = self._check_rate_limit(event.get_sender_id())
        if wait > 0:
            logger.debug(
                f"{LOG_PREFIX} 搜索请求触发频率限制: retry_after_seconds={wait}"
            )
            yield event.plain_result(
                await self._reply(
                    event,
                    f"⏳ 请求太频繁，请 {wait} 秒后再试",
                    "频率限制",
                    f"请求发图（标签：{tag or '随机'}，数量：{count_str or 1}）",
                    record_conversation,
                )
            )
            return

        # 参数解析
        max_count = self._cfg_int("max_count", 5, 1, MAX_IMAGE_COUNT)
        try:
            count = max(1, min(int(count_str), max_count)) if count_str else 1
        except (TypeError, ValueError):
            count = 1

        # R18：单次覆盖优先，否则跟随配置
        r18_mode = allow_r18_override or self._cfg_bool("allow_r18", False)
        source_key_suffix = ":r18" if allow_r18_override else ""

        timeout_sec = self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
        quality = self._cfg_str("image_quality", "large")
        downgrade_limit_mb = self._cfg_float(
            "auto_downgrade_original_mb",
            DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB,
            0.0,
            100.0,
        )
        downgrade_limit_bytes = int(downgrade_limit_mb * 1024 * 1024)

        # 获取候选：无结果时（自然语言调用）让大模型更换标签重试
        tag_retry_limit = (
            self._cfg_int("llm_search_tag_retries", 2, 0, 5)
            if tag_retry_enabled
            else 0
        )
        illusts, source_key, raw_count, search_tag, reason = (
            await self._fetch_search_candidates(
                event,
                tag,
                count=max_count,
                allow_r18=r18_mode,
                source_key_suffix=source_key_suffix,
                tag_retry_enabled=tag_retry_enabled,
                tag_retry_limit=tag_retry_limit,
            )
        )
        if reason:
            yield event.plain_result(
                await self._reply(
                    event,
                    self._search_failure_message(reason),
                    f"标签：{search_tag or '随机'}",
                    f"请求发图（标签：{tag or '随机'}，数量：{count}）",
                    record_conversation,
                )
            )
            return
        logger.info(
            f"{LOG_PREFIX} 搜索候选获取完成: "
            f"tag_configured={'yes' if search_tag else 'no'} "
            f"source={_search_source_label(source_key)} "
            f"requested_count={count} quality={_search_quality_label(quality)} "
            f"candidate_count={raw_count} r18={'yes' if r18_mode else 'no'}"
        )

        pick_count = min(count, len(illusts))
        dedupe_days = self._cfg_int("dedupe_days", 1, 0, 7)
        if (
            self.image_index is not None
            and self.image_index.retention_days != dedupe_days
        ):
            try:
                await self.image_index.set_retention_days(dedupe_days)
            except Exception:
                yield event.plain_result(
                    await self._reply(
                        event,
                        "图片去重索引更新失败，请稍后重试",
                        "去重索引",
                        f"请求发图（标签：{tag or '随机'}，数量：{count}）",
                        record_conversation,
                    )
                )
                return

        chosen = await self._pick_illusts(
            event,
            illusts,
            pick_count,
            source_key=source_key,
            dedupe_enabled=dedupe_days > 0,
            raw_count=raw_count,
        )
        if not chosen:
            yield event.plain_result(
                await self._reply(
                    event,
                    "当前去重范围内没有未发送过的图片了，换个标签或稍后再试",
                    "发图去重",
                    f"请求发图（标签：{tag or '随机'}，数量：{count}）",
                    record_conversation,
                )
            )
            return
        pick_count = len(chosen)
        pending_illust_ids: set[str] = set()
        sent_illust_ids: set[str] = set()
        if dedupe_days > 0 and self.image_index is not None:
            pending_illust_ids = {
                str(illust.get("id") or "") for illust in chosen if illust.get("id")
            }

        # 下载所有图片
        downloaded: list[tuple[dict, str, str, int]] = []
        temp_paths: list[str] = []
        try:
            for idx, illust in enumerate(chosen, 1):
                illust_id = illust.get("id", "?")
                title = illust.get("title", "无标题")

                try:
                    path, actual_q, file_size = await self.downloader.download_for_send(
                        illust,
                        quality,
                        timeout=timeout_sec,
                        downgrade_limit_bytes=downgrade_limit_bytes,
                        log_context=f"[{idx}/{pick_count}] 作品 {illust_id}",
                    )
                    logger.debug(
                        f"{LOG_PREFIX} [{idx}/{pick_count}] 作品 {illust_id} "
                        f"下载完成（大小={file_size / 1024:.2f}KB，画质={_search_quality_label(actual_q)}）"
                    )
                    temp_paths.append(path)
                    downloaded.append((illust, path, actual_q, file_size))
                except asyncio.TimeoutError:
                    logger.debug(
                        f"{LOG_PREFIX} 下载候选跳过: illust_id={illust_id} "
                        f"candidate={idx}/{pick_count} reason=timeout "
                        f"timeout_seconds={timeout_sec}"
                    )
                except Exception as e:
                    logger.debug(
                        f"{LOG_PREFIX} 下载候选跳过: illust_id={illust_id} "
                        f"candidate={idx}/{pick_count} reason=download_error "
                        f"error_type={type(e).__name__}"
                    )

            # 统一发送（避免 yield 和 send 混用导致消息拆分）
            if not downloaded:
                yield event.plain_result(
                    await self._reply(
                        event,
                        "😢 所有图片均下载失败，请稍后再试",
                        "图片下载",
                        f"请求发图（标签：{tag or '随机'}，数量：{count}）",
                        record_conversation,
                    )
                )
                return

            # 非 OneBot 平台不支持合并转发，自动降级。
            forward_threshold = self._forward_threshold()
            use_forward = self._should_use_forward(
                downloaded_count=len(downloaded),
                threshold=forward_threshold,
                platform_name=event.get_platform_name(),
            )

            if use_forward:
                # 合并转发模式：所有图片打包成一条聊天记录
                try:
                    self_id = int(event.get_self_id())
                except (TypeError, ValueError):
                    self_id = 0
                nodes = Nodes([])
                for illust, path, _actual_q, _file_size in downloaded:
                    title = illust.get("title", "无标题")
                    illust_id = illust.get("id", "?")
                    content = [
                        Plain(f"🎨 {title} (ID: {illust_id})"),
                        Image.fromFileSystem(path),
                    ]
                    nodes.nodes.append(
                        Node(
                            uin=self_id,
                            name="Pixiv",
                            content=content,
                        )
                    )
                # 如果有下载失败的图片，在合并消息末尾提示
                failed_count = pick_count - len(downloaded)
                if failed_count > 0:
                    failed_ids = [
                        str(il.get("id", "?"))
                        for il in chosen
                        if not any(d[0].get("id") == il.get("id") for d in downloaded)
                    ]
                    nodes.nodes.append(
                        Node(
                            uin=self_id,
                            name="Pixiv",
                            content=[
                                Plain(
                                    f"⚠️ {failed_count} 张图片下载失败（ID: {', '.join(failed_ids)}），已跳过"
                                )
                            ],
                        )
                    )
                # 合并转发（带重试机制）
                max_retries = 3
                forward_success = False
                for attempt in range(1, max_retries + 1):
                    try:
                        await event.send(event.chain_result([nodes]))
                        sent_illust_ids.update(
                            str(illust.get("id") or "")
                            for illust, *_rest in downloaded
                            if illust.get("id")
                        )
                        logger.info(
                            f"{LOG_PREFIX} 合并转发 {len(nodes.nodes)} 条作品"
                            + (f" (第{attempt}次尝试)" if attempt > 1 else "")
                        )
                        forward_success = True
                        break
                    except Exception as e:
                        if attempt < max_retries:
                            wait_sec = attempt * 2
                            logger.info(
                                f"{LOG_PREFIX} 合并转发失败，准备重试: "
                                f"attempt={attempt}/{max_retries} "
                                f"retry_after_seconds={wait_sec} "
                                f"error_type={type(e).__name__}"
                            )
                            await asyncio.sleep(wait_sec)
                        else:
                            friendly_err = self._friendly_send_error(e)
                            logger.warning(
                                f"{LOG_PREFIX} 合并转发失败，降级为逐条发送: "
                                f"attempts={max_retries} reason={friendly_err} "
                                f"error_type={type(e).__name__}"
                            )

                # 合并转发失败，降级为逐条发送
                if not forward_success:
                    await event.send(
                        event.plain_result("⚠️ 合并转发失败，正在逐条发送...")
                    )
                    for illust, path, actual_q, file_size in downloaded:
                        title = illust.get("title", "无标题")
                        illust_id = illust.get("id", "?")
                        content = [
                            Plain(f"🎨 {title} (ID: {illust_id})"),
                            Image.fromFileSystem(path),
                        ]
                        # 逐条发送（带重试机制）
                        for attempt in range(1, max_retries + 1):
                            try:
                                await event.send(event.chain_result(content))
                                sent_illust_ids.add(str(illust.get("id") or ""))
                                logger.info(
                                    f"{LOG_PREFIX} [降级] 作品 {illust_id} 已发送"
                                )
                                await self._record_image_usage(
                                    event,
                                    source_key,
                                    illust,
                                    feature="normal",
                                    user_id=str(event.get_sender_id() or ""),
                                )
                                break
                            except Exception as e:
                                if attempt < max_retries:
                                    await asyncio.sleep(attempt * 2)
                                else:
                                    friendly_err = self._friendly_send_error(e)
                                    logger.error(
                                        f"{LOG_PREFIX} 降级发送失败: "
                                        f"illust_id={illust_id} attempts={max_retries} "
                                        f"reason={friendly_err} "
                                        f"error_type={type(e).__name__}"
                                    )
                                    try:
                                        await event.send(
                                            event.plain_result(
                                                f"⚠️ 作品 {illust_id}「{title}」发送失败，已跳过\n请自行查看 https://www.pixiv.net/en/artworks/{self._artwork_pid(illust, illust_id)}"
                                            )
                                        )
                                    except Exception:
                                        pass
                else:
                    for illust, path, actual_q, file_size in downloaded:
                        await self._record_image_usage(
                            event,
                            source_key,
                            illust,
                            feature="normal",
                            user_id=str(event.get_sender_id() or ""),
                        )
            else:
                # 逐条发送模式
                for illust, path, actual_q, file_size in downloaded:
                    title = illust.get("title", "无标题")
                    illust_id = illust.get("id", "?")
                    content = [
                        Plain(f"🎨 {title} (ID: {illust_id})"),
                        Image.fromFileSystem(path),
                    ]
                    # 逐条发送（带重试机制）
                    max_retries = 3
                    for attempt in range(1, max_retries + 1):
                        try:
                            await event.send(event.chain_result(content))
                            sent_illust_ids.add(str(illust.get("id") or ""))
                            logger.info(
                                f"{LOG_PREFIX} 作品 {illust_id} 已发送"
                                + (f" (第{attempt}次尝试)" if attempt > 1 else "")
                            )
                            await self._record_image_usage(
                                event,
                                source_key,
                                illust,
                                feature="normal",
                                user_id=str(event.get_sender_id() or ""),
                            )
                            break
                        except Exception as e:
                            if attempt < max_retries:
                                wait_sec = attempt * 2
                                logger.info(
                                    f"{LOG_PREFIX} 作品发送失败，准备重试: "
                                    f"illust_id={illust_id} "
                                    f"attempt={attempt}/{max_retries} "
                                    f"retry_after_seconds={wait_sec} "
                                    f"error_type={type(e).__name__}"
                                )
                                await asyncio.sleep(wait_sec)
                            else:
                                friendly_err = self._friendly_send_error(e)
                                logger.error(
                                    f"{LOG_PREFIX} 作品发送失败: "
                                    f"illust_id={illust_id} attempts={max_retries} "
                                    f"reason={friendly_err} "
                                    f"error_type={type(e).__name__}"
                                )
                                try:
                                    await event.send(
                                        event.plain_result(
                                            f"⚠️ 作品 {illust_id}「{title}」发送失败，已跳过\n请自行查看 https://www.pixiv.net/en/artworks/{self._artwork_pid(illust, illust_id)}"
                                        )
                                    )
                                except Exception:
                                    pass
            # 生成图片描述（不直接发到聊天框，仅用于持久记忆）
            description = ""
            if self._cfg_bool("llm_describe_images", False) and downloaded:
                description = await self._describe_images_with_llm(event, tag, downloaded)

            if description and not record_conversation:
                # LLM 工具路径：作为工具结果回传大模型（由框架记录到对话历史，不直发聊天框）
                yield description
            elif (
                record_conversation
                and sent_illust_ids
                and (
                    self._cfg_bool("record_message_to_conversation", True)
                    or bool(description)
                )
            ):
                # 指令/自然语言触发路径：把发送结果与图片描述写入对话历史
                await self._record_conversation(
                    event,
                    tag,
                    count,
                    sent_illust_ids,
                    downloaded,
                    description=description,
                )
        finally:
            for p in temp_paths:
                cleanup(p)
            if pending_illust_ids and self.image_index is not None:
                for illust_id in pending_illust_ids - sent_illust_ids:
                    try:
                        await self.image_index.release_usage(
                            scope=self._event_scope(event),
                            source_key=source_key,
                            illust_id=illust_id,
                            feature="normal_pending",
                        )
                    except Exception:
                        pass

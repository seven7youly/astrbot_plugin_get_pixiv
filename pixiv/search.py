from __future__ import annotations

import asyncio

from astrbot.api.all import Image, Plain, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Node, Nodes

from .constants import AIOCQHTTP_PLATFORM, MAX_IMAGE_COUNT
from .downloader import cleanup


LOG_PREFIX = "[GetPx]"
DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB = 3.0


def _friendly_send_error(error: Exception) -> str:
    """生成友善的发送错误提示。"""
    error_str = str(error).lower()
    if isinstance(error, asyncio.TimeoutError) or "timeout" in error_str:
        return "图片上传超时，可能是图片太大或网络较慢，建议降低图片质量设置"
    if "cdn" in error_str or "upload" in error_str:
        return "图片上传到服务器失败，请稍后再试"
    if "network" in error_str or "connect" in error_str:
        return "网络连接异常，请检查网络后重试"
    return "发送失败，请稍后再试"


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
    ) -> tuple[list[dict], int, str]:
        """优先请求 Lolicon（仅全年龄段），失败后按有无标签回退 Pixiv。"""
        lolicon_client = getattr(self, "lolicon_client", None)
        if lolicon_client and lolicon_client.available:
            try:
                if tag:
                    illusts = await lolicon_client.search(
                        tag, count=count, aspect_ratio=aspect_ratio
                    )
                    source_key = self._source_key(tag, "lolicon")
                else:
                    illusts = await lolicon_client.random(
                        count=count, aspect_ratio=aspect_ratio
                    )
                    source_key = "lolicon:random"
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
        )
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

    async def _fetch_search_candidates(
        self,
        event: AstrMessageEvent,
        tag: str,
        *,
        count: int,
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
                illusts = await self._check_illust_blacklist_and_safety(illusts)
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
        record_conversation: bool = True,
        tag_retry_enabled: bool = False,
    ):
        """搜索并发送图片（仅全年龄段）；Lolicon 失败时按需回退 Pixiv。"""
        # 频率限制
        wait = self._check_rate_limit(event.get_sender_id())
        if wait > 0:
            logger.debug(
                f"{LOG_PREFIX} 搜索请求触发频率限制: retry_after_seconds={wait}"
            )
            async for _r in self._reply(
                event,
                f"⏳ 请求太频繁，请 {wait} 秒后再试",
                "频率限制",
                push=record_conversation,
            ):
                yield _r
            return

        # 参数解析
        max_count = self._cfg_int("max_count", 5, 1, MAX_IMAGE_COUNT)
        try:
            count = max(1, min(int(count_str), max_count)) if count_str else 1
        except (TypeError, ValueError):
            count = 1

        timeout_sec = self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
        quality = self._cfg_str("image_quality", "original")
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
                tag_retry_enabled=tag_retry_enabled,
                tag_retry_limit=tag_retry_limit,
            )
        )
        if reason:
            async for _r in self._reply(
                event,
                self._search_failure_message(reason),
                f"标签：{search_tag or '随机'}",
                push=record_conversation,
            ):
                yield _r
            return
        logger.info(
            f"{LOG_PREFIX} 搜索候选获取完成: "
            f"tag_configured={'yes' if search_tag else 'no'} "
            f"source={_search_source_label(source_key)} "
            f"requested_count={count} quality={_search_quality_label(quality)} "
            f"candidate_count={raw_count}"
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
                async for _r in self._reply(
                    event,
                    "图片去重索引更新失败，请稍后重试",
                    "去重索引",
                    push=record_conversation,
                ):
                    yield _r
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
            async for _r in self._reply(
                event,
                "当前去重范围内没有未发送过的图片了，换个标签或稍后再试",
                "发图去重",
                push=record_conversation,
            ):
                yield _r
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
                async for _r in self._reply(
                    event,
                    "😢 所有图片均下载失败，请稍后再试",
                    "图片下载",
                    push=record_conversation,
                ):
                    yield _r
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
                            friendly_err = _friendly_send_error(e)
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
                                    friendly_err = _friendly_send_error(e)
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
                                friendly_err = _friendly_send_error(e)
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

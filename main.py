"""AstrBot 插件 — 安全插画发图

通过标签搜索插画并发送图片（仅全年龄段），支持 Lolicon 主源、Pixiv 回退、
内容安全过滤（系统内置安全词，不可关闭）、去重和自然语言自动触发。

搜索指令：
    /pv [标签] [数量]           搜索并发送图片
    /pv 安全词                  查看全部安全词
    /pv help                    查看指令帮助

自动触发（需在配置中开启）：
    来一份图                   发送 1 张随机图片
    来三张初音ミク图             搜索标签「初音ミク」发送 3 张
"""

# 注意：不要在本模块使用 `from __future__ import annotations`。
# AstrBot 识别 GreedyStr 的规则：
# - 无默认值时：`annotation is GreedyStr`
# - 有默认值时：`default is GreedyStr`（不是看注解）
# 因此这里使用 GreedyStr 类作为默认哨兵，既保留贪婪参数，又支持直接无参调用。
# 字符串化注解会让贪婪参数失效。

import asyncio
from pathlib import Path
import time

from astrbot.api.all import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.star_tools import StarTools
from .pixiv import FiltersMixin, SearchMixin
from .pixiv.client import PixivClient
from .pixiv.constants import MAX_IMAGE_COUNT
from .pixiv.downloader import ImageDownloader
from .pixiv.index import ImageIndexStore
from .pixiv.llm import LlmMixin
from .pixiv.lolicon import LoliconClient
from .pixiv.safety import BUILTIN_SAFETY_TERMS
from .plugin_api import PluginWebApi

# ──────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────

LOG_PREFIX = "[GetPx]"
PLUGIN_NAME = "astrbot_plugin_get_pixiv"
PLUGIN_VERSION = "v2.2.1Beta"
WEB_INTERNAL_ERROR_MESSAGE = "服务内部错误，请稍后重试"

# ──────────────────────────────────────────────────────────────────────
# 插件主类
# ──────────────────────────────────────────────────────────────────────


class GetPxPlugin(SearchMixin, FiltersMixin, LlmMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self.client: PixivClient | None = None
        self.lolicon_client: LoliconClient | None = None
        self.downloader = ImageDownloader(
            self._cfg_str("lolicon_image_proxy_origins", "")
        )
        self._last_request: dict[str, float] = {}
        self.data_dir: Path | None = None
        self.image_index: ImageIndexStore | None = None
        self.plugin_web_api = PluginWebApi(
            self,
            plugin_name=PLUGIN_NAME,
            log_prefix=LOG_PREFIX,
            internal_error_message=WEB_INTERNAL_ERROR_MESSAGE,
        )
        self._termination_task: asyncio.Task[None] | None = None

    # ──────────────────────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────────────────────

    async def initialize(self):
        """插件加载时初始化图片源客户端与去重索引。"""
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir = Path(data_dir)
        dedupe_days = self._migrate_dedupe_config()
        self._init_client()
        # SQLite DDL/迁移是同步操作，放入线程池避免阻塞事件循环
        self.image_index = await asyncio.to_thread(
            ImageIndexStore,
            data_dir,
            retention_days=dedupe_days,
        )
        await self.image_index.cleanup_old_days(trigger="startup")
        self.plugin_web_api.register()
        logger.info(f"{LOG_PREFIX} 插件已加载: version={PLUGIN_VERSION}")

    def _init_client(self):
        """初始化 Lolicon 主源和可选的 Pixiv 回退客户端。"""
        lolicon_url = self._cfg_str(
            "lolicon_api_url", "https://api.lolicon.app/setu/v2"
        )
        if getattr(self, "lolicon_client", None) is None:
            self.lolicon_client = LoliconClient(
                api_url=lolicon_url,
                exclude_ai=self._cfg_bool("lolicon_exclude_ai", True),
                request_timeout=self._cfg_float(
                    "request_timeout", 30.0, 5.0, 120.0
                ),
            )
        token = self._cfg_str("pixiv_refresh_token")
        if not token:
            logger.info(f"{LOG_PREFIX} 未配置 Pixiv refresh_token，仅使用 Lolicon 主源")
            return

        self.client = PixivClient(
            refresh_token=token,
            request_timeout=self._cfg_float("request_timeout", 30.0, 5.0, 120.0),
        )
        logger.info(f"{LOG_PREFIX} Lolicon 主源和 Pixiv 回退客户端已初始化")

    async def terminate(self):
        """插件卸载/停用时清理资源，并让并发调用等待同一清理任务。"""
        task = self._termination_task
        if task is not None and task.done() and (
            task.cancelled() or task.exception() is not None
        ):
            self._termination_task = None
            task = None
        if task is None:
            task = asyncio.create_task(self._terminate_resources())
            self._termination_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled() and self._termination_task is task:
                self._termination_task = None
            raise
        except Exception:
            if self._termination_task is task:
                self._termination_task = None
            raise

    async def _terminate_resources(self) -> None:
        """执行一次插件资源清理。"""
        if getattr(self, "client", None) is not None:
            try:
                await self.client.close()
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 关闭 Pixiv 客户端失败: "
                    f"error_type={type(exc).__name__}"
                )
            finally:
                self.client = None
        if getattr(self, "lolicon_client", None) is not None:
            try:
                await self.lolicon_client.close()
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 关闭 Lolicon 客户端失败: "
                    f"error_type={type(exc).__name__}"
                )
            finally:
                self.lolicon_client = None
        try:
            await self.downloader.close()
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 关闭图片下载器失败: "
                f"error_type={type(exc).__name__}"
            )
        self._last_request.clear()
        if self.image_index is not None:
            try:
                self.image_index.close()
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 关闭图片索引失败: "
                    f"error_type={type(exc).__name__}"
                )
        self.image_index = None
        logger.info(f"{LOG_PREFIX} 插件已停止")

    # ──────────────────────────────────────────────────────────────
    # 指令：搜索（主指令）
    # ──────────────────────────────────────────────────────────────

    @filter.command("pv")
    async def cmd_pv(self, event: AstrMessageEvent, query: GreedyStr = GreedyStr):
        """搜索并发送图片。参数: [标签] [数量]；/pv 安全词 或 /pv help。"""
        event.stop_event()
        # 框架无参时传入空字符串；直接调用时则会保留默认哨兵。
        raw_query = "" if query is GreedyStr else str(query or "")
        trimmed = raw_query.strip()
        lowered = trimmed.casefold()

        if lowered in ("help", "帮助", "帮助信息", "帮助指令"):
            yield event.plain_result(self._build_help_text())
            return
        if lowered in ("安全词", "安全词列表", "安全詞", "safety"):
            yield event.plain_result(await self._build_safety_words_text())
            return

        if not self._ensure_client_or_error(event):
            yield event.plain_result(
                "⚠️ 图片源暂不可用，请配置 Lolicon API，或填写 pixiv_refresh_token 作为回退"
            )
            return
        tag, count = self._split_tag_and_count(trimmed)
        async for result in self._handle_search(event, tag=tag, count_str=count):
            yield result

    @staticmethod
    def _build_help_text() -> str:
        """生成指令帮助文本。"""
        return (
            "📖 星绘漫游指令帮助\n"
            "──────────────\n"
            "/pv [标签] [数量]\n"
            "    按标签搜索发图，如：/pv 初音ミク 3\n"
            "/pv [数量]\n"
            "    无标签时随机发图，如：/pv 5\n"
            "/pv 安全词\n"
            "    查看全部内置与自定义安全词\n"
            "/pv help\n"
            "    查看本帮助\n"
            "──────────────\n"
            "默认开启自然语言触发，可直接发送「来一份图」「来三张初音ミク图」等触发发图；\n"
            "接入大模型时，也可直接自然对话让 AI 调用发图（如「来张图」「发三张初音ミク的图」）。\n"
            "🔞 仅发送全年龄段图片；安全词为系统内置不可关闭，自定义屏蔽词请在插件 WebUI 中管理。"
        )

    @staticmethod
    def _split_tag_and_count(query: str) -> tuple[str, str]:
        """把 GreedyStr 参数拆成标签与尾部数量；纯数字视为随机发图数量。"""
        tokens = query.split()
        if not tokens:
            return "", ""
        if tokens[-1].isdigit():
            return " ".join(tokens[:-1]), tokens[-1]
        return " ".join(tokens), ""

    async def _build_safety_words_text(self) -> str:
        """汇总内置安全词（系统内置、不可关闭）与自定义安全词。"""
        lines = [
            f"🔒 内置安全词（{len(BUILTIN_SAFETY_TERMS)}，系统内置不可关闭）："
        ]
        lines.append("、".join(BUILTIN_SAFETY_TERMS))
        custom: list[str] = []
        if self.image_index is not None:
            try:
                custom = [
                    str(item.get("term") or "")
                    for item in await self.image_index.list_safety_terms()
                    if str(item.get("term") or "")
                ]
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 读取自定义安全词失败: error_type={type(exc).__name__}"
                )
        lines.append(
            f"✏️ 自定义安全词（{len(custom)}）："
            + ("、".join(custom) if custom else "无")
        )
        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────
    # LLM 工具：让 AstrBot 大模型在对话中以自然语言调用发图
    # ──────────────────────────────────────────────────────────────

    @filter.llm_tool(name="search_images")
    async def tool_llm_search_images(
        self,
        event: AstrMessageEvent,
        tag: str = "",
        count: str = "1",
    ):
        """搜索并发送插画图片给用户。

        Args:
            tag(string): 插画搜索标签，例如"初音未来"；可为空字符串表示随机取图
            count(string): 要发送的图片数量（1-5）。用户未明确说明数量时必须填"1"
        """
        if not self._cfg_bool("auto_trigger_enabled", True):
            yield event.plain_result("⚠️ 自然语言发图已关闭，请在插件配置中打开 auto_trigger_enabled")
            return
        if not self._ensure_client_or_error(event):
            yield event.plain_result(
                "⚠️ 图片源暂不可用，请配置 Lolicon API，或填写 pixiv_refresh_token 作为回退"
            )
            return
        try:
            async for result in self._handle_search(
                event,
                tag=str(tag or "").strip(),
                count_str=str(count or "1").strip(),
                record_conversation=False,
                tag_retry_enabled=True,
            ):
                yield result
        except Exception as exc:
            logger.error(
                f"{LOG_PREFIX} search_images 工具执行异常: "
                f"error_type={type(exc).__name__} error={exc}"
            )
            yield event.plain_result("⚠️ 发图服务执行出错，请稍后再试")

    # ──────────────────────────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────────────────────────

    def _check_rate_limit(self, user_id: str) -> int:
        """检查用户请求频率，返回需等待秒数（0 表示可立即请求）。"""
        rate_limit = self._cfg_int("rate_limit_seconds", 3, 0, 60)
        if rate_limit <= 0:
            return 0
        now = time.monotonic()
        if len(self._last_request) > 1024:
            cutoff = now - max(float(rate_limit) * 2, 60.0)
            self._last_request = {
                key: timestamp
                for key, timestamp in self._last_request.items()
                if timestamp >= cutoff
            }
        last = self._last_request.get(user_id, 0.0)
        elapsed = now - last
        if elapsed < rate_limit:
            return int(rate_limit - elapsed) + 1
        self._last_request[user_id] = now
        return 0

    # ──────────────────────────────────────────────────────────────
    # 配置读取（带类型校验）
    # ──────────────────────────────────────────────────────────────

    def _migrate_dedupe_config(self) -> int:
        config = getattr(self, "config", None)
        if config is None:
            return 1
        if not self._cfg_bool("dedupe_days_migrated", False):
            legacy_value = self._cfg_float("dedupe_ttl_hours", 24.0, 0.0, 24.0)
            config["dedupe_days"] = 0 if legacy_value <= 0 else 1
            config["dedupe_days_migrated"] = True
            persisted = False
            save_config = getattr(config, "save_config", None)
            if callable(save_config):
                try:
                    save_config()
                    persisted = True
                except Exception as exc:
                    logger.warning(
                        f"{LOG_PREFIX} 保存去重配置迁移结果失败: "
                        f"error_type={type(exc).__name__}"
                    )
            logger.info(
                f"{LOG_PREFIX} 已迁移旧去重配置: "
                f"dedupe_ttl_hours={legacy_value:g} -> "
                f"dedupe_days={config['dedupe_days']}, persisted={persisted}"
            )
        return self._cfg_int("dedupe_days", 1, 0, 7)

    def _cfg_str(self, key: str, default: str = "") -> str:
        val = self.config.get(key, default)
        return str(val).strip() if val is not None else default

    def _cfg_int(self, key: str, default: int, lo: int, hi: int) -> int:
        raw = self.config.get(key, default)
        if isinstance(raw, (bool, float)):
            return default
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return default
        return val if lo <= val <= hi else default

    def _forward_threshold(self) -> int:
        """Return the merged-forward threshold, accepting the retired bool setting."""
        if "forward_threshold" in self.config:
            return self._cfg_int("forward_threshold", 1, 0, MAX_IMAGE_COUNT)
        return 0 if self._cfg_bool("send_as_forward", True) else MAX_IMAGE_COUNT

    def _cfg_float(self, key: str, default: float, lo: float, hi: float) -> float:
        try:
            val = float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return val if lo <= val <= hi else default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        val = self.config.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val) if val is not None else default

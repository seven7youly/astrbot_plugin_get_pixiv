"""AstrBot 插件 — 安全插画发图

通过标签搜索插画并发送图片，支持 Lolicon 主源、Pixiv 回退、内容安全过滤、去重和自然语言自动触发。

搜索指令：
    /pv [标签] [数量]           搜索并发送图片
    /pv [标签] [数量] r18       单次取消 R18 限制（仅本次生效）
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
import re
import time

from astrbot.api.all import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.star_tools import StarTools
from .pixiv import DeliveryMixin, FiltersMixin, SearchMixin
from .pixiv.client import PixivClient
from .pixiv.constants import MAX_IMAGE_COUNT
from .pixiv.downloader import ImageDownloader
from .pixiv.index import ImageIndexStore
from .pixiv.lolicon import LoliconClient
from .pixiv.safety import BUILTIN_SAFETY_TERMS, safety_term_config_key
from .plugin_api import PluginWebApi

# ──────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────

LOG_PREFIX = "[GetPx]"
PLUGIN_NAME = "astrbot_plugin_get_pixiv"
PLUGIN_VERSION = "v2.0.1Beta"
WEB_INTERNAL_ERROR_MESSAGE = "服务内部错误，请稍后重试"

AUTO_TRIGGER_PATTERN = r"^/?(来\s*(.*?)(份|个|张|点))(.*?)(福利|色|瑟|涩|塞)?图$"


CHINESE_NUMBER_MAP = {
    "一": "1",
    "二": "2",
    "两": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
    "十": "10",
}

# ──────────────────────────────────────────────────────────────────────
# 插件主类
# ──────────────────────────────────────────────────────────────────────


class GetPxPlugin(SearchMixin, DeliveryMixin, FiltersMixin, Star):
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
                allow_r18=self._cfg_bool("allow_r18", False),
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
        """搜索并发送图片。参数: [标签] [数量]；末尾加 r18 单次取消 R18 限制；/pv 安全词 或 /pv help。"""
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

        # 末尾 r18：单次取消 R18 限制（仅本次指令生效）
        allow_r18_override = False
        tokens = trimmed.split()
        if tokens and tokens[-1].casefold() == "r18":
            allow_r18_override = True
            trimmed = " ".join(tokens[:-1]).strip()

        if not self._ensure_client_or_error(event):
            yield event.plain_result(
                "⚠️ 图片源暂不可用，请配置 Lolicon API，或填写 pixiv_refresh_token 作为回退"
            )
            return
        tag, count = self._split_tag_and_count(trimmed)
        async for result in self._handle_search(
            event,
            tag=tag,
            count_str=count,
            allow_r18_override=allow_r18_override,
        ):
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
            "/pv [标签] [数量] r18\n"
            "    单次取消 R18 限制（仅本次生效），如：/pv 初音ミク 2 r18\n"
            "/pv 安全词\n"
            "    查看全部内置与自定义安全词\n"
            "/pv help\n"
            "    查看本帮助\n"
            "──────────────\n"
            "开启 auto_trigger_enabled 后，可直接发送「来一份图」「来三张初音ミク图」等触发发图；\n"
            "接入大模型时，也可直接自然对话让 AI 调用发图（如「来张图」「发三张初音ミク的图」）。\n"
            "安全词开关与自定义屏蔽词请在插件 WebUI「内容安全设置」中管理。"
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
        """汇总内置安全词（含开关状态）与自定义安全词。"""
        builtin_on: list[str] = []
        builtin_off: list[str] = []
        for term in BUILTIN_SAFETY_TERMS:
            if self._cfg_bool(safety_term_config_key(term), True):
                builtin_on.append(term)
            else:
                builtin_off.append(term)
        lines = [f"🔒 内置安全词（{len(builtin_on) + len(builtin_off)}）："]
        lines.append("✅ 启用：" + "、".join(builtin_on))
        if builtin_off:
            lines.append("❌ 停用：" + "、".join(builtin_off))
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

    @filter.regex(AUTO_TRIGGER_PATTERN)
    async def auto_trigger(self, event: AstrMessageEvent):
        """自然语言自动触发发图。"""
        if not self._cfg_bool("auto_trigger_enabled", False):
            return
        if not self._ensure_client_or_error(event):
            return

        message = event.get_message_str().strip()
        match = re.match(AUTO_TRIGGER_PATTERN, message)
        if not match:
            return

        event.stop_event()

        count_part = match.group(2).strip() if match.group(2) else ""
        tag_part = (match.group(4) or "").strip()

        # 解析数量：中文数字、阿拉伯数字
        count_str = ""
        raw = count_part if count_part else "1"
        if raw.isdigit():
            count_str = raw
        else:
            for cn_digit, arabic in CHINESE_NUMBER_MAP.items():
                if raw == cn_digit:
                    count_str = arabic
                    break
            if not count_str:
                count_str = "1"

        logger.info(
            f"{LOG_PREFIX} 自然语言触发: count={count_str} "
            f"tag_configured={'yes' if tag_part else 'no'}"
        )
        async for result in self._handle_search(
            event, tag=tag_part, count_str=count_str
        ):
            yield result

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
            tag(string): 插画搜索标签，例如"初音ミク"；可为空字符串表示随机取图
            count(string): 要发送的图片数量（1-5），例如"3"
        """
        if not self._cfg_bool("auto_trigger_enabled", False):
            yield event.plain_result("⚠️ 自然语言发图未开启，请在插件配置中打开 auto_trigger_enabled")
            return
        if not self._ensure_client_or_error(event):
            yield event.plain_result(
                "⚠️ 图片源暂不可用，请配置 Lolicon API，或填写 pixiv_refresh_token 作为回退"
            )
            return
        async for result in self._handle_search(
            event,
            tag=str(tag or "").strip(),
            count_str=str(count or "1").strip(),
            record_conversation=False,
        ):
            yield result

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

from __future__ import annotations

from typing import Any

from astrbot.api.all import logger
from quart import jsonify, request

from ..pixiv.safety import (
    BUILTIN_SAFETY_TERMS,
    normalize_safety_text,
)


CONFIG_META: dict[str, dict] = {
    "pixiv_refresh_token": {
        "label": "Pixiv refresh_token",
        "type": "str",
        "default": "",
        "hint": "可选。Lolicon 主源失败时用于 Pixiv 回退；修改后需重载插件生效。",
    },
    "lolicon_api_url": {
        "label": "Lolicon API 地址",
        "type": "str",
        "default": "https://api.lolicon.app/setu/v2",
        "hint": "首选图片源地址，留空停用 Lolicon；修改后需重载插件生效。",
    },
    "lolicon_exclude_ai": {
        "label": "Lolicon 排除 AI 作品",
        "type": "bool",
        "default": True,
    },
    "lolicon_image_proxy_origins": {
        "label": "Lolicon 图片反代地址",
        "type": "text",
        "default": "",
        "hint": "每行一个 http(s) origin；修改后需重载插件生效。",
    },
    "filter_manga": {
        "label": "过滤漫画",
        "type": "bool",
        "default": True,
    },
    "max_count": {
        "label": "单次最大发送数量",
        "type": "int",
        "default": 5,
        "min": 1,
        "max": 20,
    },
    "dedupe_days": {
        "label": "图片去重天数",
        "type": "int",
        "default": 1,
        "min": 0,
        "max": 7,
        "hint": "0=关闭；1=当天；2-7=最近对应天数。",
    },
    "request_timeout": {
        "label": "下载超时（秒）",
        "type": "float",
        "default": 30.0,
        "min": 5.0,
        "max": 120.0,
    },
    "image_quality": {
        "label": "图片质量",
        "type": "select",
        "default": "original",
        "options": ["original", "large", "medium"],
    },
    "auto_downgrade_original_mb": {
        "label": "原图自动降级阈值（MiB）",
        "type": "float",
        "default": 3.0,
        "min": 0.0,
        "max": 100.0,
        "hint": "原图超过该大小时自动降级；0=禁用。",
    },
    "forward_threshold": {
        "label": "合并转发阈值",
        "type": "int",
        "default": 1,
        "min": 0,
        "max": 20,
        "hint": "仅 aiocqhttp 生效；0=始终合并，1=超过 1 张合并。",
    },
    "auto_trigger_enabled": {
        "label": "自然语言自动触发",
        "type": "bool",
        "default": True,
        "group": "llm",
    },
    "rate_limit_seconds": {
        "label": "请求频率限制（秒）",
        "type": "int",
        "default": 3,
        "min": 0,
        "max": 60,
        "hint": "同一用户请求最小间隔，0=禁用。",
    },
    "record_message_to_conversation": {
        "label": "记录发送结果到对话",
        "type": "bool",
        "default": True,
        "group": "llm",
    },
    "llm_describe_images": {
        "label": "发送后让大模型查看图片",
        "type": "bool",
        "default": True,
        "group": "llm",
        "hint": "需多模态大模型；描述只写入对话历史，不发送到聊天框。",
    },
    "llm_describe_prompt": {
        "label": "图片描述提示词",
        "type": "text",
        "default": "Please briefly describe the content and atmosphere of this image. If there are people in the image, please describe their actions, expressions, postures, clothing, and facial expressions in detail.",
        "group": "llm",
    },
    "llm_search_tag_retries": {
        "label": "无结果时更换标签次数",
        "type": "int",
        "default": 2,
        "min": 0,
        "max": 5,
        "group": "llm",
        "hint": "自然语言调用发图时，标签无结果则让大模型更换/优化标签重试，最多该次数；全部失败才提示无结果。",
    },
    "llm_natural_replies": {
        "label": "用大模型人格改写回复",
        "type": "bool",
        "default": True,
        "group": "llm",
        "hint": "功能提示按当前会话人格改写；无法调用或关闭时用默认文案。",
    },
}


class PluginWebApi:
    """Register and serve the plugin content-safety management endpoints."""

    def __init__(
        self,
        plugin: Any,
        *,
        plugin_name: str,
        log_prefix: str,
        internal_error_message: str,
    ) -> None:
        self.plugin = plugin
        self.plugin_name = plugin_name
        self.log_prefix = log_prefix
        self.internal_error_message = internal_error_message

    def __getattr__(self, name: str) -> Any:
        return getattr(self.plugin, name)

    def register(self) -> None:
        routes = (
            (
                "content-safety",
                self.content_safety,
                ["GET"],
                "Get content safety policy and term states",
            ),
            (
                "content-safety/terms/add",
                self.content_safety_term_add,
                ["POST"],
                "Add custom safety term",
            ),
            (
                "content-safety/terms/remove",
                self.content_safety_term_remove,
                ["POST"],
                "Remove custom safety term",
            ),
            (
                "config",
                self.config_get,
                ["GET"],
                "Get plugin configuration",
            ),
            (
                "config",
                self.config_update,
                ["POST"],
                "Update plugin configuration",
            ),
        )
        for path, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{self.plugin_name}/{path}", handler, methods, description
            )

    def internal_error(self, action: str, exc: Exception):
        logger.error(
            f"{self.log_prefix} Web API {action}失败: "
            f"error_type={type(exc).__name__}"
        )
        return jsonify({"success": False, "error": self.internal_error_message}), 500

    async def content_safety(self):
        if self.plugin.image_index is None:
            return self._unavailable("内容安全数据尚未初始化")
        try:
            custom_terms = await self.plugin.image_index.list_safety_terms()
            return jsonify(
                {
                    "success": True,
                    "rating_policy": "general_only",
                    "rating_label": "🔞 仅发送全年龄段图片",
                    "builtin_terms": list(BUILTIN_SAFETY_TERMS),
                    "custom_terms": custom_terms,
                }
            )
        except Exception as exc:
            return self.internal_error("读取内容安全策略", exc)

    async def content_safety_term_add(self):
        if self.plugin.image_index is None:
            return self._unavailable("内容安全数据尚未初始化")
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        term = str(payload.get("term") or "").strip()
        if normalize_safety_text(term) in {
            normalize_safety_text(item) for item in BUILTIN_SAFETY_TERMS
        }:
            return jsonify({"success": False, "error": "该词已经属于内置安全词"}), 400
        try:
            await self.plugin.image_index.add_safety_term(term, added_by="web")
            return jsonify({"success": True, "term": term})
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return self.internal_error("添加自定义安全词", exc)

    async def content_safety_term_remove(self):
        if self.plugin.image_index is None:
            return self._unavailable("内容安全数据尚未初始化")
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        term = str(payload.get("term") or "").strip()
        if normalize_safety_text(term) in {
            normalize_safety_text(item) for item in BUILTIN_SAFETY_TERMS
        }:
            return jsonify({"success": False, "error": "内置安全词不能删除"}), 400
        try:
            removed = await self.plugin.image_index.remove_safety_term(term)
            if not removed:
                return jsonify({"success": False, "error": "自定义安全词不存在"}), 404
            return jsonify({"success": True, "term": term})
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return self.internal_error("删除自定义安全词", exc)

    async def config_get(self):
        try:
            values: dict[str, Any] = {}
            for key, meta in CONFIG_META.items():
                values[key] = self.plugin.config.get(key, meta["default"])
            return jsonify({"success": True, "config": values, "schema": CONFIG_META})
        except Exception as exc:
            return self.internal_error("读取插件配置", exc)

    async def config_update(self):
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        updates = payload.get("config")
        if not isinstance(updates, dict):
            return jsonify({"success": False, "error": "config 必须是对象"}), 400
        config = self.plugin.config
        applied: dict[str, Any] = {}
        errors: list[str] = []
        for key, value in updates.items():
            meta = CONFIG_META.get(key)
            if meta is None:
                continue
            try:
                coerced = self._coerce_config_value(key, value, meta)
                config[key] = coerced
                applied[key] = coerced
            except ValueError as exc:
                errors.append(f"{key}: {exc}")
        if errors:
            return jsonify({"success": False, "error": "; ".join(errors)}), 400
        persisted = False
        save_config = getattr(config, "save_config", None)
        if callable(save_config):
            try:
                save_config()
                persisted = True
            except Exception as exc:
                logger.warning(
                    f"{self.log_prefix} 保存插件配置失败: "
                    f"error_type={type(exc).__name__}"
                )
        logger.info(
            f"{self.log_prefix} 插件配置已更新: "
            f"keys={sorted(applied)} persisted={persisted}"
        )
        return jsonify({"success": True, "applied": applied, "persisted": persisted})

    @staticmethod
    def _coerce_config_value(key: str, value: Any, meta: dict) -> Any:
        vtype = meta.get("type")
        if vtype == "bool":
            if not isinstance(value, bool):
                raise ValueError("必须是布尔值")
            return value
        if vtype in ("int", "float"):
            try:
                num = float(value) if vtype == "float" else int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("必须是数字") from exc
            lo = meta.get("min")
            hi = meta.get("max")
            if (lo is not None and num < lo) or (hi is not None and num > hi):
                raise ValueError(f"必须在 {lo}~{hi} 之间")
            return num
        if vtype == "select":
            if value not in meta.get("options", []):
                raise ValueError("取值不合法")
            return value
        return str(value or "")

    @staticmethod
    async def _request_json_object() -> dict[str, Any] | None:
        payload = await request.get_json(silent=True)
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _unavailable(message: str = "插件数据尚未初始化"):
        return jsonify({"success": False, "error": message}), 503

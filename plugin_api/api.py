from __future__ import annotations

from typing import Any

from astrbot.api.all import logger
from quart import jsonify, request

from ..pixiv.safety import (
    BUILTIN_SAFETY_TERMS,
    normalize_safety_text,
    safety_term_config_key,
)


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
                "content-safety/r18-toggle",
                self.content_safety_r18_toggle,
                ["POST"],
                "Toggle the global R18 allowance",
            ),
            (
                "content-safety/terms/toggle",
                self.content_safety_term_toggle,
                ["POST"],
                "Toggle a built-in safety term",
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
            builtin_terms = [
                {
                    "term": term,
                    "enabled": self.plugin._cfg_bool(
                        safety_term_config_key(term), True
                    ),
                }
                for term in BUILTIN_SAFETY_TERMS
            ]
            return jsonify(
                {
                    "success": True,
                    "rating_policy": "general_only",
                    "rating_label": "仅允许普通作品（allow_r18 可配置开启 R18）",
                    "allow_r18": self.plugin._cfg_bool("allow_r18", False),
                    "builtin_terms": builtin_terms,
                    "custom_terms": custom_terms,
                }
            )
        except Exception as exc:
            return self.internal_error("读取内容安全策略", exc)

    async def content_safety_r18_toggle(self):
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        if not isinstance(payload.get("enabled"), bool):
            return jsonify({"success": False, "error": "enabled 必须是布尔值"}), 400
        enabled = payload["enabled"]
        config = self.plugin.config
        config["allow_r18"] = enabled
        persisted = False
        save_config = getattr(config, "save_config", None)
        if callable(save_config):
            try:
                save_config()
                persisted = True
            except Exception as exc:
                logger.warning(
                    f"{self.log_prefix} 保存 R18 开关状态失败: "
                    f"enabled={enabled} error_type={type(exc).__name__}"
                )
        logger.info(
            f"{self.log_prefix} R18 开关状态已更新: "
            f"enabled={enabled} persisted={persisted}"
        )
        return jsonify({"success": True, "enabled": enabled})

    async def content_safety_term_toggle(self):
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        term = str(payload.get("term") or "").strip()
        if term not in BUILTIN_SAFETY_TERMS:
            return jsonify({"success": False, "error": "未知的内置安全词"}), 400
        if not isinstance(payload.get("enabled"), bool):
            return jsonify({"success": False, "error": "enabled 必须是布尔值"}), 400
        enabled = payload["enabled"]
        config = self.plugin.config
        config[safety_term_config_key(term)] = enabled
        persisted = False
        save_config = getattr(config, "save_config", None)
        if callable(save_config):
            try:
                save_config()
                persisted = True
            except Exception as exc:
                logger.warning(
                    f"{self.log_prefix} 保存安全词开关状态失败: "
                    f"term={term} error_type={type(exc).__name__}"
                )
        logger.info(
            f"{self.log_prefix} 安全词状态已更新: "
            f"term={term} enabled={enabled} persisted={persisted}"
        )
        return jsonify({"success": True, "term": term, "enabled": enabled})

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

    @staticmethod
    async def _request_json_object() -> dict[str, Any] | None:
        payload = await request.get_json(silent=True)
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _unavailable(message: str = "插件数据尚未初始化"):
        return jsonify({"success": False, "error": message}), 503

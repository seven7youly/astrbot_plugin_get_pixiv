# 星绘漫游

AstrBot 安全发图插件。本插件精简自 [astrbot_plugin_get_px](https://github.com/shitianyaa/astrbot_plugin_get_px)，仅保留获取图片功能，已移除签到功能。

- 首选图片源：[Lolicon API](https://api.lolicon.app/)，无需 Token
- 可选回退：Pixiv（需 `pixiv_refresh_token`）
- 内容安全：默认仅普通分级（`allow_r18` 可配置允许 R18）；内置安全词（r18、裸体、血腥等）逐词单独开关，在插件 WebUI「内容安全设置」页面中以开关按钮管理，默认全部开启；支持自定义屏蔽词
- 稳定性：0–7 天自然日去重、发送失败重试、临时文件自动清理

## 安装

1. 在 AstrBot WebUI 插件页安装本插件：
   - 下载本仓库 zip 后选择「导入压缩包」
   - 或粘贴仓库地址
2. 默认使用 Lolicon API，无需 Token；需要 Pixiv 作为备用时再填写 `pixiv_refresh_token`。

## 指令

| 指令 | 说明 | 示例 |
| --- | --- | --- |
| `/pv [标签] [数量]` | 按标签搜索发图 | `/pv 初音ミク 3` |
| `/pv [数量]` | 无标签时随机发图 | `/pv 5` |
| `/pv [标签] [数量] r18` | 单次取消 R18 限制（仅本次生效） | `/pv 初音ミク 2 r18` |
| `/pv 安全词` | 查看全部内置与自定义安全词 | `/pv 安全词` |
| `/pv help` | 查看全部指令帮助 | `/pv help` |

## WebUI 内容安全设置

插件页提供「内容安全设置」面板，可逐词开关 36 个内置安全词（按下=启用，未按下=停用），添加/删除自定义屏蔽词，并提供「🔞 R18 限制开关」（按下=允许 R18，红色填充）。安全词的控制仅能在该页面进行。

开启 `auto_trigger_enabled` 后可自然语言触发：

| 触发语 | 效果 |
| --- | --- |
| `来一份图` | 1 张随机图片 |
| `来三张初音ミク图` | 搜索并发送 3 张 |
| `来张风景图` | 搜索并发送 1 张 |

## 依赖

```text
pixivpy-async
aiohttp
Pillow
```

> 主要面向 QQ OneBot / aiocqhttp。其他平台会尽量降级为逐条发送，请自行测试兼容性。

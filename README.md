# 画境拾珍·发图

AstrBot 安全发图插件（精简自原项目，仅保留获取图片功能，已移除签到）。

- 首选图片源：[Lolicon API](https://api.lolicon.app/)，无需 Token
- 可选回退：Pixiv（需 `pixiv_refresh_token`）
- 内容安全：默认仅普通分级（`allow_r18` 可配置允许 R18）、内置安全词过滤可开关（`safety_filter_enabled`），支持自定义反代地址
- 稳定性：0–7 天自然日去重、发送失败重试、临时文件自动清理

## 安装

1. 在 AstrBot WebUI 插件页安装本插件：
   - 下载本仓库 zip 后选择「导入压缩包」
   - 或粘贴仓库地址
2. 默认使用 Lolicon API，无需 Token；需要 Pixiv 作为备用时再填写 `pixiv_refresh_token`。

## 指令

| 指令 | 说明 | 示例 |
| --- | --- | --- |
| `/p [标签] [数量]` | 按标签搜索发图 | `/p 初音ミク 3` |
| `/p [数量]` | 无标签时随机发图 | `/p 5` |

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

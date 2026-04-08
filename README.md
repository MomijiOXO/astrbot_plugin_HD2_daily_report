# Bilibili 今日快报插件

自动抓取 B站 UP主「银河快报」每日发布的哔哩哔哩动态图片，并推送到群聊。

## 功能

- **手动触发**：发送「今日快报」关键词获取当天快报图片
- **定时推送**：每日自动在指定时间抓取并推送快报
- **扫码登录**：B站登录态失效后，通过二维码重新登录
- **缓存机制**：已抓取的快报会缓存，避免重复下载
- **随机战备**：顾名思义，为民主之路增添乐趣

## 指令

| 指令 | 说明 | 权限 |
|------|------|------|
| `今日快报` | 手动获取当天快报 | 所有人 |
| `随机战备` | 遵循一定逻辑的随机战备 | 所有人 |
| `全随机战备` | 完全随机，大锅炖 | 所有人 
| `bili_qrcode` | 扫码登录B站 | 管理员 |
| `bili_qrcode_reset` | 重置登录环境并重新扫码 | 管理员 |

## 配置项

在 AstrBot 管理面板或 `config.json` 中配置：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `schedule_send_time` | string | `09:00` | 每日自动推送时间（HH:MM格式） |
| `whitelist_groups` | list | `[]` | 白名单群组（仅在这些群发送，为空则不限制） |
| `blacklist_groups` | list | `[]` | 黑名单群组（在这些群不发送，为空则不限制） |
| `admin_user_ids` | list | `[]` | 管理员用户ID（可触发登录指令） |
| `enable_debug_log` | bool | `false` | 是否启用调试日志 |

## 报错

- 如果遇到报错说playwright install这样的报错，是因为docker或是你本地没有安装对应的浏览器。
- 如果是docker需要进入容器中输入python -m playwright install。
- linux可以直接输入python -m playwright install。
- windwos需要打开cmd，然后输入python -m playwright install。

## 支持

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)

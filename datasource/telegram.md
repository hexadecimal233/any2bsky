# Telegram

Export tool: [popstas/telegram-download-chat](https://github.com/popstas/telegram-download-chat)（CLI，下载聊天记录为 JSON / TXT）。

macOS 下输出目录默认在 `~/Library/Application Support/telegram-download-chat/downloads/`。

## Export layout

```
downloads/
└── <chat_name>/
    ├── messages.json    完整消息元数据（JSON 数组）
    ├── messages.txt     人读/LLM 可读文本
    └── attachments/     媒体附件（使用 --media 时下载）
```

`messages.json` 是一个消息数组；每条含 `id`、`date`（ISO-8601）、`message`（正文）、`from_id` / `user_display_name`、`reply_to`、`media`、`reactions`。使用 `--media` 时，媒体以 `attachment_path`（相对 `attachments/` 的路径）引用附件文件。

## Conversion rules

- 正文取 `message` 字段进 `text`。
- 媒体取 `attachment_path`，解析为相对 `<chat_name>/` 的 `attachments/<path>`；仅保留图片 / 视频扩展名，磁盘上不存在的媒体被跳过，文档/音频/贴纸等不转换。
- 服务消息（`action` 非空：加群/置顶等）不转换。
- 频道帖子的访客评论（带 `comment_of`）不转换，与 QQ空间留言板同理。
- 事件按 `date` 升序排序；账号标题取聊天目录名。

Source key: `telegram`

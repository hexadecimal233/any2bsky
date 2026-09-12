# QQ空间 (QZone)

Export tool: [ShunCai/QZoneExport](https://github.com/ShunCai/QZoneExport)（QQ空间导出助手）。

Bulk cleanup helper: [24024-SOS/qzone-batch-delete](https://github.com/24024-SOS/qzone-batch-delete)（批量删除说说/留言）。

## Export layout

`QZoneExport` 打包下载后得到 `QQ空间备份_<qq>/`，合并媒体文件后交给本适配器的目录：

```
QQ空间备份_<qq>/
├── Messages/json/messages.json   说说（本人的动态）
├── Albums/json/albums.json       相册（独立于说说）
├── Videos/json/videos.json       视频
├── Shares/json/shares.json       分享
├── Boards/json/boards.json       留言板（不转换）
└── Common/json/user.json         账号昵称
```

## Conversion rules

- `Boards`（留言板）**不转换**：那是访客留在本人主页的评论，不是本人发布的内容。
- 每个类别展开成按时间排序的事件列表，全部写入同一个 `events.json`。
- 媒体一律用**相对路径**引用；相对路径在磁盘上不存在的图片会被丢弃（导出工具可能未下载成功）。
- 相册图片的说明文字进入每张图片的 alt 文本（上限 1000 字符），不进入正文。
- 相册事件按最早事件为锚点，10 分钟内合并为一条；视频、说说、分享永不合并。
- 分享通过事件级 `rt` 记录原文标题/URL/来源。
- 账号标题取自 `Common/json/user.json` 的 `nickname` / `account` / `qq`。

Source key: `qzone`

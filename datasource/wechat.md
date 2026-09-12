# 微信朋友圈 (WeChat Moments)

Export tool: [Panther114/Weport](https://github.com/Panther114/Weport)（本地微信聊天记录/朋友圈导出工具，Windows / macOS）。

## Export layout

适配器扫描根目录下任意含顶层 `posts` 数组的 JSON 文件，媒体按 `media[].localPath` 的相对路径引用：

```
<root>/
├── 朋友圈导出_*.json    { exportTime, totalPosts, posts: [...] }
└── media/               图片与视频（按 localPath 引用）
```

每个 post 形如：

```json
{
  "id": "...",
  "username": "...",
  "nickname": "...",
  "createTime": 1234567890,
  "createTimeStr": "...",
  "contentDesc": "...",
  "type": 1,
  "media": [{ "url": "...", "thumb": "...", "localPath": "..." }],
  "likes": [],
  "comments": [],
  "location": ""
}
```

## Post types

| type | 含义 |
|---|---|
| 1 | 文本 + 图片 |
| 2 | 纯文本 |
| 3 | 文本 + 图片 |
| 7 | 纯图片（无文本） |
| 15 | 视频 |
| 42 | QQ 音乐分享 |
| 47 | 网易云音乐分享 |

## Conversion rules

- 媒体按 `localPath` 相对路径引用；磁盘上不存在的媒体被跳过，仅有远程 URL 的媒体不转换。
- 视频扩展名（`.mp4` / `.mov` / `.webm`）识别为 `video`，其余按 `image` 处理。
- 音乐分享（42 / 47）转换为事件级 `rt`（`source` 为 `QQ音乐` 或 `网易云音乐`）。
- 账号标题取第一条 post 的 `nickname`。
- 事件按 `createTime` 升序排序。

Source key: `wechat`

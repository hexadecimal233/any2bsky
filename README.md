# any2bsky

把 QQ空间导出等社交数据迁移到 Bluesky 的流水线：
**datasource → 通用事件流 → PDS 合规任务 → DAG 调度的真实发帖**（可断点续传）。

```
导出目录(只读)  ──convert──▶  data/<源>/events.json
                              ──plan──▶  data/<源>/tasks.json (+ compressed/)
                                        ──dry / live──▶  checkpoint 回写 tasks.json
```

## 特性

- **datasource 抽象**：`qzone` 现已内置（留言板自动忽略、说说/相册/视频/分享分类型）；新增数据源只需一个 `build_events(root)` 实现
- **生态合规**（数值均标注来源）：
  - 文本 ≤ 300 graphemes/贴，超长自动 tweetstorm 拆贴（带 `1/N` 序号 + reply 链）
  - 图片每贴 ≤ 4 张（上限读取自 atproto lexicon），单张 ≤ 4000px / ≤ 2MB，超限按长边等比缩放 + **AVIF** 压缩（lossless 起逐级降质）
  - 视频 ≥ 10min 或 > 300MB **硬失败**；合规视频交由官方 `app.bsky.video` 管线转码（阻塞轮询）
  - 分享(repost)有链接 → **link facet**；无链接 → 降级为文本
  - 相册照片描述进 **alt**，不进正文
- **发布时间还原**：`createdAt` 用原帖时间，不丢失
- **DAG 执行器**：`reply_to` 是唯一依赖边，**任何任务（无论媒体/文本）都必须等父贴成功后才执行**；媒体任务由 `--heavy` 信号量全局限流（只有互不依赖的媒体任务真正并发），纯文本任务逐个串行；回复链因此天然串行、不占用并发额度
- **会话缓存**：`login` 交互式登录（密码隐藏），会话存 `data/session.json`，过期自动重登
- **全量数据收敛 `./data`**：数据源目录永远只读

## 安装

```bash
uv sync            # 或 pip install -e .
```

## 快速开始

```bash
# 1. 登录（交互式，密码隐藏；缓存到 data/session.json）
python cli.py login

# 2. 转换 + （可选）人工筛选事件
python cli.py convert <QQ空间导出目录>
python cli.py filter <QQ空间导出目录>     # 浏览器里勾选保留/删除，保存后写回 events.json

# 3. 规划（读取已筛选的 events.json；drop 的事件被跳过）
python cli.py plan   <QQ空间导出目录>

# 4. 预览（在不写真实帖子的 tasks.dry.json 上干跑）
python cli.py dry <QQ空间导出目录> --heavy 3

# 5. 真实发帖（只复用缓存会话，不需要凭证参数）
python cli.py live <QQ空间导出目录> --heavy 2
```

## CLI 参考（子命令，无布尔开关）

| 命令 | 说明 |
|---|---|
| `sources` | 列出已注册数据源 |
| `login [--handle H] [--password P] [--session F]` | 交互式登录并缓存会话；非交互环境可显式传参 |
| `convert <root> [--source qzone]` | 数据源 → `data/<源>/events.json` |
| `filter <root> [--port P]` | **迷你本地 server + 浏览器编辑器**：手动保留/删除事件（勾选→后端写回 `drop` 标记） |
| `plan <root>` | 读取已筛选的 `events.json` → `data/<源>/tasks.json` + `compressed/`（AVIF） |
| `dry <root> [--heavy N] [--video-poll S]` | 在 `tasks.dry.json` 副本上干跑 DAG 执行器 |
| `undo <root> [--dry] [--yes] [--session F]` | **后悔药**：删除已发布的帖子（`state=done` + `post_uri`），子贴先删；删后任务重置 `pending` 可重发；`--dry` 只列出不删除 |
| `live <root> [--heavy N] [--video-poll S] [--session F]` | 真实发帖（需先 `login` 有缓存且先 `plan`） |

凭证环境变量（`login` 子命令的兜底）：`BSKY_HANDLE` / `BSKY_APP_PASSWORD`（app password）。

## 输出结构（全部在 `./data`，已被 .gitignore 忽略）

```
data/
├── session.json                  # 登录会话缓存
└── <导出目录名>/
    ├── events.json               # 通用事件流（v1, version 字段）
    ├── tasks.json                # 任务数组 + 执行器 checkpoint（断点续传）
    ├── tasks.dry.json            # dry-run 演示副本
    └── compressed/               # 超过 4000px/2MB 的图压缩产物(avif)
```

## 任务与断点续传

- `tasks.json` 顶层只有 `tasks: []`（线性数组，仅 `medias`/`alts` 为数组）
- 每个任务：`text/medias(alts)/reply_to/link_url/created_at/post_uri/post_cid/parent_uri/state/fail_reason`
- `state ∈ pending|done|skipped|failed`；执行器每完成一个任务即回写整个文件，中断后从第一个非 `pending` 继续
- 所有媒体路径为**绝对路径**；`created_at` 保留原帖发布时间

## 转换规则要点（qzone 数据源）

- `Boards`（留言板）**不转换**——是访客留言（含广告），非本人内容；真·说说在 `Messages/json/messages.json`
- 相册照片按 10 分钟窗口合并为一贴（视频不参与合并）；照片 `desc` 进每图 `alt`
- 分享：`rt` 有 URL → 正文追加 URL + `link_url` facet；无 URL → 标题/来源降级为文本
- 邮件/链接 facet 的字节偏移按 UTF-8 计算

## 扩展一个数据源

```python
# datasource/your_source/convert.py
from datasource.base import BaseDataSource


class YourSource(BaseDataSource):
    source_type = "my_source"

    def build_events(self, root):
        ...  # 返回 list[shared.event.Event]
        return events
```

再在 `datasource/__init__.py` 末尾加一行 `from datasource.your_source import YourSource; register(YourSource)`（启动时注册）。

## 项目结构

```
cli.py                  # 终端入口（子命令）
datasource/
  base.py               # BaseDataSource 抽象
  __init__.py           # 启动时注册 registry
  qzone/convert.py      # QQ空间解析
shared/
  event.py              # 通用事件模型（v1）
  planner.py            # 事件 → PDS 合规任务（截断/tweetstorm/AVIF/检查）
  executor.py           # DAG 调度执行器（dry/live，checkpoint 回写）
  auth.py               # 会话缓存登录
  paths.py              # 所有产物路径中枢（data/）
```

## 依赖

- Python ≥ 3.13，`atproto`（AsyncClient + lexicon models）
- 系统工具：`ffmpeg`/`ffprobe`（AVIF 压缩、尺寸/时长探测；本机已有则零 Python 图像依赖）
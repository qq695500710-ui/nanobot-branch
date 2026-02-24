## 2026-02-24：QQ / 飞书能力补齐（建议作为 fork 分支提交）

### 目标
- **把 QQ / 飞书 的“图片 / 文件 / 回复 / 内嵌图片”能力补齐到接近 Telegram 的可用水平**
- **移除 UI 层临时补丁**（`newui.py` 曾做“最近 3 张图片缓存 + 关键词触发自动附图”），改为在 `nanobot-main` 核心层实现
- 在不显著增加 token 成本的前提下，让“上一轮发图，下一轮问图”稳定可用

---

### 关键问题与修复点（按模块）

#### 1) 会话与 AgentLoop：让“引用上一张图”在核心层生效（替代 newui 临时缓存）
- **原因**：nanobot 的历史 `get_history()` 默认只保留文本；多模态只会把“当前消息 media”编码进 prompt。因此“先发图后发字”的第二句话天然 `images=0`。
- **修复**：
  - 会话层提供 `Session.recent_image_paths(limit)`，从 session 持久化消息中回溯最近 N 张图（只取本地存在的图片文件）。
  - AgentLoop 增加 `recent_image_limit`（默认 3，可配置为 0 关闭）。当本轮无 media 且文本明显在引用上一张图时，自动附加最近图片作为本轮多模态输入。
  - 同时把 inbound 的 `msg.media` 作为 `media=[...]` 写入 session，确保后续可追溯。
- **文件与行号**：
  - `nanobot/session/manager.py`：新增 `recent_image_paths()`（L45-L84）
  - `nanobot/agent/loop.py`：新增 `recent_image_limit`、引用意图检测、按需附图与持久化 inbound media（L46-L126、L404-L422、L441-L445）
  - `nanobot/config/schema.py`：新增 `agents.defaults.recent_image_limit`（L184-L193）
  - `nanobot/cli/commands.py`：创建 `AgentLoop(...)` 时传入 `recent_image_limit`（3 处 AgentLoop 初始化附近）

#### 2) 飞书（Feishu）：post 内嵌图片 + reply 语义 + 图文同卡片
- **问题**：
  - `post` 富文本内嵌图片（`img` tag 的 `image_key`）原实现只提取文本，导致模型看不到图。
  - 回复时仅用 `message.create`，没有“对某条消息回复”的语义（对话体验弱于 Telegram）。
- **修复**（`nanobot/channels/feishu.py`）：
  - 新增 `_extract_post_image_keys()`：从 `post` payload 递归提取 `image_key`（L232-L257）
  - 新增 `_download_image_key_to_path()`：下载指定 `image_key` 保存到 `~/.nanobot/media`（位于 `_download_and_save_media` 之后的新增方法）
  - `msg_type == "post"` 时下载内嵌图片并作为 `media` 传给 `_handle_message`（在 `_on_message` 的 post 分支附近）
  - 出站发送：
    - 当存在 `OutboundMessage.metadata.message_id` 时优先用 `im.v1.message.reply`（而不是 create）
    - 当同时有图片与文本时，优先把图片上传得到 `img_key`，并与文本一起组成 **interactive card** 发送（失败则降级为先图后文）
  - `_send_message_sync` 支持 create/reply 两种路径（reply 使用 `ReplyMessageRequest` / `ReplyMessageRequestBody`）

#### 3) QQ：入站附件下载 + 引用回复 + 媒体发送（带可配置上传器与降级提示）
- **硬约束**（来自 QQ bot API / botpy）：发送富媒体需要先 `post_c2c_file(url=公网URL)` 得到 `Media.file_info`；因此 **无法直接把本地路径发送到 QQ**。
- **修复**（`nanobot/channels/qq.py` + `nanobot/config/schema.py`）：
  - 入站：解析 `attachments[]`，下载到 `~/.nanobot/media`，作为 `media` 传入 `_handle_message`（并在文本中附带 `[attachment: xxx]` 供人类可读）。
  - 出站：支持被动回复 `msg_id + msg_seq`（`msg_seq` 对同一 msg_id 必须递增，已在内存中维护计数）。
  - 出站媒体：新增可选配置 `channels.qq.mediaUploadCommand`：用于把本地文件上传到公网并输出 URL。
    - 若媒体本身已是 `http(s)://`，直接使用该 URL。
    - 若未配置 upload command，则发送明确的降级提示（告诉用户 QQ 官方接口要求公网 URL）。
    - 对本地文件：支持图片（file_type=1）、mp4（2）、silk（3）；其它类型提示受限。
  - 配置新增：
    - `channels.qq.media_upload_command`（JSON 里是 `mediaUploadCommand`）
    - `channels.qq.media_upload_timeout_s`（JSON 里是 `mediaUploadTimeoutS`）
- **文件与行号**：
  - `nanobot/config/schema.py`：`QQConfig` 新增两字段（L159-L167）
  - `nanobot/channels/qq.py`：实现下载附件、回复、媒体发送与 upload command（文件开头至 send/_on_message 相关段落）
---

### newui 临时逻辑删除（配合本次核心修复）
- `newui.py`：移除了“最近 3 张图片缓存 + 关键词触发自动附图”的临时实现；并把 `recent_image_limit` 透传进内嵌 `AgentLoop`，让行为由 `nanobot-main` 控制。

---

### 如何从日志判断是 exe 还是 py（经验规则）
- **更像未打包 Python 运行**：栈里出现类似 `...\\.venv\\Lib\\site-packages\\...` 或直接指向你本机 Python 安装路径（例如 `Python311\\Lib\\asyncio...`）。
- **更像 Nuitka 打包 exe 运行**：栈/路径更常出现 `dist_nuitka\\...\\newui.dist\\...` 或与 `newui.exe` 同目录的运行时路径。

---

### 注意事项 / 已知限制
- QQ 发送媒体依赖“公网 URL”这一平台限制：
  - **没有 upload command** 时只能发送文本 + 明确提示；
  - 有 upload command 后才能真正把本地截图/图片发到 QQ。
- “按需附图”是启发式判断：默认避免在“打开浏览器/点击/截图”等动作任务里误附旧图；可以通过 `agents.defaults.recentImageLimit=0` 关闭该能力。


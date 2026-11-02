# bilibili-mcp

B站（Bilibili）MCP Server —— 让 AI 助手直接操作B站。

支持 OpenClaw / Claude Code / Cursor / Cline 等所有 MCP 客户端。

**27 个工具**，覆盖登录认证、搜索采集、内容发布、数据分析、互动运营。

> **这是一个打过补丁的 fork（`patched` 分支）。** 相对上游 adoresever/bilibili-mcp：
>
> - **扫码登录修复**（上游 PR #5）：改用 TV 端二维码通道，凭证校验后再保存。
> - **投稿增强**（上游 PR #6）：`bili_upload_video` 支持 `publish_time` 定时发布（本机时间 `YYYY-MM-DD HH:MM`，2 小时～15 天后）、`cover43_path` 4:3 封面、`ai_content` "含AI生成内容"声明；分区表改读 bilibili_api 自带的当前列表，并拒绝已下线的分区 ID（B 站会把未知 ID 默默放进"生活 → 日常"）。
> - **加密凭证保险箱**（`secure_store.py`）：登录 cookie 只以密文落盘（`bili_credential.enc`，AES-256-GCM，scrypt 从口令派生密钥）。口令在 macOS 原生对话框里输入，密钥只留在 MCP 进程内存里；所有工具的返回值和报错都经过 `redact()`，即使接口把 cookie 带回来也不会交给 AI。调用工具的 AI 助手能"使用"登录态，但读不到凭证本身。
> - 删除了上游误提交的 `bili_credential.json.bak`（它仍在上游历史里，如果你用过上游版本，建议退出登录让旧 cookie 失效）。
> - 依赖按哈希锁定：`python3.11 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.lock`。
>
> 推荐再加一层：在 MCP 客户端里禁止 AI 编辑 `secure_store.py`、读写 `bili_credential.enc`（Claude Code 可用 permissions 的 deny 规则）。

## 功能

### 登录认证

| Tool | 功能 | 说明 |
|------|------|------|
| `bili_login` | 扫码登录 | 生成二维码（base64图片），AI直接展示给用户扫码 |
| `bili_login_check` | 登录状态检查 | 轮询扫码状态，扫码成功后自动保存凭证 |
| `bili_check_credential` | 凭证验证 | 检查当前登录是否有效，返回用户名等信息 |

### 数据采集

| Tool | 功能 | 说明 |
|------|------|------|
| `bili_search` | 搜索视频 | 按关键词搜索，支持按播放量/最新/弹幕排序 |
| `bili_comments` | 获取评论 | 获取视频热门评论，含子评论 |
| `bili_subtitle` | 获取字幕 | 获取视频AI字幕（语音转文字） |
| `bili_danmaku` | 获取弹幕 | 获取视频弹幕列表 |
| `bili_video_info` | 视频详情 | 获取播放量、评论数、收藏数等 |
| `bili_reply` | 回复评论 | 发表评论或回复评论（支持楼中楼） |
| `bili_crawl` | 批量采集 | 搜索+评论+字幕一步到位 |

### 内容发布

| Tool | 功能 | 说明 |
|------|------|------|
| `bili_send_dynamic` | 发图文动态 | 发布文字/图文动态，支持定时发布、话题关联 |
| `bili_upload_video` | 上传视频 | 单P视频上传，支持封面、标签、分区选择 |
| `bili_upload_video_multi` | 多P上传 | 多分P视频合并投稿 |
| `bili_send_opus` | 图文专栏 | 发布图文长内容（Opus） |
| `bili_video_zones` | 分区查询 | 获取常用分区ID，辅助视频上传 |

### 数据分析

| Tool | 功能 | 说明 |
|------|------|------|
| `bili_hot_videos` | 热门视频 | 获取当前B站热门视频列表 |
| `bili_hot_buzzwords` | 热搜关键词 | 获取B站热搜词/热门话题 |
| `bili_weekly_hot` | 每周必看 | 获取每周必看推荐视频 |
| `bili_rank` | 排行榜 | 全站及17个分区排行榜 |
| `bili_user_info` | 用户信息 | 获取UP主粉丝数、等级、总播放量等 |
| `bili_user_videos` | 用户视频 | 获取UP主投稿列表，支持排序和搜索 |

### 互动运营

| Tool | 功能 | 说明 |
|------|------|------|
| `bili_favorite_lists` | 收藏夹列表 | 获取自己或他人的收藏夹 |
| `bili_favorite_content` | 收藏夹内容 | 获取收藏夹内视频，支持搜索 |
| `bili_send_message` | 发私信 | 给指定用户发送文字私信 |
| `bili_unread_messages` | 未读消息 | 获取私信、@、回复、点赞等未读数 |
| `bili_received_replies` | 收到的回复 | 获取评论回复通知 |
| `bili_received_at_and_likes` | @和点赞 | 获取@提及和点赞通知 |

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/adoresever/bilibili-mcp.git
cd bilibili-mcp
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

需要 Python 3.10+，视频上传自动截取封面需要 ffmpeg。

### 3. 登录

**方式一：通过 AI 工具登录（推荐）**

接入 MCP 后，直接对 AI 说"登录B站"，AI 会调用 `bili_login` 生成二维码展示给你扫码，全程无需终端操作。

**方式二：命令行登录**

```bash
python bili_login.py
```

### 4. 测试运行

```bash
npx @modelcontextprotocol/inspector python mcp_server.py
```

## 接入 AI 工具

### OpenClaw

```bash
npm i -g mcporter
npx mcporter config add bilibili-mcp "python /path/to/bilibili-mcp/mcp_server.py"
```

或直接把 GitHub 链接粘贴到 OpenClaw 对话框，让它自动配置。

### Claude Code

在项目目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "bilibili-mcp": {
      "command": "python",
      "args": ["/path/to/bilibili-mcp/mcp_server.py"]
    }
  }
}
```

### Cursor / Cline

在设置中添加 MCP Server，command 填 `python`，args 填 `mcp_server.py` 的完整路径。

## 使用示例

接入后，你可以直接用自然语言让 AI 操作：

- "登录B站"（AI会展示二维码给你扫）
- "搜索B站上关于AI Agent的热门视频"
- "获取这个视频的评论，分析用户需求"
- "获取视频字幕，总结视频内容"
- "帮我回复这条评论"
- "批量采集OpenClaw相关视频的评论和字幕"
- "帮我发一条B站动态，配上这几张图片"
- "把这个视频上传到B站科技区"
- "看看B站现在什么最火"
- "获取科技区排行榜前10的视频"
- "分析这个UP主的粉丝和投稿数据"
- "我有多少未读私信和回复？"
- "看看我的收藏夹里有什么"

## 技术栈

- **[bilibili-api-python](https://github.com/Nemo2011/bilibili-api)** — B站 API 封装库，提供搜索、评论、字幕、弹幕等全部接口
- **[MCP (Model Context Protocol)](https://modelcontextprotocol.io/)** — Anthropic 提出的开放协议，标准化 AI 与工具的交互
- **[FastMCP](https://github.com/modelcontextprotocol/python-sdk)** — MCP Python SDK，快速构建 MCP Server

## 项目结构

```
bilibili-mcp/
├── mcp_server.py          # MCP Server 主文件（27个tool）
├── bili_login.py           # 命令行扫码登录（备用）
├── bili_credential.json    # 登录凭证（自动生成，勿提交）
├── requirements.txt        # Python 依赖
├── README.md               # 项目说明
├── LICENSE                 # MIT 开源协议
└── .gitignore              # Git 忽略文件
```

## 注意事项

- 首次使用通过 AI 对话即可完成登录，无需终端操作
- 凭证保存在本地，不会上传
- 请求间隔自动控制，避免频率过快
- 回复评论/发私信功能请谨慎使用，遵守B站社区规则
- 视频上传未指定封面时，自动从视频第3秒截取（需要ffmpeg）
- 本项目仅用于学习和研究

## License

MIT
---

## 无人值守上传（可选）

凭证口令默认通过 macOS 对话框输入。想把上传放进定时任务（CI、每日自动投稿）时，可以把口令
存进**登录钥匙串**，MCP 进程解锁凭证时优先读钥匙串，不再弹窗：

```bash
python vault_cli.py store    # 弹一次对话框，验证能解密后存进钥匙串
python vault_cli.py check    # 查看状态
python vault_cli.py clear    # 删掉，回到手动输口令
```

- 口令只落在钥匙串（`security` 的 generic password，service 可用 `BILI_MCP_KEYCHAIN_SERVICE` 改名），
  不写文件、不进环境变量、不进日志；凭证本体仍是 AES-256-GCM 密文，AI 助手依然读不到。
- 非 macOS 上自动跳过钥匙串，行为与之前完全一致。

### 慢链路 / 海外线路的上传

海外网络到 B 站 `upos` CDN 的单连接上行可能只有几十 KB/s，一块 10 MB 数据在默认 30 秒超时内写不完，
于是每块判失败并无限重传——调用方看到的只是"一直卡着"。上传路径现在是：

| 行为 | 默认 | 覆盖 |
|---|---|---|
| 上传期间 HTTP 超时 | 1800 秒 | `BILI_MCP_UPLOAD_TIMEOUT` |
| 分块并发（服务端默认 4） | 12 | `BILI_MCP_UPLOAD_THREADS` |
| 停滞看门狗（无进度即取消并回报最后阶段） | 900 秒 | `BILI_MCP_UPLOAD_STALL` |

进度事件写到 stderr（如 `CHUNK_FAILED {"offset": 10485760, ...}`），失败时返回值里带 `last_phase`，
不会再出现"调用挂住、不知道停在哪一步"。

### 核对投稿是否已提交

```text
bili_upload_manager_info(ps=10, status="is_pubing")
```

列出内容管理里的稿件（含定时发布、审核中、未过审）——公开视频接口看不到这些，定时投稿后用它确认。

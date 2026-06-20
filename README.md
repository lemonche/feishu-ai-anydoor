# Feishu AI Bridge

> 飞书消息驱动多 AI CLI 后端的桥接服务 —— 统一调度各家大模型 CLI，按需切换，结果回传飞书。

一个把飞书群聊作为统一入口、聚合调度多家 AI CLI 后端的服务。在飞书发一条消息，服务捕获后路由到指定的 AI CLI（Trae / Qwen / DeepSeek / 豆包 / Kimi 等）执行，结果以飞书富文本回传。

核心解决的问题：各家大厂都在推自家 AI CLI 并提供免费额度，但额度分散、切换割裂。本服务通过统一抽象层将它们聚合到一个飞书入口，运行时动态切换后端与模型，按需使用各家额度。

---

## 设计思路

### 为什么聚合多家 AI CLI

当前各家大厂相继推出 AI 编程 CLI 并附带免费额度：字节 Trae CLI、阿里 Qwen Code、DeepSeek、豆包、Kimi 等。每家各有侧重——有的额度充裕、有的擅长长上下文、有的推理能力强。

但实际使用中存在几个问题：

- 各家 CLI 命令不同，切换需记忆多套操作
- 额度分散在不同账号，难以统一调度
- 单一后端额度耗尽后切换成本高

本服务的做法是引入**统一后端抽象层**，将各家 CLI 封装为可插拔后端，通过飞书消息统一调度，运行时动态切换：

- `/backend qwen` —— 切换后端，零重启
- `/profile deepseek` —— 切换模型配置，按需使用各家额度
- 多 Session 并行 —— 不同任务可分配不同后端，互不干扰

### 工作流程

```
飞书消息 → 事件订阅(NDJSON) → 落盘事件文件 → 消费者路由
       → TaskQueue 排队 → 统一抽象层调度 AI CLI → Markdown 回传飞书
```

---

## 架构

```
┌─────────────┐     NDJSON 流      ┌──────────────────┐
│  飞书云服务  │ ──────────────────▶ │  lark-cli event  │
└─────────────┘                     │   (事件订阅)      │
       ▲                            └────────┬─────────┘
       │ post/text                           │ 写入事件文件
       │ Markdown                            ▼
┌──────┴──────┐                     ┌──────────────────┐
│  飞书消息    │ ◀──── 回传 ───────── │  event_consumer  │
│  (富文本)    │                     │  (事件消费/路由)  │
└─────────────┘                     └────────┬─────────┘
                                             │ 入队
                                             ▼
                                    ┌──────────────────┐
                                    │   TaskQueue × N  │
                                    │  (多Session并行)  │
                                    └────────┬─────────┘
                                             │ 统一后端抽象层
                                             ▼
              ┌──────────┬──────────┬────────┴────────┬──────────┐
              ▼          ▼          ▼                 ▼          ▼
        ┌──────────┐┌──────────┐┌──────────┐  ┌──────────┐┌──────────┐
        │ Trae CLI ││Qwen Code ││ DeepSeek │  │  豆包    ││   Kimi   │
        └──────────┘└──────────┘└──────────┘  └──────────┘└──────────┘
```

所有 AI CLI 通过统一抽象层接入，新增一家只需实现该接口，无需改动上层调度逻辑。

---

## 核心特性

### 多后端聚合与动态切换
- **统一抽象层**：各家 AI CLI 封装为可插拔后端，新增后端只需实现接口
- **运行时切换**：`/backend` 命令切换后端，`/profile` 切换模型配置，均无需重启
- **多 Session 并行**：每个 Session 独立 TaskQueue，上下文隔离，可分配不同后端

### 飞书事件订阅
- 基于 `lark-cli event consume` 的 NDJSON 流式消费
- 事件落盘后异步处理，消费者崩溃不丢消息
- 支持 `text` 与 `post`（富文本）两种消息类型

### Markdown 富文本回传
- AI 回复以飞书 `post` 类型发送，表格 / 标题 / 代码块 / 列表正确渲染
- 双通道发送：`send_feishu_markdown()` 走富文本，`send_reply()` 走纯文本

### 智能消息提示
根据消息特征区分响应策略，避免简单对话也刷"思考中"：

| 场景 | 判断条件 | 行为 |
|------|---------|------|
| 闲聊快速回复 | 非指令 + 耗时 <8s + 无步骤 | 直达结果，无提示 |
| 指令任务 | 以 `/` 开头或含指令关键词 | "思考中" + 进度推送 + 元信息 |
| 长任务 | 耗时 ≥8s 或多步骤 | 分步进度 + 心跳保活 |

### 进程管理
面向长时间运行的健壮性设计：

- **PID 文件锁**：防止多实例，避免事件重复消费
- **孤儿进程清理**：主进程退出时清理残留子进程
- **信号优雅退出**：`SIGTERM` / `SIGINT` 触发资源清理并通知飞书下线
- **健康检查**：定时探测事件流是否僵死，异常自动重建
- **启动竞态防护**：`pgrep` 检测并清理残留主进程

### 配置档案
`profiles/` 目录管理不同模型配置，运行时切换：

```yaml
# profiles/reviewer.yaml
model: "DeepSeek-V4-Pro"
description: "代码审查 - 只读权限，不允许修改文件"
config_overrides:
  - "disallowed-tool=Write"
  - "disallowed-tool=Edit"
  - "disallowed-tool=Replace"
system_prompt: |
  你是一位资深代码审查专家...
```

---

## 项目结构

```
feishu-ai-bridge/
├── main.py                      # 入口：PID锁、信号处理、主事件循环
├── restart_service.py           # 服务重启脚本（清理旧进程）
├── settings.yaml                # 主配置（飞书、后端、桥接参数）
├── com.feishu-trae-bridge.plist # macOS launchd 自启动配置
├── profiles/                    # AI 模型配置档案
│   ├── default.yaml
│   ├── deepseek.yaml
│   ├── doubao.yaml
│   ├── kimi.yaml
│   └── reviewer.yaml
└── feishu_ai_bridge/            # 核心包
    ├── __init__.py
    ├── config.py                # 配置加载、AppContext 集中状态
    ├── feishu.py                # 飞书消息发送（text/markdown 双通道）
    ├── event_consumer.py        # 事件订阅、消息处理、进程生命周期管理
    ├── session.py               # Session/SessionPool 会话池
    ├── queue.py                 # TaskQueue 异步任务队列
    ├── commands.py              # 内置命令处理（/help /status 等）
    ├── backend.py               # AI 后端调用抽象层
    ├── traecli.py               # Trae CLI 流式调用封装
    ├── qwencli.py               # Qwen Code 调用封装
    └── i18n.py                  # 界面文案国际化
```

---

## 内置命令

在飞书群聊中直接发送：

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助信息 |
| `/status` | 查看所有 Session 运行状态 |
| `/reset` | 重置当前 Session 上下文 |
| `/restart` | 重启整个服务 |
| `/stop` | 强制停止当前 Session 的任务 |
| `/backend` | 查看 / 切换 AI 后端 |
| `/profile` | 查看 / 切换配置档案 |
| `/session` | 查看 / 创建 / 切换 / 终止 Session |

```
/backend qwen                  → 切换到 Qwen Code 后端
/profile deepseek              → 切换到 DeepSeek 模型
/session create review         → 创建名为 review 的新会话

帮我写一个 Python 快速排序       → 指令模式，AI 执行并回传
你好                            → 闲聊模式，直达回复
```

---

## 快速开始

### 环境依赖

- Python 3.10+
- [lark-cli](https://github.com/larksuite/cli)（飞书 CLI）
- Trae CLI 或 Qwen Code（AI 后端，至少一个）
- PyYAML（`pip3 install pyyaml`）

### 配置

编辑 `settings.yaml`：

```yaml
feishu:
  chat_id: oc_your_chat_id_here       # 飞书群聊 ID
  lark_cli: /path/to/lark-cli         # lark-cli 可执行文件路径
  my_open_id: ou_your_open_id_here    # 你的飞书 open_id

active_backend: trae                   # 默认后端：trae / qwen

backends:
  trae:
    path: ~/.local/bin/traecli
    session_id: feishu-bridge-session-001
    timeout: 300
    yolo: true
  qwen:
    path: qwen
    session_id: feishu-bridge-qwen-001
    timeout: 300
    yolo: false

bridge:
  poll_interval: 0.5
  status_update_interval: 10
  command_prefix: /
  max_queue_size: 10
  health_check_interval: 60

profiles_dir: profiles
active_profile: default
```

### 运行

```bash
# 前台运行
python3 main.py

# macOS 开机自启
cp com.feishu-trae-bridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.feishu-trae-bridge.plist
```

启动后飞书群会收到上线通知，即可开始使用。

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 语言 | Python 3.10+ |
| 飞书集成 | lark-cli（NDJSON 事件流 + 消息发送） |
| AI 后端 | 多家 AI CLI 聚合（统一抽象层接入） |
| 配置 | PyYAML |
| 进程管理 | PID 锁、signal、subprocess |
| 自启动 | macOS launchd |
| 并发模型 | 多线程 TaskQueue（生产者-消费者） |

---

## License

MIT

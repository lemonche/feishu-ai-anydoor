# Feishu AI Bridge 🚀

> 把飞书变成你的 AI 超级终端 —— 一条消息，调度任意 AI CLI 后端，结果秒回飞书。

<div align="center">

**飞书即时消息 × AI 编程助手 × 异步任务队列 × 多模型路由**

一个让飞书群聊直接驱动 AI 编程的桥接服务。发条消息，AI 就在后台帮你写代码、查资料、做审查，结果以飞书富文本优雅回传。

</div>

---

## 🎯 这是什么

`Feishu AI Bridge` 是一个**生产级**的飞书 ↔ AI CLI 桥接服务。它把飞书群聊变成一个 AI 指令终端：

- 你在飞书里发一条消息（比如"帮我写个快速排序"）
- 服务通过飞书事件订阅实时捕获消息
- 路由到对应的 AI 后端（Trae CLI / Qwen Code）执行
- 执行结果以飞书 Markdown 富文本回传，表格、代码块、标题完美渲染

**一句话总结：飞书即终端，消息即指令，AI 即后端。**

---

## ✨ 核心特性

### 🏗️ 工程架构
- **飞书事件订阅**：基于 `lark-cli event consume` 的 NDJSON 流式事件消费，毫秒级响应
- **多后端支持**：Trae CLI、Qwen Code 双后端，运行时动态切换，零重启
- **多 Session 并行**：每个 Session 独立 TaskQueue，上下文隔离，互不干扰
- **异步任务队列**：生产者-消费者模型，任务排队、超时控制、异常兜底全链路覆盖

### 🎨 用户体验
- **Markdown 富文本渲染**：AI 回复以飞书 `post` 类型发送，表格 / 标题 / 代码块 / 列表正确渲染，告别纯文本糊一片
- **智能消息提示策略**：
  - 闲聊快速回复（非指令 + <8s + 无步骤）→ 跳过"思考中"提示，直达结果
  - 长任务指令 → 显示"思考中" + 分步进度推送 + 完成元信息
- **心跳保活**：长任务自动推送心跳，让用户知道"AI 还在干活"
- **排队感知**：任务排队时主动告知用户前方等待人数

### 🛡️ 进程管理（生产级健壮性）
- **PID 文件锁**：防止多实例同时运行，避免事件重复消费
- **孤儿进程自动清理**：主进程退出时自动 kill 残留的 event consumer 子进程
- **信号优雅退出**：`SIGTERM` / `SIGINT` 触发资源清理，飞书通知服务下线
- **健康检查**：定时探测事件流是否僵死，异常自动重建
- **启动竞态防护**：`pgrep` 检测残留主进程并清理，杜绝僵尸进程

### ⚙️ 配置与扩展
- **配置档案系统**：`profiles/` 目录管理不同 AI 模型配置，一键切换 DeepSeek / 豆包 / Kimi
- **i18n 国际化**：界面文案集中管理，易于多语言适配
- **macOS launchd 自启动**：开机即运行，崩溃自动拉起

---

## 🏛️ 架构设计

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
                                             │ 调度
                              ┌──────────────┼──────────────┐
                              ▼                             ▼
                     ┌────────────────┐           ┌────────────────┐
                     │   Trae CLI     │           │   Qwen Code    │
                     │  (流式调用)     │           │   (AI 后端)    │
                     └────────────────┘           └────────────────┘
```

**数据流**：飞书消息 → 事件订阅 → 落盘事件文件 → 消费者路由 → TaskQueue 排队 → 后端执行 → Markdown 回传飞书

---

## 📁 项目结构

```
feishu-ai-bridge/
├── main.py                      # 入口：PID锁、信号处理、主事件循环
├── restart_service.py           # 服务重启脚本（清理旧进程）
├── settings.yaml                # 主配置（飞书、后端、桥接参数）
├── com.feishu-trae-bridge.plist # macOS launchd 自启动配置
├── profiles/                    # AI 模型配置档案
│   ├── default.yaml             # 默认配置
│   ├── deepseek.yaml            # DeepSeek 深度推理
│   ├── doubao.yaml              # 豆包代码模型
│   ├── kimi.yaml                # Kimi 长上下文
│   └── reviewer.yaml            # 代码审查专用（只读）
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

## 🎮 内置命令

在飞书群聊中直接发送即可触发：

| 命令 | 说明 |
|------|------|
| `/help` | 显示完整帮助信息 |
| `/status` | 查看所有 Session 运行状态 |
| `/reset` | 重置当前 Session 上下文 |
| `/restart` | 重启整个桥接服务 |
| `/stop` | 强制停止当前 Session 的任务 |
| `/backend` | 查看 / 切换 AI 后端（Trae / Qwen） |
| `/profile` | 查看 / 切换配置档案（模型） |
| `/session` | 查看 / 创建 / 切换 / 终止 Session |

### 使用示例

```
/help                          → 查看所有命令
/backend qwen                  → 切换到 Qwen Code 后端
/profile deepseek              → 切换到 DeepSeek 模型
/session create review         → 创建一个名为 review 的新会话
/session switch review         → 切换到 review 会话

帮我写一个 Python 快速排序       → 指令模式，AI 执行并回传代码
你好                            → 闲聊模式，秒回，无多余提示
审查一下 src/utils.py 的代码     → 调用 reviewer 档案做代码审查
```

---

## 🚀 快速开始

### 环境依赖

- **Python 3.10+**
- **[lark-cli](https://github.com/larksuite/cli)**（飞书官方 CLI，用于事件订阅与消息发送）
- **Trae CLI** 或 **Qwen Code**（AI 后端，至少一个）
- **PyYAML**（`pip3 install pyyaml`）

### 1️⃣ 配置

编辑 `settings.yaml`，填入你的飞书信息：

```yaml
feishu:
  chat_id: oc_your_chat_id_here       # 飞书群聊 ID
  lark_cli: /path/to/lark-cli         # lark-cli 可执行文件路径
  my_open_id: ou_your_open_id_here    # 你自己的飞书 open_id

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

### 2️⃣ 运行

**前台运行**（调试用）：

```bash
python3 main.py
```

**macOS 开机自启**（生产用）：

```bash
# 修改 plist 中的路径为你的实际路径
cp com.feishu-trae-bridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.feishu-trae-bridge.plist
```

启动后，飞书群会收到 `🟢 桥接服务已上线` 的通知，即可开始使用。

---

## 🔧 技术亮点

### 智能消息提示策略

不是所有消息都需要"思考中"提示。服务会根据消息特征智能判断：

| 场景 | 判断条件 | 行为 |
|------|---------|------|
| 闲聊快速回复 | 非指令 + 耗时 <8s + 无步骤 | 跳过"思考中"，直达结果，无元信息头部 |
| 指令任务 | 以 `/` 开头或含指令关键词 | 显示"思考中" + 进度推送 + 完整元信息 |
| 长任务 | 耗时 ≥8s 或有多步骤 | 分步进度推送 + 心跳保活 |

这让飞书对话体验接近真人：简单问题秒回，复杂任务有进度反馈。

### 双通道消息发送

- **`send_feishu_markdown()`**：通过 `--markdown` 发送 `post` 类型富文本，让表格 / 代码块 / 标题在飞书正确渲染
- **`send_reply()` / `send_feishu()`**：通过 `--text` 发送纯文本，用于简单提示

所有 AI 回复、命令回复、进度推送统一走 Markdown 通道，视觉体验一致且专业。

### 生产级进程管理

这套进程管理机制经过实战打磨，解决了多个真实痛点：

- **PID 锁 + pgrep 双重检测**：杜绝多实例竞态
- **孤儿进程清理**：主进程崩溃后不留僵尸子进程
- **事件流僵死检测**：健康检查发现无心跳自动重建
- **优雅退出**：信号触发清理 + 飞书下线通知，不留尾巴

---

## 📦 配置档案系统

通过 `profiles/` 目录管理不同 AI 模型的配置，运行时一键切换：

```yaml
# profiles/reviewer.yaml —— 代码审查专用档案
model: "DeepSeek-V4-Pro"
description: "代码审查 - 只读权限，不允许修改文件"
config_overrides:
  - "disallowed-tool=Write"
  - "disallowed-tool=Edit"
  - "disallowed-tool=Replace"
system_prompt: |
  你是一位资深代码审查专家...
```

切换时只需在飞书发送 `/profile reviewer`，无需重启服务。

---

## 🛠️ 技术栈

| 层级 | 技术 |
|------|------|
| 语言 | Python 3.10+ |
| 飞书集成 | lark-cli（事件订阅 NDJSON 流 + 消息发送） |
| AI 后端 | Trae CLI、Qwen Code |
| 配置 | PyYAML |
| 进程管理 | PID 锁、signal 信号、subprocess |
| 自启动 | macOS launchd |
| 并发模型 | 多线程 TaskQueue（生产者-消费者） |

---

## 📝 License

MIT License - 自由使用、修改、分发。

---

<div align="center">

**如果这个项目对你有帮助，欢迎 Star ⭐ 支持！**

把飞书变成 AI 超级终端，从一条消息开始 🚀

</div>

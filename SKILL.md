---
name: agent-mind-migrate
description: |
  多 Agent 统一备份/迁移工具。一键备份 Claude Code、OpenClaw、Hermes 等 AI Agent 的全部配置和积累到 Git 仓库，支持跨机器一键还原。自动发现已安装的 Agent，按 Agent 独立存储。
  触发词：备份、迁移、backup、restore、migrate、搬家、换机器、一键迁移、导出配置、导入配置、备份我的配置、帮我打包、迁移到新机器、备份你的记忆技能、保存你的记忆、备份记忆、agent备份、多agent迁移。
  即使用户只是说"我要换机器了"或"帮我把这些东西保存下来"或"备份一下"，只要上下文暗示需要保存/迁移 AI Agent 的状态，都应该触发。
  不要用于普通的文件备份或 git 操作。
metadata:
  category: custom
  author: AlphaWill
  version: "4.1"
---

# Agent Mind Migrate v4.1

**脚本**：`~/.claude/skills/agent-mind-migrate/scripts/migrate.py`（下文简写 `migrate.py`）

---

## 意图识别 → 执行流程

收到用户消息后，先判断意图，然后走对应流程。**不要跳步，不要合并步骤。**

| 用户意图 | 关键词 | 跳转 |
|---------|--------|------|
| 日常备份 | 备份、backup、保存配置 | → [Flow A: 备份](#flow-a-备份) |
| 换机器/迁移 | 换机器、迁移、migrate、搬家 | → [Flow B: 迁移](#flow-b-迁移) |
| 还原 | 还原、restore、导入 | → [Flow C: 还原](#flow-c-还原) |
| 查状态 | 状态、status | → [Flow D: 状态/验证](#flow-d-状态验证) |
| 只还原部分 | 只还原 skills/memory/... | → [Flow C](#flow-c-还原)（带 --only） |

---

## Flow A: 备份

```
Step 1  运行 migrate.py status
        → 展示给用户：检测到哪些 Agent、上次备份时间、文件数
        → 如果 0 个 Agent 被检测到：告知用户无可备份内容，结束

Step 2  问用户：
        - 确认要备份吗？
        - 日常备份 or 完整备份（换机器）？
        → 日常 = --tier essential（默认）
        → 完整 = --tier full
        → 用户取消 → 结束，不执行任何操作

Step 3  执行：
        python3 migrate.py backup [--tier full]
        → 失败 → 展示错误输出，建议用户检查磁盘空间或 Agent 进程锁

Step 4  展示结果：备份了几个 Agent、几个文件

Step 5  问用户：要推送到远程仓库吗？
        ⚠️ --push 是网络外发操作，不要自动执行
        - 用户说是 → python3 migrate.py backup --push
          → push 失败 → 告知"本地备份已保存，网络恢复后可重试 backup --push"
        - 用户没说 / 说不 → 跳过，告知"本地备份已完成，需要时再 push"
        - 未 init 过 → 提示先执行 migrate.py init --remote <url>
```

---

## Flow B: 迁移

迁移 = 旧机器完整备份 + 新机器还原。区分用户在哪台机器上。

### 在旧机器上

```
Step 1  执行 Flow A（完整备份），自动用 --tier full
Step 2  确保 --push 成功（迁移场景必须推送）
        → push 失败 → 不要继续给新机器命令，先解决 push 问题
        → 未 init → 先引导 init，再重试 push
Step 3  告诉用户：在新机器上执行以下命令——

        git clone <backup-repo-url> ~/.claude-backup
        git clone https://github.com/AlphaWill0/agent-mind-migrate.git ~/.claude/skills/agent-mind-migrate
        python3 ~/.claude/skills/agent-mind-migrate/scripts/migrate.py restore --dry-run
        python3 ~/.claude/skills/agent-mind-migrate/scripts/migrate.py restore
        python3 ~/.claude/skills/agent-mind-migrate/scripts/migrate.py validate
```

### 在新机器上

→ 走 [Flow C: 还原](#flow-c-还原)

---

## Flow C: 还原

```
Step 1  检查 ~/.claude-backup/ 是否存在
        - 不存在 → 提示用户先 git clone 备份仓库，给出命令模板，结束
        - 存在 → 继续

Step 2  执行 dry-run 预览：
        python3 migrate.py restore --dry-run [--agents X] [--only Y Z]
        → 展示将还原的文件列表给用户
        → 如果 0 个文件要还原：告知用户备份仓库可能是空的或参数有误，结束
        → 如果出现 SHA-256 校验警告：告知用户备份可能损坏，建议重新 git clone

        参数拼接规则：
        - 用户指定了 Agent → 加 --agents claude-code,openclaw
        - 用户指定了模块 → 加 --only skills memory config
        - 两者可组合

Step 3  ⛔ 等用户确认 dry-run 输出后，再执行实际还原：
        python3 migrate.py restore --yes [--agents X] [--only Y Z]
        （加 --yes 因为 SKILL 流程已有人工确认，无需脚本再确认一次）
        → 用户取消 → 结束，不执行
        → 还原失败 → 展示错误，已还原的文件不受影响（原子写入）

        默认 --conflict backup-existing（先备份旧文件再覆盖）
        用户要求直接覆盖 → --conflict overwrite
        用户要求跳过已有 → --conflict skip

Step 4  执行验证：
        python3 migrate.py validate
        → 有 __REDACTED__ 字段 → 列出具体文件和字段名，提醒用户手动填入真实 API 密钥
        → 无问题 → 告知"还原完成，验证通过"
```

### 可用还原模块

`config` · `memory` · `skills` · `rules` · `agents` · `commands` · `scheduled_tasks` · `stats` · `project_memories` · `plans` · `history` · `plugins`

---

## Flow D: 状态/验证

```
python3 migrate.py status      # 每个 Agent 的备份时间、文件数、完整性
python3 migrate.py validate    # 本机环境健康检查
```

展示输出给用户，不需要额外操作。

---

## 初始化（首次使用）

用户还没设置过远程仓库时，任何 --push 操作都会失败。引导：

```
python3 migrate.py init --remote <git-url> [--git-user "名字"] [--git-email "邮箱"]
```

---

## 关键规则

1. **--push 是网络操作**：永远不自动执行，先告知用户再做
2. **restore 必须先 dry-run**：不要跳过预览直接还原
3. **展示再确认**：每个 Flow 的关键操作前，先展示信息，等用户确认
4. **--yes 仅在 SKILL 流程中使用**：因为人工确认已在对话中完成，脚本层不需要重复确认
5. **自动脱敏**：备份时 token/密码/API key → `__REDACTED__`，用户不需要手动处理
6. **智能合并**：还原时 `__REDACTED__` 保留本机已有真实值，不会覆盖

---

<details>
<summary><b>支持的 Agent 与备份内容</b></summary>

| Agent | 配置目录 | 备份内容 | 脱敏 |
|-------|---------|---------|------|
| **Claude Code** | `~/.claude/` | 主配置、settings、memory、skills、rules、agents、commands、定时任务、统计、项目记忆 | token/密码 → `__REDACTED__`；MCP 配置中的 env 和 --token 参数 |
| **OpenClaw** | `~/.openclaw/` | 主配置、bot配置、记忆(sqlite)、定时任务、插件、设备 | auth 字段 → `__REDACTED__` |
| **Hermes** | `~/.hermes/` | 配置、身份(SOUL.md)、记忆、技能、定时任务 | config.yaml 中的敏感值；.env/auth.json 直接排除 |

### 备份层级

| 层级 | 包含内容 | 何时用 |
|------|---------|--------|
| `essential`（默认） | 配置 + 记忆 + 技能 + 规则 + agents + commands + 定时任务 + 统计 | 日常备份 |
| `full` | 上述 + 命令历史 + 规划方案 + 插件 | 换机器 |

</details>

<details>
<summary><b>错误处理</b></summary>

| 场景 | 处理 |
|------|------|
| Agent 未安装 | 自动跳过，status 中标注「未检测到」 |
| 未 init 就 --push | 提示先执行 init --remote |
| 网络断开 push 失败 | 本地备份不受影响，网络恢复后重试 |
| restore SHA-256 校验失败 | 默认中止，提示重新 clone；--force 可跳过 |
| 旧格式备份（v3.x） | 自动识别为 Claude Code |
| __REDACTED__ 占位符 | 智能合并保留本机已有值；新机器需手动补 |
| 备份仓库不存在 | 提示 git clone 或 init |
| SQLite 被锁 | 提示关闭 OpenClaw 后重试 |

</details>

<details>
<summary><b>命令参考</b></summary>

```
migrate.py init --remote <url>           # 初始化远程仓库
migrate.py backup [--push]               # 备份（默认 essential 层级）
migrate.py backup --tier full --push     # 完整备份 + 推送
migrate.py backup --agents claude-code   # 只备份指定 Agent
migrate.py restore --dry-run             # 预览还原
migrate.py restore --yes                 # 执行还原（跳过脚本确认）
migrate.py restore --only skills memory  # 只还原指定模块
migrate.py restore --agents openclaw     # 只还原指定 Agent
migrate.py restore --no-pull             # 不自动拉取远程更新
migrate.py status                        # 备份状态
migrate.py validate                      # 健康检查
```

</details>

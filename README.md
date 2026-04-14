# claude-migrate

Claude Code 一键备份/迁移 Skill（v3.4）。将 Claude Code 的全部积累备份到 Git 仓库，支持跨机器一键还原。跨平台支持 Windows / macOS / Linux。

## 解决什么问题

Claude Code 的使用积累分散在多个位置：skills、memory、settings、rules、agents、commands、定时任务等。换机器或重装后，这些积累全部丢失。

`claude-migrate` 把它们打包成一个 Git 仓库，一键备份、一键还原。

## 安装

```bash
# 方式 1：直接 clone 到 skills 目录
git clone https://github.com/weiwancheng/ClaudeBackupSkill.git ~/.claude/skills/claude-migrate

# 方式 2：已有 Claude Code 的话，安装后说"备份一下"即可触发
```

## 快速开始

### 首次配置

```bash
# 配置远程仓库（用你自己的 GitHub 仓库 URL）
python3 ~/.claude/skills/claude-migrate/scripts/migrate.py init --remote <your-git-url>
```

### 日常备份

在 Claude Code 对话中说：

```
备份一下
```

或手动执行：

```bash
python3 ~/.claude/skills/claude-migrate/scripts/migrate.py backup --push
```

### 迁移到新机器

```bash
# 1. 新机器上 clone 备份仓库
git clone <your-backup-repo-url> ~/.claude-backup

# 2. 预览还原
python3 ~/.claude-backup/skills/claude-migrate/scripts/migrate.py restore --dry-run

# 3. 执行还原
python3 ~/.claude-backup/skills/claude-migrate/scripts/migrate.py restore --conflict backup-existing

# 4. 健康检查
python3 ~/.claude/skills/claude-migrate/scripts/migrate.py validate
```

## 备份范围

| 内容 | essential | full |
|------|:---------:|:----:|
| `.claude.json`（项目授权、MCP 服务器） | Y | Y |
| `settings.json`（环境变量、权限、hooks） | Y | Y |
| 全局 CLAUDE.md（memory） | Y | Y |
| Skills（所有已安装技能） | Y | Y |
| Rules / Agents / Commands | Y | Y |
| 定时任务、使用统计 | Y | Y |
| 项目级 Memory | Y | Y |
| 命令历史 | - | Y |
| 规划方案（plans/） | - | Y |
| Plugins | - | Y |

## 五个命令

| 命令 | 用途 |
|------|------|
| `init --remote <url>` | 初始化备份仓库并配置远程 |
| `backup [--push]` | 备份当前配置（`--tier full` 完整备份） |
| `restore --dry-run` | 预览还原操作 |
| `restore --conflict backup-existing` | 执行还原（备份已有文件） |
| `status` | 查看备份状态和差异 |
| `validate` | 健康检查 |

## 安全机制

- **自动脱敏**：API key、token 等敏感值替换为 `__REDACTED__`，不进 Git
- **智能合并还原**：检测到 `__REDACTED__` 时保留本机实际值，不覆盖
- **原子备份**：staging 目录 + 三步 rename，中途失败自动回滚
- **SHA-256 校验**：每个文件的哈希记录在 manifest.json 中
- **路径穿越防护**：还原目标必须在 `$HOME` 下
- **flock 并发锁**：防止多实例同时运行

## 依赖

- Python 3.8+（仅标准库，零外部依赖）
- Git CLI
- 支持平台：Windows / macOS / Linux

## 版本历史

| 版本 | 变更 |
|------|------|
| v3.4 | 跨平台兼容（Windows/macOS/Linux）：条件导入文件锁、symlink 安全回退、权限处理跨平台、路径还原跨平台 |
| v3.3 | skill 备份智能过滤（跳过无 SKILL.md 的目录）、description 精简 |
| v3.2 | .claude.json 白名单过滤、BASE_URL 不再脱敏、原子交换回滚修复、跨用户 HOME 路径转换、git clone 锁定 commit、projects 深度合并 |
| v3.1 | plans/ 备份、项目发现增强、stats 独立模块、完整性校验退出 |
| v3.0 | 原子备份、SHA-256 校验、智能合并、文件权限保留、选择性恢复 |

## License

MIT

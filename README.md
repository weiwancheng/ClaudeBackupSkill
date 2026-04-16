<div align="center">

# 🧠 agent-mind-migrate

**Migrate your AI agent's mind to a new machine — skills, memory, config, everything.**

One command to backup. One command to restore. Zero dependencies.

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)]()

</div>

---

## 💡 The Problem

You've spent weeks teaching your AI agent. Custom skills, carefully tuned settings, accumulated memory, rules, commands, cron jobs. Then you switch machines — and it's all gone. Starting from scratch.

**agent-mind-migrate** solves this. It backs up your AI agent's entire "mind" — everything that makes it *yours* — into a Git repo. Switch machines, restore in seconds.

## ⚡ Quick Start

**Just say this to Claude Code:**

```
帮我安装 agent-mind-migrate
```

Claude will clone the repo, set up the skill, and connect your backup repo — all automatically.

Or install manually:

```bash
git clone https://github.com/AlphaWill0/agent-mind-migrate.git ~/.claude/skills/agent-mind-migrate
python3 ~/.claude/skills/agent-mind-migrate/scripts/migrate.py init --remote <your-repo-url>
```

Then back up anytime by saying:

```
备份一下
```

Claude detects your agents, shows what it found, and backs everything up. **You don't need to remember any commands.**

## New Machine? 4 Steps.

```bash
# On the new machine:
git clone <your-backup-repo-url> ~/.claude-backup
git clone https://github.com/AlphaWill0/agent-mind-migrate.git ~/.claude/skills/agent-mind-migrate
python3 ~/.claude/skills/agent-mind-migrate/scripts/migrate.py restore --dry-run    # preview first
python3 ~/.claude/skills/agent-mind-migrate/scripts/migrate.py restore --conflict backup-existing
```

Done. Your agent remembers everything.

---

## What Gets Backed Up

### Claude Code (`~/.claude/`)

| Content | Essential | Full |
|---------|:---------:|:----:|
| Config + settings | ✅ | ✅ |
| Global memory (`CLAUDE.md`) | ✅ | ✅ |
| All skills (local = full copy, git = URL + SHA) | ✅ | ✅ |
| Rules, agents, commands | ✅ | ✅ |
| Scheduled tasks, usage stats | ✅ | ✅ |
| Project-level memory | ✅ | ✅ |
| Command history, plans, plugins | — | ✅ |

### OpenClaw (`~/.openclaw/`)

| Content | Redaction |
|---------|-----------|
| Config, bot settings | `auth` fields → `__REDACTED__` |
| Memory (SQLite), cron jobs | — |
| Extensions (skip `node_modules`) | — |
| Devices | — |

> `credentials/`, `logs/`, `tasks/`, `*.bak*` excluded.

### Hermes (`~/.hermes/`)

| Content | Notes |
|---------|-------|
| Config (`config.yaml`) | — |
| Identity (`SOUL.md`) | — |
| Memories, skills, cron | — |

> `.env`, `auth.json`, `logs/`, `sessions/`, `browser_recordings/` excluded.

---

## Commands

```
migrate.py init --remote <url>           # First-time setup
migrate.py backup [--push]               # Backup all detected agents
migrate.py backup --tier full --push     # Full backup (recommended for migration)
migrate.py backup --agents claude-code   # Backup specific agent only
migrate.py restore --dry-run             # Preview restore (always do this first!)
migrate.py restore --conflict backup-existing  # Restore with safety backup
migrate.py restore --only skills memory  # Restore specific modules only
migrate.py restore --agents openclaw     # Restore one agent only
migrate.py status                        # Backup status per agent
migrate.py validate                      # Health check
```

### Backup Tiers

| Tier | What's included | When to use |
|------|----------------|-------------|
| `essential` (default) | Config, memory, skills, rules, commands, cron, stats | Daily backup |
| `full` | Above + command history, plans, plugins | Switching machines |

### Restore Modules

`config` · `memory` · `skills` · `rules` · `agents` · `commands` · `scheduled_tasks` · `stats` · `project_memories` · `plans` · `history` · `plugins`

Mix and match with `--only`: `restore --only skills memory --conflict backup-existing`

---

## Security

Your secrets never touch Git:

- **Auto-redaction** — API keys, tokens, passwords → `__REDACTED__` before every commit
- **Smart merge** — On restore, `__REDACTED__` fields keep your machine's real values
- **Path traversal protection** — Restore targets must be under `$HOME`
- **File lock** — Prevents concurrent backup/restore
- **Credentials excluded** — Sensitive directories are never copied

---

## FAQ

**Why not just `cp -r ~/.claude ~/.claude-backup`?**
> You'd copy your API keys into a git repo. You'd miss OpenClaw and Hermes. You'd have no integrity checks, no selective restore, no smart merge. And you'd have to remember to do it.

**Why not use dotfiles managers (chezmoi, yadm)?**
> They're great for shell config. But AI agent state is messier — SQLite databases, skills that are git repos themselves, project-level memory scattered across directories. This tool understands the structure of each agent and handles the edge cases.

**What if I only use Claude Code?**
> That's fine. The tool auto-detects what's installed. If you only have Claude Code, it only backs up Claude Code.

**Is my data safe?**
> Secrets are redacted before commit. SHA-256 hashes verify every file on restore. Atomic writes mean a crash mid-backup won't corrupt anything.

---

## Add Your Own Agent

The codebase uses a plugin architecture. Each agent is a class:

```python
class YourAgentPlugin(AgentPlugin):
    name = "your-agent"
    config_dir = Path.home() / ".your-agent"

    def discover(self) -> bool: ...
    def backup(self, staging, tier) -> list: ...
    def restore(self, source, conflict, only) -> None: ...
    def sanitize(self, data) -> tuple: ...
    def status(self, repo) -> dict: ...
```

Register it in `AGENT_PLUGINS` and it will be auto-discovered. PRs welcome!

---

<details>
<summary>Backup directory structure</summary>

```
your-backup-repo/
├── manifest.json          # agents, timestamps, SHA-256 hashes
├── claude-code/
│   ├── claude.json
│   ├── settings.json
│   ├── CLAUDE.md
│   ├── skills/
│   ├── rules/
│   └── ...
├── openclaw/              # only when detected
│   ├── openclaw.json
│   ├── memory/
│   └── ...
└── hermes/                # only when detected
    ├── config.yaml
    ├── SOUL.md
    └── ...
```

Backward compatible: v3.x flat backups are auto-detected as Claude Code.

</details>

<details>
<summary>Changelog</summary>

### v4.0
- Multi-agent support: Claude Code + OpenClaw + Hermes
- Per-agent backup directories
- `--agents` flag for selective operations
- AgentPlugin architecture for extensibility
- Backward compatible with v3.x

### v3.x
- Atomic writes, SHA-256 integrity, smart merge restore
- Cross-platform (Windows / macOS / Linux)
- Selective restore by module

</details>

---

## Requirements

- Python 3.8+ (standard library only, zero `pip install`)
- Git CLI

---

<div align="center">

**Your agent's mind deserves a backup plan.**

[中文说明](#-中文说明) · MIT License · Made by [AlphaWill](https://github.com/AlphaWill0)

</div>

---

## 🇨🇳 中文说明

**agent-mind-migrate** — 多 AI Agent 统一迁移工具。

你花了几周调教 AI Agent——技能、记忆、设置、规则全都配好了。换台电脑，一切归零。这个工具把 Agent 的「心智」全部备份到 Git 仓库，换机器一条命令还原。

**支持的 Agent：** Claude Code · OpenClaw · Hermes

**使用方式：** 对 Claude Code 说「备份一下」即可。或手动运行：

```bash
# 安装
git clone https://github.com/AlphaWill0/agent-mind-migrate.git ~/.claude/skills/agent-mind-migrate

# 备份
python3 ~/.claude/skills/agent-mind-migrate/scripts/migrate.py backup --push

# 换机器还原
git clone <备份仓库URL> ~/.claude-backup
git clone https://github.com/AlphaWill0/agent-mind-migrate.git ~/.claude/skills/agent-mind-migrate
python3 ~/.claude/skills/agent-mind-migrate/scripts/migrate.py restore --dry-run
python3 ~/.claude/skills/agent-mind-migrate/scripts/migrate.py restore --conflict backup-existing
```

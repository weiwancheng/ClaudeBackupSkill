#!/usr/bin/env python3
"""
Claude Code 一键迁移工具 v3.1
用法: python migrate.py <backup|restore|status|validate|init> [options]

零外部依赖，仅需 Python 3.8+ 标准库 + git CLI。

v3.1 改进:
- plans/ 备份（full tier）：保留 Agent 规划方案文档
- 项目根 CLAUDE.md 发现增强：同时扫描 projects 字典键和 githubRepoPaths
- smart-merge 后还原文件权限
- stats-cache 独立还原模块
- manifest contents 改为检测实际备份结果
- .gitignore 条目检测改为按行匹配
- restore 完整性校验失败时默认退出（--force 跳过）
- --version CLI 参数

v3.0 改进:
- 原子备份：先写临时目录再交换，避免中途失败导致仓库损坏
- SHA-256 文件哈希完整性校验
- 智能合并还原：不会将 __REDACTED__ 写入实际配置
- claude.json 深度隐私清理（session/cost/token 等运行时数据）
- 路径穿越防护
- 文件权限保留
- encoding="utf-8" 全覆盖
- symlink 安全处理
- flock 并发保护
- 敏感键白名单机制（取代正则匹配）
- 选择性恢复 --only
- manifest 版本兼容
"""

import argparse
import copy
import datetime
import fcntl
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ── 常量 ──

CLAUDE_HOME = Path.home() / ".claude"
CLAUDE_JSON = Path.home() / ".claude.json"
DEFAULT_REPO = Path.home() / ".claude-backup"
REDACTED = "__REDACTED__"
SCRIPT_VERSION = "3.1"
MANIFEST_VERSION = "3.1"

# 敏感环境变量键名——白名单精确匹配（不再用正则）
SENSITIVE_ENV_KEYS = frozenset({
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CLAUDE_API_KEY",
    "API_KEY",
    "SECRET_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "HF_TOKEN",
    "HUGGINGFACE_TOKEN",
    "WANDB_API_KEY",
    "COHERE_API_KEY",
    "MISTRAL_API_KEY",
    "GOOGLE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "DATABASE_URL",
    "REDIS_URL",
    "PROXY_PASSWORD",
})

# 如果键名包含这些子串，也视为敏感（仅用于 env 字典中的键）
SENSITIVE_SUBSTRINGS = frozenset({
    "_SECRET", "_TOKEN", "_PASSWORD", "_CREDENTIAL",
    "SECRET_", "TOKEN_", "PASSWORD_", "CREDENTIAL_",
})

# 备份时排除的目录（skill 内部）
SKILL_EXCLUDE_DIRS = frozenset({
    "node_modules", ".git", "dist", "__pycache__",
    ".venv", "venv", ".mypy_cache", ".pytest_cache",
})

# ~/.claude.json 中需要脱敏的顶层键
CLAUDE_JSON_SENSITIVE_KEYS = frozenset({"userID"})

# ~/.claude.json 中纯粹的运行时状态，不需要备份
CLAUDE_JSON_EPHEMERAL_KEYS = frozenset({
    "cachedStatsigGates", "metricsStatusCache", "firstStartTime",
})

# ~/.claude.json → projects 下每个项目**仅保留**的有意义字段（白名单策略）
# 其余一切运行时指标（lastCost, lastSessionId, lastModelUsage, lastFps* 等）
# 在新机器上毫无意义，自动剥离。白名单确保未来新增的 last* 字段也会被自动排除。
CLAUDE_JSON_PROJECT_KEEP_KEYS = frozenset({
    "allowedTools",
    "mcpServers",
    "mcpContextUris",
    "enabledMcpjsonServers",
    "disabledMcpjsonServers",
    "hasTrustDialogAccepted",
    "hasCompletedProjectOnboarding",
    "projectOnboardingSeenCount",
    "hasClaudeMdExternalIncludesApproved",
    "hasClaudeMdExternalIncludesWarningShown",
    "exampleFiles",
})

# 备份目录中需要保留的文件/目录（清理时不删除）
REPO_PRESERVE_SET = frozenset({".git", ".gitignore", "README.md", ".backup-staging", ".backup-old"})

# restore --only 允许的模块名
RESTORE_MODULES = frozenset({
    "config", "memory", "skills", "rules", "agents",
    "commands", "scheduled_tasks", "history", "plugins",
    "project_memories", "stats", "plans",
})

# 最小兼容的 manifest 版本
MIN_COMPATIBLE_VERSION = "2.0"

LOCK_FILE = Path(tempfile.gettempdir()) / "claude-migrate.lock"


# ── 工具函数 ──

def run_git(args, cwd=None, check=True):
    # type: (List[str], Optional[Path], bool) -> subprocess.CompletedProcess
    """运行 git 命令，失败时包含 stderr 信息"""
    cmd = ["git"] + args
    result = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        stderr_msg = result.stderr.strip() if result.stderr else "(no stderr)"
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            output=result.stdout, stderr="git {}: {}".format(' '.join(args), stderr_msg)
        )
    return result


def print_header(text):
    # type: (str) -> None
    print("\n" + "=" * 60)
    print("  " + text)
    print("=" * 60 + "\n")


def print_ok(msg):
    # type: (str) -> None
    print("  [OK] " + msg)


def print_warn(msg):
    # type: (str) -> None
    print("  [!!] " + msg)


def print_info(msg):
    # type: (str) -> None
    print("  [..] " + msg)


def print_fail(msg):
    # type: (str) -> None
    print("  [FAIL] " + msg)


def is_sensitive_key(key):
    # type: (str) -> bool
    """判断一个 env key 是否敏感——白名单 + 子串匹配"""
    if key in SENSITIVE_ENV_KEYS:
        return True
    key_upper = key.upper()
    for substr in SENSITIVE_SUBSTRINGS:
        if substr in key_upper:
            return True
    return False


def sha256_file(filepath):
    # type: (Path) -> str
    """计算文件的 SHA-256 哈希"""
    h = hashlib.sha256()
    with open(str(filepath), "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def safe_path(target, base):
    # type: (Path, Path) -> bool
    """检查 target 是否在 base 目录下（防止路径穿越）"""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def acquire_lock():
    # type: () -> object
    """获取文件锁防止并发操作"""
    lock_fd = open(str(LOCK_FILE), "w", encoding="utf-8")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except (IOError, OSError):
        print_fail("另一个 migrate 实例正在运行，请等待完成后重试")
        sys.exit(1)


def release_lock(lock_fd):
    # type: (object) -> None
    """释放文件锁"""
    if lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except (IOError, OSError):
            pass


def read_json_safe(filepath, label=""):
    # type: (Path, str) -> Optional[dict]
    """安全读取 JSON 文件，失败返回 None 并打印警告"""
    try:
        with open(str(filepath), "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print_warn("{} JSON 解析失败: {}".format(label or filepath, e))
        return None
    except (IOError, OSError) as e:
        print_warn("{} 读取失败: {}".format(label or filepath, e))
        return None


def write_json_safe(filepath, data, label=""):
    # type: (Path, dict, str) -> bool
    """安全写入 JSON 文件"""
    try:
        with open(str(filepath), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return True
    except (IOError, OSError) as e:
        print_warn("{} 写入失败: {}".format(label or filepath, e))
        return False


def version_tuple(version_str):
    # type: (str) -> Tuple[int, ...]
    """将版本号字符串转为元组用于比较"""
    try:
        return tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return (0,)


# ── 脱敏函数 ──

def sanitize_settings(data):
    # type: (dict) -> Tuple[dict, List[str]]
    """
    对 settings.json 做深拷贝并脱敏。
    返回 (脱敏后的 dict, 被脱敏的字段路径列表)
    """
    sanitized = copy.deepcopy(data)
    redacted_fields = []

    env = sanitized.get("env", {})
    for key, value in env.items():
        if is_sensitive_key(key) and value and value != REDACTED:
            env[key] = REDACTED
            redacted_fields.append("settings.json -> env.{}".format(key))

    return sanitized, redacted_fields


def sanitize_claude_json(data):
    # type: (dict) -> Tuple[dict, List[str]]
    """
    对 ~/.claude.json 做深拷贝并脱敏。
    - 移除纯运行时字段
    - 脱敏 userID 等敏感字段
    - 清除每个项目的运行时状态（lastSessionId, lastCost 等）
    - projects 下的 allowedTools / mcpServers 保留（这是用户积累）
    返回 (脱敏后的 dict, 被脱敏的字段路径列表)
    """
    sanitized = copy.deepcopy(data)
    redacted_fields = []

    # 移除纯运行时状态
    for key in CLAUDE_JSON_EPHEMERAL_KEYS:
        sanitized.pop(key, None)

    # 脱敏敏感字段
    for key in CLAUDE_JSON_SENSITIVE_KEYS:
        if key in sanitized and sanitized[key]:
            sanitized[key] = REDACTED
            redacted_fields.append(".claude.json -> {}".format(key))

    # 深度清理 projects：白名单策略，仅保留用户有意义的配置
    projects = sanitized.get("projects", {})
    for proj_key, proj_data in projects.items():
        if isinstance(proj_data, dict):
            keys_to_remove = [k for k in proj_data if k not in CLAUDE_JSON_PROJECT_KEEP_KEYS]
            for k in keys_to_remove:
                del proj_data[k]

    return sanitized, redacted_fields


def smart_merge_config(backup_data, live_data, sanitized_fields):
    # type: (dict, dict, List[str]) -> dict
    """
    智能合并配置文件：用备份数据为基础，但对于 REDACTED 的字段，
    保留本地实际值，不覆盖。
    """
    merged = copy.deepcopy(backup_data)

    # 处理 settings.json 的 env 字段
    backup_env = merged.get("env", {})
    live_env = live_data.get("env", {})
    for key, value in backup_env.items():
        if value == REDACTED and key in live_env:
            backup_env[key] = live_env[key]

    # 处理 .claude.json 的顶层敏感字段
    for key in CLAUDE_JSON_SENSITIVE_KEYS:
        if merged.get(key) == REDACTED and key in live_data:
            merged[key] = live_data[key]

    return merged


# ── Skill 处理 ──

def get_skill_info(skill_dir):
    # type: (Path) -> dict
    """获取 skill 的信息：是否 git repo，remote URL 等"""
    info = {"name": skill_dir.name, "type": "local"}

    git_dir = skill_dir / ".git"
    if git_dir.exists():
        # 尝试获取 remote URL
        result = run_git(["remote", "get-url", "origin"], cwd=skill_dir, check=False)
        if result.returncode == 0 and result.stdout.strip():
            info["type"] = "git"
            info["remote"] = result.stdout.strip()

            # 获取当前 branch
            result_branch = run_git(
                ["branch", "--show-current"], cwd=skill_dir, check=False
            )
            info["branch"] = (
                result_branch.stdout.strip()
                if result_branch.returncode == 0 and result_branch.stdout.strip()
                else "main"
            )

            # 获取当前 commit SHA
            result_sha = run_git(
                ["rev-parse", "HEAD"], cwd=skill_dir, check=False
            )
            info["commit"] = (
                result_sha.stdout.strip()
                if result_sha.returncode == 0
                else ""
            )

    return info


def copy_skill_local(src, dst):
    # type: (Path, Path) -> None
    """拷贝本地 skill，排除不需要的目录，不跟随 symlink"""
    if dst.exists():
        shutil.rmtree(str(dst))

    def ignore_func(directory, contents):
        return {item for item in contents if item in SKILL_EXCLUDE_DIRS}

    shutil.copytree(str(src), str(dst), ignore=ignore_func, symlinks=True)


def write_gitremote(skill_info, dest_dir):
    # type: (dict, Path) -> None
    """将 git skill 的信息写入 .gitremote 文件"""
    filepath = dest_dir / "{}.gitremote".format(skill_info["name"])
    content = {
        "name": skill_info["name"],
        "remote": skill_info.get("remote", ""),
        "branch": skill_info.get("branch", "main"),
        "commit": skill_info.get("commit", ""),
    }
    with open(str(filepath), "w", encoding="utf-8") as f:
        json.dump(content, f, indent=2, ensure_ascii=False)
        f.write("\n")


def copy_dir_if_exists(src, dst, label):
    # type: (Path, Path, str) -> bool
    """如果源目录存在则拷贝（不跟随 symlink），返回是否拷贝了"""
    if src.exists() and any(src.iterdir()):
        if dst.exists():
            shutil.rmtree(str(dst))
        shutil.copytree(str(src), str(dst), symlinks=True)
        print_ok("{} 已备份".format(label))
        return True
    return False


def copy_file_if_exists(src, dst, label):
    # type: (Path, Path, str) -> bool
    """如果源文件存在则拷贝，返回是否拷贝了"""
    if src.exists() and not src.is_symlink():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        print_ok("{} 已备份".format(label))
        return True
    elif src.is_symlink():
        print_warn("{} 是符号链接，跳过".format(label))
        return False
    return False


def find_project_claude_mds():
    # type: () -> List[Tuple[Path, str]]
    """
    发现所有项目根目录中的 CLAUDE.md 文件。
    通过两个来源发现项目路径：
    1. ~/.claude.json 的 githubRepoPaths（GitHub 仓库路径映射）
    2. ~/.claude.json 的 projects 字典键名（所有已注册项目路径）
    返回 [(文件路径, 项目标识), ...]
    """
    results = []  # type: List[Tuple[Path, str]]
    seen_paths = set()  # type: Set[str]

    if not CLAUDE_JSON.exists():
        return results

    data = read_json_safe(CLAUDE_JSON, ".claude.json")
    if data is None:
        return results

    def _scan_project_dir(project_dir, project_id):
        # type: (Path, str) -> None
        """扫描一个项目目录中的 CLAUDE.md"""
        if not project_dir.exists() or not project_dir.is_dir():
            return
        # 安全检查：必须在 HOME 下
        if not safe_path(project_dir, Path.home()):
            return
        for claude_md in [
            project_dir / "CLAUDE.md",
            project_dir / ".claude" / "CLAUDE.md",
        ]:
            path_str = str(claude_md)
            if claude_md.exists() and path_str not in seen_paths:
                results.append((claude_md, project_id))
                seen_paths.add(path_str)

    # 来源 1：从 githubRepoPaths 提取（值可能是 string 或 list[string]）
    repo_paths = data.get("githubRepoPaths", {})
    for repo_name, local_paths in repo_paths.items():
        if isinstance(local_paths, str):
            local_paths = [local_paths]
        if not isinstance(local_paths, list):
            continue
        for local_path in local_paths:
            _scan_project_dir(Path(local_path), repo_name)

    # 来源 2：从 projects 字典键名提取（覆盖非 GitHub 项目）
    projects = data.get("projects", {})
    for proj_path_str in projects:
        proj_path = Path(proj_path_str)
        # 跳过 ~/.claude/ 下的路径（这些是 skill 开发目录，不是用户项目）
        try:
            proj_path.resolve().relative_to(CLAUDE_HOME.resolve())
            continue  # skip paths inside ~/.claude/
        except ValueError:
            pass
        # 用路径编码作为 project_id
        safe_id = proj_path_str.replace("/", "-").replace("\\", "-").strip("-")
        _scan_project_dir(proj_path, safe_id)

    return results


def compute_file_hashes(directory):
    # type: (Path) -> Dict[str, str]
    """计算目录中所有文件的 SHA-256 哈希（排除 .git）"""
    hashes = {}
    for item in sorted(directory.rglob("*")):
        if item.is_file() and ".git" not in item.parts:
            rel = str(item.relative_to(directory))
            hashes[rel] = sha256_file(item)
    return hashes


def record_permissions(directory):
    # type: (Path) -> Dict[str, int]
    """记录目录中所有文件的权限模式"""
    perms = {}
    for item in sorted(directory.rglob("*")):
        if item.is_file() and ".git" not in item.parts:
            rel = str(item.relative_to(directory))
            perms[rel] = item.stat().st_mode & 0o777
    return perms


# .gitignore 必须包含的条目
GITIGNORE_REQUIRED_ENTRIES = [
    "*.pre-restore",
    "*.tmp",
    "*.swp",
    "*.swo",
    ".DS_Store",
    "Thumbs.db",
    ".backup-staging/",
    ".backup-old/",
]


def _ensure_gitignore_entries(repo):
    # type: (Path) -> None
    """确保 .gitignore 包含所有必要条目，不存在则创建，已存在则追加缺失项"""
    gitignore = repo / ".gitignore"
    if gitignore.exists():
        with open(str(gitignore), "r", encoding="utf-8") as f:
            existing_lines = set(line.strip() for line in f.read().splitlines())
        missing = [e for e in GITIGNORE_REQUIRED_ENTRIES if e not in existing_lines]
        if missing:
            with open(str(gitignore), "a", encoding="utf-8") as f:
                f.write("\n# auto-added by claude-migrate v3.1\n")
                for entry in missing:
                    f.write(entry + "\n")
    else:
        with open(str(gitignore), "w", encoding="utf-8") as f:
            f.write("# Claude Code backup — auto-generated\n")
            for entry in GITIGNORE_REQUIRED_ENTRIES:
                f.write(entry + "\n")


# ── init 命令 ──

def cmd_init(args):
    """初始化备份仓库并配置远程 Git 仓库"""
    repo = Path(args.repo).expanduser()
    remote_url = args.remote

    print_header("初始化备份仓库")

    # 创建并 init
    if not repo.exists():
        repo.mkdir(parents=True)
    if not (repo / ".git").exists():
        run_git(["init"], cwd=repo)
        print_ok("已创建 git 仓库: {}".format(repo))
    else:
        print_info("git 仓库已存在: {}".format(repo))

    # 配置 remote
    if remote_url:
        result = run_git(["remote", "get-url", "origin"], cwd=repo, check=False)
        if result.returncode == 0:
            old_url = result.stdout.strip()
            if old_url == remote_url:
                print_ok("remote origin 已是: {}".format(remote_url))
            else:
                run_git(["remote", "set-url", "origin", remote_url], cwd=repo)
                print_ok("已更新 remote origin: {} -> {}".format(old_url, remote_url))
        else:
            run_git(["remote", "add", "origin", remote_url], cwd=repo)
            print_ok("已添加 remote origin: {}".format(remote_url))

        # 配置 Git 用户名
        if args.git_user:
            run_git(["config", "user.name", args.git_user], cwd=repo)
            print_ok("已设置 git user.name: {}".format(args.git_user))
        if args.git_email:
            run_git(["config", "user.email", args.git_email], cwd=repo)
            print_ok("已设置 git user.email: {}".format(args.git_email))

        # 写 .gitignore（改进版）
        _ensure_gitignore_entries(repo)
        if not (repo / ".gitignore").stat().st_size > 50:
            print_ok("已创建 .gitignore")

        print()
        print_info("初始化完成。现在可以运行:")
        print_info("  python {} backup --push".format(__file__))
    else:
        print()
        print_info("仓库已创建（本地模式）。如需配置远程仓库:")
        print_info("  python {} init --remote <git-url>".format(__file__))

    print()


# ── backup 命令 ──

def cmd_backup(args):
    repo = Path(args.repo).expanduser()
    tier = args.tier
    message = args.message
    push = args.push

    # 获取文件锁
    lock_fd = acquire_lock()
    try:
        _do_backup(repo, tier, message, push)
    finally:
        release_lock(lock_fd)


def _do_backup(repo, tier, message, push):
    # type: (Path, str, Optional[str], bool) -> None
    print_header("Claude Code 备份 (v{})".format(SCRIPT_VERSION))
    print_info("备份层级: {}".format(tier))
    print_info("备份目标: {}".format(repo))

    # 1. 初始化 git repo（如果不存在）
    if not repo.exists():
        repo.mkdir(parents=True)
        run_git(["init"], cwd=repo)
        print_ok("已创建备份仓库: {}".format(repo))
    elif not (repo / ".git").exists():
        run_git(["init"], cwd=repo)
        print_ok("已初始化 git: {}".format(repo))

    # 2. 原子备份：先写到临时 staging 目录
    staging_dir = repo / ".backup-staging"
    if staging_dir.exists():
        shutil.rmtree(str(staging_dir))
    staging_dir.mkdir(parents=True)

    all_sanitized_fields = []  # type: List[str]

    # ─── 3. ~/.claude.json（主配置文件）───
    if CLAUDE_JSON.exists():
        claude_json_data = read_json_safe(CLAUDE_JSON, ".claude.json")
        if claude_json_data is not None:
            sanitized_data, fields = sanitize_claude_json(claude_json_data)
            all_sanitized_fields.extend(fields)
            if write_json_safe(staging_dir / "claude.json", sanitized_data, ".claude.json"):
                projects = sanitized_data.get("projects", {})
                skill_usage = sanitized_data.get("skillUsage", {})
                print_ok(".claude.json 已备份（{} 个项目配置, {} 条 skill 使用记录）".format(
                    len(projects), len(skill_usage)
                ))
                if fields:
                    print_info("  脱敏: {}".format(", ".join(fields)))
    else:
        print_info("无 ~/.claude.json，跳过")

    # ─── 4. settings.json（脱敏）───
    settings_src = CLAUDE_HOME / "settings.json"
    if settings_src.exists():
        settings_data = read_json_safe(settings_src, "settings.json")
        if settings_data is not None:
            sanitized_data, fields = sanitize_settings(settings_data)
            all_sanitized_fields.extend(fields)
            if write_json_safe(staging_dir / "settings.json", sanitized_data, "settings.json"):
                msg = "settings.json 已备份"
                if fields:
                    msg += "（脱敏: {}）".format(", ".join(fields))
                print_ok(msg)
    else:
        print_warn("settings.json 不存在，跳过")

    # ─── 5. 全局 CLAUDE.md ───
    global_memory = CLAUDE_HOME / "CLAUDE.md"
    if global_memory.exists() and not global_memory.is_symlink():
        shutil.copy2(str(global_memory), str(staging_dir / "CLAUDE.md"))
        print_ok("CLAUDE.md（全局 memory）已备份")
    elif global_memory.is_symlink():
        print_warn("全局 CLAUDE.md 是符号链接，跳过")
    else:
        print_info("无全局 CLAUDE.md，跳过")

    # ─── 6. rules/ ───
    rules_src = CLAUDE_HOME / "rules"
    copy_dir_if_exists(rules_src, staging_dir / "rules", "rules/（用户规则）")

    # ─── 7. agents/ ───
    agents_src = CLAUDE_HOME / "agents"
    copy_dir_if_exists(agents_src, staging_dir / "agents", "agents/（自定义 agents）")

    # ─── 8. commands/ ───
    commands_src = CLAUDE_HOME / "commands"
    copy_dir_if_exists(commands_src, staging_dir / "commands", "commands/（自定义命令）")

    # ─── 9. scheduled_tasks.json ───
    scheduled_src = CLAUDE_HOME / "scheduled_tasks.json"
    copy_file_if_exists(
        scheduled_src, staging_dir / "scheduled_tasks.json",
        "scheduled_tasks.json（定时任务）"
    )

    # ─── 9b. stats-cache.json（使用统计）───
    stats_src = CLAUDE_HOME / "stats-cache.json"
    copy_file_if_exists(
        stats_src, staging_dir / "stats-cache.json",
        "stats-cache.json（使用统计）"
    )

    # ─── 10. Skills ───
    skills_src = CLAUDE_HOME / "skills"
    skills_dst = staging_dir / "skills"
    skills_dst.mkdir(parents=True, exist_ok=True)
    skills_manifest = []  # type: List[dict]

    if skills_src.exists():
        for skill_dir in sorted(skills_src.iterdir()):
            if not skill_dir.is_dir():
                continue

            info = get_skill_info(skill_dir)
            skills_manifest.append(info)

            if info["type"] == "git":
                write_gitremote(info, skills_dst)
                print_ok("skill [{}] -> .gitremote（{}）".format(
                    info["name"], info["remote"]
                ))
            else:
                copy_skill_local(skill_dir, skills_dst / info["name"])
                print_ok("skill [{}] -> 完整拷贝".format(info["name"]))
    else:
        print_warn("skills/ 目录不存在")

    # ─── 11. 项目级 CLAUDE.md（~/.claude/projects/ 内）───
    projects_src = CLAUDE_HOME / "projects"
    projects_dst = staging_dir / "projects"
    project_memories_count = 0

    if projects_src.exists():
        for project_dir in projects_src.iterdir():
            if not project_dir.is_dir():
                continue
            for memory_file in [
                project_dir / "CLAUDE.md",
                project_dir / "memory" / "CLAUDE.md",
            ]:
                if memory_file.exists() and not memory_file.is_symlink():
                    dst_dir = projects_dst / project_dir.name
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    rel = memory_file.relative_to(project_dir)
                    dst_file = dst_dir / rel
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(memory_file), str(dst_file))
                    project_memories_count += 1

    # ─── 12. 项目根目录的 CLAUDE.md ───
    project_root_mds = find_project_claude_mds()
    for claude_md_path, project_id in project_root_mds:
        # 安全检查
        if not safe_path(claude_md_path, Path.home()):
            print_warn("跳过不安全路径: {}".format(claude_md_path))
            continue

        safe_id = project_id.replace("/", "-").replace("\\", "-").strip("-")
        dst_dir = staging_dir / "project-root-memories" / safe_id
        dst_dir.mkdir(parents=True, exist_ok=True)

        shutil.copy2(str(claude_md_path), str(dst_dir / "CLAUDE.md"))
        with open(str(dst_dir / ".source_path"), "w", encoding="utf-8") as f:
            f.write(str(claude_md_path) + "\n")
        project_memories_count += 1

    if project_memories_count > 0:
        print_ok("项目级 memory: {} 个已备份".format(project_memories_count))
    else:
        print_info("无项目级 CLAUDE.md")

    # ─── 13. full tier 额外内容 ───
    if tier == "full":
        # history.jsonl
        copy_file_if_exists(
            CLAUDE_HOME / "history.jsonl",
            staging_dir / "history.jsonl",
            "history.jsonl（命令历史）",
        )

        # plans/（Agent 规划方案文档）
        plans_src = CLAUDE_HOME / "plans"
        if plans_src.exists() and any(plans_src.iterdir()):
            plans_dst = staging_dir / "plans"
            if plans_dst.exists():
                shutil.rmtree(str(plans_dst))
            # 只拷贝 .md 文件，跳过可能的临时文件
            plans_dst.mkdir(parents=True, exist_ok=True)
            plans_count = 0
            for item in plans_src.iterdir():
                if item.is_file() and item.suffix == ".md":
                    shutil.copy2(str(item), str(plans_dst / item.name))
                    plans_count += 1
            if plans_count > 0:
                print_ok("plans/ 已备份（{} 个规划文档）".format(plans_count))

        # plugins（排除 .git/node_modules）
        plugins_src = CLAUDE_HOME / "plugins"
        if plugins_src.exists():
            plugins_dst = staging_dir / "plugins"
            if plugins_dst.exists():
                shutil.rmtree(str(plugins_dst))

            def plugins_ignore(directory, contents):
                return {
                    item for item in contents
                    if item in {".git", "node_modules", "__pycache__"}
                }

            shutil.copytree(
                str(plugins_src), str(plugins_dst),
                ignore=plugins_ignore, symlinks=True
            )
            print_ok("plugins/ 已备份")

    # ─── 14. 计算文件哈希 ───
    print_info("正在计算文件完整性哈希...")
    file_hashes = compute_file_hashes(staging_dir)
    file_permissions = record_permissions(staging_dir)

    # ─── 15. 生成 manifest.json ───
    file_count = len(file_hashes)

    manifest = {
        "version": MANIFEST_VERSION,
        "created_at": datetime.datetime.now().isoformat(),
        "machine": {
            "hostname": platform.node(),
            "os": "{} {}".format(platform.system(), platform.release()),
            "user": os.environ.get("USER", os.environ.get("USERNAME", "unknown")),
            "home": str(Path.home()),
        },
        "tier": tier,
        "contents": {
            # 检测 staging 目录中实际存在的文件（而非源端）
            "claude_json": (staging_dir / "claude.json").exists(),
            "settings_json": (staging_dir / "settings.json").exists(),
            "global_memory": (staging_dir / "CLAUDE.md").exists(),
            "rules": (staging_dir / "rules").exists(),
            "agents": (staging_dir / "agents").exists(),
            "commands": (staging_dir / "commands").exists(),
            "scheduled_tasks": (staging_dir / "scheduled_tasks.json").exists(),
            "stats_cache": (staging_dir / "stats-cache.json").exists(),
            "skills": len(skills_manifest),
            "project_memories": project_memories_count,
            "project_root_memories": len(project_root_mds),
            "history": (staging_dir / "history.jsonl").exists(),
            "plugins": (staging_dir / "plugins").exists(),
            "plans": (staging_dir / "plans").exists(),
        },
        "skills": skills_manifest,
        "sanitized_fields": all_sanitized_fields,
        "file_count": file_count + 1,  # +1 for manifest itself
        "file_hashes": file_hashes,
        "file_permissions": file_permissions,
    }

    manifest_path = staging_dir / "manifest.json"
    write_json_safe(manifest_path, manifest, "manifest.json")
    print_ok("manifest.json 已生成（含 {} 个文件哈希）".format(file_count))

    # ─── 16. 原子交换：staging → repo（三步 rename 策略）───
    print_info("正在执行原子交换...")

    # 确保 .gitignore 包含 staging 目录（防止崩溃后被 git add）
    _ensure_gitignore_entries(repo)

    # 三步原子交换：
    #   1) rename repo 中的旧内容到 .backup-old（保留 .git/.gitignore/README.md）
    #   2) move staging 中的新文件到 repo
    #   3) 成功后删除 .backup-old；失败则回滚
    old_dir = repo / ".backup-old"
    if old_dir.exists():
        shutil.rmtree(str(old_dir))
    old_dir.mkdir()

    # Step 1: 把旧内容移到 .backup-old
    moved_items = []
    try:
        for item in list(repo.iterdir()):
            if item.name in REPO_PRESERVE_SET or item.name in (".backup-staging", ".backup-old"):
                continue
            dst_old = old_dir / item.name
            shutil.move(str(item), str(dst_old))
            moved_items.append((dst_old, repo / item.name))

        # Step 2: 把 staging 中的内容移入 repo
        for item in list(staging_dir.iterdir()):
            shutil.move(str(item), str(repo / item.name))

    except (IOError, OSError, shutil.Error) as e:
        # 回滚：把 .backup-old 中的内容移回 repo
        print_fail("原子交换失败: {}，正在回滚...".format(e))
        for old_item, original_pos in moved_items:
            if old_item.exists() and not original_pos.exists():
                try:
                    shutil.move(str(old_item), str(original_pos))
                except (IOError, OSError):
                    pass
        # 清理
        if old_dir.exists():
            shutil.rmtree(str(old_dir), ignore_errors=True)
        if staging_dir.exists():
            shutil.rmtree(str(staging_dir), ignore_errors=True)
        raise

    # Step 3: 成功，清理临时目录
    if old_dir.exists():
        shutil.rmtree(str(old_dir), ignore_errors=True)
    if staging_dir.exists():
        shutil.rmtree(str(staging_dir), ignore_errors=True)

    print_ok("原子交换完成")

    # ─── 17. Git commit（容错：commit 失败不影响已交换的文件）───
    run_git(["add", "-A"], cwd=repo)

    status_result = run_git(["status", "--porcelain"], cwd=repo)
    if not status_result.stdout.strip():
        print_info("无变更，跳过 commit")
    else:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        commit_msg = message or "Claude Code 备份 ({}) - {}".format(tier, timestamp)
        commit_result = run_git(["commit", "-m", commit_msg], cwd=repo, check=False)
        if commit_result.returncode == 0:
            print_ok("已提交: {}".format(commit_msg))
        else:
            stderr = commit_result.stderr.strip() if commit_result.stderr else "(未知)"
            print_warn("git commit 失败（备份文件已就位，可手动提交）: {}".format(stderr))

    # ─── 18. 可选推送 ───
    if push:
        remote_check = run_git(
            ["remote", "get-url", "origin"], cwd=repo, check=False
        )
        if remote_check.returncode != 0:
            print_warn("未配置远程仓库，跳过推送")
            print_info("请先运行: python {} init --remote <git-url>".format(__file__))
        else:
            branch_result = run_git(
                ["branch", "--show-current"], cwd=repo, check=False
            )
            branch = (
                branch_result.stdout.strip()
                if branch_result.returncode == 0 and branch_result.stdout.strip()
                else "main"
            )

            result = run_git(
                ["push", "-u", "origin", branch], cwd=repo, check=False
            )
            if result.returncode == 0:
                print_ok("已推送到 remote ({})".format(remote_check.stdout.strip()))
            else:
                stderr = result.stderr.strip() if result.stderr else "(未知错误)"
                print_warn("推送失败: {}".format(stderr))

    # ─── 19. 汇总 ───
    print_header("备份完成")
    print_info("备份位置: {}".format(repo))
    print_info("文件总数: {}".format(manifest["file_count"]))
    print_info("Skills: {} 个（git: {}, 本地: {}）".format(
        len(skills_manifest),
        sum(1 for s in skills_manifest if s["type"] == "git"),
        sum(1 for s in skills_manifest if s["type"] == "local"),
    ))
    if all_sanitized_fields:
        print_info("脱敏字段: {}".format(", ".join(all_sanitized_fields)))

    remote_check = run_git(
        ["remote", "get-url", "origin"], cwd=repo, check=False
    )
    if remote_check.returncode != 0:
        print()
        print_info("提示: 尚未配置远程仓库。如需推送到远程:")
        print_info("  python {} init --remote <git-url>".format(__file__))

    print()


# ── restore 命令 ──

def cmd_restore(args):
    repo = Path(args.repo).expanduser()
    dry_run = args.dry_run
    conflict = args.conflict
    only_modules = set(args.only) if args.only else None
    force = args.force

    # 获取文件锁
    lock_fd = None
    if not dry_run:
        lock_fd = acquire_lock()

    try:
        _do_restore(repo, dry_run, conflict, only_modules, force)
    finally:
        if lock_fd:
            release_lock(lock_fd)


def _do_restore(repo, dry_run, conflict, only_modules, force=False):
    # type: (Path, bool, str, Optional[Set[str]], bool) -> None
    print_header("Claude Code 还原" + ("（DRY RUN）" if dry_run else ""))

    if not repo.exists():
        print_fail("备份仓库不存在: {}".format(repo))
        print_info("请先将备份仓库 clone 或拷贝到该路径")
        sys.exit(1)

    manifest_path = repo / "manifest.json"
    if not manifest_path.exists():
        print_fail("manifest.json 不存在，这不是一个有效的备份仓库")
        sys.exit(1)

    manifest = read_json_safe(manifest_path, "manifest.json")
    if manifest is None:
        sys.exit(1)

    # 版本兼容检查
    backup_version = manifest.get("version", "1.0")
    if version_tuple(backup_version) < version_tuple(MIN_COMPATIBLE_VERSION):
        print_fail("备份版本 v{} 太旧，最低兼容 v{}".format(
            backup_version, MIN_COMPATIBLE_VERSION
        ))
        print_info("请使用旧版 migrate.py 或重新备份")
        sys.exit(1)

    print_info("备份版本: v{}".format(backup_version))
    print_info("备份时间: {}".format(manifest.get("created_at", "未知")))
    print_info("来源机器: {}".format(manifest.get("machine", {}).get("hostname", "未知")))
    print_info("备份层级: {}".format(manifest.get("tier", "未知")))
    print_info("冲突策略: {}".format(conflict))
    if only_modules:
        print_info("选择性还原: {}".format(", ".join(sorted(only_modules))))
    print()

    # 完整性校验（如果 manifest 中有哈希）
    file_hashes = manifest.get("file_hashes", {})
    if file_hashes:
        print_info("正在校验备份文件完整性...")
        integrity_ok = True
        checked = 0
        for rel_path, expected_hash in file_hashes.items():
            full_path = repo / rel_path
            if full_path.exists():
                actual_hash = sha256_file(full_path)
                if actual_hash != expected_hash:
                    print_fail("文件损坏: {} (期望 {}..., 实际 {}...)".format(
                        rel_path, expected_hash[:12], actual_hash[:12]
                    ))
                    integrity_ok = False
                checked += 1
        if integrity_ok:
            print_ok("完整性校验通过（{} 个文件）".format(checked))
        else:
            print_fail("部分文件完整性校验失败！建议重新 clone 备份仓库")
            if not dry_run and not force:
                print_fail("还原已中止。使用 --force 可强制跳过完整性检查")
                sys.exit(1)
            elif not dry_run and force:
                print_warn("--force 模式：忽略完整性错误，继续还原")
    else:
        print_info("备份为 v2.0 格式，无完整性哈希（下次备份后将支持）")

    actions = []  # type: List[Tuple[str, Path, Path, str]]

    def should_restore(module_name):
        # type: (str) -> bool
        """检查是否应该还原此模块"""
        if only_modules is None:
            return True
        return module_name in only_modules

    def plan_file(src, dst, desc):
        # type: (Path, Path, str) -> None
        if dst.exists():
            if conflict == "skip":
                actions.append(("skip", src, dst, "[跳过] {}（已存在）".format(desc)))
            elif conflict == "overwrite":
                actions.append(("overwrite", src, dst, "[覆盖] {}".format(desc)))
            elif conflict == "backup-existing":
                actions.append(("backup-overwrite", src, dst, "[备份+覆盖] {}".format(desc)))
        else:
            actions.append(("create", src, dst, "[新建] {}".format(desc)))

    def plan_dir(src, dst, desc):
        # type: (Path, Path, str) -> None
        """规划整个目录的拷贝"""
        if not src.exists():
            return
        for item in src.rglob("*"):
            if item.is_file():
                rel = item.relative_to(src)
                plan_file(item, dst / rel, "{}/{}".format(desc, rel))

    # 1. claude.json -> ~/.claude.json
    if should_restore("config"):
        claude_json_src = repo / "claude.json"
        if claude_json_src.exists():
            if CLAUDE_JSON.exists() and conflict == "skip":
                actions.append(("skip", claude_json_src, CLAUDE_JSON,
                                "[跳过] .claude.json（已存在）"))
            else:
                # 智能合并：保留本机敏感值
                actions.append(("smart-merge-claude-json", claude_json_src, CLAUDE_JSON,
                                "[智能合并] .claude.json（保留本机敏感值）"))

    # 2. settings.json
    if should_restore("config"):
        settings_src = repo / "settings.json"
        if settings_src.exists():
            dst = CLAUDE_HOME / "settings.json"
            if dst.exists() and conflict == "skip":
                actions.append(("skip", settings_src, dst,
                                "[跳过] settings.json（已存在）"))
            else:
                # 智能合并：保留本机敏感值
                actions.append(("smart-merge-settings", settings_src, dst,
                                "[智能合并] settings.json（保留本机敏感值）"))

    # 3. 全局 CLAUDE.md
    if should_restore("memory"):
        memory_src = repo / "CLAUDE.md"
        if memory_src.exists():
            plan_file(memory_src, CLAUDE_HOME / "CLAUDE.md", "CLAUDE.md（全局 memory）")

    # 4. rules/
    if should_restore("rules"):
        rules_src = repo / "rules"
        if rules_src.exists():
            plan_dir(rules_src, CLAUDE_HOME / "rules", "rules")

    # 5. agents/
    if should_restore("agents"):
        agents_src = repo / "agents"
        if agents_src.exists():
            plan_dir(agents_src, CLAUDE_HOME / "agents", "agents")

    # 6. commands/
    if should_restore("commands"):
        commands_src = repo / "commands"
        if commands_src.exists():
            plan_dir(commands_src, CLAUDE_HOME / "commands", "commands")

    # 7. scheduled_tasks.json
    if should_restore("scheduled_tasks"):
        sched_src = repo / "scheduled_tasks.json"
        if sched_src.exists():
            plan_file(
                sched_src, CLAUDE_HOME / "scheduled_tasks.json",
                "scheduled_tasks.json（定时任务）"
            )

    # 7b. stats-cache.json（独立 stats 模块）
    if should_restore("stats"):
        stats_src = repo / "stats-cache.json"
        if stats_src.exists():
            plan_file(
                stats_src, CLAUDE_HOME / "stats-cache.json",
                "stats-cache.json（使用统计）"
            )

    # 8. Skills
    if should_restore("skills"):
        skills_src = repo / "skills"
        if skills_src.exists():
            for item in sorted(skills_src.iterdir()):
                if item.is_dir():
                    plan_dir(
                        item, CLAUDE_HOME / "skills" / item.name,
                        "skill/{}".format(item.name)
                    )
                elif item.suffix == ".gitremote":
                    gitinfo = read_json_safe(item, item.name)
                    if gitinfo is None:
                        continue
                    skill_name = gitinfo.get("name", item.stem)
                    dst = CLAUDE_HOME / "skills" / skill_name
                    if dst.exists():
                        if conflict == "skip":
                            actions.append((
                                "skip", item, dst,
                                "[跳过] skill/{}（git, 已存在）".format(skill_name)
                            ))
                        elif conflict == "overwrite":
                            actions.append((
                                "git-clone", item, dst,
                                "[重新 clone] skill/{} <- {}".format(
                                    skill_name, gitinfo.get("remote", "?")
                                )
                            ))
                        elif conflict == "backup-existing":
                            actions.append((
                                "git-clone-backup", item, dst,
                                "[备份+clone] skill/{} <- {}".format(
                                    skill_name, gitinfo.get("remote", "?")
                                )
                            ))
                    else:
                        actions.append((
                            "git-clone", item, dst,
                            "[clone] skill/{} <- {}".format(
                                skill_name, gitinfo.get("remote", "?")
                            )
                        ))

    # 9. 项目级 memory（~/.claude/projects/）
    if should_restore("project_memories"):
        projects_src = repo / "projects"
        if projects_src.exists():
            for project_dir in projects_src.iterdir():
                if project_dir.is_dir():
                    plan_dir(
                        project_dir,
                        CLAUDE_HOME / "projects" / project_dir.name,
                        "project-memory/{}".format(project_dir.name),
                    )

    # 10. 项目根目录 CLAUDE.md（路径穿越防护）
    if should_restore("project_memories"):
        project_root_src = repo / "project-root-memories"
        if project_root_src.exists():
            for project_dir in project_root_src.iterdir():
                if not project_dir.is_dir():
                    continue
                source_path_file = project_dir / ".source_path"
                if source_path_file.exists():
                    try:
                        with open(str(source_path_file), "r", encoding="utf-8") as f:
                            original_path_str = f.read().strip()
                    except (IOError, OSError):
                        continue
                    original_path = Path(original_path_str)

                    # 路径穿越防护：必须在 HOME 下
                    if not safe_path(original_path, Path.home()):
                        print_warn(
                            "跳过不安全的还原路径: {} (不在 HOME 目录下)".format(
                                original_path
                            )
                        )
                        continue

                    claude_md = project_dir / "CLAUDE.md"
                    if claude_md.exists():
                        plan_file(
                            claude_md, original_path,
                            "项目 CLAUDE.md -> {}".format(original_path)
                        )

    # 11. history.jsonl
    if should_restore("history"):
        history_src = repo / "history.jsonl"
        if history_src.exists():
            plan_file(history_src, CLAUDE_HOME / "history.jsonl", "history.jsonl")

    # 12. plugins
    if should_restore("plugins"):
        plugins_src = repo / "plugins"
        if plugins_src.exists():
            plan_dir(plugins_src, CLAUDE_HOME / "plugins", "plugins")

    # 13. plans/（Agent 规划方案文档）
    if should_restore("plans"):
        plans_src = repo / "plans"
        if plans_src.exists():
            plan_dir(plans_src, CLAUDE_HOME / "plans", "plans")

    # 显示计划
    print_header("还原计划")
    if not actions:
        print_info("没有需要还原的内容")
        return

    for action_type, src, dst, desc in actions:
        print("  {}".format(desc))

    create_count = sum(1 for a in actions if a[0] == "create")
    overwrite_count = sum(
        1 for a in actions
        if a[0] in ("overwrite", "backup-overwrite", "smart-merge-claude-json", "smart-merge-settings")
    )
    skip_count = sum(1 for a in actions if a[0] == "skip")
    clone_count = sum(1 for a in actions if a[0] in ("git-clone", "git-clone-backup"))

    print()
    print_info("新建: {}, 覆盖/合并: {}, 跳过: {}, Git clone: {}".format(
        create_count, overwrite_count, skip_count, clone_count
    ))

    if dry_run:
        print()
        print_warn("这是 DRY RUN，未做任何实际操作")
        print_info("确认无误后，运行不带 --dry-run 的命令来实际执行还原")
        return

    # 实际执行
    print_header("正在执行还原...")

    sanitized_fields = manifest.get("sanitized_fields", [])
    file_permissions_map = manifest.get("file_permissions", {})

    for action_type, src, dst, desc in actions:
        if action_type == "skip":
            continue

        elif action_type == "smart-merge-claude-json":
            # 智能合并 .claude.json：不覆盖本机敏感值
            backup_data = read_json_safe(src, ".claude.json backup")
            if backup_data is None:
                continue
            if dst.exists():
                live_data = read_json_safe(dst, ".claude.json live")
                if live_data is None:
                    live_data = {}
                merged = smart_merge_config(backup_data, live_data, sanitized_fields)
                # 备份旧文件
                if conflict == "backup-existing":
                    backup_path = dst.with_suffix(dst.suffix + ".pre-restore")
                    shutil.copy2(str(dst), str(backup_path))
                write_json_safe(dst, merged, ".claude.json merged")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                write_json_safe(dst, backup_data, ".claude.json")

        elif action_type == "smart-merge-settings":
            # 智能合并 settings.json：不覆盖本机敏感值
            backup_data = read_json_safe(src, "settings.json backup")
            if backup_data is None:
                continue
            if dst.exists():
                live_data = read_json_safe(dst, "settings.json live")
                if live_data is None:
                    live_data = {}
                merged = smart_merge_config(backup_data, live_data, sanitized_fields)
                if conflict == "backup-existing":
                    backup_path = dst.with_suffix(dst.suffix + ".pre-restore")
                    shutil.copy2(str(dst), str(backup_path))
                write_json_safe(dst, merged, "settings.json merged")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                write_json_safe(dst, backup_data, "settings.json")

        elif action_type == "create":
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))

        elif action_type == "overwrite":
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))

        elif action_type == "backup-overwrite":
            backup_path = dst.with_suffix(dst.suffix + ".pre-restore")
            shutil.copy2(str(dst), str(backup_path))
            shutil.copy2(str(src), str(dst))

        elif action_type == "git-clone":
            gitinfo = read_json_safe(src, "gitremote")
            if gitinfo is None:
                continue
            if dst.exists():
                shutil.rmtree(str(dst))
            branch = gitinfo.get("branch", "main")
            result = run_git(
                ["clone", "-b", branch, gitinfo["remote"], str(dst)],
                check=False,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip() if result.stderr else "(未知)"
                print_warn("Clone 失败 [{}]: {}".format(
                    gitinfo.get("name", "?"), stderr
                ))
                continue

        elif action_type == "git-clone-backup":
            gitinfo = read_json_safe(src, "gitremote")
            if gitinfo is None:
                continue
            if dst.exists():
                backup_dir = dst.with_suffix(".pre-restore")
                if backup_dir.exists():
                    shutil.rmtree(str(backup_dir))
                dst.rename(backup_dir)
            branch = gitinfo.get("branch", "main")
            result = run_git(
                ["clone", "-b", branch, gitinfo["remote"], str(dst)],
                check=False,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip() if result.stderr else "(未知)"
                print_warn("Clone 失败 [{}]: {}".format(
                    gitinfo.get("name", "?"), stderr
                ))
                continue

        # 还原文件权限（如果 manifest 中有记录）
        if action_type in (
            "create", "overwrite", "backup-overwrite",
            "smart-merge-claude-json", "smart-merge-settings",
        ) and file_permissions_map:
            try:
                if action_type in ("smart-merge-claude-json", "smart-merge-settings"):
                    # smart-merge 的 src 是备份仓库中的文件，用其相对路径查权限
                    rel_path = str(src.relative_to(repo))
                else:
                    rel_path = str(src.relative_to(repo))
                if rel_path in file_permissions_map:
                    os.chmod(str(dst), file_permissions_map[rel_path])
            except (ValueError, OSError):
                pass

        action_label = desc.split("] ", 1)[-1] if "] " in desc else desc
        print_ok(action_label)

    # 提醒脱敏字段
    remaining_redacted = []
    for field in sanitized_fields:
        remaining_redacted.append(field)

    # 检查还原后是否仍有 REDACTED 值
    settings_live = CLAUDE_HOME / "settings.json"
    if settings_live.exists():
        live_data = read_json_safe(settings_live, "settings.json")
        if live_data:
            env = live_data.get("env", {})
            still_redacted = [k for k, v in env.items() if v == REDACTED]
            if still_redacted:
                print_header("需要手动填写的脱敏字段")
                print_warn("以下 settings.json 字段仍为占位符：")
                for key in still_redacted:
                    print("    - env.{}".format(key))
                print()

    print_header("还原完成")
    print_info("建议运行 validate 命令检查还原结果：")
    print_info("  python {} validate".format(__file__))
    print()


# ── status 命令 ──

def cmd_status(args):
    repo = Path(args.repo).expanduser()

    print_header("Claude Code 备份状态")

    if not repo.exists() or not (repo / ".git").exists():
        print_warn("备份仓库不存在或不是 git 仓库: {}".format(repo))
        print_info("运行 backup 命令创建首次备份")
        return

    # 读取 manifest
    manifest_path = repo / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = read_json_safe(manifest_path, "manifest.json") or {}
        if manifest:
            print_info("备份版本: v{}".format(manifest.get("version", "1.0")))
            print_info("最近备份时间: {}".format(manifest.get("created_at", "未知")))
            print_info("备份层级: {}".format(manifest.get("tier", "未知")))
            print_info("来源机器: {}".format(
                manifest.get("machine", {}).get("hostname", "未知")
            ))
            print_info("文件总数: {}".format(manifest.get("file_count", "未知")))

            # 内容清单
            contents = manifest.get("contents", {})
            if contents:
                items = []
                if contents.get("claude_json"):
                    items.append(".claude.json")
                if contents.get("settings_json"):
                    items.append("settings.json")
                if contents.get("global_memory"):
                    items.append("CLAUDE.md")
                if contents.get("rules"):
                    items.append("rules/")
                if contents.get("agents"):
                    items.append("agents/")
                if contents.get("commands"):
                    items.append("commands/")
                if contents.get("scheduled_tasks"):
                    items.append("scheduled_tasks.json")
                if contents.get("history"):
                    items.append("history.jsonl")
                if contents.get("plugins"):
                    items.append("plugins/")
                if contents.get("plans"):
                    items.append("plans/")
                print_info("包含: {}".format(", ".join(items)))

            skills = manifest.get("skills", [])
            git_skills = [s for s in skills if s.get("type") == "git"]
            local_skills = [s for s in skills if s.get("type") == "local"]
            print_info("Skills: {} 个（git: {}, 本地: {}）".format(
                len(skills), len(git_skills), len(local_skills)
            ))

            pm = contents.get("project_memories", 0)
            prm = contents.get("project_root_memories", 0)
            if pm or prm:
                print_info("项目 memory: {} 个（projects/）+ {} 个（项目根目录）".format(pm, prm))

            if manifest.get("sanitized_fields"):
                print_info("脱敏字段: {}".format(
                    ", ".join(manifest["sanitized_fields"])
                ))

            # 完整性校验
            file_hashes = manifest.get("file_hashes", {})
            if file_hashes:
                corrupted = []
                for rel_path, expected_hash in file_hashes.items():
                    full_path = repo / rel_path
                    if full_path.exists():
                        actual = sha256_file(full_path)
                        if actual != expected_hash:
                            corrupted.append(rel_path)
                if corrupted:
                    print_fail("发现 {} 个文件损坏！".format(len(corrupted)))
                    for f in corrupted[:5]:
                        print_fail("  - {}".format(f))
                    if len(corrupted) > 5:
                        print_fail("  ...及 {} 个更多".format(len(corrupted) - 5))
                else:
                    print_ok("完整性校验通过（{} 个文件）".format(len(file_hashes)))
    else:
        print_warn("manifest.json 不存在")

    # Remote 状态
    remote_result = run_git(["remote", "get-url", "origin"], cwd=repo, check=False)
    if remote_result.returncode == 0:
        print_info("远程仓库: {}".format(remote_result.stdout.strip()))
    else:
        print_info("远程仓库: 未配置")

    # Git 日志
    print_header("备份历史（最近 10 次）")
    result = run_git(
        ["log", "--oneline", "--format=%h  %ci  %s", "-10"],
        cwd=repo, check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.strip().split("\n"):
            print("  {}".format(line))
    else:
        print_info("暂无备份记录")

    # 差异检测
    print_header("当前配置 vs 最近备份")

    # Skills 差异
    live_skills = set()  # type: Set[str]
    skills_dir = CLAUDE_HOME / "skills"
    if skills_dir.exists():
        for d in skills_dir.iterdir():
            if d.is_dir():
                live_skills.add(d.name)

    backed_skills = set()  # type: Set[str]
    backup_skills_dir = repo / "skills"
    if backup_skills_dir.exists():
        for item in backup_skills_dir.iterdir():
            if item.is_dir():
                backed_skills.add(item.name)
            elif item.suffix == ".gitremote":
                backed_skills.add(item.stem)

    new_skills = live_skills - backed_skills
    removed_skills = backed_skills - live_skills

    if new_skills:
        print_warn("新增未备份的 skills: {}".format(", ".join(sorted(new_skills))))
    if removed_skills:
        print_warn("已备份但本地已删除: {}".format(", ".join(sorted(removed_skills))))
    if not new_skills and not removed_skills:
        print_ok("Skills 列表与备份一致")

    # settings.json 差异
    settings_live = CLAUDE_HOME / "settings.json"
    settings_backup = repo / "settings.json"
    if settings_live.exists() and settings_backup.exists():
        live_data = read_json_safe(settings_live, "settings.json live")
        backup_data = read_json_safe(settings_backup, "settings.json backup")

        if live_data and backup_data:
            live_compare = copy.deepcopy(live_data)
            for field in manifest.get("sanitized_fields", []):
                if "settings.json" not in field:
                    continue
                # 解析 "settings.json -> env.KEY" 格式
                if "->" in field:
                    key_part = field.split("->")[-1].strip()
                elif "\u2192" in field:
                    key_part = field.split("\u2192")[-1].strip()
                else:
                    key_part = field
                parts = key_part.split(".")
                obj = live_compare
                for part in parts[:-1]:
                    if isinstance(obj, dict):
                        obj = obj.get(part, {})
                if isinstance(obj, dict) and parts[-1] in obj:
                    obj[parts[-1]] = REDACTED

            if live_compare == backup_data:
                print_ok("settings.json 与备份一致（忽略脱敏字段）")
            else:
                print_warn("settings.json 已有变更（建议重新备份）")

    # 全局 CLAUDE.md
    global_memory = CLAUDE_HOME / "CLAUDE.md"
    backup_memory = repo / "CLAUDE.md"
    if global_memory.exists() and not backup_memory.exists():
        print_warn("全局 CLAUDE.md 存在但未备份")
    elif not global_memory.exists() and backup_memory.exists():
        print_info("备份中有全局 CLAUDE.md，但本地已删除")
    elif global_memory.exists() and backup_memory.exists():
        with open(str(global_memory), "r", encoding="utf-8") as f:
            live_text = f.read()
        with open(str(backup_memory), "r", encoding="utf-8") as f:
            backup_text = f.read()
        if live_text == backup_text:
            print_ok("全局 CLAUDE.md 与备份一致")
        else:
            print_warn("全局 CLAUDE.md 已有变更（建议重新备份）")

    # .claude.json 差异
    if CLAUDE_JSON.exists() and (repo / "claude.json").exists():
        print_ok(".claude.json 已备份")
    elif CLAUDE_JSON.exists():
        print_warn(".claude.json 存在但未备份（可能是旧版备份）")

    print()


# ── validate 命令 ──

def cmd_validate(args):
    print_header("Claude Code 安装验证")

    issues = 0

    # 1. ~/.claude.json
    if CLAUDE_JSON.exists():
        data = read_json_safe(CLAUDE_JSON, ".claude.json")
        if data is not None:
            print_ok(".claude.json 有效（{} 个顶层键）".format(len(data)))

            # 检查脱敏占位符
            if data.get("userID") == REDACTED:
                print_warn(
                    ".claude.json 中 userID 为占位符（正常，会自动重新生成）"
                )
        else:
            print_fail(".claude.json 解析失败")
            issues += 1
    else:
        print_info("无 ~/.claude.json（首次启动时会自动创建）")

    # 2. settings.json
    settings_path = CLAUDE_HOME / "settings.json"
    if settings_path.exists():
        data = read_json_safe(settings_path, "settings.json")
        if data is not None:
            print_ok("settings.json 是有效 JSON")

            env = data.get("env", {})
            redacted = [k for k, v in env.items() if v == REDACTED]
            if redacted:
                print_fail("settings.json 中残留 {} 占位符: {}".format(
                    REDACTED, ", ".join(redacted)
                ))
                issues += 1
            else:
                print_ok("settings.json 无残留占位符")
        else:
            print_fail("settings.json 解析失败")
            issues += 1
    else:
        print_warn("settings.json 不存在")
        issues += 1

    # 3. Skills 完整性
    skills_dir = CLAUDE_HOME / "skills"
    if skills_dir.exists():
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                print_ok("skill [{}] SKILL.md OK".format(skill_dir.name))
            else:
                found = list(skill_dir.rglob("SKILL.md"))
                if found:
                    print_ok("skill [{}] SKILL.md OK（{}）".format(
                        skill_dir.name, found[0].relative_to(skill_dir)
                    ))
                else:
                    print_warn("skill [{}] 缺少 SKILL.md".format(skill_dir.name))
                    issues += 1

            # Git-based skills
            git_dir = skill_dir / ".git"
            if git_dir.exists():
                result = run_git(
                    ["remote", "get-url", "origin"],
                    cwd=skill_dir, check=False,
                )
                if result.returncode == 0:
                    remote = result.stdout.strip()
                    if remote.startswith("http") or remote.startswith("git@"):
                        print_ok("  - git remote: {}".format(remote))
                    else:
                        print_warn("  - git remote 格式异常: {}".format(remote))
                        issues += 1
    else:
        print_warn("skills/ 目录不存在")

    # 4. 其他配置目录
    for name, label in [
        ("rules", "用户规则"),
        ("agents", "自定义 agents"),
        ("commands", "自定义命令"),
    ]:
        path = CLAUDE_HOME / name
        if path.exists():
            count = sum(1 for f in path.rglob("*.md"))
            print_ok("{} / 存在（{} 个 .md 文件）".format(name, count))

    # 5. 全局 CLAUDE.md
    global_memory = CLAUDE_HOME / "CLAUDE.md"
    if global_memory.exists():
        print_ok("全局 CLAUDE.md 存在")
    else:
        print_info("无全局 CLAUDE.md（可通过 /memory 创建）")

    # 6. scheduled_tasks.json
    sched = CLAUDE_HOME / "scheduled_tasks.json"
    if sched.exists():
        data = read_json_safe(sched, "scheduled_tasks.json")
        if data is not None:
            print_ok("scheduled_tasks.json 有效")
        else:
            print_fail("scheduled_tasks.json 无效")
            issues += 1

    # 7. 备份仓库完整性
    repo = Path(args.repo).expanduser() if hasattr(args, "repo") else DEFAULT_REPO
    manifest_path = repo / "manifest.json"
    if manifest_path.exists():
        manifest = read_json_safe(manifest_path, "manifest.json")
        if manifest:
            file_hashes = manifest.get("file_hashes", {})
            if file_hashes:
                corrupted = []
                for rel_path, expected_hash in file_hashes.items():
                    full_path = repo / rel_path
                    if full_path.exists():
                        actual = sha256_file(full_path)
                        if actual != expected_hash:
                            corrupted.append(rel_path)
                if corrupted:
                    print_fail("备份仓库 {} 个文件损坏".format(len(corrupted)))
                    for f in corrupted[:5]:
                        print_fail("  - {}".format(f))
                    issues += len(corrupted)
                else:
                    print_ok("备份仓库完整性校验通过（{} 个文件）".format(
                        len(file_hashes)
                    ))

    # 汇总
    print_header("验证结果")
    if issues == 0:
        print_ok("全部检查通过，环境健康")
    else:
        print_fail("发现 {} 个问题，请检查上方输出".format(issues))

    print()
    return issues


# ── CLI 入口 ──

def main():
    parser = argparse.ArgumentParser(
        description="Claude Code 一键迁移工具 v{}".format(SCRIPT_VERSION),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s init --remote git@github.com:user/backup.git   # 配置远程仓库
  %(prog)s backup                                          # 备份到 ~/.claude-backup
  %(prog)s backup --push                                   # 备份并推送到远程
  %(prog)s backup --tier full --push -m "完整备份"          # 完整备份并推送
  %(prog)s restore --dry-run                               # 预览还原
  %(prog)s restore --conflict backup-existing              # 实际还原
  %(prog)s restore --only skills memory                    # 只还原 skills 和 memory
  %(prog)s status                                          # 查看备份状态
  %(prog)s validate                                        # 健康检查
        """,
    )

    parser.add_argument(
        "--version", action="version",
        version="claude-migrate v{}".format(SCRIPT_VERSION),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = subparsers.add_parser("init", help="初始化备份仓库并配置远程 Git")
    p_init.add_argument(
        "--repo", default=str(DEFAULT_REPO),
        help="备份仓库路径（默认: ~/.claude-backup）",
    )
    p_init.add_argument("--remote", help="远程 Git 仓库 URL")
    p_init.add_argument("--git-user", help="Git 用户名")
    p_init.add_argument("--git-email", help="Git 邮箱")

    # backup
    p_backup = subparsers.add_parser("backup", help="备份当前 Claude Code 配置")
    p_backup.add_argument(
        "--repo", default=str(DEFAULT_REPO),
        help="备份仓库路径（默认: ~/.claude-backup）",
    )
    p_backup.add_argument(
        "--tier", choices=["essential", "full"], default="essential",
        help="备份层级（默认: essential）",
    )
    p_backup.add_argument("--message", "-m", help="自定义 commit message")
    p_backup.add_argument(
        "--push", action="store_true", help="备份后推送到 remote"
    )

    # restore
    p_restore = subparsers.add_parser("restore", help="从备份还原 Claude Code 配置")
    p_restore.add_argument(
        "--repo", default=str(DEFAULT_REPO),
        help="备份仓库路径（默认: ~/.claude-backup）",
    )
    p_restore.add_argument(
        "--dry-run", action="store_true", default=False,
        help="只预览，不实际操作",
    )
    p_restore.add_argument(
        "--conflict", choices=["overwrite", "skip", "backup-existing"],
        default="skip", help="冲突处理策略（默认: skip）",
    )
    p_restore.add_argument(
        "--only", nargs="+", choices=sorted(RESTORE_MODULES),
        help="只还原指定模块（可选: {}）".format(", ".join(sorted(RESTORE_MODULES))),
    )
    p_restore.add_argument(
        "--force", action="store_true", default=False,
        help="完整性校验失败时强制继续还原",
    )

    # status
    p_status = subparsers.add_parser("status", help="查看备份状态和差异")
    p_status.add_argument(
        "--repo", default=str(DEFAULT_REPO),
        help="备份仓库路径（默认: ~/.claude-backup）",
    )

    # validate
    p_validate = subparsers.add_parser("validate", help="验证当前安装的健康状态")
    p_validate.add_argument(
        "--repo", default=str(DEFAULT_REPO),
        help="备份仓库路径（默认: ~/.claude-backup）",
    )

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "backup":
        cmd_backup(args)
    elif args.command == "restore":
        cmd_restore(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "validate":
        sys.exit(cmd_validate(args))


if __name__ == "__main__":
    main()

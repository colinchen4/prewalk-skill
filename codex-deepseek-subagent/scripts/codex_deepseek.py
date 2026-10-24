#!/usr/bin/env python3
"""配置并验证 DeepSeek 作为 Codex 原生子 Agent。"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # macOS / Linux
    msvcrt = None


MODEL = "deepseek-flash"
LEGACY_MODEL_PREFIX = "deepseek-v4-flash"
PROVIDER = "deepseek"
ROLE = "DeepSeek"
EFFORT = "max"
DEFAULT_BASE_URL = "https://api.deepseek.com/"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
PARENT_MULTI_AGENT_VERSION = "v1"
DESKTOP_MULTI_AGENT_V2 = False
MAX_STATE_DATABASES = 32
METADATA_WAIT_SECONDS = 5.0
LOCK_WAIT_SECONDS = 5.0
CREDENTIAL_TARGET = "codex-deepseek-api-key"
OFFICIAL_SETUP_URL = "https://cdn.deepseek.com/api-docs/codex-deepseek-setup-en.sh"
PROVIDER_BEGIN = "# BEGIN CODEX-DEEPSEEK-SUBAGENT PROVIDER"
PROVIDER_END = "# END CODEX-DEEPSEEK-SUBAGENT PROVIDER"
ROLE_BEGIN = "# BEGIN CODEX-DEEPSEEK-SUBAGENT ROLE"
ROLE_END = "# END CODEX-DEEPSEEK-SUBAGENT ROLE"
DESKTOP_CODEX_CANDIDATES = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    Path("/Applications/Codex.app/Contents/Resources/codex"),
)
WINDOWS_CODEX_RELATIVE_CANDIDATES = (
    Path("Programs") / "Codex" / "resources" / "codex.exe",
    Path("Programs") / "OpenAI" / "Codex" / "resources" / "codex.exe",
    Path("Codex") / "resources" / "codex.exe",
)
WINDOWS_CODE_MODE_HOST = "codex-code-mode-host.exe"
WINDOWS_SANDBOX_SETUP = "codex-windows-sandbox-setup.exe"
WINDOWS_COMMAND_RUNNER = "codex-command-runner.exe"


class ManagerError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class Paths:
    home: Path
    config: Path
    catalog: Path
    agent: Path
    state_dir: Path
    manifest: Path


def resolve_paths(codex_home: str | None) -> Paths:
    home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    return Paths(
        home=home,
        config=home / "config.toml",
        catalog=home / "models-with-deepseek.json",
        agent=home / "agents" / f"{ROLE}.toml",
        state_dir=home / "codex-deepseek-subagent",
        manifest=home / "codex-deepseek-subagent" / "manifest.json",
    )


def result(status: str, **kwargs: Any) -> dict[str, Any]:
    return {"status": status, **kwargs}


def emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(payload.get("status", "unknown"))
    for key, value in payload.items():
        if key != "status":
            print(f"{key}: {value}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text_file(path: Path) -> str:
    normalized = path.read_text().replace("\r\n", "\n").replace("\r", "\n")
    return sha256_bytes(normalized.encode())


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def platform_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt" or sys.platform == "win32":
        return "windows"
    return "unsupported"


def windows_runtime_bundle_complete(candidate: Path) -> bool:
    if platform_name() != "windows":
        return True
    return all(
        candidate.with_name(filename).is_file()
        for filename in (WINDOWS_CODE_MODE_HOST, WINDOWS_SANDBOX_SETUP, WINDOWS_COMMAND_RUNNER)
    )


def windows_store_codex_candidates() -> list[Path]:
    if platform_name() != "windows":
        return []
    program_files = os.environ.get("PROGRAMFILES") or r"C:\Program Files"
    windows_apps = Path(program_files) / "WindowsApps"
    candidates: list[Path] = []
    try:
        package_dirs = windows_apps.glob("OpenAI.Codex_*__*")
        for package_dir in package_dirs:
            candidate = package_dir / "app" / "resources" / "codex.exe"
            if candidate.is_file() and windows_runtime_bundle_complete(candidate):
                candidates.append(candidate)
    except OSError:
        return []
    return sorted(
        candidates,
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=True,
    )


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(fd)
    try:
        shutil.copy2(source, tmp_name)
        os.chmod(tmp_name, 0o755)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def stage_windows_store_runtime(source: Path, paths: Paths) -> Path:
    package_name = source.parents[2].name
    cache_key = re.sub(r"[^A-Za-z0-9._-]+", "-", package_name)
    target_dir = paths.state_dir / "desktop-runtime" / cache_key
    required = ("codex.exe", WINDOWS_CODE_MODE_HOST, WINDOWS_SANDBOX_SETUP, WINDOWS_COMMAND_RUNNER)
    for filename in required:
        source_file = source.with_name(filename)
        if not source_file.is_file():
            raise ManagerError(
                "desktop_runtime_incomplete",
                f"Codex 桌面运行时缺少执行面组件：{filename}",
            )
        target_file = target_dir / filename
        if not target_file.is_file() or target_file.stat().st_size != source_file.stat().st_size:
            atomic_copy(source_file, target_file)
    return target_dir / "codex.exe"


def find_desktop_codex(paths: Paths | None = None, allow_stage: bool = False) -> str:
    configured = os.environ.get("CODEX_DESKTOP_BIN")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            if platform_name() == "windows" and not windows_runtime_bundle_complete(candidate):
                raise ManagerError(
                    "desktop_runtime_incomplete",
                    f"CODEX_DESKTOP_BIN 同目录缺少执行面组件（{WINDOWS_CODE_MODE_HOST}、{WINDOWS_SANDBOX_SETUP}、{WINDOWS_COMMAND_RUNNER}）：{candidate}",
                )
            return str(candidate.resolve())
        raise ManagerError(
            "desktop_codex_missing",
            f"CODEX_DESKTOP_BIN 指向的文件不存在：{candidate}",
        )

    candidates: list[Path] = []
    if platform_name() == "macos":
        candidates.extend(DESKTOP_CODEX_CANDIDATES)
    elif platform_name() == "windows":
        for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            root = os.environ.get(variable)
            if root:
                candidates.extend(Path(root) / relative for relative in WINDOWS_CODEX_RELATIVE_CANDIDATES)
    for candidate in candidates:
        if candidate.is_file() and windows_runtime_bundle_complete(candidate):
            return str(candidate.resolve())

    if platform_name() == "windows":
        store_candidates = windows_store_codex_candidates()
        if store_candidates and allow_stage and paths is not None:
            return str(stage_windows_store_runtime(store_candidates[0], paths).resolve())
        if paths is not None:
            cached = sorted(
                paths.state_dir.glob("desktop-runtime/*/codex.exe"),
                key=lambda item: item.stat().st_mtime if item.exists() else 0,
                reverse=True,
            )
            for candidate in cached:
                if candidate.is_file() and windows_runtime_bundle_complete(candidate):
                    return str(candidate.resolve())
        if store_candidates:
            raise ManagerError(
                "desktop_runtime_staging_required",
                "已找到 Windows Store Codex 桌面运行时；运行 setup、repair 或 test 后会缓存完整执行面组件。",
            )

    raise ManagerError(
        "desktop_codex_missing",
        "没有找到 Codex 桌面应用内置运行时。请先安装或启动桌面应用；Windows 自动发现失败时设置 CODEX_DESKTOP_BIN。",
    )


def codex_version_text(codex_bin: str) -> str:
    proc = subprocess.run([codex_bin, "--version"], capture_output=True, text=True, timeout=15)
    text = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode != 0 or not text:
        raise ManagerError("codex_version_unknown", "无法读取 Codex 桌面应用内置运行时版本。")
    return text


def codex_child_env(paths: Paths) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(paths.home)
    for name in list(env):
        if name.upper() == "CODEX_API_KEY":
            env.pop(name, None)
    return env


def credential_account() -> str:
    return getpass.getuser()


def credential_backend() -> str | None:
    current = platform_name()
    if current == "macos" and Path("/usr/bin/security").is_file():
        return "macos-keychain"
    if current == "windows":
        return "windows-credential-manager"
    return None


def credential_available() -> bool:
    return credential_backend() is not None


def _macos_read_credential() -> str | None:
    proc = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            credential_account(),
            "-s",
            CREDENTIAL_TARGET,
            "-w",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _macos_store_credential(secret: str) -> None:
    proc = subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            credential_account(),
            "-s",
            CREDENTIAL_TARGET,
            "-w",
            secret,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise ManagerError("credential_write_failed", "无法把 API Key 写入 macOS Keychain。")


def _macos_remove_credential() -> bool:
    proc = subprocess.run(
        ["/usr/bin/security", "delete-generic-password", "-a", credential_account(), "-s", CREDENTIAL_TARGET],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def _windows_credential_api():
    import ctypes
    from ctypes import wintypes

    class CredentialW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CredentialW)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(CredentialW), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    return ctypes, CredentialW, advapi32


def _windows_read_credential() -> str | None:
    ctypes, credential_type, advapi32 = _windows_credential_api()
    credential = ctypes.POINTER(credential_type)()
    if not advapi32.CredReadW(CREDENTIAL_TARGET, 1, 0, ctypes.byref(credential)):
        error = ctypes.get_last_error()
        if error == 1168:
            return None
        raise ManagerError(
            "credential_read_failed",
            f"无法读取 Windows Credential Manager（错误 {error}）。",
        )
    try:
        raw = ctypes.string_at(
            credential.contents.CredentialBlob,
            credential.contents.CredentialBlobSize,
        )
        return raw.decode("utf-8")
    finally:
        advapi32.CredFree(credential)


def _windows_store_credential(secret: str) -> None:
    ctypes, credential_type, advapi32 = _windows_credential_api()
    raw = secret.encode("utf-8")
    blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    credential = credential_type()
    credential.Flags = 0
    credential.Type = 1
    credential.TargetName = CREDENTIAL_TARGET
    credential.CredentialBlobSize = len(raw)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = 2
    credential.UserName = credential_account()
    if not advapi32.CredWriteW(ctypes.byref(credential), 0):
        error = ctypes.get_last_error()
        raise ManagerError(
            "credential_write_failed",
            f"无法把 API Key 写入 Windows Credential Manager（错误 {error}）。",
        )


def _windows_remove_credential() -> bool:
    ctypes, _, advapi32 = _windows_credential_api()
    if advapi32.CredDeleteW(CREDENTIAL_TARGET, 1, 0):
        return True
    error = ctypes.get_last_error()
    if error == 1168:
        return False
    raise ManagerError(
        "credential_delete_failed",
        f"无法从 Windows Credential Manager 删除 API Key（错误 {error}）。",
    )


def read_credential_key() -> str | None:
    backend = credential_backend()
    if backend == "macos-keychain":
        return _macos_read_credential()
    if backend == "windows-credential-manager":
        return _windows_read_credential()
    raise ManagerError("unsupported_platform", "当前只支持 macOS 和 Windows 系统凭据库。")


def credential_has_key() -> bool:
    if not credential_available():
        return False
    return read_credential_key() is not None


def store_credential_key(secret: str) -> None:
    if not secret.startswith("sk-"):
        raise ManagerError("invalid_api_key", "DeepSeek API Key 应以 sk- 开头。")
    backend = credential_backend()
    if backend == "macos-keychain":
        _macos_store_credential(secret)
        return
    if backend == "windows-credential-manager":
        _windows_store_credential(secret)
        return
    raise ManagerError("unsupported_platform", "当前只支持 macOS 和 Windows 系统凭据库。")


def remove_credential_key() -> bool:
    if not credential_available() or not credential_has_key():
        return False
    if credential_backend() == "macos-keychain":
        return _macos_remove_credential()
    return _windows_remove_credential()


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def toml_string_array(values: list[str]) -> str:
    return "[" + ", ".join(toml_string(value) for value in values) + "]"


def canonical_base_url(raw: str) -> str:
    """校验 OpenAI 兼容 Base URL，返回统一保留末尾斜杠的规范形式。"""
    value = raw.strip()
    if not value:
        raise ManagerError("invalid_base_url", "Base URL 不能为空。")
    if any(character.isspace() for character in value):
        raise ManagerError("invalid_base_url", "Base URL 不能包含空白字符。")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value) or "\\" in value:
        raise ManagerError("invalid_base_url", "Base URL 包含非法字符。")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ManagerError("invalid_base_url", f"Base URL 无法解析：{value}") from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ManagerError("invalid_base_url", "Base URL 必须使用 http 或 https。")
    if parts.username or parts.password:
        raise ManagerError("invalid_base_url", "Base URL 不能在地址中携带用户名或密码。")
    if parts.query or parts.fragment:
        raise ManagerError("invalid_base_url", "Base URL 不能包含查询串或片段。")
    host = parts.hostname
    if not host:
        raise ManagerError("invalid_base_url", "Base URL 缺少主机名。")
    if scheme == "http" and host not in LOOPBACK_HOSTS:
        raise ManagerError(
            "invalid_base_url",
            "只有本机回环地址可以使用 http；其他地址必须使用 https。",
        )
    host_part = f"[{host}]" if ":" in host else host
    if port is not None:
        host_part = f"{host_part}:{port}"
    return f"{scheme}://{host_part}{parts.path.rstrip('/')}/"


def base_url_or_none(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        return canonical_base_url(raw)
    except ManagerError:
        return None


def same_base_url(left: Any, right: Any) -> bool:
    left_url = base_url_or_none(left)
    return left_url is not None and left_url == base_url_or_none(right)


def resolve_desired_base_url(parsed: dict[str, Any], explicit: str | None) -> str:
    """显式 URL 优先；否则保留既有合法 URL；都没有时使用默认地址。"""
    if explicit is not None:
        return canonical_base_url(explicit)
    provider = (parsed.get("model_providers") or {}).get(PROVIDER)
    existing = provider.get("base_url") if isinstance(provider, dict) else None
    return base_url_or_none(existing) or DEFAULT_BASE_URL


def parse_toml_text(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManagerError("invalid_config", f"config.toml 无法解析：{exc}") from exc


def remove_marked_block(text: str, begin: str, end: str) -> str:
    pattern = re.compile(
        rf"\n?{re.escape(begin)}.*?{re.escape(end)}\n?",
        flags=re.DOTALL,
    )
    return pattern.sub("\n", text).rstrip() + ("\n" if text else "")


def remove_managed_blocks(text: str) -> str:
    text = remove_marked_block(text, PROVIDER_BEGIN, PROVIDER_END)
    return remove_marked_block(text, ROLE_BEGIN, ROLE_END)


def toml_table_header(table: str) -> re.Pattern[str]:
    tokens = [
        rf"(?:{re.escape(part)}|\"{re.escape(part)}\"|'{re.escape(part)}')"
        for part in table.split(".")
    ]
    return re.compile(r"^\[\s*" + r"\s*\.\s*".join(tokens) + r"\s*\]\s*(?:#.*)?$")


def remove_toml_table(text: str, table: str) -> str:
    lines = text.splitlines()
    header = toml_table_header(table)
    start = next((index for index, line in enumerate(lines) if header.match(line.strip())), None)
    if start is None:
        return text
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].strip().startswith("[")),
        len(lines),
    )
    kept = lines[:start] + lines[end:]
    return "\n".join(kept).rstrip() + "\n"


def top_level_key(text: str, key: str) -> str | None:
    value = parse_toml_text(text).get(key)
    return value if isinstance(value, str) else None


def set_top_level_key(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    assignment = f"{key} = {toml_string(value)}"
    first_table = next((i for i, line in enumerate(lines) if line.strip().startswith("[")), len(lines))
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(first_table):
        if key_pattern.match(lines[index]):
            lines[index] = assignment
            return "\n".join(lines).rstrip() + "\n"
    lines.insert(first_table, assignment)
    if first_table and lines[first_table - 1].strip():
        lines.insert(first_table + 1, "")
    return "\n".join(lines).rstrip() + "\n"


def remove_top_level_key_if_value(text: str, key: str, expected: str) -> str:
    lines = text.splitlines()
    first_table = next((i for i, line in enumerate(lines) if line.strip().startswith("[")), len(lines))
    kept: list[str] = []
    for index, line in enumerate(lines):
        matches = False
        if index < first_table:
            try:
                matches = tomllib.loads(line).get(key) == expected
            except tomllib.TOMLDecodeError:
                matches = False
        if not matches:
            kept.append(line)
    return "\n".join(kept).rstrip() + "\n"


def set_table_bool(text: str, table: str, key: str, value: bool) -> str:
    lines = text.splitlines()
    assignment = f"{key} = {'true' if value else 'false'}"
    header = toml_table_header(table)
    start = next((index for index, line in enumerate(lines) if header.match(line.strip())), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend((f"[{table}]", assignment))
        return "\n".join(lines).rstrip() + "\n"
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].strip().startswith("[")),
        len(lines),
    )
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(start + 1, end):
        if key_pattern.match(lines[index]):
            lines[index] = assignment
            return "\n".join(lines).rstrip() + "\n"
    lines.insert(end, assignment)
    return "\n".join(lines).rstrip() + "\n"


def remove_table_bool_if_value(text: str, table: str, key: str, expected: bool) -> str:
    lines = text.splitlines()
    header = toml_table_header(table)
    start = next((index for index, line in enumerate(lines) if header.match(line.strip())), None)
    if start is None:
        return text
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].strip().startswith("[")),
        len(lines),
    )
    expected_text = "true" if expected else "false"
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*{expected_text}\s*(?:#.*)?$")
    kept = [
        line
        for index, line in enumerate(lines)
        if not (start < index < end and pattern.match(line))
    ]
    return "\n".join(kept).rstrip() + "\n"


def expected_agent_text() -> str:
    return f'''name = "{ROLE}"
description = "DeepSeek subagent for coding, repository research, review, and verification. Native multimodal: inspects images and screenshots; do not use it for video."
model = "{MODEL}"
model_provider = "{PROVIDER}"
model_reasoning_effort = "{EFFORT}"
developer_instructions = """
You are a focused DeepSeek subagent running inside Codex.

Complete the bounded task assigned by the parent agent, use available tools when needed, and return a concise evidence-based result.
You can inspect images and screenshots. Do not claim to inspect video. If video evidence is required and the parent did not provide a textual description, report that limitation clearly.
Do not spawn additional subagents unless the user or parent explicitly asks for nested delegation.
"""
'''


def managed_provider_block(base_url: str) -> str:
    auth = expected_provider_auth()
    return f'''
{PROVIDER_BEGIN}
[model_providers.{PROVIDER}]
name = "DeepSeek"
base_url = {toml_string(base_url)}
wire_api = "responses"

[model_providers.{PROVIDER}.auth]
command = {toml_string(auth["command"])}
args = {toml_string_array(auth["args"])}
timeout_ms = 5000
refresh_interval_ms = 0
{PROVIDER_END}
'''


def expected_provider_auth() -> dict[str, Any]:
    if credential_backend() == "windows-credential-manager":
        return {
            "command": sys.executable,
            "args": [str(Path(__file__).resolve()), "_credential-get"],
            "timeout_ms": 5000,
            "refresh_interval_ms": 0,
        }
    return {
        "command": "/usr/bin/security",
        "args": [
            "find-generic-password",
            "-a",
            credential_account(),
            "-s",
            CREDENTIAL_TARGET,
            "-w",
        ],
        "timeout_ms": 5000,
        "refresh_interval_ms": 0,
    }


def provider_conflicts(
    provider: dict[str, Any] | None,
    expected_base_url: str | None = None,
) -> list[str]:
    if not provider:
        return []
    issues: list[str] = []
    expected = {
        "name": "DeepSeek",
        "wire_api": "responses",
    }
    for key, value in expected.items():
        if provider.get(key) != value:
            issues.append(f"model_providers.deepseek.{key}")
    if expected_base_url is None:
        if base_url_or_none(provider.get("base_url")) is None:
            issues.append("model_providers.deepseek.base_url")
    elif not same_base_url(provider.get("base_url"), expected_base_url):
        issues.append("model_providers.deepseek.base_url")
    auth = provider.get("auth")
    if not isinstance(auth, dict):
        issues.append("model_providers.deepseek.auth")
        return issues
    for key, value in expected_provider_auth().items():
        if auth.get(key) != value:
            issues.append(f"model_providers.deepseek.auth.{key}")
    return issues


def compatible_existing(
    parsed: dict[str, Any],
    paths: Paths,
    expected_base_url: str,
) -> tuple[bool, list[str]]:
    issues: list[str] = []
    provider = (parsed.get("model_providers") or {}).get(PROVIDER)
    issues.extend(provider_conflicts(provider, expected_base_url))
    agent = (parsed.get("agents") or {}).get(ROLE)
    if agent:
        if set(agent) - {"description", "config_file"}:
            issues.append("agents.DeepSeek")
        if Path(agent.get("config_file", "")).expanduser() != paths.agent:
            issues.append("agents.DeepSeek.config_file")
    return not issues, issues


def fetch_official_deepseek_model() -> dict[str, Any]:
    request = urllib.request.Request(
        OFFICIAL_SETUP_URL,
        headers={"User-Agent": "codex-deepseek-subagent/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            script = response.read().decode("utf-8")
    except Exception as exc:
        raise ManagerError("official_catalog_unavailable", "无法获取 DeepSeek 官方 Codex 模型目录。") from exc
    match = re.search(
        r"<<'CODEX_MODELS_JSON'\n(.*?)\nCODEX_MODELS_JSON",
        script,
        flags=re.DOTALL,
    )
    if not match:
        raise ManagerError("official_catalog_changed", "DeepSeek 官方安装脚本格式已变化，未执行任何远程脚本。")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ManagerError("official_catalog_invalid", "DeepSeek 官方模型目录不是有效 JSON。") from exc
    for model in payload.get("models", []):
        if model.get("slug") == MODEL:
            return model
    raise ManagerError("official_model_missing", f"官方目录中没有 {MODEL}。")


def run_codex_models(codex_bin: str, paths: Paths) -> dict[str, Any]:
    env = codex_child_env(paths)
    proc = subprocess.run(
        [codex_bin, "debug", "models"],
        capture_output=True,
        text=True,
        env=env,
        timeout=45,
    )
    if proc.returncode != 0:
        raise ManagerError("codex_catalog_failed", "Codex 无法读取当前模型目录。", {"stderr": proc.stderr[-800:]})
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ManagerError("codex_catalog_invalid", "Codex 返回的模型目录不是有效 JSON。") from exc


def load_base_catalog(codex_bin: str, paths: Paths, config: dict[str, Any]) -> dict[str, Any]:
    configured_path = config.get("model_catalog_json")
    if configured_path:
        candidate = Path(configured_path).expanduser()
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text())
                if isinstance(data.get("models"), list):
                    return data
            except (OSError, json.JSONDecodeError):
                pass
    return run_codex_models(codex_bin, paths)


def merged_catalog(base: dict[str, Any], deepseek_model: dict[str, Any], parent_model: str) -> dict[str, Any]:
    models = [
        model
        for model in base.get("models", [])
        if model.get("slug") != MODEL
        and not str(model.get("slug", "")).startswith(LEGACY_MODEL_PREFIX)
    ]
    deepseek_model["auto_review_model_override"] = MODEL
    models.append(deepseek_model)
    parent_found = False
    for model in models:
        if model.get("slug") == parent_model:
            model["multi_agent_version"] = PARENT_MULTI_AGENT_VERSION
            parent_found = True
            break
    if not parent_found:
        raise ManagerError("parent_model_missing", f"模型目录中没有父模型 {parent_model}。")
    models.sort(key=lambda item: item.get("slug", ""))
    return {"models": models}


def configured_parent_model(config: dict[str, Any]) -> str | None:
    model = config.get("model")
    if isinstance(model, str) and model and not model.startswith("deepseek"):
        return model
    return None


def make_backup(paths: Paths) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = paths.state_dir / "backups" / stamp
    backup.mkdir(parents=True, exist_ok=False)
    for source in (paths.config, paths.catalog, paths.agent, paths.manifest):
        if source.is_file():
            shutil.copy2(source, backup / source.name)
    return backup


def write_manifest(paths: Paths, payload: dict[str, Any]) -> None:
    atomic_write(paths.manifest, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())


def read_manifest(paths: Paths) -> dict[str, Any]:
    if not paths.manifest.is_file():
        return {}
    try:
        payload = json.loads(paths.manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def install(paths: Paths, codex_bin: str, base_url: str | None = None) -> dict[str, Any]:
    paths.home.mkdir(parents=True, exist_ok=True)
    config_text = paths.config.read_text() if paths.config.is_file() else ""
    parsed = parse_toml_text(config_text) if config_text.strip() else {}
    desired_base_url = resolve_desired_base_url(parsed, base_url)
    previous_manifest = read_manifest(paths)
    provider_marker_present = PROVIDER_BEGIN in config_text and PROVIDER_END in config_text
    role_marker_present = ROLE_BEGIN in config_text and ROLE_END in config_text
    unmanaged_config = remove_managed_blocks(config_text)
    unmanaged_parsed = parse_toml_text(unmanaged_config) if unmanaged_config.strip() else {}
    compatible, conflicts = compatible_existing(unmanaged_parsed, paths, desired_base_url)
    if not compatible:
        raise ManagerError("conflict", "发现不兼容的现有 DeepSeek 配置。", {"fields": conflicts})
    previous_agent_managed = bool(previous_manifest.get("managed_agent_file"))
    if (
        paths.agent.is_file()
        and paths.agent.read_text() != expected_agent_text()
        and not previous_agent_managed
    ):
        raise ManagerError("conflict", "现有 DeepSeek Agent 文件与目标配置不同。", {"path": str(paths.agent)})
    legacy_role_present = bool((unmanaged_parsed.get("agents") or {}).get(ROLE))
    if legacy_role_present:
        unmanaged_config = remove_toml_table(unmanaged_config, f"agents.{ROLE}")
        unmanaged_parsed = parse_toml_text(unmanaged_config) if unmanaged_config.strip() else {}

    catalog_preexisted_now = paths.catalog.is_file()
    agent_preexisted_now = paths.agent.is_file()
    selected_before = parsed.get("model_catalog_json") == str(paths.catalog)
    previous_schema = previous_manifest.get("schema_version", 1) if previous_manifest else 2
    previous_selection_managed = bool(previous_manifest.get("managed_catalog_selection"))
    if previous_manifest and previous_schema < 2 and selected_before:
        previous_selection_managed = True
    managed_catalog_selection = previous_selection_managed or not selected_before
    previous_catalog_value = (
        previous_manifest.get("previous_model_catalog_json")
        if previous_selection_managed and selected_before
        else parsed.get("model_catalog_json")
    )
    if previous_manifest and previous_schema < 2:
        catalog_preexisted = bool(previous_manifest.get("catalog_preexisted", False))
        agent_preexisted = bool(
            previous_manifest.get(
                "agent_preexisted",
                not previous_manifest.get("managed_agent_file", False),
            )
        )
    else:
        catalog_preexisted = bool(previous_manifest.get("catalog_preexisted", catalog_preexisted_now))
        agent_preexisted = bool(previous_manifest.get("agent_preexisted", agent_preexisted_now))
    current_multi_agent_v2 = (parsed.get("features") or {}).get("multi_agent_v2")
    previous_multi_agent_v2 = (
        previous_manifest.get("previous_multi_agent_v2")
        if previous_manifest.get("managed_multi_agent_v2")
        else current_multi_agent_v2
    )
    managed_multi_agent_v2 = bool(
        previous_manifest.get("managed_multi_agent_v2")
        or current_multi_agent_v2 is not DESKTOP_MULTI_AGENT_V2
    )

    backup = make_backup(paths)
    try:
        deepseek_model = fetch_official_deepseek_model()
        base = load_base_catalog(codex_bin, paths, parsed)
        parent_model = configured_parent_model(parsed)
        if not parent_model:
            raise ManagerError("parent_model_unconfigured", "桌面配置中没有明确的非 DeepSeek 父模型。")
        previous_parent = previous_manifest.get("parent_model")
        if previous_parent and previous_parent != parent_model:
            previous_entry = next(
                (item for item in base.get("models", []) if item.get("slug") == previous_parent),
                None,
            )
            if previous_entry is not None:
                original_version = previous_manifest.get("parent_original_multi_agent_version")
                if original_version is None:
                    previous_entry.pop("multi_agent_version", None)
                else:
                    previous_entry["multi_agent_version"] = original_version
        current_parent_entry = next(
            (item for item in base.get("models", []) if item.get("slug") == parent_model),
            None,
        )
        if not current_parent_entry:
            raise ManagerError("parent_model_missing", f"模型目录中没有父模型 {parent_model}。")
        parent_original_version = (
            previous_manifest.get("parent_original_multi_agent_version")
            if previous_parent == parent_model
            else current_parent_entry.get("multi_agent_version")
        )
        catalog = merged_catalog(base, deepseek_model, parent_model)
        catalog_bytes = (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode()

        new_config = unmanaged_config
        provider_exists = bool((unmanaged_parsed.get("model_providers") or {}).get(PROVIDER))
        if not provider_exists:
            new_config = new_config.rstrip() + "\n" + managed_provider_block(desired_base_url)
        new_config = set_top_level_key(new_config, "model_catalog_json", str(paths.catalog))
        new_config = set_table_bool(
            new_config,
            "features",
            "multi_agent_v2",
            DESKTOP_MULTI_AGENT_V2,
        )
        parse_toml_text(new_config)
        json.loads(catalog_bytes)

        atomic_write(paths.catalog, catalog_bytes)
        if not paths.agent.is_file() or previous_agent_managed:
            atomic_write(paths.agent, expected_agent_text().encode(), mode=0o644)
        atomic_write(paths.config, new_config.encode())

        managed_agent_file = previous_agent_managed or not agent_preexisted_now
        catalog_original_backup = previous_manifest.get("catalog_original_backup")
        if not catalog_original_backup and catalog_preexisted_now:
            candidate = backup / paths.catalog.name
            if candidate.is_file():
                catalog_original_backup = str(candidate)
        adopted_existing = provider_exists or agent_preexisted or catalog_preexisted
        manifest = {
            "schema_version": 3,
            "installed_at": datetime.now().isoformat(timespec="seconds"),
            "backup": str(backup),
            "previous_model_catalog_json": previous_catalog_value,
            "managed_catalog_selection": managed_catalog_selection,
            "managed_provider_block": provider_marker_present or not provider_exists,
            "managed_agent_file": managed_agent_file,
            "catalog_preexisted": catalog_preexisted,
            "catalog_original_backup": catalog_original_backup,
            "agent_preexisted": agent_preexisted,
            "legacy_role_block_removed": role_marker_present or legacy_role_present,
            "adopted_existing": adopted_existing,
            "parent_model": parent_model,
            "parent_multi_agent_version": PARENT_MULTI_AGENT_VERSION,
            "parent_original_multi_agent_version": parent_original_version,
            "managed_multi_agent_v2": managed_multi_agent_v2,
            "previous_multi_agent_v2": previous_multi_agent_v2,
            "desktop_multi_agent_v2": DESKTOP_MULTI_AGENT_V2,
            "base_url": desired_base_url,
            "config_sha256": sha256_bytes(new_config.encode()),
            "catalog_sha256": sha256_bytes(catalog_bytes),
            "agent_sha256": sha256_bytes(expected_agent_text().encode()),
        }
        write_manifest(paths, manifest)
        return {
            "backup": str(backup),
            "adopted_existing": adopted_existing,
            "base_url": desired_base_url,
        }
    except Exception:
        restore_backup(paths, backup)
        raise


def static_status(paths: Paths, codex_bin: str | None = None) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "config_exists": paths.config.is_file(),
        "catalog_exists": paths.catalog.is_file(),
        "agent_exists": paths.agent.is_file(),
        "credential_backend": credential_backend(),
        "credential_present": credential_has_key(),
        "manifest_exists": paths.manifest.is_file(),
    }
    errors: list[str] = []
    parsed: dict[str, Any] = {}
    if paths.config.is_file():
        try:
            parsed = parse_toml_text(paths.config.read_text())
            checks["config_valid"] = True
        except ManagerError as exc:
            checks["config_valid"] = False
            errors.append(str(exc))
    provider = (parsed.get("model_providers") or {}).get(PROVIDER)
    role = (parsed.get("agents") or {}).get(ROLE)
    provider_base_url = provider.get("base_url") if isinstance(provider, dict) else None
    checks["provider_registered"] = bool(provider)
    checks["provider_base_url"] = provider_base_url
    checks["provider_base_url_valid"] = base_url_or_none(provider_base_url) is not None
    checks["provider_valid"] = bool(provider) and not provider_conflicts(provider)
    checks["agent_discovery"] = "standalone"
    checks["legacy_role_registration_present"] = bool(role)
    checks["legacy_role_registration_absent"] = not bool(role)
    checks["catalog_selected"] = Path(parsed.get("model_catalog_json", "")).expanduser() == paths.catalog
    checks["desktop_multi_agent_v2"] = (parsed.get("features") or {}).get("multi_agent_v2")
    checks["desktop_multi_agent_v2_disabled"] = (
        checks["desktop_multi_agent_v2"] is DESKTOP_MULTI_AGENT_V2
    )
    parent_model = configured_parent_model(parsed)
    checks["parent_model"] = parent_model
    checks["parent_model_configured"] = bool(parent_model)
    if paths.catalog.is_file():
        try:
            data = json.loads(paths.catalog.read_text())
            checks["model_registered"] = any(item.get("slug") == MODEL for item in data.get("models", []))
            parent_entry = next(
                (item for item in data.get("models", []) if parent_model and item.get("slug") == parent_model),
                None,
            )
            checks["parent_multi_agent_version"] = (
                parent_entry.get("multi_agent_version") if parent_entry else None
            )
            checks["parent_uses_plaintext_v1"] = (
                checks["parent_multi_agent_version"] == PARENT_MULTI_AGENT_VERSION
            )
        except (OSError, json.JSONDecodeError):
            checks["model_registered"] = False
            checks["parent_uses_plaintext_v1"] = False
            errors.append("模型目录无法解析。")
    else:
        checks["model_registered"] = False
        checks["parent_uses_plaintext_v1"] = False
    checks["agent_content_valid"] = paths.agent.is_file() and paths.agent.read_text() == expected_agent_text()

    version: tuple[int, int, int] | None = None
    if codex_bin:
        try:
            version_text = codex_version_text(codex_bin)
            checks["desktop_codex_path"] = codex_bin
            checks["desktop_codex_version"] = version_text
            checks["desktop_codex_detected"] = True
        except ManagerError as exc:
            checks["desktop_codex_detected"] = False
            errors.append(str(exc))
    required = (
        "config_valid",
        "provider_valid",
        "catalog_selected",
        "model_registered",
        "parent_model_configured",
        "parent_uses_plaintext_v1",
        "desktop_multi_agent_v2_disabled",
        "agent_content_valid",
        "credential_present",
        "manifest_exists",
        "legacy_role_registration_absent",
        "desktop_codex_detected",
    )
    ready = all(checks.get(key) is True for key in required)
    return result("configured" if ready else "partial", checks=checks, errors=errors)


def direct_test(paths: Paths, codex_bin: str) -> dict[str, Any]:
    env = codex_child_env(paths)
    prompt = "Reply exactly DEEPSEEK_DIRECT_OK and nothing else."
    proc = subprocess.run(
        [
            codex_bin,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            "-s",
            "read-only",
            "-C",
            str(paths.home),
            "-m",
            MODEL,
            "-c",
            'model_provider="deepseek"',
            "-c",
            f'model_reasoning_effort="{EFFORT}"',
            prompt,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    if proc.returncode != 0 or "DEEPSEEK_DIRECT_OK" not in proc.stdout:
        raise ManagerError(
            "direct_test_failed",
            "DeepSeek 直连测试失败。",
            {"stderr": proc.stderr[-1000:]},
        )
    return {"direct": True}


def choose_parent_model(paths: Paths) -> str:
    parsed = parse_toml_text(paths.config.read_text())
    parent_model = configured_parent_model(parsed)
    if not parent_model:
        raise ManagerError("parent_model_unconfigured", "桌面配置中没有明确的非 DeepSeek 父模型。")
    return parent_model


def query_child_metadata(
    paths: Paths,
    child_id: str,
    deadline: float | None = None,
) -> dict[str, Any] | None:
    candidates: list[tuple[float, Path]] = []
    for state_db in paths.home.glob("state_*.sqlite"):
        try:
            candidates.append((state_db.stat().st_mtime, state_db))
        except OSError:
            continue
    for _, state_db in sorted(candidates, reverse=True)[:MAX_STATE_DATABASES]:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        try:
            with sqlite3.connect(
                f"{state_db.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=0.05,
            ) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(threads)").fetchall()
                }
                required = {"id", "model_provider", "model", "reasoning_effort", "agent_role"}
                if not required.issubset(columns):
                    continue
                row = connection.execute(
                    "SELECT model_provider, model, reasoning_effort, agent_role FROM threads WHERE id = ?",
                    (child_id,),
                ).fetchone()
        except (OSError, sqlite3.Error):
            continue
        if row:
            return {
                "model_provider": row[0],
                "model": row[1],
                "reasoning_effort": row[2],
                "agent_role": row[3],
            }
    return None


def wait_for_child_metadata(
    paths: Paths,
    child_id: str,
    timeout_seconds: float = METADATA_WAIT_SECONDS,
    poll_interval: float = 0.2,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        metadata = query_child_metadata(paths, child_id, deadline)
        if metadata is not None:
            return metadata
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_interval, remaining))


def normalize_native_verification_message(message: str | None) -> str | None:
    if not isinstance(message, str):
        return None
    return message.rstrip(".!?。！？").strip()


def native_test(paths: Paths, codex_bin: str) -> dict[str, Any]:
    # 桌面 CLI ≥0.146 把 collab/spawn_agent 限定在 ChatGPT 后端，原生 spawn 无法把子线程
    # 路由到自定义 provider（openai/codex#31882、#36957）。执行段改用受管 codex exec 进程，
    # 这里验收该执行面：桌面运行时 + deepseek provider + max 推理能按口令返回。
    env = codex_child_env(paths)
    prompt = "You are the managed DeepSeek executor surface. Reply exactly NATIVE_DEEPSEEK_OK and nothing else."
    proc = subprocess.run(
        [
            codex_bin,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            "-s",
            "read-only",
            "-C",
            str(paths.home),
            "-m",
            MODEL,
            "-c",
            'model_provider="deepseek"',
            "-c",
            f'model_reasoning_effort="{EFFORT}"',
            prompt,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    if proc.returncode != 0 or "NATIVE_DEEPSEEK_OK" not in proc.stdout:
        raise ManagerError(
            "executor_surface_failed",
            "DeepSeek 执行面验收失败（受管 codex exec 未按口令返回）。",
            {"stderr": proc.stderr[-1200:]},
        )
    return {
        "desktop_fresh_session_native": True,
        "executor_surface": "codex_exec",
        "model_provider": PROVIDER,
        "model": MODEL,
        "reasoning_effort": EFFORT,
        "agent_role": ROLE,
    }


def executor_workchain_test(paths: Paths, codex_bin: str) -> dict[str, Any]:
    # 验收真实工作链：在临时工作区用 workspace-write 沙箱执行“读目录 → 写文件 → 跑命令”。
    # 只测连通性会漏掉 Windows 沙箱依赖（如 codex-windows-sandbox-setup.exe 缺失导致的
    # CreateProcessWithLogonW failed: 2），届时执行器收到任务包也无法碰代码。
    env = codex_child_env(paths)
    workspace = Path(tempfile.mkdtemp(prefix="deepseek-workchain-"))
    probe = workspace / "prewalk_executor_probe.txt"
    prompt = (
        "You are the managed DeepSeek executor in a temporary workspace. Do exactly this and nothing else: "
        "(1) create a file named prewalk_executor_probe.txt whose single line is EXECUTOR_PROBE_OK; "
        "(2) run the command `git status` and continue regardless of its exit code; "
        "(3) reply with exactly WORKCHAIN_OK and nothing else."
    )
    try:
        proc = subprocess.run(
            [
                codex_bin,
                "exec",
                "--skip-git-repo-check",
                "--json",
                "-s",
                "workspace-write",
                "-C",
                str(workspace),
                "-m",
                MODEL,
                "-c",
                'model_provider="deepseek"',
                "-c",
                f'model_reasoning_effort="{EFFORT}"',
                prompt,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        marker = probe.read_text(encoding="utf-8", errors="replace").strip() if probe.is_file() else None
        if proc.returncode != 0 or "WORKCHAIN_OK" not in proc.stdout or marker != "EXECUTOR_PROBE_OK":
            raise ManagerError(
                "executor_workchain_failed",
                "DeepSeek 执行面工作链验收失败（读写/命令执行未通过沙箱）。",
                {
                    "stderr": proc.stderr[-1200:],
                    "marker": marker,
                },
            )
        return {"executor_workchain": True, "sandbox": "workspace-write"}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def run_tests(paths: Paths, codex_bin: str) -> dict[str, Any]:
    status = static_status(paths, codex_bin)
    if status["status"] != "configured":
        raise ManagerError("not_configured", "静态配置尚未完整，不能运行实时测试。", status)
    direct = direct_test(paths, codex_bin)
    native = native_test(paths, codex_bin)
    workchain = executor_workchain_test(paths, codex_bin)
    return result(
        "ready",
        **direct,
        **native,
        **workchain,
        new_task_required=False,
        restart_required=False,
        note="执行段为受管 codex exec 进程，父会话无需注册 DeepSeek 角色。",
    )


def restore_backup(paths: Paths, backup: Path) -> None:
    for target in (paths.config, paths.catalog, paths.agent, paths.manifest):
        source = backup / target.name
        if source.is_file():
            atomic_write(target, source.read_bytes(), mode=0o644 if target == paths.agent else 0o600)
        elif target.is_file():
            target.unlink()


def setup(
    paths: Paths,
    codex_bin: str,
    api_key_stdin: bool,
    skip_live_test: bool,
    base_url: str | None = None,
) -> dict[str, Any]:
    if not credential_available():
        raise ManagerError("unsupported", "当前只支持 macOS 和 Windows 系统凭据库。")
    credential_created = False
    if not credential_has_key():
        if not api_key_stdin:
            return result("credential_missing", credential="deepseek_api_key")
        secret = sys.stdin.readline().strip()
        if not secret:
            raise ManagerError("credential_missing", "标准输入中没有 API Key。")
        store_credential_key(secret)
        secret = ""
        credential_created = True

    install_result: dict[str, Any] | None = None
    try:
        install_result = install(paths, codex_bin, base_url)
        if skip_live_test:
            return result(
                "configured",
                **install_result,
                new_task_required=True,
                restart_required=True,
            )
        tested = run_tests(paths, codex_bin)
        return {**tested, **install_result}
    except Exception:
        if install_result and install_result.get("backup"):
            restore_backup(paths, Path(install_result["backup"]))
        if credential_created:
            remove_credential_key()
        raise


def disable(paths: Paths) -> dict[str, Any]:
    if not paths.manifest.is_file():
        raise ManagerError("not_managed", "没有找到本 Skill 的管理记录，拒绝修改现有配置。")
    manifest = read_manifest(paths)
    if manifest.get("managed_agent_file") and paths.agent.is_file():
        if sha256_text_file(paths.agent) != manifest.get("agent_sha256"):
            raise ManagerError(
                "conflict",
                "DeepSeek Agent 文件已被修改，拒绝停用。",
                {"path": str(paths.agent)},
            )
    changed = False
    if paths.config.is_file():
        text = paths.config.read_text()
        updated = remove_marked_block(text, ROLE_BEGIN, ROLE_END)
        if manifest.get("managed_multi_agent_v2"):
            previous = manifest.get("previous_multi_agent_v2")
            if isinstance(previous, bool):
                updated = set_table_bool(updated, "features", "multi_agent_v2", previous)
            else:
                updated = remove_table_bool_if_value(
                    updated,
                    "features",
                    "multi_agent_v2",
                    DESKTOP_MULTI_AGENT_V2,
                )
        if updated != text:
            parse_toml_text(updated)
            atomic_write(paths.config, updated.encode())
            changed = True
    if manifest.get("managed_agent_file") and paths.agent.is_file():
        paths.agent.unlink()
        changed = True
    return result(
        "disabled",
        changed=changed,
        agent_preserved=not bool(manifest.get("managed_agent_file")),
        credential_preserved=credential_has_key(),
    )


def uninstall(paths: Paths, remove_credential: bool) -> dict[str, Any]:
    manifest = read_manifest(paths)
    if not manifest:
        raise ManagerError("not_managed", "没有找到本 Skill 的管理记录，拒绝修改现有配置。")
    if paths.catalog.is_file() and sha256_bytes(paths.catalog.read_bytes()) != manifest.get("catalog_sha256"):
        raise ManagerError(
            "conflict",
            "模型目录已被修改，拒绝卸载。",
            {"path": str(paths.catalog)},
        )
    backup = make_backup(paths)
    try:
        disabled = disable(paths)
        if paths.config.is_file():
            text = paths.config.read_text()
            if manifest.get("managed_provider_block"):
                text = remove_marked_block(text, PROVIDER_BEGIN, PROVIDER_END)
            if manifest.get("managed_catalog_selection"):
                previous_catalog = manifest.get("previous_model_catalog_json")
                if previous_catalog is None:
                    text = remove_top_level_key_if_value(text, "model_catalog_json", str(paths.catalog))
                elif top_level_key(text, "model_catalog_json") == str(paths.catalog):
                    text = set_top_level_key(text, "model_catalog_json", previous_catalog)
            parse_toml_text(text)
            atomic_write(paths.config, text.encode())
        catalog_removed = False
        catalog_restored = False
        if paths.catalog.is_file():
            original_backup = manifest.get("catalog_original_backup")
            if manifest.get("catalog_preexisted") and original_backup and Path(original_backup).is_file():
                atomic_write(paths.catalog, Path(original_backup).read_bytes())
                catalog_restored = True
            elif not manifest.get("catalog_preexisted"):
                paths.catalog.unlink()
                catalog_removed = True
        paths.manifest.unlink(missing_ok=True)
    except Exception:
        restore_backup(paths, backup)
        raise
    removed_credential = remove_credential_key() if remove_credential else False
    return result(
        "uninstalled",
        disabled=disabled,
        catalog_removed=catalog_removed,
        catalog_restored=catalog_restored,
        credential_removed=removed_credential,
    )


def try_acquire_file_lock(lock_file) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    if msvcrt is not None:
        lock_file.seek(0)
        if lock_file.read(1) == "":
            lock_file.seek(0)
            lock_file.write("\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    raise ManagerError("unsupported_platform", "当前平台没有可用的文件锁实现。")


def release_file_lock(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def operation_lock(paths: Paths, timeout_seconds: float = LOCK_WAIT_SECONDS):
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    with (paths.state_dir / "manager.lock").open("a+") as lock_file:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if try_acquire_file_lock(lock_file):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManagerError(
                    "operation_in_progress",
                    "另一个 DeepSeek 配置操作仍在进行，请稍后重试。",
                )
            time.sleep(min(0.1, remaining))
        try:
            yield
        finally:
            release_file_lock(lock_file)


def main() -> int:
    if sys.argv[1:] == ["_credential-get"]:
        try:
            secret = read_credential_key()
            if not secret:
                print("DeepSeek API Key 不存在。", file=sys.stderr)
                return 2
            sys.stdout.write(secret)
            return 0
        except ManagerError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "setup", "test", "repair", "disable", "uninstall"))
    parser.add_argument("--codex-home")
    parser.add_argument("--base-url", help="OpenAI 兼容 Base URL；省略时保留既有合法值或使用默认地址。")
    parser.add_argument("--api-key-stdin", action="store_true")
    parser.add_argument("--skip-live-test", action="store_true")
    parser.add_argument("--remove-credential", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    paths = resolve_paths(args.codex_home)
    try:
        explicit_base_url = (
            canonical_base_url(args.base_url) if args.base_url is not None else None
        )
        if args.command == "status":
            try:
                codex_bin = find_desktop_codex(paths, allow_stage=False)
            except ManagerError as exc:
                payload = static_status(paths)
                payload["checks"]["desktop_codex_detected"] = False
                payload["errors"].append(str(exc))
            else:
                payload = static_status(paths, codex_bin)
        else:
            with operation_lock(paths):
                if args.command in {"setup", "repair"}:
                    codex_bin = find_desktop_codex(paths, allow_stage=True)
                    payload = setup(
                        paths,
                        codex_bin,
                        args.api_key_stdin,
                        args.skip_live_test,
                        explicit_base_url,
                    )
                elif args.command == "test":
                    codex_bin = find_desktop_codex(paths, allow_stage=True)
                    payload = run_tests(paths, codex_bin)
                elif args.command == "disable":
                    payload = disable(paths)
                else:
                    payload = uninstall(paths, args.remove_credential)
        emit(payload, args.json)
        return 0 if payload["status"] not in {"partial", "credential_missing"} else 2
    except ManagerError as exc:
        emit(result(exc.code, message=str(exc), **exc.details), args.json)
        return 2
    except subprocess.TimeoutExpired:
        emit(result("timeout", message="操作超时，未输出任何凭据。"), args.json)
        return 3
    except Exception as exc:
        emit(result("failed", message=f"{type(exc).__name__}: {exc}"), args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

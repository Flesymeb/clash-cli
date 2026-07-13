"""Native clash runtime and subscription management."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import shutil
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

import httpx
import yaml

from clash_cli.api.client import ClashApiError, ClashClient
from clash_cli.config import Config

HELP_FLAGS = {"-h", "--help"}
URL_RE = re.compile(r"(?:https?|file)://[^\s'\"]+")
LEGACY_DIRECT_IP_RULES = {"DOMAIN,API64.IPIFY.ORG,DIRECT"}
HTML_RESPONSE_RE = re.compile(
    r"<\s*(?:!doctype|html|head|body|title)(?:[\s>]|$)",
    re.IGNORECASE,
)


class _QuotedString(str):
    pass


class _YamlDumper(yaml.SafeDumper):
    pass


def _represent_quoted_string(dumper: yaml.SafeDumper, value: _QuotedString) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style='"')


_YamlDumper.add_representer(_QuotedString, _represent_quoted_string)


class ClashctlError(RuntimeError):
    """Raised when the local clash runtime cannot be managed."""


@dataclass
class ClashctlResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def output(self) -> str:
        return "\n".join(part for part in [self.stdout, self.stderr] if part)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _prepare_yaml(value: Any, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        prepared = {key: _prepare_yaml(item, str(key)) for key, item in value.items()}
        if parent_key in {"reality-opts", "reality_opts"}:
            for key in ("short-id", "short_id"):
                if isinstance(prepared.get(key), str):
                    prepared[key] = _QuotedString(prepared[key])
        return prepared
    if isinstance(value, list):
        return [_prepare_yaml(item) for item in value]
    return value


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.dump(
            _prepare_yaml(data),
            f,
            Dumper=_YamlDumper,
            allow_unicode=True,
            sort_keys=False,
        )


def _tail_file(path: Path, lines: int = 40) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(errors="ignore").splitlines()[-lines:]).strip()


def mask_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme in ("http", "https") and parsed.netloc:
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        try:
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError:
            port = ""
        return f"{parsed.scheme}://{host}{port}/..."
    if parsed.scheme == "file":
        return "file://..."
    if not url:
        return ""
    return "***"


def _mask_sensitive_text(text: str) -> str:
    return URL_RE.sub(lambda match: mask_url(match.group(0)), text)


def _format_rows(title: str, rows: Sequence[tuple[str, str]]) -> str:
    width = max((len(name) for name, _value in rows), default=0)
    body = [f"{name:<{width}}  {value}" for name, value in rows]
    return "\n".join([title, *body])


def _process_uses_proxy(environ: bytes, proxy_url: str) -> bool:
    values: dict[str, str] = {}
    for entry in environ.split(b"\0"):
        if b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        values[key.decode(errors="ignore")] = value.decode(errors="ignore")
    expected = urlparse(proxy_url)
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        value = values.get(key, "")
        if not value:
            continue
        candidate = urlparse(value)
        if candidate.hostname == expected.hostname and candidate.port == expected.port:
            return True
    return False


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _deep_merge(merged.get(key), value)
        return merged
    return override if override is not None else base


class ClashctlBridge:
    """Native manager kept under the old name to avoid touching callers."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config.load()
        self.root = self.config.clashctl_dir.expanduser()
        self.resources = self.root / "resources"
        self.base_config = self.resources / "config.yaml"
        self.mixin_config = self.resources / "mixin.yaml"
        self.runtime_config = self.resources / "runtime.yaml"
        self.temp_config = self.resources / "temp.yaml"
        self.profiles_meta = self.resources / "profiles.yaml"
        self.profiles_dir = self.resources / "profiles"
        self.profiles_log = self.resources / "profiles.log"
        self.env = self._load_env()
        self.kernel_name = self.env.get("KERNEL_NAME", "mihomo")
        self.kernel = self.root / "bin" / self.kernel_name
        self.log_file = self.resources / f"{self.kernel_name}.log"
        self.subconverter_dir = self.root / "bin" / "subconverter"
        self.subconverter = self.subconverter_dir / "subconverter"
        self.subconverter_config = self.subconverter_dir / "pref.yml"
        self.subconverter_log = self.subconverter_dir / "latest.log"

    def _load_env(self) -> dict[str, str]:
        env_path = self.root / ".env"
        data: dict[str, str] = {}
        if not env_path.exists():
            return data
        for raw in env_path.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip().strip("\"'")
        return data

    def _ensure_runtime(self) -> None:
        if not self.root.exists():
            raise ClashctlError(f"clash runtime not found: {self.root}")
        if not self.kernel.exists():
            raise ClashctlError(f"kernel binary not found: {self.kernel}")
        self.resources.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

    async def clashctl(self, args: Sequence[str | int] = ()) -> ClashctlResult:
        cmd = str(args[0]) if args else ""
        rest = [str(arg) for arg in args[1:]]
        try:
            if cmd in ("", "-h", "--help"):
                return self._help()
            if any(arg in HELP_FLAGS for arg in rest):
                return self._command_help(cmd)
            if cmd in {"on", "off", "restart", "status", "ui"} and rest:
                return ClashctlResult(2, stderr=f"usage: ccli {cmd}")
            if cmd == "on":
                return await self.on()
            if cmd == "off":
                return await self.off()
            if cmd == "restart":
                return await self.restart()
            if cmd == "status":
                return await self.status()
            if cmd == "doctor":
                return await self.doctor()
            if cmd == "fallback":
                return await self.fallback(rest)
            if cmd == "ui":
                return self.ui()
            if cmd == "proxy":
                return self.proxy(rest)
            if cmd == "tun":
                return await self.tun(rest)
            if cmd == "mixin":
                return await self.mixin(rest)
            if cmd == "secret":
                return await self.secret(rest)
            if cmd == "sub":
                return await self.clashsub(rest)
            if cmd == "log":
                return self.log(rest)
            if cmd == "upgrade":
                return await self.upgrade(rest)
            return ClashctlResult(2, stderr=f"unknown clashctl command: {cmd}")
        except ClashctlError as e:
            return ClashctlResult(1, stderr=str(e))

    async def clashsub(self, args: Sequence[str | int] = ()) -> ClashctlResult:
        cmd = str(args[0]) if args else "ls"
        rest = [str(arg) for arg in args[1:]]
        try:
            if cmd in ("", "-h", "--help"):
                return self._sub_help()
            if any(arg in HELP_FLAGS for arg in rest):
                return self._sub_command_help(cmd)
            if cmd in ("ls", "list"):
                return self.sub_list()
            if cmd == "add":
                parsed = self._parse_sub_add_args(rest)
                if isinstance(parsed, ClashctlResult):
                    return parsed
                url, force_convert = parsed
                return await self.sub_add(url, force_convert=force_convert)
            if cmd in ("del", "delete", "rm"):
                if len(rest) != 1:
                    return ClashctlResult(2, stderr="usage: ccli sub del <id>")
                if rest[0].startswith("-"):
                    return ClashctlResult(2, stderr=f"unknown option for sub del: {rest[0]}")
                sub_id = self._parse_sub_id(rest[0], "usage: ccli sub del <id>")
                if isinstance(sub_id, ClashctlResult):
                    return sub_id
                return self.sub_del(sub_id)
            if cmd == "use":
                if len(rest) != 1:
                    return ClashctlResult(2, stderr="usage: ccli sub use <id>")
                if rest[0].startswith("-"):
                    return ClashctlResult(2, stderr=f"unknown option for sub use: {rest[0]}")
                sub_id = self._parse_sub_id(rest[0], "usage: ccli sub use <id>")
                if isinstance(sub_id, ClashctlResult):
                    return sub_id
                return await self.sub_use(sub_id)
            if cmd == "update":
                parsed = self._parse_sub_update_args(rest)
                if isinstance(parsed, ClashctlResult):
                    return parsed
                sub_id, force_convert, auto = parsed
                if auto:
                    return await self.sub_update_auto()
                return await self.sub_update(sub_id, force_convert=force_convert)
            if cmd == "log":
                if len(rest) > 1:
                    return ClashctlResult(2, stderr="usage: ccli sub log [lines]")
                if rest and not rest[0].isdigit():
                    return ClashctlResult(2, stderr="usage: ccli sub log [lines]")
                lines = int(rest[0]) if rest else 20
                return self.sub_log(lines)
            return ClashctlResult(2, stderr=f"unknown sub command: {cmd}")
        except ClashctlError as e:
            return ClashctlResult(1, stderr=str(e))

    def _help(self) -> ClashctlResult:
        return ClashctlResult(
            0,
            "Usage: ccli ctl COMMAND [OPTIONS]\n\n"
            "Commands:\n"
            "  on/off/restart/status/ui/log\n"
            "  doctor\n"
            "  fallback [status|list|set|on|off]\n"
            "  proxy [on|off]\n"
            "  tun [on|off]\n"
            "  mixin [-r|-c]\n"
            "  secret [new-secret]\n"
            "  sub <command>\n"
            "  upgrade",
        )

    def _command_help(self, cmd: str) -> ClashctlResult:
        help_text = {
            "on": "Usage: ccli on\n\nStart the local mihomo service.",
            "off": "Usage: ccli off\n\nStop the local mihomo service.",
            "restart": "Usage: ccli restart\n\nRestart the local mihomo service.",
            "status": "Usage: ccli ctl status\n\nShow the running mihomo process.",
            "doctor": "Usage: ccli doctor\n\nRun local runtime, config, API, subscription, shell, and converter checks.",
            "fallback": (
                "Usage: ccli fallback [status|list|on|off]\n"
                "       ccli fallback set <primary> <backup> [backup...]\n\n"
                "Configure ordered automatic failover nodes."
            ),
            "ui": "Usage: ccli ui\n\nPrint the local dashboard URL.",
            "proxy": "Usage: ccli proxy [on|off]\n\nToggle or show shell proxy integration state.",
            "tun": "Usage: ccli tun [on|off]\n\nToggle or show TUN mode in mixin.yaml.",
            "mixin": "Usage: ccli mixin [-r|-c]\n\nShow mixin, runtime, or base config.",
            "secret": "Usage: ccli secret [new-secret]\n\nShow or update the Clash API secret.",
            "log": "Usage: ccli log [lines]\n\nShow recent mihomo log lines.",
            "upgrade": "Usage: ccli upgrade [--release|--alpha]\n\nAsk the dashboard API to upgrade the core.",
            "sub": self._sub_help().stdout,
        }
        return ClashctlResult(0, help_text.get(cmd, f"unknown clashctl command: {cmd}"))

    def _sub_help(self) -> ClashctlResult:
        return ClashctlResult(
            0,
            "Usage: ccli sub COMMAND [OPTIONS]\n\n"
            "Commands:\n"
            "  add [--convert] <url>     Add a subscription\n"
            "  ls | list                 List subscriptions\n"
            "  del | rm <id>             Delete a subscription\n"
            "  use <id>                  Switch to a subscription\n"
            "  update [--convert] [id]   Update a subscription\n"
            "  update --auto             Install cron auto-update\n"
            "  log                       Show subscription log",
        )

    def _sub_command_help(self, cmd: str) -> ClashctlResult:
        help_text = {
            "add": "Usage: ccli sub add [--convert] <url>\n\nAdd a subscription. If <url> is omitted in a TTY, ccli prompts for it.",
            "ls": "Usage: ccli sub ls\n\nList subscriptions.",
            "list": "Usage: ccli sub list\n\nList subscriptions.",
            "del": "Usage: ccli sub del <id>\n\nDelete a subscription that is not currently active.",
            "delete": "Usage: ccli sub delete <id>\n\nDelete a subscription that is not currently active.",
            "rm": "Usage: ccli sub rm <id>\n\nDelete a subscription that is not currently active.",
            "use": "Usage: ccli sub use <id>\n\nSwitch to a subscription and restart mihomo if needed.",
            "update": "Usage: ccli sub update [--convert|--auto] [id]\n\nUpdate the selected subscription, or a specific id.",
            "log": "Usage: ccli sub log [lines]\n\nShow recent subscription log lines.",
        }
        return ClashctlResult(0, help_text.get(cmd, f"unknown sub command: {cmd}"))

    def _parse_sub_add_args(self, args: Sequence[str]) -> tuple[str, bool] | ClashctlResult:
        url = ""
        force_convert = False
        for arg in args:
            if arg == "--convert":
                force_convert = True
            elif arg.startswith("-"):
                return ClashctlResult(2, stderr=f"unknown option for sub add: {arg}")
            elif not url:
                url = arg
            else:
                return ClashctlResult(2, stderr=f"unexpected argument for sub add: {arg}")
        if not url:
            return ClashctlResult(2, stderr="usage: ccli sub add [--convert] <url>")
        return url, force_convert

    def _parse_sub_id(self, value: str, usage: str) -> int | ClashctlResult:
        try:
            sub_id = int(value)
        except ValueError:
            return ClashctlResult(2, stderr=f"{usage}\nerror: id must be a positive integer")
        if sub_id <= 0:
            return ClashctlResult(2, stderr=f"{usage}\nerror: id must be a positive integer")
        return sub_id

    def _parse_sub_update_args(self, args: Sequence[str]) -> tuple[int | None, bool, bool] | ClashctlResult:
        sub_id: int | None = None
        force_convert = False
        auto = False
        for arg in args:
            if arg == "--convert":
                force_convert = True
            elif arg == "--auto":
                auto = True
            elif arg.startswith("-"):
                return ClashctlResult(2, stderr=f"unknown option for sub update: {arg}")
            elif sub_id is None:
                parsed = self._parse_sub_id(arg, "usage: ccli sub update [--convert|--auto] [id]")
                if isinstance(parsed, ClashctlResult):
                    return parsed
                sub_id = parsed
            else:
                return ClashctlResult(2, stderr=f"unexpected argument for sub update: {arg}")
        if auto and (sub_id is not None or force_convert):
            return ClashctlResult(2, stderr="usage: ccli sub update --auto")
        return sub_id, force_convert, auto

    def _profiles(self) -> dict[str, Any]:
        data = _load_yaml(self.profiles_meta)
        data.setdefault("use", 0)
        data.setdefault("profiles", [])
        return data

    def _masked_profiles(self) -> dict[str, Any]:
        data = self._profiles()
        masked = {
            key: value for key, value in data.items()
            if key != "profiles"
        }
        masked["profiles"] = []
        for profile in data.get("profiles", []):
            if not isinstance(profile, dict):
                continue
            item = dict(profile)
            item["url"] = mask_url(str(item.get("url", "")))
            masked["profiles"].append(item)
        return masked

    def _save_profiles(self, data: dict[str, Any]) -> None:
        _write_yaml(self.profiles_meta, data)

    def _profile_by_id(self, sub_id: int) -> dict[str, Any]:
        for profile in self._profiles().get("profiles", []):
            if int(profile.get("id", -1)) == sub_id:
                return profile
        raise ClashctlError(f"subscription id not found: {sub_id}")

    def _log_sub(self, message: str) -> None:
        self.profiles_log.parent.mkdir(parents=True, exist_ok=True)
        with self.profiles_log.open("a") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")

    def _raw_subscription_path(self, dest: Path) -> Path:
        return dest.with_name(dest.name + ".raw")

    def _subconverter_port(self) -> int:
        data = _load_yaml(self.subconverter_config)
        try:
            return int(data.get("server", {}).get("port", 25500))
        except (TypeError, ValueError):
            return 25500

    def _set_subconverter_port(self, port: int) -> None:
        data = _load_yaml(self.subconverter_config)
        server = data.setdefault("server", {})
        if not isinstance(server, dict):
            server = {}
            data["server"] = server
        server["port"] = port
        _write_yaml(self.subconverter_config, data)

    def _port_in_use(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    def _find_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    async def _prepare_subconverter_port(self) -> int:
        port = self._subconverter_port()
        if await self._subconverter_ready(port):
            return port
        if self._port_in_use(port):
            port = self._find_free_port()
            self._set_subconverter_port(port)
        return port

    def _validate_subscription_source(self, url: str) -> None:
        if url.startswith("file://"):
            return
        scheme = urlparse(url).scheme
        if scheme not in ("http", "https"):
            raise ClashctlError("subscription URL must start with http://, https://, or file://")

    def _normalize_subscription(self, dest: Path) -> None:
        try:
            payload = dest.read_bytes()
        except OSError as e:
            raise ClashctlError(f"subscription response cannot be read: {dest}") from e
        if not payload.strip():
            raise ClashctlError("subscription response is empty")

        text: str | None = None
        for encoding in ("utf-8-sig", "gb18030", "gbk", "big5"):
            try:
                text = payload.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ClashctlError("subscription response is not valid UTF-8, GB18030, GBK, or BIG5 text")

        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            raise ClashctlError("subscription response is empty")
        dest.write_text(text, encoding="utf-8")

    def _validate_subscription_content(self, dest: Path) -> None:
        text = dest.read_text(encoding="utf-8")
        if HTML_RESPONSE_RE.search(text[:8192]):
            raise ClashctlError(
                "subscription response looks like HTML; check the URL or subscription User-Agent"
            )
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            raise ClashctlError(f"subscription response is invalid YAML: {e}") from e
        if not isinstance(data, dict):
            raise ClashctlError("subscription config must be a YAML mapping")

        proxies = data.get("proxies", [])
        providers = data.get("proxy-providers", {})
        proxy_count = len(proxies) if isinstance(proxies, list) else 0
        provider_count = len(providers) if isinstance(providers, dict) else 0
        if proxy_count + provider_count == 0:
            raise ClashctlError("subscription config does not contain any proxy nodes or providers")

    def _prepare_subscription_content(self, dest: Path) -> None:
        self._normalize_subscription(dest)
        text = dest.read_text(encoding="utf-8")
        if HTML_RESPONSE_RE.search(text[:8192]):
            raise ClashctlError(
                "subscription response looks like HTML; check the URL or subscription User-Agent"
            )

    async def _validate_subscription_config(self, dest: Path) -> None:
        await self._valid_config(dest)
        self._validate_subscription_content(dest)

    async def _download_raw_subscription(self, url: str, dest: Path) -> None:
        self._validate_subscription_source(url)
        headers = {"User-Agent": self.env.get("CLASH_SUB_UA", "clash-verge/v2.4.0")}
        dest.parent.mkdir(parents=True, exist_ok=True)
        if url.startswith("file://"):
            src = Path(url[7:]).expanduser()
            try:
                dest.write_bytes(src.read_bytes())
            except OSError as e:
                raise ClashctlError(f"subscription file cannot be read: {src}") from e
            return
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            try:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                detail = _mask_sensitive_text(str(e))
                raise ClashctlError(f"subscription download failed: {detail}") from e
        dest.write_bytes(resp.content)

    async def _subconverter_ready(self, port: int) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
                resp = await client.get(f"http://127.0.0.1:{port}/version")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def _start_subconverter(self, port: int) -> asyncio.subprocess.Process | None:
        if await self._subconverter_ready(port):
            return None
        if not self.subconverter.exists():
            raise ClashctlError(f"subconverter binary not found: {self.subconverter}")

        self.subconverter_log.parent.mkdir(parents=True, exist_ok=True)
        with self.subconverter_log.open("ab") as log:
            proc = await asyncio.create_subprocess_exec(
                str(self.subconverter),
                cwd=str(self.subconverter_dir),
                stdout=log,
                stderr=log,
            )

        for _ in range(15):
            await asyncio.sleep(0.2)
            if await self._subconverter_ready(port):
                return proc
            if proc.returncode is not None:
                break
        await self._stop_subconverter(proc)
        raise ClashctlError(f"subconverter did not start, check log: {self.subconverter_log}")

    async def _stop_subconverter(self, proc: asyncio.subprocess.Process | None) -> None:
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def _download_converted_subscription(self, url: str, dest: Path) -> None:
        self._validate_subscription_source(url)
        if url.startswith("file://"):
            raise ClashctlError("cannot convert local file subscription")

        headers = {"User-Agent": self.env.get("CLASH_SUB_UA", "clash-verge/v2.4.0")}
        port = await self._prepare_subconverter_port()
        proc = await self._start_subconverter(port)
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False) as client:
                try:
                    resp = await client.get(
                        f"http://127.0.0.1:{port}/sub",
                        params={"target": "clash", "url": url},
                        headers=headers,
                    )
                    resp.raise_for_status()
                except httpx.HTTPError as e:
                    detail = _mask_sensitive_text(str(e))
                    raise ClashctlError(f"subscription conversion failed: {detail}") from e
            dest.write_bytes(resp.content)
        finally:
            await self._stop_subconverter(proc)

    async def _download_subscription(self, url: str, dest: Path, *, force_convert: bool = False) -> None:
        raw_path = self._raw_subscription_path(dest)
        raw_error_text = ""
        if force_convert:
            await self._download_converted_subscription(url, dest)
            self._prepare_subscription_content(dest)
            await self._validate_subscription_config(dest)
            return

        await self._download_raw_subscription(url, dest)
        self._prepare_subscription_content(dest)
        try:
            await self._validate_subscription_config(dest)
            if raw_path.exists():
                raw_path.unlink()
            return
        except ClashctlError as e:
            raw_error_text = str(e)
            raw_path.write_bytes(dest.read_bytes())
            if url.startswith("file://"):
                raise ClashctlError(f"subscription is not a valid Clash config: {raw_error_text}") from e

        try:
            await self._download_converted_subscription(url, dest)
            self._prepare_subscription_content(dest)
            await self._validate_subscription_config(dest)
        except ClashctlError as convert_error:
            raise ClashctlError(
                "subscription is not a valid Clash config after conversion\n"
                f"raw config: {raw_path}\n"
                f"converted config: {dest}\n"
                f"converter log: {self.subconverter_log}\n"
                f"raw error: {raw_error_text}\n"
                f"conversion error: {convert_error}"
            ) from convert_error

    async def _run_process(self, args: Sequence[str | Path]) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *(str(arg) for arg in args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )

    async def _run_process_input(self, args: Sequence[str | Path], stdin: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *(str(arg) for arg in args),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(stdin.encode())
        return (
            proc.returncode,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )

    async def _valid_config(self, path: Path) -> None:
        self._ensure_runtime()
        code, out, err = await self._run_process([self.kernel, "-d", path.parent, "-f", path, "-t"])
        if code != 0:
            raise ClashctlError(err or out or f"invalid config: {path}")

    async def merge_config(self) -> None:
        base = _load_yaml(self.base_config)
        mixin = _load_yaml(self.mixin_config)
        custom = mixin.pop("_custom", None)
        rules_mixin = mixin.pop("rules", {}) if isinstance(mixin.get("rules"), dict) else {}
        proxies_mixin = mixin.pop("proxies", {}) if isinstance(mixin.get("proxies"), dict) else {}
        groups_mixin = mixin.pop("proxy-groups", {}) if isinstance(mixin.get("proxy-groups"), dict) else {}

        runtime = _deep_merge(base, mixin)
        if custom is not None:
            mixin["_custom"] = custom

        combined_rules = (
            list(rules_mixin.get("prefix", []) or [])
            + list(base.get("rules", []) or [])
            + list(rules_mixin.get("suffix", []) or [])
        )
        runtime["rules"] = [
            rule for rule in combined_rules
            if not self._is_terminal_match(rule) and not self._is_legacy_direct_ip_rule(rule)
        ]
        runtime["rules"].append(f"MATCH,{self.config.selector_group}")
        runtime["proxies"] = self._merge_named_list(base.get("proxies", []), proxies_mixin)
        runtime["proxy-groups"] = self._merge_groups(base.get("proxy-groups", []), groups_mixin)
        self._apply_fallback_group(runtime, custom)
        self._ensure_rule_targets(runtime)

        temp = self.runtime_config.with_suffix(".tmp")
        _write_yaml(temp, runtime)
        await self._valid_config(temp)
        temp.replace(self.runtime_config)

    @staticmethod
    def _is_terminal_match(rule: Any) -> bool:
        return isinstance(rule, str) and rule.strip().upper().startswith("MATCH,")

    @staticmethod
    def _is_legacy_direct_ip_rule(rule: Any) -> bool:
        return isinstance(rule, str) and rule.strip().upper() in LEGACY_DIRECT_IP_RULES

    def _apply_fallback_group(self, runtime: dict[str, Any], custom: Any) -> None:
        if not isinstance(custom, dict):
            return
        settings = custom.get("fallback", {})
        if not isinstance(settings, dict) or not settings.get("enable"):
            return
        nodes = self._fallback_nodes(settings)
        if len(nodes) < 2:
            raise ClashctlError("fallback requires at least two nodes")
        name = str(settings.get("name", "ccli-fallback"))
        group = {
            "name": name,
            "type": "fallback",
            "proxies": nodes,
            "url": str(settings.get("url", "https://www.gstatic.com/generate_204")),
            "interval": self._fallback_int(settings, "interval", 60),
            "lazy": True,
            "timeout": self._fallback_int(settings, "timeout", 5000),
            "max-failed-times": self._fallback_int(settings, "max-failed-times", 2),
        }
        groups = runtime.setdefault("proxy-groups", [])
        groups[:] = [item for item in groups if not isinstance(item, dict) or item.get("name") != name]
        groups.insert(0, group)

    @staticmethod
    def _fallback_int(settings: dict[str, Any], key: str, default: int) -> int:
        try:
            value = int(settings.get(key, default))
        except (TypeError, ValueError) as e:
            raise ClashctlError(f"fallback {key} must be an integer") from e
        if value <= 0:
            raise ClashctlError(f"fallback {key} must be positive")
        return value

    @staticmethod
    def _fallback_nodes(settings: dict[str, Any]) -> list[str]:
        raw_nodes = settings.get("nodes", [])
        if not isinstance(raw_nodes, list):
            raise ClashctlError("fallback nodes must be a list")
        return list(dict.fromkeys(str(node) for node in raw_nodes if node))

    def _merge_named_list(self, base_items: list[dict[str, Any]], mixin: dict[str, Any]) -> list[dict[str, Any]]:
        overrides = {item.get("name"): item for item in mixin.get("override", []) or []}
        merged = [overrides.get(item.get("name"), item) for item in base_items]
        return list(mixin.get("prefix", []) or []) + merged + list(mixin.get("suffix", []) or [])

    def _merge_groups(self, base_items: list[dict[str, Any]], mixin: dict[str, Any]) -> list[dict[str, Any]]:
        merged = self._merge_named_list(base_items, mixin)
        inject = mixin.get("inject", {}) or {}
        for group in merged:
            extra = inject.get(group.get("name"), [])
            if not extra:
                continue
            existing = list(group.get("proxies", []) or [])
            for item in extra:
                if item not in existing:
                    existing.append(item)
            group["proxies"] = existing
        return merged

    def _ensure_rule_targets(self, runtime: dict[str, Any]) -> None:
        groups = runtime.setdefault("proxy-groups", [])
        proxies = runtime.get("proxies", []) or []
        proxy_names = {item.get("name") for item in proxies if isinstance(item, dict)}
        group_names = {item.get("name") for item in groups if isinstance(item, dict)}
        builtins = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "GLOBAL"}

        source = next(
            (
                group for group in groups
                if isinstance(group, dict)
                and group.get("type") == "select"
                and group.get("proxies")
            ),
            None,
        )
        if not source:
            return

        policy_names = group_names | proxy_names | builtins
        missing_targets: list[str] = []
        for rule in runtime.get("rules", []) or []:
            if not isinstance(rule, str) or "," not in rule:
                continue
            target = rule.rsplit(",", 1)[1].strip()
            if target and target not in policy_names and target not in missing_targets:
                missing_targets.append(target)

        source_proxies = list(source.get("proxies", []) or [])
        for target in missing_targets:
            groups.append(
                {
                    "name": target,
                    "type": "select",
                    "proxies": source_proxies,
                }
            )
            group_names.add(target)
            policy_names.add(target)

    async def sub_add(self, url: str, *, force_convert: bool = False) -> ClashctlResult:
        url = url.strip()
        if not url:
            return ClashctlResult(2, stderr="subscription URL is empty")

        data = self._profiles()
        for profile in data.get("profiles", []):
            if profile.get("url") == url:
                return ClashctlResult(1, stderr="subscription URL already exists")
        next_id = max([int(p.get("id", 0)) for p in data.get("profiles", [])] or [0]) + 1
        profile_path = self.profiles_dir / f"{next_id}.yaml"
        await self._download_subscription(url, self.temp_config, force_convert=force_convert)
        self.temp_config.replace(profile_path)
        data["profiles"].append({"id": next_id, "path": str(profile_path), "url": url})
        self._save_profiles(data)
        masked_url = mask_url(url)
        self._log_sub(f"added subscription [{next_id}] {masked_url}")
        return ClashctlResult(0, f"subscription added: [{next_id}] {masked_url}")

    def sub_del(self, sub_id: int) -> ClashctlResult:
        data = self._profiles()
        if int(data.get("use", 0)) == sub_id:
            return ClashctlResult(1, stderr=f"subscription {sub_id} is currently in use")
        profile = self._profile_by_id(sub_id)
        path = Path(profile.get("path", "")).expanduser()
        if path.exists():
            path.unlink()
        data["profiles"] = [p for p in data.get("profiles", []) if int(p.get("id", -1)) != sub_id]
        self._save_profiles(data)
        self._log_sub(f"deleted subscription [{sub_id}] {mask_url(str(profile.get('url', '')))}")
        return ClashctlResult(0, f"subscription deleted: [{sub_id}]")

    def sub_list(self) -> ClashctlResult:
        return ClashctlResult(0, yaml.safe_dump(self._masked_profiles(), allow_unicode=True, sort_keys=False).strip())

    async def sub_use(self, sub_id: int) -> ClashctlResult:
        profile = self._profile_by_id(sub_id)
        path = Path(profile.get("path", "")).expanduser()
        if not path.exists():
            raise ClashctlError(f"profile file not found: {path}")
        self.base_config.write_bytes(path.read_bytes())
        await self.merge_config()
        data = self._profiles()
        data["use"] = sub_id
        self._save_profiles(data)
        await self.restart_if_running()
        self._log_sub(f"selected subscription [{sub_id}] {mask_url(str(profile.get('url', '')))}")
        return ClashctlResult(0, f"subscription selected: [{sub_id}]")

    async def sub_update(self, sub_id: int | None = None, *, force_convert: bool = False) -> ClashctlResult:
        data = self._profiles()
        use_id = sub_id or int(data.get("use", 0) or 0)
        profile = self._profile_by_id(use_id)
        url = profile.get("url", "")
        if not url:
            return ClashctlResult(1, stderr=f"subscription {use_id} has empty URL")
        await self._download_subscription(url, self.temp_config, force_convert=force_convert)
        profile_path = Path(profile.get("path", "")).expanduser()
        profile_path.write_bytes(self.temp_config.read_bytes())
        self._log_sub(f"updated subscription [{use_id}] {mask_url(str(url))}")
        if int(data.get("use", 0)) == use_id:
            return await self.sub_use(use_id)
        return ClashctlResult(0, f"subscription updated: [{use_id}]")

    async def sub_update_auto(self) -> ClashctlResult:
        crontab = shutil.which("crontab")
        if not crontab:
            return ClashctlResult(1, stderr="crontab command not found; install cron first")

        shell = os.environ.get("SHELL", "/bin/sh")
        line = f"0 0 */2 * * {shell} -i -c 'ccli sub update'"
        code, out, err = await self._run_process([crontab, "-l"])
        current = out if code == 0 else ""
        if line in current or "ccli sub update" in current:
            return ClashctlResult(0, "subscription auto-update already configured")

        new_crontab = (current.rstrip() + "\n" if current.strip() else "") + line + "\n"
        code, out, err = await self._run_process_input([crontab, "-"], new_crontab)
        if code != 0:
            return ClashctlResult(1, stderr=err or out or "failed to install subscription auto-update")
        return ClashctlResult(0, "subscription auto-update configured")

    def sub_log(self, lines: int = 20) -> ClashctlResult:
        if not self.profiles_log.exists():
            return ClashctlResult(0, "")
        text = "\n".join(self.profiles_log.read_text(errors="ignore").splitlines()[-lines:])
        return ClashctlResult(0, _mask_sensitive_text(text))

    def _fallback_config(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        mixin = _load_yaml(self.mixin_config)
        custom = mixin.setdefault("_custom", {})
        if not isinstance(custom, dict):
            custom = {}
            mixin["_custom"] = custom
        settings = custom.setdefault("fallback", {})
        if not isinstance(settings, dict):
            settings = {}
            custom["fallback"] = settings
        return mixin, custom, settings

    @staticmethod
    def _configured_selector_group(mixin: dict[str, Any]) -> str:
        custom = mixin.get("_custom", {})
        if isinstance(custom, dict) and custom.get("selector-group"):
            return str(custom["selector-group"])
        rules = mixin.get("rules", {})
        suffix = rules.get("suffix", []) if isinstance(rules, dict) else []
        for rule in reversed(suffix or []):
            if isinstance(rule, str) and rule.strip().upper().startswith("MATCH,"):
                return rule.strip().split(",", 2)[1].strip()
        return "节点选择"

    def _available_policy_names(self) -> set[str]:
        base = _load_yaml(self.base_config)
        names = {
            str(item.get("name"))
            for key in ("proxies", "proxy-groups")
            for item in (base.get(key, []) or [])
            if isinstance(item, dict) and item.get("name")
        }
        return names

    async def _reload_after_fallback_change(self) -> None:
        self.config = Config.load(self.root)
        await self.merge_config()
        if self._pid():
            await self.restart()

    async def fallback(self, args: Sequence[str]) -> ClashctlResult:
        command = args[0] if args else "status"
        if (command == "status" and len(args) == 1) or not args:
            return await self.fallback_status()
        if command in ("list", "ls", "candidates") and len(args) == 1:
            return await self.fallback_candidates()
        if command == "set":
            nodes = list(dict.fromkeys(args[1:]))
            if len(nodes) < 2:
                return ClashctlResult(
                    2,
                    stderr="usage: ccli fallback set <primary> <backup> [backup...]",
                )
            missing = [node for node in nodes if node not in self._available_policy_names()]
            if missing:
                return ClashctlResult(1, stderr=f"unknown fallback node: {', '.join(missing)}")
            mixin, custom, settings = self._fallback_config()
            custom.setdefault("selector-group", self._configured_selector_group(mixin))
            settings.update(
                {
                    "enable": True,
                    "name": "ccli-fallback",
                    "nodes": nodes,
                    "url": "https://www.gstatic.com/generate_204",
                    "interval": 60,
                    "timeout": 5000,
                    "max-failed-times": 2,
                }
            )
            _write_yaml(self.mixin_config, mixin)
            await self._reload_after_fallback_change()
            return await self.fallback_status()
        if command in ("on", "off") and len(args) == 1:
            mixin, custom, settings = self._fallback_config()
            nodes = self._fallback_nodes(settings)
            if command == "on" and len(nodes) < 2:
                return ClashctlResult(
                    1,
                    stderr="fallback is not configured; run ccli fallback set <primary> <backup> [backup...]",
                )
            custom.setdefault("selector-group", self._configured_selector_group(mixin))
            settings["enable"] = command == "on"
            _write_yaml(self.mixin_config, mixin)
            await self._reload_after_fallback_change()
            return await self.fallback_status()
        return ClashctlResult(
            2,
            stderr=(
                "usage: ccli fallback [status|list|on|off]\n"
                "       ccli fallback set <primary> <backup> [backup...]"
            ),
        )

    async def fallback_status(self) -> ClashctlResult:
        _mixin, _custom, settings = self._fallback_config()
        nodes = self._fallback_nodes(settings)
        enabled = bool(settings.get("enable")) and len(nodes) >= 2
        name = str(settings.get("name", "ccli-fallback"))
        active = "-"
        api = "offline" if not self._pid() else "not checked"
        if enabled and self._pid() and self.config.secret:
            client = ClashClient(self.config.api_base, self.config.secret)
            try:
                active = await client.get_current_node(name)
                api = "ok"
            except ClashApiError as e:
                api = str(e)
        rows = [
            ("fallback", "on" if enabled else "off"),
            ("group", name),
            ("active", active),
            ("order", " -> ".join(nodes) if nodes else "not configured"),
            ("check", str(settings.get("url", "https://www.gstatic.com/generate_204"))),
            ("interval", f"{self._fallback_int(settings, 'interval', 60)}s"),
            ("api", api),
        ]
        return ClashctlResult(0, _format_rows("clash-cli fallback", rows))

    async def fallback_candidates(self) -> ClashctlResult:
        mixin, _custom, _settings = self._fallback_config()
        selector = self._configured_selector_group(mixin)
        names: list[str] = []
        if self._pid() and self.config.secret:
            client = ClashClient(self.config.api_base, self.config.secret)
            try:
                names = (await client.get_group(selector)).nodes
            except ClashApiError:
                pass
        if not names:
            base = _load_yaml(self.base_config)
            names = [
                str(item.get("name"))
                for item in (base.get("proxies", []) or [])
                if isinstance(item, dict) and item.get("name")
            ]
        if not names:
            return ClashctlResult(1, stderr=f"no fallback candidates found in {selector!r}")
        return ClashctlResult(0, "\n".join([f"Fallback candidates ({selector})", *names]))

    async def status(self) -> ClashctlResult:
        pid = self._pid()
        if not pid:
            return ClashctlResult(1, stderr=f"{self.kernel_name} is not running")
        return ClashctlResult(0, f"{pid} {self.kernel} -d {self.resources} -f {self.runtime_config}")

    async def doctor(self) -> ClashctlResult:
        lines = ["ccli doctor"]
        failures = 0

        def add(level: str, name: str, detail: str) -> None:
            nonlocal failures
            if level == "FAIL":
                failures += 1
            lines.append(f"{level:<4} {name:<12} {detail}")

        add("OK" if self.root.exists() else "FAIL", "runtime", str(self.root))
        add("OK" if self.resources.exists() else "FAIL", "resources", str(self.resources))
        if self.kernel.exists():
            executable = os.access(self.kernel, os.X_OK)
            add("OK" if executable else "FAIL", "kernel", str(self.kernel))
        else:
            add("FAIL", "kernel", f"missing: {self.kernel}")

        config = Config.load(self.root)
        if config.config_errors:
            for error in config.config_errors:
                add("FAIL", "config", error)
        else:
            add("OK", "config", "mixin.yaml and profiles.yaml parsed")

        pid = self._pid()
        if pid:
            add("OK", "service", f"{self.kernel_name} running pid {pid}")
        else:
            add("WARN", "service", f"{self.kernel_name} is not running")

        if config.secret:
            add("OK", "secret", "configured")
        else:
            add("FAIL", "secret", "missing Clash API secret")

        if pid and config.secret:
            client = ClashClient(config.api_base, config.secret)
            try:
                current = await client.get_current_node(config.selector_group)
                add("OK", "api", f"{config.api_base} current={current}")
            except ClashApiError as e:
                add("FAIL", "api", str(e))
        elif config.api_base:
            add("WARN", "api", f"not checked: {config.api_base}")

        if config.subscriptions:
            current_ids = {sub.id for sub in config.subscriptions}
            if config.current_sub_id in current_ids:
                add("OK", "sub", f"{len(config.subscriptions)} profiles, current #{config.current_sub_id}")
            else:
                add("FAIL", "sub", f"current subscription #{config.current_sub_id} is not in profiles")
        else:
            add("WARN", "sub", "no subscriptions configured")

        http_proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or ""
        if http_proxy == config.proxy_url:
            add("OK", "shell", f"http_proxy={http_proxy}")
        elif http_proxy:
            add("WARN", "shell", f"http_proxy={http_proxy}, expected {config.proxy_url}")
        else:
            add("WARN", "shell", f"proxy env not set; run eval \"$(ccli env)\"")

        codex_total, codex_proxied, codex_unreadable = self._codex_proxy_processes(config.proxy_url)
        codex_missing = codex_total - codex_proxied - codex_unreadable
        if codex_total == 0:
            add("WARN", "codex", "no running Codex process found")
        elif codex_missing:
            try:
                tun = _load_yaml(self.mixin_config).get("tun", {})
            except (OSError, yaml.YAMLError):
                tun = {}
            tun_on = isinstance(tun, dict) and bool(tun.get("enable"))
            suffix = "; TUN is on" if tun_on else "; restart those Codex sessions"
            add(
                "WARN",
                "codex",
                f"{codex_missing}/{codex_total} processes missing proxy env{suffix}",
            )
        elif codex_unreadable:
            add("WARN", "codex", f"{codex_unreadable}/{codex_total} process environments unreadable")
        else:
            add("OK", "codex", f"{codex_proxied}/{codex_total} processes use {config.proxy_url}")

        if self.subconverter.exists():
            port = self._subconverter_port()
            if await self._subconverter_ready(port):
                add("OK", "converter", f"{self.subconverter} ready on {port}")
            elif self._port_in_use(port):
                add("WARN", "converter", f"port {port} is occupied; conversion will choose a free port")
            else:
                add("OK", "converter", f"{self.subconverter} port {port}")
        else:
            add("WARN", "converter", f"missing: {self.subconverter}")

        return ClashctlResult(1 if failures else 0, "\n".join(lines))

    def _pid(self) -> int | None:
        try:
            pids = os.listdir("/proc")
        except OSError:
            return None
        for pid in pids:
            if not pid.isdigit():
                continue
            cmdline = Path("/proc") / pid / "cmdline"
            try:
                text = cmdline.read_text(errors="ignore").replace("\x00", " ")
            except OSError:
                continue
            if str(self.kernel) in text:
                return int(pid)
        return None

    def _codex_proxy_processes(self, proxy_url: str) -> tuple[int, int, int]:
        total = proxied = unreadable = 0
        proc_root = Path("/proc")
        if not proc_root.exists():
            return total, proxied, unreadable
        for proc_dir in proc_root.iterdir():
            if not proc_dir.name.isdigit():
                continue
            try:
                comm = (proc_dir / "comm").read_text(errors="ignore").strip().lower()
            except OSError:
                continue
            if not comm.startswith("codex"):
                continue
            total += 1
            try:
                environ = (proc_dir / "environ").read_bytes()
            except OSError:
                unreadable += 1
                continue
            if _process_uses_proxy(environ, proxy_url):
                proxied += 1
        return total, proxied, unreadable

    async def _runtime_summary(self, proxy: str, pid: int | None, note: str = "") -> str:
        config = Config.load(self.root)
        node = "unknown"
        api = config.api_base
        if pid and config.secret:
            client = ClashClient(config.api_base, config.secret)
            try:
                node = await client.get_current_node(config.selector_group)
            except ClashApiError as e:
                api = f"{config.api_base} ({e})"
        elif not pid:
            node = "-"
            api = "offline"
        elif not config.secret:
            api = f"{config.api_base} (secret missing)"

        shell_proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or ""
        if proxy == "on":
            endpoint = config.proxy_url
            if shell_proxy == config.proxy_url:
                shell = "on"
            elif shell_proxy:
                shell = f"other ({shell_proxy})"
            else:
                shell = "not applied"
        else:
            endpoint = "disabled"
            shell = "off" if not shell_proxy else f"still set ({shell_proxy})"

        service = f"{self.kernel_name} pid {pid}" if pid else f"{self.kernel_name} stopped"
        rows = [
            ("proxy", proxy),
            ("selected", node),
            ("group", config.selector_group),
            ("service", service),
            ("endpoint", endpoint),
            ("api", api),
            ("shell", shell),
        ]
        if config.current_sub_id:
            rows.append(("sub", f"#{config.current_sub_id}"))
        if note:
            rows.append(("state", note))
        return _format_rows("clash-cli", rows)

    async def on(self) -> ClashctlResult:
        self._ensure_runtime()
        pid = self._pid()
        if pid:
            return ClashctlResult(0, await self._runtime_summary("on", pid, "already running"))
        await self.merge_config()
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("ab") as log:
            proc = await asyncio.create_subprocess_exec(
                str(self.kernel),
                "-d",
                str(self.resources),
                "-f",
                str(self.runtime_config),
                stdout=log,
                stderr=log,
                start_new_session=True,
            )

        try:
            await asyncio.wait_for(proc.wait(), timeout=0.8)
        except asyncio.TimeoutError:
            pass
        else:
            tail = _tail_file(self.log_file)
            detail = f"\n{tail}" if tail else ""
            return ClashctlResult(
                1,
                stderr=f"{self.kernel_name} exited immediately with code {proc.returncode}{detail}",
            )

        pid = self._pid() or proc.pid
        return ClashctlResult(0, await self._runtime_summary("on", pid, "started"))

    async def off(self) -> ClashctlResult:
        pid = self._pid()
        if not pid:
            return ClashctlResult(0, await self._runtime_summary("off", None, "already stopped"))
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return ClashctlResult(0, await self._runtime_summary("off", None, "stopped"))
        except PermissionError as e:
            raise ClashctlError(f"permission denied stopping {self.kernel_name} pid {pid}") from e
        if not await self._wait_until_stopped(pid, timeout=1.0):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as e:
                raise ClashctlError(f"permission denied killing {self.kernel_name} pid {pid}") from e
            if not await self._wait_until_stopped(pid, timeout=1.0):
                raise ClashctlError(f"{self.kernel_name} pid {pid} did not stop")
        return ClashctlResult(0, await self._runtime_summary("off", None, "stopped"))

    async def _wait_until_stopped(self, pid: int, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._pid() != pid:
                return True
            await asyncio.sleep(0.05)
        return self._pid() != pid

    async def restart(self) -> ClashctlResult:
        await self.off()
        return await self.on()

    async def restart_if_running(self) -> None:
        if self._pid():
            result = await self.restart()
            if result.returncode != 0:
                raise ClashctlError(result.stderr or result.stdout or f"failed to restart {self.kernel_name}")

    def ui(self) -> ClashctlResult:
        cfg = _load_yaml(self.runtime_config) or _load_yaml(self.mixin_config)
        controller = cfg.get("external-controller", "127.0.0.1:9090")
        host, _, port = str(controller).partition(":")
        if host in ("0.0.0.0", "*", ""):
            host = "127.0.0.1"
        return ClashctlResult(0, f"Dashboard: http://{host}:{port or '9090'}/ui")

    def proxy(self, args: Sequence[str]) -> ClashctlResult:
        mixin = _load_yaml(self.mixin_config)
        custom = mixin.setdefault("_custom", {})
        system_proxy = custom.setdefault("system-proxy", {})
        if len(args) > 1:
            return ClashctlResult(2, stderr="usage: ccli proxy [on|off]")
        if args and args[0] not in ("on", "off"):
            return ClashctlResult(2, stderr="usage: ccli proxy [on|off]")
        if args and args[0] == "on":
            system_proxy["enable"] = True
            _write_yaml(self.mixin_config, mixin)
            return ClashctlResult(0, 'proxy flag enabled; run eval "$(ccli env)" in this shell')
        if args and args[0] == "off":
            system_proxy["enable"] = False
            _write_yaml(self.mixin_config, mixin)
            return ClashctlResult(0, 'proxy flag disabled; run eval "$(ccli env --unset)" in this shell')
        state = "on" if system_proxy.get("enable") else "off"
        return ClashctlResult(0, f"proxy flag: {state}")

    async def tun(self, args: Sequence[str]) -> ClashctlResult:
        mixin = _load_yaml(self.mixin_config)
        tun = mixin.setdefault("tun", {})
        if not isinstance(tun, dict):
            tun = {}
            mixin["tun"] = tun
        if len(args) > 1:
            return ClashctlResult(2, stderr="usage: ccli tun [on|off]")
        if args and args[0] not in ("on", "off"):
            return ClashctlResult(2, stderr="usage: ccli tun [on|off]")
        if args and args[0] in ("on", "off"):
            enabled = args[0] == "on"
            previous = bool(tun.get("enable"))
            if enabled == previous:
                return ClashctlResult(0, f"tun: {'on' if enabled else 'off'}")
            if enabled and not self._kernel_has_tun_capability():
                raise ClashctlError(
                    f"{self.kernel_name} lacks CAP_NET_ADMIN; run: "
                    f"sudo setcap cap_net_admin,cap_net_bind_service=+ep {self.kernel}"
                )

            was_running = bool(self._pid())
            tun["enable"] = enabled
            try:
                _write_yaml(self.mixin_config, mixin)
                await self.merge_config()
                await self.restart_if_running()
            except ClashctlError as change_error:
                tun["enable"] = previous
                _write_yaml(self.mixin_config, mixin)
                restore_error = ""
                try:
                    await self.merge_config()
                    if was_running and not self._pid():
                        restored = await self.on()
                        if restored.returncode != 0:
                            restore_error = restored.stderr or restored.stdout
                except ClashctlError as e:
                    restore_error = str(e)
                if restore_error:
                    raise ClashctlError(
                        f"{change_error}; rollback failed: {restore_error}"
                    ) from change_error
                raise
        return ClashctlResult(0, f"tun: {'on' if tun.get('enable') else 'off'}")

    def _kernel_has_tun_capability(self) -> bool:
        if os.geteuid() == 0:
            return True
        getcap = shutil.which("getcap")
        if not getcap or not self.kernel.exists():
            return False
        try:
            result = subprocess.run(
                [getcap, str(self.kernel)],
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and "cap_net_admin" in result.stdout.lower()

    async def mixin(self, args: Sequence[str]) -> ClashctlResult:
        if len(args) > 1:
            return ClashctlResult(2, stderr="usage: ccli mixin [-r|-c]")
        if args and args[0] == "-r":
            return ClashctlResult(0, self.runtime_config.read_text(errors="ignore"))
        if args and args[0] == "-c":
            return ClashctlResult(0, self.base_config.read_text(errors="ignore"))
        if args:
            return ClashctlResult(2, stderr="usage: ccli mixin [-r|-c]")
        return ClashctlResult(0, self.mixin_config.read_text(errors="ignore"))

    async def secret(self, args: Sequence[str]) -> ClashctlResult:
        mixin = _load_yaml(self.mixin_config)
        if not args:
            return ClashctlResult(0, f"secret: {mixin.get('secret', '')}")
        if len(args) > 1 or args[0].startswith("-"):
            return ClashctlResult(2, stderr="usage: ccli secret [new-secret]")
        mixin["secret"] = args[0]
        _write_yaml(self.mixin_config, mixin)
        await self.merge_config()
        await self.restart_if_running()
        return ClashctlResult(0, "secret updated")

    def log(self, args: Sequence[str]) -> ClashctlResult:
        lines = 120
        if len(args) > 1:
            return ClashctlResult(2, stderr="usage: ccli log [lines]")
        if args:
            if not args[0].isdigit():
                return ClashctlResult(2, stderr="usage: ccli log [lines]")
            lines = int(args[0])
        if not self.log_file.exists():
            return ClashctlResult(0, "")
        return ClashctlResult(0, "\n".join(self.log_file.read_text(errors="ignore").splitlines()[-lines:]))

    async def upgrade(self, args: Sequence[str]) -> ClashctlResult:
        channel = ""
        unknown = [arg for arg in args if arg not in ("--alpha", "-a", "--release", "-r")]
        alpha = "--alpha" in args or "-a" in args
        release = "--release" in args or "-r" in args
        if unknown or (alpha and release):
            return ClashctlResult(2, stderr="usage: ccli upgrade [--release|--alpha]")
        if alpha:
            channel = "alpha"
        elif release:
            channel = "release"
        config = Config.load(self.root)
        headers = {"Authorization": f"Bearer {config.secret}"}
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                resp = await client.post(f"{config.api_base}/upgrade", params={"channel": channel}, headers=headers)
                return ClashctlResult(0 if resp.is_success else 1, resp.text)
        except httpx.HTTPError as e:
            return ClashctlResult(1, stderr=str(e))

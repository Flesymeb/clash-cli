"""Configuration loader for clash-cli."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

_GB = 2**30

# Default direct/bypass list: loopback + RFC1918 LAN. Override via mixin
# `_custom.system-proxy.ignore-hosts` (wildcard form "*.domain" — also valid
# for GNOME/Chromium proxy bypass rules).
DEFAULT_NO_PROXY_HOSTS = [
    "localhost", "127.0.0.0/8", "::1",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "*.local",
]


@dataclass(frozen=True)
class TrafficInfo:
    """Parsed ``Subscription-Userinfo`` header from a subscription provider."""

    upload: int = 0
    download: int = 0
    total: int = 0
    expire: int = 0  # unix seconds; 0 when unknown

    @classmethod
    def parse(cls, raw: str | None) -> TrafficInfo | None:
        if not raw:
            return None
        values: dict[str, int] = {}
        for part in raw.split(";"):
            key, sep, value = part.strip().partition("=")
            if not sep:
                continue
            try:
                values[key.strip().lower()] = int(value.strip())
            except ValueError:
                continue
        if not values:
            return None
        return cls(
            upload=values.get("upload", 0),
            download=values.get("download", 0),
            total=values.get("total", 0),
            expire=values.get("expire", 0),
        )

    @property
    def used(self) -> int:
        return self.upload + self.download

    @property
    def used_gb(self) -> float:
        return self.used / _GB

    @property
    def total_gb(self) -> float | None:
        return self.total / _GB if self.total > 0 else None

    @property
    def remaining_gb(self) -> float | None:
        if self.total <= 0:
            return None
        return max(self.total - self.used, 0) / _GB

    @property
    def percent_used(self) -> int | None:
        if self.total <= 0:
            return None
        return min(round(self.used * 100 / self.total), 100)

    @property
    def expire_date(self) -> str | None:
        if self.expire <= 0:
            return None
        return datetime.fromtimestamp(self.expire).strftime("%Y-%m-%d")

    @property
    def days_left(self) -> int | None:
        if self.expire <= 0:
            return None
        return max((self.expire - int(time.time())) // 86400, 0)


@dataclass
class Subscription:
    id: int
    path: str
    url: str
    is_current: bool = False


@dataclass
class Config:
    api_base: str = "http://127.0.0.1:9090"
    secret: str = ""
    proxy_url: str = "http://127.0.0.1:7890"
    selector_group: str = "节点选择"
    clashctl_dir: Path = field(default_factory=lambda: Path.home() / "clashctl")
    subscriptions: list[Subscription] = field(default_factory=list)
    current_sub_id: int = 0
    traffic: TrafficInfo | None = None
    no_proxy_hosts: list[str] = field(default_factory=lambda: list(DEFAULT_NO_PROXY_HOSTS))
    config_errors: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, clashctl_dir: Path | None = None) -> Config:
        cfg = cls()

        # Env overrides
        cfg.api_base = os.environ.get("CLASH_API", cfg.api_base)
        cfg.secret = os.environ.get("CLASH_SECRET", cfg.secret)
        cfg.proxy_url = os.environ.get("CLASH_PROXY", cfg.proxy_url)

        env_home = os.environ.get("CLASH_HOME")
        if clashctl_dir is not None:
            cfg.clashctl_dir = clashctl_dir.expanduser()
        elif env_home:
            cfg.clashctl_dir = Path(env_home).expanduser()
        else:
            # Prefer the installed runtime. The repository's resources/ folder
            # is only a development fallback and may not contain the live secret.
            candidates = [Path.home() / "clashctl", Path.home() / ".clashctl"]
            cwd_resources = Path.cwd() / "resources"
            if cwd_resources.exists():
                candidates.append(Path.cwd())
            for d in candidates:
                if d.exists():
                    cfg.clashctl_dir = d
                    break

        resources = cfg.clashctl_dir / "resources"

        # Read mixin.yaml for secret and port
        mixin_path = resources / "mixin.yaml"
        if mixin_path.exists():
            try:
                with open(mixin_path) as f:
                    mixin = yaml.safe_load(f) or {}
                if not isinstance(mixin, dict):
                    raise ValueError("expected a YAML mapping")
                if not cfg.secret:
                    cfg.secret = mixin.get("secret", "")
                port = mixin.get("mixed-port", 7890)
                cfg.proxy_url = f"http://127.0.0.1:{port}"
                ctrl = mixin.get("external-controller", "127.0.0.1:9090")
                if not ctrl.startswith("http"):
                    ctrl = f"http://{ctrl}"
                cfg.api_base = ctrl
                rules = mixin.get("rules", {})
                suffix = rules.get("suffix", []) if isinstance(rules, dict) else []
                for rule in reversed(suffix or []):
                    if isinstance(rule, str) and rule.strip().upper().startswith("MATCH,"):
                        cfg.selector_group = rule.strip().split(",", 2)[1].strip()
                        break
                custom = mixin.get("_custom", {})
                if isinstance(custom, dict):
                    if custom.get("selector-group"):
                        cfg.selector_group = str(custom["selector-group"])
                    system_proxy = custom.get("system-proxy", {})
                    if isinstance(system_proxy, dict):
                        hosts = system_proxy.get("ignore-hosts")
                        if (
                            isinstance(hosts, list)
                            and hosts
                            and all(isinstance(h, str) and h.strip() for h in hosts)
                        ):
                            cfg.no_proxy_hosts = [h.strip() for h in hosts]
                    fallback = custom.get("fallback", {})
                    fallback_nodes = fallback.get("nodes", []) if isinstance(fallback, dict) else []
                    if (
                        isinstance(fallback, dict)
                        and fallback.get("enable")
                        and isinstance(fallback_nodes, list)
                        and len(fallback_nodes) >= 2
                    ):
                        cfg.selector_group = str(fallback.get("name", "ccli-fallback"))
            except Exception as e:
                cfg.config_errors.append(f"{mixin_path}: {e}")

        # Read profiles.yaml for subscriptions
        profiles_path = resources / "profiles.yaml"
        if profiles_path.exists():
            try:
                with open(profiles_path) as f:
                    data = yaml.safe_load(f) or {}
                if not isinstance(data, dict):
                    raise ValueError("expected a YAML mapping")
                cfg.current_sub_id = data.get("use", 0)
                for p in data.get("profiles", []):
                    if not isinstance(p, dict):
                        raise ValueError("profiles entries must be mappings")
                    cfg.subscriptions.append(
                        Subscription(
                            id=p.get("id", 0),
                            path=p.get("path", ""),
                            url=p.get("url", ""),
                            is_current=p.get("id") == cfg.current_sub_id,
                        )
                    )
                    if p.get("id") == cfg.current_sub_id and p.get("traffic"):
                        cfg.traffic = TrafficInfo.parse(str(p["traffic"]))
            except Exception as e:
                cfg.config_errors.append(f"{profiles_path}: {e}")

        return cfg

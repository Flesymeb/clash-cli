"""Main Textual TUI application for clash-cli."""

from __future__ import annotations

import asyncio
import os
import random
import re
import sys
from pathlib import Path

from clash_cli import __version__
from clash_cli.api.client import ClashClient, ClashApiError
from clash_cli.api.models import Node, UnlockStatus
from clash_cli.checker.unlock import check_all as check_unlock
from clash_cli.clashctl import ClashctlBridge, ClashctlError, ClashctlResult, mask_url
from clash_cli.config import Config
from clash_cli.scanner.scanner import full_scan, quick_scan


COLOR_OK = "green"
COLOR_BLOCKED = "red"
COLOR_FAIL = "yellow"
COLOR_DIM = "dim"
COLOR_CYAN = "cyan"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CLASHCTL_COMMANDS = (
    "on", "off", "restart", "doctor", "fallback", "ui", "log",
    "proxy", "tun", "mixin", "secret", "upgrade",
)
# Pseudo-entries that are not real switchable proxy nodes.
NODE_SKIP_NAMES = frozenset({
    "DIRECT", "REJECT", "PASS", "REJECT-DROP",
    "自动节点", "故障节点", "故障转移", "自动选择",
})


def clean_output(text: str) -> str:
    return ANSI_RE.sub("", text).strip()


def last_output_line(result: ClashctlResult) -> str:
    output = clean_output(result.output)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def format_rows(title: str, rows: list[tuple[str, str]]) -> str:
    width = max((len(name) for name, _value in rows), default=0)
    body = [f"{name:>{width}}  {value}" for name, value in rows]
    return "\n".join([title, *body])


def format_delay_plain(delay: int) -> str:
    return f"{delay}ms" if delay > 0 else "timeout"


def proxy_exports(config: Config) -> str:
    proxy = config.proxy_url
    no_proxy = "localhost,127.0.0.1,::1"
    return "\n".join(
        [
            f"export http_proxy={sh_quote(proxy)}",
            f"export https_proxy={sh_quote(proxy)}",
            f"export HTTP_PROXY={sh_quote(proxy)}",
            f"export HTTPS_PROXY={sh_quote(proxy)}",
            f"export all_proxy={sh_quote(proxy)}",
            f"export ALL_PROXY={sh_quote(proxy)}",
            f"export no_proxy={sh_quote(no_proxy)}",
            f"export NO_PROXY={sh_quote(no_proxy)}",
        ]
    )


def proxy_unsets() -> str:
    names = [
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
    ]
    return "unset " + " ".join(names)


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def term_style(text: str, code: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def color_unlock(val: str) -> str:
    if val.startswith("ok"):
        return f"[{COLOR_OK}]✓ {val[3:]}[/]"
    if val.startswith("blocked"):
        return f"[{COLOR_BLOCKED}]✗ {val[8:]}[/]"
    if val == "fail":
        return f"[{COLOR_FAIL}]?[/]"
    return f"[{COLOR_DIM}]···[/]"


def color_delay(ms: int | None) -> str:
    if ms is None:
        return f"[{COLOR_DIM}]···[/]"
    if ms < 0:
        return f"[{COLOR_BLOCKED}]timeout[/]"
    if ms < 500:
        return f"[{COLOR_OK}]{ms}ms[/]"
    if ms < 1500:
        return f"[{COLOR_FAIL}]{ms}ms[/]"
    return f"[{COLOR_BLOCKED}]{ms}ms[/]"


async def resolve_switch_target(client: ClashClient, group_name: str, requested: str | None) -> tuple[str, str]:
    group = await client.get_group(group_name)
    if group.group_type.lower() == "fallback":
        raise RuntimeError("fallback controls node selection; run ccli fallback off before manual switching")
    if requested:
        return requested, "manual"
    candidates = [name for name in group.nodes if name != group.current]
    if not candidates:
        candidates = list(group.nodes)
    if not candidates:
        raise RuntimeError(f"no nodes available in group {group_name!r}")
    return random.choice(candidates), "random"


def main():
    import argparse

    raw_args = sys.argv[1:]
    if raw_args:
        command, rest = raw_args[0], raw_args[1:]
        if command == "sub":
            asyncio.run(cli_sub(rest))
            return
        if command == "ctl":
            asyncio.run(cli_ctl(rest))
            return
        if command in CLASHCTL_COMMANDS:
            asyncio.run(cli_ctl([command, *rest]))
            return

    parser = argparse.ArgumentParser(prog="ccli", description="Clash proxy node manager")
    parser.add_argument("--version", action="version", version=f"ccli {__version__}")
    sub = parser.add_subparsers(dest="command")
    scan_parser = sub.add_parser("scan", help="Scan nodes for AI unlock")
    scan_parser.add_argument("--full", action="store_true", help="Full scan (all nodes)")
    status_parser = sub.add_parser("status", help="Show current node and unlock status")
    status_parser.add_argument("--no-check", action="store_true", help="Skip unlock checks")
    status_parser.add_argument("--timeout", type=float, default=10.0, help="Unlock check timeout in seconds")
    switch_parser = sub.add_parser("switch", help="Switch to a node, or random node if omitted")
    switch_parser.add_argument("node", nargs="?", help="Node name. Omit to choose a random node.")
    watch_parser = sub.add_parser("watch", help="Auto-watch and switch on failure")
    watch_parser.add_argument(
        "--interval", type=int, default=120,
        help="Check interval in seconds (default: 120)",
    )
    watch_parser.add_argument(
        "--once", action="store_true",
        help="Run one check and exit",
    )
    env_parser = sub.add_parser("env", help="Print shell proxy exports")
    env_parser.add_argument("--unset", action="store_true", help="Print commands to unset proxy variables")
    shell_status_parser = sub.add_parser("shell-status", help="Print concise shell proxy status")
    shell_status_parser.add_argument("--proxy", choices=("on", "off"), default="on")
    shell_init_parser = sub.add_parser("shell-init", help="Print or install shell integration")
    shell_init_parser.add_argument("shell", nargs="?", choices=("bash", "zsh", "fish"), help="Shell type")
    shell_init_parser.add_argument("--install", action="store_true", help="Install shell integration into rc files")
    sub_parser = sub.add_parser("sub", help="Manage subscriptions via clashsub", add_help=False)
    sub_parser.add_argument("sub_args", nargs=argparse.REMAINDER)
    ctl_parser = sub.add_parser("ctl", help="Manage the local clash runtime", add_help=False)
    ctl_parser.add_argument("ctl_args", nargs=argparse.REMAINDER)
    for name in CLASHCTL_COMMANDS:
        ctl_shortcut = sub.add_parser(name, help=f"Run ctl {name}", add_help=False)
        ctl_shortcut.add_argument("ctl_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.command is None:
        from clash_cli.tui import ClashApp
        ClashApp().run()
    elif args.command == "status":
        asyncio.run(cli_status(no_check=args.no_check, timeout=args.timeout))
    elif args.command == "scan":
        asyncio.run(cli_scan(full=args.full))
    elif args.command == "switch":
        asyncio.run(cli_switch(args.node))
    elif args.command == "watch":
        asyncio.run(cli_watch(args.interval, args.once))
    elif args.command == "env":
        cli_env(unset=args.unset)
    elif args.command == "shell-status":
        asyncio.run(cli_shell_status(args.proxy))
    elif args.command == "shell-init":
        cli_shell_init(args.shell, install=args.install)
    elif args.command == "sub":
        asyncio.run(cli_sub(args.sub_args))
    elif args.command == "ctl":
        asyncio.run(cli_ctl(args.ctl_args))
    elif args.command in CLASHCTL_COMMANDS:
        asyncio.run(cli_ctl([args.command, *args.ctl_args]))


async def cli_status(no_check: bool = False, timeout: float = 10.0):
    config = Config.load()
    _check_config(config)
    client = ClashClient(config.api_base, config.secret)
    try:
        chain = await client.get_proxy_chain(config.selector_group)
        current = chain[-1]
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    rows = [
        ("group", config.selector_group),
        ("selected", current),
        ("route", " -> ".join(chain)),
        ("proxy", config.proxy_url),
    ]
    if no_check:
        rows.append(("unlock", "skipped"))
    else:
        check_timeout = max(timeout, 0.1)
        try:
            unlock = await asyncio.wait_for(check_unlock(config.proxy_url), timeout=check_timeout)
            rows.extend(
                [
                    ("Claude region", unlock.claude),
                    ("ChatGPT region", unlock.chatgpt),
                    ("Gemini region", unlock.gemini),
                ]
            )
        except asyncio.TimeoutError:
            rows.append(("unlock", f"timeout after {check_timeout:g}s"))
        except Exception as e:
            rows.append(("unlock", f"error: {e}"))
    print(format_rows("clash-cli status", rows))


async def cli_scan(full: bool = False):
    config = Config.load()
    _check_config(config)
    client = ClashClient(config.api_base, config.secret)
    try:
        if full:
            result = await full_scan(client, config.proxy_url, group_name=config.selector_group)
        else:
            result = await quick_scan(client, config.proxy_url, group_name=config.selector_group)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    for node in result.nodes:
        marker = " → " if node.name == (result.best and result.best.name) else "   "
        d = f"{node.delay}ms" if node.delay and node.delay > 0 else "timeout"
        print(f"{marker}{node.name:<20}  {d:<8}  Claude:{node.unlock.claude}  ChatGPT:{node.unlock.chatgpt}  Gemini:{node.unlock.gemini}")
    if result.best:
        print(f"\nFound: {result.best.name} ({result.best.delay}ms, Claude and ChatGPT available)")
    else:
        print("\nNo fully unlocked node found")


async def cli_switch(node: str | None = None):
    config = Config.load()
    _check_config(config)
    client = ClashClient(config.api_base, config.secret)
    try:
        target, mode = await resolve_switch_target(client, config.selector_group, node)
    except Exception as e:
        print(f"Failed to choose node: {e}", file=sys.stderr)
        sys.exit(1)

    ok = await client.switch_node(config.selector_group, target)
    if ok:
        await asyncio.sleep(0.3)
        delay = await client.test_delay(target)
        rows = [
            ("mode", mode),
            ("group", config.selector_group),
            ("selected", target),
            ("latency", format_delay_plain(delay)),
        ]
        try:
            unlock = await asyncio.wait_for(check_unlock(config.proxy_url), timeout=10)
            rows.extend(
                [
                    ("Claude region", unlock.claude),
                    ("ChatGPT region", unlock.chatgpt),
                    ("Gemini region", unlock.gemini),
                ]
            )
        except asyncio.TimeoutError:
            rows.append(("unlock", "timeout after 10s"))
        except Exception as e:
            rows.append(("unlock", f"error: {e}"))
        print(format_rows("clash-cli switch", rows))
    else:
        detail = f": {client.last_error}" if client.last_error else ""
        print(f"Failed to switch to: {target}{detail}", file=sys.stderr)
        sys.exit(1)


def cli_env(unset: bool = False) -> None:
    if unset:
        print(proxy_unsets())
        return
    config = Config.load()
    print(proxy_exports(config))


async def cli_shell_status(proxy: str = "on") -> None:
    config = Config.load()
    selected = "unknown"
    chain = [config.selector_group]
    api = "ok"
    try:
        client = ClashClient(config.api_base, config.secret)
        chain = await client.get_proxy_chain(config.selector_group)
        selected = chain[-1]
    except Exception as e:
        api = f"error: {e}"

    endpoint = config.proxy_url if proxy == "on" else "disabled"
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        from rich.console import Console
        from rich.table import Table
        from rich.text import Text

        console = Console()
        status = Text(proxy, style="bold green" if proxy == "on" else "bold red")
        title = Text("clash-cli", style="bold")
        title.append("  ")
        title.append("proxy ", style="dim")
        title.append(status)

        table = Table.grid(padding=(0, 2))
        table.add_column(justify="right", style="dim")
        table.add_column(style="white")
        table.add_column(style="dim")
        table.add_row("proxy", endpoint, f"api {api}")
        table.add_row("selected", Text(selected, style="cyan bold"), "")
        table.add_row("route", " -> ".join(chain), f"sub #{config.current_sub_id}")

        console.print(title)
        console.print(table)
        return

    print(f"clash-cli  proxy {proxy}")
    print(f"{'proxy':>6}  {endpoint}  api {api}")
    print(f"{'selected':>8}  {selected}")
    route = " -> ".join(chain) if api == "ok" else config.selector_group
    print(f"{'route':>8}  {route}  sub #{config.current_sub_id}")


def cli_shell_init(shell: str | None = None, install: bool = False) -> None:
    shell_name = shell or Path(os.environ.get("SHELL", "bash")).name
    script = shell_init_script(shell_name)
    if install:
        install_shell_init(shell_name, script)
        return
    print(script)


def shell_init_script(shell_name: str) -> str:
    if shell_name == "fish":
        return """function ccli
    switch $argv[1]
        case on off proxy
            set -l sub $argv[1]
            set -l out (command ccli ctl $sub --shell-integration $argv[2..-1])
            set -l rc $status
            eval $out
            return $rc
        case '*'
            command ccli $argv
    end
end"""

    return """ccli() {
  case "$1" in
    on|off|proxy)
      local out rc sub
      sub="$1"
      shift
      out="$(command ccli ctl "$sub" --shell-integration "$@")"
      rc=$?
      eval "$out"
      return "$rc"
      ;;
    *)
      command ccli "$@"
      ;;
  esac
}"""


def install_shell_init(shell_name: str, script: str) -> None:
    home = Path.home()
    targets: list[Path]
    if shell_name == "fish":
        targets = [home / ".config" / "fish" / "conf.d" / "ccli.fish"]
    elif shell_name == "zsh":
        targets = [home / ".zshrc"]
    elif shell_name == "bash":
        targets = [home / ".bashrc"]
    else:
        targets = [home / ".bashrc"]

    start = "# ccli START"
    end = "# ccli END"
    block = f"\n{start}\n# Load ccli shell integration\n{script}\n{end}\n"
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        old = target.read_text(errors="ignore") if target.exists() else ""
        old = re.sub(r"\n?# ccli START.*?# ccli END\n?", "\n", old, flags=re.S)
        target.write_text(old.rstrip() + block)
        print(f"installed shell integration: {target}")


def _print_clashctl_result(result: ClashctlResult) -> None:
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        sys.exit(result.returncode)


def _shell_integration_exports(args: Sequence[str], config: Config) -> str:
    """Shell statements the caller should eval after a ctl on/off/proxy run.

    Returns ``""`` when the command does not change the proxy environment, so
    the shell wrapper can always ``eval`` the output safely.
    """
    cmd = args[0] if args else ""
    if cmd == "on":
        return proxy_exports(config)
    if cmd == "off":
        return proxy_unsets()
    if cmd == "proxy":
        sub = args[1] if len(args) > 1 else ""
        if sub == "on":
            return proxy_exports(config)
        if sub == "off":
            return proxy_unsets()
    return ""


def _read_subscription_url() -> str:
    if sys.stdin.isatty():
        print("subscription url: ", end="", file=sys.stderr, flush=True)
        try:
            return sys.stdin.readline().strip()
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return ""

    text = sys.stdin.read()
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _sub_add_needs_prompt(args: list[str]) -> bool:
    if not args or args[0] != "add":
        return False
    if any(arg in ("-h", "--help") for arg in args[1:]):
        return False

    has_url = False
    for arg in args[1:]:
        if arg == "--convert":
            continue
        if arg.startswith("-"):
            return False
        has_url = True
    return not has_url


async def cli_sub(args: list[str]):
    if _sub_add_needs_prompt(args):
        url = _read_subscription_url()
        if not url:
            _print_clashctl_result(ClashctlResult(2, stderr="subscription URL is empty"))
        args = [*args, url]

    config = Config.load()
    bridge = ClashctlBridge(config)
    try:
        result = await bridge.clashsub(args)
    except ClashctlError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    _print_clashctl_result(result)


async def cli_ctl(args: list[str]):
    shell_integration = False
    cleaned: list[str] = []
    for arg in args:
        if arg == "--shell-integration":
            shell_integration = True
        else:
            cleaned.append(arg)
    args = cleaned
    config = Config.load()
    bridge = ClashctlBridge(config)
    try:
        if args and args[0] == "restart":
            result = await bridge.restart()
        else:
            result = await bridge.clashctl(args)
    except ClashctlError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not shell_integration:
        _print_clashctl_result(result)
        return

    # Shell-integration mode: print eval-able env statements to stdout (the
    # caller captures and evals them) and the human-readable status to stderr
    # (it flows straight to the terminal). Collapses on/off/proxy from three
    # ccli invocations to one.
    exports = _shell_integration_exports(args, config)
    always_apply = args[:1] == ["off"] or args[:2] == ["proxy", "off"]
    if exports and (result.returncode == 0 or always_apply):
        print(exports)
    if result.stdout:
        print(result.stdout, file=sys.stderr)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        sys.exit(result.returncode)


async def cli_watch(interval: int = 120, once: bool = False):
    """Background daemon: periodically check unlock and auto-switch on failure."""
    config = Config.load()
    _check_config(config)
    client = ClashClient(config.api_base, config.secret)

    async def check_and_fix() -> bool:
        """Return True if node is ok, False if we had to switch."""
        try:
            current = await client.get_current_node(config.selector_group)
            unlock = await check_unlock(config.proxy_url)
            if unlock.primary_ok:
                print(f"[ok] {current} Claude:{unlock.claude} ChatGPT:{unlock.chatgpt} Gemini:{unlock.gemini}")
                return True
            print(f"[FAIL] {current} Claude:{unlock.claude} ChatGPT:{unlock.chatgpt} Gemini:{unlock.gemini}")
        except ClashApiError as e:
            # API 不可达 → 多半 mihomo 挂了,尝试自愈拉起,不继续 scan(API 都不通 scan 也无意义)
            print(f"[ERR] API unreachable: {e}")
            try:
                bridge = ClashctlBridge(config)
                r = await bridge.on()
                print(f"  [heal] mihomo start attempted: {last_output_line(r)}")
            except ClashctlError as ce:
                print(f"  [heal] mihomo start failed: {ce}")
            except Exception as ce:
                print(f"  [heal] mihomo start failed unexpectedly: {ce}")
            return False
        except Exception as e:
            print(f"[ERR] {e}")

        print("  Scanning for working node...")
        try:
            result = await quick_scan(client, config.proxy_url, group_name=config.selector_group)
            if result.best:
                print(f"  [OK] Switched to: {result.best.name} ({result.best.delay}ms)")
                return True
            else:
                print("  [FAIL] No working node found")
                return False
        except Exception as e:
            print(f"  [ERR] Scan failed: {e}")
            return False

    if once:
        ok = await check_and_fix()
        sys.exit(0 if ok else 1)

    print(f"Auto-watch started (interval: {interval}s). Press Ctrl+C to stop.")
    while True:
        await check_and_fix()
        await asyncio.sleep(interval)


def _check_config(config: Config) -> None:
    """Ensure config has a valid secret, exit with message otherwise."""
    for error in config.config_errors:
        print(f"Config warning: {error}", file=sys.stderr)
    if not config.secret:
        print("Error: Clash API secret not set.", file=sys.stderr)
        print("Run: clashsecret <your-secret>", file=sys.stderr)
        print("Or set CLASH_SECRET environment variable.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

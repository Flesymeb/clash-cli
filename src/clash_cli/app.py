"""Main Textual TUI application for clash-cli."""

from __future__ import annotations

import asyncio
import os
import random
import re
import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    ListItem,
    ListView,
    ProgressBar,
    Static,
)

from clash_cli.api.client import ClashClient
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
CONTROL_ACTIONS = [
    ("Kernel status", "ctl status", "Show running mihomo/clash process", ["status"]),
    ("Fallback status", "fallback", "Show ordered automatic failover", ["fallback"]),
    ("Web dashboard", "ctl ui", "Print local and public dashboard URLs", ["ui"]),
    ("Proxy status", "ctl proxy", "Show shell proxy variables", ["proxy"]),
    ("Start service", "ctl on", "Start kernel service and proxy environment", ["on"]),
    ("Stop service", "ctl off", "Stop kernel service", ["off"]),
    ("Restart service", "ctl restart", "Restart kernel service", ["restart"]),
]


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


# ── Main Dashboard ─────────────────────────────────────────────

class MenuItem(ListItem):
    def __init__(self, key: str, title: str, detail: str, action: str) -> None:
        super().__init__()
        self.action_name = action
        self._key = key
        self._title = title
        self._detail = detail

    def compose(self) -> ComposeResult:
        yield Static(f" {self._key.upper():<2} {self._title:<18} {self._detail}", markup=False)


class MainScreen(Screen):
    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select", "Select"),
        Binding("s", "quick_scan", "Quick Scan"),
        Binding("f", "full_scan", "Full Scan"),
        Binding("l", "node_list", "Node List"),
        Binding("u", "subscriptions", "Subscriptions"),
        Binding("c", "control", "Control"),
        Binding("w", "toggle_watch", "Auto Watch"),
        Binding("r", "refresh", "Refresh"),
        Binding("h", "show_help", "Help"),
        Binding("question_mark", "show_help", "Help", show=False),
    ]

    WATCH_INTERVAL = 120  # seconds between health checks

    def __init__(self, config: Config, client: ClashClient) -> None:
        super().__init__()
        self.config = config
        self.client = client
        self._watching = False
        self._watch_timer: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="dashboard"):
            yield Static("", id="status-box")
            yield ListView(
                MenuItem("s", "Quick scan", "Find a working node", "quick_scan"),
                MenuItem("f", "Full scan", "Test every node", "full_scan"),
                MenuItem("l", "Nodes", "Switch proxy node", "node_list"),
                MenuItem("u", "Subscriptions", "Use or update profiles", "subscriptions"),
                MenuItem("c", "Control", "Service and proxy tools", "control"),
                id="main-menu",
            )
            yield Static("", id="watch-line")
        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_status()
        self.query_one("#main-menu", ListView).focus()
        self._update_watch_line()

    def _update_watch_line(self) -> None:
        line = self.query_one("#watch-line", Static)
        if self._watching:
            line.update(f"  [bold yellow]⏳ Auto-watch ON[/] (every {self.WATCH_INTERVAL}s)  [dim]press w to toggle[/]")
        else:
            line.update(f"  [dim]Auto-watch OFF  press w to enable[/]")

    async def _watch_loop(self) -> None:
        """Background task: periodically check unlock and auto-switch."""
        while self._watching:
            await asyncio.sleep(self.WATCH_INTERVAL)
            if not self._watching:
                break
            try:
                unlock = await check_unlock(self.config.proxy_url)
                if not unlock.all_ok:
                    self.notify(
                        f"Node failed! Claude:{unlock.claude} ChatGPT:{unlock.chatgpt} Gemini:{unlock.gemini}",
                        severity="error",
                    )
                    self.notify("Auto-scanning for working node...", severity="warning", timeout=5)
                    result = await quick_scan(
                        self.client, self.config.proxy_url,
                        group_name=self.config.selector_group,
                    )
                    if result.best:
                        self.notify(
                            f"Auto-switched to: {result.best.name} ({result.best.delay}ms)",
                            severity="information",
                            timeout=5,
                        )
                    else:
                        self.notify("No working node found!", severity="error", timeout=10)
            except Exception:
                pass
            await self.refresh_status()

    def action_toggle_watch(self) -> None:
        self._watching = not self._watching
        if self._watching:
            self._watch_timer = asyncio.create_task(self._watch_loop())
        self._update_watch_line()
        self.notify(
            f"Auto-watch {'ON' if self._watching else 'OFF'}",
            severity="information",
            timeout=2,
        )

    async def refresh_status(self) -> None:
        try:
            current = await self.client.get_current_node(self.config.selector_group)
            unlock = await check_unlock(self.config.proxy_url)
        except Exception as e:
            current = f"Error: {e}"
            unlock = UnlockStatus()

        status = self.query_one("#status-box", Static)
        status.update(
            f"[bold]clash-cli[/bold]  "
            f"group=[{COLOR_CYAN}]{self.config.selector_group}[/{COLOR_CYAN}]  "
            f"node=[{COLOR_CYAN}]{current}[/{COLOR_CYAN}]  "
            f"sub=[{COLOR_CYAN}]#{self.config.current_sub_id}[/{COLOR_CYAN}]\n"
            f"proxy=[{COLOR_CYAN}]{self.config.proxy_url}[/{COLOR_CYAN}]  "
            f"api=[{COLOR_CYAN}]{self.config.api_base}[/{COLOR_CYAN}]\n"
            f"Service regions  Claude: {color_unlock(unlock.claude)}  "
            f"ChatGPT: {color_unlock(unlock.chatgpt)}  "
            f"Gemini: {color_unlock(unlock.gemini)}"
        )

    def action_show_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_select(self) -> None:
        menu = self.query_one("#main-menu", ListView)
        if menu.index is not None and menu.index < len(menu.children):
            item = menu.children[menu.index]
            if hasattr(item, "action_name"):
                action = item.action_name
                getattr(self, f"action_{action}", lambda: None)()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if hasattr(event.item, "action_name"):
            getattr(self, f"action_{event.item.action_name}", lambda: None)()

    def action_quick_scan(self) -> None:
        self.app.push_screen(ScanScreen(self.config, self.client, mode="quick"))

    def action_full_scan(self) -> None:
        self.app.push_screen(ScanScreen(self.config, self.client, mode="full"))

    def action_node_list(self) -> None:
        self.app.push_screen(NodeListScreen(self.config, self.client))

    def action_subscriptions(self) -> None:
        self.app.push_screen(SubScreen(self.config, self.client))

    def action_control(self) -> None:
        self.app.push_screen(ControlScreen(self.config))

    async def action_refresh(self) -> None:
        self.config = Config.load()
        await self.refresh_status()


# ── Scan Progress Screen ───────────────────────────────────────

class ScanScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
    ]

    def __init__(self, config: Config, client: ClashClient, mode: str = "quick") -> None:
        super().__init__()
        self.config = config
        self.client = client
        self.mode = mode

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="scan-area"):
            yield Static(
                f"[bold]  {'Quick Scan' if self.mode == 'quick' else 'Full Scan'}[/bold]",
                id="scan-title",
            )
            yield Static("  Starting...", id="scan-log")
            yield ProgressBar(total=100, id="scan-progress")
        yield Footer()

    async def on_mount(self) -> None:
        asyncio.create_task(self.run_scan())

    async def run_scan(self) -> None:
        log = self.query_one("#scan-log", Static)
        progress = self.query_one("#scan-progress", ProgressBar)

        try:
            if self.mode == "quick":
                result = await quick_scan(
                    self.client,
                    self.config.proxy_url,
                    group_name=self.config.selector_group,
                )
            else:
                result = await full_scan(
                    self.client,
                    self.config.proxy_url,
                    group_name=self.config.selector_group,
                )
        except Exception as e:
            log.update(f"[red]  Error: {e}[/]")
            return

        progress.progress = 100

        lines = []
        for node in result.nodes:
            marker = " → " if node.name == (result.best and result.best.name) else "   "
            lines.append(
                f"{marker}[bold]{node.name:<20}[/bold]  "
                f"{color_delay(node.delay)}  "
                f"Claude:{color_unlock(node.unlock.claude)}  "
                f"ChatGPT:{color_unlock(node.unlock.chatgpt)}  "
                f"Gemini:{color_unlock(node.unlock.gemini)}"
            )

        if result.best:
            lines.append(f"\n  [green bold]Found: {result.best.name} ({result.best.delay}ms, all unlocked)[/]")
        else:
            lines.append("\n  [red bold]No fully unlocked node found[/]")

        log.update("\n".join(lines))

    def action_back(self) -> None:
        self.app.pop_screen()


# ── Node List Screen ───────────────────────────────────────────

class NodeListScreen(Screen):
    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
        Binding("s", "quick_scan", "Quick Scan"),
        Binding("f", "full_scan", "Full Scan"),
        Binding("r", "refresh_list", "Refresh"),
    ]

    def __init__(self, config: Config, client: ClashClient) -> None:
        super().__init__()
        self.config = config
        self.client = client
        self._node_list: list[str] = []
        self._current_node: str = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static("", id="node-header", markup=False)
            yield DataTable(id="node-table")
        yield Footer()

    async def on_mount(self) -> None:
        await self._load_nodes()
        self.query_one("#node-table", DataTable).focus()

    async def _load_nodes(self) -> None:
        table = self.query_one("#node-table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "Name", "Latency", "Claude", "ChatGPT", "Gemini")
        table.cursor_type = "row"

        try:
            group = await self.client.get_group(self.config.selector_group)
            chain = await self.client.get_proxy_chain(self.config.selector_group)
            self._node_list = [n for n in group.nodes if n not in (
                "DIRECT", "REJECT", "PASS", "REJECT-DROP",
                "自动节点", "故障节点", "故障转移", "自动选择",
            )]
            self._current_node = chain[-1]
        except Exception:
            self._node_list = []
            self._current_node = ""

        header = self.query_one("#node-header", Static)
        header.update(f"Nodes  group={self.config.selector_group}  current={self._current_node}")

        for i, name in enumerate(self._node_list, 1):
            marker = " →" if name == self._current_node else ""
            table.add_row(str(i), f"{marker} {name}", "···", "···", "···", "···")

        # Position cursor on current node
        try:
            idx = self._node_list.index(self._current_node)
            table.move_cursor(row=idx)
        except (ValueError, Exception):
            pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter on a row - switch to that node."""
        row = event.cursor_row
        if row is not None and row < len(self._node_list):
            name = self._node_list[row]
            asyncio.create_task(self._do_switch(name))

    async def _do_switch(self, name: str) -> None:
        try:
            ok = await self.client.switch_node(self.config.selector_group, name)
        except Exception as e:
            self.notify(f"Failed to switch to: {name}: {e}", severity="error", timeout=6)
            return
        if not ok:
            detail = f": {self.client.last_error}" if self.client.last_error else ""
            self.notify(f"Failed to switch to: {name}{detail}", severity="error", timeout=6)
            return
        self.notify(f"Switched to: {name}", severity="information", timeout=3)
        await self._load_nodes()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quick_scan(self) -> None:
        self.app.push_screen(ScanScreen(self.config, self.client, mode="quick"))

    def action_full_scan(self) -> None:
        self.app.push_screen(ScanScreen(self.config, self.client, mode="full"))

    async def action_refresh_list(self) -> None:
        await self._load_nodes()


# ── Subscription Screen ────────────────────────────────────────

class SubScreen(Screen):
    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("r", "update_subscription", "Update"),
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
    ]

    def __init__(self, config: Config, client: ClashClient) -> None:
        super().__init__()
        self.config = config
        self.client = client
        self.bridge = ClashctlBridge(config)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static("", id="sub-header", markup=False)
            yield DataTable(id="sub-table")
        yield Footer()

    async def on_mount(self) -> None:
        await self._load_subscriptions()
        self.query_one("#sub-table", DataTable).focus()

    async def _load_subscriptions(self) -> None:
        self.bridge = ClashctlBridge(self.config)
        self.config = Config.load(self.bridge.root)

        table = self.query_one("#sub-table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "ID", "Path", "URL")
        table.cursor_type = "row"

        for i, sub in enumerate(self.config.subscriptions):
            marker = "✓" if sub.is_current else " "
            table.add_row(marker, str(sub.id), sub.path, mask_url(sub.url))

        header = self.query_one("#sub-header", Static)
        header.update(f"Subscriptions  current=#{self.config.current_sub_id}")

        # Focus on current
        for i, sub in enumerate(self.config.subscriptions):
            if sub.is_current:
                table.move_cursor(row=i)
                break

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter on a row - switch subscription."""
        row = event.cursor_row
        if row is not None and row < len(self.config.subscriptions):
            sub = self.config.subscriptions[row]
            asyncio.create_task(self._use_subscription(sub.id))

    def _selected_subscription(self):
        table = self.query_one("#sub-table", DataTable)
        row = None
        try:
            row = table.cursor_coordinate.row
        except Exception:
            row = getattr(table, "cursor_row", None)
        if row is None or row < 0 or row >= len(self.config.subscriptions):
            return None
        return self.config.subscriptions[row]

    async def _run_sub_command(self, args: list[str | int], success: str) -> bool:
        try:
            result = await self.bridge.clashsub(args)
        except ClashctlError as e:
            self.notify(str(e), severity="error", timeout=8)
            return False

        if result.returncode != 0:
            self.notify(last_output_line(result) or "clashsub failed", severity="error", timeout=8)
            return False

        self.notify(last_output_line(result) or success, severity="information", timeout=4)
        await self._load_subscriptions()

        if isinstance(self.app, ClashApp):
            self.app.config = self.config
            if self.config.secret:
                self.app.client = ClashClient(self.config.api_base, self.config.secret)
                self.client = self.app.client
        return True

    async def _use_subscription(self, sub_id: int) -> None:
        await self._run_sub_command(["use", sub_id], f"Subscription #{sub_id} selected")

    async def _update_subscription(self, sub_id: int) -> None:
        await self._run_sub_command(["update", sub_id], f"Subscription #{sub_id} updated")

    async def action_update_subscription(self) -> None:
        sub = self._selected_subscription()
        if sub is None:
            self.notify("No subscription selected", severity="warning", timeout=3)
            return
        await self._update_subscription(sub.id)

    def action_back(self) -> None:
        self.app.pop_screen()


# ── Control Screen ──────────────────────────────────────────────

class ControlScreen(Screen):
    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.bridge = ClashctlBridge(config)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="control-area"):
            yield Static("Control  service, dashboard, proxy environment", id="control-header", markup=False)
            yield DataTable(id="control-table")
            yield Static("", id="control-output", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#control-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Action", "Command", "Purpose")
        for title, command, detail, _args in CONTROL_ACTIONS:
            table.add_row(title, command, detail)
        table.focus()
        self._set_output(
            "Enter runs the selected command.\n"
            "Current shell proxy cannot be changed by a Python subprocess.\n"
            'After starting the service, run: eval "$(ccli env)"'
        )

    def _set_output(self, text: str) -> None:
        self.query_one("#control-output", Static).update(clean_output(text))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.cursor_row < 0 or event.cursor_row >= len(CONTROL_ACTIONS):
            return
        asyncio.create_task(self._run_action(event.cursor_row))

    async def _run_action(self, row: int) -> None:
        title, _command, _detail, args = CONTROL_ACTIONS[row]
        self._set_output(f"Running {title}...")
        try:
            if args and args[0] == "restart":
                result = await self.bridge.restart()
            else:
                result = await self.bridge.clashctl(args)
        except ClashctlError as e:
            self._set_output(str(e))
            self.notify(str(e), severity="error", timeout=6)
            return

        output = result.output or f"{title} completed"
        if args in (["on"], ["proxy", "on"]):
            output += '\n\nTo enable proxy in this shell, run: eval "$(ccli env)"'
        if args == ["off"]:
            output += '\n\nTo remove proxy variables in this shell, run: eval "$(ccli env --unset)"'
        self._set_output(output)
        severity = "information" if result.returncode == 0 else "error"
        self.notify(last_output_line(result) or title, severity=severity, timeout=4)

    def action_back(self) -> None:
        self.app.pop_screen()


# ── Help Screen ─────────────────────────────────────────────────

class HelpScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
    ]
    HELP_TEXT = """
[bold]Keys[/]
  [bold]↑↓ / jk[/]  Navigate
  [bold]Enter[/]    Select / Switch
  [bold]q[/]         Quit
  [bold]Esc[/]       Back

[bold]Main Menu[/]
  [bold]s[/]         Quick Scan (random 8)
  [bold]f[/]         Full Scan (all nodes)
  [bold]l[/]         Node List
  [bold]u[/]         Subscriptions
  [bold]c[/]         Control
  [bold]w[/]         Toggle Auto-watch
  [bold]r[/]         Refresh status
  [bold]h / ?[/]     This help

[bold]Node List[/]
  [bold]Enter[/]     Switch to selected node
  [bold]s[/]         Quick Scan
  [bold]f[/]         Full Scan

[bold]Subscriptions[/]
  [bold]Enter[/]     Use selected subscription
  [bold]r[/]         Update selected subscription

[bold]CLI Commands[/]
  [green]clash-cli[/]                  Launch TUI
  [green]clash-cli status[/]           Show current node
  [green]clash-cli scan[/]             Quick scan
  [green]clash-cli scan --full[/]      Full scan
  [green]clash-cli switch <name>[/]    Switch node
  [green]clash-cli watch[/]            Auto-watch daemon
  [green]clash-cli watch --once[/]     One-shot check
  [green]clash-cli sub <args>[/]       Run clashsub
  [green]clash-cli ctl <args>[/]       Manage local runtime
  [green]eval "$(ccli env)"[/]         Enable proxy vars in current shell
"""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="help-area"):
            yield Static(self.HELP_TEXT)
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()


# ── Main App ───────────────────────────────────────────────────

class ClashApp(App):
    TITLE = "clash-cli"
    BINDINGS = [Binding("q", "quit", "Quit")]
    CSS = """
    Screen {
        layout: vertical;
        background: #0f1419;
        color: #d8dee9;
    }
    #dashboard {
        height: 1fr;
        padding: 1 2;
    }
    #status-box {
        height: auto;
        padding: 1;
        border: round #4c566a;
        background: #161b22;
        margin: 0 0 1 0;
    }
    #main-menu {
        height: 1fr;
        border: round #2e3440;
        background: #111820;
    }
    #main-menu ListItem {
        padding: 0 2;
        height: 2;
    }
    #main-menu ListItem.--highlight {
        background: #243447;
        color: white;
    }
    #scan-area {
        height: 1fr;
        padding: 1 2;
    }
    #scan-title {
        height: auto;
        margin: 0 0 1 0;
    }
    #scan-log {
        height: 1fr;
    }
    #scan-progress {
        margin: 1 0 0 0;
    }
    #node-header, #sub-header {
        height: auto;
        padding: 1 2;
        background: #161b22;
    }
    #node-table, #sub-table {
        height: 1fr;
    }
    #control-area {
        height: 1fr;
        padding: 1 2;
    }
    #control-header {
        height: auto;
        padding: 0 0 1 0;
    }
    #control-table {
        height: 14;
    }
    #control-output {
        height: 1fr;
        margin: 1 0 0 0;
        padding: 1;
        border: round #4c566a;
        background: #111820;
    }
    #help-area {
        height: 1fr;
        padding: 1 2;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.config = Config.load()
        if not self.config.secret:
            self.exit(message="Error: Clash API secret not set.\n"
                "Run: clashsecret <your-secret>\n"
                "Or set CLASH_SECRET environment variable.")
        self.client = ClashClient(self.config.api_base, self.config.secret)

    def action_quit(self) -> None:
        self.exit()

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self.config, self.client))


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

    parser = argparse.ArgumentParser(prog="clash-cli", description="Clash proxy node manager")
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
        print(f"\nFound: {result.best.name} ({result.best.delay}ms, all unlocked)")
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
        case on
            set -l out (command ccli ctl on $argv[2..-1])
            set -l rc $status
            if test $rc -eq 0
                eval (command ccli env)
                command ccli shell-status --proxy on
            else
                echo "$out"
            end
            return $rc
        case off
            set -l out (command ccli ctl off $argv[2..-1])
            set -l rc $status
            eval (command ccli env --unset)
            if test $rc -eq 0
                command ccli shell-status --proxy off
            else
                echo "$out"
            end
            return $rc
        case proxy
            set -l out (command ccli ctl proxy $argv[2..-1])
            set -l rc $status
            if test "$argv[2]" = on
                eval (command ccli env)
                command ccli shell-status --proxy on
            else if test "$argv[2]" = off
                eval (command ccli env --unset)
                command ccli shell-status --proxy off
            else
                echo "$out"
            end
            return $rc
        case '*'
            command ccli $argv
    end
end"""

    return """ccli() {
  case "$1" in
    on)
      shift
      local out
      out="$(command ccli ctl on "$@")"
      local rc=$?
      if [ "$rc" -eq 0 ]; then
        eval "$(command ccli env)"
        command ccli shell-status --proxy on
      else
        printf '%s\n' "$out"
      fi
      return "$rc"
      ;;
    off)
      shift
      local out
      out="$(command ccli ctl off "$@")"
      local rc=$?
      eval "$(command ccli env --unset)"
      if [ "$rc" -eq 0 ]; then
        command ccli shell-status --proxy off
      else
        printf '%s\n' "$out"
      fi
      return "$rc"
      ;;
    proxy)
      shift
      local out
      out="$(command ccli ctl proxy "$@")"
      local rc=$?
      case "$1" in
        on)
          [ "$rc" -eq 0 ] && eval "$(command ccli env)"
          [ "$rc" -eq 0 ] && command ccli shell-status --proxy on || printf '%s\n' "$out"
          ;;
        off)
          eval "$(command ccli env --unset)"
          [ "$rc" -eq 0 ] && command ccli shell-status --proxy off || printf '%s\n' "$out"
          ;;
        *)
          printf '%s\n' "$out"
          ;;
      esac
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
    _print_clashctl_result(result)


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
            if unlock.all_ok:
                print(f"[ok] {current} Claude:{unlock.claude} ChatGPT:{unlock.chatgpt} Gemini:{unlock.gemini}")
                return True
            print(f"[FAIL] {current} Claude:{unlock.claude} ChatGPT:{unlock.chatgpt} Gemini:{unlock.gemini}")
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

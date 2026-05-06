"""Main Textual TUI application for clash-cli."""

from __future__ import annotations

import asyncio
import sys

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
from clash_cli.config import Config
from clash_cli.scanner.scanner import full_scan, quick_scan


COLOR_OK = "green"
COLOR_BLOCKED = "red"
COLOR_FAIL = "yellow"
COLOR_DIM = "dim"
COLOR_CYAN = "cyan"


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


# ── Main Dashboard ─────────────────────────────────────────────

class MenuItem(ListItem):
    def __init__(self, key: str, label: str, action: str) -> None:
        super().__init__()
        self.action_name = action
        self._key = key
        self._label = label

    def compose(self) -> ComposeResult:
        yield Static(f"  [{self._key}] {self._label}")


class MainScreen(Screen):
    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select", "Select"),
        Binding("s", "quick_scan", "Quick Scan"),
        Binding("f", "full_scan", "Full Scan"),
        Binding("l", "node_list", "Node List"),
        Binding("u", "subscriptions", "Subscriptions"),
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
                MenuItem("s", "Quick Scan (random 8)", "quick_scan"),
                MenuItem("f", "Full Scan (all nodes)", "full_scan"),
                MenuItem("l", "Node List", "node_list"),
                MenuItem("u", "Subscriptions", "subscriptions"),
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
            line.update(f"  [dim]⏾ Auto-watch OFF  press w to enable[/]")

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
            f"[bold]  节点选择 → [{COLOR_CYAN}]{current}[/{COLOR_CYAN}][/bold]\n"
            f"  Claude: {color_unlock(unlock.claude)}  "
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

    def action_quick_scan(self) -> None:
        self.app.push_screen(ScanScreen(self.config, self.client, mode="quick"))

    def action_full_scan(self) -> None:
        self.app.push_screen(ScanScreen(self.config, self.client, mode="full"))

    def action_node_list(self) -> None:
        self.app.push_screen(NodeListScreen(self.config, self.client))

    def action_subscriptions(self) -> None:
        self.app.push_screen(SubScreen(self.config, self.client))

    async def action_refresh(self) -> None:
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
                result = await quick_scan(self.client, self.config.proxy_url)
            else:
                result = await full_scan(self.client, self.config.proxy_url)
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
            yield Static("", id="node-header")
            yield DataTable(id="node-table")
        yield Footer()

    async def on_mount(self) -> None:
        await self._load_nodes()
        self.query_one("#node-table", DataTable).focus()

    async def _load_nodes(self) -> None:
        table = self.query_one("#node-table", DataTable)
        table.clear()
        table.add_columns("#", "Name", "Latency", "Claude", "ChatGPT", "Gemini")
        table.cursor_type = "row"

        try:
            group = await self.client.get_group(self.config.selector_group)
            self._node_list = [n for n in group.nodes if n not in (
                "DIRECT", "REJECT", "PASS", "REJECT-DROP",
                "自动节点", "故障节点", "故障转移", "自动选择",
            )]
            self._current_node = group.current
        except Exception:
            self._node_list = []
            self._current_node = ""

        header = self.query_one("#node-header", Static)
        header.update(
            f"  Node List: [{COLOR_CYAN}]{self.config.selector_group}[/{COLOR_CYAN}]  "
            f"Current: [green]{self._current_node}[/green]"
        )

        for i, name in enumerate(self._node_list, 1):
            marker = " →" if name == self._current_node else ""
            table.add_row(str(i), f"{marker} {name}", "···", "···", "···", "···")

        # Position cursor on current node
        try:
            idx = self._node_list.index(self._current_node)
            table.move_cursor(row=idx)
        except (ValueError, Exception):
            pass

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Handle Enter on a row - switch to that node."""
        row = event.cell_key.row_key.value
        if row is not None and row < len(self._node_list):
            name = self._node_list[row]
            asyncio.create_task(self._do_switch(name))

    async def _do_switch(self, name: str) -> None:
        await self.client.switch_node(self.config.selector_group, name)
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
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
    ]

    def __init__(self, config: Config, client: ClashClient) -> None:
        super().__init__()
        self.config = config
        self.client = client

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static("", id="sub-header")
            yield DataTable(id="sub-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sub-table", DataTable)
        table.add_columns("#", "ID", "Path", "URL")
        table.cursor_type = "row"

        for i, sub in enumerate(self.config.subscriptions):
            marker = "✓" if sub.is_current else " "
            url_short = sub.url[:60] + "..." if len(sub.url) > 60 else sub.url
            table.add_row(marker, str(sub.id), sub.path, url_short)

        header = self.query_one("#sub-header", Static)
        header.update(f"  Subscriptions  (current: #{self.config.current_sub_id})")

        # Focus on current
        for i, sub in enumerate(self.config.subscriptions):
            if sub.is_current:
                table.move_cursor(row=i)
                break
        table.focus()

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Handle Enter on a row - switch subscription."""
        row = event.cell_key.row_key.value
        if row is not None and row < len(self.config.subscriptions):
            sub = self.config.subscriptions[row]
            self.notify(
                f"Sub switch via shell: clashsub use {sub.id}",
                severity="warning",
            )

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
  [bold]w[/]         Toggle Auto-watch
  [bold]r[/]         Refresh status
  [bold]h / ?[/]     This help

[bold]Node List[/]
  [bold]Enter[/]     Switch to selected node
  [bold]s[/]         Quick Scan
  [bold]f[/]         Full Scan

[bold]CLI Commands[/]
  [green]clash-cli[/]                  Launch TUI
  [green]clash-cli status[/]           Show current node
  [green]clash-cli scan[/]             Quick scan
  [green]clash-cli scan --full[/]      Full scan
  [green]clash-cli switch <name>[/]    Switch node
  [green]clash-cli watch[/]            Auto-watch daemon
  [green]clash-cli watch --once[/]     One-shot check
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
    }
    #dashboard {
        height: 1fr;
        padding: 1 2;
    }
    #status-box {
        height: auto;
        padding: 1;
        border: solid $primary;
        margin: 0 0 1 0;
    }
    #main-menu {
        height: 1fr;
    }
    #main-menu ListItem {
        padding: 0 1;
    }
    #main-menu ListItem.--highlight {
        background: $accent 30%;
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
        padding: 1 0;
    }
    #node-table, #sub-table {
        height: 1fr;
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

    parser = argparse.ArgumentParser(prog="clash-cli", description="Clash proxy node manager")
    sub = parser.add_subparsers(dest="command")
    scan_parser = sub.add_parser("scan", help="Scan nodes for AI unlock")
    scan_parser.add_argument("--full", action="store_true", help="Full scan (all nodes)")
    sub.add_parser("status", help="Show current node and unlock status")
    switch_parser = sub.add_parser("switch", help="Switch to a node")
    switch_parser.add_argument("node", help="Node name")
    watch_parser = sub.add_parser("watch", help="Auto-watch and switch on failure")
    watch_parser.add_argument(
        "--interval", type=int, default=120,
        help="Check interval in seconds (default: 120)",
    )
    watch_parser.add_argument(
        "--once", action="store_true",
        help="Run one check and exit",
    )
    args = parser.parse_args()

    if args.command is None:
        ClashApp().run()
    elif args.command == "status":
        asyncio.run(cli_status())
    elif args.command == "scan":
        asyncio.run(cli_scan(full=args.full))
    elif args.command == "switch":
        asyncio.run(cli_switch(args.node))
    elif args.command == "watch":
        asyncio.run(cli_watch(args.interval, args.once))


async def cli_status():
    config = Config.load()
    _check_config(config)
    client = ClashClient(config.api_base, config.secret)
    try:
        current = await client.get_current_node(config.selector_group)
        unlock = await check_unlock(config.proxy_url)
        print(f"  节点选择 → {current}")
        print(f"  Claude:  {unlock.claude}")
        print(f"  ChatGPT: {unlock.chatgpt}")
        print(f"  Gemini:  {unlock.gemini}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


async def cli_scan(full: bool = False):
    config = Config.load()
    _check_config(config)
    client = ClashClient(config.api_base, config.secret)
    try:
        result = await full_scan(client, config.proxy_url) if full else await quick_scan(client, config.proxy_url)
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


async def cli_switch(node: str):
    config = Config.load()
    _check_config(config)
    client = ClashClient(config.api_base, config.secret)
    ok = await client.switch_node(config.selector_group, node)
    if ok:
        print(f"Switched to: {node}")
    else:
        print(f"Failed to switch to: {node}", file=sys.stderr)
        sys.exit(1)


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
    if not config.secret:
        print("Error: Clash API secret not set.", file=sys.stderr)
        print("Run: clashsecret <your-secret>", file=sys.stderr)
        print("Or set CLASH_SECRET environment variable.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

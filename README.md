# clash-cli

Clash 代理节点智能管理 TUI 工具。

- 一键检测节点能否访问 Claude / ChatGPT / Gemini
- 快速扫描找到最快可用节点，自动切换
- 后台守护，节点挂了自动切
- 短命令：`ccli`

## 安装

```bash
# 1. 安装代理内核
git clone --depth 1 https://github.com/Flesymeb/clash-cli.git && cd clash-cli && bash install.sh

# 2. 安装 TUI
pipx install git+https://github.com/Flesymeb/clash-cli.git
```

（需要 Python 3.10+，`pipx` 自动隔离环境。没有 pipx？`pip install pipx && pipx ensurepath`）

## 使用

```bash
ccli              # 启动 TUI
ccli status       # 查看当前节点解锁状态
ccli scan         # 快速扫描（随机 8 个，找到可用即换）
ccli scan --full  # 全量扫描所有节点
ccli switch <名>   # 切换到指定节点
ccli watch        # 后台守护（每 120s 检测，挂了自动切）
ccli watch --once # 一次性检测
```

TUI 快捷键：`↑↓` 导航 `Enter` 选择 `s` 扫描 `w` 守护 `h` 帮助 `q` 退出

## 致谢

- [nelvko/clash-for-linux-install](https://github.com/nelvko/clash-for-linux-install) — 原版 clashctl
- [clash-verge-rev](https://github.com/clash-verge-rev/clash-verge-rev) — 解锁检测方案

MIT

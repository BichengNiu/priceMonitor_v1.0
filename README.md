# MOET 必需品价格定时采集器

这是一个 Windows 价格采集程序。发布版 `MoetPriceMonitor.exe` 已内置 Python、`requests` 和 `pandas`，目标电脑运行它不需要安装 Python 或任何 Python 依赖包。

程序每天上午 10:00（电脑本地时间）检查一次。同一发布日期只会成功采集一次；发现新发布日期时，程序下载完整商品—门店快照并追加到 Stata 面板数据。

## 构建独立程序

构建机只需要安装 Python 3.14 和 Python Launcher。Python 仅用于构建，构建完成后不会成为运行依赖：

```powershell
.\build.ps1
```

脚本会在 `.build-venv` 中安装构建依赖，并将可独立运行的文件写入 `release`：

- `release\MoetPriceMonitor.exe`：包含 Python 解释器和运行依赖的单文件程序；
- `release\config.toml`：运行配置；
- `release\install_task.ps1`：从发布目录安装计划任务的脚本。

构建脚本会优先复制项目现有的 `config.toml`，不会复制或覆盖 `data` 中的历史数据。若需要把程序部署到另一台电脑，请把 `release` 目录整体复制过去，并按需编辑其中的 `config.toml`。

## 配置和手动运行

首次配置：

```powershell
Copy-Item .\config.example.toml .\config.toml
```

默认先用 5,000 条请求探测总量，再用一个完整响应取得同一时点快照；连接超时 10 秒、完整响应读取超时 180 秒，失败最多尝试 3 次。可编辑 `config.toml` 调整探测大小、超时、输出目录和日志级别。

使用源代码运行：

```powershell
python run_monitor.py --config config.toml
```

使用独立程序运行：

```powershell
.\release\MoetPriceMonitor.exe --config .\config.toml
```

不传 `--config` 时，程序会读取可执行文件旁边的 `config.toml`。

成功退出码为 `0`，失败退出码为 `1`。输出文件为：

- `data\prices_panel.dta`：Stata 118 格式面板文件；
- `data\state.json`：上次成功快照和指纹，用于去重、变更检测与恢复；
- `logs\monitor.log`：滚动运行日志，单文件 5 MB，保留 5 个备份。

每次成功运行后，程序会清理自身产生的事务临时文件、Python 缓存、pytest 临时目录以及构建缓存目录；`data`、`logs`、`release` 和配置文件会保留。运行中的 Python 环境不会被自身删除。

升级自旧版时，程序会将现有 `prices_panel.csv` 一次性转换为 `prices_panel.dta`，同时保留 CSV 作为备份。不要手工修改或删除 `state.json`，否则可能在面板中重复写入同一个发布日期。

## 安装自动计划任务

在项目根目录构建后，先预览：

```powershell
.\install_task.ps1 -WhatIf
```

确认后安装：

```powershell
.\install_task.ps1
```

脚本会优先使用 `release\MoetPriceMonitor.exe`，并使用项目根目录的 `config.toml` 和 `data`，这样不会丢失已有历史数据。若从复制后的 `release` 目录直接执行其中的安装脚本，则使用该目录旁边的配置和数据目录。

计划任务名为 `MOET Essential Goods Price Monitor`。它不再调用 `.venv` 或外部 Python，而是直接调用独立 exe。任务每天上午 10:00（电脑本地时间）运行一次。关机错过执行时间时，`StartWhenAvailable` 会在电脑恢复可用并登录后补跑；上一次仍在运行时不会启动重叠实例；使用电池供电时也允许执行。

检查任务：

```powershell
Get-ScheduledTask -TaskName "MOET Essential Goods Price Monitor"
Get-ScheduledTaskInfo -TaskName "MOET Essential Goods Price Monitor"
```

## 测试

测试使用模拟 HTTP 和临时文件，不访问真实政府网站，也不会注册真实计划任务：

```powershell
python -m pytest -q
```

## 故障排查

- `Configuration file not found`：确认已将 `config.example.toml` 复制为 `config.toml`，或把 `config.toml` 放在 exe 旁边。
- 连续网络失败：检查网络后手动运行一次；失败版本不会写入 CSV 或状态。
- Excel 正打开 CSV 时 Windows 可能锁定文件，导致提交失败。关闭 Excel 后重试，事务恢复会在下一次运行开始时完成。
- 网站接口或字段变化时程序会明确失败并写入日志，不会静默生成缺字段数据。
- 计划任务失败时先看 `logs\monitor.log`，再查看 `Get-ScheduledTaskInfo` 的 `LastTaskResult`。

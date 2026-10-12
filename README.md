# MOET 必需品价格定时采集器

面板使用原生存储类型：DuckDB 将 publication_date 存为 DATE，将 quantity、standardized_price、price_aed 存为固定精度 DECIMAL；DTA 使用 Stata 日期、双精度数值，以及带原始文本值标签的分类编码。Stata 默认可显示文本标签；若其他软件导入时只显示整数代码，请启用值标签转换，例如 pandas 的 read_stata(..., convert_categoricals=True)。程序会在写入前检查数值精度范围，超出声明精度时停止写入，避免静默舍入。

这是一个 Windows 价格采集程序。人工运行和计划任务统一通过项目根目录的 `run_monitor.bat` 启动；`release\MoetPriceMonitor.exe` 是 BAT 调用的内部执行程序，不作为单独的运行入口。目标电脑不需要安装 Python 或 Python 依赖包。

程序每天上午 10:00（电脑本地时间）检查一次。同一发布日期只会成功采集一次；发现新发布日期时，程序下载完整商品—门店快照并追加到 DuckDB 面板数据库，同时更新本地 Stata 面板文件。

## 构建运行组件

构建机只需要安装 Python 3.14 和 Python Launcher。Python 仅用于构建，构建完成后不会成为运行依赖：

```powershell
.\build.ps1
```

脚本会在 `.build-venv` 中安装构建依赖，并将内部执行程序写入固定的 `release` 目录：

- `release\MoetPriceMonitor.exe`：包含 Python 解释器和运行依赖的单文件执行程序，由根目录 BAT 调用。

构建不会复制或覆盖配置和历史数据。部署时需保留项目根目录的 `run_monitor.bat`、`install_task.ps1`、`config.toml`、`data` 和 `release` 目录。

## 配置和手动运行

项目根目录的 `config.toml` 是采集程序使用的配置。默认先用 5,000 条请求探测总量，再用一个完整响应取得同一时点快照；连接超时 10 秒、完整响应读取超时 180 秒，失败最多尝试 3 次。可编辑 `config.toml` 调整探测大小、超时、输出目录和日志级别。

首次使用前请确认根目录配置存在并构建内部执行程序。人工更新只需双击项目根目录的 `run_monitor.bat`；计划任务也调用同一个 BAT。不要直接运行 Python 源文件或 `release\MoetPriceMonitor.exe`。

成功退出码为 `0`，失败退出码为 `1`。数据文件为：

- `data\prices_panel.duckdb`：采集程序使用的主数据库，纳入 Git LFS 并上传 GitHub；
- `data\prices_panel.dta`：Stata 118 格式本地副本，供用户使用，不纳入 GitHub；
- `logs\monitor.log`：滚动运行日志，单文件 5 MB，保留 5 个备份。

DuckDB 是主存储，DTA 是每次成功提交时同步生成的本地导出；手工修改 DTA 不会反向修改数据库。程序要求当前版本的 DuckDB 和 DTA 同时存在且结构正确，不会从旧 DTA 导入数据，不会自动升级旧数据库结构，也不会自动修复过期或不一致的 DTA。

每次成功运行后，程序会清理自身产生的事务临时文件、项目源码缓存、pytest 临时目录以及构建缓存目录；`data`、`logs`、`release`、配置文件和 Python 虚拟环境会保留。

升级后若数据结构不符合当前版本，程序会明确报错；请从当前版本的 DuckDB/DTA 备份恢复，不会尝试回退兼容。

## 安装自动计划任务

在项目根目录构建后，先预览：

```powershell
.\install_task.ps1 -WhatIf
```

确认后安装：

```powershell
.\install_task.ps1
```

安装器只从项目根目录注册任务，并配置计划任务通过 `cmd.exe` 调用根目录的 `run_monitor.bat --scheduled`。定时与人工执行走同一个 BAT；`--scheduled` 仅用于禁用交互式暂停。

计划任务名为 `MOET Essential Goods Price Monitor`。它每天上午 10:00（电脑本地时间）运行一次。关机错过执行时间时，`StartWhenAvailable` 会在电脑恢复可用并登录后补跑；上一次仍在运行时不会启动重叠实例；使用电池供电时也允许执行。

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

- `Configuration file not found`：确认项目根目录存在 `config.toml`。
- 连续网络失败：检查网络后双击根目录 `run_monitor.bat` 重试；失败版本不会写入面板。
- 网站接口或字段变化时程序会明确失败并写入日志，不会静默生成缺字段数据。
- 计划任务失败时先看 `logs\monitor.log`，再查看 `Get-ScheduledTaskInfo` 的 `LastTaskResult`。

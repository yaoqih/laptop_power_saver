# laptop_power_saver
在 Windows 11 上以极低开销按固定间隔采样系统进程，统计每进程的等效核心数（core-seconds/平均核心占用）、累计 CPU 时间、“活跃时间”（按阈值判定）、可执行文件路径等信息，持久化到 SQLite，支持 CSV 导出与终端 top 观察。

- 默认每 1 秒采样（可调）
- 默认保留最近 30 天原始样本（可调）
- 单文件数据库，WAL 模式，单 tick 批事务，稳健容错

适合用于寻找“耗电嫌疑进程”、系统长期负载画像、软件功耗回归验证等场景。

--------------------------------------------------------------------------------

## 特性总览
- 采样与统计
  - 差分 user+kernel CPU 时间，稳健计算等效核心数（Δcpu/dt），统计累计 CPU 时间（秒）
  - 以阈值判定“活跃时间”（active wall time），默认阈值=0.005（约单核 0.5%/s）
  - 解析并记录 exe_path、name、cmdline、username、ppid
  - 可选采集内存 RSS/VMS 与 IO 读/写字节（默认开启）
- 存储与清理
  - SQLite 单文件（默认 [./lps.db](lps.db)），WAL 模式，单 tick 批量写入
  - 原始样本按保留期自动清理（默认 30 天）
- 导出与查看
  - CSV 导出：按 exe 或按 (pid, create_time) 会话聚合
  - 终端 top：按观察窗口聚合并排序，快速定位热点

--------------------------------------------------------------------------------

## 环境要求
- 操作系统：Windows 11（兼容性以 psutil 为准）
- Python：3.8+（建议 3.9+）
- 依赖：见 [requirements.txt](requirements.txt)
  - 当前仅依赖 psutil

注：非管理员权限即可运行；受权限限制的进程字段可能读取不到（例如内存/IO/路径），会被跳过或记为 NULL，并在 process 表记录 partial_meta=1。

--------------------------------------------------------------------------------

## 安装
建议使用虚拟环境：
- python -m venv .venv
- .venv\Scripts\activate
- pip install -r [requirements.txt](requirements.txt)

无需额外安装步骤；以模块形式运行 CLI（依赖 [lps/__main__.py](lps/__main__.py) 与 [cli.py](cli.py)）。

--------------------------------------------------------------------------------

## 快速开始
1) 启动采样（默认 1 秒间隔、保留 30 天、采集内存与 IO）
- python -m lps run --db ./lps.db

可选参数（见“命令参考”获取完整说明）：
- --interval 采样间隔秒（默认 1.0）
- --active-threshold 活跃阈值（默认 0.005）
- --retention 保留期（默认 30d，支持 s/m/h/d）
- --no-mem/--no-io 关闭内存或 IO 采集
- --log-level DEBUG|INFO|WARNING|ERROR

按 Ctrl+C 安全停止，程序会在下一次循环结束后退出。

2) 导出最近 24 小时按 exe 聚合的 CSV
- python -m lps export csv --db ./lps.db --group exe --since 24h --out out.csv

3) 导出指定时间窗、按 (pid, create_time) 会话聚合
- python -m lps export csv --db ./lps.db --group pid --since 2025-09-01 --until 2025-09-02 --out pid_window.csv

4) 终端观察热点（近 10 分钟，按 exe）
- python -m lps top --db ./lps.db --window 10m --group exe --limit 20

5) 数据维护
- VACUUM 压缩：python -m lps vacuum --db ./lps.db
- 重置数据库（清空 sample 与 process 并 VACUUM）：python -m lps reset --db ./lps.db

--------------------------------------------------------------------------------

## 命令参考
所有命令均以 python -m lps 子命令形式提供（入口见 [lps/__main__.py](lps/__main__.py)，命令定义见 [cli.py](cli.py)）。

- 采样器
  - run
    - --db ./lps.db
    - --interval 1.0
    - --active-threshold 0.005
    - --retention 30d
    - --no-mem / --no-io
    - --log-level DEBUG|INFO|WARNING|ERROR
- 导出
  - export csv
    - --db ./lps.db
    - --group exe|pid（默认 exe）
    - --since 24h（相对如 24h，绝对 ISO/epoch，或 now）
    - --until now（相对/绝对/now）
    - --out report.csv（必填）
- 终端 top
  - top
    - --db ./lps.db
    - --window 10m（例如 10m/1h）
    - --group exe|pid（默认 exe）
    - --limit 20
- 维护
  - vacuum
    - --db ./lps.db
  - reset
    - --db ./lps.db

参数解析与时间窗口计算实现位于 [lps/utils.py](lps/utils.py)。

--------------------------------------------------------------------------------

## 导出 CSV 字段说明
- 当 group=exe（按 exe 路径聚合）：
  - 列：exe_path, samples, cpu_s, wall_s, active_wall_s, avg_eff_cores, avg_cpu_percent, avg_rss, since_ts, until_ts
- 当 group=pid（按 (pid, create_time) 会话聚合）：
  - 列：pid, create_time, exe_path, samples, cpu_s, wall_s, active_wall_s, avg_eff_cores, avg_cpu_percent, avg_rss, since_ts, until_ts

列含义：
- samples：样本数
- cpu_s：累计 CPU 时间（秒），等价于 SUM(Δcpu)
- wall_s：累计观察墙钟时间（秒），为进程“存在于枚举列表中”的时间总和
- active_wall_s：满足活跃阈值的墙钟时间累计（“运行的时间”）
- avg_eff_cores：平均等效核心数 = SUM(Δcpu) / SUM(dt)
- avg_cpu_percent：平均 CPU 百分比（逻辑核百分比）= avg_eff_cores × 100
- avg_rss：平均 RSS 内存（字节）
- since_ts / until_ts：导出的时间窗口边界（秒）

示例输出文件参见 [out.csv](out.csv)（若已生成）。

--------------------------------------------------------------------------------

## 数据模型与索引
数据库结构由运行期自动创建（见 [lps/db.py](lps/db.py)）：
- 表 process：每一行对应 (pid, create_time) 唯一会话
  - 字段：pid, create_time, exe_path, name, cmdline, username, ppid, first_seen, last_seen, ended, partial_meta
  - 约束：UNIQUE(pid, create_time)
  - 索引：exe_path、ended
- 表 sample：每一行为一次采样结果
  - 字段：ts, process_id, dt_s, delta_cpu_s, eff_cores, active, rss_bytes, vms_bytes, io_read_bytes, io_write_bytes
  - 索引：ts、(process_id, ts)

默认保存在 [./lps.db](lps.db) 中，可通过 --db 指定其他路径。

--------------------------------------------------------------------------------

## 设计要点与性能
- 稳健的等效核心数计算
  - 读取 psutil 的 user+system CPU 时间并差分，避免初次调用抖动与时钟漂移影响
  - 对等效核心数设置上限：不超过逻辑核数的 1.5 倍，避免异常尖峰
- 时间基准
  - 采样间隔使用 monotonic（抗系统时间调整），入库时间戳使用 time.time 便于窗口查询
- PID 复用安全
  - 以 (pid, create_time) 作为会话唯一键；进程连续缺失 2 个 tick 视作结束并落盘 ended=1
- 低开销实践
  - psutil.process_iter 批量拉取、单 tick 单事务写入、SQLite WAL+NORMAL，同步开销低
- 自动清理
  - 每 ~60 秒按保留期删除旧样本（默认 30 天），减少数据库体积

--------------------------------------------------------------------------------

## 注意事项与限制
- 权限限制：部分系统/服务进程的内存、IO、路径不可读，将被跳过或置 NULL，并记录 partial_meta=1
- 仅统计“可见”与“可访问”的进程；跨用户会话的可见性取决于系统策略
- 长期运行建议将数据库放在 SSD，并定期导出归档
- 本工具不会改变系统电源策略；“省电”效果来自定位噪声/异常进程与治理

--------------------------------------------------------------------------------

## 故障排查
- CSV 为空或行数很少
  - 确认采样已运行足够时间；检查 --since/--until 是否设置过小窗口
- “Invalid duration spec”/“Invalid time point spec”
  - 检查时间参数格式：支持 30s/15m/2h/7d、ISO（2025-09-02T12:30:00/2025-09-02 12:30:00/2025-09-02）、epoch、now
- “SQLite database is locked”
  - 确认是否有其他程序占用；避免同时对同一 DB 做大量写入操作
- 读取进程信息异常
  - 常见 AccessDenied/NoSuchProcess 由权限/进程生命周期导致，程序已容错并跳过

--------------------------------------------------------------------------------

## 常见使用配方
- 近 24 小时“耗电可执行文件”排行（按 CPU 累计排序）
  - python -m lps export csv --db ./lps.db --group exe --since 24h --out exe_24h.csv
- 观察某次构建期间的“活跃进程”分布（绝对时间窗）
  - python -m lps export csv --db ./lps.db --group pid --since 2025-09-01T10:00:00 --until 2025-09-01T12:00:00 --out build_window.csv
- 10 分钟快速热点扫描
  - python -m lps top --db ./lps.db --window 10m --group exe --limit 30

--------------------------------------------------------------------------------

## 项目结构
- [cli.py](cli.py)：命令行入口与子命令解析
- [lps/__init__.py](lps/__init__.py)：包元信息
- [lps/__main__.py](lps/__main__.py)：python -m lps 入口
- [lps/sampler.py](lps/sampler.py)：采样主循环与 Δcpu/活跃判定
- [lps/db.py](lps/db.py)：数据库初始化、批量写入、清理与状态更新
- [lps/export.py](lps/export.py)：CSV 导出实现（exe/pid 两种聚合）
- [lps/utils.py](lps/utils.py)：时长/时间点解析、工具函数
- [lps/windows.py](lps/windows.py)：电池电量获取（可用于扩展自适应采样）
- [requirements.txt](requirements.txt)：依赖清单
- 数据样例：导出后可在 [out.csv](out.csv) 查看

（注意：本仓库没有 src/ 目录，上述文件均位于仓库根目录及根下的 lps/ 包内。）

--------------------------------------------------------------------------------

## 开发与扩展
- 建议以 DEBUG 日志运行便于排查：
  - python -m lps run --db ./lps.db --log-level DEBUG
- 可选扩展方向（欢迎基于当前代码继续迭代）：
  - 低电量自动降采样（参见电池读数实现 [lps/windows.py](lps/windows.py)）
  - 任务计划或服务化自启动
  - JSON/HTTP 只读接口
  - 更丰富的导出指标与图表
  - “耗电嫌疑评分”/回归报警

提交 PR/Issue 前，建议先阅读采样实现与导出逻辑：见 [lps/sampler.py](lps/sampler.py) 与 [lps/export.py](lps/export.py)。

--------------------------------------------------------------------------------

## 验证建议
- 启动采样 1–2 分钟后导出 CSV，检查 cpu_s/avg_eff_cores/active_wall_s 是否与直觉一致（运行编译或压测时应明显上升）
- 使用 top 命令观察 10 分钟窗口是否与任务管理器趋势一致

--------------------------------------------------------------------------------

若需更多示例或将“低电量降采样”等能力落地，可在当前代码基础上追加需求继续迭代。
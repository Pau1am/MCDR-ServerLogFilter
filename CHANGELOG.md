# Changelog

本插件所有值得一提的变更都记录在此。插件包内也附带同一份 `CHANGELOG.md`。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [1.0.2] - 2026-10-02

### 修复

1. **最低 MCDR 版本声明由 `>=2.13.0` 修正为 `>=2.15.0`。**
   经逐版本实测，`mcdreforged.api.types.InfoActionFlag` 是 **MCDR 2.15.0** 才引入的 API，
   2.13.0 / 2.13.1 / 2.13.2 / 2.14.0 / 2.14.1 的整包内均不存在该类。
   旧声明会让这些版本上的用户在加载阶段直接遇到
   `ImportError: cannot import name 'InfoActionFlag' from 'mcdreforged.api.types'`，
   且插件被标记为加载失败。

2. **低版本 MCDR 上改为给出可读提示。**
   `InfoActionFlag` 改为容错导入；当 MCDR < 2.15.0 时，插件会打印
   「需要 MCDR >= 2.15.0，当前版本不支持 InfoActionFlag，插件已停用」并放弃注册，
   而不是抛出难以理解的裸 `ImportError`。
   配合修正后的依赖声明，实际运行时由 MCDR 原生依赖检查直接拦截，
   提示为 `依赖项 mcdreforged@x.y.z 不满足版本约束 >=2.15.0`。

   > 本插件「只摘控制台回显、保留事件分发」的设计依赖 `InfoActionFlag.hidden()`；
   > 2.14.x 及更早的 `InfoFilter` 只有「返回 `False` 即丢弃整条」的语义，无法等价实现，
   > 因此不存在兼容 2.14 及以下版本的写法。

### 未改动

- 过滤逻辑、命令、配置项与文件格式**全部不变**，从 1.0.1 升级无需调整任何配置。

### 兼容性实测结论

| 维度 | 结果 |
| --- | --- |
| MCDR 2.13.0 / 2.14.1 | 1.0.1 加载失败；1.0.2 由依赖检查明确拦截 |
| MCDR 2.15.0 / 2.15.7 / 2.16.0 | 加载与过滤均正常 |
| MC 1.16.5 / 1.19.4 / 1.20.1 / 1.20.6 / 1.21.8 / 1.21.11 / 26.1 / 26.2 / 26.3 | 真实服务端实测通过，服务端原生日志完整 |

---

## [1.0.1] - 2026-10-01

### 变更

- 元数据迁移到新 schema：`authors`（含 `link`）、`links.homepage`、`license: MIT`，
  以满足 MCDReforged 官方插件目录的收录校验。

---

## [1.0.0] - 2026-10-01

### 新增

- 首个版本。基于 `InfoActionFlag.hidden()` 实现「只从 MCDR 控制台隐去、保留事件分发」的日志过滤。
- 命令：`!!logfilter`（状态）、`list`、`test <文本>`、`reload`（需 admin）。
- 配置：`config/server_log_filter/config.json` 的 `patterns[]` / `log_matched_lines` / `report_on_server_stop`。
- 匹配使用 `re.search`（包含匹配），正则**不要**自己加 `.*`。

[1.0.2]: https://github.com/Pau1am/MCDR-ServerLogFilter/releases/tag/v1.0.2
[1.0.1]: https://github.com/Pau1am/MCDR-ServerLogFilter/releases/tag/v1.0.1
[1.0.0]: https://github.com/Pau1am/MCDR-ServerLogFilter/releases/tag/v1.0.0

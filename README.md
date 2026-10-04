# MCDR-ServerLogFilter

**语言 / Language:** **简体中文** | [English](README_en.md)

一个 MCDReforged 插件：**把服务端的刷屏日志从 MCDR 控制台隐去，同时完整保留服务端自己的日志文件。**

[![MCDR](https://img.shields.io/badge/MCDReforged-%3E%3D2.15-blue)](https://mcdreforged.com/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.9-blue)](https://www.python.org/)

---

## 为什么需要它

MCDR 会把服务端打印的每一行原样回显到控制台。绝大多数情况下这正是我们想要的，但有些服务端版本会反复打印不含任何有效信息的日志行，例如 Minecraft 26.3 的：

```
[Server thread/INFO]: Player Steve standing on air - force-sending blocks below
```

这是 26.3 新增的「幽灵方块自动修复」机制打的 INFO 日志，属于官方**误报**（Mojira [MC-311474](https://bugs.mojang.com/browse/MC-311474) / [MC-311727](https://bugs.mojang.com/browse/MC-311727)）——玩家正常跳跃和移动时也会触发，每人每 10 秒最多一条，会持续刷屏。

直接改服务端的 `log4j2.xml` 也能过滤，但那是**全局性**改动：改错了可能让服务端丢失重要日志，而且只对服务端生效。本插件换了个思路——**只在 MCDR 这一侧动手**。

## 它安全吗

**不会弄坏你的服务端**——这是本插件最看重的一点。

- **只是「不在控制台显示」，不是「把日志丢掉」。**
  被过滤的行依然会照常传给 MCDR 和你的其它插件。所以**即使规则写得过宽，
  也不会影响 MCDR 对「开服完成 / 关服 / 玩家进出」的判断**，最多只是控制台上看不到。
- **服务端自己的日志文件完全不受影响。**
  `server/logs/latest.log` 由服务端进程独立写入，原始记录一条不少。

所以：想查完整原始日志 → 翻 `server/logs/latest.log`；想让控制台 / 网页面板干净 → 用本插件。

> 技术细节：本插件用的是 `InfoActionFlag.hidden()` 而不是 `discarded()`——前者只摘掉控制台回显、
> 保留事件分发，后者是整条丢弃。上面的「不影响其它插件」正来源于此。

## 安装

1. 从 [Releases](../../releases) 下载 `ServerLogFilter-vX.Y.Z.mcdr`，或自行打包（见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)）
2. 放进对应 MCDR 实例的 `plugins/` 目录：

```
<MCDR目录>/plugins/ServerLogFilter-vX.Y.Z.mcdr
```

有多个子服就每个子服各放一份，配置互相独立。

3. 生效：

- MCDR 运行中：`!!MCDR reload plugin server_log_filter`
- 未运行：直接开服

## 配置

首次加载会自动生成 `config/server_log_filter/config.json`：

```json
{
    "language": "auto",
    "patterns": [
        "standing on air - force-sending blocks below"
    ],
    "log_matched_lines": false,
    "report_on_server_stop": true,
    "warn_about_stale_rules": true,
    "stale_rule_threshold": 3,
    "validate_patterns": true,
    "pattern_probe_timeout_ms": 25,
    "announce_config_upgrade": true,
    "announce_broken_config": true
}
```

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `language` | `string` | `"auto"` | 提示语言。`auto` = 跟随 MCDR 的 `language` 设置；也可写 `zh_cn` / `en_us`，见[界面语言](#界面语言简体中文--english) |
| `patterns` | `string[]` | 见上 | 正则规则列表。对每行日志的**正文**（MCDR 已剥掉 `[时间] [线程/级别]:` 前缀）做匹配，命中即隐去 |
| `log_matched_lines` | `bool` | `false` | 调试用。设为 `true` 会把被隐去的行以 INFO 级别写进 MCDR 日志，方便确认规则生效 |
| `report_on_server_stop` | `bool` | `true` | 服务端停止时，在 MCDR 日志里汇总本次共隐去多少行 |
| `warn_about_stale_rules` | `bool` | `true` | 某条规则连续多个开服周期零命中时给出提醒，见下节 |
| `stale_rule_threshold` | `int` | `3` | 连续多少个开服周期零命中才提醒。设为 `0` 可关闭提醒 |
| `validate_patterns` | `bool` | `true` | 载入时检查规则是否会导致正则灾难性回溯，见下节 |
| `pattern_probe_timeout_ms` | `int` | `25` | 上面那项检查的耗时上限（毫秒），一般不用改 |
| `announce_config_upgrade` | `bool` | `true` | 插件升级后配置被自动补齐时，在控制台说明新增了哪些选项，见下节 |
| `announce_broken_config` | `bool` | `true` | 配置文件写坏被备份并重置时，在控制台说明原因与备份路径，见下节 |

> **匹配方式是 `re.search`（包含匹配），不是全匹配。**
> 写 `standing on air` 就能命中整行，不需要加 `.*`。
> 反过来要注意：**正则里的 `.` 匹配任意字符**，想匹配字面点号请写 `\.`。

> 配置文件是 JSON，**语法上不支持写注释**——每一行的含义见上表。

### 界面语言（简体中文 / English）

插件说给人听的每一句话——控制台提示、日志、`!!logfilter` 的回执——都在语言文件里，
由 `language` 决定用哪一份：

| 取值 | 效果 |
|---|---|
| `auto`（默认） | **跟随 MCDR 当前的语言**（MCDR `config.yml` 里的 `language`）——你把 MCDR 设成中文，插件就说中文，无需任何配置 |
| `zh_cn` | 简体中文，无论 MCDR 设成什么 |
| `en_us` | English，无论 MCDR 设成什么 |

- 大小写与 `-` / `_` 都随意：`zh-CN`、`EN_us`、`en` 都能认。
- 没有对应语言文件时回落到 `en_us`，并在日志里提醒一句——静默换语言比报错更难查。
- MCDR 设成 `zh_tw` 而插件暂时没有繁体文件时会用 `zh_cn`，与 MCDR 自己的回落顺序一致。
- 配置文件写坏时，这个值只能从**你的文件原文**里读（配置已经解析失败了），
  所以写在语法错误之后也照样有效，那段「配置被重置」的播报会用它选语言。
- 改完用 `!!logfilter reload` 立即生效，不必重启服务端。

> **`auto` 读的是 MCDR 当前 `config.yml` 里的 `language`，不是什么内置默认值。**
> 也就是说你把 MCDR 设成 `zh_cn`，插件就说中文——**不需要配置任何东西**。
> 反过来，只有当你**从来没改过** MCDR 的语言时（MCDR 出厂默认是 `en_us`），
> 升级到 1.2.2 之后插件提示才会是英文；这时把 `language` 设成 `zh_cn` 即可。

**想加一门语言？** 复制 `en_us.json` 改成 `<语言代码>.json`（如 `ja_jp.json`、`zh_tw.json`）、
只翻译取值、跑一次 `pytest tests -k language`，然后提 PR 就行——**不需要改任何代码**。
文件都在 `server_log_filter/lang/`，同目录下的 `README.md` 是写给译者的说明。
`en_us` 是回落语言，请保持完整。

### 升级后配置会自动补齐，并告诉你补了什么

插件更新后，配置文件里缺少的新配置项会**自动按默认值补上**（这一步是 MCDR 已有的行为），
**你已有的取值不受影响**。补完之后插件会在日志里说明：

```
==================================================================
[ServerLogFilter] 配置已更新：本次新增了 4 个配置项，已按默认值写入
  · warn_about_stale_rules  （v1.1.0 加入）
      某条规则连续多个开服周期一次都没命中时，在下次开服完成之后给出提醒……
  · stale_rule_threshold  （v1.1.0 加入）
      连续多少个开服周期零命中才提醒。设成 0 可完全关闭这项提醒。
  ……
  配置文件：config/server_log_filter/config.json
  每一项的用途见下面「配置」一节的表格。
==================================================================
```

想让它安静点，把 `announce_config_upgrade` 设成 `false` 即可（见文末「关掉这些提示」）。

顺带一提：如果你把某个选项名**拼错了**，它也会被当作「多余的键」自动清掉——
配置因此不会长期留着一堆无效条目，但反过来说，拼错的选项不会报错而是静默消失，
改配置后建议用 `!!logfilter` 看一次状态确认生效。

### 配置文件写坏了会怎样

JSON 对格式很严格，手工往 `patterns` 里加规则时**漏掉一个逗号**就会解析失败。
插件会在把文件交给 MCDR 之前先自己检查一遍。发现问题时：

1. 出问题的文件被**原样改名**为 `config.json.old`（不会丢）
2. 用默认值生成一份新的 `config.json`，插件照常启动、照常过滤
3. 在后台明确告诉你发生了什么：

```
==================================================================
[ServerLogFilter] 配置文件无法解析，已重置为默认配置
  · 出问题的文件：config/server_log_filter/config.json
  · 原文件已备份为：config/server_log_filter/config.json.old
  · 具体原因：JSON 语法错误：Expecting ',' delimiter: line 4 column 9 (char 83)
  · 新的 config.json 已用默认值生成。请对照备份文件把内容修正后填回，
    或用 !!logfilter reload 在修好后立即重新加载。
  常见原因：数组（patterns）里每个元素之间都要有英文逗号 ","，
            且最后一项后面不要留多余的逗号。
==================================================================
```

**行号与列号直接指向出错位置**，照着改就行。改好之后用 `!!logfilter reload` 立即生效，
不必重启服务端。

> 为什么插件要自己做这件事：MCDR 在解析失败时会**直接用默认值覆盖原文件**。
> 若不先把原文件挪走，你写的规则就真的没了。
> `config.json.old` 只有一个备份位——再次出错会覆盖上一次的备份。

其他也会被当作「损坏」的情况：文件是空的、或者顶层不是一个 JSON 对象（比如整个文件被写成了数组）。

不想看到上面那段播报，把 `announce_broken_config` 设成 `false`。**备份动作照旧执行**，
被静默的只是消息；但「连备份都失败」那条错误属于数据丢失警告，不受开关影响，一律会报出来。

### 零命中提醒

配置里某条规则如果连续几个开服周期一次都没命中，它多半已经没用了——可能是正则写错了
（拼写 / 大小写 / 转义），也可能是它要过滤的那段日志已经不再产生（比如上游 bug 已被修复）。
插件会在下一次**开服完成（控制台出现 `Done`）之后**提醒一次：

```
==================================================================
[ServerLogFilter] 注意：有 2 条过滤规则连续 3 次及以上开服都没有命中
  · standing on air - force-sending blocks below
  · test12345
  这些规则通常只有两种可能：
    1. 正则写错了（拼写 / 大小写 / 转义，或与当前服务端版本不匹配）
    2. 它要过滤的日志已经不再产生（例如上游 bug 已被修复）
  建议：用 !!logfilter test <一行日志> 验证规则是否还匹配；
        确认无用后从配置的 patterns 里删除对应条目，
        若确认只是「本来就罕见」可调大 stale_rule_threshold，
        或用 !!logfilter reset 清空计数重新观察。
==================================================================
```

连续次数只写在标题里；每条规则**只列它自己**。只有当某条规则比阈值更久没命中时，
才会在它后面补一句 `（已连续 9 次零命中）`——那是标题里没有的信息。

几个要点：

- **提醒不影响性能。** 统计只在开服 / 关服两个时点做，逐行过滤的路径上没有任何额外开销。
  这个功能的作用是**保持配置干净**，不是提速。
- 统计结果存在 `config/server_log_filter/state.json`，与你自己写的 `config.json` 分开，
  不会被插件改动。
- **删掉某条规则后，它在 `state.json` 里的统计会立刻清掉**：改完配置执行
  `!!logfilter reload`（或 `!!MCDR reload plugin server_log_filter`）即可，
  不必等下一次开服 / 关服。插件会在日志里列出清掉了哪些规则。
  仍在配置里、只是暂时没命中的规则不受影响——只有**从 `patterns` 里真正删除**的规则才会被忘掉。
- 如果某条规则本来就命中得很少（比如一个月才触发一次），把 `stale_rule_threshold` 调大，
  或者用 `!!logfilter reset` 清空计数重新观察。
- 提醒只在**完成过启动**的周期上统计。服务端启动失败（比如 mod 报错）不会把规则刷成零命中。

### 正则安全检查

带嵌套量词的正则（例如 `(a+)+$`）在匹配失败时会指数级回溯，**单行**就能让 MCDR 卡住数百毫秒
甚至数秒，足以拖垮服务端。插件在载入时会用几个很短的探测串试跑每条规则，把这类规则拦下并报错：

```
[ServerLogFilter] 过滤规则存在灾难性回溯风险，已跳过: '(a+)+$' —— 在 22 字符的探测串上已耗时 90 ms（上限 25 ms）。
```

这条检查只在插件载入时跑一次，正常规则耗时为微秒级；被拦下的规则不会影响其它规则。
除非你很清楚自己在做什么，否则建议保持 `validate_patterns` 开启。

### 关掉这些提示

上面三类提示各有一个开关，默认全开：

| 想关掉的提示 | 开关 | 关掉之后 |
|---|---|---|
| 升级后配置被自动补齐的说明 | `announce_config_upgrade` | 配置**照旧**补齐并写回文件，只是不再播报 |
| 配置文件写坏被重置的说明 | `announce_broken_config` | 坏文件**照旧**备份成 `config.json.old` 并重建，只是不再播报 |
| 规则长期零命中的提醒 | `warn_about_stale_rules` | 统计**照旧**记录在 `state.json`，只是不再提醒 |

**开关只管「说不说」，不管「做不做」。** 三个安全动作（补齐配置、备份坏文件、记录统计）
都不会因为你把提示关掉而停摆。

两条使用上的细节：

- 写坏配置的那一刻，`announce_broken_config` 的值只能**从你自己的文件里读**（配置已经解析失败了，
  没有别的地方能拿到它）。所以这个开关即使写在语法错误的后面也照样有效，写成 `false` 就真的不播报。
- 配置一旦被自动重置，文件里就只剩默认值了，此时这个开关会**回到默认的 `true`**。
  也就是说：修好配置后它会重新打开，需要再次手动设成 `false`。

## 命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `!!logfilter` | user | 查看状态：规则数、各规则命中次数、连续零命中次数 |
| `!!logfilter list` | user | 同上 |
| `!!logfilter test <文本>` | user | 测试某行是否会被隐去，并指出命中哪条规则 |
| `!!logfilter reload` | admin | 重读配置文件并立即生效，无需重启；同时清除已从配置删除的规则的统计 |
| `!!logfilter reset` | admin | 清空「连续零命中」计数，重新开始观察 |

`!!logfilter test` 特别实用：把日志原文粘进去就能确认规则对不对，不用真等它触发。

## 用例：加更多规则

把要过滤的日志正文加进 `patterns` 数组即可（其余字段保持原值）：

```json
{
    "patterns": [
        "standing on air - force-sending blocks below",
        "Ignoring chat session from .* due to missing Services public key",
        "^\\[Async Chat Thread"
    ]
}
```

改完先 `!!logfilter test <文本>` 验证，再 `!!logfilter reload`。

规则写错不会导致插件崩溃——编译失败的规则会被跳过并在 MCDR 日志里给出警告，其余规则照常工作。

## 装好之后你会看到什么

控制台不再刷屏，同时服务端自己的日志一条不少：

```
[ServerLogFilter] 已启用 1 条日志过滤规则；命中后仅从 MCDR 控制台隐去，服务端日志不受影响
[ServerLogFilter] 本次运行共从 MCDR 控制台隐去 3 行服务端日志（服务端日志文件不受影响）
```

MCDR 是英文、或 `language` 设成 `en_us` 时，这两句会是对应的英文：

```
[ServerLogFilter] Enabled 1 log filter rule(s); matches are hidden from the MCDR console only, the server log is unaffected
[ServerLogFilter] Hidden 3 server log line(s) from the MCDR console this run (the server log file is unaffected)
```

那些被隐去的行，在 `server/logs/latest.log` 里**依然完整存在**。

## 遇到问题

| 现象 | 怎么办 |
|---|---|
| 规则好像没生效 | `!!logfilter test <一行日志>` 看它会不会被隐去；确认后 `!!logfilter reload` |
| 提示某条规则长期没命中 | 正则可能写错了，或者那段日志已不再产生；按提示里的建议处理 |
| 提示突然变成英文（或中文） | `language` 默认 `auto`，跟随 MCDR 的语言；想固定就写成 `zh_cn` 或 `en_us` |
| 配置文件被重置了 | 找 `config.json.old`，那是你原来的文件；报错信息会写清原因和出错的行号 |
| 想临时关掉过滤 | 把 `patterns` 清空，再 `!!logfilter reload` |

## 环境要求与兼容性

| 维度 | 支持情况 |
|---|---|
| MCDReforged | **>= 2.15.0**（2.15.0 / 2.15.7 / 2.16.0 已实测） |
| Python | >= 3.9（随 MCDR 自带，一般不用管） |
| Minecraft | **与 MC 版本无关**。1.16.5 / 1.19.4 / 1.20.1 / 1.20.6 / 1.21.8 / 1.21.11 / 26.1 / 26.2 / 26.3 均已用真实服务端实测通过 |

装到更早的 MCDR 上不会静默出错——MCDR 会直接提示版本不满足并拒绝加载。

过滤在 MCDR 侧完成（对服务端输出逐行匹配），所以不随 MC 版本变化。
唯一与版本相关的是**默认规则的目标日志**：`standing on air - force-sending blocks below`
只由 **MC 26.3** 产生。装到更早版本上插件照常工作，只是默认规则不会命中任何内容——
此时把你要过滤的日志填进 `patterns` 即可。

## 开发者

打包、测试、版本兼容性矩阵等技术细节见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)，
测试套件细则见 [tests/README.md](tests/README.md)。

## 许可证

[MIT](LICENSE)

"""Server Log Filter —— 在 MCDR 侧隐去服务端的刷屏日志。

MCDR 会把服务端打印的每一行原样回显到控制台，绝大多数情况下这正是我们想要的。
但有些服务端版本会反复打印某些不含任何有效信息的日志行（例如 MC 26.3 的
``Player <名字> standing on air - force-sending blocks below``，属于官方误报 bug
MC-311474 / MC-311727，每人每 10 秒最多一条）。本插件按用户提供的正则规则，
把匹配到的行从 MCDR 控制台隐去。

设计要点
--------
1. 用 ``InfoActionFlag.hidden()`` 而不是 ``discarded()``。
   前者只摘掉 ``echo_to_console``（控制台回显），保留 ``process``；后者是整条丢弃。
   保留 ``process`` 意味着这些行**依然会正常派发给 MCDR 的信息响应器和插件事件**，
   所以即使你写的规则过于宽泛，也不会破坏 MCDR 自身的「服务端启动完成 / 停止 /
   玩家进出」等状态检测——最多只是控制台上看不到而已。这是本插件选择 hidden 的
   主要原因，也是它比直接改 log4j2 更安全的地方。
2. 服务端自己的日志文件（``server/logs/latest.log``）由服务端进程用 log4j 自己写入，
   与本插件无关，**完全不受影响**，原始记录一条不少。
3. ``filter_server_info`` 运行在 MCDR 的主线程上（不是任务执行器线程），必须足够快。
   因此正则在载入时预编译，匹配时按顺序短路返回；只有命中时才加锁计数；
   ``hidden()`` 这个不变常量也在构造时算好，避免每次命中都重新构造。

关于「零命中提醒」
------------------
配置里的某条规则如果连续若干个开服周期一次都没命中，它大概率是**写错了**，
或者它要过滤的那段日志**已经不再产生**（例如上游 bug 被修复）。插件会在下一个开服周期
完成启动（控制台出现 ``Done``）之后给出一次醒目提醒，方便管理员清理配置。

需要说明的是：**这个提醒本身并不降低运行开销。** 实测本插件的过滤开销约为
每行 0.2 µs（1 条规则），不到 MCDR 自身解析同一行所需时间的十分之一；
即便在 1000 行/秒的极端突发下，每小时累计也只占 0.7 秒。
删掉一条用不到的规则所能省下的时间远低于测量噪声——这个提醒的价值在于**配置卫生**：
尽早发现写错的规则，以及发现「已经没必要再过滤了」的规则。
（上述数字可用 ``benchmarks/bench_filter.py`` 复现。）

真正会影响性能的是**写坏的正则**：带嵌套量词的表达式（如 ``(a+)+$``）会产生
灾难性回溯，实测单行就能耗掉数百毫秒乃至数秒，足以冻结 MCDR 主线程。
因此 ``validate_patterns`` 默认开启：载入时用短探测串检查每条规则，
把这类规则拦下并给出明确提示，而不是等它在生产环境里拖垮服务端。

运行要求
--------
MCDR **>= 2.15.0**。``InfoActionFlag``（``hidden()`` / ``discarded()`` 的区分）自 2.15.0
起才存在；2.14.x 及更早的 ``InfoFilter`` 只有「返回 False 即丢弃整条」的语义，
无法在保留事件分发的同时只摘掉控制台回显。低版本上插件会打印一条明确的错误并停用。
"""

import json
import os
import re
import threading
import time
from typing import Dict, List, Optional, Pattern, Tuple

from mcdreforged.api.command import GreedyText, Literal
from mcdreforged.api.rtext import RColor, RText, RTextList
from mcdreforged.api.types import (
    InfoFilter,
    PermissionLevel,
    PluginServerInterface,
)
from mcdreforged.api.utils import Serializable


DEFAULT_PATTERN = r"standing on air - force-sending blocks below"

# InfoActionFlag 是 MCDR 2.15.0 才引入的 API；2.14.x 及更早只有「丢弃整条」的
# InfoFilter 协议，无法实现本插件「只摘控制台回显、保留事件分发」的核心设计。
# 这里做容错导入，让低版本 MCDR 得到一句人能看懂的提示，而不是裸 ImportError。
MIN_MCDR_VERSION = "2.15.0"

# 存放「每个开服周期的命中数」等历史，与用户手写的 config.json 分开，互不干扰。
STATE_FILE_NAME = "state.json"

# 用户手写的配置文件。解析失败时先备份成 config.json.old 再让 MCDR 重建。
CONFIG_FILE_NAME = "config.json"
CONFIG_BACKUP_SUFFIX = ".old"

# 供提示信息里显示的可读路径（从 MCDR 根目录算起）。
# 由本文件所在目录名推导，因此不会与插件 id 漂移。
_CONFIG_FOLDER_DISPLAY = "config/{}".format(os.path.basename(os.path.dirname(os.path.abspath(__file__))))

try:
    from mcdreforged.api.types import InfoActionFlag
except ImportError:  # pragma: no cover - 仅在 MCDR < 2.15.0 上触发
    InfoActionFlag = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
#  配置与状态
# --------------------------------------------------------------------------


class Config(Serializable):
    patterns: List[str] = [DEFAULT_PATTERN]
    """正则规则列表。对服务端每行日志的「正文」用 re.search 匹配，命中即从控制台隐去。"""

    log_matched_lines: bool = False
    """调试用：把被隐去的行以 INFO 级别写进 MCDR 日志，便于确认规则是否生效。"""

    report_on_server_stop: bool = True
    """服务端停止时，在 MCDR 日志里汇总本次运行共隐去了多少行。"""

    warn_about_stale_rules: bool = True
    """某条规则连续多个开服周期零命中时，在下次启动完成后给出提醒。"""

    stale_rule_threshold: int = 3
    """连续多少个开服周期零命中才提醒。调大可以容忍「本来就罕见」的规则。"""

    validate_patterns: bool = True
    """载入时用短探测串检查灾难性回溯，把这类规则拦下。强烈建议保持开启。"""

    pattern_probe_timeout_ms: int = 25
    """单条规则单个探测串的耗时上限（毫秒）。正常规则约 1 µs，余量超过一万倍。"""

    announce_config_upgrade: bool = True
    """插件升级后配置被自动补齐时，在控制台说明本次新增了哪些选项。"""

    announce_broken_config: bool = True
    """配置文件解析失败、被备份并重置时，在控制台说明原因与备份路径。"""


# `announce_broken_config` 的选项名。写成常量供 _quarantine_broken_config 使用：
# 那一刻配置已经读不出来了，只能拿这个名字去文件原文里找。测试会钉住它必须
# 是 Config 里真实存在的字段，改名时不会悄悄失配。
BROKEN_CONFIG_NOTICE_OPTION = "announce_broken_config"


# --------------------------------------------------------------------------
#  配置项一览（用于升级提示）
#
#  这里只描述「每个选项是干什么的、哪个版本加的」，用于在插件更新后告诉管理员
#  配置里多了什么。配置文件本身不写任何注释——JSON 没有注释语法，而在文件里塞
#  自定义键会被 MCDR 当成「冗余键」在下一次保存时删掉（实测确认过），
#  为此专门维护一份说明字段反而把配置弄复杂了。选项的完整说明在 README 里。
# --------------------------------------------------------------------------

# 配置项 -> (加入的版本, 一句话说明)
CONFIG_DOC: Dict[str, Tuple[str, str]] = {
    "patterns": (
        "1.0.0",
        "要隐去的日志。正则列表，一行命中任意一条就不在 MCDR 控制台显示"
        "（服务端自身日志文件不受影响）。",
    ),
    "log_matched_lines": (
        "1.0.0",
        "true 时把每条被隐去的行也写进 MCDR 日志，用来确认规则真的生效。",
    ),
    "report_on_server_stop": (
        "1.0.0",
        "true 时在服务端停止时汇总一句「本次共隐去多少行」。",
    ),
    "warn_about_stale_rules": (
        "1.1.0",
        "true 时，某条规则连续多个开服周期零命中会提醒一次，便于清理失效规则。",
    ),
    "stale_rule_threshold": (
        "1.1.0",
        "连续多少个开服周期零命中才提醒。设成 0 可关闭该提醒。",
    ),
    "validate_patterns": (
        "1.1.0",
        "true 时在载入时拦下会拖死主线程的危险正则（如 (a+)+$）并在日志报错。",
    ),
    "pattern_probe_timeout_ms": (
        "1.1.0",
        "上面那项检查的耗时上限（毫秒）。正常规则约 1 µs，一般无需改动。",
    ),
    "announce_config_upgrade": (
        "1.2.1",
        "true 时，插件更新后若配置被自动补齐，会在控制台列出本次新增的选项。",
    ),
    "announce_broken_config": (
        "1.2.1",
        "true 时，配置文件解析失败被备份并重置时，会在控制台说明原因与备份路径。",
    ),
}


def _config_option_names() -> List[str]:
    """按声明顺序列出用户配置项。"""
    return list(Config.get_field_annotations())


class RuleState(Serializable):
    """某条规则的跨周期历史。按 pattern 字符串记录，与配置顺序无关。"""

    hits_last_session: int = 0
    zero_streak: int = 0
    total_hits: int = 0
    last_hit_session: int = 0


class State(Serializable):
    """持久化在 ``config/server_log_filter/state.json``，与用户手写的配置分开。"""

    session_index: int = 0
    rules: Dict[str, RuleState] = {}


# --------------------------------------------------------------------------
#  规则与过滤器
# --------------------------------------------------------------------------


class Rule:
    """一条已编译的过滤规则，附带命中计数。"""

    __slots__ = ("pattern", "regex", "count")

    def __init__(self, pattern: str, regex: Optional[Pattern] = None):
        self.pattern = pattern
        # 允许传入已编译的正则，避免「先编译校验、再编译一次」的重复开销
        self.regex: Pattern = regex if regex is not None else re.compile(pattern)
        self.count = 0


class ServerLogFilter(InfoFilter):
    """按规则把匹配的服务端输出从控制台隐去。"""

    def __init__(self, rules: List[Rule], logger, log_matched_lines: bool):
        # 用 tuple：迭代略快，且避免外部误改；替换时整体赋值，天然是原子操作。
        self._rules: Tuple[Rule, ...] = tuple(rules)
        self._logger = logger
        self._log_matched_lines = log_matched_lines
        # filter_server_info 在 MCDR 主线程被调用，而 !!logfilter 命令与
        # on_server_stop 在别的线程读取计数，所以计数需要加锁保护。
        self._lock = threading.Lock()
        self._total = 0
        # hidden() 每次调用都会新建一个 Flag 对象，而它其实是个不变常量。
        # 这里算一次、每次命中直接复用，省掉命中路径上的最后一次函数调用。
        self._hidden_flag = InfoActionFlag.hidden() if InfoActionFlag is not None else None

    @property
    def rules(self) -> Tuple[Rule, ...]:
        return self._rules

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    def carry_over_from(self, previous: Optional["ServerLogFilter"]) -> int:
        """插件热重载时，把上一份实例的命中数接过来。

        不接过来的话，一次开服周期会被重载切成两段，重载后的计数从 0 重新开始，
        停止时就会把一个正常命中的周期误判成「零命中」，进而触发假的零命中提醒。
        """
        if previous is None:
            return 0
        previous_counts = {rule.pattern: rule.count for rule in previous.rules if rule.count}
        carried = 0
        with self._lock:
            for rule in self._rules:
                hits = previous_counts.get(rule.pattern, 0)
                if hits:
                    rule.count = hits
                    carried += hits
            self._total = carried
        return carried

    def reload_rules(self, rules: List[Rule], log_matched_lines: bool) -> None:
        """就地替换规则（用于命令重载，无需重新注册 InfoFilter）。

        替换的是 tuple 引用，Python 中该操作是原子的；正在迭代旧 tuple 的调用会安全地
        跑完旧 tuple，不会看到半新半旧的状态。
        """
        with self._lock:
            self._rules = tuple(rules)
            self._log_matched_lines = log_matched_lines
            self._total = 0

    def reset_counters(self) -> None:
        with self._lock:
            self._total = 0
            for rule in self._rules:
                rule.count = 0

    def match(self, content: str) -> Optional[Rule]:
        """返回第一条命中的规则，没有则返回 None。"""
        for rule in self._rules:
            if rule.regex.search(content):
                return rule
        return None

    def filter_server_info(self, info) -> None:
        rules = self._rules
        if not rules:
            return

        content = info.content
        if content is None:
            content = getattr(info, "raw_content", None)
        if not content:
            return

        rule = self.match(content)
        if rule is None:
            return

        # hidden() = send_to_server | process，即「不回显但照常分发」。
        # 注意：这里**不能** return False —— 那等价于 discarded()，会把整条信息
        # 丢掉，导致 MCDR 的启动 / 停止 / 玩家进出检测失效。
        info.action_flag = self._hidden_flag

        with self._lock:
            rule.count += 1
            self._total += 1

        if self._log_matched_lines:
            self._logger.info("[ServerLogFilter] 已隐去: {}".format(content))


# --------------------------------------------------------------------------
#  规则装载：编译 + 灾难性回溯探测
# --------------------------------------------------------------------------

# 探测串：短到即使最坏情况也只花几百毫秒，又要长到足以把危险模式与正常模式
# 拉开几个数量级。实测 a*22 与 a*22+"!" 能拦下常见嵌套量词模式（单串 200~450 ms），
# 而正常规则在同一批探测串上最坏仅 1.3 µs —— 余量约两万倍。
PROBE_SUBJECTS = (
    "a" * 22,
    "a" * 22 + "!",
    "0" * 22 + "!",
    "a " * 11 + "!",
)


def _probe_pattern(regex: Pattern, budget_ms: int) -> Optional[str]:
    """检查正则是否存在灾难性回溯；正常返回 None，可疑则返回说明文字。

    带嵌套量词的表达式（``(a+)+$`` 之类）在匹配失败时会指数级回溯。这类规则每遇到
    一行近似日志就会冻结 MCDR 主线程：实测 ``(a+)+$`` 在 27 个字符上要 3 秒。
    用短探测串就能提前发现——因为耗时随长度按 2^n 增长，短串上已经明显超预算的模式，
    到了真实日志行长度只会更糟。
    """
    budget = budget_ms / 1000.0
    for subject in PROBE_SUBJECTS:
        start = time.perf_counter()
        regex.search(subject)
        cost = time.perf_counter() - start
        if cost > budget:
            return "在 {} 字符的探测串上已耗时 {:.0f} ms（上限 {} ms）".format(
                len(subject), cost * 1000, budget_ms
            )
    return None


def _build_rules(
    server: PluginServerInterface,
    patterns: List[str],
    validate: bool = True,
    probe_budget_ms: int = 25,
) -> List[Rule]:
    """编译规则；单条写错不影响其他规则，只在日志里报错或警告。"""
    rules: List[Rule] = []
    for raw in patterns:
        pattern = (raw or "").strip()
        if not pattern:
            continue
        try:
            regex = re.compile(pattern)
        except re.error as error:
            server.logger.warning(
                "过滤规则编译失败，已跳过: {!r} ({})".format(pattern, error)
            )
            continue
        if validate:
            problem = _probe_pattern(regex, probe_budget_ms)
            if problem is not None:
                server.logger.error(
                    "过滤规则存在灾难性回溯风险，已跳过: {!r} —— {}。"
                    "这条规则几乎每行都会让 MCDR 主线程卡顿，请改写成普通子串或"
                    "去掉嵌套量词后重试。".format(pattern, problem)
                )
                continue
        rules.append(Rule(pattern, regex))
    return rules


# --------------------------------------------------------------------------
#  模块级状态
# --------------------------------------------------------------------------

_config: Optional[Config] = None
_log_filter: Optional[ServerLogFilter] = None
_state: Optional[State] = None
# on_load 拿到的 PluginServerInterface。注意不能从命令的 source 取：那条路拿到的是
# ServerInterface，并没有 load_config_simple()（它只属于 PluginServerInterface）。
_server: Optional[PluginServerInterface] = None

# 本轮服务端是否真的完成过启动。只有完成过启动的周期才计入「零命中」统计——
# 否则服务端连续几次启动失败（比如 mod 报错）会把所有规则刷成零命中，产生假提醒。
_session_reached_startup = False


# 本轮读到的配置里缺少哪些选项（= 插件升级后新增的），由 _config_migrator 填写，
# _announce_new_options 负责报告。
_newly_added_options: List[str] = []

# 本次加载时配置文件是否因为写坏而被自动重置过。重置后 patterns 是默认值而非用户
# 真实配置，据此清理 state.json 会误删用户的历史统计，所以 _forget_orphans 会跳过。
_config_was_reset = False


def _log_summary(server: PluginServerInterface, rules: List[Rule]) -> None:
    if rules:
        server.logger.info(
            "已启用 {} 条日志过滤规则；命中后仅从 MCDR 控制台隐去，服务端日志不受影响".format(
                len(rules)
            )
        )
    else:
        server.logger.warning("未启用任何过滤规则（patterns 为空或全部编译失败）")


def _load_state(server: PluginServerInterface) -> State:
    return server.load_config_simple(
        file_name=STATE_FILE_NAME, target_class=State, echo_in_console=False
    )


def _save_state(server: PluginServerInterface, state: State) -> None:
    server.save_config_simple(state, file_name=STATE_FILE_NAME)


def _config_migrator(read_data) -> bool:
    """``load_config_simple(data_processor=...)`` 钩子：记录哪些选项是本次新添的。

    读到的配置里缺少的选项，正是插件升级后新增的那些；MCDR 会按默认值把它们补上
    并写回文件（用户已有的取值不受影响），我们只负责把这件事说出来。

    始终返回 False：补写由 MCDR 自己完成，这里不需要它额外再写一次。
    """
    global _newly_added_options

    if not isinstance(read_data, dict):
        return False

    present = set(read_data)
    _newly_added_options = [
        name for name in _config_option_names() if name not in present
    ]
    return False


def _announce_new_options(server: PluginServerInterface) -> None:
    """插件升级后，把新增了哪些配置项告诉管理员。"""
    if not _newly_added_options:
        return
    lines = [
        "",
        "=" * 66,
        "[ServerLogFilter] 配置已更新：本次新增了 {} 个配置项，已按默认值写入".format(
            len(_newly_added_options)
        ),
    ]
    for name in _newly_added_options:
        since, desc = CONFIG_DOC.get(name, ("?", "（未记录说明）"))
        lines.append("  · {}  （v{} 加入）".format(name, since))
        lines.append("      {}".format(desc))
    lines += [
        "  配置文件：config/server_log_filter/config.json",
        "  各项含义见 README 的「配置」一节。",
        "=" * 66,
        "",
    ]
    server.logger.info("\n".join(lines))


def _load_config(server: PluginServerInterface) -> Config:
    """读取配置：自动补齐新增选项，并在有新增时给出提示。

    补齐动作本身照常进行、不受任何开关影响——`announce_config_upgrade` 只管
    「说不说」。把它关掉不会让配置少补一个键，只是你不再收到那段说明。
    """
    global _newly_added_options, _config_was_reset
    _newly_added_options = []
    _config_was_reset = _quarantine_broken_config(server, CONFIG_FILE_NAME) is not None
    config = server.load_config_simple(target_class=Config, data_processor=_config_migrator)
    if config.announce_config_upgrade:
        _announce_new_options(server)
    return config


def _raw_bool_option(raw: str, name: str) -> Optional[bool]:
    """从一段（可能根本不是合法 JSON 的）文本里尽力读出某个布尔选项。

    **只在配置文件已经写坏时才需要**：那一刻用户真正的设置只存在于这份原文里
    （文件马上要被备份、配置马上要被重置成默认值），正规解析器已经派不上用场。
    于是退化成一次 ``"name": true|false`` 的文本查找——顶层的缩进与顺序怎么写
    都不影响，找不到就返回 ``None``，由调用方决定默认行为。

    找不到与 ``true`` 是一回事：默认开启，照常提示。
    """
    match = re.search(
        r'"{}"\s*:\s*(true|false)\b'.format(re.escape(name)),
        raw,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return match.group(1).lower() == "true"


def _quarantine_broken_config(
    server: PluginServerInterface, file_name: str
) -> Optional[str]:
    """配置文件解析失败时，先把它挪到 ``<name>.old``。

    返回原始的解析错误说明（一切正常时返回 None），供上层提示用。

    **为什么要在交给 MCDR 之前自己先看一遍：** ``load_config_simple`` 默认
    ``failure_policy='regen'``，解析失败时它会直接用默认值把原文件**覆盖**掉。
    用户辛苦写的规则就此消失，而且（在加上这个检查之前）不会有任何提示——
    最常见的触发方式就是往 ``patterns`` 数组里加规则时漏了一个逗号。

    先把原文件挪走，用户就能照着自己写的那份把内容抄回来。

    ``announce_broken_config`` 只决定要不要把上面这段说明**说出来**，备份动作
    本身永远执行。开关的值只能从原文里读（见 ``_raw_bool_option``）——此刻
    配置已经解析失败，没有别的地方能拿到用户当初的设置。
    """
    path = os.path.join(server.get_data_folder(), file_name)
    if not os.path.isfile(path):
        return None

    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        # 读不动（权限之类）就交给 MCDR 自己处理，不在这里抢着报错
        return None

    try:
        data = json.loads(raw)
    except ValueError as error:
        reason = "JSON 语法错误：{}".format(error)
    else:
        if isinstance(data, dict):
            return None
        reason = "顶层应为 JSON 对象，实际是 {}".format(type(data).__name__)

    backup = path + CONFIG_BACKUP_SUFFIX
    try:
        os.replace(path, backup)
    except OSError as error:
        # 连备份都失败，意味着原文件真的会被 MCDR 覆盖掉——这是数据丢失，
        # 不属于「提示」，因此不受 announce_broken_config 开关影响，一律报出来。
        server.logger.error(
            "[ServerLogFilter] 配置文件无法解析，但备份到 {}.old 也失败了（{}）。"
            "原文件可能已被重置，请注意保存。".format(file_name, error)
        )
        return reason

    if _raw_bool_option(raw, BROKEN_CONFIG_NOTICE_OPTION) is False:
        # 用户把这盏灯关掉了。备份照做（上面那步），只是不再播报这一段。
        return reason

    server.logger.error(
        "\n".join(
            [
                "",
                "=" * 66,
                "[ServerLogFilter] 配置文件无法解析，已重置为默认配置",
                "  · 出问题的文件：{}/{}".format(_CONFIG_FOLDER_DISPLAY, file_name),
                "  · 原文件已备份为：{}/{}{}".format(
                    _CONFIG_FOLDER_DISPLAY, file_name, CONFIG_BACKUP_SUFFIX
                ),
                "  · 具体原因：{}".format(reason),
                "  · 新的 {} 已用默认值生成。请对照备份文件把内容修正后填回，".format(file_name),
                "    或用 !!logfilter reload 在修好后立即重新加载。",
                "  常见原因：数组（patterns）里每个元素之间都要有英文逗号 \",\"，",
                "            且最后一项后面不要留多余的逗号。",
                "=" * 66,
                "",
            ]
        )
    )
    return reason


def _orphan_patterns(state: State, patterns: List[str]) -> List[str]:
    """列出 state.json 里「配置中已经没有了」的规则。

    对照的是**配置原文**，而不是编译成功的规则：一条仍然写在配置里、只是当前
    编译失败的规则（正则写错、或触发灾难性回溯保护被跳过）不该被当成已删除——
    否则把错别字改好之后，它的历史统计已经从零开始了。
    """
    configured = set()
    for raw in patterns:
        pattern = (raw or "").strip()
        if pattern:
            configured.add(pattern)
    return [pattern for pattern in state.rules if pattern not in configured]


def _forget_orphans(state: State, patterns: List[str]) -> List[str]:
    """就地删掉孤立的规则统计，返回被删掉的 pattern（不写盘）。

    **配置刚因写坏而被自动重置时不清理。** 那一刻 ``patterns`` 是临时生成的默认值，
    并不是用户真实配置；照此清理会把用户所有规则的历史一并抹掉，而这些统计在用户
    对照 ``config.json.old`` 把内容改回来之后本可以继续用。
    """
    if _config_was_reset:
        return []
    removed = _orphan_patterns(state, patterns)
    for pattern in removed:
        del state.rules[pattern]
    return removed


def _prune_state(
    server: PluginServerInterface, state: State, patterns: List[str]
) -> List[str]:
    """把「配置里已删除」的规则统计从 state.json 里清掉。

    只在真的清掉了东西时才写盘：没有变化就一个字节都不动，避免每次重载都白白
    改一次文件的修改时间。

    这在**重载时**就要做，而不是等到服务端停止：``_update_state_after_session``
    虽然也会清理，但它只在一个开服周期正常结束（``on_server_stop``）时才跑。
    管理员删掉一条规则后立刻看 ``state.json``，看到的是那份已经没用的旧统计。
    """
    removed = _forget_orphans(state, patterns)
    if removed:
        _save_state(server, state)
    return removed


def _announce_pruned(server: PluginServerInterface, removed: List[str]) -> None:
    """告诉管理员 state.json 里清掉了哪些统计（配置里已删除的规则）。"""
    if not removed:
        return
    lines = [
        "[ServerLogFilter] 已从 state.json 清除 {} 条规则统计（它们已不在配置的 patterns 中）".format(
            len(removed)
        ),
    ]
    lines += ["  · {}".format(pattern) for pattern in removed]
    server.logger.info("\n".join(lines))


def _update_state_after_session(
    server: PluginServerInterface, state: State, rules: List[Rule], patterns: List[str]
) -> None:
    """一次开服周期结束时，把命中数写进历史并推进「连续零命中」计数。"""
    state.session_index += 1
    session_index = state.session_index

    for rule in rules:
        entry = state.rules.get(rule.pattern)
        if entry is None:
            entry = RuleState()
            state.rules[rule.pattern] = entry
        entry.hits_last_session = rule.count
        entry.total_hits += rule.count
        if rule.count > 0:
            entry.zero_streak = 0
            entry.last_hit_session = session_index
        else:
            entry.zero_streak += 1

    # 已从配置中移除的规则一并清掉，避免状态文件无限增长
    _forget_orphans(state, patterns)

    _save_state(server, state)


def _collect_stale(
    state: State, rules: List[Rule], threshold: int
) -> List[Tuple[str, RuleState]]:
    """找出连续零命中已达阈值的规则（只关心当前配置里还在的）。"""
    stale: List[Tuple[str, RuleState]] = []
    for rule in rules:
        entry = state.rules.get(rule.pattern)
        if entry is not None and entry.zero_streak >= threshold:
            stale.append((rule.pattern, entry))
    return stale


def _emit_stale_warning(
    server: PluginServerInterface,
    stale: List[Tuple[str, RuleState]],
    threshold: int,
) -> None:
    """在开服完成（Done）之后给出一次醒目提醒。

    整段拼成一条消息再发，避免控制台被几十行带时间戳的独立 WARNING 淹没。

    每条规则**只列出它的模式**：连续零命中的次数已经写在标题里了，
    逐条重复一遍同样的句子只会把有用信息淹掉。只有当某条规则自己的
    零命中次数比阈值更高（也就是它比标题说的更久没动静）时，
    才在它后面补一句说明——那条信息是标题里没有的。
    """
    lines = [
        "",
        "=" * 66,
        "[ServerLogFilter] 注意：有 {} 条过滤规则连续 {} 次及以上开服都没有命中".format(
            len(stale), threshold
        ),
    ]
    for pattern, entry in stale:
        suffix = ""
        if entry.zero_streak > threshold:
            suffix = "   （已连续 {} 次零命中）".format(entry.zero_streak)
        lines.append("  · {}{}".format(pattern, suffix))
    lines += [
        "  这些规则通常只有两种可能：",
        "    1. 正则写错了（拼写 / 大小写 / 转义，或与当前服务端版本不匹配）",
        "    2. 它要过滤的日志已经不再产生（例如上游 bug 已被修复）",
        "  建议：用 !!logfilter test <一行日志> 验证规则是否还匹配；",
        "        确认无用后从配置的 patterns 里删除对应条目，",
        "        若确认只是「本来就罕见」可调大 stale_rule_threshold，",
        "        或用 !!logfilter reset 清空计数重新观察。",
        "  （提醒本身不影响性能，只是为了保持配置干净。）",
        "=" * 66,
        "",
    ]
    server.logger.warning("\n".join(lines))


# --------------------------------------------------------------------------
#  生命周期
# --------------------------------------------------------------------------


def on_load(server: PluginServerInterface, prev_module) -> None:
    global _config, _log_filter, _state, _server, _session_reached_startup

    _server = server

    if InfoActionFlag is None:
        # 低版本 MCDR 上主动给出可读提示，并放弃注册，避免后续每行日志都抛异常。
        server.logger.error(
            "Server Log Filter 需要 MCDR >= {}，当前版本不支持 InfoActionFlag，插件已停用。"
            "请升级 MCDR 后重试。".format(MIN_MCDR_VERSION)
        )
        return

    _config = _load_config(server)
    _state = _load_state(server)
    rules = _build_rules(
        server,
        _config.patterns,
        validate=_config.validate_patterns,
        probe_budget_ms=_config.pattern_probe_timeout_ms,
    )
    _log_filter = ServerLogFilter(rules, server.logger, _config.log_matched_lines)

    # 配置里删掉的规则，其统计立刻清掉（重载后就该消失，不必等到服务端停止）。
    _announce_pruned(server, _prune_state(server, _state, _config.patterns))

    # 热重载：把上一份实例已有的命中数接过来，避免把一次开服周期切成两段后误判零命中。
    carried = _log_filter.carry_over_from(getattr(prev_module, "_log_filter", None))
    if carried:
        server.logger.info("已从重载前的实例接续 {} 条命中计数".format(carried))

    # 真实的重载会构造一个全新的模块，本模块的 _session_reached_startup 会回到 False。
    # 若服务端当时正在运行，这次重载就会把当前周期从统计里抹掉，所以要把状态一并接续。
    if getattr(prev_module, "_session_reached_startup", False):
        _session_reached_startup = True

    # InfoFilter 必须在插件加载阶段（on_load）注册。MCDR 会在本插件卸载时
    # 自动移除它，无需手动反注册。
    server.register_info_filter(_log_filter)

    server.register_help_message(
        "!!logfilter", "服务端日志过滤器：查看状态 / 测试 / 重载规则"
    )
    server.register_command(
        Literal("!!logfilter")
        .runs(_show_status)
        .then(Literal("list").runs(_show_status))
        .then(
            Literal("reload")
            .requires(lambda src: src.has_permission(PermissionLevel.ADMIN))
            .runs(_reload)
        )
        .then(
            Literal("reset")
            .requires(lambda src: src.has_permission(PermissionLevel.ADMIN))
            .runs(_reset_streaks)
        )
        .then(Literal("test").then(GreedyText("text").runs(_test_line)))
    )

    _log_summary(server, rules)


def _apply_config(server: PluginServerInterface) -> Tuple[List[Rule], List[str]]:
    """重读配置并就地替换规则，不重新注册命令与过滤器。

    返回 ``(规则列表, 被清掉的旧规则统计)``，后者供调用方提示管理员。
    """
    global _config
    _config = _load_config(server)
    rules = _build_rules(
        server,
        _config.patterns,
        validate=_config.validate_patterns,
        probe_budget_ms=_config.pattern_probe_timeout_ms,
    )
    if _log_filter is not None:
        _log_filter.reload_rules(rules, _config.log_matched_lines)
    pruned = [] if _state is None else _prune_state(server, _state, _config.patterns)
    return rules, pruned


def on_server_start(server: PluginServerInterface) -> None:
    # 每轮服务端从零计数，这样停止时的汇总才是「本次运行」的准确数字。
    global _session_reached_startup
    _session_reached_startup = False
    if _log_filter is not None:
        _log_filter.reset_counters()


def on_server_startup(server: PluginServerInterface) -> None:
    """服务端完成启动（控制台出现 ``Done``）时触发。

    零命中提醒放在这里而不是 on_server_start：开服瞬间日志量很大，
    提醒会被淹没；等 Done 之后再报才醒目。
    """
    global _session_reached_startup
    _session_reached_startup = True

    if (
        not _config.warn_about_stale_rules
        or _state is None
        or _log_filter is None
        or _config.stale_rule_threshold <= 0
    ):
        return

    stale = _collect_stale(_state, list(_log_filter.rules), _config.stale_rule_threshold)
    if stale:
        _emit_stale_warning(server, stale, _config.stale_rule_threshold)


def on_server_stop(server: PluginServerInterface, server_return_code: int) -> None:
    global _session_reached_startup

    if _log_filter is None or _config is None:
        return

    # 只有真正完成过启动的周期才计入统计，避免「启动失败」污染零命中计数。
    if _session_reached_startup and _state is not None:
        try:
            _update_state_after_session(
                server, _state, list(_log_filter.rules), _config.patterns
            )
        except Exception:  # noqa: BLE001 - 状态写盘失败不应影响服务端收尾
            server.logger.exception("写入日志过滤统计（state.json）失败")
    _session_reached_startup = False

    if not _config.report_on_server_stop:
        return
    total = _log_filter.total
    if total:
        server.logger.info(
            "本次运行共从 MCDR 控制台隐去 {} 行服务端日志（服务端日志文件不受影响）".format(
                total
            )
        )


# --------------------------------------------------------------------------
#  命令实现
# --------------------------------------------------------------------------


def _show_status(source) -> None:
    if _log_filter is None:
        source.reply(RText("日志过滤器尚未初始化", RColor.red))
        return

    rules = _log_filter.rules
    parts = RTextList(
        RText("------ Server Log Filter ------", RColor.aqua),
        "\n",
        RText("规则数: ", RColor.gray),
        RText(str(len(rules)), RColor.green),
        RText("    本次已隐去: ", RColor.gray),
        RText(str(_log_filter.total), RColor.green),
        RText(" 行", RColor.gray),
    )

    if not rules:
        parts.append("\n")
        parts.append(RText("（无规则，请在配置文件 patterns 里填写）", RColor.yellow))

    threshold = _config.stale_rule_threshold if _config is not None else 0
    for index, rule in enumerate(rules, start=1):
        parts.append("\n")
        parts.append(RText(" [{:>2}] ".format(index), RColor.dark_gray))
        parts.append(RText("{:>5} 次  ".format(rule.count), RColor.green))
        parts.append(RText(rule.pattern, RColor.white))
        entry = _state.rules.get(rule.pattern) if _state is not None else None
        if entry is not None and entry.zero_streak:
            if threshold and entry.zero_streak >= threshold:
                parts.append(RText("   ⚠ 连续 {} 次开服零命中".format(entry.zero_streak), RColor.red))
            else:
                parts.append(
                    RText("   （连续 {} 次零命中）".format(entry.zero_streak), RColor.dark_gray)
                )

    if _state is not None and _state.session_index:
        parts.append("\n")
        parts.append(
            RText("已统计 {} 个开服周期".format(_state.session_index), RColor.dark_gray)
        )

    parts.append("\n")
    parts.append(
        RText(
            "提示: 服务端自身日志不受影响；被隐去的行仍会正常派发给插件事件。",
            RColor.dark_gray,
        )
    )
    source.reply(parts)


def _reload(source) -> None:
    # 必须用插件自己的 PluginServerInterface，而不是 source.get_server()：
    # 后者返回 ServerInterface，没有 load_config_simple()。
    if _server is None:
        source.reply(RText("插件尚未初始化，无法重载", RColor.red))
        return
    rules, pruned = _apply_config(_server)
    _log_summary(_server, rules)
    _announce_pruned(_server, pruned)
    reply = RTextList(
        RText("已重载日志过滤规则", RColor.green),
        RText("（当前 {} 条）".format(len(rules)), RColor.gray),
    )
    if pruned:
        reply.append(
            RText(
                "；已清除 {} 条已删除规则的统计".format(len(pruned)),
                RColor.gray,
            )
        )
    source.reply(reply)


def _reset_streaks(source) -> None:
    """清空「连续零命中」计数，用于「确认这条规则本来就罕见，别再来烦我」。"""
    if _server is None or _state is None or _log_filter is None:
        source.reply(RText("插件尚未初始化，无法重置", RColor.red))
        return
    cleared = 0
    for rule in _log_filter.rules:
        entry = _state.rules.get(rule.pattern)
        if entry is not None and entry.zero_streak:
            entry.zero_streak = 0
            cleared += 1
    try:
        _save_state(_server, _state)
    except Exception:  # noqa: BLE001
        _server.logger.exception("写入 state.json 失败")
    source.reply(
        RTextList(
            RText("已重置连续零命中计数", RColor.green),
            RText("（涉及 {} 条规则）".format(cleared), RColor.gray),
        )
    )


def _test_line(source, context) -> None:
    if _log_filter is None:
        source.reply(RText("日志过滤器尚未初始化", RColor.red))
        return

    text = context["text"]
    rule = _log_filter.match(text)
    if rule is None:
        source.reply(
            RTextList(
                RText("不会被隐去", RColor.green),
                RText("    该行不匹配任何规则", RColor.gray),
            )
        )
    else:
        source.reply(
            RTextList(
                RText("会被隐去", RColor.yellow),
                RText("    命中规则: ", RColor.gray),
                RText(rule.pattern, RColor.white),
            )
        )

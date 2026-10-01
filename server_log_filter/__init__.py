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
   因此正则在载入时预编译，匹配时按顺序短路返回；只有命中时才加锁计数。
"""

import re
import threading
from typing import List, Optional, Pattern

from mcdreforged.api.command import GreedyText, Literal
from mcdreforged.api.rtext import RColor, RText, RTextList
from mcdreforged.api.types import (
    InfoActionFlag,
    InfoFilter,
    PermissionLevel,
    PluginServerInterface,
)
from mcdreforged.api.utils import Serializable


DEFAULT_PATTERN = r"standing on air - force-sending blocks below"


class Config(Serializable):
    patterns: List[str] = [DEFAULT_PATTERN]
    """正则规则列表。对服务端每行日志的「正文」用 re.search 匹配，命中即从控制台隐去。"""

    log_matched_lines: bool = False
    """调试用：把被隐去的行以 INFO 级别写进 MCDR 日志，便于确认规则是否生效。"""

    report_on_server_stop: bool = True
    """服务端停止时，在 MCDR 日志里汇总本次运行共隐去了多少行。"""


class Rule:
    """一条已编译的过滤规则，附带命中计数。"""

    __slots__ = ("pattern", "regex", "count")

    def __init__(self, pattern: str):
        self.pattern = pattern
        self.regex: Pattern = re.compile(pattern)
        self.count = 0


class ServerLogFilter(InfoFilter):
    """按规则把匹配的服务端输出从控制台隐去。"""

    def __init__(self, rules: List[Rule], logger, log_matched_lines: bool):
        self._rules = rules
        self._logger = logger
        self._log_matched_lines = log_matched_lines
        # filter_server_info 在 MCDR 主线程被调用，而 !!logfilter 命令在任务执行器
        # 线程读取计数，所以计数需要加锁保护。
        self._lock = threading.Lock()
        self._total = 0

    @property
    def rules(self) -> List[Rule]:
        return self._rules

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    def reload_rules(self, rules: List[Rule], log_matched_lines: bool) -> None:
        """就地替换规则（用于命令重载，无需重新注册 InfoFilter）。

        替换的是列表引用，Python 中该操作是原子的；正在迭代旧列表的调用会安全地
        跑完旧列表，不会看到半新半旧的状态。
        """
        with self._lock:
            self._rules = rules
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
        info.action_flag = InfoActionFlag.hidden()

        with self._lock:
            rule.count += 1
            self._total += 1

        if self._log_matched_lines:
            self._logger.info("[ServerLogFilter] 已隐去: {}".format(content))


# --------------------------------------------------------------------------
#  模块级状态
# --------------------------------------------------------------------------

_config: Optional[Config] = None
_log_filter: Optional[ServerLogFilter] = None
# on_load 拿到的 PluginServerInterface。注意不能从命令的 source 取：那条路拿到的是
# ServerInterface，并没有 load_config_simple()（它只属于 PluginServerInterface）。
_server: Optional[PluginServerInterface] = None


def _build_rules(server: PluginServerInterface, patterns: List[str]) -> List[Rule]:
    """编译规则；单条写错不影响其他规则，只在日志里报警告。"""
    rules: List[Rule] = []
    for raw in patterns:
        pattern = (raw or "").strip()
        if not pattern:
            continue
        try:
            rules.append(Rule(pattern))
        except re.error as error:
            server.logger.warning(
                "过滤规则编译失败，已跳过: {!r} ({})".format(pattern, error)
            )
    return rules


def _log_summary(server: PluginServerInterface, rules: List[Rule]) -> None:
    if rules:
        server.logger.info(
            "已启用 {} 条日志过滤规则；命中后仅从 MCDR 控制台隐去，服务端日志不受影响".format(
                len(rules)
            )
        )
    else:
        server.logger.warning("未启用任何过滤规则（patterns 为空或全部编译失败）")


def on_load(server: PluginServerInterface, prev_module) -> None:
    global _config, _log_filter, _server

    _server = server
    _config = server.load_config_simple(target_class=Config)
    rules = _build_rules(server, _config.patterns)
    _log_filter = ServerLogFilter(rules, server.logger, _config.log_matched_lines)

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
        .then(Literal("test").then(GreedyText("text").runs(_test_line)))
    )

    _log_summary(server, rules)


def _apply_config(server: PluginServerInterface) -> List[Rule]:
    """重读配置并就地替换规则，不重新注册命令与过滤器。"""
    global _config
    _config = server.load_config_simple(target_class=Config)
    rules = _build_rules(server, _config.patterns)
    if _log_filter is not None:
        _log_filter.reload_rules(rules, _config.log_matched_lines)
    return rules


def on_server_start(server: PluginServerInterface) -> None:
    # 每轮服务端从零计数，这样停止时的汇总才是「本次运行」的准确数字。
    if _log_filter is not None:
        _log_filter.reset_counters()


def on_server_stop(server: PluginServerInterface, server_return_code: int) -> None:
    if _log_filter is None or _config is None or not _config.report_on_server_stop:
        return
    if _log_filter.total:
        server.logger.info(
            "本次运行共从 MCDR 控制台隐去 {} 行服务端日志（服务端日志文件不受影响）".format(
                _log_filter.total
            )
        )


# --------------------------------------------------------------------------
#  命令实现
# --------------------------------------------------------------------------


def _show_status(source) -> None:
    if _log_filter is None:
        source.reply(RText("日志过滤器尚未初始化", RColor.red))
        return

    parts = RTextList(
        RText("------ Server Log Filter ------", RColor.aqua),
        "\n",
        RText("规则数: ", RColor.gray),
        RText(str(len(_log_filter.rules)), RColor.green),
        RText("    本次已隐去: ", RColor.gray),
        RText(str(_log_filter.total), RColor.green),
        RText(" 行", RColor.gray),
    )

    if not _log_filter.rules:
        parts.append("\n")
        parts.append(RText("（无规则，请在配置文件 patterns 里填写）", RColor.yellow))

    for index, rule in enumerate(_log_filter.rules, start=1):
        parts.append("\n")
        parts.append(RText(" [{:>2}] ".format(index), RColor.dark_gray))
        parts.append(RText("{:>5} 次  ".format(rule.count), RColor.green))
        parts.append(RText(rule.pattern, RColor.white))

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
    rules = _apply_config(_server)
    _log_summary(_server, rules)
    source.reply(
        RTextList(
            RText("已重载日志过滤规则", RColor.green),
            RText("（当前 {} 条）".format(len(rules)), RColor.gray),
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

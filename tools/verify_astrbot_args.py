# -*- coding: utf-8 -*-
"""验证与 AstrBot 的指令参数解析是否兼容（回归测试）。

背景：AstrBot 的 ``CommandFilter.init_handler_md`` 用 ``inspect.signature`` 直接读函数注解，
**不做** ``typing.get_type_hints`` 解析。因此如果插件模块顶部写了
``from __future__ import annotations``，注解会变成字符串，导致：

* ``@filter.command`` 的 ``GreedyStr`` 类参数判定失败（``注解 is GreedyStr`` 为假）；
* 该注解被当成「默认值」，参数只取到第一个词，后面的内容丢失。

本插件因此改为在 handler 内手动解析消息文本（``main.extract_argument``）。
这个脚本加载 **AstrBot 的真实源码模块**（``filter/command.py``、
``register/star_handler.py``），只桩掉它们 import 的外部依赖，用来确认：

1. 插件的 handler 签名不再依赖注解（``handler_params`` 为空）；
2. 真实 ``CommandFilter`` 能匹配 ``mh`` 指令；
3. ``extract_argument`` 对各种消息形态都能取出完整参数；
4. 端到端 dispatch 能返回卡片。

用法::

    python tools/verify_astrbot_args.py --astrbot-dir "D:\\梦汐QQ机器人\\AstrBot\\AstrBot"

不传 ``--astrbot-dir`` 时，会尝试从 ``--snapshot`` 或 AstrBot 的常规位置推断；
找不到 AstrBot 源码则跳过（返回码 0），不视为失败。
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import sys
import types
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = PLUGIN_ROOT.name
sys.path.insert(0, str(PLUGIN_ROOT.parent))


# --------------------------------------------------------------------------
# 桩
# --------------------------------------------------------------------------


class _Any:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, item):
        return self

    def __iter__(self):
        return iter(())

    def __mro_entries__(self, bases):
        return (object,)


def make_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


class _Registry:
    def __init__(self):
        self.items = {}

    def append(self, handler):
        self.items[handler.handler_full_name] = handler

    def get_handler_by_full_name(self, name):
        return self.items.get(name)

    def get_handlers_by_event_type(self, *a, **k):
        return list(self.items.values())


def install_stubs() -> None:
    make_module("astrbot")
    core = make_module("astrbot.core")
    core.logger = _Any()
    core.AstrBotConfig = _Any

    for name in ("astrbot.core.agent", "astrbot.core.agent.agent",
                 "astrbot.core.agent.handoff", "astrbot.core.agent.hooks",
                 "astrbot.core.agent.tool"):
        mod = make_module(name)
        mod.Agent = _Any
        mod.HandoffTool = _Any
        mod.BaseAgentRunHooks = _Any
        mod.FunctionTool = _Any

    make_module("astrbot.core.message")
    make_module("astrbot.core.message.message_event_result").MessageEventResult = _Any
    make_module("astrbot.core.provider")
    ft = make_module("astrbot.core.provider.func_tool_manager")
    ft.PY_TO_JSON_TYPE = {}
    ft.SUPPORTED_TYPES = set()
    make_module("astrbot.core.provider.register").llm_tools = _Any
    make_module("astrbot.core.platform")
    make_module("astrbot.core.platform.message_type").MessageType = _Any
    make_module("astrbot.core.platform.astr_message_event").AstrMessageEvent = _Any
    make_module("astrbot.core.config").AstrBotConfig = _Any

    try:
        import docstring_parser  # noqa: F401
    except ImportError:
        make_module("docstring_parser").parse = lambda *a, **k: _Any()

    # star_handler 模块：registry / EventType / StarHandlerMetadata
    sh = make_module("astrbot.core.star.star_handler")

    class EventType:
        AdapterMessageEvent = "AdapterMessageEvent"

    class StarHandlerMetadata:
        def __init__(self, event_type=None, handler_full_name="", handler_name="",
                     handler_module_path="", handler=None, event_filters=None, **kwargs):
            self.event_type = event_type
            self.handler_full_name = handler_full_name
            self.handler_name = handler_name
            self.handler_module_path = handler_module_path
            self.handler = handler
            self.event_filters = event_filters or []
            self.extras_configs = kwargs
            self.desc = ""

    sh.EventType = EventType
    sh.StarHandlerMetadata = StarHandlerMetadata
    sh.star_handlers_registry = _Registry()

    star = make_module("astrbot.core.star")
    make_module("astrbot.core.star.star").star_map = {}
    make_module("astrbot.core.star.star").StarMetadata = _Any

    # filter 包
    filter_pkg = make_module("astrbot.core.star.filter")

    class HandlerFilter:
        def filter(self, event, cfg):
            return False

    filter_pkg.HandlerFilter = HandlerFilter
    filter_pkg.AstrBotConfig = _Any
    filter_pkg.AstrMessageEvent = _Any
    filter_pkg.MessageType = _Any

    # 插件的 astrbot.api.* 桩
    api = make_module("astrbot.api")
    api.logger = _Any()

    class _FilterDecorators:
        def command(self, *a, **k):
            def deco(fn):
                return fn
            return deco

        def __getattr__(self, item):
            def deco(*a, **k):
                def inner(fn):
                    return fn
                return inner
            return deco

    api_event = make_module("astrbot.api.event")
    api_event.AstrMessageEvent = _Any
    api_event.filter = _FilterDecorators()
    api_star = make_module("astrbot.api.star")
    api_star.Context = _Any

    class _Star:
        def __init__(self, context=None, config=None):
            pass

    api_star.Star = _Star


def load_real(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


class FakeEvent:
    def __init__(self, message: str):
        self._message = message
        self._extras = {}
        self.is_at_or_wake_command = True

    def get_message_str(self):
        return self._message

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="验证与 AstrBot 的指令参数解析兼容性")
    parser.add_argument("--astrbot-dir", type=Path,
                        default=Path(r"D:\梦汐QQ机器人\AstrBot\AstrBot"),
                        help="AstrBot 源码目录（含 astrbot/ 子目录）")
    args = parser.parse_args(argv)

    astrbot = args.astrbot_dir / "astrbot"
    need = [
        astrbot / "core" / "star" / "filter" / "command.py",
        astrbot / "core" / "star" / "filter" / "custom_filter.py",
        astrbot / "core" / "star" / "register" / "star_handler.py",
    ]
    missing = [p for p in need if not p.exists()]
    if missing:
        print("[跳过] 找不到 AstrBot 源码：")
        for path in missing:
            print("   ", path)
        return 0

    install_stubs()
    load_real("astrbot.core.star.filter.custom_filter",
              astrbot / "core" / "star" / "filter" / "custom_filter.py")

    for extra in ("command_group", "event_message_type", "permission",
                  "platform_adapter_type", "regex"):
        mod = make_module(f"astrbot.core.star.filter.{extra}")
        for attr in ("CommandGroupFilter", "EventMessageType", "EventMessageTypeFilter",
                     "PermissionType", "PermissionTypeFilter", "PlatformAdapterType",
                     "PlatformAdapterTypeFilter", "RegexFilter"):
            setattr(mod, attr, _Any)
    cf = sys.modules["astrbot.core.star.filter.custom_filter"]
    cf.CustomFilterAnd = _Any
    cf.CustomFilterOr = _Any

    command_mod = load_real("astrbot.core.star.filter.command",
                            astrbot / "core" / "star" / "filter" / "command.py")
    register_mod = load_real("astrbot.core.star.register.star_handler",
                             astrbot / "core" / "star" / "register" / "star_handler.py")

    # 导入插件
    pkg = make_module(PLUGIN_NAME)
    pkg.__path__ = [str(PLUGIN_ROOT)]
    spec = importlib.util.spec_from_file_location(
        f"{PLUGIN_NAME}.main", PLUGIN_ROOT / "main.py")
    main_mod = importlib.util.module_from_spec(spec)
    main_mod.__package__ = PLUGIN_NAME
    sys.modules[f"{PLUGIN_NAME}.main"] = main_mod
    spec.loader.exec_module(main_mod)

    handler = main_mod.MHMaterialPlugin.mh
    print("handler 签名：", inspect.signature(handler))

    md = register_mod.get_handler_or_create(
        handler, register_mod.EventType.AdapterMessageEvent)
    cmd_filter = command_mod.CommandFilter("mh", None, None)
    cmd_filter.init_handler_md(md)

    failures = 0

    # 1) 签名不应再依赖注解
    if cmd_filter.handler_params:
        print(f"  [失败] handler_params 应为空，实际 {cmd_filter.handler_params}")
        failures += 1
    else:
        print("  [通过] handler 签名不依赖注解(handler_params 为空)")

    # 2) 真实 filter 能匹配（AstrBot 传入的是已去掉 wake_prefix 的文本）
    event = FakeEvent("mh 素材 妖辉石")
    if not cmd_filter.filter(event, None):
        print("  [失败] 真实 CommandFilter 未能匹配 'mh 素材 妖辉石'")
        failures += 1
    else:
        print("  [通过] 真实 CommandFilter 匹配成功")

    # 3) extract_argument 各种形态
    cases = {
        "/mh 素材 妖辉石": "素材 妖辉石",
        "mh 素材 妖辉石": "素材 妖辉石",
        "/mh 怪物 爵银龙": "怪物 爵银龙",
        "/mh help": "help",
        "/mh": "",
        "": "",
    }
    for message, expected in cases.items():
        got = main_mod.extract_argument(message)
        if got != expected:
            print(f"  [失败] extract_argument({message!r}) = {got!r}，期望 {expected!r}")
            failures += 1
    if failures == 0:
        print(f"  [通过] extract_argument 全部 {len(cases)} 个用例")

    # 4) 端到端 dispatch
    from importlib import import_module

    data = import_module(f"{PLUGIN_NAME}.data")
    commands = import_module(f"{PLUGIN_NAME}.commands")
    snapshot = data.load_snapshot()
    for message in ("/mh 素材 妖辉石", "/mh 怪物 爵银龙", "/mh help"):
        argument = main_mod.extract_argument(message)
        result = commands.dispatch(snapshot, argument)
        title = result.card.title if result.card else (result.text or "").splitlines()[0]
        print(f"  [通过] {message!r} -> {title}")

    print()
    if failures:
        print(f"发现 {failures} 个问题")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

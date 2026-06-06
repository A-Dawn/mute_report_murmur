"""禁言报告(碎碎念)插件。

当 NapCat 适配器把群禁言 notice 注入 MaiBot 后，本插件会记录机器人在群内的禁言状态，
并在禁言期间阻止同群新消息继续进入命令、HeartFlow 与 Maisaka 主链。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import time

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder


MuteKey = Tuple[str, str]


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "shield"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class ReportConfig(PluginConfigBase):
    """小报告配置。"""

    __ui_label__ = "小报告"
    __ui_icon__ = "file-warning"
    __ui_order__ = 1

    target_chat: str = Field(default="", description="小报告目标，格式为 平台:group/private:号码，例如 qq:group:123456")
    target_stream_id: str = Field(default="", description="小报告目标聊天流 ID，兼容旧配置；优先使用 target_chat")
    intent_template: str = Field(
        default=(
            "你刚刚在 QQ 群 {group_id} 被{mute_type}，请按当前人设写一段简短的小报告发到这里。"
            "请包含禁言情况、禁言时长、预计解除时间，并保持自然、有个性，不要暴露系统实现细节。"
        ),
        description="触发 Maisaka 主动任务时使用的提示模板",
    )


class MuteConfig(PluginConfigBase):
    """禁言处理配置。"""

    __ui_label__ = "禁言处理"
    __ui_icon__ = "ban"
    __ui_order__ = 2

    handle_whole_group_ban: bool = Field(default=True, description="全员禁言时也按机器人被禁言处理")
    block_muted_group_messages: bool = Field(default=True, description="禁言期间阻止同群新消息触发回复链")
    report_cooldown_seconds: int = Field(default=60, description="同一群禁言小报告最小触发间隔，单位秒")


class MuteReportMurmurConfig(PluginConfigBase):
    """禁言报告(碎碎念)插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    mute: MuteConfig = Field(default_factory=MuteConfig)


@dataclass
class MuteState:
    """记录一个群的机器人禁言状态。"""

    platform: str
    group_id: str
    mute_type: str
    target_user_id: str
    operator_id: str
    duration_seconds: int
    start_time: float
    lift_time: Optional[float]
    last_report_time: float = 0.0

    def is_active(self, now: Optional[float] = None) -> bool:
        """判断禁言状态是否仍然有效。"""

        current_time = time.time() if now is None else now
        return self.lift_time is None or self.lift_time > current_time


@dataclass(frozen=True)
class ReportTarget:
    """小报告目标。"""

    platform: str
    chat_type: str
    target_id: str


class MuteReportMurmurPlugin(MaiBotPlugin):
    """禁言报告(碎碎念)插件。"""

    config_model = MuteReportMurmurConfig

    def __init__(self) -> None:
        super().__init__()
        self._mute_states: Dict[MuteKey, MuteState] = {}

    async def on_load(self) -> None:
        """处理插件加载。"""

        self._get_logger().info("禁言报告(碎碎念)插件已加载")

    async def on_unload(self) -> None:
        """处理插件卸载。"""

        self._mute_states.clear()
        self._get_logger().info("禁言报告(碎碎念)插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """处理配置热重载。"""

        del config_data
        self._mute_states.clear()
        self._get_logger().info(f"禁言报告(碎碎念)配置已更新: scope={scope}, version={version}")

    @HookHandler(
        "chat.receive.before_process",
        name="mute_report_murmur_before_process",
        description="记录 NapCat 群禁言通知，并在禁言期间阻止同群新消息进入回复主链。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
    )
    async def handle_before_process(self, message: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        """处理消息进入主链前的 Hook。"""

        modified_kwargs = dict(kwargs)
        modified_kwargs["message"] = message
        if not self.config.plugin.enabled:
            return self._build_continue_result(modified_kwargs)

        self._purge_expired_states()
        notice_payload = self._extract_napcat_notice_payload(message)
        if notice_payload:
            await self._handle_notice_message(message, notice_payload)
            return self._build_continue_result(modified_kwargs)

        if self.config.mute.block_muted_group_messages and self._should_block_message(message):
            group_id = self._extract_group_id(message)
            return self._build_abort_result(f"机器人在群 {group_id} 处于禁言状态，已阻止回复链处理", modified_kwargs)

        return self._build_continue_result(modified_kwargs)

    async def _handle_notice_message(self, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
        """处理 NapCat notice 消息。"""

        if str(payload.get("notice_type") or "").strip() != "group_ban":
            return

        sub_type = str(payload.get("sub_type") or "").strip()
        if sub_type == "ban":
            await self._handle_ban_notice(message, payload)
            return
        if sub_type in {"lift_ban", "whole_lift_ban"}:
            self._handle_lift_notice(message, payload, sub_type)

    async def _handle_ban_notice(self, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
        """记录禁言通知并触发小报告。"""

        platform = str(message.get("platform") or "qq").strip() or "qq"
        group_id = self._extract_group_id(message) or self._normalize_id(payload.get("group_id"))
        if not group_id:
            return

        target_user_id = self._normalize_id(payload.get("user_id"))
        self_id = self._normalize_id(payload.get("self_id"))
        is_whole_group_ban = self._is_whole_group_ban(target_user_id, payload)
        if not self._is_bot_mute_notice(target_user_id, self_id, is_whole_group_ban):
            return

        now = self._extract_event_time(payload)
        duration_seconds = self._normalize_duration(payload.get("duration"))
        lift_time = None if duration_seconds <= 0 else now + duration_seconds
        state = MuteState(
            platform=platform,
            group_id=group_id,
            mute_type="全员禁言" if is_whole_group_ban else "单独禁言",
            target_user_id=target_user_id,
            operator_id=self._normalize_id(payload.get("operator_id")),
            duration_seconds=duration_seconds,
            start_time=now,
            lift_time=lift_time,
        )
        key = self._build_mute_key(platform, group_id)
        old_state = self._mute_states.get(key)
        if old_state is not None:
            state.last_report_time = old_state.last_report_time
        self._mute_states[key] = state
        await self._trigger_report_if_needed(state)

    def _handle_lift_notice(self, message: Dict[str, Any], payload: Dict[str, Any], sub_type: str) -> None:
        """处理解除禁言通知。"""

        platform = str(message.get("platform") or "qq").strip() or "qq"
        group_id = self._extract_group_id(message) or self._normalize_id(payload.get("group_id"))
        if not group_id:
            return

        key = self._build_mute_key(platform, group_id)
        if sub_type == "whole_lift_ban":
            self._mute_states.pop(key, None)
            return

        target_user_id = self._normalize_id(payload.get("user_id"))
        self_id = self._normalize_id(payload.get("self_id"))
        state = self._mute_states.get(key)
        if state is None:
            return
        if target_user_id == state.target_user_id or (self_id and target_user_id == self_id):
            self._mute_states.pop(key, None)

    async def _trigger_report_if_needed(self, state: MuteState) -> None:
        """在冷却时间允许时触发小报告。"""

        target_stream_id = await self._resolve_report_stream_id()
        if not target_stream_id:
            return

        now = time.time()
        cooldown_seconds = max(0, int(self.config.mute.report_cooldown_seconds))
        if state.last_report_time and now - state.last_report_time < cooldown_seconds:
            self._get_logger().debug(f"群 {state.group_id} 禁言小报告仍在冷却中，跳过重复触发")
            return

        state.last_report_time = now
        facts_text = self._build_report_facts_text(state)
        intent = self._build_report_intent(state)
        try:
            await self.ctx.maisaka.append_context(
                target_stream_id,
                [{"type": "text", "content": facts_text}],
                visible_text=facts_text,
                source_kind="plugin:mute_report_murmur",
                message_id=f"mute-report-murmur:{state.platform}:{state.group_id}:{int(state.start_time)}",
            )
            result = await self.ctx.maisaka.trigger_proactive(
                target_stream_id,
                intent,
                reason="机器人在 NapCat 群聊中被禁言",
                priority="high",
                metadata={
                    "platform": state.platform,
                    "group_id": state.group_id,
                    "mute_type": state.mute_type,
                    "duration_seconds": state.duration_seconds,
                    "lift_time": state.lift_time,
                },
            )
            if isinstance(result, dict) and result.get("success") is False:
                self._get_logger().warning(f"禁言小报告主动任务触发失败: {result.get('error')}")
        except Exception as exc:
            self._get_logger().error(f"触发禁言小报告失败: {exc}", exc_info=True)

    async def _resolve_report_stream_id(self) -> str:
        """解析小报告目标聊天流。"""

        target_chat = self.config.report.target_chat.strip()
        if target_chat:
            target = self._parse_report_target(target_chat)
            if target is None:
                self._get_logger().warning("小报告目标格式无效，请填写 平台:group/private:号码，例如 qq:group:123456")
                return ""
            stream = await self._find_report_target_stream(target)
            stream_id = self._extract_stream_id_from_stream(stream)
            if not stream_id:
                self._get_logger().warning(f"未找到小报告目标聊天流: {target_chat}")
                return ""
            return stream_id

        legacy_stream_id = self.config.report.target_stream_id.strip()
        if legacy_stream_id:
            return legacy_stream_id

        self._get_logger().warning("未配置小报告目标，请填写 report.target_chat，例如 qq:group:123456")
        return ""

    async def _find_report_target_stream(self, target: ReportTarget) -> Any:
        """按平台目标查找真实聊天流。"""

        if target.chat_type == "group":
            return await self.ctx.chat.get_stream_by_group_id(target.target_id, platform=target.platform)
        return await self.ctx.chat.get_stream_by_user_id(target.target_id, platform=target.platform)

    def _should_block_message(self, message: Dict[str, Any]) -> bool:
        """判断普通消息是否需要被禁言状态拦截。"""

        if bool(message.get("is_notify")):
            return False

        group_id = self._extract_group_id(message)
        if not group_id:
            return False

        platform = str(message.get("platform") or "qq").strip() or "qq"
        key = self._build_mute_key(platform, group_id)
        state = self._mute_states.get(key)
        if state is None:
            return False
        if state.is_active():
            return True

        self._mute_states.pop(key, None)
        return False

    def _purge_expired_states(self) -> None:
        """清理已经自然过期的禁言状态。"""

        expired_keys = [key for key, state in self._mute_states.items() if not state.is_active()]
        for key in expired_keys:
            self._mute_states.pop(key, None)

    def _build_report_intent(self, state: MuteState) -> str:
        """根据配置模板构造主动任务意图。"""

        lift_time_text = self._format_timestamp(state.lift_time)
        duration_text = self._format_duration(state.duration_seconds)
        template = self.config.report.intent_template.strip()
        if not template:
            template = ReportConfig().intent_template
        return template.format(
            group_id=state.group_id,
            mute_type=state.mute_type,
            duration=duration_text,
            lift_time=lift_time_text,
            operator_id=state.operator_id or "未知",
            target_user_id=state.target_user_id or "全体成员",
        )

    def _build_report_facts_text(self, state: MuteState) -> str:
        """构造写入 Maisaka 上下文的禁言事实。"""

        return (
            "[NapCat 禁言事件]\n"
            f"平台：{state.platform}\n"
            f"群号：{state.group_id}\n"
            f"禁言类型：{state.mute_type}\n"
            f"目标用户：{state.target_user_id or '全体成员'}\n"
            f"操作者：{state.operator_id or '未知'}\n"
            f"禁言时长：{self._format_duration(state.duration_seconds)}\n"
            f"预计解除时间：{self._format_timestamp(state.lift_time)}"
        )

    def _is_bot_mute_notice(self, target_user_id: str, self_id: str, is_whole_group_ban: bool) -> bool:
        """判断通知是否代表机器人不可发言。"""

        if is_whole_group_ban:
            return self.config.mute.handle_whole_group_ban
        return bool(target_user_id and self_id and target_user_id == self_id)

    @staticmethod
    def _extract_napcat_notice_payload(message: Dict[str, Any]) -> Dict[str, Any]:
        """从消息中提取 NapCat notice 原始 payload。"""

        message_info = message.get("message_info")
        if not isinstance(message_info, dict):
            return {}
        additional_config = message_info.get("additional_config")
        if not isinstance(additional_config, dict):
            return {}
        payload = additional_config.get("napcat_notice_payload")
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _parse_report_target(raw_target: str) -> Optional[ReportTarget]:
        """解析 ``平台:group/private:号码`` 格式的小报告目标。"""

        parts = [part.strip() for part in str(raw_target or "").split(":")]
        if len(parts) != 3:
            return None

        platform, chat_type, target_id = parts
        if not platform or chat_type not in {"group", "private"} or not target_id:
            return None
        return ReportTarget(platform=platform, chat_type=chat_type, target_id=target_id)

    @staticmethod
    def _extract_stream_id_from_stream(stream: Any) -> str:
        """从聊天流查询结果中提取 stream_id。"""

        if not isinstance(stream, dict):
            return ""
        stream_id = str(stream.get("stream_id") or stream.get("session_id") or "").strip()
        if stream_id:
            return stream_id
        nested_stream = stream.get("stream")
        if not isinstance(nested_stream, dict):
            return ""
        return str(nested_stream.get("stream_id") or nested_stream.get("session_id") or "").strip()

    @staticmethod
    def _extract_group_id(message: Dict[str, Any]) -> str:
        """从消息结构中读取群号。"""

        message_info = message.get("message_info")
        if not isinstance(message_info, dict):
            return ""
        group_info = message_info.get("group_info")
        if not isinstance(group_info, dict):
            return ""
        return MuteReportMurmurPlugin._normalize_id(group_info.get("group_id"))

    @staticmethod
    def _is_whole_group_ban(target_user_id: str, payload: Dict[str, Any]) -> bool:
        """判断通知是否为全员禁言。"""

        if target_user_id in {"", "0"}:
            return True
        raw_duration = payload.get("duration")
        sub_type = str(payload.get("sub_type") or "").strip()
        return sub_type == "ban" and raw_duration is not None and str(raw_duration).strip() == "-1"

    @staticmethod
    def _normalize_duration(value: Any) -> int:
        """规范化禁言时长。"""

        try:
            duration = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return 0
        return max(0, duration)

    @staticmethod
    def _extract_event_time(payload: Dict[str, Any]) -> float:
        """读取事件时间戳。"""

        try:
            return float(payload.get("time"))
        except (TypeError, ValueError):
            return time.time()

    @staticmethod
    def _normalize_id(value: Any) -> str:
        """规范化平台 ID。"""

        return str(value or "").strip()

    @staticmethod
    def _build_mute_key(platform: str, group_id: str) -> MuteKey:
        """构造禁言状态键。"""

        return platform, group_id

    @staticmethod
    def _format_duration(duration_seconds: int) -> str:
        """格式化禁言时长。"""

        if duration_seconds <= 0:
            return "未知或永久"
        duration = timedelta(seconds=duration_seconds)
        days = duration.days
        hours, remainder = divmod(duration.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts = []
        if days:
            parts.append(f"{days}天")
        if hours:
            parts.append(f"{hours}小时")
        if minutes:
            parts.append(f"{minutes}分钟")
        if seconds or not parts:
            parts.append(f"{seconds}秒")
        return "".join(parts)

    @staticmethod
    def _format_timestamp(timestamp: Optional[float]) -> str:
        """格式化时间戳。"""

        if timestamp is None:
            return "未知"
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _build_continue_result(modified_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """构造 Hook 继续执行结果。"""

        return {"action": "continue", "modified_kwargs": modified_kwargs}

    @staticmethod
    def _build_abort_result(reason: str, modified_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """构造 Hook 中止结果。"""

        return {"action": "abort", "reason": reason, "modified_kwargs": modified_kwargs}


def create_plugin() -> MuteReportMurmurPlugin:
    """创建插件实例。"""

    return MuteReportMurmurPlugin()

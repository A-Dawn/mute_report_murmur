# 禁言报告(碎碎念)

当机器人在 QQ 群里被禁言时，它当然不能在原地开麦辩解。于是这个插件会替它悄悄记一笔，跑到你指定的聊天流里生成一份符合人设的“小报告”，跟你倒倒苦水嘛。

## 功能

- 监听 NapCat 适配器注入 MaiBot 的群禁言通知。
- 当机器人被单独禁言，或群里开启全员禁言时，记录对应群的禁言状态。
- 把禁言事实写入目标聊天流的 Maisaka 上下文。
- 触发 Maisaka 主动任务，让机器人按当前人设碎碎念一份“小报告”。
- 在禁言期间拦截该群后续新消息，避免继续触发命令、HeartFlow、Maisaka 决策器或 timing gate。
- 收到解除禁言通知，或禁言自然过期后，自动恢复放行。

## 配置

先填 `report.target_chat`！
不填的话，插件仍会记录禁言和阻断消息，但“小报告”没有地方送达，只能在心里小声叹气。

目标格式是：

```text
平台:group/private:号码
```

例如：

```text
qq:group:123456789
qq:private:987654321
```

这里的号码是平台侧的群号或用户号（比如说QQ或者其他），不需要手动填写 MaiBot 内部的聊天流 ID。
插件会通过 MaiBot 的聊天流查询能力解析到真实 `stream_id`。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.enabled` | `true` | 是否启用插件 |
| `report.target_chat` | 空 | 小报告目标，格式为 `平台:group/private:号码` |
| `report.target_stream_id` | 空 | 兼容旧配置的聊天流 ID；优先使用 `target_chat` |
| `report.intent_template` | 内置模板 | 触发 Maisaka 主动任务时使用的提示模板 |
| `mute.handle_whole_group_ban` | `true` | 全员禁言时是否也按机器人被禁言处理 |
| `mute.block_muted_group_messages` | `true` | 禁言期间是否拦截同群新消息 |
| `mute.report_cooldown_seconds` | `60` | 同一群重复小报告的最小触发间隔 |

`report.intent_template` 可以使用这些变量：

- `{group_id}`：群号
- `{mute_type}`：禁言类型
- `{duration}`：禁言时长
- `{lift_time}`：预计解除时间
- `{operator_id}`：操作者 ID
- `{target_user_id}`：被禁言目标 ID

## 边界说明

这个插件拦截的是“禁言后新进入 MaiBot 的消息”。如果禁言前已经有一轮 Maisaka 任务在排队或运行，已经排队的任务不会被打断或取消。新的聊天流消息会被阻止进入回复链，
避免BOT在不能说话的时候继续认真准备一段发不出去的回复。


## 插件信息

- 插件 ID：`github.A-Dawn.mute-report-murmur`
- 插件目录：`plugins/mute_report_murmur`
- 作者：[A-Dawn](https://github.com/A-Dawn)

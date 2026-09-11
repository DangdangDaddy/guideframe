# Screen Studio 鼠标轨迹格式调研

日期：2026-09-11。仅分析本地数据结构，未开发渲染器、修改源项目或复制光标素材。

> 后续验证更新（2026-09-11）：本次文档中“尚未确认最终 MP4 尺寸/帧率”的段落是当时工具受限下的记录。后续只读核验已确认两份样本的最终 MP4 均为 `2774×1290`，且均为可变帧率（VFR）素材。坐标、PTS、热点缩放和时间线的完整更新见[《轨迹与视频对齐调研》](./轨迹与视频对齐调研.md)。除这项明确更新外，原文保留，避免混淆当时观察与后续结论。

## 样本与范围

样本根目录：`/Users/jianghaoqi/Screen Studio Projects/`。

| 项目 | 元数据时长 | 移动事件 | 按下/抬起事件 |
| --- | --- | --- | --- |
| Area 2026-09-11 00:19:01.screenstudio | 19.799710 秒 | 533 | 3 / 3 |
| Area 2026-09-11 01:08:34.screenstudio | 9.520459 秒 | 509 | 4 / 4 |

两份样本的 `polyrecorderVersion` 均为 `2.7.0`，均为同一区域录制。结论针对这两份样本，不代表全部版本、显示器布局或录制模式。

## 文件结构

`recording/` 内的关联文件：

- `mousemoves-0.json`：鼠标移动事件 JSON 数组。
- `mouseclicks-0.json`：鼠标按下和抬起事件 JSON 数组。
- `metadata.json`：录制通道、分段、文件名、时间起点和录制区域。
- `cursors.json`：光标 ID、热点、标准尺寸和是否系统光标；`cursors/` 存放对应素材。
- `channel-1-display-0.mp4`：原始视频。
- 另有 `metadata-raw.json`、M3U8 与 M4S 分片；样本的 raw 元数据指向 M3U8，最终元数据指向 MP4。

输入文件名由 `recorders[type=input].sessions[]` 的 `mouseMovesFilename`、`mouseClicksFilename` 提供；视频文件名由 display 通道 sessions 的 `outputFilename` 提供。未来应按元数据关联，避免硬编码后缀 `-0`。

## 鼠标事件

第一份样本的一条原始移动记录：

```json
{
  "activeModifiers": [],
  "cursorId": "arrow",
  "processTimeMs": 880.9740417636931,
  "type": "mouseMoved",
  "unixTimeMs": 1789057142700.1353,
  "x": 719.140625,
  "y": 375.19140625
}
```

点击记录使用相同字段，增加 `button`，`type` 为 `mouseDown` 或 `mouseUp`。样本仅观察到 `button: "left"`，不应据此宣称已验证右键、中键或双击格式。

| 字段 | 观察到的类型/值 | 处理含义 |
| --- | --- | --- |
| activeModifiers | 数组，样本均为空 | 保留原值；非空元素结构待验证 |
| cursorId | 字符串：arrow、pointingHand、iBeam | 同一位置也可能切换光标形状 |
| processTimeMs | 浮点数 | 与录制元数据共同确定相对时间 |
| unixTimeMs | 浮点数 | Unix 毫秒时间，可辅助对齐核查 |
| type | mouseMoved / mouseDown / mouseUp | 保留按下/抬起区别以支持拖拽和按住反馈 |
| x、y | 浮点数 | 需按录制区域转换，不能直接视为视频像素 |
| button | 字符串，样本为 left | 点击记录额外字段 |

移动采样不均匀：第一份相邻间隔约 0.103–4886.233 ms，第二份约 0.536–1413.401 ms。不能按数组下标或固定帧率推算事件时间。第一份开头两条事件位置相同、cursorId 不同，不能仅按位置去重。

## 时间对齐

建议以匹配视频分段的 `processTimeStartMs` 为起点：

```text
视频相对时间（秒）= (事件 processTimeMs - 视频分段 processTimeStartMs) / 1000
```

第一份视频起点为 `802.4064577184618` ms，首个移动事件相对时间约为 `0.078568` 秒；第二份首个移动事件约为 `0.428152` 秒。不能将第一条鼠标事件自行归零，否则会改变与视频的同步关系。

两份样本 input/display 起点基本一致。多段录制、暂停恢复和视频 PTS 对齐仍需更多样本验证；目前未逐帧校验同步效果。

## 坐标与光标热点

两份录制区域都是 `{x:13, y:152, width:1387, height:645}`，`cropRect.yAxis` 明确为 `topBasedIncreasingDownwards`。第一份日志记录显示器逻辑范围 `1408×881`、`displayScaleFactor: 2`、`recordingScale: 0.5`，采集流配置为 `2774×1290`。

这些数据支持鼠标使用屏幕逻辑坐标的判断，但鼠标坐标轴本身仍应通过画面校验确认。针对当前区域录制，拟采用以下映射，并以实际解码视频尺寸 W、H 为准：

```text
u = (x - bounds.x) / bounds.width
v = (y - bounds.y) / bounds.height
视频坐标 = (u * W, v * H)
```

不要仅用 `recordingScale` 乘鼠标坐标；还涉及 Retina 倍率、区域偏移及实际编码尺寸。当前环境 PATH 未发现 ffprobe，Spotlight 也未提供视频尺寸，因此本轮未确认最终 MP4 尺寸/帧率；日志中的采集流尺寸不能直接当成最终文件尺寸。`displayRefreshRate: 60` 也不等于已确认视频恒定 60 fps。

`cursors.json` 每项含 `id`、`hotSpot: {x,y}`、`standardSize: {width,height}`、`systemCursor`。绘制时鼠标坐标应对准热点，而非图片左上角。本轮只阅读结构，后续使用自行绘制或授权明确的光标素材。

## 本项目采用方向

按用户要求，鼠标移动/点击部分沿用上述字段名和数组结构。`session.json` 可负责关联原始视频、鼠标文件、时间起点、录制 bounds 与自定义缩放时间轴；具体顶层结构留待设计。

保留源事件，平滑、插值和缩放映射作为后期计算，避免覆盖原数据。第一阶段默认原视频不含光标。当前只做熟悉与调研，不开始实际开发。

后续待验证：画面与坐标/热点对齐、多显示器和负坐标、跨段录制、拖拽/右键、非空修饰键、自定义光标。第一阶段兼容范围可先限定为已观察到的单段区域录制。

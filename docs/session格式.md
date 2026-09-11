# 第一阶段 session.json 格式

第一阶段只处理单段、未裁剪、未变速且不含光标的本地 MP4。所有时间都以该 MP4 的源时间线为准，渲染过程不读取网络资源。

## 完整示例

```json
{
  "version": 1,
  "video": {
    "path": "source.mp4",
    "width": 1920,
    "height": 1080
  },
  "bounds": {
    "x": 13,
    "y": 152,
    "width": 1387,
    "height": 645
  },
  "processTimeStartMs": 802.4064577184618,
  "mouseMoves": "mousemoves.json",
  "mouseClicks": "mouseclicks.json",
  "zooms": [
    {
      "startMs": 1500,
      "endMs": 5000,
      "target": {"x": 0.72, "y": 0.38},
      "scale": 2.0
    }
  ],
  "outputFps": 30,
  "cursorScale": 1.5
}
```

`video.path`、`mouseMoves` 和 `mouseClicks` 中的相对路径均相对于 session 文件所在目录解析。只接受存在的本地普通文件，URL 和 URI 不被接受。视频必须是 `.mp4`。`mouseMoves` 和 `mouseClicks` 也可以直接写成事件数组，不必另存文件。

`video.width` 与 `video.height` 是可选的输入断言，必须同时提供。渲染器会将它们与实际解码帧核对；省略时以解码尺寸为准。输出保持该尺寸。`outputFps` 默认 `30`，允许大于 0 且不超过 240。`cursorScale` 是自绘光标相对标准尺寸的倍率，默认 `1.5`。

## 鼠标事件

移动文件或内联数组沿用 Screen Studio 已观察到的字段结构：

```json
[
  {
    "activeModifiers": [],
    "cursorId": "arrow",
    "processTimeMs": 880.9740417636931,
    "type": "mouseMoved",
    "unixTimeMs": 1789057142700.1353,
    "x": 719.140625,
    "y": 375.19140625
  }
]
```

点击事件增加 `button`，`type` 为 `mouseDown` 或 `mouseUp`：

```json
[
  {
    "activeModifiers": [],
    "button": "left",
    "cursorId": "pointingHand",
    "processTimeMs": 5244.543,
    "type": "mouseDown",
    "unixTimeMs": 1789057147063.704,
    "x": 1035.7578125,
    "y": 382.30859375
  }
]
```

必填字段是 `processTimeMs`、`type`、`x` 和 `y`。`cursorId` 省略时使用 `arrow`，`activeModifiers` 省略时使用空数组，`unixTimeMs` 可省略。点击还必须提供 `button: "left"`；第一阶段不处理其他按钮。事件中的未知字段会保留在数据对象的 `extra` 中，非空 `activeModifiers` 也会原样保留，但第一阶段不解释其语义。

两个事件数组分别要求按 `processTimeMs` 非递减排列。相同时间戳合法，并按数组原顺序稳定处理；同一时刻最后一条移动事件决定光标形状，同一时刻的 `mouseDown` 位置优先于移动位置。

事件在视频上的相对时间为：

```text
t = (processTimeMs - processTimeStartMs) / 1000
```

因此 `processTimeMs` 不能早于 `processTimeStartMs`，也不能把首条鼠标事件自行归零。事件坐标必须落在闭区间 bounds 内。坐标原点在左上方，向右、向下递增；映射到实际视频帧的公式为：

```text
sourceX = (eventX - bounds.x) * decodedWidth  / bounds.width
sourceY = (eventY - bounds.y) * decodedHeight / bounds.height
```

第一条位置事件之前不绘制光标，最后一条之后保持末位置。若只有点击而没有移动，首次 `mouseDown` 起显示默认箭头。位置轨迹先对 55 ms 局部窗口做对称高斯去抖，再用不越过相邻坐标范围的分段三次 Hermite 曲线采样；首末移动点保持不变。每个 `mouseDown` 都作为强制位置锚点，点击时刻的光标热点准确落在点击坐标。光标形状使用独立的阶梯时间线，不被位置平滑改变。

## 缩放区间

`zooms` 必须按 `startMs` 排列。每项的 `startMs` 和 `endMs` 是从视频 `t=0` 开始的毫秒，满足 `0 <= startMs < endMs`。相邻区间可以首尾相接，区间不能重叠。`target.x/y` 是画面归一化坐标，范围为 `[0, 1]`；`scale` 必须大于 1，省略时为 2。

每个区间覆盖进入、停留、退出的完整生命周期。通常进入和退出各 250 ms；区间短于 500 ms 时，各占区间的一半，中间没有停留。倍率和镜头目标都使用 `smoothstep` 变化。`startMs` 时仍为 1 倍，`endMs` 时精确回到 1 倍。目标靠近边缘时，最终视口中心会按当前倍率夹取，保证不采样源画面以外的区域。

图层顺序为源画面镜头变换、点击圆环、自绘光标。每个自绘素材按自身可见尖端定义热点；光标热点和点击中心都先映射到源视频坐标，再应用同一个镜头变换。左键 `mouseDown` 触发 380 ms 的单个扩散圆环；多个点击各自独立。圆环和光标保持固定输出像素尺寸，不随镜头倍率放大。已支持 `arrow`、`pointingHand` 和 `iBeam` 自绘形状，其他 `cursorId` 回退为自绘箭头。

## Python 核心接口

```python
from local_demo.session import load_session
from local_demo.renderer import Renderer

session = load_session("session.json")
renderer = Renderer(session, (decoded_width, decoded_height))
output_image = renderer.render(t_seconds, source_pil_image)
```

`render` 返回与输入帧同尺寸的 Pillow `RGB` 图像。`render_frame` 是媒体管线使用的同义方法。三种导出必须以输出时刻调用这套接口，才能让 MP4、GIF 和 PNG 共用一致的轨迹、点击和缩放语义。

校验错误使用 `SessionValidationError`，消息包含 JSON 字段路径，例如 `zooms[1]: overlaps the previous zoom interval`。加载阶段可校验结构、路径、有限数、事件顺序、bounds 和缩放重叠；事件或缩放是否晚于视频结尾只能在媒体层取得实际时长后校验。

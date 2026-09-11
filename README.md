# local-demo

`local-demo` 是一个完全本地运行的第一阶段屏幕录制后期渲染器。它把无光标的本地 MP4 与 `session.json` 合成为带平滑鼠标、左键圆环和显式缩放的 MP4、GIF 或精确时刻 PNG；运行时不登录、不上传，也不使用系统 PATH 中的 FFmpeg。

当前工作区的虚拟环境已准备好。用一条命令生成合成输入和三种导出：

```bash
.venv/bin/python -m local_demo demo --output examples/my-demo
```

`--output` 必须是尚未包含同名演示文件的目录。生成的 `source-vfr.mp4`、事件 JSON、`session.json`、`sample.mp4`、`sample.gif` 和 `sample.png` 都是本地合成素材。当前最终验收演示位于 [`examples/validated_demo`](examples/validated_demo)。

## 安装

本机已验证的组合是 Python 3.9、PyAV 13.1.0、Pillow 11.3.0、NumPy 2.0.2、imageio-ffmpeg 0.6.0 和 pytest 8.3.5；版本固定在 [`requirements.txt`](requirements.txt)。`imageio-ffmpeg` wheel 提供项目使用的 FFmpeg，程序不会查找或调用其他应用的二进制文件。

联网安装：

```bash
scripts/setup_local_env.sh
```

离线安装前，需要在一台可联网且兼容的平台准备 wheelhouse；仓库目前**不包含** wheelhouse：

```bash
python3 -m pip download --only-binary=:all: -r requirements.txt -d wheelhouse
scripts/setup_local_env.sh --offline
```

之后所有渲染命令均可离线执行。

## 使用

先校验 session、引用的本地文件与视频属性：

```bash
.venv/bin/python -m local_demo validate examples/validated_demo/session.json
```

渲染完整 MP4。输出帧率默认读取 `session.json` 的 `outputFps`，省略时为 30 fps：

```bash
.venv/bin/python -m local_demo render session.json output.mp4
.venv/bin/python -m local_demo render session.json output.mp4 --fps 60
.venv/bin/python -m local_demo render session.json silent.mp4 --no-audio
```

渲染半开区间 `[start, end)` 的 GIF，或渲染指定精确时间的 PNG：

```bash
.venv/bin/python -m local_demo render session.json clip.gif --gif-start 1.2 --gif-end 5.4 --gif-fps 12
.venv/bin/python -m local_demo render session.json frame.png --at 2.65
```

默认拒绝覆盖已有输出，也拒绝将输出写到源视频、session 或外部事件 JSON 的同一路径。所有导出先写入目标目录中的临时文件，解码核验成功后才发布。MP4 默认保留第一条音轨：可直接封装且时间起点合适时复制，否则转为 AAC；`--no-audio` 可显式关闭。GIF 不含音轨。

## 测试与验收素材

运行测试：

```bash
.venv/bin/python -m pytest -q
```

端到端测试读取 [`examples/validated_demo`](examples/validated_demo)。新检出若没有该目录，可先生成一次：

```bash
.venv/bin/python -m local_demo demo --output examples/validated_demo
```

该目录已存在时不要重复生成；导出默认拒绝覆盖。验收范围和结果见 [`docs/第一阶段验证报告.md`](docs/第一阶段验证报告.md)。

## Session 与兼容范围

格式完整说明见 [`docs/session格式.md`](docs/session格式.md)。鼠标事件使用已观察到的 Screen Studio 字段名：`processTimeMs`、`x`、`y`、`cursorId`、`type`，点击另有 `button`；事件数组可内联，也可用相对 `session.json` 的 JSON 文件引用。坐标通过 `bounds` 映射到实际解码尺寸，事件时间以 `processTimeStartMs` 对齐视频 `t=0`。

这只兼容上述事件字段和坐标语义，**不会**直接导入 `.screenstudio` 完整工程、商业素材或应用资源。

第一阶段限制如下：

- 仅本地、单段、未旋转、方形像素、偶数尺寸的无光标 MP4；输出保持源尺寸。
- 仅 8-bit SDR 屏幕录制；HDR、广色域、非方形像素和旋转输入会明确拒绝。缺少颜色矩阵标签时按 BT.709 SDR 处理并由 `validate` 标记该假设。
- 只执行 session 中显式且不重叠的 zoom 区间；尚未实现点击自动生成缩放、桌面 UI 或录制。
- 支持 `arrow`、`pointingHand`、`iBeam` 自绘光标；其他 `cursorId` 回退为箭头。仅 `button: "left"` 触发 380 ms 点击圆环。
- 输入以视频 PTS 选取不晚于输出时刻的最近源帧；MP4 默认输出 30 fps CFR，动画在每个输出时刻独立计算。GIF 与 PNG 使用同一渲染逻辑。

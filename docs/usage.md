# 使用说明

本文是 [README](../README.zh-CN.md) 之后的详细用法：命令行客户端、渲染器参数、配置与安全、排错。
接口细节见 [`api-reference.md`](api-reference.md)，工作台见 [`studio.md`](studio.md)。

---

## 1. Python 客户端 / SDK

`client/dg_bridge.py` 只依赖 Python 标准库。

```bash
python3 client/dg_bridge.py status
python3 client/dg_bridge.py color '#FF3355'
python3 client/dg_bridge.py brush 10
python3 client/dg_bridge.py draw-json examples/strokes.json --clear
python3 client/dg_bridge.py draw-image /path/to/image.png --cells 64 --levels 8
python3 client/dg_bridge.py clear
python3 client/dg_bridge.py undo
python3 client/dg_bridge.py watch
```

作为 SDK 使用：

```python
import sys
sys.path.insert(0, "client")        # 在仓库根目录运行
from dg_bridge import BridgeClient

bridge = BridgeClient()          # 可传 base_url / token
print(bridge.state())

bridge.color("#FF3355")
bridge.brush(10)
bridge.draw_strokes([
    {"type": "line", "color": "#FF3355", "size": 10,
     "points": [[0.1, 0.2], [0.5, 0.8], [0.9, 0.2]]}
])

bridge.draw_image("cat.png", cells=64, levels=8)
```

环境变量：

```bash
export DG_BRIDGE_URL="http://127.0.0.1:8765"
export DG_BRIDGE_TOKEN="..."
```

### 1.1 图像临摹渲染器 `dg_artist.py`

`/api/v1/image` 是网格采样式填色；`client/dg_artist.py` 则从颜色区域和线稿规划有限的矢量操作。这里不是语义理解或风格迁移模型，不能保证保留原画的全部风格和细节。

```text
平涂：去线底色 → 保边平滑与分色 → 无描边色块 → 局部颜色细节 → 单中心线描绘
其他风格：区域/笔触规划 → 对应的水洗、厚涂或速写处理
```

- 平涂按预算默认使用 16 / 24 / 40 色，色块边界不再全部画成黑线；
- AI 或经典线稿先细化为单像素骨架，再沿中心追踪，每条连接只访问一次，不描绘线像素的两侧；速写、水彩共用该追踪器；
- 平涂不再用随机排线消耗剩余预算，留出的预算优先用于原图颜色细节；
- `--fill-mode auto` 默认使用多边形底色；显式 `real` 才使用同色闭合边界和游戏真实填充，失败时回退多边形；
- **自交多边形直接跳过**：轮廓简化或坐标取整可能把窄颈折成自交；游戏三角化会把闭环填成深色大块，而预览的 even-odd 填充会留空。`simple_polygon()` 在 `transform()` 之后复检，宁可少一个细节色块，也不让实际画面多一块深色污渍；
- 填充轮廓色在 `OnFinishDrawingCurrent()` 之后写回填充色（防御性）。实测 polygon fill 的画布在改动前后逐像素相同，这条路径本来就不显示激活色轮廓；保留它是为了 `real fill` 不被 `OnShapeFillDone` 染成激活色；
- 时间预算是上限，不是必须填满的配额。较长时间不意味着一定有更多有价值的笔画；
- PNG 预览是近似重建，不是游戏截图；真实填充和 Unity 排序层仍可能造成差异。截图的中间调通常比预览低约 10 个灰阶（白纸不受影响），来自色彩管理，不是调色板错误。

#### 六种风格（默认四宫格保持原四种）

| `--style` | 处理方式 | 适合 |
|---|---|---|
| `anime_flat`（默认） | 原图分色 + 单中心线，不加随机排线 | 二次元插画、简笔画上色 |
| `anime_study` | 主体去背景、少色留白、粗笔弧线涂色→阴影→最终勾线，全程无填色，逐笔发送 | 有限时间的二次元概括练习 |
| `anime_human` | 模仿手绘板玩家：每笔压感粗细不同、真人节奏和笔顺，颜色用画笔涂在线稿下面的虚拟图层上，插件不支持时用油漆桶（见 [`anime-human.md`](anime-human.md)） | 想让过程和结果都像真人手绘 |
| `sketch` | 中心线 + 排线 + 少量彩色点缀 | 速写、线稿 |
| `impasto` | 多层曲线笔触覆盖 | 厚涂感、风景 |
| `watercolor` | 浅色块 + 留白 + 细线 | 水彩/马克笔近似 |

每种处理方式都是算法近似，尤其五官、小面积颜色及柔和渐变仍会损失。原四种风格的单机 Sketchbook 默认速率是 65 预算单位/秒，联机是 11；可用 `--rate 11` 预览较紧的笔画预算。`anime_study` 另按逐笔请求和主动停顿估时，不使用 65 单位/秒推算实际绘制时长。

#### `anime_study`：模仿人手临摹

目标是画出“像人在对着图临摹”的轨迹：先起稿，再少量平涂，再阴影，最后勾线；颜色少、笔画有限、可以抽象但能认出来。完整算法见 [`artist-renderer.md`](artist-renderer.md)。

1. **主体分割**：装了 AI 组件时用 anime-seg（ONNX）抠出角色；没有就退回 GrabCut。
2. **线稿**：Anime2Sketch（可选）或经典边缘检测，细化后追踪成长曲线，再按重要性取舍。
3. **少量填色**：大色块用游戏的 `fill` 平涂（多边形必须是简单多边形），其余用长蛇形粗笔涂。
4. **阴影与收尾**：最大的几个色块上少量阴影，最后重画关键轮廓。
5. **时间余量与节奏**：自动读取回合倒计时，预留余量并动态调整每笔停顿；`--pace slow`（默认）慢慢画完，`--pace instant` 立刻画完。所有风格都支持。
6. **单层画布**：接力轮等只有一层画布的模式，会按 起稿 → 填色（向内缩几像素）→ 阴影 → 勾线 的顺序画，避免颜色盖住线稿。

预览是近似重建，不是游戏截图；复杂背景、多人构图、手指和衣褶仍会失真。不能把 API 成功或测试通过当作画质合格，请用 `tools/study_metrics.py` 看数值指标。

**只生成离线计划（不连接游戏、不清空、不绘制）**：

```bash
python client/dg_artist.py examples/sample.png --style anime_study --time 120 --dry-run \
  --preview .cache/check.png --plan-out .cache/check.json

# 更少颜色和笔数
python client/dg_artist.py examples/sample.png --style anime_study --time 60 \
  --colors 6 --max-strokes 100 --dry-run --preview .cache/limited.png

# 数值质量报告（不需要视觉模型）
python tools/study_metrics.py examples/sample.png .cache/check.json

# 离线测试，不需要游戏、下载或模型
python -m unittest discover -s tests
```

#### 用法

```bash
# 在仓库根目录运行（用 .venv 里的 Python，见安装步骤）
python client/dg_artist.py image.png --style anime_flat --time 60

# 先预览笔画计划，不碰画布
python client/dg_artist.py image.png --style impasto --time 60 --dry-run --preview plan.png

# 只用多边形填充（不用游戏真实填充）
python client/dg_artist.py image.png --style anime_flat --fill-mode polygon

# 长时间回合：更多色块和细节
python client/dg_artist.py image.png --style anime_flat --time 300 --max-regions 800

# 四宫格：同一张图，四格四种风格
# 顺序：左上 sketch、右上 anime_flat、左下 impasto、右下 watercolor
python client/dg_artist.py image.png --grid --time 60

# 自定义四格顺序
python client/dg_artist.py image.png --grid --grid-styles anime_flat,sketch,watercolor,impasto
```

四宫格会把总预算平均分给四个格子，并在画布中央画出分隔线；四个格子各自使用独立的分割与调色板，因此同一张图能直接对比四种画风。

也可以直接调用（需要 Python 里有 `cv2`、`numpy`、`Pillow`）：

```bash
python client/dg_artist.py image.png \
  --style anime_flat --time 60
```

#### 常用参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--style` | `anime_flat` | `sketch` / `anime_flat` / `anime_study` / `anime_human` / `impasto` / `watercolor` |
| `--grid` | 关 | 四宫格模式，忽略 `--style` |
| `--grid-styles` | 四种全用 | 四个逗号分隔的风格，顺序为左上、右上、左下、右下 |
| `--time` | 读取回合计时器 | 剩余秒数；Sketchbook 没有计时器时用 `--default-time` |
| `--default-time` | 180 | 无计时器时的预算 |
| `--rate` | 65 离线 / 11 联机 | 每秒可执行的「预算单位」，用于换算笔画数 |
| `--budget` | 自动 | 直接指定预算单位，覆盖时间换算 |
| `--colors` | 按风格与预算 | 平涂 16 / 24 / 40 色；study 按剩余时间 3–8 个基色，参数限 2–12；速写/水彩 8 色；厚涂 24 色 |
| `--study-pause` / `--study-speed` | `0.26` / `0.90` | 仅 study：起落笔最小停顿秒数 / 随路径长度增加的停顿系数 |
| `--study-stage-dir` | — | 仅 study：输出四阶段累计 PNG |
| `--max-regions` | 600 | 主要色块数量上限；平涂的局部细节另受剩余预算限制 |
| `--fill-mode` | `auto`（多边形） | `real` 同色边界 + 真实填充 / `polygon` 多边形填充 |
| `--fill-white` | 关 | 兼容旧参数；新版平涂保留白色底色，不依赖此开关 |
| `--line-color` | 源图取色 | 显式指定时覆盖平涂中心线颜色 |
| `--seg-k` | 按风格 | 速写/厚涂/水彩区域分割参数；新版平涂不使用 |
| `--lineart` | `auto` | `auto` 有模型时用 Anime2Sketch，否则用经典边缘；`off` 强制经典；`anime2sketch` 强制模型 |
| `--lineart-model` | `models/anime2sketch/netG.pth` | 模型权重路径 |
| `--lineart-device` | `auto` | `auto` 依次尝试 CUDA、MPS，否则 CPU |
| `--lineart-threshold` | `45` | 线稿深浅阈值；越大保留的线越少、越深 |
| `--margin` | 0.04 | 画布四周留白 |
| `--preview` | — | 输出笔画预览 PNG |
| `--plan-out` | — | 输出笔画计划 JSON |
| `--dry-run` | 关 | 只分析/规划，不绘制 |
| `--clear` / `--no-clear` | 清空 | 绘制前是否清空画布 |
| `--force` | 关 | 游戏报告不可绘制时仍尝试 |
| `--seed` | 7 | 随机种子，保证可复现 |

> 真实填充依赖闭合轮廓，复杂区域偶尔填不进去，程序会自动回退为多边形填充，并在结果里统计 `real_fills` / `fallbacks`。
> 图片模式（`/api/v1/image`）和 `dg_artist.py` 都只支持普通绘制模式，不支持 Pixel Art。

基础依赖只有 `numpy`、`opencv-python`、`Pillow`（见 `client/requirements.txt`）；AI 线稿额外需要 `torch`，缺失时会自动回退。如果使用自己的 Python：

```bash
/path/to/python -m pip install -r client/requirements.txt
DG_PYTHON=/path/to/python python client/dg_artist.py image.png --style sketch
```

#### AI 线稿增强（Anime2Sketch）

`models/anime2sketch/netG.pth` 已下载完整权重（217,631,959 字节，约 218 MB）。渲染器默认 `--lineart auto`：模型存在且 Python 有 `torch` 时，会先用 Anime2Sketch 提取线稿，再交给速写/平涂/水彩风格使用；否则自动回退到经典边缘检测。

```bash
# 默认：自动选择设备（CUDA / MPS / CPU）
python client/dg_artist.py image.png --style sketch --time 60

# 强制关闭 AI 线稿
python client/dg_artist.py image.png --style sketch --lineart off

# 强制使用模型，并调整线条密度
python client/dg_artist.py image.png --style sketch --lineart anime2sketch \
  --lineart-device mps --lineart-threshold 40

# 强制用 CPU
python client/dg_artist.py image.png --style sketch --lineart-device cpu
```

模型结构实现在 `client/anime2sketch_net.py`（U-Net，约 5440 万参数），只依赖 `torch`；不需要 `torchvision`。删除权重文件不会影响其他功能，`auto` 会自动回退。

#### 线稿回归测试与画质限制

```bash
python -I tests/test_artist_geometry.py -v
```

测试覆盖粗线中心化、闭环不重复、交叉处不重走、相邻真实线条不合并、经典线稿、填充参数、预算及嵌套颜色。通过测试仅代表这些约束成立，不等于画质已通过。

`examples/anime-flat-comparison.png` 为原图 / 旧版预览 / 修正版预览对照；修正版减少了重描与随机排线，但五官、原图的柔和渐变和笔触风格仍有明显差距。本轮只生成离线预览，没有清空或重画游戏画布。

---

## 2. 配置与安全

BepInEx 配置：

```text
<游戏目录>/BepInEx/config/com.clarke.drawguess.bridge.cfg
```

| 配置项 | 默认 | 说明 |
|---|---|---|
| `ListenAddress` | `127.0.0.1` | 监听地址；不要随意改成 `0.0.0.0` |
| `Port` | `8765` | HTTP/WebSocket 端口 |
| `Token` | 空 | 可选 API Token |
| `StrokesPerFrame` | 24 | 每帧笔画数 |
| `RoundLineJoins` | true | 每条桥接笔画强制 `PolylineJoins.Round`。游戏只有开启自带“高线质量”时才使用圆接头，否则每段都是平头方块，粗笔看起来像矩形 |
| `LongOperationTimeoutSeconds` | 180 | 图片/批量超时 |
| `LogRequests` | false | 记录每个请求 |
| `[Studio] Enabled` | true | 随游戏启动本机 Python 绘制服务 |
| `[Studio] Python` | 空（安装器写入 `.venv` 里的解释器） | 渲染器解释器 |
| `[Studio] Script` | 空（安装器写入） | `client/studio_server.py` 的路径 |
| `[Studio] WorkingDirectory` | 空（安装器写入） | 仓库根目录；作业存到 `.cache/studio` |
| `[Studio] Port` | 8770 | 绘制服务端口（只监听本机） |

设置 Token 后，请求需要带上：

```bash
-H "X-Api-Key: YOUR_TOKEN"
# 或
-H "Authorization: Bearer YOUR_TOKEN"
# WebSocket:
ws://127.0.0.1:8765/ws?token=YOUR_TOKEN
```

Web UI 顶部可以保存 Token 到浏览器 localStorage。绘制服务启动时由插件传入同一个 token，用于向 8765 发请求；修改 token 后必须重启游戏。

> 服务默认只绑定本机。不要在没有 Token 的情况下暴露到局域网/公网。
> `ui/click` 可以点击游戏内按钮，属于自动化接口，请按需使用。

---

## 3. 图片效果建议

- 卡通/纯色图：`cells=48–64`、`levels=6–10`，通常几十到几百个 `fill`。
- 照片：`cells=80–160`、`levels=12–24`，笔画会明显增多，注意 `maxStrokes`。
- 线稿：先二值化/去白底，或使用自己生成的 `line` JSON 笔画，效果比色块更干净。
- 透明背景：默认把透明/接近背景的块跳过并保留画布白色。
- 需要精确矢量时，直接用 `draw-json` 发送 `line` / `fill`，不经过图片采样。

---

## 4. 故障排查

| 现象 | 处理 |
|---|---|
| `curl` 连不上 | 看 `<游戏目录>/BepInEx/LogOutput.log` 是否有 `Draw & Guess Bridge ... ready`；检查端口是否被占用（macOS：`lsof -i :8765`，Windows：`netstat -ano | findstr 8765`） |
| 状态 `available:false` | 还没进入可绘制场景。打开 Sketchbook 或加入绘画回合 |
| `canDraw:false` | 不是你的绘画回合或 UI 被遮挡；可尝试 `force=true` |
| `No drawing module` | 同上；插件在菜单里只提供状态，不绘制 |
| 图片报 409 | 当前是 Pixel Art 模式，或画布未就绪 |
| Steam 启动后没有插件 | macOS：启动选项路径必须带引号，`run_bepinex.sh` 要可执行。Windows：确认游戏目录里有 `winhttp.dll` 和 `BepInEx/` |
| 直接启动无法登录 | 正常。直接运行不经过 Steam，联机请用 Steam 启动选项 |
| 日志出现 `Unable to start Unity log writer` | Unity 6 上的 BepInEx 6 已知警告，不影响文件日志和插件 |
| 游戏更新后失效 | 重新运行 `python tools/install.py`；必要时更新 BepInEx 6 构建 |
| 页面“绘制服务”红点 | `curl 127.0.0.1:8770/health`；看设置里的手动命令；检查 `[Studio]` 路径与端口；重启游戏 |
| 作业失败 / 预览空白 | 展开“作业日志”或看 `.cache/studio/jobs/<id>/run.log`；常见是未进入画布、token 不匹配、缺少 cv2/torch |
| 剪贴板按钮无权限 | 直接按 ⌘V / Ctrl+V（页面监听 paste 事件），或拖入图片 |
| 重启游戏画布丢了 | 重启前用右侧“导出笔画”，重启后“恢复笔画”；命令行可用 `tools/restore_canvas.py` |

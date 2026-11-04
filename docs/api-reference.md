# HTTP / WebSocket API 参考

默认地址：`http://127.0.0.1:8765`（只监听本机）。所有 JSON 响应统一为：

```json
{"ok": true, "result": { ... }}
```

错误：

```json
{"ok": false, "error": "message"}
```

| 状态码 | 含义 |
|---|---|
| 400 | 参数缺失/格式错误、点数不足 |
| 404 | 未知端点、没有匹配的 UI 按钮（`ui/click`） |
| 405 | 方法不允许（GET 端点用了 POST，或反之） |
| 409 | 当前不可绘制（`canDraw=false`）、画布未初始化、Pixel Art 模式 |
| 500 | 游戏拒绝生成元素、内部异常 |
| 504 | 主线程请求超时（普通请求 15 秒；长操作见配置） |

鉴权（可选，配置 `Token` 后启用）：

```text
X-Api-Key: <token>
Authorization: Bearer <token>
WS: ws://127.0.0.1:8765/ws?token=<token>
```

---

## GET 端点

### `GET /api/v1/state`（别名 `/api/v1/status`）

示例（实际运行时的输出，字段可能因模块不同而增减）：

```json
{
  "available": true,
  "module": "SketchbookDrawModule",
  "canDraw": true,
  "submitted": false,
  "activeObject": true,
  "tool": "Brush",
  "color": "#80935F",
  "brushSize": 5,
  "specialMode": "Normal",
  "colorMode": "RGB",
  "pixelArt": false,
  "owner": "7D84E8394927BF3",
  "canvas": {
    "initialized": true,
    "width": 17.77778,
    "height": 10.0,
    "aspect": 1.77778,
    "minX": -8.88991,
    "maxX": 8.88787,
    "minY": -5.0,
    "maxY": 5.0
  },
  "layers": {"selected": 0, "total": 3},
  "lines": 551,
  "hasUndo": true,
  "hasRedo": false
}
```

- `lines` 是**当前玩家**已完成的元素数。
- 没有可绘制模块时：`{"available": false, "hint": "...", "brushSize": 10}`。
- `canvas.width` 是规划器换算笔宽用的世界宽度。

### `GET /api/v1/lines`（别名 `/api/v1/snapshot`）

参数：`limit`（默认 10000，0–100000）。

```json
{
  "module": "SketchbookDrawModule",
  "count": 2,
  "lines": [
    {
      "type": 0, "color": "#34333D", "size": 3, "ident": 802,
      "owner": "7D84E8394927BF3", "layer": 0, "sortingOrder": 1816,
      "normalized": true, "aspectRatio": 1.77778,
      "points": [[0.50773, 0.91227], [0.50728, 0.91208]]
    }
  ]
}
```

- `type`：0 = line，1 = disc，3 = fill。
- `points` 是归一化坐标，**y 向上**。
- 这是导出格式，不是可直接回放的完整场景；排序/图层以字段为准。

### `GET /api/v1/round`（别名 `/api/v1/timer`）

```json
{
  "module": "IngameDrawModule",
  "canDraw": true,
  "timers": [
    {"active": true, "name": "Timer", "path": "...", "total": 180.0,
     "remaining": 142.3, "passed": 37.7, "peak": 180.0, "moduleTimer": true}
  ],
  "recommended": {"active": true, "name": "Timer", "remaining": 142.3,
                  "total": 180.0, "passed": 37.7, "moduleTimer": true,
                  "source": "module-field"}
}
```

- `peak` = max(`total`, `remaining + passed`)：这个计时器本局最长走过的时间（客户端按它做规划）。
- `recommended` 优先选被当前 `DrawModule` 引用的计时器，否则选运行时间最长的活动计时器。
- Sketchbook：没有活动计时器，`recommended = null`，客户端退回 `--default-time`。

### `GET /api/v1/game`

`GetState()` 与 `GetRound()` 的组合，另加实时词和回合文本。Studio 工作台默认读这个端点。

```json
{
  "available": true,
  "module": "IngameDrawModule",
  "canDraw": true,
  "word": "苹果",
  "previousWord": "雪人",
  "wordHistory": [{"word": "苹果", "at": 1761123456},
                  {"word": "雪人", "at": 1761123210}],
  "roundText": "第 3 轮 · 绘画",
  "round": {"canDraw": true, "timers": [...], "recommended": {...}},
  "canvas": {...},
  "lines": 0
}
```

- `word` 来自本机 `IngameDrawModule.LastWord`，只在绘图模块里可见；猜测阶段可能为空。
- `wordHistory` 是插件进程内静态历史，按首次看到该词去重，最多 20 条；`previousWord` 是历史第 2 条。重启游戏后清空。
- `roundText` 反射读取 `RoundInfoDisplay` 的 `RoundText`；读不到时为空字符串。
- 没有绘图模块时仍返回 `available:false` 和 `round`，不会抛 404。
- 旧页面/客户端可继续用 `/state` + `/round`。

### `GET /api/v1/studio`

插件侧查看 Python 绘制服务状态（不经过 Unity 主线程）：

```json
{
  "enabled": true, "running": true, "startedByUs": true, "pid": 90495,
  "port": 8770,
  "python": "/path/to/sumie/.venv/bin/python",
  "script": "/path/to/sumie/client/studio_server.py",
  "workingDirectory": "/path/to/sumie",
  "lastError": "",
  "log": "studio ready: http://127.0.0.1:8770/ ..."
}
```

### `GET /api/v1/ui/buttons`

```json
{"buttons": [
  {"name": "BackButton", "text": "", "active": true, "interactable": true,
   "x": 40.0, "y": 1120.0, "width": 96.0, "height": 48.0}
]}
```

坐标为屏幕像素，仅用于调试；点击不依赖坐标。

### `GET /api/v1/debug/coords?x=0.5&y=0.5`

把归一化点换算成 surface 坐标和 `GetARcorrectedArea` 坐标，并返回原始笔画。用于校准坐标系，不参与绘制。

---

## POST 端点

### `POST /api/v1/strokes`（别名 `/api/v1/draw`）

请求体：

```json
{
  "clear": false,
  "force": false,
  "strokes": [
    {"type": "line", "color": "#FF3355", "size": 10, "space": "normalized",
     "points": [[0.10,0.20],[0.25,0.80],[0.50,0.30]]},
    {"type": "disc", "color": "#000000", "size": 8, "points": [[0.5,0.5]]}
  ]
}
```

也接受：
- 顶层直接是数组（没有 `clear`/`force`）；
- 单个笔画对象（含 `points` 而不是 `strokes`）。

字段：

| 字段 | 说明 |
|---|---|
| `type` | `line` / `disc` / `fill`；数字 `0`/`1`/`3` 也接受 |
| `color` | `#RRGGBB`、`RRGGBB`、`r,g,b`、`[r,g,b]`；省略用当前颜色 |
| `size` | 1–255；省略用当前笔刷 |
| `space` | `normalized`（默认）或 `canvas`/`world` |
| `layer` | 0–2（真实图层，游戏只有三层排序层）；省略用当前选中图层。更大的值会被钳到 2 |
| `vlayer` | 虚拟图层 0..（`GET /api/v1/layers` 的 `virtual`−1）；插件换算成真实图层和 `SortingOrder` 区间，优先于 `layer`。对 `fill` 无效（填充只认真实图层）|
| `points` | `[[x,y],...]` 或 `[{"x":..,"y":..},...]` |

约束：
- `line` 至少 2 点（1 点自动转 `disc`）；`disc` 只用第一个点；`fill` 至少 3 点。
- 所有点 clamp 到 `0..1`。
- 批量按 `StrokesPerFrame` 分帧应用。

响应：

```json
{"applied": 2, "failed": 0, "requested": 2, "errors": []}
```

> 注意：`anime_study` 一笔一个请求，因为每笔之间要停顿；普通风格才批量发送。

### `POST /api/v1/image`

两种输入：

1. `Content-Type: image/png|jpeg|webp`，body 为图片字节。
2. `Content-Type: application/json`，`{"image": "data:image/png;base64,..."}` 或纯 base64。

Query 参数：

| 参数 | 默认 | 范围 | 说明 |
|---|---:|---|---|
| `cells` | 48 | 4–200 | 长边分块数 |
| `levels` | 8 | 2–64 | 每通道颜色等级 |
| `clear` | true | | 先清空 |
| `force` | false | | 强制绘制 |
| `skipBackground` | true | | 跳过接近背景/透明的块 |
| `margin` | 0.04 | 0–0.45 | 留白比例 |
| `background` | `#FFFFFF` | | 背景色 |
| `maxStrokes` | 6000 | 1–50000 | 超出直接报错 |
| `perFrame` | 24 | 1–1000 | 每帧应用数 |

返回与 `/strokes` 相同的统计。Pixel Art 模式不支持。

### `POST /api/v1/clear`

```json
{"force": false}
```

清空**所有图层**。返回 `{"ok": true, "result": {...}}`。

### `POST /api/v1/undo` · `/api/v1/redo` · `/api/v1/submit`

无 body。撤销有约 80 ms 防抖，连续调用请间隔 ≥0.1 s。

### `POST /api/v1/color`

```json
{"color": "#FF0000"}
```

### `POST /api/v1/brush`

```json
{"size": 10}
```

范围 1–255。注意游戏 UI 上限是 35，但 API 允许更大；超过 UI 的值可能被游戏钳制。

### `POST /api/v1/tool`

```json
{"tool": "Brush"}
```

工具名来自游戏枚举（`Brush`、`Eraser`、`Fill` 等）；匹配失败返回 400。

### `POST /api/v1/fill`

真实填充（按种子点找闭合区域）：

```json
{"points": [[0.5,0.5]], "color": "#FFD700", "space": "normalized", "force": false}
```

`points` 为填充种子；`point`（单点）也可接受。返回 `{"filled": N, "failed": M, "requested": K, "errors": [...], "regions": [{"area": 0.021, "points": 38}]}`；复杂区域可能失败，调用方应回退多边形。`regions` 与成功的种子一一对应，`area` 是新色块占画布面积的比例（外轮廓的鞋带公式），调用方用它发现漏色（远大于预期就 `undo`）。

### `GET /api/v1/pen`

虚拟手绘板状态：`virtualPen`（是否已创建）、`currentPen` / `pointer`（`Pen.current` / `Pointer.current` 的名字）、`usePenPressure`（游戏设置）、`focused`、`runInBackground`、`background`（InputSystem 的 `backgroundBehavior`）。

### `POST /api/v1/stroke/pen`

通过虚拟 `Pen` 设备回放一笔，走游戏自己的输入路径（`LinePressure`、点简化、同步、撤销）：

```json
{"samples": [[0, 0.30, 0.50, 0.12], [400, 0.45, 0.55, 0.40], [900, 0.70, 0.50, 0.22]],
 "force": false, "ignoreFocus": true, "vlayer": 6}
```

`vlayer`（可选）：这一笔放到哪个虚拟图层；插件在游戏创建线条的那一刻（`DrawableElement.Init`）改写排序层和顺序，所以正在画的这一笔就在正确的层上。

`samples` 为 `[t_ms, x, y, pressure]`，x/y 归一化（y 向上），按时间线性插值，每帧发一个 `PenState`；结束时压力归零再等几帧让游戏完成这笔。`ignoreFocus` 在这一笔期间临时让输入系统在后台也处理事件。需要游戏设置里的压感打开，否则 409。返回 `{"frames": N, "created": 1, "maxPressure": 0.4, "brushSize": 14, "type": "DrawableLine", "points": 31}`；`created` 为 0 表示游戏没有接受虚拟笔。

### `GET /api/v1/layers`

虚拟图层能力：`{"virtual": 9, "real": 3, "bands": 3, "realLayerUi": true, "module": "SketchbookDrawModule", "nativePatch": true, "exportSort": true}`。404 表示插件太旧。游戏只有 `SHAPES_1..3` 三个排序层（各带 `_FILL`），无法在运行时增加；虚拟图层 `v` = 真实图层 `v / bands` 加上 `SortingOrder` 的第 `v % bands` 个区间（`-28671 + band × 20479 + 计数`，`bands` 由配置 `[Layers] BandsPerLayer`，默认 3，范围 2–8）。`LineInformation` 的 `Layer` 和 `SortingOrder` 会同步给所有玩家，所以别人看到的叠放顺序一样，不需要对方装插件。规则：高层永远盖住低层，与画的先后无关；同一真实图层里填充（桶）永远在线条之下。`nativePatch` 表示 Harmony 补丁已装好，`/stroke/pen` 才能带 `vlayer`；`exportSort` 表示导出列表（相册 `MatchDisplayer`、PNG/SVG）按 `(Layer, 非填充, SortingOrder)` 排序，否则相册会按创建顺序重编号。`/api/v1/state` 里也有 `virtualLayers`。

### `POST /api/v1/layer`

```json
{"index": 0}
```

范围 0–31（游戏内部再钳制到 0–127）。

### `POST /api/v1/ui/click`

```json
{"text": "Skizzen", "name": "", "index": 0, "includeInactive": false}
```

- `text` 在按钮文本中做不区分大小写的子串匹配；`name` 匹配 GameObject 名；两者可组合。
- 游戏文本会随语言变化；自动化时优先用稳定的 GameObject 名，例如 `{"name":"SketchbookButton"}`。
- 没有匹配返回 404；`index` 越界返回 400。
- 成功返回 `{"clicked": "...", "text": "...", "matches": N}`。

### `POST /api/v1/studio/start` · `POST /api/v1/studio/stop`

- `start`：调用 `StudioProcess.Start()`，返回与 `GET /api/v1/studio` 相同的状态对象。端口已被占用时直接返回状态，不会重复启动。
- `stop`：异步请求服务 `/shutdown`，然后等待/终止插件自己启动的进程；手动启动的服务不会被强杀。
- 需要在 `[Studio] Enabled=true` 且 Python/Script 路径存在；否则 `lastError` 会说明原因。

---

## 绘制服务（Studio，127.0.0.1:8770）

这是插件启动的 Python 服务，不是桥接的一部分。所有响应为 JSON（文件端点除外），CORS 允许任意来源，但只绑定本机。它与桥接共用 token：启动时由插件传 `--bridge-token`，服务向 8765 发请求时带 `X-Api-Key`。

### `GET /health`

```json
{"ok": true, "name": "Draw & Guess Studio", "version": "1.0.0",
 "repo": "/path/to/sumie",
 "python": "/path/to/sumie/.venv/bin/python",
 "bridge_url": "http://127.0.0.1:8765", "jobs": 3, "active": null}
```

### `GET /styles`

返回五种风格的 `id/name/description/defaults`。UI 以此填充风格选择。

### `POST /jobs`

```json
{
  "mode": "plan",
  "image": "data:image/png;base64,...",
  "filename": "clipboard.png",
  "options": {
    "style": "anime_study",
    "time": null,
    "default_time": 180,
    "colors": null,
    "max_strokes": null,
    "study_pause": 0.26,
    "study_speed": 0.90,
    "seed": 7,
    "margin": 0.04,
    "max_dim": 900,
    "lineart": "auto",
    "lineart_device": "auto",
    "canvas_aspect": 1.77778,
    "force": false
  },
  "region": [0.55, 0.55, 0.30, 0.30],
  "clear": false,
  "plan_job": null
}
```

- `mode`：`plan`（只规划，生成预览/阶段/指标）或 `draw`（规划并执行）。
- `image`：base64 或 data URL，PNG/JPEG/WebP/GIF/BMP，最大 24 MB。
- `region`：归一化 `[x, y, w, h]`，y 向上；`null`/`[0,0,1,1]` 表示全屏。服务会校验范围并钳制边缘。
- `clear`：传给执行阶段；默认 false。`plan` 模式忽略。
- `plan_job`：绘制作业可复用已有计划；服务用图片字节 + 归一化参数（去掉 `plan_time`）+ 选区校验签名，不匹配则重新规划。
- `time=null` 时服务读桥接 `/api/v1/game` 的 `round.recommended.remaining`（>3 秒）作为 `plan_time`，否则用 `default_time`；`canvas_aspect` 也会用实时画布覆盖。
- 返回 `201 {"ok":true,"job":{...snapshot...}}`。

### 作业快照与状态

`GET /jobs/<id>`：

```json
{
  "id": "20261022-131500-a1b2c3",
  "mode": "plan", "status": "planned",
  "created": 1761124500, "updated": 1761124512,
  "options": {...}, "region": [0.55, 0.55, 0.30, 0.30],
  "clear": false, "plan_job": null, "reused_plan": false,
  "image_name": "clipboard.png",
  "image_url": "/jobs/<id>/input.png?v=...",
  "preview_url": "/jobs/<id>/preview.png?v=...",
  "stages": [{"name": "01-sketch", "url": "/jobs/<id>/stages/01-sketch.png?v=..."}],
  "progress": {}, "plan_ops": 50, "metrics": {...},
  "result": null, "error": null, "log_tail": "..."
}
```

状态机：`queued → planning → planned → drawing → done`；异常/操作为 `failed`、`cancelled`、`interrupted`。`plan` 作业在 `planned` 终止；`draw` 作业从 `planned` 继续到 `drawing`。`progress` 只对 `draw` 作业有内容（`sent/ops/applied/failed/elapsed/phase`）。

### 其他端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/jobs?limit=N` | 最新作业列表（默认 50，1–200） |
| GET | `/jobs/<id>/metrics` | 惰性运行 `study_metrics.py --json`，返回 `{status:"ready"|"computing"|"error"}` |
| GET | `/jobs/<id>/progress.json` | 原始进度 |
| GET | `/jobs/<id>/plan.json` | 计划 |
| GET | `/jobs/<id>/metrics.json` | 指标缓存 |
| GET | `/jobs/<id>/job.json` | 作业元数据 |
| GET | `/jobs/<id>/preview.png` | 预览图 |
| GET | `/jobs/<id>/input.<ext>` | 原始图片 |
| GET | `/jobs/<id>/stages/<file>` | 阶段图；已解析并校验路径在 stages 目录内 |
| POST | `/jobs/<id>/cancel` | 终止子进程并把状态设为 `cancelled` |
| POST | `/shutdown` | 关闭服务（插件 `StudioProcess.Stop()` 使用） |

作业目录默认 `<WorkingDirectory>/.cache/studio/jobs/<id>/`；文件清单见 [`studio.md`](studio.md#43-作业存储)。

---

## WebSocket

地址：`ws://127.0.0.1:8765/ws`（Token 用 `?token=`）。

连接后先收到：

```json
{"type":"hello","protocol":1,"name":"Draw & Guess Bridge","data":{ ...state... }}
```

请求：

```json
{"id": 1, "type": "strokes", "payload": {"strokes": [...]}}
```

支持的 `type`：

| type | payload |
|---|---|
| `state` / `status` | — |
| `lines` | `{"limit": 10000}` |
| `strokes` / `draw` | 与 HTTP `/strokes` 相同 |
| `clear` | `{"force": false}` |
| `undo` / `redo` / `submit` | — |
| `color` | `{"color": "#FF0000"}` |
| `brush` | `{"size": 10}` |
| `tool` | `{"tool": "Brush"}` |
| `ping` | —（返回 `pong`） |

**WS 不支持** `image`、`fill`、`layer`、`ui/*`；这些请用 HTTP。

响应：

```json
{"type":"response","id":1,"ok":true,"result":{...}}
{"type":"response","id":1,"ok":false,"status":409,"error":"..."}
```

事件：

```json
{"type":"event","event":"strokes.applied","data":{"applied":57,"failed":0,...}}
{"type":"event","event":"state","data":{...}}
```

---

## Python 客户端

`client/dg_bridge.py` 只依赖标准库：

```python
from dg_bridge import BridgeClient

bridge = BridgeClient()                 # 或 BridgeClient("http://127.0.0.1:8765")
bridge.state(); bridge.round(); bridge.lines()
bridge.color("#FF3355"); bridge.brush(10); bridge.layer(0)
bridge.draw_strokes([...], clear=False, force=False)
bridge.draw_image("cat.png", cells=64, levels=8)
bridge.fill([[0.5, 0.5]], color="#FFD700")
bridge.click(text="Skizzen")
```

环境变量：`DG_BRIDGE_URL`、`DG_BRIDGE_TOKEN`。

CLI：

```bash
python3 client/dg_bridge.py status
python3 client/dg_bridge.py draw-json examples/strokes.json --clear
python3 client/dg_bridge.py draw-image image.png --cells 64
python3 client/dg_bridge.py watch
```

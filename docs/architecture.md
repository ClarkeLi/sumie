# 架构与渲染链路

## 1. 总体结构

```text
┌──────────────────────────── Draw & Guess (Unity 6, beta 2.1046) ───────────────────────────┐
│  DrawModule / LayerDrawModule / IngameDrawModule                                           │
│      ▲                                                                                     │
│      │ Unity 主线程                                                                         │
│  MainThreadDispatcher ◄── BridgeServer (HTTP + WebSocket, 127.0.0.1:8765)                  │
│      ▲                         ▲                                                           │
│  BepInEx 插件 DrawGuessBridge ─┘── StudioProcess ── Python 子进程 (127.0.0.1:8770)         │
└────────────────────────────────────────────────────────────────────────────────────────────┘
                                 ▲ HTTP / WS                         ▲ HTTP (CORS)
        ┌────────────────────────┼─────────────────────────┐          │
        │                        │                         │          │
   Web UI (ui.html)      client/dg_bridge.py        client/studio_server.py
   状态 / 拖图 / 撤销       SDK / CLI                 作业队列 / 预览 / 进度
   粘贴 / 风格 / 选区                                      └─ client/dg_artist.py
                                                               └─ client/anime_study.py
```

- 插件不模拟鼠标，也不依赖辅助功能/屏幕录制权限。它直接构造 `LineInformation` 并调用 `DrawHelpers.SpawnDrawElement`，所以笔画会进入游戏自己的排序、撤销和联机同步流程。
- 浏览器工作台由插件提供：`http://127.0.0.1:8765/`。
- 插件同时启动一个本机 Python 绘制服务（`StudioProcess.cs`，默认 `127.0.0.1:8770`），页面把粘贴的图片、风格参数和选区提交给它；服务再运行 `dg_artist.py` 子进程。
- 所有坐标、笔画规划都在 Python 侧完成；C# 只负责把请求安全地搬到 Unity 主线程并落成游戏元素。
- 绘制服务只绑定本机，允许跨端口 CORS（页面 8765 → 服务 8770），由插件按配置自动启停。

## 2. 线程模型

来源：`src/DrawGuessBridge/MainThreadDispatcher.cs`、`BridgeServer.cs`、`Plugin.cs`。

1. HTTP 监听和 WebSocket 客户端在后台线程。
2. 任何触碰 Unity 对象的调用都通过 `MainThreadDispatcher`：
   - 普通请求：`InvokeJson()` → `_dispatcher.Invoke(fn, 15000)`，超过 15 秒返回 504。
   - 长操作（批量笔画、图片）：`RunLong()` → 把启动器 Post 到主线程，后台线程等待 `LongOperationTimeoutSeconds`（默认 180 秒）。
3. `BridgePlugin.Update()` 每帧执行 `_server.Pump()`，最多处理 256 个排队动作。
4. 批量笔画由协程 `ApplyStrokesRoutine` 执行，每 `StrokesPerFrame`（默认 24）条 `yield return null` 一次，避免单帧卡死。
5. WebSocket 每条消息同样走 dispatcher；不要把长任务写到 WS 处理循环里。
6. 绘制服务是独立进程，不在 Unity 线程里：`StudioProcess` 只负责启动、端口探测、日志缓冲和 `/shutdown`；它的作业队列是单 worker，一次只规划/绘制一个作业。

## 3. 坐标系与单位

这是最容易出错的部分。三套坐标必须分清：

| 空间 | 范围 | 原点 | 用途 |
|---|---|---|---|
| 归一化画布 | `0..1` | **左下角**，y 向上 | API 的 `points`、计划 JSON、`/lines` |
| 画布世界坐标 | `x: -8.88991..8.88787`，`y: -5..5` | 画布中心 | `space="canvas"`、调试 |
| 截图像素 | 1920×1080 等 | 左上角，y 向下 | 预览/对比/人工测量 |

- **归一化 y 是向上的**：`y=0` 是画布底边，`y=1` 是顶边。验证方法：`/api/v1/lines` 里头顶的描边 y≈0.92，脚 y≈0.07；预览渲染用 `1 - y` 翻转。
- 世界坐标换算（`GetCoords`）：
  ```text
  worldX = surface.MinX + nx * surface.Width
  worldY = surface.MinY + ny * surface.Heigth
  ```
- 写入时所有点都会 `Clamp01`，NaN/Infinity 返回 400。
- 截图对比时必须翻转：`screenY = canvasTop + (1 - ny) * canvasHeight`。

### 笔宽

- 游戏内部：`PolylineRenderer.Thickness = size / 100`（世界单位）。`size` 是 UI 的 1–255 画笔号。
- 像素换算是**按画布宽度**：
  ```text
  px = size * canvasPixelWidth / (100 * canvasWorldWidth)
  ```
  当前画布 `canvasWorldWidth = 17.77778`。1920 px 宽时 1 个 size 单位 ≈ 1.08 px；size 30 ≈ 32 px。
- Web UI 预览、`render_preview()`、`tools/compare_canvas.py` 都用同一公式；改公式必须三处同步，否则又会“预览比游戏粗”。

## 4. 一笔画是怎么落地的

`GameApi.ApplyOneStroke()`（`GameApi.cs:706`）：

1. 校验 `CanDraw`（除非 `force=true`）。
2. `space=normalized` 直接使用；`canvas/world` 按 surface 换算。
3. 类型归一：
   - `line` 至少 2 点；1 点自动变 `disc`。
   - `disc` 只保留第一个点。
   - `fill` 至少 3 点，自动去掉与首点重合的末点。
4. 颜色缺省用当前画笔颜色；大小缺省用 `DrawModule.BrushSize`。
5. 构造 `LineInformation`：`DrawElementType` 0=line、1=disc、3=fill，`IsNormalized=true`，`AspectRatio`/`ScaleFactorWidth` 取自 surface。
6. `DrawHelpers.SpawnDrawElement` 生成 `DrawableLine` / `DrawableDisc` / fill。
7. **接头**：如果生成的元素是 `DrawableLine` 且 `RoundLineJoins=true`，强制 `PolylineRenderer.Joins = PolylineJoins.Round`。游戏默认只在“高线质量”开启时用圆接头，否则粗笔每段都是平头四边形。
8. 加入当前模块的 created 集合，执行 `OnFinishDrawingCurrent`（Battle Royale 走精简的 base 版本，避免每条笔画都触发 ML）。

**圆帽不在 C# 里**：`PolylineJoins` 只控制折点，不控制端点。`anime_study` 的粗笔（size ≥ 8）由客户端在起笔/收笔额外发送两个同色 `disc`，直径等于线宽。别把 `RoundLineJoins` 说成“圆头”——它只修折角。

### 图层

- 请求里的 `layer` 缺省为当前选中图层；`anime_study` 执行前显式切到 0，并把所有笔画写到 layer 0，避免不同图层排序打断视觉顺序。
- `state.layers = {selected, total}`。
- `POST /api/v1/clear` 调用 `GameApi.Clear(force)`，默认**清空所有图层**。

### 填充

- `type=fill` 是游戏的多边形填充（`DrawElementType=3`）。
- `POST /api/v1/fill` 是另一个真实填充工具（`RealFill`），按种子点找闭合区域；失败会返回在结果里，客户端可回退多边形。
- `anime_study` 两者都不用，回归测试会断言没有 `fill`。

## 5. 状态、笔画导出与回合

- `GET /state`：模块是否可用、能否绘制、工具/颜色/笔刷、特殊模式、画布 min/max/宽高、图层、自己的元素数、撤销/重做。
- `GET /lines?limit=N`：`GetDrawingAsLineInfo(finishActiveLines:false, normalizeLinesForExport:true)`，返回**当前玩家**的笔画；`type` 为数字 0/1/3；`points` 已归一化。`limit` 默认 10000，最大 100000。
- `GET /round`：扫描场景内所有 `Timer`，返回 `timers[]` 和 `recommended`。`recommended.remaining` 是 `anime_study` 读取剩余时间的来源；Sketchbook 没有活动计时器，`recommended=null`。
- `GET /game`：`state` + `round` 的组合，并额外返回 `word`（本机 `IngameDrawModule.LastWord`）、`previousWord`、`wordHistory`（最多 20 条，进程内静态历史）和 `roundText`（从 `RoundInfoDisplay` 反射读取）。工作台的实时面板和自动时间都读它；旧客户端仍可分别读 `/state`、`/round`。
- `canDraw=false` 常见于：不是自己的回合、UI 遮挡、尚未进入绘画场景。`force=true` 只绕过客户端检查，游戏仍可能拒绝。

## 6. 时间预算分层

两层预算不要混淆：

1. **通用笔画预算**（`dg_artist.resolve_budget`，`dg_artist.py:593`）：
   - 剩余秒数：`/round.recommended.remaining` → `--time` → `--default-time`。
   - `usable = remaining - max(2, remaining * 0.08)`，预留 8%。
   - 速率：联机 11 units/s，离线 Sketchbook 65 units/s；可用 `--rate`/`--budget` 覆盖。
   - 每种风格有上限：`sketch 1600`、`anime_flat 2200`、`anime_study 900`、`impasto 3000`、`watercolor 2000`。
2. **study 的执行截止时间**（`dg_artist.main`）：
   - `planning_start = time.monotonic()` 在读图/模型推理**之前**。
   - `args.study_deadline = planning_start + usable_seconds`。
   - `execute_study` 每一笔、每次换色/换笔前后都检查这个绝对截止时间；规划、清空、切层耗时都不会重置它。
   - `fit_plan()` 还会预留最多 12 秒（或 usable 的 18%）作为规划余量。

因此：`--time 180` 表示“从程序启动规划算起，180 秒内完成”，不是“规划完再画 180 秒”。

## 7. Web UI / Studio

- `BridgeServer` 在启动时记录程序集目录，`ui.html` **每次请求重新读取**（`BridgeServer.cs:41,671`）。改完复制到插件目录，刷新浏览器即可，不用重启游戏。
- 如果 DLL 旁边没有 `ui.html`，返回 `WebUi.FallbackHtml`（只有 API 链接）。
- 页面是三栏工作台：图片/风格/参数/选区（左）、当前画布/计划预览/阶段/指标/进度（中）、实时游戏数据与作业记录（右）。详细用法见 [`studio.md`](studio.md)。
- 预览按上面第 3 节的笔宽公式绘制，`lineJoin/lineCap = round`；跨端口预览图带 `crossOrigin="anonymous"`，否则 canvas 会被 CORS 污染，像素读取抛 `SecurityError`。
- 页面轮询：游戏状态约 1 秒、画布约 1.8 秒（绘制中 0.8 秒）、活动作业 0.6–0.8 秒、作业列表 8 秒。作业本体在 Python 服务，刷新页面不会中断。
- 预览图/阶段图/输入图由 8770 服务提供；游戏状态、画布、撤销/提交走 8765 桥接。
- 旧的“拖图 → `/api/v1/image` 网格填色”仍然保留在桥接里，但工作台不再使用它。

## 8. 关键文件

| 路径 | 作用 |
|---|---|
| `src/DrawGuessBridge/Plugin.cs` | BepInEx 入口、配置绑定、启动服务器 |
| `src/DrawGuessBridge/GameApi.cs` | Unity 侧绘制/状态/回合/UI 自动化 |
| `src/DrawGuessBridge/BridgeServer.cs` | 路由、请求解析、WebSocket、长操作 |
| `src/DrawGuessBridge/MainThreadDispatcher.cs` | 主线程调度与超时 |
| `src/DrawGuessBridge/WebSocketClient.cs` | 最小 RFC6455 实现 |
| `src/DrawGuessBridge/StudioProcess.cs` | 启动/停止本机 Python 绘制服务、状态与日志 |
| `src/DrawGuessBridge/ui.html` | 一站式 Studio 工作台（源文件；安装时复制到插件目录） |
| `client/studio_server.py` | 8770 服务：作业队列、计划/预览/执行、进度、指标、CORS |
| `client/studio_common.py` | 区域解析/数值钳制（不依赖 cv2/numpy） |
| `tools/restore_canvas.py` | 把导出的 `/lines` JSON 回放到画布 |
| `client/dg_bridge.py` | 零依赖客户端/SDK/CLI |
| `client/dg_artist.py` | 图片 → 笔画计划；预算、预览、执行 |
| `client/anime_study.py` | `anime_study` 规划与逐笔执行 |
| `client/artist_geometry.py` | 骨架细化与单中心线追踪 |
| `client/anime2sketch_net.py` | Anime2Sketch U-Net（可选线稿） |
| `tools/study_metrics.py` | 计划的数值质量报告 |
| `tools/compare_canvas.py` | 计划/`/lines` 与真实截图的像素对比 |
| `docs/studio.md` | 工作台/服务端使用、配置、排错 |

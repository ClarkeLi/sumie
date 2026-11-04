# 开发指南

面向想改代码的贡献者：环境、构建、改动如何生效、排错、代码约定。只想使用的话，看根目录的 README。

## 1. 环境

| 项目 | 说明 |
|---|---|
| 游戏 | Draw & Guess（Steam，Unity 6，**Mono** 版本）。IL2CPP 版本无法加载本插件 |
| BepInEx | 6（Unity Mono）build 790，由 `tools/install.py` 下载并校验 SHA-256 |
| .NET SDK | 6 或更新（只用于编译 `netstandard2.1` 的插件） |
| Python | 3.10+；渲染器需要 numpy、opencv-python、Pillow；AI 组件可选（torch、onnxruntime） |
| 桥接 | `127.0.0.1:8765`，配置 `<游戏目录>/BepInEx/config/com.clarke.drawguess.bridge.cfg` |
| Studio 页面 | `http://127.0.0.1:8765/`（插件提供 `ui.html`，每次请求从磁盘读取） |
| 绘制服务 | `127.0.0.1:8770`（插件按 `[Studio]` 配置启动 Python） |
| 作业目录 | `<仓库>/.cache/studio/jobs/<id>/` |
| 日志 | `<游戏目录>/BepInEx/LogOutput.log`（含 `[studio]` 行） |

插件 GUID 和配置文件名 `com.clarke.drawguess.bridge` 保持不变，以免破坏已有配置。

## 2. 构建与安装

```bash
python tools/setup.py            # .venv + 依赖（加 --ai 装 torch/onnxruntime 并下载模型）
python tools/install.py          # 构建插件 + 安装 BepInEx + 部署插件 + 写 [Studio] 配置
python tools/install.py --check  # 只检查：找到游戏、.NET SDK、Python 环境
python tools/install.py --uninstall
```

只想编译、不装进游戏：

```bash
dotnet build src/DrawGuessBridge/DrawGuessBridge.csproj -c Release \
  -p:BepInExDir=<BepInEx 解压目录>/BepInEx/core \
  -p:GameManaged=<游戏>/…/Managed
```

产物在 `dist/`。`BepInExDir` 和 `GameManaged` 没有默认可用值（游戏的程序集不随仓库分发），必须传入；`install.py` 会替你传。

### 改动生效矩阵

| 改动 | 需要的操作 |
|---|---|
| C#（`src/DrawGuessBridge/*.cs`） | `python tools/install.py` → **重启游戏** |
| `ui.html` | `python tools/install.py` → **刷新浏览器**；不用重启 |
| Python 渲染器（`dg_artist.py` / `anime_study.py` 等） | 直接重新提交作业；每个作业都是新子进程 |
| `studio_server.py` / `studio_common.py` | 重启绘制服务（设置 → 重启，或重启游戏） |
| BepInEx 配置 | 修改 cfg → **重启游戏** |
| `tools/*.py` | 直接运行 |

## 3. 运行与测试

```bash
python -m unittest discover -s tests          # 全部离线测试，不需要游戏、网络或模型

# 只看计划，不碰画布
python client/dg_artist.py examples/sample.png --style anime_study --time 120 --dry-run \
  --plan-out .cache/check.json --preview .cache/check.png

# 数值质量报告（不使用视觉模型）
python tools/study_metrics.py examples/sample.png .cache/check.json

# 导出 / 恢复当前画布
curl -s 'http://127.0.0.1:8765/api/v1/lines?limit=100000' -o canvas.json
python tools/restore_canvas.py canvas.json --dry-run
```

完整 API 见 [`api-reference.md`](api-reference.md)；离线进入 Sketchbook 的方法见 [`studio.md`](studio.md)。

### 自动进入 Sketchbook

主菜单按钮可以用名称点击（文本随语言变化，名称更稳）：

```bash
curl -s -X POST http://127.0.0.1:8765/api/v1/ui/click -H 'Content-Type: application/json' -d '{"name":"SketchbookButton"}'
curl -s -X POST http://127.0.0.1:8765/api/v1/ui/click -H 'Content-Type: application/json' -d '{"name":"DarkUtilButtonWithText-Create"}'
curl -s -X POST http://127.0.0.1:8765/api/v1/ui/click -H 'Content-Type: application/json' -d '{"name":"NormalModeButton"}'
```

第三次点击后，`/api/v1/state` 才会返回 `SketchbookDrawModule` / `canDraw:true`。`GET /api/v1/ui/buttons` 可以看按钮的 `name/text/active`；`active:false` 的无法点击。

## 4. 排错

### 桥接连不上

- macOS：`lsof -nP -iTCP:8765 -sTCP:LISTEN`；Windows：`netstat -ano | findstr 8765`。
- 看 `LogOutput.log` 是否有 `Draw & Guess Bridge ... ready`。
- 没有监听：插件没加载（macOS 没带 `run_bepinex.sh` 的启动选项；Windows 缺 `winhttp.dll`；或 DLL 没装）。
- 有监听但 `available:false`：还没进入可绘制场景。`canDraw:false`：不是你的回合。
- 端口被占：改配置 `Port` 后重启。

### 绘制服务没起来 / 页面红点

```bash
curl -s http://127.0.0.1:8765/api/v1/studio
curl -s http://127.0.0.1:8770/health
```

- `lastError` 提示路径未配置：重新运行 `python tools/install.py`。
- `lastError` 指向 Python 或脚本不存在：检查 cfg 里 `[Studio]` 的绝对路径（仓库移动过位置就要重装）。
- 服务在线但作业失败：UI 展开“作业日志”，或看 `.cache/studio/jobs/<id>/run.log`。
- token 修改后必须重启游戏（插件和服务都只在启动时读取）。

### 剪贴板 / 预览

- 浏览器拒绝 `navigator.clipboard.read` 时，直接按 ⌘V / Ctrl+V。
- 预览图跨端口（8765 → 8770），必须带 `crossOrigin="anonymous"`。
- 刷新页面不会取消作业；页面会恢复上次作业。

### Python 报缺包

```bash
.venv/bin/python -I -c "import cv2, numpy, PIL; print('ok')"        # Windows: .venv\Scripts\python.exe
```

### AI 组件没被使用

- 确认用 `python tools/setup.py --ai` 装过，且 `models/` 下有 `anime2sketch/netG.pth` 和 `anime-seg/isnetis.onnx`。
- `--lineart anime2sketch` 会在缺模型时直接报错，而不是静默回退。
- 设备：`--lineart-device cuda|mps|cpu`。

### 画出来明显不对

先看 `study_metrics.py` 和 `compare_canvas.py` 的数值，再查：主体 mask 是否为空或选错；回合剩余时间是否读对（`/round`）；执行是否被截止时间截断（`sent` vs `ops`）；画笔号是否超过游戏 UI 上限 35。

## 5. 代码约定

- Python 侧不要混用 Unity 坐标、源图像素坐标、截图像素；`Op.points` 永远是归一化 y-up。
- 新增端点：`BridgeServer.RouteGet/RoutePost` + `GameApi` + `client/dg_bridge.py` 三处同步；长操作走 `RunLong`。
- 改笔宽公式必须同步：`GameApi`、`ui.html`、`render_preview()`、`compare_canvas.py`。
- 生成 `fill` 时用 `simple_polygon()`，并在 **`transform()` 之后**再用 `is_simple_polygon()` 检查；自交多边形在游戏里会被三角化成深色大块，预览却留空。
- 单层画布（接力轮）不能切图层；`anime_study.adapt_layers` 负责压平，见 `artist-renderer.md`。
- 规划阶段不要引入会占满所有核的线程池（`scipy.spatial`、默认的 onnxruntime CPU 会话等），见 `artist-renderer.md` 的“CPU 占用”一节。
- 新增 Studio 端点或字段：同步 `studio_server.py`、`ui.html`、`docs/studio.md`、`docs/api-reference.md` 和 `tests/test_studio_server.py`。
- UI 必须自包含、离线可用，不引入第三方 CDN。
- 提交前跑全部离线测试；改画质时附上 `study_metrics` 的数值变化。

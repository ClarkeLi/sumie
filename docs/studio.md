# Studio：浏览器一站式工作台

`http://127.0.0.1:8765/` 是插件的 Web 工作台。页面本身由 BepInEx 插件提供，负责实时游戏数据、画布显示和游戏控制；风格规划与绘制由一个随插件自动启动的本机 Python 服务完成（默认 `127.0.0.1:8770`）。

> 为什么需要第二个端口：插件运行在 Unity Mono 里，不能加载 OpenCV/torch。浏览器也不能直接运行 `dg_artist.py`。所以由插件启动一个只监听本机的 Python 子进程，页面跨端口调用它（服务端允许 CORS）。

## 1. 启动与检查

正常流程：安装插件 → 启动游戏 → 插件启动桥接 `8765` 和绘制服务 `8770`。

```bash
# 桥接（插件）
curl -s http://127.0.0.1:8765/health
# 实时游戏状态（新端点，含当前词/上一轮词/回合文本/计时器）
curl -s http://127.0.0.1:8765/api/v1/game | python3 -m json.tool | head -40
# 绘制服务状态（插件视角）
curl -s http://127.0.0.1:8765/api/v1/studio | python3 -m json.tool
# 绘制服务自身
curl -s http://127.0.0.1:8770/health | python3 -m json.tool
```

页面顶部三个状态点分别对应：桥接、绘制服务、游戏画布。绘制服务离线时，游戏状态和“当前画布”仍可用，但风格预览/绘制不可用。

## 2. 页面工作流

### 2.1 图片

- **⌘V / Ctrl+V 直接粘贴**剪贴板图片；
- 拖入页面或点击“选择文件”；
- “从剪贴板粘贴 / 粘贴并预览 / 粘贴并直接绘制”三个入口；
- 支持 PNG/JPEG/WebP/GIF/BMP，上限 24 MB；
- 勾选“粘贴后自动生成预览”时，图片载入后自动创建一个预览作业（默认开启）。

### 2.2 风格与参数

| 控件 | 说明 |
|---|---|
| 风格 | `anime_study`（默认）、`anime_flat`、`sketch`、`impasto`、`watercolor` |
| 时间 | **自动读回合**：创建作业时读 `/api/v1/game` 的 `round.recommended.remaining`；无计时器（Sketchbook）用 180 秒。**手动秒数**覆盖。 |
| 基色数 | 留空由风格/预算决定；`anime_study` 内部上限 10，其他风格可到 64 |
| 笔画上限 | 留空 900（由 `--max-strokes` 传递） |
| 随机种子 / 留白 / 最大边长 | 对应 `--seed` / `--margin` / `--max-dim` |
| 起笔停顿 / 长度系数 | 仅 `anime_study` 使用（`study_pause` / `study_speed`） |
| 线稿 / 推理设备 | `--lineart` / `--lineart-device` |
| 强制 | 游戏报告 `canDraw=false` 时仍提交（很可能失败） |

预设按钮“快速 60s / 标准 180s / 精细 300s”切换为手动时间，并设置对应的基色数。

### 2.3 绘制范围

- **全屏**：图片按比例适配整个画布。
- **选区**：只把图片放进归一化矩形 `x,y,w,h`；在中间画布上拖动即可框选，也可以在 X/Y/宽/高输入框精确设置。
- 坐标原点在**左下角**，与游戏一致；y 向上。输入框和预览标签都按这个约定显示。
- 选区模式下，画布外区域变暗，蓝色虚线是绘制范围。
- 更改图片、风格、参数或选区后，旧预览会标记“已过期”；点击绘制时会重新规划，除非签名仍匹配。

### 2.4 预览、指标与作业

- **计划预览**：`dg_artist.py --dry-run` 的输出，不碰游戏画布。
- **阶段**：`anime_study` 生成的 4 张中间阶段图（线稿/底色/阴影/细节），点击缩略图可在主画布查看。
- **指标**：`tools/study_metrics.py --json` 的数值报告（笔数、颜色数、覆盖率、外溢、颜色误差、弯曲比、预计时长）。没有视觉模型参与。“存在填充/长直线”警告只对 `anime_study` 显示；`anime_flat` 正常使用 `fill`，并且会跳过自交多边形（游戏会把它们填成深色大块，日志里有 `flat: skipped ...`）。
- **开始绘制（复用预览）**：把预览作业的 `plan.json` 传给执行作业（`--plan-in`），不再重新规划；服务端会用图片字节 + 归一化参数 + 选区校验签名，匹配才复用。
- **进度**：执行作业每写一笔更新 `progress.json` 与 `run.log`，页面轮询显示 `sent/ops`、阶段、耗时；可取消。
- **作业记录**：最近 30 个作业，点击可载入图片、参数、选区和预览；“导出笔画/恢复笔画”在右侧游戏面板。

### 2.5 当前画布与游戏

右侧实时面板每秒读取 `/api/v1/game`：

- 当前词（`word`）、上一轮词（`previousWord`）、本机见过的词历史（最多 20 个，页面显示前 6 个）；
- 回合文本（`roundText`）、推荐计时器剩余秒数；
- 模块、`canDraw`、`submitted`、工具、颜色、画笔大小、元素数、图层。

画布控件直接调用桥接：撤销、重做、清空、提交。清空需要二次确认。

“导出笔画”保存 `/api/v1/lines` 的 JSON；“恢复笔画”读取同样的 JSON（也接受原始 `{ok, result}` 包装），以 `clear=false, force=true` 回放。**重启游戏前先导出，进入新画布后恢复**，即可保住当前进度。

### 2.6 设置与持久化

- 设置对话框：API Token（必须与 BepInEx `Server/Token` 一致）、绘制服务端口（通常 8770）。
- 页面设置保存在浏览器 `localStorage` 的 `dgStudio.v1`：风格、时间、参数、选区、token、端口、上次作业 id。
- 刷新页面后会从绘制服务恢复上次作业的图片、参数和预览；不会自动开始绘制。
- 作业和历史保存在服务端，不依赖浏览器。

键盘：`⌘↵`（Windows：`Ctrl+Enter`）生成预览，`⇧⌘↵`（`Ctrl+Shift+Enter`）开始绘制（焦点不在输入框时）。

## 3. 默认保留画布

- 绘制作业的 `clear` 默认 **false**，UI 不会自动清空；
- 只有显式勾选“绘制前清空画布”并确认，才会发送 `clear=true`；
- 不勾选时，新笔画叠加在当前画布上；选区绘制适合在空白角落补充内容；
- 需要完整重画时，先“导出笔画”留档，再勾选清空。

## 4. 绘制服务

### 4.1 启动方式

插件在 `Awake()` 里按配置启动（`StudioProcess.cs`），并在 `OnDestroy` / `OnApplicationQuit` 时通过 `POST /shutdown` 关闭自己启动的进程。如果端口已被占用（例如手动启动过），插件不会重复启动。

手动启动：

```bash
python -u \
  client/studio_server.py \
  --port 8770 \
  --bridge-url http://127.0.0.1:8765 \
  --repo . \
  --work-dir ./.cache/studio
```

如果 `Server/Token` 非空，加 `--bridge-token <token>`。

### 4.2 配置（`BepInEx/config/com.clarke.drawguess.bridge.cfg`）

```ini
[Studio]
Enabled = true
Python = /path/to/sumie/.venv/bin/python
Script = /path/to/sumie/client/studio_server.py
WorkingDirectory = /path/to/sumie
Port = 8770
```

- `Enabled=false` 时插件不启动服务，页面仍可查看游戏状态/画布。
- 修改路径、端口或 token 后必须重启游戏（插件只在启动时读取配置）。
- 页面设置里的端口只改变浏览器连接目标；服务端端口仍由插件配置决定。

### 4.3 作业存储

默认 `<WorkingDirectory>/.cache/studio/jobs/<job-id>/`：

| 文件 | 内容 |
|---|---|
| `input.png/jpg/...` | 原始粘贴图片 |
| `job.json` | 模式、状态、选项、选区、签名、plan_job、结果 |
| `plan.json` | 生成的笔画计划（`dg_artist` 格式） |
| `preview.png` | 计划预览 |
| `stages/` | `anime_study` 阶段图 |
| `progress.json` | 执行进度（每笔更新） |
| `run.log` | 子进程 stdout/stderr（token 已脱敏） |
| `metrics.json` | 数值指标缓存 |

作业服务一次只执行一个作业（队列 + 单 worker），避免多个渲染器同时向游戏发送笔画。队列状态：`queued → planning → planned → drawing → done`，失败/取消/中断分别记录。

### 4.4 计划复用

创建绘制作业时若带 `plan_job`：

1. 读取源作业的 `plan.json`；
2. 用**新图片原始字节 + 归一化参数（去掉 `plan_time`）+ 选区**计算签名；
3. 与源作业签名比较，一致才复用（预览和阶段图也复制）；
4. 不一致则自动重新规划，不会报错。

因此“自动时间”不会因为两次读取的倒计时不同而破坏复用；执行阶段重新读取实时剩余时间。

## 5. 服务 HTTP API（摘要）

完整字段见 [`api-reference.md`](api-reference.md#绘制服务studio1270018770)。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/health` | 版本、仓库、作业数、活动作业 |
| GET | `/styles` | 五种风格的默认参数 |
| GET | `/jobs` | 作业列表（最新在前） |
| GET | `/jobs/<id>` | 作业快照（含日志尾部、预览/阶段 URL、进度、指标） |
| GET | `/jobs/<id>/preview.png` | 预览图 |
| GET | `/jobs/<id>/stages/<file>` | 阶段图（路径已做目录穿越检查） |
| GET | `/jobs/<id>/metrics` | 惰性计算/返回指标 |
| GET | `/jobs/<id>/progress.json` | 原始进度 |
| POST | `/jobs` | 创建 `plan` 或 `draw` 作业 |
| POST | `/jobs/<id>/cancel` | 取消 |
| POST | `/shutdown` | 关闭服务（插件使用） |

所有响应都是 JSON；文件端点除外。服务允许 `*` CORS，但只绑定 `127.0.0.1`。

## 6. 排错

| 现象 | 检查 / 处理 |
|---|---|
| 页面能开，绘制服务红点 | `curl 127.0.0.1:8770/health`；看设置里的手动命令；确认 cfg 的 Python/Script 路径存在；重启游戏 |
| 服务在线，提交作业 400 | 图片格式/大小；token 不匹配时桥接请求会失败，日志里看 401 |
| 作业 `failed` | 展开“作业日志”；常见原因：游戏不在画布（`canDraw=false`）、Python 缺 cv2/torch/Pillow、线稿权重缺失 |
| 预览空白但指标正常 | 可能是选区过小或计划只有细线；切到“当前画布”确认游戏侧是否真的在画 |
| 剪贴板读取失败 | 浏览器权限限制；直接按 ⌘V / Ctrl+V（页面监听 paste 事件） |
| 端口冲突 | 改 `[Studio] Port` 并重启；页面设置里填同一端口 |
| 修改 token 后失败 | 服务和插件都只在启动时读取 token，重启游戏 |
| 改了 UI 不生效 | `ui.html` 每次请求读取；刷新页面即可。改了 DLL 必须重启游戏 |
| 刷新页面作业丢了 | 作业在服务端；看 `.cache/studio/jobs`，或等页面自动恢复上次作业 |
| 画布被清空 | 使用右侧“恢复笔画”，选择重启前导出的 JSON |

## 7. 验收（不使用视觉模型）

```bash
# 单元测试（含 studio 纯函数）
python -I -m unittest discover -s tests

# 服务健康 + 作业快照
curl -s http://127.0.0.1:8770/health | python3 -m json.tool
curl -s http://127.0.0.1:8770/jobs | python3 -m json.tool | head -60

# 计划指标
python -I tools/study_metrics.py \
  examples/sample.png .cache/studio/jobs/<id>/plan.json \
  --canvas-aspect 1.77778

# 当前画布与导出对比（截图对齐见 verification.md）
python3 tools/compare_canvas.py <shot.png> .cache/studio/jobs/<id>/plan.json --rect ...
```

页面本身的布局与交互可以用无头浏览器做数值检查（不截图给人看）：读取元素边界、`scrollWidth`、状态点 class、`getImageData` 非白像素比例。注意跨端口图片必须带 `crossOrigin="anonymous"`，否则 canvas 会被污染，`getImageData` 会抛 `SecurityError`。

## 8. 已知限制

- 服务是**单 worker**：一次只规划/绘制一个作业；连续粘贴会排队。
- 作业图片存在本机磁盘（`.cache/studio`），不会上传；但仍建议不要把敏感图片留在共享机器上。
- 页面没有多用户/权限系统；token 只保护桥接 API，绘制服务的 `/health`、`/jobs` 同机可读。
- 自动读取剩余时间依赖 `/api/v1/game`；插件未更新时页面会退回 `/state` + `/round`，没有词/回合文本。

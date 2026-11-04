# 数值验证（不使用视觉模型）

画质验收不依赖模型“看一眼”截图，而是用可复现的像素统计：

1. 计划级指标：`tools/study_metrics.py`
2. 预览/真实画布对比：`tools/compare_canvas.py`
3. 单元测试：`tests/`
4. 必要时做单笔宽度剖面测量

这些指标能发现“变细/留白/变方/溢出/没画完”，但不能证明“好看”。画质判断仍需人眼。

## 1. 单元测试

`tests/` 里的用例全部离线运行，不需要游戏、网络或模型，覆盖中心线追踪、填充多边形校验、`anime_study` 的计划与节奏、Studio 服务的纯函数、安装器逻辑。

```bash
python -m unittest discover -s tests -v
```

所有测试都应通过。它们不需要游戏、模型或网络（`test_anime_study.py` 会用很小的假图跑规划）。

## 2. 计划级指标

```bash
mkdir -p .cache/anime-study
python client/dg_artist.py examples/sample.png --style anime_study --time 180 --dry-run \
  --plan-out .cache/anime-study/sample-180.json \
  --preview .cache/anime-study/sample-180.png

python tools/study_metrics.py \
  examples/sample.png .cache/anime-study/sample-180.json \
  --render .cache/anime-study/metrics.png
```

字段：

| 字段 | 含义 | 期望 |
|---|---|---|
| `ops` | 操作数 | 随剩余时间变化，硬上限 900 |
| `kinds` | line/disc/fill 数量 | `fill` 只用于大色块，数量有限 |
| `colours` | 不同笔画颜色数 | 不是调色板数量，含阴影/墨线 |
| `brush_*` | 笔号范围 | 涂色 4–35，墨线 2–5 |
| `arc_ratio_mean` | 所有线的总长/首尾距离 | 涂色块多折返，整体应 >2.5 |
| `two_point_long` | 长直线数量 | **必须为 0** |
| `subject_coverage` | 主体像素被覆盖比例 | ≥0.85（0.88–0.93 正常） |
| `colour_error_painted` | 主体内平均颜色误差（0–255） | ≤55（45–50 正常） |
| `bleed_outside_subject` | 主体外非白像素被涂比例 | ≤0.12（0.08–0.11 正常） |
| `estimated_seconds` | 预估执行时间 | 应 ≤ 可用时间 |

注意：`subject_coverage` 不是越高越好。白色高光本来就是故意留白；追到 1.0 通常意味着把轮廓外或纸白也涂了。

## 3. 截图与画布对齐

### 3.1 截图

1. 把游戏窗口切到前台，然后截图保存为 `game-shot.png`（macOS：`screencapture -x game-shot.png`；Windows：用截图工具保存整个窗口）：

2. 确认拍到的是游戏：画面里应该只有画布、左侧工具栏和右上角头像。

3. 例如在 1920×1200 屏幕、游戏窗口贴左上时，画布矩形是：

   ```text
   --rect 0,95,1920,1080
   ```

   即画布在截图中的左上角 `(0, 95)`，宽 1920、高 1080。窗口位置变了就重新校准。

### 3.2 校准 `--rect`

画布是 16:9，宽度等于窗口内容宽度。若不确信 y 偏移，用 IoU 搜索：

```bash
for y in $(seq 60 2 140); do
  iou=$(python3 tools/compare_canvas.py game-shot.png .cache/anime-study/sample-180.json \
        --rect 0,$y,1920,1080 --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["iou"])')
  echo "$y $iou"
done | sort -k2 -nr | head
```

正确对齐时 IoU 约 0.90；y 偏移 10 px 以上会明显下降。宽度不确定时同理搜索 `--rect x,95,W,1080`，但 W 应与截图里画布实际像素宽一致。

### 3.3 排除 UI

左侧工具栏、右上头像、右下撤销按钮会干扰统计，用 `--exclude` 去掉（全局截图坐标）：

```bash
python3 tools/compare_canvas.py game-shot.png .cache/anime-study/sample-180.json \
  --rect 0,95,1920,1080 \
  --exclude 0,90,100,1080 \
  --exclude 1750,60,170,140 \
  --exclude 1720,1010,200,160 \
  --render canvas-compare.png
```

`--render` 生成三联图：预测 / 截图 / 差异（绿=一致，红=游戏有但计划没有，蓝=计划有但游戏没有）。

## 4. 如何解读对比结果

参考验收线（数值取决于图片，仅作排错参考）：

- `anime_study`：没有长直线（`two_point_long == 0`）；
- `subject_coverage >= 0.85`；
- `bleed <= 0.12`；
- 对比 `IoU >= 0.88`、`recall >= 0.90`、`precision >= 0.95`；
- 厚度：同一画笔号在预览与截图中的线宽差 ≤2 px。

### 4.1 `anime_flat` 的额外检查

`anime_flat` 大量使用 `fill`，要额外检查：

1. **自交填充**：`plan.json` 里的每个 `fill` 在经过 `transform()` 四舍五入后必须通过 `is_simple_polygon`。自交多边形在游戏里会被三角化成深色大块，而预览（PIL even-odd）会留空，这是“预览干净、实际更差”的最大来源。现在 `build_anime_flat` 会跳过它们，日志里有 `flat: skipped N self-intersecting fill(s)`。
2. **计划 vs 截图**与**预览 vs 截图**都做一次；后者把截图缩放到预览分辨率，避免分辨率差被算成几何差。
3. **深色块指标**：统计“计划填充为浅色、距计划线条 >4 px、实际亮度 <80”的连通域面积。它能独立于总 IoU 发现深色 blob。

低于这些值时，先排查：

| 现象 | 原因 |
|---|---|
| IoU 高但 recall 低 | 游戏画面有额外 UI/旧笔画；检查 `--exclude`，或画布没清空 |
| IoU 整体偏低、边缘错位 | `--rect` 不对，或 `compare_canvas.py` 与游戏用的世界宽度不同 |
| precision 低 | 计划里有游戏没画出的笔画（截止时间到了/请求失败），检查执行统计 |
| 线宽系统性偏细/偏粗 | 笔宽公式三处不一致（C#/Web UI/预览/compare） |

## 5. 单笔圆头/圆角验证（可选）

在画布空白处画一条已知粗线，量端点宽度剖面：

```bash
# 一条 size=34 的竖线；execute_study 之外需要手动补两个圆帽
curl -s -X POST http://127.0.0.1:8765/api/v1/strokes \
  -H 'Content-Type: application/json' -d '{
    "strokes": [
      {"type":"line","color":"#000000","size":34,"points":[[0.10,0.10],[0.20,0.60]]},
      {"type":"disc","color":"#000000","size":34,"points":[[0.10,0.10]]},
      {"type":"disc","color":"#000000","size":34,"points":[[0.20,0.60]]}
    ]}'
```

然后截图，按垂直于笔画的方向逐行数黑色像素：

- 圆帽：最后几行宽度从约 36 px 平滑降到 0；
- 方头：最后一行仍是约 36 px；
- 圆折角：V 形内侧应平滑，而不是缺口或方角。

测完撤销 3 次（每次间隔 ≥0.1 s），或用 `state.lines` 确认元素数恢复。注意撤销可能按组进行，先看 `hasUndo`/`lines` 的变化。

脚本化测量思路：把端点映射到截图坐标，沿切线方向逐距离采样，统计垂直于切线的已涂像素宽度；远离其他笔画的空白区域才能得到干净剖面。

## 6. Studio 页面的数值检查（无截图验收）

工作台本身也要求/产生数值，可以完全不用视觉模型：

### 6.1 布局

用无头 Chrome（Playwright）在 1680/1280/1024/800/390 宽度分别检查：

- `document.documentElement.scrollWidth <= innerWidth`；
- header / main / aside / stage 的 `getBoundingClientRect().right` 不超过视口；
- 状态点 class（`pill ok/bad`）、`#jobStatus` 文本、阶段图数量；
- 控制台 `pageerror` 为空。

显式重新加载页面（`page.reload()`）可以复位 `localStorage` 默认值；测试时注意浏览器上下文是否复用。

### 6.2 预览/画布像素

```js
const stats = await page.evaluate(() => {
  const canvas = document.querySelector('#stage');
  const ctx = canvas.getContext('2d');
  const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  let ink = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i] < 245 || data[i + 1] < 245 || data[i + 2] < 245) ink++;
  }
  return { ratio: ink / (canvas.width * canvas.height) };
});
```

注意：

- **选区模式**下，`drawRegionOverlay` 会把选区外涂成半透明深色，`ratio` 会接近 0.85；
  要测真实笔迹，切回全屏或只统计选区内像素。
- 预览图来自 `8770`，与页面 `8765` 跨端口；`Image` 必须 `crossOrigin="anonymous"`，
  否则 `getImageData` 抛 `SecurityError: The canvas has been tainted by cross-origin data`。
- 画布 `width/height` 是 devicePixelRatio 后的像素；宽度比例不受影响。

### 6.3 计划复用 / 执行

- 作业快照里 `reused_plan=true` 且 `plan_job=<预览 id>` 表示复用；
- `progress.json` 的 `sent == ops`、`applied == sent`、`failed == 0`；
- `result.elapsed` 应小于计划时间；`/api/v1/state.lines` 增加量 = 线 + 帽 + disc。

### 6.4 画布导出/恢复

重启游戏前导出 `/api/v1/lines`，恢复后重新导出，比较**几何多重集**（忽略 ident/sortingOrder）：

```python
def canon(line):
    return (line["type"], line["color"], line["size"], line["layer"],
            tuple(map(tuple, line["points"])))
# Counter(map(canon, before)) == Counter(map(canon, after))
```

当前 551 元素画布已验证完全一致。

## 7. 常见误判

- **截图没切到游戏**：本机第一次 `screencapture` 拍到的是 Bilibili 页面（游戏在后台）。先 `open -a` 激活。
- **把抗锯齿当溢出**：`--threshold 240` 会把浅色边缘算作已涂；想只统计深色时降低阈值，想把浅色也算进来时提高阈值。
- **拿旧计划比新画布**：先确认 `/api/v1/lines` 的元素数和计划一致，否则可能画布没清空或执行被截止时间截断。
- **颜色误差包含留白**：`colour_error_painted` 只统计主体内已涂像素；白色高光仍会拉高误差。
- **指标全绿不等于画得好**：覆盖率、IoU 只是几何一致性；五官、手、衣褶仍可能明显失真。

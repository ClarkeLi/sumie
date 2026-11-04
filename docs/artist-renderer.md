# 图像临摹渲染器

`client/dg_artist.py` 把一张图片规划成有限数量的矢量笔画，再通过桥接逐条画到游戏里。`client/anime_study.py` 是其中的 `anime_study` 风格。本文只讲实现，不讲怎么安装游戏（见 `development.md`）。

## 1. 入口与预算

```text
dg_artist.main()
 ├─ 读取 state / round                        （--dry-run 时不连桥接）
 ├─ resolve_budget()                           remaining → usable → units
 ├─ load_image(max_dim=900)
 ├─ 可选 Anime2Sketch 线稿                      --lineart auto/off/anime2sketch
 ├─ make_transform()                           图片适配画布，留白 --margin
 ├─ build_ops(style)                           生成 Op 列表
 │   └─ anime_study: fit_plan()                按绝对截止时间裁剪
 ├─ render_preview()                           --preview / --dry-run
 └─ execute_plan()                             study 走 execute_study()
```

预算规则见 `architecture.md` 第 6 节。`--time` 覆盖读取到的回合剩余时间；Sketchbook 没有计时器时使用 `--default-time`（180 秒）。

## 2. 坐标与适配

`make_transform(width, height, canvas_aspect, margin)`：

```text
fit_w = fit_h = 1 - 2*margin
若图片更宽：fit_h = fit_w * canvas_aspect / image_aspect
否则：      fit_w = fit_h * image_aspect / canvas_aspect
nx = origin_x + (x / width) * fit_w
ny = origin_y + (1 - y / height) * fit_h      # 翻转成 y 向上
```

所以 `Op.points` 永远是归一化、y 向上的；不要把源图像素坐标直接塞进 `Op`。

笔宽换算：`source_brush_size(source_px, width, fit_w, world_width)` 把源图像素宽度换算成游戏 size：

```text
size = source_px * 100 * fit_w * world_width / source_width
clamp 到 2..35
```

这个函数是“源图上的视觉笔宽”与“游戏画笔号”之间唯一的桥。

## 3. 分析阶段

`make_analysis(image, style, args)`：

- 用 k-means 量化出 `labels / quantized / palette`；
- `edges`：Anime2Sketch 可用时用模型线稿，否则 `edge_map()`（经典梯度/脊线）；
- `gray`：灰度，用于暗部、眼睛和阴影判断。

Anime2Sketch 只依赖 `torch`；权重在 `models/anime2sketch/netG.pth`（约 218 MB，已 gitignore）。有 CUDA 或 MPS 时约 2 秒，否则用 CPU。模型缺失/无 torch 时 `auto` 自动回退经典边缘，并打印原因。

## 4. 各风格（简要）

| 风格 | 规划方式 |
|---|---|
| `anime_flat` | 去线底色、保边平滑、区域/局部颜色、单中心线；可用多边形或真实填充 |
| `sketch` | 中心线 + 排线 + 少量颜色点缀 |
| `anime_study` | 主体分割、线稿、少量填色、阴影、收尾勾线（见第 5 节） |
| `impasto` | 多层曲线笔触覆盖，颜色块 + 笔触纹理 |
| `watercolor` | 浅色块 + 留白 + 细线 |

本文其余部分只展开 `anime_study`。

## 5. anime_study 当前流程

像人临摹：先线稿，再大色块，再少量阴影，最后补细线。

1. **主体**：`client/anime_seg.py`（`models/anime-seg/isnetis.onnx`，ONNX Runtime）抠出角色；权重缺失时回退 GrabCut。
2. **线稿**（`client/study_lineart.py`，图层 1）：Anime2Sketch 线稿 → 骨架 → `merge_paths` 把在接点处断开的碎片按切线连续性接成长线 → 只在真实转角处切断，单笔不超过画幅 24% → 外轮廓缺口用主体轮廓补上。笔画分三级：外轮廓、主要内线（先画，phase `sketch`）、细节线（最后补，phase `detail`），按重要度（长度×墨色）取舍，再用最近邻排序，从左上起笔。
3. **填色**（图层 0，phase `color`）：Lab k-means 调色板 → 标签图平滑 → 每个色块一个 `fill` 多边形（只有几个角点，必须是简单多边形，自交的不发送）。按"填充外轮廓"面积从大到小画，嵌套的脸会盖在身体/头发之上。露出纸面的空洞不能用多边形盖住，这类色块退回粗笔 `paint_mass`。
4. **阴影**：最大的几个色块里偏暗的部分用同色加深 24% 的粗笔补几笔（phase `shade`）。
5. **单图层画布**：线稿放到填色之后（phase `detail`），否则色块会盖住线稿。
6. 预览按图层从低到高绘制；`execute_study` 按 `op.layer` 切图层并发送 `fill`。
7. 眼睛、人脸检测等特征提取已全部去掉。

评测：`tools/study_eval.py`（模糊后 Lab dE、SSIM、轮廓 IoU、外溢、空洞、边缘 P/R、笔画长度分布）。

## 6. 时间余量与绘制节奏（所有风格）

`--pace slow|instant`，默认 `slow`；工作台里是“绘制节奏”下拉。

- **保留时间**：`pacing.reserve_seconds` = 剩余时间的 12%，至少 3 秒，最多 20 秒。180 秒的回合可用 160 秒，120 秒可用 105.6 秒。所有风格同一规则。
- **预算起点**：读到预算的那一刻开始计时（`args.budget_anchor`），规划耗掉的时间不会再算一遍。
- **重读回合计时**：执行中每约 6 秒重读一次 `/round`，截止时间只会提前，不会推后。显式 `--time` 不受影响。
- **实测延迟**：每次请求都计时并平滑，用 1.3 倍的余量估算后面还需要多少不可压缩的时间。
- **slow（默认）**：
  - `anime_study` 每画一笔前重算：剩余时间减去后面所有笔的请求耗时，剩下的才能用来停顿。够用就按自然节奏；不够就按比例压缩停顿（最低压到 40%）。
  - 压到 40% 还不够，就从计划**尾部**逐笔裁掉（细节线、阴影最先丢），保证画面完整但更简单。
  - “停下来看一眼”的额外停顿只在时间宽裕时出现。
  - 其他风格把绘制拆成小批量，并按剩余时间摊开；落后时间隔缩到 0，同样用截止时间兜底。
- **instant**：不停顿，按批量连续发送；仍然在截止时间前停手。
- 不再出现“时间到了还没画完”：到点时画到哪算哪，且顺序保证已画部分是最重要的。

## 7. 规划进度（planlog）

规划期间 `client/planlog.py` 记录每一步的状态、耗时和细节。`--plan-progress-file` 指定输出的 JSON；工作台给规划作业自动传入 `<job>/plan_progress.json`，并在 `GET /jobs/<id>` 的 `plan_progress` 里返回。

- 步骤（`anime_study`）：读取回合时间 → 读取图片 → Anime2Sketch 线稿推理 → anime-seg 主体分割 → 整理线稿笔画 → 提取调色板 → 规划填色多边形 → 规划阴影笔触 → 按时间裁剪计划 → 渲染预览。其他风格和 `--plan-in` 步骤更少。
- 细节：模型权重加载、推理设备（mps/CPU）和输入尺寸、碎线数量、第几条曲线 `i/n`、第几个色块 `i/n`、保留笔画数。
- 模型前向没有真实百分比，只显示"进行中 + 已用时间"，心跳线程每 0.5 秒刷新一次。有计数的步骤（整理线稿、填色）才有步内进度条。
- 页面在进度条下显示步骤清单（✓ 完成 / ● 进行中 / – 跳过 / ✗ 失败）和各步耗时，进度条是真实的步骤比例；规划中每 0.4 秒轮询。
- 日志里同步输出 `[plan 3/10] ...` 行。没有 reporter 时所有调用都是空操作。

## 8. 大图自动压缩（`client/imagesize.py`）

`--max-dim` 不给时，工作图的最长边按**回合剩余时间**自动选，范围 480–960，取 20 的倍数；工作台的“最大边长”留空即自动，填数字则固定。

实测（M4，anime_study，2400 px 原图缩小后规划）：

| 最长边 | 400 | 560 | 720 | 900 | 1200 | 1800 |
|---|---|---|---|---|---|---|
| 规划耗时 | 6.4s | 8.5s | 10.5s | 11.6s | 15.8s | 34s |

两个模型是固定成本（Anime2Sketch 固定 512 px，anime-seg 固定 1024 px，合计约 3 秒），其余按边长的 1.6 次方增长，主要在“整理线稿笔画”。色差、SSIM、轮廓 IoU 在 720–960 之后不再提升，所以更大的工作图只多花时间。

公式：让规划时间不超过回合的 8%，即 `side = 900 * ((秒数*0.08/风格系数 - 4) / 7.6) ** (1/1.6)`，限制在 480–960。其他风格 900 px 的实测规划耗时分别是 flat 5.7s、sketch 8.6s、impasto 9.3s、watercolor 8.1s，按此折成风格系数。

6000 px 的 JPEG 测试：60 秒回合 → 326×480（规划 8.1s），120 秒 → 502×740（9.6s），180 秒 → 651×960（9.8s）。

解码也做了优化：JPEG 用 draft 模式按 1/2、1/4、1/8 解码，先缩小再合成白底，所以 12000 px 的照片不会变成整张 RGBA；按 EXIF 方向摆正；小图不放大。

## 9. 规划阶段的 CPU 占用（风扇噪声）

症状：Python 的 CPU 占用看起来不高，但风扇狂转。原因是线程池空转，而不是单核太慢。
在 960px 的图上，原来一次规划约 3.1s 墙钟时间却消耗约 11.1s CPU 时间（3.6 倍）。

- `study_lineart.merge_paths`：原来每次合并都重建 O(N²) 的 numpy 距离矩阵，还导入 `scipy.spatial`
  （导入本身就会拉起 BLAS 线程，0.23s 墙钟耗 2.2s CPU）。现改为网格哈希近邻 `_near_pairs` + 堆，
  `extract_lineart` 从约 4.0s 降到约 0.4s，输出笔画统计一致。
- `anime_seg`：onnxruntime 的 CPU provider 默认开满所有核并自旋（0.9s 墙钟耗 ~7 CPU 秒）。
  现优先使用 CoreML（`.cache/coreml` 缓存编译结果）：首次编译约 7s，之后加载 0.4s、推理 0.3s，CPU 约为原来的 1/3；
  CoreML 不可用时退回 4 线程且关闭自旋。
- `dg_artist`：在 numpy/cv2/torch 加载前把 `OPENBLAS/OMP/VECLIB/MKL_NUM_THREADS` 设为 2，
  并调用 `cv2.setNumThreads(2)`、`torch.set_num_threads(2)`；Anime2Sketch 连通域清理改为向量化。

结果（同一张图，`--dry-run`）：墙钟 3.06s → 2.56s，CPU 11.1s → 3.4s。可按 planlog 的阶段记录对比每个阶段的 wall 与 cpu 时间。

## 10. 单层画布（接力轮）的绘制顺序

接力轮等模块只有一层画布，`/api/v1/layer` 会返回 409。计划仍按三层生成，执行时由
`anime_study.adapt_layers` 压平：

- 顺序保持真人的习惯：起稿 → 填色 → 阴影 → 收尾勾线（`sketch → color → shade → detail`）。
- 颜色会盖住先画的起稿线，所以每个平涂填充向内缩 1px（按 1280px 宽栅格计算，`FLAT_INSET_PX`），
  像涂色时留在线内；缩后小于原面积 20% 的细碎填充保持原样，缩后分裂的块各自成为一个填充。
- 内缩越大线稿越完整，但相邻色块之间会露出白缝。实测（366 个操作）：内缩 1px 时填色面积保留 96%、
  缝隙约 4%，起稿线可见 38%；内缩 4px 时缝隙约 23%，起稿线可见 70%。默认取 1px 以保证色块连续，
  被盖住的线由最后的勾线（phase `detail`）补回。
- 多图层的画布不受影响，计划原样执行。

## 11. 预览

`render_preview()`：

- 3× 超采样，18 px 边距，画布比例默认 16:9；
- 笔宽 `size * fit_w / (100 * world_width) * 3`；
- 线用 `joint="curve"`，粗笔补首尾圆点；
- y 翻转：`screenY = oy + (1 - ny) * fit_h`；
- 最后 LANCZOS 缩回目标宽度。

**它不是游戏截图**。真实的抗锯齿、排序层、阴影和填充拓扑会有差异；数值对比见 `verification.md`。

## 12. 参数速查（study 相关）

| 参数 | 默认 | 作用 |
|---|---|---|
| `--time` | 读回合 | 可用秒数 |
| `--default-time` | 180 | 无计时器时的秒数 |
| `--colors` | 按时间 3–8 | 基色数，上限 10 |
| `--study-pause` / `--study-speed` | 0.26 / 0.90 | 逐笔停顿 |
| `--max-strokes` | 900 | 操作上限 |
| `--budget` | 自动 | 直接限制 units |
| `--rate` | 65 离线 / 11 联机 | 通用预算换算 |
| `--margin` | 0.04 | 画布留白 |
| `--seed` | 7 | 可复现 |
| `--plan-out` / `--preview` / `--study-stage-dir` | — | 输出计划/预览/四阶段累计图 |
| `--dry-run` | 关 | 只规划，不连桥接、不清空 |
| `--lineart` / `--lineart-threshold` / `--lineart-device` | auto / 45 / auto | 线稿模型 |

调参建议：

- **想更“厚涂”**：提高 `--colors` 会增多区域、反而降低单块覆盖率；优先调大 `--time`，或把阴影关掉（当前没有独立开关，可改 `detail_level` 阈值）。
- **想更“干净”**：降低 `--colors`、提高 `--study-pause`（更像人但更慢）。
- **短回合（<60 s）**：不要提高 `--colors`；眼睛和轮廓会被时间挤掉。
- **复现某次结果**：固定 `--seed`，且注意 `subject_mask()` 里 `cv2.setRNGSeed(7)` 是固定的；GrabCut 本身仍可能受 OpenCV 版本影响。

## 13. 硬性不变量（回归测试）

`tests/test_anime_study.py` 等守护以下契约，改算法时不要破坏：

- 计划里的 `fill` 必须是简单多边形（不自交），数量有限；
- 不出现两点长直线；颜色数有上限；
- 预算和截止时间不被突破，颜色/笔刷 setter 顺序正确；
- 单层画布不切图层，顺序是 起稿 → 填色 → 阴影 → 勾线；
- 主体不确定时不退化成整图填色。

运行：`python -m unittest discover -s tests -v`

## 14. 已知限制

- 复杂背景、多人、贴边构图可能漏肢体或选错主体；没有 AI 组件时分割退回 GrabCut，质量更低。
- 手指、衣褶、饰品在有限时间内只剩概括形状。
- 预览是近似重建，和游戏截图最多做到像素级接近，不是完全一致。
- 单层画布上颜色会盖住一部分起稿线（见第 10 节），用内缩填充和最后的勾线补偿。


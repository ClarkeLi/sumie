# Sumie Technical Whitepaper

**Version 1.0 · Language: English · [简体中文](whitepaper.zh-CN.md)**

> **Restrictions:** for learning and technical exchange only; no commercial use; **do not use it in online multiplayer games**; delete within 24 hours of downloading. See the full notice in the root README and Chapter 9.

> **Who is this written for?**
> - **Readers with no programming background:** read Chapter 0 (a ten-minute primer) and Chapter 1 and you will understand what Sumie is, why it is built this way and what happens inside the computer while a picture is drawn. Every term is explained with an everyday example the first time it appears, and there is a glossary at the end.
> - **Experienced developers:** Chapters 2–9 give the **data structures, formulas, constants, interfaces and edge cases** of every layer, and Chapter 10 is a step-by-step "rebuild it from scratch" guide with runnable code. After reading it you should be able to build an equivalent implementation without opening the source.
>
> Anything written in `code style` — function names, file names, constants — can be found in the repository. All numbers (constants, thresholds, measured results) come from the repository's code and tests.

## Contents

0. A ten-minute primer (for beginners)
1. The big picture: what happens after you press "Start drawing"
2. Game integration layer: how the plugin "hands a stroke to the game"
3. Perception: how a computer "understands" an illustration
4. Planning: turning a picture into an ordered stroke list
5. The human model: making every stroke look human
6. Time budget: how it never runs over
7. The Studio service and web workbench
8. Verification: judging quality without "feelings"
9. Security, privacy and fair play
10. Rebuild it from scratch (eight steps)
11. Parameter and interface cheat sheet
12. FAQ
13. Known limits and roadmap
14. Glossary
15. Third-party components and credits

---

## 0. A ten-minute primer (for beginners)

### 0.1 What does this project do?

Picture a draw-and-guess game: each round one person draws the prompt on a canvas and the others guess. Sometimes you would like to put a beautiful illustration you have on the canvas, but a round lasts a minute or two and drawing by hand is far too slow.

**What Sumie does:** you "paste" that illustration into a web page and click a button, and it **draws the picture stroke by stroke** into the game canvas — and the way it draws, the thickness of the lines and the speed look a lot like a player holding a pen tablet, and it guarantees to finish before the round ends.

### 0.2 Why not simply "control the mouse"?

The obvious idea is to write a program that moves the mouse pointer and presses the left button by itself. That is like **hiring a robot to hold the mouse for you**. It has three problems:

1. **It needs permissions.** Operating systems (macOS, for instance) are wary of "a program secretly controlling my mouse" and make you grant permission by hand, again and again.
2. **It is fragile.** The pointer position depends on window position, display scaling and whether you touched the mouse yourself. It works today and is off tomorrow when the window is a different size.
3. **It looks fake.** A robot moves at constant speed with one line width and stiff polylines; a person speeds up and slows down and presses lightly or hard.

**Sumie's way:** do not touch the mouse at all. Install a **plugin** in the game (think of it as a small "reception window" added to the game) and let a program **talk to the game directly**: "please draw a line between these coordinates, in this colour, this thick." The game draws the line itself, exactly as if you had drawn it — and it automatically goes through the game's own undo and multiplayer-sync paths.

### 0.3 How does a picture "become" strokes?

Compare it with how a person paints:

| How a person draws | Sumie's matching step |
|---|---|
| Look closely at what the picture shows, what is the character, what is the background | **Perception:** cut out the subject, extract line art, find the main colours |
| Sketch in your head: outline first, then colour, details last | **Planning:** decide which lines to draw, which colour areas to fill, in what order |
| Put each stroke down, light or heavy, fast or slow | **Human model:** give each stroke a pressure, a duration and a pause |
| Glance at the clock; hurry up when time is short | **Time budget:** calculate as it goes, speed up or drop a few strokes when needed |
| Put the pen on the canvas | **Plugin:** hand the strokes to the game |

### 0.4 Three key ideas

**① Pen pressure.** A pen tablet senses how hard you press. Light pressure makes a thin line, heavy pressure a thick one. The game supports this, so different players' lines have different widths. Sumie invents a plausible pressure for every stroke and pre-computes how thick the game will finally record it.

**② Layers.** Imagine several transparent plastic sheets stacked up: colour on the bottom sheet, black outlines on the top one. You can paint colour freely without covering the lines. The game offers only 3 "sheets"; Sumie's plugin cuts each one into 3 "bands", giving **9 virtual layers**, and other players see the same stacking order.

**③ Paint bucket.** The tool in drawing programs that fills a closed region with one click. Its weakness: if the boundary has one tiny gap, the colour "flows" over the whole canvas. Sumie first **simulates** the game's bucket on the computer, patches the line if it would leak, and only then lets the game really pour — so it does not blow up.

### 0.5 What is the project made of?

- **The plugin** (installed in the game): talks to the game.
- **The draw service** (a Python program on your computer): does the "thinking" — looks at the image, plans, simulates.
- **The web workbench** (a web page): talks to you — paste an image, preview, press start.

All three communicate only inside your own computer (address `127.0.0.1`); nothing goes to the internet and your pictures are not uploaded.

> If you do not need the details, you can stop here. Read on to learn why it is designed this way and exactly how it works.

---

## 1. The big picture: what happens after you press "Start drawing"

### 1.1 Architecture

<img src="../assets/architecture.en.png" alt="Architecture (English)">

<img src="../assets/architecture.zh.png" alt="架构图（中文）">

The system is **two processes and one web page**:

| Part | Where it runs | Responsibility |
|---|---|---|
| Plugin `DrawGuessBridge.dll` | inside the game process (BepInEx 6, Unity Mono) | serves HTTP / WebSocket on `127.0.0.1:8765`; moves requests onto the Unity main thread; turns strokes into game elements; provides the virtual pen and virtual layers; starts/stops the Python service from its config |
| Draw service `studio_server.py` | local Python child process, `127.0.0.1:8770` | single-worker job queue; image loading, line art, segmentation, planning, preview, metrics; executes strokes one by one and reports progress |
| Web workbench `ui.html` | served by the plugin on `8765`, self-contained, no external CDN | paste/drop an image, choose style and options, preview, draw, undo/redo/export/restore, show the live word and countdown |

**One design principle runs through everything:** *all geometry and aesthetics live in Python* (fast to change, easy to test), and *C# does exactly one thing — move requests safely onto the Unity main thread and turn them into game elements*. Game updates and style iterations then do not drag each other along.

### 1.2 Twelve steps

When you paste an image in the page and click "Start drawing":

1. **The page** packs the image (plus the style, seconds and region you chose) and POSTs it to `POST /jobs` on `8770`.
2. **The job queue** accepts it, assigns an id (like `20261104-180000-ab12cd`), creates `.cache/studio/jobs/<id>/` and stores the original image.
3. **Read the round time:** the draw service asks the plugin (`GET /api/v1/round`) "how many seconds are left in this round?". The single-player Sketchbook has no timer, so the seconds you typed are used.
4. **Perception:** load and scale → segment the subject → extract line art → extract the palette.
5. **Planning:** produce an **operation list** (`line`, `disc` for dots, `fill` for filled polygons), each with coordinates, colour, width, layer and phase.
6. **Human model** adds rhythm: how long each stroke takes, its pressure curve and the pauses between strokes; and rehearses each paint-bucket pour in the offline bucket model, patching gaps to prevent leaks.
7. **Fit to time:** compute the total duration and, if it exceeds the deadline, delete from the **end** of the list (the least important details).
8. **Render a preview** PNG and compute numeric metrics (coverage, colour error, estimated time). The page shows them so you can check.
9. You press "Start drawing": the executor **reuses the plan just made** (no re-planning).
10. **Execute:** before each stroke the executor checks how long is left until the deadline, decides how fast to draw it, and sends a request to `8765` (the whole stroke at once, or replayed frame by frame through the "virtual pen").
11. **The plugin** queues the request, takes it out on the next Unity main-thread frame, builds the game's own "line information" and asks the game to create the line.
12. **The game** adds the line to the canvas, records it on the undo stack and synchronises it to the other players in the room. Progress is reported all the way back to the page.

### 1.3 Why three parts?

- **A game can only host C# (.NET) plugins**, while the image-processing ecosystem (OpenCV, PyTorch, ONNX) lives in Python. Splitting lets each side do what it is good at.
- Putting the heavy planning in a **separate process** never freezes the game's picture.
- The page is served by the plugin, so users install nothing extra: open the browser and go.

---

## 2. Game integration layer: how the plugin "hands a stroke to the game"

### 2.1 Background: BepInEx and plugins

A Unity game is a compiled program. **BepInEx** is an open-source "plugin loader": when the game starts it loads itself ahead of the game code, looks for `.dll` files in `BepInEx/plugins/`, and instantiates the classes marked `[BepInPlugin]`. Our code then **runs inside the game process** and can call the game's internal classes and functions directly (provided the game uses the **Mono** runtime; IL2CPP builds compile code to native code that BepInEx 6's Mono loader cannot handle — which is why the installer refuses IL2CPP builds).

The plugin entry is short (simplified from `Plugin.cs`):

```csharp
[BepInPlugin("com.clarke.drawguess.bridge", "Draw & Guess Bridge", "1.0.0")]
public sealed class BridgePlugin : BaseUnityPlugin
{
    void Awake()   { /* read config; start the HTTP server; start the Python draw service if enabled */ }
    void Update()  { _server.Pump(); }       // every frame: handle queued actions
    void OnDestroy(){ /* stop the server, stop the Python child process */ }
}
```

**How is it compiled?** The plugin references the game's own assemblies (`Assembly-CSharp.dll` and friends) and BepInEx's assemblies. The repository **does not ship** them: `tools/install.py` copies the needed DLLs from the game folder into `.cache/game-managed` on your machine, downloads and verifies BepInEx into `.cache/bepinex`, then builds with `dotnet build` (target `netstandard2.1`). That is how "no game files are redistributed" is implemented.

### 2.2 Why a "main-thread dispatcher"

Unity has an iron rule: **only the main thread may touch game objects** (create lines, read the canvas, …). An HTTP server must handle many requests at once, so it has to run on other threads.

An analogy: the game's main thread is a **clerk who only works at the counter**, the HTTP threads are **greeters in the hall**. A greeter cannot enter the counter; they drop your slip (an `Action`) into the **in-tray**, and each frame the clerk takes slips from the tray, handles them and hands the result back. That in-tray is `MainThreadDispatcher`:

```csharp
// simplified from MainThreadDispatcher.cs
ConcurrentQueue<Action> _queue;
public void Post(Action a) { if (IsMainThread) a(); else _queue.Enqueue(a); }
public void Pump(int max = 256)          // called by Update() every frame
{
    for (int i = 0; i < max && _queue.TryDequeue(out var a); i++)
        try { a(); } catch (Exception e) { Log.LogError(e); }
}
```

Details:

- **At most 256 actions per frame**, so one frame is never flooded and the picture does not stutter.
- Ordinary requests wait via `InvokeJson()` with a **15-second timeout**, returning HTTP 504.
- A batch of strokes is a "long operation" waiting up to `LongOperationTimeoutSeconds` (180 s by default); it runs in a **coroutine** that does `yield return null` every `StrokesPerFrame` (24 by default) strokes.
- Every WebSocket message also goes through the dispatcher — **never** do long work inside the WebSocket loop.

### 2.3 Three coordinate spaces — the easiest thing to get wrong

| Space | Range | Origin | Used for |
|---|---|---|---|
| **Normalised canvas** | `x, y ∈ [0, 1]` | **bottom-left**, y **up** | `points` in the API, plan JSON |
| Canvas world | `x ∈ [-8.89, 8.89]`, `y ∈ [-5, 5]` | canvas centre | inside the game, debugging |
| Screenshot pixels | e.g. 1920×1080 | top-left, y **down** | preview, comparison with screenshots |

Conversion (`GetCoords`):

```text
worldX = surface.MinX + nx * surface.Width
worldY = surface.MinY + ny * surface.Height
screen pixel y = canvas_top + (1 - ny) * canvas_pixel_height     # flip for screenshots
```

**Why "y up"?** Because the game's world coordinates are y-up; in Python everything uses the game's direction and the flip happens exactly once, when drawing onto an image. How to check: export `/api/v1/lines`; strokes at the top of the head have y ≈ 0.92 and at the feet y ≈ 0.07.

All coordinates are `Clamp01`-ed on the way in; `NaN` / `Infinity` returns HTTP 400.

#### Placing an image on the canvas (`make_transform`)

The canvas is 16:9 (world 17.78×10); images vary, so scale uniformly and leave a margin:

```text
fit_w = fit_h = 1 - 2*margin                      # margin defaults to 0.04
if the image is wider:  fit_h = fit_w * canvas_aspect / image_aspect
else:                   fit_w = fit_h * image_aspect / canvas_aspect
nx = origin_x + (x / image_width)  * fit_w
ny = origin_y + (1 - y / image_height) * fit_h    # flipped to y-up
```

**Worked example:** a 480×300 image (aspect 1.6), canvas aspect 1.778, margin 0.04. The image is narrower than the canvas, so `fit_h = 0.92` and `fit_w = 0.92 × 1.6 / 1.778 = 0.828`; `origin_x = (1-0.828)/2 = 0.086`. The image centre (240, 150) maps to (0.5, 0.5) and the top-left corner (0, 0) maps to (0.086, 0.96).

### 2.4 Converting pen width

The game's line width is **exactly `size / 100` world units** (`size` is the 1–255 brush number in the UI). In pixels:

```text
px = size × canvas_pixel_width / (100 × canvas_world_width)       # canvas_world_width = 17.77778
```

On a 1920-px-wide canvas, size 1 is about 1.08 px and size 30 about 32 px. The preview renderer, the web preview and the comparison tools **must use the same formula**, otherwise you get "the preview is thicker than the real thing" (it really happened in the project's history; once fixed: preview vs. actual hollow 0.0 %, ink ratio 0.95, line width 5.7 vs 5.5 px).

**Line ends and corners:** the game draws lines with **flat ends and round joins** (joins are only round when "high line quality" is on or `RoundLineJoins=true`). For round-ended pens the only way is to add a same-colour `disc` whose diameter equals the line width at each end. That is documented: `RoundLineJoins` only fixes corners, it is not "round caps".

### 2.5 How one stroke lands in the game

This is `GameApi.ApplyOneStroke()`:

1. Check `CanDraw` (unless the request says `force=true`).
2. Coordinates: `space=normalized` is used as is; `canvas` / `world` are converted first.
3. Normalise the type: `line` needs at least 2 points (1 point becomes a `disc`); `disc` keeps only the first point; `fill` needs at least 3 points and drops a last point equal to the first.
4. Colour defaults to the current brush colour; size to the current brush.
5. Build `LineInformation`: `DrawElementType` 0 = line, 1 = disc, 3 = fill; `IsNormalized = true`; `AspectRatio` and `ScaleFactorWidth` from the canvas.
6. Call `DrawHelpers.SpawnDrawElement` to get a `DrawableLine` / `DrawableDisc` / fill.
7. Force round joins (`RoundLineJoins`).
8. Add it to the current module's "created elements" set and run `OnFinishDrawingCurrent` (a trimmed version for Battle Royale, so that every line does not trigger machine-learning recognition).

**A game quirk found by testing:** after each fill the game leaves it "selectable", and the next stroke or colour change **recolours it with the current colour**. Normal players switch tools and never trigger it; the bridge never switches tools and does. Two countermeasures: the plugin calls `LineFillable.SetLineSelectActive(false)` after every `RealFill`, and the executor calls `tool("Brush")` after every pour.

### 2.6 HTTP / WebSocket interface (the core ones)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/state` | can draw, tool, colour, brush, canvas size, layers, element count |
| `GET /api/v1/game` | `state` + round timer + current word, previous word, word history |
| `GET /api/v1/round` | every `Timer` in the scene and `recommended.remaining` (seconds left) |
| `GET /api/v1/lines?limit=N` | export **your own** strokes (`points` already normalised) |
| `POST /api/v1/strokes` | draw lines / dots / fills in bulk |
| `POST /api/v1/stroke/pen` | replay one stroke frame by frame through the virtual pen (with pressure) |
| `POST /api/v1/fill` | the real paint bucket (by seed point), returns the new region's area |
| `POST /api/v1/undo` `redo` `clear` `submit` `color` `brush` `tool` `layer` | the usual operations |
| `GET /api/v1/layers` | virtual-layer capabilities |

A minimal `POST /api/v1/strokes` body:

```json
{
  "clear": false,
  "strokes": [
    {"type": "line", "color": "#FF3355", "size": 10, "space": "normalized",
     "points": [[0.10, 0.20], [0.25, 0.80], [0.50, 0.30]]},
    {"type": "disc", "color": "#000000", "size": 8, "points": [[0.5, 0.5]]}
  ]
}
```

Response: `{"applied": 2, "failed": 0, "requested": 2, "errors": []}`. If a `Token` is set in the config, requests must carry the header `X-Api-Key: <token>` or `Authorization: Bearer <token>`. Full fields are in [`api-reference.md`](api-reference.md) (Chinese).

### 2.7 Plugin configuration (`BepInEx/config/com.clarke.drawguess.bridge.cfg`)

| Section / key | Default | Meaning |
|---|---|---|
| `Server/ListenAddress` | `127.0.0.1` | listen address — **do not** expose it |
| `Server/Port` | `8765` | bridge port |
| `Server/Token` | empty | if non-empty the API needs the token |
| `Drawing/StrokesPerFrame` | `24` | max external strokes applied per frame |
| `Drawing/RoundLineJoins` | `true` | force round joins at vertices |
| `Drawing/LongOperationTimeoutSeconds` | `180` | long-operation timeout |
| `Layers/BandsPerLayer` | `3` | bands per real layer (2–8); virtual layers = 3 × this |
| `Layers/SortExportByLayer` | `true` | sort exports (gallery, PNG/SVG) by layer |
| `Studio/Enabled, Python, Script, WorkingDirectory, Port` | written by the installer | whether to auto-start the draw service and where it lives |

---

## 3. Perception: how a computer "understands" an illustration

Goal: from a bitmap get three things — **where the subject is**, **what the line art is** and **which main colours exist**.

### 3.1 Loading and scaling

The longest side is chosen automatically between **480 and 960 px** from the time left: short time → smaller image (faster planning), long time → more detail kept. `dg_artist.py` decides automatically; in Studio an empty "max side" means automatic.

### 3.2 Subject segmentation (`anime_seg.py`)

It uses the **ISNet** model from `anime-segmentation` (ONNX format, `models/anime-seg/isnetis.onnx`, run with ONNX Runtime) and outputs a mask of "which pixels belong to the character". Without the model it falls back to OpenCV's **GrabCut** (a classic foreground-extraction algorithm).

Why cut out the subject? Fine background texture hardly helps the result look right, yet eats the precious stroke budget.

### 3.3 Line-art extraction (`study_lineart.py` + Anime2Sketch)

Line art decides "how much it looks like the original". The pipeline:

1. **Anime2Sketch** (a U-Net neural network; `client/anime2sketch_net.py` is a re-implementation that loads the official `netG.pth` weights) turns the colour picture into clean black-and-white line art. About 2 s with CUDA or Apple MPS, otherwise CPU. Without torch / the model it falls back to classical edge detection (gradient / ridge).
2. **Skeletonisation:** thin lines of unequal width down to 1-px centre lines (`artist_geometry.py`).
3. **Joining** (`merge_paths`): a skeleton breaks into many fragments at crossings. Fragments with similar direction are joined by **tangent continuity** into long lines — a person draws straight through a crossing instead of lifting the pen at every junction.
4. **Cutting:** break only at real corners, and a single stroke is at most **24 %** of the frame (a hand cannot draw longer ones).
5. **Closing the outline:** gaps in the outer contour are filled from the subject mask's outline.
6. **Tiers and selection:** each line is scored by "length × ink darkness" and put in one of three tiers — **outline**, **main inner lines** (phase `sketch`, drawn first) and **detail lines** (phase `detail`, added last); then ordered by nearest neighbour (starting at the top-left, always picking the start point closest to the current pen tip) to shorten pen-up travel.

### 3.4 Palette and colour regions

1. **k-means in Lab colour space** picks a few base colours (by default chosen from the time, usually 3–8). In Lab, numeric distance is closer to the colour difference the eye sees, so the clusters look more natural.
2. Smooth the **label map** (which base colour each pixel belongs to) to remove noise.
3. Take each region's outer contour and simplify it to a **simple polygon with only a few corner points** (a `fill` operation).
   - **Self-intersecting polygons are never sent:** the game triangulates them into one big dark blob, while the preview (even-odd rule) leaves them empty, making preview and result differ.
   - A "hole" that shows paper cannot be covered with a polygon; such regions fall back to coarse brush strokes (`paint_mass`).

### 3.5 Characters cut by the frame (`frame_cut.py`)

The bottom of a half-length portrait or a cut-off arm leaves a **dead-straight cut** at the image edge; copied as is, it screams "traced from the picture". The treatment:

1. detect the stretches where the character meets each image edge;
2. replace the straight edge by an **irregular curve** that bows into the character with slow waves and tremor (bow about 5 % of the character's size) and clip the mask to it;
3. line art near the curve ends at **random positions**, some of it turning into shorter and fainter "vanishing lines";
4. along the new edge draw no ink line and no dark border: colour regions simply end at an "invisible, undulating wall".

Numeric check `tools/frame_check.py`: along every cut edge, how often painting stops at the **same pixel position** — copying gives 0.76–1.0, after treatment 0.2–0.35 (> 0.4 fails). Cost: about 5–8 % of the character is trimmed, so LPIPS to the original is slightly worse (about 0.005–0.03) — a deliberate trade-off.

---

## 4. Planning: turning a picture into an ordered stroke list

### 4.1 What an "operation" (Op) looks like

The planner outputs a **deterministic** operation list (same input + same random seed → same output). Each operation roughly contains:

```text
type     line | disc | fill
points   [[x, y], ...]                 normalised, y-up
color    #RRGGBB
size     brush number 1–35 (line width)
layer    real layer or virtual layer
phase    sketch / color / shade / detail ...
role     construction / contour / inner / detail / accent / gap_close / shade / fill
alt      spare seed points for a bucket pour
```

Determinism matters: it lets preview and drawing reuse one plan and lets tests compare JSON.

### 4.2 What to draw and in which order (the `anime_human` order)

1. **Line art:** grouped by body part (top to bottom), long outlines before short details; each stroke overshoots a little at both ends (as a person does when finishing a stroke); horizontal lines tend to run left→right, vertical lines top→bottom.
2. **Flat colouring (flatting):** for each colour region first ask "do the existing lines already enclose it?" — if so pour or paint right away; otherwise patch the gap with one or two short lines, and outline the region only as a last resort (the outline uses **the region's own colour** and overshoots at both ends to meet its neighbours' lines).
3. **Brush masses and shading:** thick strokes with fairly high pressure.
4. **Detail line art last:** eyes, folds, hair strands.

**Time split** (tuned for rounds of about 120 s): line art is the skeleton and takes about **38 %** of the round — two thirds of it first (outline and structure), one third kept for interior details after the colour; flatting runs to about **88 %** minus that detail reserve; if time is left, it keeps flatting the remaining regions instead of lengthening pauses. In short rounds "**completeness beats detail**": less line art and more colour, so the picture is whole when time is up.

### 4.3 Why the detail line art must come after the colour

Colour (especially large masses) covers lines already drawn. So the final detail lines are drawn after the colour, or on a **higher layer**. That is exactly the problem virtual layers solve (see 5.4).

---

## 5. The human model: making every stroke look human

`anime_human` only decides **how** to draw, not **what** to draw. It is made of four independent, individually testable models.

### 5.1 Pressure model

#### 5.1.1 The game's pressure rules (read from its code)

- With pressure detection on, the line width follows the pressure live while drawing, **but only the local player sees it**.
- When a stroke ends, the `LineInformation` synchronised to everyone carries **a single `BrushSize`** (one width for the whole stroke).
- How `BrushSize` is computed: for each frame's pressure `p` (only `p > 0.05` counts as "pressed") compute `int(2 + 33 × min(1, p))` and store it in a **10-slot ring buffer**; when the stroke ends, average the **5 readings from the 5th to the 9th newest**; the last 4 frames before pen-up do not count.

So a real tablet player leaves strokes that **differ in width from stroke to stroke but are uniform inside a stroke**.

#### 5.1.2 Sumie's port (`motor.py::game_brush_size`, verbatim excerpt)

```python
PEN_MIN, PEN_MAX, SMOOTHING, DROP, CUTOFF = 2, 35, 10, 5, 0.05

def game_brush_size(pressures):
    values = [0] * SMOOTHING
    index = consider = 0
    for p in pressures:
        if p > CUTOFF:
            values[index] = int(PEN_MIN + (PEN_MAX - PEN_MIN) * min(1.0, p))
            index = (index + 1) % SMOOTHING
            consider = min(consider + 1, SMOOTHING)
    if consider == 0:
        return None
    if DROP >= consider:
        return int(sum(values[:consider]) / consider)
    total = 0
    for i in range(consider - DROP):
        total += values[(index - DROP - i) % consider]
    return total // (consider - DROP)
```

**Worked example:** a stroke with 12 frames of constant pressure `0.5` gives readings `int(2 + 33×0.5) = 18`, so `BrushSize = 18`. With constant pressure `0.062` (just above the 0.05 threshold) the reading is `int(2 + 33×0.062) = 4` and the result 4 (the project notes say "the thinnest stored width is 3", matching the pressure floor `PRESSURE_FLOOR = 0.052`). With only 3 frames of pressure 0.5 before pen-up the "5th to 9th newest" window lacks readings and degrades to averaging what exists — still 18.

**Inverting it:** for a desired `BrushSize = S` the pressure needed is about `p ≈ (S − 2) / 33`; add a little random jitter and make sure **every frame is > 0.05** (otherwise the game splits one stroke into two). The executor inverts the game's own smoothing to shape the pressure curve so the stored `BrushSize` hits the target (measured total error 9 over about 200 strokes — small).

#### 5.1.3 Two details against "clipped starts"

- Just after touchdown the game starts recording a frame or two late and would shave 1–4 cells off the start → each stroke **holds for 70 ms** at its start before moving.
- Every frame's pressure must be `> 0.05` until pen-up.

#### 5.1.4 Two ways to put a stroke down

- **`direct`:** after finishing a stroke, send the whole line in one request with the modelled `size` (`POST /strokes`). Needs neither pressure nor the virtual pen.
- **`native`:** the plugin creates a **virtual `Pen` device** (a digital pen in Unity's new Input System) and **replays** the `[t_ms, x, y, pressure]` samples frame by frame (`POST /stroke/pen`) through the game's **own** input path: the line really grows frame by frame, and `LinePressure` itself computes `BrushSize`, simplifies points, synchronises and records undo. Do not move the mouse during playback (`Pointer.current` would switch back to the mouse, pressure drops to zero and the stroke ends early); while the game is in the background the plugin temporarily sets `backgroundBehavior = IgnoreFocus` and turns on `runInBackground`, restoring both afterwards. **After 3 consecutive strokes the game did not accept it falls back to `direct`.**

`--pen auto` (default): use `native` when pressure is on in the game and the plugin supports the virtual pen, else `direct`.

#### 5.1.5 The "feel" pressure of each stroke kind

Different strokes are made with different hand force (`ROLE_PRESSURE`, as a fraction of the sensor range):

| Role | Pressure range | Meaning |
|---|---|---|
| construction | 0.065–0.11 | very light |
| contour | 0.07–0.17 | light to medium |
| inner | 0.065–0.14 | light |
| detail | 0.062–0.105 | lightest |
| accent | 0.18–0.34 | heavier |
| gap_close | 0.07–0.13 | light |
| shade | 0.062–0.10 | light |
| fill (thick brush) | 0.30–0.55 | broad sweep; pressure searched for the size |

Each simulated "person" also has a **habit factor** for the whole drawing, `habit = exp(N(0, 0.12))` (heavier or lighter than average today), which keeps the "feel" consistent across a drawing for a given seed.

### 5.2 Pacing model

#### 5.2.1 Where the data comes from

`tools/calibrate_motor.py` reads the raw Google **Quick, Draw!** data (human doodles with per-point timestamps, about **85,000 strokes**) and fits:

- **Stroke duration vs. length power law:** `duration = a × length^b × noise`, defaults `a = 0.885, b = 0.616`, log-normal noise (`σ = 0.62`). Length is relative to the longer side of the drawing. Meaning: longer strokes take longer, but **not linearly** (long strokes are drawn faster).
- **Pauses between strokes:** log-normal, `μ = −0.79, σ = 0.72`, and growing linearly with the distance moved to the next start point: `pause ≈ 0.44 + 0.5 × travel`.
- **Bell-shaped speed profile:** a stroke is divided into 20 segments with relative speeds (slow start, fast middle, slow end): `[0.63, 0.82, 0.98, 1.08, 1.13, 1.15, 1.15, 1.13, 1.10, 1.07, 1.04, 1.00, 0.96, 0.92, 0.88, 0.84, 0.80, 0.75, 0.70, 0.63]`.
- **Curvature–speed power law** (β = −0.416): the sharper the turn, the slower.

**Only these summary statistics ship with the repository** (`client/calibration/quickdraw_motor.json`); the raw data is not included. The data has no pressure, so the pressure curve is modelled on the "rise–plateau–decay" shape common in handwriting and tablet studies.

#### 5.2.2 Time factors per stroke kind

`ROLE_TIME`: construction 0.8, contour 1.0, inner 0.95, detail 0.85, accent 1.2, gap_close 0.7, shade 1.05, fill 0.75 (smaller is faster), further divided by the "skill" `skill` (default 2.3, adjustable; a larger number draws faster and smoother). A stroke's duration is clamped to **0.16–3.6 s**.

### 5.3 Sampling: turning a stroke into per-frame points

The game **records a point only after the pen has moved a minimum distance** (`MIN_STEP_WORLD = 0.015` world units) and reads input once per frame (`--game-fps`, default 240; `tools/probe_pen.py` can measure the real value). `sample_stroke()` walks the path by "time + speed profile" producing `[t, x, y, pressure]` with a rise–plateau–decay pressure curve. The frame rate matters because it decides how long before pen-up the "5th–9th newest" readings lie.

### 5.4 Virtual layers and brush colouring

#### 5.4.1 The problem

A paint bucket needs the target region closed by lines **on the same layer**. One gap and the colour floods out; patch lines (dark borders) make the picture look messy. People use layers: line art on one, colour on one below it, paint freely, never cover the lines.

#### 5.4.2 The game's limit and the way around it

The game has only **3 sorting layers** (`SHAPES_1..3`, each with a `_FILL` twin) and none can be added at run time. **But every line is synchronised with a `Layer` and a `SortingOrder` value, and the receiver places it by those two values.** The plugin exploits that: it cuts `SortingOrder` into **bands**, so

```text
virtual layer v  =  (real layer = v ÷ bands rounded down,  band = v mod bands)
SortingOrder     = -28671 + band × 20479 + counter
```

With the default `bands = 3` that gives **3 × 3 = 9 virtual layers**. Rules: **a higher layer always covers a lower one, whatever the drawing order**; inside one real layer, fills (bucket) always lie under lines. Because those two values synchronise to every player, **others see the same stacking order and do not need the plugin**.

To make a stroke "be on the right layer from its very first frame", the plugin uses a Harmony patch at the moment the game creates the line (`DrawableElement.Init`) to rewrite the sorting layer and order (`nativePatch`); to make the gallery / PNG / SVG exports come out in the right order, exported lists are sorted by `(Layer, is-fill, SortingOrder)` (`exportSort`).

#### 5.4.3 Mapping roles to layers (`layers.py`, with 9 layers)

| Layer | Role | Content |
|---|---|---|
| 0 | sketch | (reserved) |
| 1 | flat | large colour areas |
| 2 | flat_top | regions smaller than 2 % of the total area |
| 3 | shade | shading strokes |
| 4 | light | colour accents |
| 6 | ink | line art, patch lines |
| 7 | top | (reserved, above the line art) |

With fewer than 9 layers the roles share layers (**5 is the minimum**); with fewer than 5, or if the plugin has no `/api/v1/layers` (an old plugin, game not restarted), it **falls back to the paint bucket automatically**.

#### 5.4.4 The brush-colouring algorithm (`brush_fill.py`)

Goal: like a person, sweep back and forth with a thick brush, follow edges with smaller ones and dab corners with the smallest. It works on a raster **twice as fine** as the bucket model's (brush size s is s pixels wide, because a game collision cell is 0.02 world units while line width is `s/100`).

1. **Allowed area** = the colour polygon ∪ the part within 18 cells of (line art ∪ colours drawn later): the brush may lie over lines and over colours that will cover it later, but never on bare paper and never over a colour already finished.
2. **Thick parts** (where a size-35 brush fits with room to spare): first go once round the edge, then sweep back and forth along the long axis (zigzags joined into long strokes), with a row spacing of **0.86** of the brush width.
3. **Thin parts** (hair strands, edges): take the medial axis, join segments of similar direction at forks, and draw each with just-wide-enough brush (**35 / 22 / 12 / 6 / 3**) in one stroke.
4. **Every stroke must add at least 4 %** of new coverage or it is dropped; coverage target **93 %** (85 % for small regions); the outermost cell is not required (it is under the line art).
5. Regions of the same colour are drawn consecutively (respecting "later is on top"), changing colour rarely; round one gives every region a first colour, round two finishes the edges (only inside the region itself so it never covers a neighbour).
6. Finally: where a region edge has no line, add a thin dark border (on the `ink` layer), clip shading and accent strokes to the parts where the **whole brush** lies in the region, then draw the detail line art.

### 5.5 The offline paint-bucket leak model (`bucket.py`)

The rules of the game's paint bucket (`ShapeFiller` + `CollisionMatrix`) are ported into an offline model:

- the canvas is divided into a grid of **0.02 world units**;
- every **polyline** is a wall on that grid with half-width `= line width/100 × 0.2` on each side;
- **discs do not block**; **fill polygons** add their edges;
- the game first simplifies every line (`GAME_SIMPLIFY = 0.01` world units) and the model does the same.

**While planning,** each region to be poured is first tried in the model:

1. leaks → find "the narrowest point of the escape path" and add a short line there;
2. still leaks → outline the whole region;
3. the region is split into several pieces by lines → pour once per piece;
4. very small or thin regions are not poured; a single "dab" stroke is used.

**While executing:** the game's `POST /fill` returns the new region's real area; **real > predicted × 1.8 means `undo`**, find the gap again using the *real* lines in the game, patch it and pour again (the game simplifies recorded points and occasionally shows gaps the model never saw). A pour that fills less than **35 %** of the expected area first tries a **spare seed point** (`Op.alt`) and if that fails is undone, leaving no stray bits. The seed point is "**the deepest point in the real cells**" (farthest from the walls), not the centre of the colour polygon — the centre can lie in a small pocket on the other side of a wall (a trap that bit once: a pour expected to give 0.0095 gave only 0.0001).

Measured: about 200 strokes, 31 pours, **0 leaks, 0 repairs**.

---

## 6. Time budget: how it never runs over

### 6.1 Two budgets — do not mix them up

**Layer one: the generic stroke budget** (`dg_artist.resolve_budget`)

```text
remaining  ← priority: /round.recommended.remaining → --time → --default-time (180)
usable     = remaining − max(2, remaining × 0.08)           # keep 8 % back
rate       = 11 units/s online; 65 units/s in the offline Sketchbook
cap        = sketch 1600 · anime_flat 2200 · anime_study 900 · impasto 3000 · watercolor 2000
```

**Layer two: the execution deadline (absolute time)**

```text
planning_start = time.monotonic() BEFORE image loading / model inference
deadline       = planning_start + usable
```

The executor checks it **before every stroke and every colour change**; the time spent planning, clearing and switching layers **never resets** it. So `--time 180` really means "finished within 180 s counted from the start of planning", **not "draw for 180 s after planning"**.

`pacing.py` adds safety rules: keep back **12 %** (at least 3 s, at most 20 s); measured request latency is trusted only after multiplying by a **1.3** margin; the live round timer is re-read every **6 s** and the deadline **can only move earlier, never later**; the longest inter-batch pause in slow mode is 1.2 s; pauses are compressed to at most **40 %** (`SPEED_FLOOR`).

### 6.2 Whole-round strategy with "auto (read round)" (`anime_human`)

- **Planning uses the longest the round can be** (the timer's `peak = max(total, elapsed + remaining)`): in a 180-s round that has only 120 s left when drawing starts the plan is still the 180-s picture; the content budget is `peak − 30 s`.
- **Execution must finish within "current remaining − 30 s"** (at most 40 % kept back in a short round), leaving the player time to adjust.
- **Dynamic compression:** before each stroke compute the ratio

```text
ratio = usable time left ÷ sum of the "natural durations" of all remaining strokes       # floor 0.3 (about 3× faster)
```

so the same picture is drawn faster: what is compressed are stroke durations and pauses; the native pen also compresses each stroke's sample times proportionally (the 70-ms hold after touchdown is kept). If that is still not enough, **strokes are dropped from the end of the plan (the least important detail lines)**.

**Measured example:** 290 operations planned for 180 s but given only 90 s (deadline 79 s): 277 were drawn within 79 s, all through the native pen, with no failures and no colour leaks.

### 6.3 A common misunderstanding

"Does a longer time give a finer picture?" Yes. Time only makes it **more, never less**: planning follows the longest round time and the operation count grows with the budget (we measured: for one image a 170-s budget gives about 356 operations and 300 s about 555, and only then do the face details clearly appear).

---

## 7. The Studio service and web workbench

### 7.1 Job lifecycle

```text
queued → planning → planned (preview ready) → drawing → done
                        └────────────────────→ cancelled / failed / interrupted
```

- **Single-worker queue:** one plan/draw at a time, so two pictures never fight over the pen.
- Job folder `.cache/studio/jobs/<id>/`: `input.png` (original), `plan.json`, `preview.png`, stage images, `progress.json`, `run.log`, `metrics.json`, `job.json`. **Reloading the page never interrupts a job** — the job lives in the Python service and the page just polls.
- **Plan reuse:** after previewing, drawing reads `plan.json` (`--plan-in`) instead of planning again.
- **Region:** `--region x,y,w,h` (normalised, y-up, origin bottom-left) draws only inside part of the canvas.
- **Export / restore:** `GET /lines` exports the current canvas as JSON; `tools/restore_canvas.py` or the page's "Restore strokes" replays it, to save progress before restarting the game.
- **The canvas is kept by default:** clearing before drawing must be ticked and confirmed manually.

### 7.2 Numeric metrics

| Metric | Meaning |
|---|---|
| Coverage | how much of the subject the plan covers |
| Overflow | share painted outside the subject |
| Colour error | colour distance between the painted parts and the original |
| Curvature ratio | how curved the strokes are (are they too straight?) |
| Predicted leaks | number of leaks the offline bucket model predicts |
| Strokes / minute | pacing |
| Estimated time | seconds needed to complete the whole plan |

### 7.3 How the page's localisation works

The page is **one self-contained HTML file** (no CDN, no build step) whose source language is Chinese. Translations are not scattered through business code; there is a **dictionary plus an automatic translation layer**:

1. The dictionary is an array `I18N_ROWS` with 5 columns per row: `[Chinese source, English, 日本語, 한국어, Deutsch]`.
2. Rows containing `{0}` `{1}` are **patterns** (for example `已用 {0}s`); at run time they become regular expressions, and captured pieces are **translated recursively** before filling the template.
3. Text joined as `A · B · C` (for instance the status bar) is split at ` · ` and translated piece by piece.
4. A `MutationObserver` watches the whole page: **text and attributes written after load** (`placeholder`, `title`, `aria-label`, `alt`) are translated automatically; each text node remembers its own Chinese original and re-translates from it when the language changes, so switching back and forth never accumulates errors.
5. Whole explanations containing HTML tags (with `<code>` inside) are marked `data-i18n-html` and the whole paragraph is the dictionary key, avoiding the word-order damage of piecewise translation (Japanese and Korean order differs a lot from Chinese).
6. `window.confirm()` is wrapped as well.
7. The choice is kept in `localStorage`, defaults to the browser language, and falls back to English.

**Why not an existing i18n library?** The page has to be **dependency-free**, work offline and be a single file. A dictionary is the simplest thing at this size (about 300 entries). `tests/test_ui_i18n.py` checks that every row has 5 columns, that placeholders match across languages, that no source string is duplicated and that every Chinese text node in the page has a translation.

### 7.4 Ports and cross-origin

The page is served by `8765` and the draw service listens on `8770`, so the browser makes **cross-port requests**. The draw service sends CORS headers; preview images need `crossOrigin="anonymous"`, otherwise the `<canvas>` is "tainted" and reading pixels throws `SecurityError`. Polling rates: game state about 1 s, canvas about 1.8 s (0.8 s while drawing), the active job 0.6–0.8 s, the job list 8 s.

---

## 8. Verification: judging quality without "feelings"

"It looks fine" is subjective and cannot go into an automated test. So the project's rule is: **image quality is verified only with numeric metrics**.

| Tool | Measures |
|---|---|
| `tools/study_metrics.py` | subject coverage, overflow, colour error, curvature ratio |
| `tools/compare_actual.py` | aligns the plan simulation with a game screenshot by the lines' bounding box and reports hollow (paint in the plan but not in the game), extra, line ink ratio and line width |
| `tools/human_eval.py` | pacing, pressure widths, bucket predictions |
| `tools/frame_check.py` | how often painting stops at the same pixel along a frame cut |
| `tools/perceptual.py` | LPIPS / CLIP similarity (development only; needs `pip install lpips open_clip_torch`) |
| `tools/probe_layers.py` | crosses adjacent virtual layers pairwise, draws the high layer first and the low one second, measures the crossings in a screenshot, prints `LAYERS OK` |
| `tools/probe_pen.py` | whether the virtual pen is accepted by the game; measures the game frame rate |
| `tools/probe_fill_colors.py` | whether fills get recoloured by the next stroke |

**Representative results** (same image, 180 s budget, plan simulation; lower LPIPS = closer to the original):

| | `anime_study` | `anime_human` v2 |
|---|---|---|
| operations | 303 | about 240–280 |
| subject coverage | 0.947 | 0.90 |
| colour error (painted part) | 33.2 | 33.9 |
| LPIPS | 0.397 | 0.45 |

`anime_human` is slightly lower numerically and buys **a process and stroke look that is closer to a real person**. Layered brush colouring vs. the bucket (LPIPS, 120 s / 180 s): marisa 0.488 → 0.467 / 0.437 → 0.437; in1 0.424 → 0.396 / 0.394 → 0.380; in2 0.273 → 0.247 / 0.226 → 0.212; sanae 0.109 → 0.105 / 0.142 → 0.109; coverage clearly higher (sanae 0.69 → 0.98). In-game: about 200 strokes all through the native pen with no failures, 31 pours with 0 leaks and 0 repairs, total pressure-width error 9.

**The offline test suite** (204 tests, no game needed): the pressure-pipeline port, the pacing model, the bucket model (closed / leaking / discs do not block / patching), planning (line before bucket, every pour closed, time budget, determinism, JSON round trips), execution (pressure sizes, leak undo and repair, native pen and fallback), Studio pure functions and the UI dictionary; it runs continuously on Ubuntu / macOS / Windows × Python 3.10 / 3.13 (`.github/workflows/tests.yml`).

---

## 9. Security, privacy and fair play

- **Local only:** the bridge (8765) and the draw service (8770) bind only to `127.0.0.1`. Once a `Token` is set, API requests must carry it. **Do not expose the ports to a network.**
- **Images stay on the machine:** images are processed locally and never uploaded to any server. The only things downloaded at install time are BepInEx and (optionally) two model files, **all SHA-256 verified**.
- **No game files are distributed:** the plugin is compiled on the user's machine against the local game assemblies.
- **Process isolation:** the Python service is a separate child process; a crash does not take the game down; the plugin starts and stops it according to its config and offers `/shutdown`.
- **Fair play:** Sumie is meant for solo Sketchbook play, private lobbies whose players agree to it, experiments and learning. **Do not use it to gain an unfair edge in public games or to disrupt other players;** following the game's terms of service is the user's own responsibility. Making the result look hand-drawn is for aesthetics, **not for disguising identity or evading detection**.

---

## 10. Rebuild it from scratch (eight steps)

This guide lets you build a minimal equivalent system **without reading the source**. Every step can be verified on its own.

### Step 1: Prepare the environment

- Game: Draw & Guess on Steam, **Mono build**.
- Tools: Python ≥ 3.10, .NET SDK ≥ 6, git.
- Find the game folder (macOS example: `~/Library/Application Support/Steam/steamapps/common/Draw & Guess`, Windows example: `C:\Program Files (x86)\Steam\steamapps\common\Draw & Guess`).
- Install BepInEx 6 (the `Unity.Mono` build; the repository pins build 790) and verify its SHA-256. On macOS use `run_bepinex.sh` as the Steam launch option; on Windows the `winhttp.dll` proxy loads it automatically. **You can run `tools/install.py --check` to see what it finds.**

### Step 2: Write a minimal plugin (able to receive HTTP requests)

1. Create a `netstandard2.1` class library referencing `BepInEx.Core`, `BepInEx.Unity.Mono`, `0Harmony`, the game's `UnityEngine*.dll` and `Assembly-CSharp.dll` (`Private=false`).
2. Write a class deriving from `BaseUnityPlugin` (see 2.1).
3. In `Awake()` listen on `http://127.0.0.1:8765/` with `System.Net.HttpListener`, reading requests on a **background thread**.
4. Implement the `MainThreadDispatcher` of 2.2 and call `Pump()` in `Update()`.
5. Implement one read-only endpoint first, `GET /api/v1/state`: put "can draw" and similar into JSON, **waiting with `Invoke` until the main thread has computed it before replying**.
6. Build, copy the DLL into `BepInEx/plugins/`, start the game and open `http://127.0.0.1:8765/api/v1/state` in a browser. Seeing JSON means success.

> Tip: for the game's internal class names (such as `DrawHelpers`, `LineInformation`, `DrawModule`) decompile `Assembly-CSharp.dll` with dnSpy / ILSpy; the repository's `GameApi.cs` is an implementation you can compare with.

### Step 3: Make the plugin draw one line

In `POST /api/v1/strokes`, for every stroke follow the 8 steps of 2.5 to build `LineInformation` and call `SpawnDrawElement`. **Use a coroutine that handles at most 24 per frame.** Implement only `line` and `disc` first.

Verify:

```bash
curl -s -X POST http://127.0.0.1:8765/api/v1/strokes \
  -H 'Content-Type: application/json' \
  -d '{"strokes":[{"type":"line","color":"#FF3355","size":10,"points":[[0.1,0.2],[0.25,0.8],[0.5,0.3]]}]}'
```

A red polyline should appear on the canvas. **Read it back with `GET /api/v1/lines` and confirm y is up (the top of the head has a large y).**

### Step 4: Turn a picture into strokes in Python

The repository includes a minimal demo, `examples/minimal_strokes.py` (needs only OpenCV):

```python
img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
img = cv2.resize(img, ...)                              # longest side 480
edges = cv2.Canny(cv2.GaussianBlur(img, (3, 3), 0), 60, 140)
contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
...
for c in contours:
    c = cv2.approxPolyDP(c, 1.2, False)[:, 0, :]        # simplify the polyline
    pts = [[x0 + px / w * fit_w, y0 + (1 - py / h) * fit_h] for px, py in c]   # the 2.3 conversion
    strokes.append({"type": "line", "color": "#202020", "size": 4, "points": pts})
```

Run:

```bash
python examples/minimal_strokes.py examples/sample.png > strokes.json
python client/dg_bridge.py draw-json strokes.json
```

You now have a version that "draws the edges". Every following step improves **how human it looks and how much it resembles the original**.

### Step 5: Better line art and a palette

- Replace Canny with Anime2Sketch (3.3). The weights `netG.pth` can be downloaded and verified with `python tools/setup.py --ai`.
- Add skeletonisation + joining + cutting (3.3, steps 2–4) to get few, long strokes.
- k-means in Lab for 3–8 colours, build a **simple polygon** for every region and send it as `fill` (3.4); **never send self-intersecting polygons.**

### Step 6: Add pressure and rhythm

1. Copy `game_brush_size` from 5.1.2 (a pure function, easy to unit-test).
2. Pick a target `size` per stroke, invert the pressure `p ≈ (size − 2) / 33`, and produce a per-frame pressure sequence (every frame > 0.05, no movement for the first 70 ms).
3. Use the power law of 5.2 for stroke durations and pauses; spread time along the path with the 20-segment speed profile.
4. Start with `direct` mode (one `POST /strokes` per stroke, `sleep` between strokes).
5. Then implement `POST /stroke/pen` in the plugin: create a virtual `Pen`, `InputSystem.QueueStateEvent` per sample frame, and at the end drop the pressure to zero and wait a few frames. **The game setting for pressure must be on, otherwise return 409.**

### Step 7: Add the time budget

As in 6.1: record `planning_start` **before** loading the image, compute `deadline`; check it before every stroke in the execution loop and adjust the pace with the ratio of 6.2; if time is short, drop strokes from the end of the list.

### Step 8: Add layers and the leak model (advanced)

- Implement virtual layers in the plugin (5.4.2), expose `GET /api/v1/layers` and let `POST /strokes` accept `vlayer`.
- Implement `brush_fill` in Python (5.4.4): the allowed area, back-and-forth sweeps, medial axis, coverage target.
- When you need the bucket, implement the offline bucket model (5.5) and compare against the real area when executing.

### Pitfalls you will most likely meet

| Pitfall | Symptom | Fix |
|---|---|---|
| Touching Unity objects off the main thread | random crashes / errors | always go through the dispatcher |
| Treating y as down | picture upside down | y-up everywhere, flip only when drawing onto an image |
| Frames with pressure ≤ 0.05 | a stroke split in two / truncated | every frame > 0.05, hold 70 ms at the start |
| Sending self-intersecting polygons | big dark blobs | drop them while planning |
| Preview and real line-width formulas differ | preview thicker than real | use the formula of 2.4 everywhere |
| Colour changed by the next stroke after a pour | every fill ends up one colour | disable "selectable" in the plugin; switch back to the brush after a pour |
| Seeding a pour at the region centre | nothing gets filled | use the deepest cell and spare seeds |
| Planning time not counted in the deadline | still unfinished when the round ends | start the deadline before image loading |

---

## 11. Parameter and interface cheat sheet

**`client/dg_artist.py` (command line)**

| Option | Default | Notes |
|---|---|---|
| `--style` | `anime_flat` | `sketch` / `anime_flat` / `anime_study` / `anime_human` / `impasto` / `watercolor` (Studio defaults to `anime_study`; this paper recommends `anime_human`) |
| `--time` | read the round timer | seconds left; `--default-time` when there is no timer |
| `--region x,y,w,h` | the whole canvas | normalised region (y-up) |
| `--margin` | 0.04 | margin around the image |
| `--clear` / `--no-clear` | clear | whether to clear before drawing |
| `--dry-run` | no | plan only, do not draw |
| `--plan-out` / `--plan-in` | — | write / reuse a plan |
| `--preview PNG` | — | render a preview |
| `--pen auto|direct|native` | auto | how strokes are put down |
| `--colour auto|brush|bucket` | auto | colouring method |
| `--pressure` | 1.0 | pressure multiplier |
| `--skill` | 2.3 | larger = faster and smoother |
| `--game-fps` | 240 | game frame rate |
| `--borders on|off` | on | draw colour-area borders after brush colouring |
| `--soften-frame auto|on|off` | on for `anime_human` | soften frame cuts |
| `--pace slow|instant` | slow | slowly / all at once |
| `--seed` | 7 | random seed (decides the "hand's" habits) |

The full list is in [`usage.md`](usage.md); the API in [`api-reference.md`](api-reference.md) (Chinese).

---

## 12. FAQ

**Q: Can this be detected?**
A: Sumie does not hide itself and was not designed to defeat detection. The hand-drawn look is only for aesthetics and a natural process. Please follow the game's terms of service and use it only where appropriate (Chapter 9).

**Q: Why does my result not look much like the original?**
A: The shorter the time, the fewer and simpler the strokes. Try 180–300 seconds, or install the AI models (`--ai`). Check coverage and colour error in the preview first.

**Q: Why must it be the Mono build?**
A: BepInEx 6's Unity.Mono loader only supports games on the Mono runtime. IL2CPP builds need a different (Il2Cpp) loader that this project does not support, and the installer refuses them.

**Q: What if the plugin breaks after a game update?**
A: Run the installer again; if the game's internal classes were renamed or changed signature, `GameApi.cs` needs changes.

**Q: Why is the Python service a separate process instead of living in the plugin?**
A: The game can only run .NET code while OpenCV / PyTorch live in Python; also, planning is CPU-heavy and in its own process it cannot freeze the game's picture, and a crash cannot take the game down.

**Q: Can I add a style of my own?**
A: Yes. Register a new style function in `dg_artist.py` that produces an Op list; the executor, preview, metrics and page are all reused.

---

## 13. Known limits and roadmap

**Limits**

- Colours are region medians, so soft gradients become flat colour; shading adds only a little from outlined dark parts;
- very thin patch lines (pressure near the 0.05 floor) are hard to see in the game but still block the bucket;
- a major game update may change internal classes and require plugin changes;
- how virtual layers look on other players' clients is best confirmed in a private room with a second computer;
- only the Mono build of the game is supported;
- the UI is localised, but command-line output and the other technical documents in `docs/` are mostly Chinese.

**Roadmap**

- more styles and a finer shading model;
- more real-machine verification on Windows and Linux;
- English translations of the remaining `docs/` documents;
- WebSocket progress push instead of polling.

---

## 14. Glossary

| Term | Plain explanation |
|---|---|
| **Plugin** | a small program installed into another program to add features |
| **BepInEx** | an open-source tool that loads plugins into Unity games |
| **Mono / IL2CPP** | two ways a Unity game can run; Mono can be loaded directly by BepInEx 6 |
| **Main thread** | the only "clerk" in the game allowed to operate game objects |
| **HTTP / WebSocket** | two ways programs talk: question-and-answer / a line that stays open |
| **127.0.0.1** | the "this machine" address; only your own computer can reach it |
| **API** | an agreed "order form" between programs |
| **Bitmap / vector** | a picture made of pixels / a picture described by lines and coordinates |
| **Line art** | a black-and-white picture with outlines only |
| **Skeletonisation** | thinning thick lines down to one centre line |
| **k-means** | an algorithm that sorts similar data into k groups; used here to find the main colours |
| **Lab colour space** | a colour representation whose numeric distance is close to perceived colour difference |
| **GrabCut** | a classic foreground-extraction algorithm in OpenCV |
| **ONNX** | a cross-framework file format for neural networks |
| **U-Net / ISNet** | two network architectures commonly used for image-to-image tasks |
| **Pen pressure** | how hard a tablet senses you press; harder means a thicker line |
| **Layer** | a transparent sheet in a stack; a higher one covers a lower one |
| **SortingOrder** | the Unity value that decides what covers what |
| **Paint bucket** | the tool that fills a closed region with one click |
| **Normalised coordinates** | coordinates scaled so width and height both run 0–1 |
| **Power law** | a relation of the form `y = a·x^b` |
| **Log-normal** | a random quantity whose logarithm is normally (bell) distributed; fits "always positive, occasionally large" quantities |
| **LPIPS** | a neural-network-based number for how alike two pictures look; smaller is more alike |
| **CORS** | the browser's safety rule for requests across sites / ports |
| **Determinism** | the same input always gives the same output |
| **Harmony** | a library that "patches" .NET methods at run time |

---

## 15. Third-party components and credits

BepInEx 6 / HarmonyX (LGPL-2.1 / MIT, downloaded at install time, not stored in the repository), Anime2Sketch (MIT), anime-segmentation (Apache-2.0), the Google *Quick, Draw!* dataset (CC BY 4.0, only summary statistics are stored), numpy / OpenCV / Pillow / PyTorch / ONNX Runtime. See [`NOTICE.md`](../NOTICE.md). "Draw & Guess" belongs to its respective owners; this project is an unofficial fan tool and is not affiliated with the game's developers.

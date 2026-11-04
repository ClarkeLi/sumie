using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Reflection;
using Newtonsoft.Json.Linq;
using Shapes;
using UnityEngine;
using UnityEngine.UI;

namespace DrawGuessBridge
{
    internal static partial class GameApi
    {
        private const BindingFlags InstanceNonPublic = BindingFlags.Instance | BindingFlags.NonPublic;
        private const BindingFlags InstanceAny = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;

        private static readonly FieldInfo CreatedDrawablesField =
            typeof(DrawModule).GetField("CreatedDrawablesByMe", InstanceNonPublic);

        private static readonly FieldInfo SortingLayerField =
            typeof(DrawModule).GetField("SortingLayerForSpawnLines", InstanceNonPublic);

        private static readonly PropertyInfo LocalPlayfabIdProperty =
            typeof(DrawModule).GetProperty("LocalPlayfabId", InstanceNonPublic);

        private static readonly MethodInfo BaseFinishDrawingMethod =
            typeof(DrawModule).GetMethod("OnFinishDrawingCurrent", BindingFlags.Instance | BindingFlags.Public);

        private static readonly MethodInfo CreateNewFillMethod =
            typeof(DrawModule).GetMethod("CreateNewFill", BindingFlags.Instance | BindingFlags.NonPublic);

        private static readonly object WordLock = new object();
        private static readonly List<JObject> WordHistory = new List<JObject>();
        private static string _lastSeenWord = "";
        private static bool _roundTextLookup;
        private static FieldInfo _roundTextField;

        internal static DrawModule CurrentModule => DrawModule.DrawModuleInstance;

        internal static JObject GetState()
        {
            var module = CurrentModule;
            var state = new JObject
            {
                ["available"] = module != null
            };

            if (module == null)
            {
                state["hint"] = "No drawing module is active. Open the sketchbook or join a draw round.";
                state["brushSize"] = SafeGet(() => DrawModule.BrushSize.Value, 10);
                return state;
            }

            state["module"] = module.GetType().Name;
            state["canDraw"] = SafeGet(module.CanDraw, false);
            state["submitted"] = SafeGet(() => module.HasSubmitted, false);
            state["activeObject"] = module.gameObject != null && module.gameObject.activeInHierarchy;

            var ingame = module as IngameDrawModule;
            if (ingame != null)
                state["isActive"] = SafeGet(() => ingame.IsActive, false);

            var word = "";
            if (ingame != null)
                word = (SafeGet(() => ingame.LastWord, string.Empty) ?? "").Trim();
            state["roundText"] = ReadRoundText();
            state["word"] = word;
            lock (WordLock)
            {
                if (!string.IsNullOrEmpty(word) && word != _lastSeenWord)
                {
                    WordHistory.Insert(0, new JObject
                    {
                        ["word"] = word,
                        ["at"] = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
                    });
                    if (WordHistory.Count > 20)
                        WordHistory.RemoveAt(WordHistory.Count - 1);
                    _lastSeenWord = word;
                }
                state["previousWord"] = WordHistory.Count > 1
                    ? WordHistory[1]["word"]?.ToString() ?? ""
                    : "";
                state["wordHistory"] = new JArray(WordHistory.Select(item => item.DeepClone()));
            }

            var stateStruct = SafeGet(module.GetState, default(DrawModule.DrawState));
            state["tool"] = stateStruct.Tool.ToString();
            state["color"] = ToHexString(stateStruct.RGBColor);
            state["brushSize"] = SafeGet(() => DrawModule.BrushSize.Value, 10);

            state["specialMode"] = SafeGet(() => module.SpecialMode.ToString(), "Unknown");
            state["colorMode"] = SafeGet(() => module.ColorMode.ToString(), "Unknown");
            state["pixelArt"] = SafeGet(() => module.IsPixelArt, false);
            state["owner"] = SafeGet(GetLocalPlayfabId, string.Empty);

            var surface = SafeGet(() => module.DrawSurface, null);
            var canvas = new JObject
            {
                ["initialized"] = surface != null && surface.IsInitialised
            };
            if (surface != null)
            {
                canvas["width"] = Round(surface.Width);
                canvas["height"] = Round(surface.Heigth);
                canvas["aspect"] = Round(surface.AspectRatio);
                canvas["minX"] = Round(surface.MinX);
                canvas["maxX"] = Round(surface.MaxX);
                canvas["minY"] = Round(surface.MinY);
                canvas["maxY"] = Round(surface.MaxY);
            }
            state["canvas"] = canvas;

            var layerModule = module as LayerDrawModule;
            if (layerModule != null)
            {
                state["layers"] = new JObject
                {
                    ["selected"] = layerModule.SelectedLayer,
                    ["total"] = SafeGet(() => layerModule.TotalLayers, 0)
                };
            }

            state["virtualLayers"] = VirtualLayers.Describe(module);

            if (SafeGet(() => module.IsPixelArt, false))
            {
                var pixelModule = SafeGet(() => module.PixelDrawModule, null);
                var host = SafeGet(() => pixelModule?.PixelHost, null);
                if (host != null)
                {
                    var dims = SafeGet(host.GetCanvasPixelWidthAndHeight, Vector2Int.zero);
                    state["pixelCanvas"] = new JObject { ["width"] = dims.x, ["height"] = dims.y };
                }
            }

            state["lines"] = SafeGet(() => module.GetMyDrawnElements().Count, 0);
            state["hasUndo"] = SafeGet(() => module.UndoSystem.CanUndo, false);
            state["hasRedo"] = SafeGet(() => module.UndoSystem.CanRedo, false);
            return state;
        }

        internal static JObject GetGameSnapshot()
        {
            var state = GetState();
            state["round"] = GetRound();
            return state;
        }

        private static string ReadRoundText()
        {
            try
            {
                var display = RoundInfoDisplay.Instance;
                if (display == null)
                    return "";
                if (!_roundTextLookup)
                {
                    _roundTextLookup = true;
                    _roundTextField = typeof(RoundInfoDisplay).GetField("RoundText", InstanceNonPublic);
                }
                var text = _roundTextField?.GetValue(display);
                if (text == null)
                    return "";
                return text.GetType().GetProperty("text", InstanceAny)?.GetValue(text, null) as string
                       ?? "";
            }
            catch
            {
                return "";
            }
        }

        internal static JObject GetLines(int limit)
        {
            var module = RequireModule();
            var lines = module.GetDrawingAsLineInfo(finishActiveLines: false, normalizeLinesForExport: true)
                          ?? new List<LineInformation>();

            var array = new JArray();
            foreach (var line in lines.Take(Math.Max(0, limit)))
            {
                if (line?.Points == null)
                    continue;

                var points = new JArray();
                foreach (var point in line.Points)
                    points.Add(new JArray(Round(point.x), Round(point.y)));

                array.Add(new JObject
                {
                    ["type"] = (byte)line.DrawElementType,
                    ["color"] = "#" + line.Color.ToString("X6"),
                    ["size"] = (int)line.BrushSize,
                    ["ident"] = line.Ident,
                    ["owner"] = line.Owner ?? string.Empty,
                    ["layer"] = (int)line.Layer,
                    ["sortingOrder"] = (int)line.SortingOrder,
                    ["normalized"] = line.IsNormalized,
                    ["aspectRatio"] = Round(line.AspectRatio),
                    ["points"] = points
                });
            }

            return new JObject
            {
                ["module"] = module.GetType().Name,
                ["count"] = array.Count,
                ["lines"] = array
            };
        }

        internal static JObject SetColor(string colorText)
        {
            var module = RequireModule();
            var color = ParseColor(colorText, GetActiveColor(module));
            module.SetRGBColor(color, updatePicker: true, forceChange: true, isTemporary: false, withSound: false);
            return new JObject
            {
                ["color"] = ToHexString(color),
                ["brushSize"] = SafeGet(() => DrawModule.BrushSize.Value, 10)
            };
        }

        internal static JObject SetBrush(int size)
        {
            RequireModule();
            var clamped = Mathf.Clamp(size, 1, 255);
            DrawModule.BrushSize.Value = clamped;
            return new JObject { ["brushSize"] = DrawModule.BrushSize.Value };
        }

        internal static JObject ActivateTool(string toolName)
        {
            var module = RequireModule();
            if (!Enum.TryParse<DrawModule.DrawTool>(toolName, ignoreCase: true, out var tool))
                throw new BridgeApiException($"Unknown tool '{toolName}'. Valid values: {string.Join(", ", Enum.GetNames(typeof(DrawModule.DrawTool)))}", 400);
            module.ActivateTool(tool);
            return new JObject { ["tool"] = module.ActiveDrawTool.ToString() };
        }

        internal static JObject Clear(bool force, bool allLayers = true)
        {
            var module = RequireModule();
            if (!force && !SafeGet(module.CanDraw, false))
                throw new BridgeApiException("The game does not allow drawing right now. Use force=true to try anyway.", 409);

            var trigger = force ? DrawingToolTrigger.System : DrawingToolTrigger.Button;
            VirtualLayers.Reset();
            if (allLayers && module is LayerDrawModule layerModule)
            {
                var original = layerModule.SelectedLayer;
                var total = Mathf.Max(1, layerModule.TotalLayers);
                for (var i = 0; i < total; i++)
                {
                    layerModule.SetLayer(i);
                    module.ExecuteClear(trigger, fromLocal: true, sync: true);
                }
                layerModule.SetLayer(original);
            }
            else
            {
                module.ExecuteClear(trigger, fromLocal: true, sync: true);
            }

            return new JObject { ["cleared"] = true, ["force"] = force, ["allLayers"] = allLayers };
        }

        internal static JObject Undo()
        {
            var module = RequireModule();
            module.Undo(DrawingToolTrigger.Button);
            return new JObject { ["canUndo"] = SafeGet(() => module.UndoSystem.CanUndo, false) };
        }

        internal static JObject Redo()
        {
            var module = RequireModule();
            module.Redo(DrawingToolTrigger.Button);
            return new JObject { ["canRedo"] = SafeGet(() => module.UndoSystem.CanRedo, false) };
        }

        internal static JArray GetUiButtons()
        {
            var result = new JArray();
            var buttons = UnityEngine.Object.FindObjectsByType<Button>(FindObjectsInactive.Include, FindObjectsSortMode.None);
            foreach (var button in buttons)
            {
                if (button == null)
                    continue;
                var rect = button.GetComponent<RectTransform>();
                var canvas = button.GetComponentInParent<Canvas>();
                var camera = canvas != null && canvas.renderMode != RenderMode.ScreenSpaceOverlay
                    ? canvas.worldCamera
                    : null;
                var corners = new Vector3[4];
                rect.GetWorldCorners(corners);
                var min = RectTransformUtility.WorldToScreenPoint(camera, corners[0]);
                var max = RectTransformUtility.WorldToScreenPoint(camera, corners[2]);

                result.Add(new JObject
                {
                    ["name"] = button.name,
                    ["text"] = GetButtonText(button),
                    ["active"] = button.gameObject.activeInHierarchy,
                    ["interactable"] = button.interactable,
                    ["x"] = Math.Round(min.x, 1),
                    ["y"] = Math.Round(min.y, 1),
                    ["width"] = Math.Round(max.x - min.x, 1),
                    ["height"] = Math.Round(max.y - min.y, 1)
                });
            }
            return result;
        }

        internal static JObject ClickUiButton(string text, string name, int index, bool includeInactive)
        {
            var buttons = UnityEngine.Object.FindObjectsByType<Button>(includeInactive ? FindObjectsInactive.Include : FindObjectsInactive.Exclude, FindObjectsSortMode.None);
            var matches = new List<Button>();
            foreach (var button in buttons)
            {
                if (button == null || !button.interactable)
                    continue;
                if (!includeInactive && !button.gameObject.activeInHierarchy)
                    continue;

                var label = GetButtonText(button);
                var matchesText = string.IsNullOrEmpty(text) ||
                                  (!string.IsNullOrEmpty(label) && label.IndexOf(text, StringComparison.OrdinalIgnoreCase) >= 0);
                var matchesName = string.IsNullOrEmpty(name) ||
                                  (!string.IsNullOrEmpty(button.name) && button.name.IndexOf(name, StringComparison.OrdinalIgnoreCase) >= 0);
                if (matchesText && matchesName)
                    matches.Add(button);
            }

            if (matches.Count == 0)
                throw new BridgeApiException("No matching UI button was found.", 404);
            if (index < 0 || index >= matches.Count)
                throw new BridgeApiException($"Button index {index} is out of range (found {matches.Count}).", 400);

            var target = matches[index];
            var targetText = GetButtonText(target);
            target.onClick.Invoke();
            return new JObject
            {
                ["clicked"] = target.name,
                ["text"] = targetText,
                ["matches"] = matches.Count
            };
        }

        private static string GetButtonText(Button button)
        {
            try
            {
                var uiText = button.GetComponentInChildren<Text>(includeInactive: true);
                if (uiText != null && !string.IsNullOrEmpty(uiText.text))
                    return uiText.text;
            }
            catch { }

            try
            {
                foreach (var component in button.GetComponentsInChildren<Component>(includeInactive: true))
                {
                    if (component == null)
                        continue;
                    var type = component.GetType();
                    if (type.FullName == "TMPro.TMP_Text" || type.FullName == "TMPro.TextMeshProUGUI")
                    {
                        var textProperty = type.GetProperty("text");
                        var value = textProperty?.GetValue(component, null) as string;
                        if (!string.IsNullOrEmpty(value))
                            return value;
                    }
                }
            }
            catch { }

            return string.Empty;
        }

        internal static JObject GetRound()
        {
            var module = CurrentModule;
            var result = new JObject
            {
                ["module"] = module != null ? module.GetType().Name : null,
                ["canDraw"] = module != null && SafeGet(module.CanDraw, false),
                ["timers"] = new JArray()
            };

            var timers = UnityEngine.Object.FindObjectsByType<Timer>(FindObjectsInactive.Include, FindObjectsSortMode.None);
            var active = new List<Tuple<Timer, JObject, bool>>();

            foreach (var timer in timers)
            {
                if (timer == null)
                    continue;

                var isActive = SafeGet(() => timer.IsActive, false);
                var total = SafeGet(() => timer.TotalRuntime, 0);
                var remaining = SafeGet(() => timer.TimeRemainingInSecF, 0f);
                var passed = SafeGet(() => timer.PassedSecondsF, 0f);
                var isModuleTimer = module != null && IsTimerReferencedBy(module, timer);

                var item = new JObject
                {
                    ["active"] = isActive,
                    ["name"] = timer.name,
                    ["path"] = GetTransformPath(timer.transform),
                    ["total"] = Round(total),
                    ["remaining"] = Round(remaining),
                    ["passed"] = Round(passed),
                    // the longest this timer ran this round (what a plan should be made for)
                    ["peak"] = Round(Math.Max(total, remaining + passed)),
                    ["moduleTimer"] = isModuleTimer
                };
                ((JArray)result["timers"]).Add(item);

                if (isActive && remaining > 0f)
                    active.Add(Tuple.Create(timer, item, isModuleTimer));
            }

            JObject recommended = null;
            var preferred = active.FirstOrDefault(x => x.Item3) ?? active.OrderByDescending(x => x.Item1.TotalRuntime).FirstOrDefault();
            if (preferred != null)
            {
                recommended = (JObject)preferred.Item2.DeepClone();
                recommended["source"] = preferred.Item3 ? "module-field" : "active-scan";
            }
            result["recommended"] = recommended;
            return result;
        }

        internal static JObject VirtualLayerInfo()
        {
            return VirtualLayers.Describe(CurrentModule);
        }

        internal static JObject SetLayer(int index)
        {
            var module = RequireModule();
            if (!(module is LayerDrawModule layerModule))
                throw new BridgeApiException("The active drawing module does not support layers.", 409);
            var clamped = Mathf.Clamp(index, 0, layerModule.TotalLayers - 1);
            layerModule.SetLayer(clamped);
            return new JObject
            {
                ["selected"] = layerModule.SelectedLayer,
                ["total"] = layerModule.TotalLayers
            };
        }

        internal static JObject RealFill(JArray points, string colorText, string space, bool force)
        {
            var module = RequireModule();
            EnsureDrawingPossible(module, force);
            if (CreateNewFillMethod == null)
                throw new BridgeApiException("This game version does not expose the internal fill method.", 500);

            var surface = module.DrawSurface;
            if (surface == null || !surface.IsInitialised || surface.Width <= 0.0001f || surface.Heigth <= 0.0001f)
                throw new BridgeApiException("The drawing surface is not ready for filling.", 409);

            if (!string.IsNullOrWhiteSpace(colorText))
            {
                var color = ParseColor(colorText, GetActiveColor(module));
                module.SetRGBColor(color, updatePicker: false, forceChange: true, isTemporary: false, withSound: false);
            }

            var before = SafeGet(() => module.GetMyDrawnElements().Count, 0);
            var filled = 0;
            var failed = 0;
            var errors = new JArray();
            var regions = new JArray();

            foreach (var token in points)
            {
                float x;
                float y;
                if (token is JArray arr && arr.Count >= 2)
                {
                    x = arr[0].Value<float>();
                    y = arr[1].Value<float>();
                }
                else if (token is JObject obj)
                {
                    x = obj["x"]?.Value<float>() ?? 0f;
                    y = obj["y"]?.Value<float>() ?? 0f;
                }
                else
                {
                    failed++;
                    continue;
                }

                var candidates = new List<Vector2>();
                if (string.Equals(space, "canvas", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(space, "world", StringComparison.OrdinalIgnoreCase))
                {
                    candidates.Add(new Vector2(x, y));
                }
                else
                {
                    candidates.Add(new Vector2(
                        surface.MinX + Mathf.Clamp01(x) * surface.Width,
                        surface.MinY + Mathf.Clamp01(y) * surface.Heigth));

                    try
                    {
                        var area = DrawHelpers.GetARcorrectedArea(surface.AspectRatio, module.LineParent);
                        candidates.Add(new Vector2(
                            Mathf.Lerp(area.xMinMax.x, area.xMinMax.y, Mathf.Clamp01(x)),
                            Mathf.Lerp(area.yMinMax.x, area.yMinMax.y, Mathf.Clamp01(y))));
                    }
                    catch { }
                }

                var filledThisSeed = false;
                foreach (var world in candidates)
                {
                    if (world.x < surface.MinX || world.x > surface.MaxX || world.y < surface.MinY || world.y > surface.MaxY)
                        continue;

                    try
                    {
                        CreateNewFillMethod.Invoke(module, new object[] { world });
                        var after = SafeGet(() => module.GetMyDrawnElements().Count, 0);
                        if (after > before)
                        {
                            filledThisSeed = true;
                            filled++;
                            before = after;
                            regions.Add(DescribeNewFill(module, surface));
                            break;
                        }
                    }
                    catch (Exception ex)
                    {
                        var actual = ex is TargetInvocationException tie ? (tie.InnerException ?? ex) : ex;
                        BridgePlugin.Instance?.Log.LogError($"Real fill failed at [{x:F3},{y:F3}] world={world}: {actual}");
                        if (errors.Count < 12)
                            errors.Add($"fill at [{x:F3},{y:F3}]: {actual.GetType().Name}: {actual.Message}");
                    }
                }

                if (!filledThisSeed)
                {
                    failed++;
                    if (errors.Count < 12)
                        errors.Add($"fill at [{x:F3},{y:F3}] did not create a region (is the area closed?)");
                }
            }

            return new JObject
            {
                ["filled"] = filled,
                ["failed"] = failed,
                ["requested"] = points.Count,
                ["errors"] = errors,
                ["regions"] = regions
            };
        }

        // Size of the fill the game just made: lets the caller notice a leak (a fill
        // that flooded far more than the outline it was meant to stay in) and undo it.
        private static JObject DescribeNewFill(DrawModule module, DrawableSurface surface)
        {
            var info = new JObject();
            try
            {
                var elements = module.GetMyDrawnElements();
                if (elements.Count > 0 && elements[elements.Count - 1] is DrawableShape shape &&
                    shape.OutlinePolyLine != null)
                {
                    // After a fill the game leaves it "selectable" (hover preview, and the next
                    // pen press recolours it with the active colour). That belongs to the Fill
                    // tool, which the game switches off again whenever another tool is picked;
                    // a bridge that never switches tools would let every later stroke or colour
                    // change repaint the fills. Switch it off the way ActivateTool does.
                    try { shape.LineFillable?.SetLineSelectActive(false); } catch { /* keep the fill */ }
                    var outline = shape.OutlinePolyLine.points;
                    double twice = 0;
                    for (var i = 0; i < outline.Count; i++)
                    {
                        var a = outline[i].point;
                        var b = outline[(i + 1) % outline.Count].point;
                        twice += (double)a.x * b.y - (double)b.x * a.y;
                    }
                    var canvas = Math.Max(1e-6, (double)surface.Width * surface.Heigth);
                    info["area"] = Math.Abs(twice) / 2.0 / canvas;
                    info["points"] = outline.Count;
                }
            }
            catch (Exception ex)
            {
                info["error"] = ex.Message;
            }
            return info;
        }

        private static bool IsTimerReferencedBy(DrawModule module, Timer timer)
        {
            var type = module.GetType();
            while (type != null && type != typeof(MonoBehaviour) && type != typeof(object))
            {
                foreach (var field in type.GetFields(InstanceNonPublic))
                {
                    if (field.FieldType != typeof(Timer))
                        continue;
                    try
                    {
                        if (ReferenceEquals(field.GetValue(module), timer))
                            return true;
                    }
                    catch { }
                }
                type = type.BaseType;
            }
            return false;
        }

        private static string GetTransformPath(Transform transform)
        {
            var parts = new List<string>();
            while (transform != null)
            {
                parts.Add(transform.name);
                transform = transform.parent;
            }
            parts.Reverse();
            return string.Join("/", parts);
        }

        internal static JObject GetCoords(float x, float y)
        {
            var module = RequireModule();
            var surface = module.DrawSurface;
            if (surface == null)
                throw new BridgeApiException("No draw surface.", 409);

            var result = new JObject
            {
                ["surface"] = new JObject
                {
                    ["minX"] = Round(surface.MinX), ["maxX"] = Round(surface.MaxX),
                    ["minY"] = Round(surface.MinY), ["maxY"] = Round(surface.MaxY),
                    ["width"] = Round(surface.Width), ["height"] = Round(surface.Heigth),
                    ["aspect"] = Round(surface.AspectRatio)
                },
                ["bySurface"] = new JObject
                {
                    ["x"] = Round(surface.MinX + x * surface.Width),
                    ["y"] = Round(surface.MinY + y * surface.Heigth)
                }
            };

            try
            {
                var area = DrawHelpers.GetARcorrectedArea(surface.AspectRatio, module.LineParent);
                result["byAspectArea"] = new JObject
                {
                    ["xMin"] = Round(area.xMinMax.x), ["xMax"] = Round(area.xMinMax.y),
                    ["yMin"] = Round(area.yMinMax.x), ["yMax"] = Round(area.yMinMax.y),
                    ["x"] = Round(Mathf.Lerp(area.xMinMax.x, area.xMinMax.y, x)),
                    ["y"] = Round(Mathf.Lerp(area.yMinMax.x, area.yMinMax.y, y))
                };
            }
            catch (Exception ex)
            {
                result["byAspectAreaError"] = ex.Message;
            }

            var raw = module.GetDrawingAsLineInfo(false, false);
            var lines = new JArray();
            foreach (var line in raw)
            {
                var pts = new JArray();
                foreach (var p in line.Points)
                    pts.Add(new JArray(Round(p.x), Round(p.y)));
                lines.Add(pts);
            }
            result["rawLines"] = lines;
            return result;
        }

        internal static JObject Submit()
        {
            var module = RequireModule();
            if (!(module is IngameDrawModule ingame))
                throw new BridgeApiException("The active drawing module cannot submit a drawing.", 409);
            if (!ingame.IsActive)
                throw new BridgeApiException("Drawing is not currently active.", 409);
            ingame.SubmitDrawing();
            return new JObject { ["submitted"] = true };
        }

        internal static void StartApplyStrokes(
            List<StrokeSpec> strokes,
            bool clear,
            bool force,
            int strokesPerFrame,
            Action<JObject> completed)
        {
            var module = RequireModule();
            EnsureDrawingPossible(module, force);

            if (clear)
            {
                module.ExecuteClear(force ? DrawingToolTrigger.System : DrawingToolTrigger.Button, fromLocal: true, sync: true);
            }

            BridgePlugin.Instance.StartCoroutine(ApplyStrokesRoutine(module, strokes, force, strokesPerFrame, completed));
        }

        internal static void StartApplyImage(
            byte[] imageBytes,
            ImageOptions options,
            Action<JObject> completed)
        {
            var module = RequireModule();
            EnsureDrawingPossible(module, options.Force);

            if (SafeGet(() => module.IsPixelArt, false))
                throw new BridgeApiException("Image import is not supported in pixel-art mode yet. Use normal drawing mode.", 409);

            List<StrokeSpec> strokes;
            ImageBuildInfo info;
            try
            {
                strokes = BuildImageStrokes(module, imageBytes, options, out info);
            }
            catch (BridgeApiException)
            {
                throw;
            }
            catch (Exception ex)
            {
                throw new BridgeApiException("Could not decode the image: " + ex.Message, 400);
            }

            if (options.Clear)
                module.ExecuteClear(options.Force ? DrawingToolTrigger.System : DrawingToolTrigger.Button, fromLocal: true, sync: true);

            BridgePlugin.Instance.StartCoroutine(ApplyStrokesRoutine(module, strokes, options.Force, options.StrokesPerFrame, outcome =>
            {
                var result = outcome;
                result["image"] = new JObject
                {
                    ["width"] = info.Width,
                    ["height"] = info.Height,
                    ["columns"] = info.Columns,
                    ["rows"] = info.Rows,
                    ["colors"] = info.Levels
                };
                completed(result);
            }));
        }

        private static IEnumerator ApplyStrokesRoutine(
            DrawModule module,
            List<StrokeSpec> strokes,
            bool force,
            int strokesPerFrame,
            Action<JObject> completed)
        {
            var applied = 0;
            var failed = 0;
            var errors = new JArray();
            var perFrame = Math.Max(1, strokesPerFrame);

            try
            {
                EnsureDrawingPossible(module, force);

                for (var i = 0; i < strokes.Count; i++)
                {
                    if (module == null || module.gameObject == null)
                    {
                        failed += strokes.Count - i;
                        errors.Add("The drawing module disappeared while the batch was running.");
                        break;
                    }

                    try
                    {
                        ApplyOneStroke(module, strokes[i], force);
                        applied++;
                    }
                    catch (Exception ex)
                    {
                        failed++;
                        if (errors.Count < 12)
                            errors.Add($"stroke {i}: {ex.Message}");
                    }

                    if ((i + 1) % perFrame == 0)
                        yield return null;
                }
            }
            finally
            {
                completed?.Invoke(new JObject
                {
                    ["applied"] = applied,
                    ["failed"] = failed,
                    ["requested"] = strokes.Count,
                    ["errors"] = errors
                });
            }
        }

        private static void ApplyOneStroke(DrawModule module, StrokeSpec spec, bool force)
        {
            if (!force && !SafeGet(module.CanDraw, false))
                throw new BridgeApiException("Drawing is not allowed right now.", 409);

            if (spec == null || spec.Points == null || spec.Points.Count == 0)
                throw new BridgeApiException("Stroke has no points.", 400);

            var surface = module.DrawSurface;
            var isFill = string.Equals(spec.Type, "fill", StringComparison.OrdinalIgnoreCase);
            var type = spec.Type?.ToLowerInvariant();

            var points = new List<SerializableV2>(spec.Points.Count);
            foreach (var point in spec.Points)
            {
                var x = point[0];
                var y = point[1];
                if (string.Equals(spec.Space, "canvas", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(spec.Space, "world", StringComparison.OrdinalIgnoreCase))
                {
                    if (surface == null || !surface.IsInitialised || surface.Width <= 0.0001f || surface.Heigth <= 0.0001f)
                        throw new BridgeApiException("Canvas-space points need an initialized draw surface.", 409);
                    x = (x - surface.MinX) / surface.Width;
                    y = (y - surface.MinY) / surface.Heigth;
                }

                if (float.IsNaN(x) || float.IsNaN(y) || float.IsInfinity(x) || float.IsInfinity(y))
                    throw new BridgeApiException("Stroke contains NaN/Infinity coordinates.", 400);

                points.Add(new SerializableV2(Mathf.Clamp01(x), Mathf.Clamp01(y)));
            }

            if (type == "line" && points.Count == 1)
                type = "disc";
            if (type == "fill" && points.Count < 3)
                throw new BridgeApiException("A fill needs at least 3 points.", 400);
            if (type == "disc" && points.Count != 1)
                points = new List<SerializableV2> { points[0] };
            if (type != "fill" && type != "disc" && points.Count < 2)
                throw new BridgeApiException("A line needs at least 2 points (or use type=disc).", 400);

            if (isFill && points.Count > 1 &&
                Math.Abs(points[0].x - points[points.Count - 1].x) < 0.000001f &&
                Math.Abs(points[0].y - points[points.Count - 1].y) < 0.000001f)
            {
                points.RemoveAt(points.Count - 1);
            }

            var color = string.IsNullOrWhiteSpace(spec.Color)
                ? GetActiveColor(module)
                : ParseColor(spec.Color, GetActiveColor(module));

            var size = Mathf.Clamp(spec.Size ?? SafeGet(() => DrawModule.BrushSize.Value, 10), 1, 255);
            var layerModule = module as LayerDrawModule;
            var layer = spec.Layer ?? (layerModule != null ? layerModule.SelectedLayer : 0);
            // The game only has three sorting layers; anything above lands on 'Default'.
            layer = Mathf.Clamp(layer, 0, VirtualLayers.RealLayers - 1);

            var isFillFlag = type == "fill";
            var sortOrder = module.SortOrder.GetNextSortOrder(isFillFlag);
            if (spec.VLayer.HasValue && !isFillFlag)
            {
                VirtualLayers.Next(module, spec.VLayer.Value, out layer, out sortOrder);
                if (layerModule != null && layerModule.SelectedLayer != layer)
                    layerModule.SetLayer(layer);
            }
            var ident = Math.Max(1, DrawableElement.LastIdentifier + 1);

            var info = new LineInformation
            {
                DrawElementType = (byte)(isFillFlag ? 3 : (type == "disc" ? 1 : 0)),
                Points = points.ToArray(),
                BrushSize = (byte)size,
                Color = ToHex(color),
                Ident = ident,
                Owner = GetLocalPlayfabId(),
                SortingOrder = (short)Mathf.Clamp(sortOrder, short.MinValue, short.MaxValue),
                Layer = (sbyte)layer,
                IsNormalized = true,
                AspectRatio = surface != null && surface.IsInitialised && surface.Heigth > 0.0001f
                    ? surface.AspectRatio
                    : 16f / 9f,
                ScaleFactorWidth = surface != null && surface.IsInitialised && surface.Width > 0.0001f
                    ? 1f / surface.Width
                    : 1f / 1920f
            };

            var sortLayerType = DrawHelpers.SortLayerType.Normal;
            if (SortingLayerField != null)
            {
                try { sortLayerType = (DrawHelpers.SortLayerType)SortingLayerField.GetValue(module); }
                catch { /* keep Normal */ }
            }

            if (!DrawHelpers.SpawnDrawElement(out var spawned, module, info, module.LineParent, 0, sortLayerType) || spawned == null)
                throw new BridgeApiException("The game rejected the stroke (spawn failed).", 500);

            // DrawableLine only chooses round joins when the player has "high line
            // quality" enabled; otherwise every segment is a flat quad and thick
            // brush strokes read as square blocks. Force round joins per stroke so
            // the result matches the round brush preview and normal pen strokes.
            if (spawned is DrawableLine drawableLine &&
                (BridgePlugin.Instance == null ||
                 BridgePlugin.Instance.BridgeConfiguration.RoundLineJoins))
            {
                drawableLine.PolylineRenderer.Joins = PolylineJoins.Round;
            }

            TryAddToCreatedCollection(module, spawned);
            module.CheckSpecialMode(spawned, newlyCreatedByOwner: true);

            // Battle Royale runs an ML prediction for every finished stroke. Batches would
            // trigger that hundreds of times, so only run the base bookkeeping there.
            if (module.GetType().Name.IndexOf("BattleRoyal", StringComparison.OrdinalIgnoreCase) >= 0)
                BaseFinishDrawingMethod.Invoke(module, new object[] { spawned });
            else
                module.OnFinishDrawingCurrent(spawned);

            // The finish handler (OnShapeFillDone) may reset a fill outline to
            // the active colour, usually the dark ink colour. Polygon fills
            // currently render identically either way (verified pixel-for-pixel),
            // but real fills go through that handler, so keep the outline equal
            // to the fill colour instead of the active ink colour.
            if (isFillFlag && spawned is DrawableShape drawableShape)
            {
                drawableShape.OutlinePolyLine.Color = color;
            }

            DrawableElement.IncreaseIfHigher(info.Ident);
        }

        private static void TryAddToCreatedCollection(DrawModule module, DrawableElement element)
        {
            var collection = CreatedDrawablesField?.GetValue(module);
            if (collection == null)
                return;
            var add = collection.GetType().GetMethod("Add", new[] { typeof(DrawableElement) });
            add?.Invoke(collection, new object[] { element });
        }

        private static void EnsureDrawingPossible(DrawModule module, bool force)
        {
            if (module == null)
                throw new BridgeApiException("No drawing module is active. Open the sketchbook or join a draw round.", 409);
            if (module.gameObject == null || !module.gameObject.activeInHierarchy)
                throw new BridgeApiException("The drawing module is not active in the current scene.", 409);
            if (SafeGet(() => module.IsPixelArt, false))
                throw new BridgeApiException("This build of the bridge does not draw vector strokes in pixel-art mode.", 409);
            if (!force && !SafeGet(module.CanDraw, false))
                throw new BridgeApiException("The game does not allow drawing right now (not your turn / UI blocked). Use force=true to try anyway.", 409);
            if (module.LineParent == null)
                throw new BridgeApiException("The drawing surface is not ready yet.", 409);
        }

        private static DrawModule RequireModule()
        {
            var module = CurrentModule;
            if (module == null)
                throw new BridgeApiException("No drawing module is active. Open the sketchbook or join a draw round.", 409);
            return module;
        }

        private static Color GetActiveColor(DrawModule module)
        {
            return SafeGet(() => module.GetState().RGBColor, Color.black);
        }

        private static string GetLocalPlayfabId()
        {
            try
            {
                var module = CurrentModule;
                if (module != null && LocalPlayfabIdProperty != null)
                    return LocalPlayfabIdProperty.GetValue(module, null) as string ?? string.Empty;
            }
            catch { }

            try
            {
                return NetworkInit.PlayfabUser?.PlayfabTitleUserId ?? string.Empty;
            }
            catch
            {
                return string.Empty;
            }
        }

        private static List<StrokeSpec> BuildImageStrokes(
            DrawModule module,
            byte[] imageBytes,
            ImageOptions options,
            out ImageBuildInfo info)
        {
            if (imageBytes == null || imageBytes.Length == 0)
                throw new BridgeApiException("Empty image body. Send a PNG/JPEG as the raw request body.", 400);

            var texture = new Texture2D(2, 2, TextureFormat.RGBA32, false);
            try
            {
                if (!ImageConversion.LoadImage(texture, imageBytes, markNonReadable: false))
                    throw new BridgeApiException("Unity could not decode the image. PNG and JPEG are supported.", 400);

                var surface = module.DrawSurface;
                var canvasAspect = surface != null && surface.IsInitialised && surface.Heigth > 0.0001f
                    ? surface.AspectRatio
                    : 16f / 9f;
                var imageAspect = texture.width / (float)Math.Max(1, texture.height);

                var margin = Mathf.Clamp(options.Margin, 0f, 0.45f);
                var fitWidth = 1f - 2f * margin;
                var fitHeight = 1f - 2f * margin;
                if (imageAspect > canvasAspect)
                    fitHeight = fitWidth * canvasAspect / imageAspect;
                else
                    fitWidth = fitHeight * imageAspect / canvasAspect;

                var cells = Mathf.Clamp(options.Cells, 4, 200);
                int columns;
                int rows;
                if (imageAspect >= 1f)
                {
                    columns = cells;
                    rows = Mathf.Max(1, Mathf.RoundToInt(cells / imageAspect));
                }
                else
                {
                    rows = cells;
                    columns = Mathf.Max(1, Mathf.RoundToInt(cells * imageAspect));
                }

                var cellWidth = fitWidth / columns;
                var cellHeight = fitHeight / rows;
                var originX = 0.5f - fitWidth * 0.5f;
                var originY = 0.5f - fitHeight * 0.5f;
                var levels = Mathf.Clamp(options.Levels, 2, 64);
                var background = ParseColor(options.Background, Color.white);
                var backgroundHex = ToHex(background);

                var strokes = new List<StrokeSpec>();
                var tooMany = false;

                for (var row = 0; row < rows && !tooMany; row++)
                {
                    var runStart = -1;
                    var runColor = 0;
                    for (var col = 0; col <= columns; col++)
                    {
                        var hasCell = col < columns;
                        var colorInt = 0;
                        var skip = true;
                        if (hasCell)
                        {
                            var u = (col + 0.5f) / columns;
                            var v = (row + 0.5f) / rows;
                            var sampleX = Mathf.Clamp(Mathf.FloorToInt(u * texture.width), 0, texture.width - 1);
                            var sampleY = Mathf.Clamp(Mathf.FloorToInt(v * texture.height), 0, texture.height - 1);
                            var pixel = texture.GetPixel(sampleX, sampleY);
                            var alpha = pixel.a;
                            if (alpha <= 0.02f)
                            {
                                colorInt = backgroundHex;
                                skip = options.SkipBackground;
                            }
                            else
                            {
                                var composited = new Color(
                                    pixel.r * alpha + background.r * (1f - alpha),
                                    pixel.g * alpha + background.g * (1f - alpha),
                                    pixel.b * alpha + background.b * (1f - alpha),
                                    1f);
                                colorInt = ToHex(Quantize(composited, levels));
                                skip = options.SkipBackground && colorInt == backgroundHex;
                            }
                        }

                        var sameRun = hasCell && !skip && runStart >= 0 && colorInt == runColor;
                        if (sameRun)
                            continue;

                        if (runStart >= 0)
                        {
                            AddRunStroke(strokes, originX, originY, cellWidth, cellHeight, row, runStart, col - 1, runColor);
                            if (strokes.Count >= options.MaxStrokes)
                            {
                                tooMany = true;
                                break;
                            }
                        }

                        if (hasCell && !skip)
                        {
                            runStart = col;
                            runColor = colorInt;
                        }
                        else
                        {
                            runStart = -1;
                        }
                    }
                }

                if (tooMany)
                    throw new BridgeApiException(
                        $"The image would need more than {options.MaxStrokes} strokes. Lower cells/levels or raise maxStrokes.", 400);

                info = new ImageBuildInfo
                {
                    Width = texture.width,
                    Height = texture.height,
                    Columns = columns,
                    Rows = rows,
                    Levels = levels
                };
                return strokes;
            }
            finally
            {
                UnityEngine.Object.Destroy(texture);
            }
        }

        private static void AddRunStroke(
            List<StrokeSpec> strokes,
            float originX,
            float originY,
            float cellWidth,
            float cellHeight,
            int row,
            int fromColumn,
            int toColumn,
            int color)
        {
            const float overlap = 0.0012f;
            var x0 = Mathf.Clamp01(originX + fromColumn * cellWidth - overlap);
            var x1 = Mathf.Clamp01(originX + (toColumn + 1) * cellWidth + overlap);
            var y0 = Mathf.Clamp01(originY + row * cellHeight - overlap);
            var y1 = Mathf.Clamp01(originY + (row + 1) * cellHeight + overlap);

            strokes.Add(new StrokeSpec
            {
                Type = "fill",
                Color = "#" + color.ToString("X6"),
                Size = 1,
                Space = "normalized",
                Points = new List<float[]>
                {
                    new[] { x0, y0 },
                    new[] { x1, y0 },
                    new[] { x1, y1 },
                    new[] { x0, y1 }
                }
            });
        }

        private static Color Quantize(Color color, int levels)
        {
            var steps = levels - 1f;
            return new Color(
                Mathf.Round(Mathf.Clamp01(color.r) * steps) / steps,
                Mathf.Round(Mathf.Clamp01(color.g) * steps) / steps,
                Mathf.Round(Mathf.Clamp01(color.b) * steps) / steps,
                1f);
        }

        internal static Color ParseColor(string text, Color fallback)
        {
            if (string.IsNullOrWhiteSpace(text))
                return fallback;

            var value = text.Trim();
            if (value.StartsWith("#", StringComparison.Ordinal))
                value = value.Substring(1);

            if (value.StartsWith("0x", StringComparison.OrdinalIgnoreCase))
                value = value.Substring(2);

            if (value.Length == 3 || value.Length == 4)
            {
                value = string.Concat(value.Select(c => new string(c, 2)));
            }

            if ((value.Length == 6 || value.Length == 8) &&
                int.TryParse(value.Substring(0, 6), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var rgb))
            {
                return FromHex(rgb);
            }

            var parts = value.Split(new[] { ',', ' ', ';' }, StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length >= 3 &&
                float.TryParse(parts[0], NumberStyles.Float, CultureInfo.InvariantCulture, out var r) &&
                float.TryParse(parts[1], NumberStyles.Float, CultureInfo.InvariantCulture, out var g) &&
                float.TryParse(parts[2], NumberStyles.Float, CultureInfo.InvariantCulture, out var b))
            {
                var maximum = Math.Max(r, Math.Max(g, b));
                if (maximum > 1.001f)
                {
                    r /= 255f;
                    g /= 255f;
                    b /= 255f;
                }
                return new Color(Mathf.Clamp01(r), Mathf.Clamp01(g), Mathf.Clamp01(b), 1f);
            }

            throw new BridgeApiException($"Invalid color '{text}'. Use #RRGGBB, RRGGBB or 'r,g,b'.", 400);
        }

        internal static Color FromHex(int rgb)
        {
            return new Color(
                ((rgb >> 16) & 0xFF) / 255f,
                ((rgb >> 8) & 0xFF) / 255f,
                (rgb & 0xFF) / 255f,
                1f);
        }

        internal static int ToHex(Color color)
        {
            var c = (Color32)color;
            return (c.r << 16) | (c.g << 8) | c.b;
        }

        internal static string ToHexString(Color color)
        {
            return "#" + ToHex(color).ToString("X6");
        }

        private static double Round(float value)
        {
            if (float.IsNaN(value) || float.IsInfinity(value))
                return 0;
            return Math.Round(value, 5);
        }

        private static T SafeGet<T>(Func<T> getter, T fallback)
        {
            try
            {
                return getter();
            }
            catch
            {
                return fallback;
            }
        }

        internal sealed class StrokeSpec
        {
            public string Type = "line";
            public string Color;
            public int? Size;
            public string Space = "normalized";
            public int? Layer;
            public int? VLayer;
            public List<float[]> Points = new List<float[]>();
        }

        internal sealed class ImageOptions
        {
            public int Cells = 48;
            public int Levels = 8;
            public float Margin = 0.04f;
            public string Background = "#FFFFFF";
            public bool Clear = true;
            public bool Force;
            public bool SkipBackground = true;
            public int MaxStrokes = 6000;
            public int StrokesPerFrame = 24;
        }

        private struct ImageBuildInfo
        {
            public int Width;
            public int Height;
            public int Columns;
            public int Rows;
            public int Levels;
        }
    }
}

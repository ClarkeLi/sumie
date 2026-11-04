using System;
using System.Collections;
using Newtonsoft.Json.Linq;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.InputSystem.LowLevel;

namespace DrawGuessBridge
{
    // A virtual pen tablet. The game's own LinePressure component reads Pen.current,
    // so a stroke drawn through this device goes through the real input path: the
    // line grows frame by frame, its width follows the pressure, and the game itself
    // decides the final BrushSize, simplifies the points and synchronises the line.
    internal static partial class GameApi
    {
        private static Pen _virtualPen;

        internal static JObject PenStatus()
        {
            return new JObject
            {
                ["virtualPen"] = _virtualPen != null && _virtualPen.added,
                ["currentPen"] = Pen.current?.name,
                ["pointer"] = Pointer.current?.name,
                ["usePenPressure"] = SafeGet(() => MenuGameSettings_D.UsePenPressure.Value, false),
                ["focused"] = Application.isFocused,
                ["runInBackground"] = Application.runInBackground,
                ["background"] = InputSystem.settings.backgroundBehavior.ToString()
            };
        }

        /// <param name="samples">[[t_ms, x, y, pressure], ...] with x/y normalised (0..1, y up).</param>
        internal static void StartPenStroke(JArray samples, bool force, bool ignoreFocus, int? vlayer, Action<JObject> completed)
        {
            var module = RequireModule();
            EnsureDrawingPossible(module, force);
            if (samples == null || samples.Count < 2)
                throw new BridgeApiException("A pen stroke needs at least 2 samples.", 400);
            if (!SafeGet(() => MenuGameSettings_D.UsePenPressure.Value, false))
                throw new BridgeApiException("Pen pressure is switched off in the game settings.", 409);
            var surface = module.DrawSurface;
            if (surface == null || !surface.IsInitialised)
                throw new BridgeApiException("The drawing surface is not ready.", 409);
            if (Camera.main == null)
                throw new BridgeApiException("No main camera.", 409);

            var data = new float[samples.Count, 4];
            for (var i = 0; i < samples.Count; i++)
            {
                var row = (JArray)samples[i];
                for (var k = 0; k < 4; k++)
                    data[i, k] = row[k].Value<float>();
            }

            var armed = false;
            if (vlayer.HasValue)
            {
                if (!VirtualLayers.PatchActive)
                    throw new BridgeApiException("Virtual layers need the native pen patch, which is not active.", 409);
                VirtualLayers.Arm(module, vlayer.Value, out var real);
                armed = true;
                if (module is LayerDrawModule layerModule && layerModule.SelectedLayer != real)
                    layerModule.SetLayer(real);
            }

            BridgePlugin.Instance.StartCoroutine(PenStrokeRoutine(module, surface, data, ignoreFocus, armed, completed));
        }

        private static IEnumerator PenStrokeRoutine(DrawModule module, DrawableSurface surface, float[,] data,
            bool ignoreFocus, bool armed, Action<JObject> completed)
        {
            var result = new JObject();
            var previousBackground = InputSystem.settings.backgroundBehavior;
            var previousRun = Application.runInBackground;
            Pen pen = null;
            try
            {
                if (_virtualPen == null || !_virtualPen.added)
                    _virtualPen = InputSystem.AddDevice<Pen>("DGBridgePen");
                pen = _virtualPen;
                if (ignoreFocus)
                {
                    InputSystem.settings.backgroundBehavior = InputSettings.BackgroundBehavior.IgnoreFocus;
                    Application.runInBackground = true;
                }
            }
            catch (Exception ex)
            {
                result["error"] = "Could not create the virtual pen: " + ex.Message;
                if (armed)
                    VirtualLayers.Disarm();
                completed(result);
                yield break;
            }

            var before = SafeGet(() => module.GetMyDrawnElements().Count, 0);
            var count = data.GetLength(0);
            var endMs = data[count - 1, 0];
            var start = Time.realtimeSinceStartup;
            var frames = 0;
            var maxPressure = 0f;
            var index = 0;
            var failed = false;

            while (true)
            {
                var now = (Time.realtimeSinceStartup - start) * 1000f;
                if (module == null || module.gameObject == null)
                {
                    failed = true;
                    if (armed)
                        VirtualLayers.Disarm();
                    result["error"] = "The drawing module disappeared during the stroke.";
                    break;
                }

                while (index + 2 < count && data[index + 1, 0] <= now)
                    index++;
                var span = Mathf.Max(1e-3f, data[index + 1, 0] - data[index, 0]);
                var f = Mathf.Clamp01((now - data[index, 0]) / span);
                var x = Mathf.Lerp(data[index, 1], data[index + 1, 1], f);
                var y = Mathf.Lerp(data[index, 2], data[index + 1, 2], f);
                var pressure = Mathf.Lerp(data[index, 3], data[index + 1, 3], f);
                maxPressure = Mathf.Max(maxPressure, pressure);

                var world = new Vector3(surface.MinX + Mathf.Clamp01(x) * surface.Width,
                                        surface.MinY + Mathf.Clamp01(y) * surface.Heigth, 0f);
                var screen = Camera.main.WorldToScreenPoint(world);
                QueuePen(pen, new Vector2(screen.x, screen.y), pressure, true);
                frames++;

                if (now >= endMs)
                    break;
                yield return null;
            }

            // lift: pressure to zero, then give the game a few frames to finish the line
            var last = Camera.main.WorldToScreenPoint(new Vector3(
                surface.MinX + Mathf.Clamp01(data[count - 1, 1]) * surface.Width,
                surface.MinY + Mathf.Clamp01(data[count - 1, 2]) * surface.Heigth, 0f));
            QueuePen(pen, new Vector2(last.x, last.y), 0f, true);
            yield return null;
            QueuePen(pen, new Vector2(last.x, last.y), 0f, false);
            for (var i = 0; i < 4; i++)
                yield return null;
            // Park the pen in a corner of the window. Left over the canvas it would stay
            // "under the pointer" of every fill and line below it: the game then shows the
            // recolour preview there and the fills follow the active colour.
            QueuePen(pen, new Vector2(3f, 3f), 0f, false);
            yield return null;

            InputSystem.settings.backgroundBehavior = previousBackground;
            Application.runInBackground = previousRun;
            if (armed)
                VirtualLayers.Disarm();

            var elements = SafeGet(() => module.GetMyDrawnElements(), null);
            var after = elements?.Count ?? before;
            result["frames"] = frames;
            result["created"] = after - before;
            result["maxPressure"] = maxPressure;
            if (!failed && elements != null && after > before)
            {
                var element = elements[elements.Count - 1];
                result["brushSize"] = (int)element.BrushSize;
                result["type"] = element.GetType().Name;
                if (element is DrawableLine line)
                    result["points"] = line.PolylineRenderer.points.Count;
            }

            completed(result);
        }

        private static void QueuePen(Pen pen, Vector2 position, float pressure, bool inRange)
        {
            var state = new PenState { position = position, pressure = pressure };
            if (inRange)
                state = state.WithButton(PenButton.InRange);
            if (pressure > 0.05f)
                state = state.WithButton(PenButton.Tip);
            InputSystem.QueueStateEvent(pen, state);
        }
    }
}

using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using HarmonyLib;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace DrawGuessBridge
{
    // The game only has three real sorting layers (SHAPES_1..3, each with a _FILL variant).
    // A line carries two synchronised values, Layer and SortingOrder, and every client
    // sorts by (layer, order). A virtual layer is therefore (real layer, band of the
    // SortingOrder range): a line of a higher virtual layer is always drawn above a line
    // of a lower one, for every player and in the exports, whatever the drawing order.
    //
    //   virtual v  ->  real = v / Bands, band = v % Bands
    //   SortingOrder = MinOrder + band * BandWidth + counter[real, band]
    internal static class VirtualLayers
    {
        internal const int RealLayers = 3;
        internal const int MinBands = 2;
        internal const int MaxBands = 8;
        private const int MinOrder = -28671;
        private const int MaxOrder = 32767;

        private static readonly int[,] Counters = new int[RealLayers, MaxBands];
        private static int _boundModule;

        // set for exactly one DrawableElement.Init call (native pen strokes)
        private static bool _pending;
        private static int _pendingReal;
        private static int _pendingOrder;

        internal static bool PatchActive;
        internal static bool ExportPatchActive;

        internal static int Bands
        {
            get
            {
                var configured = BridgePlugin.Instance?.BridgeConfiguration?.BandsPerLayer ?? 3;
                return Mathf.Clamp(configured, MinBands, MaxBands);
            }
        }

        internal static int Total => RealLayers * Bands;
        private static int BandWidth => (MaxOrder - MinOrder + 1) / Bands;

        internal static void Reset()
        {
            Array.Clear(Counters, 0, Counters.Length);
            _boundModule = 0;
        }

        /// <summary>Real layer and SortingOrder for the next element of virtual layer v.</summary>
        internal static void Next(DrawModule module, int v, out int real, out int order)
        {
            Bind(module);
            v = Mathf.Clamp(v, 0, Total - 1);
            var bands = Bands;
            real = v / bands;
            var band = v % bands;
            var used = Counters[real, band];
            if (used >= BandWidth - 1)
            {
                used = BandWidth - 1;     // practically unreachable (~20 000 strokes per band)
                BridgePlugin.Instance?.Log.LogWarning($"virtual layer {v} is full; strokes share one sort order");
            }
            else
            {
                Counters[real, band] = used + 1;
            }
            order = MinOrder + band * BandWidth + used;
        }

        // Learn the counters from the elements already on the canvas (after a reload or
        // when the module changed), so new strokes never fall below existing ones.
        private static void Bind(DrawModule module)
        {
            var id = module == null ? 0 : module.GetInstanceID();
            if (id == _boundModule)
                return;
            Reset();
            _boundModule = id;
            var field = typeof(DrawModule).GetField("CreatedDrawablesByMe",
                BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
            var collection = field?.GetValue(module) as IEnumerable;
            if (collection == null)
                return;
            var bands = Bands;
            foreach (var item in collection)
            {
                if (!(item is DrawableElement element) || element == null)
                    continue;
                var order = element.GetGenericRenderer().SortingOrder;
                var real = Mathf.Clamp(element.Layer, 0, RealLayers - 1);
                var band = Mathf.Clamp((order - MinOrder) / BandWidth, 0, bands - 1);
                var used = order - (MinOrder + band * BandWidth) + 1;
                if (used > Counters[real, band])
                    Counters[real, band] = used;
            }
        }

        /// <summary>Arm the Init patch for the next element the game creates.</summary>
        internal static void Arm(DrawModule module, int v, out int real)
        {
            Next(module, v, out real, out var order);
            _pending = true;
            _pendingReal = real;
            _pendingOrder = order;
        }

        internal static void Disarm()
        {
            _pending = false;
        }

        internal static bool IsArmed => _pending;

        internal static JObject Describe(DrawModule module)
        {
            return new JObject
            {
                ["virtual"] = Total,
                ["real"] = RealLayers,
                ["bands"] = Bands,
                ["realLayerUi"] = module is LayerDrawModule,
                ["module"] = module == null ? null : module.GetType().Name,
                ["nativePatch"] = PatchActive,
                ["exportSort"] = ExportPatchActive
            };
        }

        internal static void Install(Harmony harmony, Action<string> log)
        {
            try
            {
                harmony.PatchAll(typeof(InitPatch));
                PatchActive = true;
            }
            catch (Exception ex)
            {
                log("Virtual layers: native pen patch failed: " + ex.Message);
            }

            if (BridgePlugin.Instance?.BridgeConfiguration?.SortExportByLayer ?? true)
            {
                try
                {
                    harmony.PatchAll(typeof(ExportPatch));
                    ExportPatchActive = true;
                }
                catch (Exception ex)
                {
                    log("Virtual layers: export sort patch failed: " + ex.Message);
                }
            }
        }

        [HarmonyPatch]
        private static class InitPatch
        {
            private static MethodBase TargetMethod()
            {
                return AccessTools.Method(typeof(DrawableElement), "Init",
                    new[] { typeof(byte), typeof(Color), typeof(int), typeof(int), typeof(string), typeof(DrawModule) });
            }

            private static void Prefix(ref int sortingOrder, ref int sortingLayer, out bool __state)
            {
                __state = false;
                if (!_pending)
                    return;
                _pending = false;
                sortingOrder = _pendingOrder;
                sortingLayer = DrawHelpers.GetSortingLayer(_pendingReal, false, DrawHelpers.SortLayerType.Normal);
                __state = true;
            }

            private static void Postfix(DrawableElement __instance, bool __state)
            {
                if (__state && __instance != null)
                    __instance.Layer = _pendingReal;
            }
        }

        // MatchDisplayer renumbers lines by list order, and the list is in creation order.
        // Sort it the way the exporters do, so the gallery shows the same picture.
        [HarmonyPatch]
        private static class ExportPatch
        {
            private static IEnumerable<MethodBase> TargetMethods()
            {
                foreach (var type in new[] { typeof(DrawModule), typeof(SyncDrawModule), typeof(LobbyDrawModule) })
                {
                    var method = AccessTools.DeclaredMethod(type, "GetDrawingAsLineInfo", new[] { typeof(bool), typeof(bool) });
                    if (method != null)
                        yield return method;
                }
            }

            private static void Postfix(DrawModule __instance, ref List<LineInformation> __result)
            {
                try
                {
                    if (__result == null || __result.Count < 2 || __instance.IsPixelArt)
                        return;
                    __result = __result
                        .Select((line, index) => new { line, index })
                        .OrderBy(x => (int)x.line.Layer)
                        .ThenBy(x => x.line.IsFill ? 0 : 1)
                        .ThenBy(x => (int)x.line.SortingOrder)
                        .ThenBy(x => x.index)
                        .Select(x => x.line)
                        .ToList();
                }
                catch
                {
                    // keep the unsorted list
                }
            }
        }
    }
}

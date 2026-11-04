using System;
using BepInEx;
using BepInEx.Configuration;
using BepInEx.Unity.Mono;
using HarmonyLib;
using UnityEngine;

namespace DrawGuessBridge
{
    [BepInPlugin(Guid, "Draw & Guess Bridge", "1.0.0")]
    public sealed class BridgePlugin : BaseUnityPlugin
    {
        public const string Guid = "com.clarke.drawguess.bridge";
        public const string DisplayName = "Draw & Guess Bridge";
        public const string Version = "1.0.0";

        internal static BridgePlugin Instance { get; private set; }

        private BridgeServer _server;

        internal BridgeConfig BridgeConfiguration { get; private set; }

        internal StudioProcess Studio { get; private set; }

        internal BepInEx.Logging.ManualLogSource Log => Logger;

        private void Awake()
        {
            Instance = this;
            DontDestroyOnLoad(gameObject);

            BridgeConfiguration = new BridgeConfig
            {
                ListenAddress = Config.Bind(
                    "Server", "ListenAddress", "127.0.0.1",
                    "Address the local bridge listens on. Keep 127.0.0.1 unless you really need LAN access.").Value,
                Port = Config.Bind(
                    "Server", "Port", 8765,
                    "TCP port for the HTTP/WebSocket bridge.").Value,
                Token = Config.Bind(
                    "Server", "Token", "",
                    "Optional API token. When set, clients must send it as 'X-Api-Key' or 'Authorization: Bearer <token>'.").Value,
                StrokesPerFrame = Config.Bind(
                    "Drawing", "StrokesPerFrame", 24,
                    "Maximum number of externally supplied strokes applied per rendered frame.").Value,
                RoundLineJoins = Config.Bind(
                    "Drawing", "RoundLineJoins", true,
                    "Force round polyline joins for bridge strokes. The game otherwise uses " +
                    "flat/square joins when its 'high line quality' option is off. This covers " +
                    "joins only; round stroke ends are added by clients as same-colour discs.").Value,
                LongOperationTimeoutSeconds = Config.Bind(
                    "Drawing", "LongOperationTimeoutSeconds", 180,
                    "Timeout for image/stroke batches in seconds.").Value,
                BandsPerLayer = Config.Bind(
                    "Layers", "BandsPerLayer", 3,
                    "Virtual layers = 3 real layers x this many bands (2..8). 3 gives 9 virtual layers.").Value,
                SortExportByLayer = Config.Bind(
                    "Layers", "SortExportByLayer", true,
                    "Sort exported drawings by (layer, fill, sort order) so the gallery shows virtual layers correctly.").Value,
                LogRequests = Config.Bind(
                    "Server", "LogRequests", false,
                    "Log every HTTP request to the BepInEx console.").Value,
                StudioEnabled = Config.Bind(
                    "Studio", "Enabled", true,
                    "Start the local Python studio service (style-based image rendering) with the game.").Value,
                StudioPython = Config.Bind(
                    "Studio", "Python", "",
                    "Python interpreter that has numpy/opencv/Pillow (the installer sets this to the project virtualenv).").Value,
                StudioScript = Config.Bind(
                    "Studio", "Script", "",
                    "Path to client/studio_server.py (set by the installer).").Value,
                StudioWorkingDirectory = Config.Bind(
                    "Studio", "WorkingDirectory", "",
                    "Repository root used as the studio process working directory (set by the installer).").Value,
                StudioPort = Config.Bind(
                    "Studio", "Port", 8770,
                    "Local port for the studio service. The web UI discovers it through /api/v1/studio.").Value,
            };

            try
            {
                Harmony.CreateAndPatchAll(typeof(LocalPlayerFallbackPatch));
                Logger.LogInfo("Offline LocalPlayfabId fallback installed");
            }
            catch (Exception ex)
            {
                Logger.LogWarning("Could not install LocalPlayfabId fallback: " + ex.Message);
            }

            VirtualLayers.Install(new Harmony(Guid + ".layers"), message => Logger.LogWarning(message));

            _server = new BridgeServer(this, BridgeConfiguration);
            try
            {
                _server.Start();
                Logger.LogInfo($"{DisplayName} {Version} ready");
                Logger.LogInfo($"HTTP API:   http://127.0.0.1:{BridgeConfiguration.Port}/api/v1/state");
                Logger.LogInfo($"WebSocket:  ws://127.0.0.1:{BridgeConfiguration.Port}/ws");
                Logger.LogInfo($"Web UI:     http://127.0.0.1:{BridgeConfiguration.Port}/");
            }
            catch (Exception ex)
            {
                Logger.LogError($"Could not start bridge server: {ex}");
                _server = null;
            }

            Studio = new StudioProcess(BridgeConfiguration, BridgeConfiguration.Port,
                                       message => Logger.LogInfo("[studio] " + message));
            if (BridgeConfiguration.StudioEnabled)
            {
                try
                {
                    Studio.Start();
                }
                catch (Exception ex)
                {
                    Logger.LogWarning("Could not start the studio service: " + ex.Message);
                }
            }
        }

        private void Update()
        {
            _server?.Pump();
        }

        private void OnDestroy()
        {
            try { Studio?.Stop(); } catch { }
            Studio = null;
            _server?.Stop();
            _server = null;
            if (Instance == this)
                Instance = null;
        }

        private void OnApplicationQuit()
        {
            try { Studio?.Stop(); } catch { }
            _server?.Stop();
        }
    }
}

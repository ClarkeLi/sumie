namespace DrawGuessBridge
{
    internal sealed class BridgeConfig
    {
        public string ListenAddress = "127.0.0.1";
        public int Port = 8765;
        public string Token = "";
        public int StrokesPerFrame = 24;
        public bool RoundLineJoins = true;
        public int LongOperationTimeoutSeconds = 180;
        public bool LogRequests;

        // Virtual layers: bands of the sort order per real layer (3 real layers).
        public int BandsPerLayer = 3;
        public bool SortExportByLayer = true;

        // Optional local Python renderer service used by the web studio.
        public bool StudioEnabled = true;
        // Written by tools/install.py; empty until the installer has run.
        public string StudioPython = "";
        public string StudioScript = "";
        public string StudioWorkingDirectory = "";
        public int StudioPort = 8770;
    }
}

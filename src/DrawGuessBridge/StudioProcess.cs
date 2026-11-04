using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using Newtonsoft.Json.Linq;

namespace DrawGuessBridge
{
    /// <summary>
    /// Owns the optional local Python "studio" service. The service turns pasted
    /// images into renderer jobs and is what makes the web UI's style controls
    /// work; the bridge itself cannot run OpenCV or torch.
    /// </summary>
    internal sealed class StudioProcess
    {
        private readonly BridgeConfig _config;
        private readonly int _bridgePort;
        private readonly Action<string> _log;
        private readonly object _lock = new object();
        private readonly StringBuilder _output = new StringBuilder();

        private Process _process;
        private bool _startedByUs;
        private string _lastError = "";

        internal StudioProcess(BridgeConfig config, int bridgePort, Action<string> log)
        {
            _config = config;
            _bridgePort = bridgePort;
            _log = log;
        }

        internal bool Enabled => _config.StudioEnabled;

        internal JObject Status()
        {
            lock (_lock)
            {
                var running = IsPortOpen(_config.StudioPort);
                var pid = 0;
                try
                {
                    if (_process != null && !_process.HasExited)
                    {
                        pid = _process.Id;
                        running = true;
                    }
                }
                catch
                {
                    // The process object can be invalid after a crash; the port
                    // probe above is authoritative.
                }

                return new JObject
                {
                    ["enabled"] = _config.StudioEnabled,
                    ["running"] = running,
                    ["startedByUs"] = _startedByUs,
                    ["pid"] = pid,
                    ["port"] = _config.StudioPort,
                    ["python"] = _config.StudioPython,
                    ["script"] = _config.StudioScript,
                    ["workingDirectory"] = _config.StudioWorkingDirectory,
                    ["lastError"] = _lastError,
                    ["log"] = Tail(20),
                };
            }
        }

        internal JObject Start()
        {
            lock (_lock)
            {
                if (!_config.StudioEnabled)
                    return StatusError("The studio service is disabled in the BepInEx config.");

                if (IsPortOpen(_config.StudioPort))
                    return Status();

                if (string.IsNullOrWhiteSpace(_config.StudioScript) ||
                    string.IsNullOrWhiteSpace(_config.StudioPython) ||
                    string.IsNullOrWhiteSpace(_config.StudioWorkingDirectory))
                    return StatusError("The studio paths are not configured. Run `python tools/install.py` " +
                                       "or fill the [Studio] section of the BepInEx config.");
                if (!File.Exists(_config.StudioScript))
                    return StatusError("Studio script not found: " + _config.StudioScript);
                // A bare command such as "python" is resolved through PATH.
                var pythonIsPath = _config.StudioPython.IndexOfAny(new[] { '/', '\\' }) >= 0;
                if (pythonIsPath && !File.Exists(_config.StudioPython))
                    return StatusError("Python not found: " + _config.StudioPython);

                try
                {
                    var workDir = Path.Combine(_config.StudioWorkingDirectory, ".cache", "studio");
                    var arguments = "-u " + Quote(_config.StudioScript) +
                                    " --port " + _config.StudioPort +
                                    " --bridge-url http://127.0.0.1:" + _bridgePort +
                                    " --repo " + Quote(_config.StudioWorkingDirectory) +
                                    " --work-dir " + Quote(workDir);
                    if (!string.IsNullOrEmpty(_config.Token))
                        arguments += " --bridge-token " + Quote(_config.Token);

                    var info = new ProcessStartInfo
                    {
                        FileName = _config.StudioPython,
                        Arguments = arguments,
                        WorkingDirectory = _config.StudioWorkingDirectory,
                        UseShellExecute = false,
                        RedirectStandardOutput = true,
                        RedirectStandardError = true,
                        CreateNoWindow = true,
                    };
                    info.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";

                    var process = new Process { StartInfo = info, EnableRaisingEvents = true };
                    process.OutputDataReceived += (sender, eventArgs) =>
                    {
                        if (eventArgs.Data != null) Append(eventArgs.Data);
                    };
                    process.ErrorDataReceived += (sender, eventArgs) =>
                    {
                        if (eventArgs.Data != null) Append(eventArgs.Data);
                    };
                    process.Exited += (sender, eventArgs) =>
                    {
                        _log("studio service exited");
                    };

                    if (!process.Start())
                        return StatusError("The studio process did not start.");

                    _process = process;
                    _startedByUs = true;
                    _lastError = "";
                    process.BeginOutputReadLine();
                    process.BeginErrorReadLine();
                    _log("studio service starting on port " + _config.StudioPort);

                    if (process.WaitForExit(500))
                    {
                        _lastError = "The studio service exited during startup. " + Tail(6);
                        _process = null;
                        _startedByUs = false;
                    }
                }
                catch (Exception ex)
                {
                    _lastError = ex.Message;
                    _log("could not start studio service: " + ex);
                }

                return Status();
            }
        }

        internal void Stop()
        {
            Process process;
            lock (_lock)
            {
                process = _process;
                if (process == null || !_startedByUs)
                    return;
                _startedByUs = false;
            }

            try
            {
                RequestShutdown(_config.StudioPort);
            }
            catch
            {
                // Fall through to the process kill.
            }

            try
            {
                if (!process.WaitForExit(2500))
                    process.Kill();
            }
            catch
            {
                try { process.Kill(); } catch { }
            }

            lock (_lock)
            {
                if (ReferenceEquals(_process, process))
                {
                    _process = null;
                    _startedByUs = false;
                }
            }
        }

        private JObject StatusError(string message)
        {
            _lastError = message;
            _log(message);
            return Status();
        }

        private void Append(string line)
        {
            lock (_lock)
            {
                if (_output.Length > 16000)
                    _output.Remove(0, _output.Length - 12000);
                _output.AppendLine(line);
            }
        }

        private string Tail(int lines)
        {
            string text;
            lock (_lock)
                text = _output.ToString();
            var split = text.Split(new[] { '\n' }, StringSplitOptions.RemoveEmptyEntries);
            var start = Math.Max(0, split.Length - Math.Max(1, lines));
            return string.Join("\n", split, start, split.Length - start);
        }

        private static bool IsPortOpen(int port)
        {
            try
            {
                using (var client = new TcpClient())
                {
                    var task = client.ConnectAsync("127.0.0.1", port);
                    if (!task.Wait(250))
                        return false;
                    return client.Connected;
                }
            }
            catch
            {
                return false;
            }
        }

        private static void RequestShutdown(int port)
        {
            var request = (HttpWebRequest)WebRequest.Create(
                "http://127.0.0.1:" + port + "/shutdown");
            request.Method = "POST";
            request.ContentLength = 0;
            request.Timeout = 600;
            request.ReadWriteTimeout = 600;
            using ((HttpWebResponse)request.GetResponse())
            {
            }
        }

        private static string Quote(string value)
        {
            if (string.IsNullOrEmpty(value))
                return "\"\"";
            return "\"" + value.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
        }
    }
}

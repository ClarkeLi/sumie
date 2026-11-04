using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace DrawGuessBridge
{
    internal sealed class BridgeServer
    {
        private const string WebSocketGuid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
        private const int MaxHeaderBytes = 64 * 1024;
        private const int MaxBodyBytes = 32 * 1024 * 1024;

        private readonly BridgePlugin _plugin;
        private readonly BridgeConfig _config;
        private readonly MainThreadDispatcher _dispatcher = new MainThreadDispatcher();
        private readonly object _clientsLock = new object();
        private readonly List<WebSocketClient> _clients = new List<WebSocketClient>();
        private readonly string _uiPath;

        private TcpListener _listener;
        private Thread _acceptThread;
        private volatile bool _running;
        private string _lastBroadcastState;
        private float _nextStateBroadcast;

        public BridgeServer(BridgePlugin plugin, BridgeConfig config)
        {
            _plugin = plugin;
            _config = config;
            var assemblyDirectory = Path.GetDirectoryName(typeof(BridgePlugin).Assembly.Location) ?? string.Empty;
            _uiPath = Path.Combine(assemblyDirectory, "ui.html");
        }

        public void Start()
        {
            IPAddress address;
            if (string.Equals(_config.ListenAddress, "0.0.0.0", StringComparison.Ordinal))
                address = IPAddress.Any;
            else if (string.Equals(_config.ListenAddress, "::", StringComparison.Ordinal))
                address = IPAddress.IPv6Any;
            else if (!IPAddress.TryParse(_config.ListenAddress, out address))
                throw new InvalidOperationException($"Invalid ListenAddress '{_config.ListenAddress}'.");

            _listener = new TcpListener(address, _config.Port);
            // Deliberately do not set SO_REUSEADDR: on macOS it lets two game
            // instances bind the same port, which makes requests hit a random one.
            _listener.Start();
            _running = true;

            _acceptThread = new Thread(AcceptLoop)
            {
                IsBackground = true,
                Name = "DrawGuessBridge.Listener"
            };
            _acceptThread.Start();
        }

        public void Stop()
        {
            _running = false;

            try { _listener?.Stop(); } catch { }
            _listener = null;

            List<WebSocketClient> clients;
            lock (_clientsLock)
            {
                clients = new List<WebSocketClient>(_clients);
                _clients.Clear();
            }

            foreach (var client in clients)
                client.Dispose();

            try { _acceptThread?.Join(2000); } catch { }
            _acceptThread = null;
        }

        public void Pump()
        {
            _dispatcher.Pump();

            if (UnityEngine.Time.unscaledTime < _nextStateBroadcast)
                return;

            _nextStateBroadcast = UnityEngine.Time.unscaledTime + 0.5f;
            try
            {
                var state = GameApi.GetState();
                var json = state.ToString(Formatting.None);
                if (json == _lastBroadcastState)
                    return;

                _lastBroadcastState = json;
                Broadcast(new JObject
                {
                    ["type"] = "event",
                    ["event"] = "state",
                    ["data"] = state
                });
            }
            catch
            {
                // The game can be in the middle of a scene change; try again next tick.
            }
        }

        private void AcceptLoop()
        {
            while (_running)
            {
                TcpClient client;
                try
                {
                    client = _listener.AcceptTcpClient();
                }
                catch (SocketException)
                {
                    if (_running)
                        _plugin.Log.LogWarning("Bridge listener socket stopped unexpectedly.");
                    return;
                }
                catch (ObjectDisposedException)
                {
                    return;
                }
                catch (Exception ex)
                {
                    if (_running)
                        _plugin.Log.LogError("Bridge accept failed: " + ex.Message);
                    return;
                }

                ThreadPool.QueueUserWorkItem(_ => HandleClient(client));
            }
        }

        private void HandleClient(TcpClient client)
        {
            var keepOpen = false;
            try
            {
                client.NoDelay = true;
                client.ReceiveTimeout = 15000;
                client.SendTimeout = 15000;
                using (var stream = client.GetStream())
                {
                    var request = HttpRequest.Read(stream, MaxHeaderBytes, MaxBodyBytes);
                    if (request == null)
                        return;

                    if (_config.LogRequests)
                        _plugin.Log.LogInfo($"{request.Method} {request.Path}{request.RawQuery}");

                    if (IsWebSocketUpgrade(request))
                    {
                        keepOpen = true;
                        HandleWebSocket(client, stream, request);
                        return;
                    }

                    var response = Route(request);
                    WriteResponse(stream, response);
                }
            }
            catch (IOException)
            {
                // Client disconnected.
            }
            catch (SocketException)
            {
                // Client disconnected.
            }
            catch (Exception ex)
            {
                _plugin.Log.LogWarning("Bridge request failed: " + ex.Message);
            }
            finally
            {
                if (!keepOpen)
                {
                    try { client.Close(); } catch { }
                }
            }
        }

        private static bool IsWebSocketUpgrade(HttpRequest request)
        {
            return request.Headers.TryGetValue("Upgrade", out var upgrade) &&
                   string.Equals(upgrade, "websocket", StringComparison.OrdinalIgnoreCase);
        }

        private ApiResult Route(HttpRequest request)
        {
            try
            {
                if (string.Equals(request.Method, "OPTIONS", StringComparison.OrdinalIgnoreCase))
                    return ApiResult.NoContent();

                var path = NormalizePath(request.Path);
                var query = ParseQuery(request.RawQuery);

                if (string.Equals(path, "/health", StringComparison.OrdinalIgnoreCase))
                {
                    return ApiResult.Ok(new JObject
                    {
                        ["name"] = BridgePlugin.DisplayName,
                        ["version"] = BridgePlugin.Version,
                        ["protocol"] = 1
                    });
                }

                if (string.Equals(path, "/", StringComparison.Ordinal) ||
                    string.Equals(path, "/ui", StringComparison.OrdinalIgnoreCase))
                {
                    return ApiResult.Html(ReadUi());
                }

                if (string.Equals(path, "/favicon.ico", StringComparison.OrdinalIgnoreCase))
                    return ApiResult.NoContent();

                if (!IsAuthorized(request, query))
                    return ApiResult.Error(401, "Missing or invalid API token. Send X-Api-Key or Authorization: Bearer <token>.");

                if (request.Method == "GET" || request.Method == "HEAD")
                    return RouteGet(path, query);

                if (request.Method == "POST")
                    return RoutePost(request, path, query);

                return ApiResult.Error(405, "Method not allowed.");
            }
            catch (BridgeApiException ex)
            {
                return ApiResult.Error(ex.StatusCode, ex.Message);
            }
            catch (TimeoutException ex)
            {
                return ApiResult.Error(504, ex.Message);
            }
            catch (Exception ex)
            {
                _plugin.Log.LogError("Bridge API error: " + ex);
                return ApiResult.Error(500, ex.Message);
            }
        }

        private ApiResult RouteGet(string path, Dictionary<string, string> query)
        {
            if (path == "/api/v1/state" || path == "/api/v1/status")
                return InvokeJson(GameApi.GetState);

            if (path == "/api/v1/lines" || path == "/api/v1/snapshot")
            {
                var limit = GetInt(query, "limit", 10000, 0, 100000);
                return InvokeJson(() => GameApi.GetLines(limit));
            }

            if (path == "/api/v1/ui/buttons")
                return InvokeJson(() => new JObject { ["buttons"] = GameApi.GetUiButtons() });

            if (path == "/api/v1/pen")
                return InvokeJson(GameApi.PenStatus);

            if (path == "/api/v1/layers")
                return InvokeJson(GameApi.VirtualLayerInfo);

            if (path == "/api/v1/screenshot")
            {
                var file = GetString(query, "path", null);
                return RunLong(completed => GameApi.StartScreenshot(file, completed), 20);
            }

            if (path == "/api/v1/round" || path == "/api/v1/timer")
                return InvokeJson(GameApi.GetRound);

            if (path == "/api/v1/game")
                return InvokeJson(GameApi.GetGameSnapshot);

            if (path == "/api/v1/studio")
            {
                var studio = _plugin.Studio;
                return ApiResult.Ok(studio != null
                    ? studio.Status()
                    : new JObject { ["enabled"] = false, ["running"] = false });
            }

            if (path == "/api/v1/debug/coords")
            {
                var x = GetFloat(query, "x", 0.5f, -10f, 10f);
                var y = GetFloat(query, "y", 0.5f, -10f, 10f);
                return InvokeJson(() => GameApi.GetCoords(x, y));
            }

            return ApiResult.Error(404, "Unknown endpoint.");
        }

        private ApiResult RoutePost(HttpRequest request, string path, Dictionary<string, string> query)
        {
            if (path == "/api/v1/strokes" || path == "/api/v1/draw")
            {
                var payload = ParseJsonBody(request);
                var clear = GetBool(payload, "clear", false);
                var force = GetBool(payload, "force", false);
                var strokes = ParseStrokes(payload);
                if (strokes.Count == 0)
                    throw new BridgeApiException("No strokes supplied.", 400);

                return RunLong(completed => GameApi.StartApplyStrokes(
                    strokes, clear, force, _config.StrokesPerFrame, result =>
                    {
                        completed(result);
                        BroadcastApplied(result);
                    }), _config.LongOperationTimeoutSeconds);
            }

            if (path == "/api/v1/stroke/pen")
            {
                var payload = ParseJsonBody(request);
                var samples = payload["samples"] as JArray;
                if (samples == null)
                    throw new BridgeApiException("Missing 'samples': [[t_ms, x, y, pressure], ...].", 400);
                var force = GetBool(payload, "force", false);
                var ignoreFocus = GetBool(payload, "ignoreFocus", true);
                int? vlayer = payload["vlayer"] != null && payload["vlayer"].Type != JTokenType.Null
                    ? payload["vlayer"].Value<int>()
                    : (int?)null;
                return RunLong(completed => GameApi.StartPenStroke(samples, force, ignoreFocus, vlayer, completed),
                    _config.LongOperationTimeoutSeconds);
            }

            if (path == "/api/v1/image")
            {
                var options = new GameApi.ImageOptions
                {
                    Cells = GetInt(query, "cells", 48, 4, 200),
                    Levels = GetInt(query, "levels", 8, 2, 64),
                    Margin = GetFloat(query, "margin", 0.04f, 0f, 0.45f),
                    Background = GetString(query, "background", "#FFFFFF"),
                    Clear = GetBool(query, "clear", true),
                    Force = GetBool(query, "force", false),
                    SkipBackground = GetBool(query, "skipBackground", true),
                    MaxStrokes = GetInt(query, "maxStrokes", 6000, 1, 50000),
                    StrokesPerFrame = GetInt(query, "perFrame", _config.StrokesPerFrame, 1, 1000)
                };

                var imageBytes = ExtractImageBytes(request);
                return RunLong(completed => GameApi.StartApplyImage(imageBytes, options, result =>
                {
                    completed(result);
                    BroadcastApplied(result);
                }), _config.LongOperationTimeoutSeconds);
            }

            if (path == "/api/v1/clear")
            {
                var payload = ParseJsonBody(request, allowEmpty: true);
                var force = GetBool(payload, "force", false);
                return InvokeJson(() => GameApi.Clear(force));
            }

            if (path == "/api/v1/undo")
                return InvokeJson(GameApi.Undo);

            if (path == "/api/v1/redo")
                return InvokeJson(GameApi.Redo);

            if (path == "/api/v1/submit")
                return InvokeJson(GameApi.Submit);

            if (path == "/api/v1/color")
            {
                var payload = ParseJsonBody(request, allowEmpty: true);
                var color = payload["color"]?.ToString() ?? GetString(query, "color", null);
                if (string.IsNullOrWhiteSpace(color))
                    throw new BridgeApiException("Missing color.", 400);
                return InvokeJson(() => GameApi.SetColor(color));
            }

            if (path == "/api/v1/brush")
            {
                var payload = ParseJsonBody(request, allowEmpty: true);
                var size = GetInt(payload, "size", GetInt(query, "size", DrawModule.BrushSize.Value, 1, 255), 1, 255);
                return InvokeJson(() => GameApi.SetBrush(size));
            }

            if (path == "/api/v1/tool")
            {
                var payload = ParseJsonBody(request, allowEmpty: true);
                var tool = GetString(payload, "tool", null) ?? GetString(query, "tool", null);
                if (string.IsNullOrWhiteSpace(tool))
                    throw new BridgeApiException("Missing tool.", 400);
                return InvokeJson(() => GameApi.ActivateTool(tool));
            }

            if (path == "/api/v1/fill")
            {
                var payload = ParseJsonBody(request);
                var points = payload["points"] as JArray;
                if (points == null && payload["point"] != null)
                    points = new JArray(payload["point"]);
                if (points == null || points.Count == 0)
                    throw new BridgeApiException("Missing 'points' array for fill seeds.", 400);
                var color = GetString(payload, "color", null);
                var space = GetString(payload, "space", "normalized");
                var force = GetBool(payload, "force", false);
                return InvokeJson(() => GameApi.RealFill(points, color, space, force));
            }

            if (path == "/api/v1/layer")
            {
                var payload = ParseJsonBody(request);
                var index = GetInt(payload, "index", GetInt(query, "index", 0, 0, 31), 0, 31);
                return InvokeJson(() => GameApi.SetLayer(index));
            }

            if (path == "/api/v1/ui/click")
            {
                var payload = ParseJsonBody(request, allowEmpty: true);
                var text = GetString(payload, "text", GetString(query, "text", null));
                var name = GetString(payload, "name", GetString(query, "name", null));
                var index = GetInt(payload, "index", GetInt(query, "index", 0, 0, 200), 0, 200);
                var includeInactive = GetBool(payload, "includeInactive", GetBool(query, "includeInactive", false));
                return InvokeJson(() => GameApi.ClickUiButton(text, name, index, includeInactive));
            }

            if (path == "/api/v1/studio/start")
            {
                var studio = _plugin.Studio;
                if (studio == null)
                    return ApiResult.Error(503, "The studio service is not available.");
                return ApiResult.Ok(studio.Start());
            }

            if (path == "/api/v1/studio/stop")
            {
                var studio = _plugin.Studio;
                if (studio != null)
                    Task.Run(() => studio.Stop());
                return ApiResult.Ok(new JObject { ["stopping"] = true });
            }

            return ApiResult.Error(404, "Unknown endpoint.");
        }

        private ApiResult InvokeJson(Func<JObject> function)
        {
            return ApiResult.Ok(_dispatcher.Invoke(function, 15000));
        }

        private ApiResult RunLong(Action<Action<JObject>> starter, int timeoutSeconds)
        {
            JObject result = null;
            Exception error = null;
            using (var done = new ManualResetEventSlim(false))
            {
                _dispatcher.Post(() =>
                {
                    try
                    {
                        starter(value =>
                        {
                            result = value;
                            done.Set();
                        });
                    }
                    catch (Exception ex)
                    {
                        error = ex;
                        done.Set();
                    }
                });

                if (!done.Wait(TimeSpan.FromSeconds(Math.Max(1, timeoutSeconds))))
                    return ApiResult.Error(504, "Timed out while the game was applying the drawing.");
            }

            if (error != null)
            {
                var apiError = error as BridgeApiException;
                return ApiResult.Error(apiError?.StatusCode ?? 500, error.Message);
            }

            return ApiResult.Ok(result);
        }

        private void HandleWebSocket(TcpClient client, NetworkStream stream, HttpRequest request)
        {
            if (!request.Headers.TryGetValue("Sec-WebSocket-Key", out var key) || string.IsNullOrWhiteSpace(key))
            {
                WriteResponse(stream, ApiResult.Error(400, "Missing Sec-WebSocket-Key."));
                return;
            }

            var query = ParseQuery(request.RawQuery);
            if (!IsAuthorized(request, query))
            {
                WriteResponse(stream, ApiResult.Error(401, "Missing or invalid API token."));
                return;
            }

            string accept;
            using (var sha1 = SHA1.Create())
            {
                accept = Convert.ToBase64String(sha1.ComputeHash(Encoding.UTF8.GetBytes(key.Trim() + WebSocketGuid)));
            }

            var response = "HTTP/1.1 101 Switching Protocols\r\n" +
                           "Upgrade: websocket\r\n" +
                           "Connection: Upgrade\r\n" +
                           "Sec-WebSocket-Accept: " + accept + "\r\n" +
                           "Access-Control-Allow-Origin: *\r\n\r\n";
            var responseBytes = Encoding.ASCII.GetBytes(response);
            stream.Write(responseBytes, 0, responseBytes.Length);
            stream.Flush();

            var ws = new WebSocketClient(client, stream);
            lock (_clientsLock)
                _clients.Add(ws);

            try
            {
                JObject hello;
                try
                {
                    hello = _dispatcher.Invoke(GameApi.GetState, 10000);
                }
                catch (Exception ex)
                {
                    hello = new JObject { ["available"] = false, ["error"] = ex.Message };
                }

                ws.SendJson(new JObject
                {
                    ["type"] = "hello",
                    ["protocol"] = 1,
                    ["name"] = BridgePlugin.DisplayName,
                    ["version"] = BridgePlugin.Version,
                    ["data"] = hello
                });

                while (_running && ws.IsAlive)
                {
                    var message = ws.ReadMessage();
                    if (message == null)
                        break;
                    HandleWebSocketMessage(ws, message);
                }
            }
            catch (Exception ex)
            {
                if (_running)
                    _plugin.Log.LogDebug("WebSocket closed: " + ex.Message);
            }
            finally
            {
                lock (_clientsLock)
                    _clients.Remove(ws);
                ws.Dispose();
            }
        }

        private void HandleWebSocketMessage(WebSocketClient ws, string message)
        {
            JToken id = null;
            try
            {
                var root = JObject.Parse(message);
                id = root["id"];
                var type = root["type"]?.ToString() ?? "state";
                var payload = root["payload"] as JObject ?? root;

                ApiResult response;
                switch (type.ToLowerInvariant())
                {
                    case "state":
                    case "status":
                        response = InvokeJson(GameApi.GetState);
                        break;
                    case "lines":
                        response = InvokeJson(() => GameApi.GetLines(GetInt(payload, "limit", 10000, 0, 100000)));
                        break;
                    case "strokes":
                    case "draw":
                    {
                        var strokes = ParseStrokes(payload);
                        response = RunLong(completed => GameApi.StartApplyStrokes(
                            strokes,
                            GetBool(payload, "clear", false),
                            GetBool(payload, "force", false),
                            _config.StrokesPerFrame,
                            result =>
                            {
                                completed(result);
                                BroadcastApplied(result);
                            }), _config.LongOperationTimeoutSeconds);
                        break;
                    }
                    case "clear":
                        response = InvokeJson(() => GameApi.Clear(GetBool(payload, "force", false)));
                        break;
                    case "undo":
                        response = InvokeJson(GameApi.Undo);
                        break;
                    case "redo":
                        response = InvokeJson(GameApi.Redo);
                        break;
                    case "submit":
                        response = InvokeJson(GameApi.Submit);
                        break;
                    case "color":
                    {
                        var color = payload["color"]?.ToString();
                        if (string.IsNullOrWhiteSpace(color))
                            throw new BridgeApiException("Missing color.", 400);
                        response = InvokeJson(() => GameApi.SetColor(color));
                        break;
                    }
                    case "brush":
                        response = InvokeJson(() => GameApi.SetBrush(GetInt(payload, "size", DrawModule.BrushSize.Value, 1, 255)));
                        break;
                    case "tool":
                    {
                        var tool = payload["tool"]?.ToString();
                        if (string.IsNullOrWhiteSpace(tool))
                            throw new BridgeApiException("Missing tool.", 400);
                        response = InvokeJson(() => GameApi.ActivateTool(tool));
                        break;
                    }
                    case "ping":
                        ws.SendJson(new JObject { ["type"] = "pong", ["id"] = id, ["time"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() });
                        return;
                    default:
                        throw new BridgeApiException($"Unknown WebSocket command '{type}'.", 400);
                }

                var parsed = JObject.Parse(response.Body);
                parsed["type"] = "response";
                parsed["id"] = id;
                ws.SendJson(parsed);
            }
            catch (Exception ex)
            {
                var apiError = ex as BridgeApiException;
                ws.SendJson(new JObject
                {
                    ["type"] = "response",
                    ["id"] = id,
                    ["ok"] = false,
                    ["error"] = ex.Message,
                    ["status"] = apiError?.StatusCode ?? 500
                });
            }
        }

        private void BroadcastApplied(JObject result)
        {
            Broadcast(new JObject
            {
                ["type"] = "event",
                ["event"] = "strokes.applied",
                ["data"] = result
            });
            _lastBroadcastState = null;
        }

        private void Broadcast(JObject message)
        {
            var json = message.ToString(Formatting.None);
            List<WebSocketClient> clients;
            lock (_clientsLock)
                clients = new List<WebSocketClient>(_clients);

            foreach (var client in clients)
            {
                try
                {
                    client.SendText(json);
                }
                catch
                {
                    lock (_clientsLock)
                        _clients.Remove(client);
                    client.Dispose();
                }
            }
        }

        private bool IsAuthorized(HttpRequest request, Dictionary<string, string> query)
        {
            if (string.IsNullOrEmpty(_config.Token))
                return true;

            if (query.TryGetValue("token", out var queryToken) &&
                string.Equals(queryToken, _config.Token, StringComparison.Ordinal))
                return true;

            if (request.Headers.TryGetValue("X-Api-Key", out var apiKey) &&
                string.Equals(apiKey, _config.Token, StringComparison.Ordinal))
                return true;

            if (request.Headers.TryGetValue("Authorization", out var authorization) &&
                authorization != null &&
                authorization.StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase) &&
                string.Equals(authorization.Substring(7).Trim(), _config.Token, StringComparison.Ordinal))
                return true;

            return false;
        }

        private string ReadUi()
        {
            try
            {
                if (File.Exists(_uiPath))
                    return File.ReadAllText(_uiPath);
            }
            catch (Exception ex)
            {
                _plugin.Log.LogWarning("Could not read ui.html: " + ex.Message);
            }

            return WebUi.FallbackHtml;
        }

        private static JObject ParseJsonBody(HttpRequest request, bool allowEmpty = false)
        {
            if (request.Body == null || request.Body.Length == 0)
            {
                if (allowEmpty)
                    return new JObject();
                throw new BridgeApiException("Request body is empty. Send JSON.", 400);
            }

            try
            {
                var text = Encoding.UTF8.GetString(request.Body).Trim();
                if (text.Length == 0)
                {
                    if (allowEmpty)
                        return new JObject();
                    throw new BridgeApiException("Request body is empty. Send JSON.", 400);
                }
                return JObject.Parse(text);
            }
            catch (JsonException ex)
            {
                throw new BridgeApiException("Invalid JSON: " + ex.Message, 400);
            }
        }

        private static List<GameApi.StrokeSpec> ParseStrokes(JToken payload)
        {
            JToken strokesToken;
            if (payload is JArray array)
                strokesToken = array;
            else if (payload is JObject obj && obj["strokes"] != null)
                strokesToken = obj["strokes"];
            else if (payload is JObject single && single["points"] != null)
                strokesToken = new JArray(single);
            else
                throw new BridgeApiException("Missing 'strokes' array.", 400);

            if (!(strokesToken is JArray strokeArray))
                throw new BridgeApiException("'strokes' must be an array.", 400);

            var result = new List<GameApi.StrokeSpec>();
            foreach (var token in strokeArray)
            {
                if (!(token is JObject stroke))
                    throw new BridgeApiException("Each stroke must be an object.", 400);

                var spec = new GameApi.StrokeSpec
                {
                    Type = ReadStrokeType(stroke["type"]),
                    Color = ReadColor(stroke["color"]),
                    Size = ReadNullableInt(stroke["size"] ?? stroke["brushSize"]),
                    Space = stroke["space"]?.ToString() ?? "normalized",
                    Layer = ReadNullableInt(stroke["layer"]),
                    VLayer = ReadNullableInt(stroke["vlayer"])
                };

                var pointsToken = stroke["points"] as JArray;
                if (pointsToken == null)
                    throw new BridgeApiException("Stroke is missing a 'points' array.", 400);

                foreach (var pointToken in pointsToken)
                {
                    float x;
                    float y;
                    if (pointToken is JArray pointArray && pointArray.Count >= 2)
                    {
                        x = pointArray[0].Value<float>();
                        y = pointArray[1].Value<float>();
                    }
                    else if (pointToken is JObject pointObject)
                    {
                        x = pointObject["x"]?.Value<float>() ?? 0f;
                        y = pointObject["y"]?.Value<float>() ?? 0f;
                    }
                    else
                    {
                        throw new BridgeApiException("Every point must be [x,y] or {x,y}.", 400);
                    }

                    spec.Points.Add(new[] { x, y });
                }

                result.Add(spec);
            }

            return result;
        }

        private static string ReadStrokeType(JToken token)
        {
            if (token == null || token.Type == JTokenType.Null)
                return "line";

            if (token.Type == JTokenType.Integer)
            {
                switch (token.Value<int>())
                {
                    case 1: return "disc";
                    case 3: return "fill";
                    default: return "line";
                }
            }

            return token.ToString();
        }

        private static string ReadColor(JToken token)
        {
            if (token == null || token.Type == JTokenType.Null)
                return null;

            if (token.Type == JTokenType.String)
                return token.ToString();

            if (token is JArray array && array.Count >= 3)
            {
                int r = array[0].Value<int>();
                int g = array[1].Value<int>();
                int b = array[2].Value<int>();
                if (array.All(v => v.Type == JTokenType.Float || v.Type == JTokenType.Integer))
                {
                    var max = Math.Max(r, Math.Max(g, b));
                    if (max <= 1)
                    {
                        r = (int)Math.Round(array[0].Value<float>() * 255f);
                        g = (int)Math.Round(array[1].Value<float>() * 255f);
                        b = (int)Math.Round(array[2].Value<float>() * 255f);
                    }
                }
                return "#" + (ClampByte(r) << 16 | ClampByte(g) << 8 | ClampByte(b)).ToString("X6");
            }

            if (token is JObject obj)
            {
                var r = MathfRoundToByte(obj["r"]?.Value<float>() ?? 0f);
                var g = MathfRoundToByte(obj["g"]?.Value<float>() ?? 0f);
                var b = MathfRoundToByte(obj["b"]?.Value<float>() ?? 0f);
                return "#" + (r << 16 | g << 8 | b).ToString("X6");
            }

            return token.ToString();
        }

        private static int ClampByte(int value)
        {
            return Math.Max(0, Math.Min(255, value));
        }

        private static int MathfRoundToByte(float value)
        {
            if (value <= 1f)
                value *= 255f;
            return ClampByte((int)Math.Round(value));
        }

        private static int? ReadNullableInt(JToken token)
        {
            if (token == null || token.Type == JTokenType.Null)
                return null;
            try
            {
                return token.Value<int>();
            }
            catch
            {
                return null;
            }
        }

        private static byte[] ExtractImageBytes(HttpRequest request)
        {
            if (request.Body == null || request.Body.Length == 0)
                throw new BridgeApiException("Empty image body. POST a raw PNG/JPEG.", 400);

            var contentType = request.Headers.TryGetValue("Content-Type", out var ct) ? ct : string.Empty;
            if (contentType.IndexOf("json", StringComparison.OrdinalIgnoreCase) >= 0 ||
                (request.Body.Length > 0 && request.Body[0] == (byte)'{'))
            {
                try
                {
                    var json = JObject.Parse(Encoding.UTF8.GetString(request.Body));
                    var base64 = json["image"]?.ToString() ?? json["base64"]?.ToString();
                    if (string.IsNullOrWhiteSpace(base64))
                        throw new BridgeApiException("JSON image body needs an 'image' base64 field.", 400);
                    var comma = base64.IndexOf(',');
                    if (comma >= 0)
                        base64 = base64.Substring(comma + 1);
                    return Convert.FromBase64String(base64);
                }
                catch (FormatException)
                {
                    throw new BridgeApiException("Invalid base64 image data.", 400);
                }
            }

            return request.Body;
        }

        private static void WriteResponse(NetworkStream stream, ApiResult response)
        {
            var body = Encoding.UTF8.GetBytes(response.Body ?? string.Empty);
            var headers = new StringBuilder();
            headers.Append("HTTP/1.1 ").Append(response.StatusCode).Append(' ').Append(StatusText(response.StatusCode)).Append("\r\n");
            headers.Append("Content-Type: ").Append(response.ContentType).Append("\r\n");
            headers.Append("Content-Length: ").Append(body.Length).Append("\r\n");
            headers.Append("Access-Control-Allow-Origin: *\r\n");
            headers.Append("Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n");
            headers.Append("Access-Control-Allow-Headers: Content-Type, Authorization, X-Api-Key\r\n");
            headers.Append("Cache-Control: no-store\r\n");
            headers.Append("Connection: close\r\n\r\n");

            var headerBytes = Encoding.ASCII.GetBytes(headers.ToString());
            stream.Write(headerBytes, 0, headerBytes.Length);
            if (body.Length > 0)
                stream.Write(body, 0, body.Length);
            stream.Flush();
        }

        private static string StatusText(int status)
        {
            switch (status)
            {
                case 200: return "OK";
                case 204: return "No Content";
                case 400: return "Bad Request";
                case 401: return "Unauthorized";
                case 404: return "Not Found";
                case 405: return "Method Not Allowed";
                case 409: return "Conflict";
                case 500: return "Internal Server Error";
                case 504: return "Gateway Timeout";
                default: return "Status";
            }
        }

        private static string NormalizePath(string path)
        {
            if (string.IsNullOrEmpty(path))
                return "/";
            if (path.Length > 1 && path.EndsWith("/", StringComparison.Ordinal))
                return path.TrimEnd('/');
            return path;
        }

        private static Dictionary<string, string> ParseQuery(string rawQuery)
        {
            var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            if (string.IsNullOrEmpty(rawQuery))
                return result;

            if (rawQuery.StartsWith("?", StringComparison.Ordinal))
                rawQuery = rawQuery.Substring(1);

            foreach (var pair in rawQuery.Split(new[] { '&' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var index = pair.IndexOf('=');
                var key = index >= 0 ? pair.Substring(0, index) : pair;
                var value = index >= 0 ? pair.Substring(index + 1) : string.Empty;
                try
                {
                    key = Uri.UnescapeDataString(key.Replace("+", " "));
                    value = Uri.UnescapeDataString(value.Replace("+", " "));
                }
                catch { }
                result[key] = value;
            }

            return result;
        }

        private static int GetInt(Dictionary<string, string> query, string key, int fallback, int minimum, int maximum)
        {
            if (!query.TryGetValue(key, out var raw))
                return fallback;
            return int.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out var value)
                ? Math.Max(minimum, Math.Min(maximum, value))
                : fallback;
        }

        private static float GetFloat(Dictionary<string, string> query, string key, float fallback, float minimum, float maximum)
        {
            if (!query.TryGetValue(key, out var raw))
                return fallback;
            return float.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out var value)
                ? Math.Max(minimum, Math.Min(maximum, value))
                : fallback;
        }

        private static bool GetBool(Dictionary<string, string> query, string key, bool fallback)
        {
            if (!query.TryGetValue(key, out var raw))
                return fallback;
            return ParseBool(raw, fallback);
        }

        private static bool GetBool(JObject obj, string key, bool fallback)
        {
            var token = obj?[key];
            if (token == null || token.Type == JTokenType.Null)
                return fallback;
            if (token.Type == JTokenType.Boolean)
                return token.Value<bool>();
            return ParseBool(token.ToString(), fallback);
        }

        private static bool ParseBool(string raw, bool fallback)
        {
            if (bool.TryParse(raw, out var value))
                return value;
            switch ((raw ?? string.Empty).ToLowerInvariant())
            {
                case "1":
                case "yes":
                case "on":
                    return true;
                case "0":
                case "no":
                case "off":
                    return false;
                default:
                    return fallback;
            }
        }

        private static int GetInt(JObject obj, string key, int fallback, int minimum, int maximum)
        {
            var token = obj?[key];
            if (token == null || token.Type == JTokenType.Null)
                return fallback;
            try
            {
                return Math.Max(minimum, Math.Min(maximum, token.Value<int>()));
            }
            catch
            {
                return fallback;
            }
        }

        private static string GetString(Dictionary<string, string> query, string key, string fallback)
        {
            return query.TryGetValue(key, out var value) ? value : fallback;
        }

        private static string GetString(JObject obj, string key, string fallback)
        {
            return obj?[key]?.ToString() ?? fallback;
        }
    }
}

using System;

namespace DrawGuessBridge
{
    internal static class WebUi
    {
        public const string FallbackHtml = @"<!doctype html>
<html lang=""zh-CN"">
<head>
<meta charset=""utf-8"">
<meta name=""viewport"" content=""width=device-width, initial-scale=1"">
<title>Draw &amp; Guess Bridge</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #0f1115; color: #e6e8ee;
         font: 15px/1.6 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
  main { width: min(640px, calc(100vw - 32px)); border: 1px solid #2a2f3a; border-radius: 16px;
         padding: 24px; background: #171a21; }
  h1 { margin: 0 0 8px; font-size: 20px; }
  code { background: #0f1115; padding: 2px 6px; border-radius: 6px; }
  a { color: #7ab7ff; }
  .muted { color: #8d94a5; }
</style>
</head>
<body><main>
  <h1>Draw &amp; Guess Bridge</h1>
  <p>插件已运行，但 <code>ui.html</code> 没有和 DLL 放在同一目录。API 仍然可用：</p>
  <p><a href=""/api/v1/state"">/api/v1/state</a> · <a href=""/api/v1/lines"">/api/v1/lines</a> · <code>ws://127.0.0.1:8765/ws</code></p>
  <p class=""muted"">把仓库里的 ui.html 复制到 BepInEx/plugins/DrawGuessBridge/ 即可恢复完整控制台。</p>
</main></body></html>";
    }
}

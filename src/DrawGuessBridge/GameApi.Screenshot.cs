using System;
using System.Collections;
using System.IO;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace DrawGuessBridge
{
    internal static partial class GameApi
    {
        /// <summary>Captures the game screen at the end of the frame and writes a PNG to `path`.</summary>
        internal static void StartScreenshot(string path, Action<JObject> completed)
        {
            if (string.IsNullOrWhiteSpace(path))
                throw new BridgeApiException("Missing 'path' for the screenshot file.", 400);
            BridgePlugin.Instance.StartCoroutine(ScreenshotRoutine(path, completed));
        }

        private static IEnumerator ScreenshotRoutine(string path, Action<JObject> completed)
        {
            yield return new WaitForEndOfFrame();
            var result = new JObject();
            try
            {
                var texture = ScreenCapture.CaptureScreenshotAsTexture();
                var bytes = ImageConversion.EncodeToPNG(texture);
                var directory = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(directory))
                    Directory.CreateDirectory(directory);
                File.WriteAllBytes(path, bytes);
                result["path"] = path;
                result["width"] = texture.width;
                result["height"] = texture.height;
                UnityEngine.Object.Destroy(texture);
            }
            catch (Exception ex)
            {
                result["error"] = ex.Message;
            }
            completed(result);
        }
    }
}

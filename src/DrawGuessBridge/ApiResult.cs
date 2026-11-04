using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace DrawGuessBridge
{
    internal sealed class ApiResult
    {
        public int StatusCode { get; private set; }
        public string Body { get; private set; }
        public string ContentType { get; private set; }

        private ApiResult(int statusCode, string body, string contentType)
        {
            StatusCode = statusCode;
            Body = body ?? string.Empty;
            ContentType = contentType;
        }

        public static ApiResult Ok(object result)
        {
            var payload = new JObject
            {
                ["ok"] = true,
                ["result"] = result == null ? JValue.CreateNull() : JToken.FromObject(result)
            };
            return Json(200, payload);
        }

        public static ApiResult Error(int statusCode, string message)
        {
            var payload = new JObject
            {
                ["ok"] = false,
                ["error"] = message ?? "Unknown error"
            };
            return Json(statusCode, payload);
        }

        public static ApiResult Json(int statusCode, JToken payload)
        {
            return new ApiResult(statusCode, payload.ToString(Formatting.None), "application/json; charset=utf-8");
        }

        public static ApiResult Html(string html)
        {
            return new ApiResult(200, html ?? string.Empty, "text/html; charset=utf-8");
        }

        public static ApiResult NoContent()
        {
            return new ApiResult(204, string.Empty, "text/plain; charset=utf-8");
        }

        public static ApiResult Plain(int statusCode, string text)
        {
            return new ApiResult(statusCode, text, "text/plain; charset=utf-8");
        }
    }
}

using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Sockets;
using System.Text;

namespace DrawGuessBridge
{
    internal sealed class HttpRequest
    {
        public string Method { get; private set; }
        public string Path { get; private set; }
        public string RawQuery { get; private set; }
        public string Version { get; private set; }
        public Dictionary<string, string> Headers { get; } =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        public byte[] Body { get; private set; } = Array.Empty<byte>();

        public static HttpRequest Read(NetworkStream stream, int maxHeaderBytes, int maxBodyBytes)
        {
            var headerBuffer = new MemoryStream();
            var tail = new byte[4];
            var tailCount = 0;
            var value = 0;

            while (true)
            {
                value = stream.ReadByte();
                if (value < 0)
                    return null;

                headerBuffer.WriteByte((byte)value);
                if (headerBuffer.Length > maxHeaderBytes)
                    throw new IOException("HTTP headers are too large.");

                tail[tailCount % 4] = (byte)value;
                tailCount++;
                if (tailCount >= 4 && tail[(tailCount - 4) % 4] == '\r' &&
                    tail[(tailCount - 3) % 4] == '\n' &&
                    tail[(tailCount - 2) % 4] == '\r' &&
                    tail[(tailCount - 1) % 4] == '\n')
                {
                    break;
                }
            }

            var headerText = Encoding.UTF8.GetString(headerBuffer.ToArray());
            var lines = headerText.Split(new[] { "\r\n" }, StringSplitOptions.None);
            if (lines.Length == 0 || string.IsNullOrWhiteSpace(lines[0]))
                return null;

            var requestLine = lines[0].Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
            if (requestLine.Length < 2)
                throw new IOException("Malformed HTTP request line.");

            var request = new HttpRequest
            {
                Method = requestLine[0].ToUpperInvariant(),
                Version = requestLine.Length > 2 ? requestLine[2] : "HTTP/1.1"
            };

            var target = requestLine[1];
            if (target.StartsWith("http://", StringComparison.OrdinalIgnoreCase) ||
                target.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
            {
                var uri = new Uri(target);
                request.Path = uri.AbsolutePath;
                request.RawQuery = uri.Query;
            }
            else
            {
                var queryIndex = target.IndexOf('?');
                request.Path = queryIndex >= 0 ? target.Substring(0, queryIndex) : target;
                request.RawQuery = queryIndex >= 0 ? target.Substring(queryIndex) : string.Empty;
            }

            for (var i = 1; i < lines.Length; i++)
            {
                var line = lines[i];
                if (string.IsNullOrEmpty(line))
                    break;
                var colon = line.IndexOf(':');
                if (colon <= 0)
                    continue;
                var name = line.Substring(0, colon).Trim();
                var headerValue = line.Substring(colon + 1).Trim();
                request.Headers[name] = headerValue;
            }

            if (request.Headers.TryGetValue("Transfer-Encoding", out var transferEncoding) &&
                transferEncoding.IndexOf("chunked", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                throw new IOException("Chunked request bodies are not supported. Send Content-Length.");
            }

            var contentLength = 0;
            if (request.Headers.TryGetValue("Content-Length", out var lengthText) &&
                !string.IsNullOrWhiteSpace(lengthText))
            {
                if (!int.TryParse(lengthText, out contentLength) || contentLength < 0)
                    throw new IOException("Invalid Content-Length.");
            }

            if (contentLength > maxBodyBytes)
                throw new IOException("Request body is too large.");

            if (contentLength > 0)
            {
                var body = new byte[contentLength];
                var offset = 0;
                while (offset < contentLength)
                {
                    var read = stream.Read(body, offset, contentLength - offset);
                    if (read <= 0)
                        throw new IOException("Unexpected end of request body.");
                    offset += read;
                }
                request.Body = body;
            }

            return request;
        }
    }
}

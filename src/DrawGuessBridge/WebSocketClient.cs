using System;
using System.IO;
using System.Net.Sockets;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace DrawGuessBridge
{
    internal sealed class WebSocketClient : IDisposable
    {
        private const int MaxMessageBytes = 16 * 1024 * 1024;

        private readonly TcpClient _client;
        private readonly NetworkStream _stream;
        private readonly object _writeLock = new object();
        private readonly object _readLock = new object();
        private volatile bool _alive = true;
        private MemoryStream _fragments;
        private int _fragmentOpcode;

        public bool IsAlive => _alive;

        public WebSocketClient(TcpClient client, NetworkStream stream)
        {
            _client = client;
            _stream = stream;
        }

        public string ReadMessage()
        {
            lock (_readLock)
            {
                while (_alive)
                {
                    var first = ReadByteOrMinusOne();
                    if (first < 0)
                        return null;

                    var fin = (first & 0x80) != 0;
                    var opcode = first & 0x0F;
                    var second = ReadByteOrMinusOne();
                    if (second < 0)
                        return null;

                    var masked = (second & 0x80) != 0;
                    long payloadLength = second & 0x7F;

                    if (payloadLength == 126)
                    {
                        var lengthBytes = ReadExact(2);
                        if (lengthBytes == null)
                            return null;
                        payloadLength = (lengthBytes[0] << 8) | lengthBytes[1];
                    }
                    else if (payloadLength == 127)
                    {
                        var lengthBytes = ReadExact(8);
                        if (lengthBytes == null)
                            return null;
                        payloadLength = 0;
                        for (var i = 0; i < 8; i++)
                            payloadLength = (payloadLength << 8) | lengthBytes[i];
                    }

                    if (payloadLength < 0 || payloadLength > MaxMessageBytes)
                    {
                        _alive = false;
                        return null;
                    }

                    var mask = masked ? ReadExact(4) : null;
                    if (masked && mask == null)
                        return null;

                    var payload = ReadExact((int)payloadLength);
                    if (payload == null)
                        return null;

                    if (masked)
                    {
                        for (var i = 0; i < payload.Length; i++)
                            payload[i] ^= mask[i % 4];
                    }

                    if (opcode == 0x8)
                    {
                        TrySendFrame(0x8, payload);
                        _alive = false;
                        return null;
                    }

                    if (opcode == 0x9)
                    {
                        TrySendFrame(0xA, payload);
                        continue;
                    }

                    if (opcode == 0xA)
                        continue;

                    if (opcode == 0x0)
                    {
                        if (_fragments == null)
                            continue;
                        _fragments.Write(payload, 0, payload.Length);
                        if (_fragments.Length > MaxMessageBytes)
                        {
                            _alive = false;
                            return null;
                        }
                        if (fin)
                        {
                            var full = _fragments.ToArray();
                            var messageOpcodeFinal = _fragmentOpcode;
                            _fragments.Dispose();
                            _fragments = null;
                            _fragmentOpcode = 0;
                            if (messageOpcodeFinal == 0x1)
                                return Encoding.UTF8.GetString(full);
                            continue;
                        }
                        continue;
                    }

                    if (opcode == 0x1 || opcode == 0x2)
                    {
                        if (!fin)
                        {
                            _fragments?.Dispose();
                            _fragments = new MemoryStream();
                            _fragments.Write(payload, 0, payload.Length);
                            _fragmentOpcode = opcode;
                            continue;
                        }

                        if (opcode == 0x1)
                            return Encoding.UTF8.GetString(payload);

                        // Binary frames are not part of the protocol; ignore them.
                        continue;
                    }

                    // Unknown opcode: ignore.
                }

                return null;
            }
        }

        public void SendJson(JObject payload)
        {
            SendText(payload.ToString(Formatting.None));
        }

        public void SendText(string text)
        {
            if (text == null)
                return;
            var payload = Encoding.UTF8.GetBytes(text);
            SendFrame(0x1, payload);
        }

        private void SendFrame(int opcode, byte[] payload)
        {
            lock (_writeLock)
            {
                if (!_alive)
                    return;

                var headerLength = payload.Length <= 125 ? 2 : (payload.Length <= ushort.MaxValue ? 4 : 10);
                var frame = new byte[headerLength + payload.Length];
                frame[0] = (byte)(0x80 | (opcode & 0x0F));

                if (payload.Length <= 125)
                {
                    frame[1] = (byte)payload.Length;
                }
                else if (payload.Length <= ushort.MaxValue)
                {
                    frame[1] = 126;
                    frame[2] = (byte)((payload.Length >> 8) & 0xFF);
                    frame[3] = (byte)(payload.Length & 0xFF);
                }
                else
                {
                    frame[1] = 127;
                    var length = (long)payload.Length;
                    for (var i = 0; i < 8; i++)
                        frame[2 + i] = (byte)((length >> (56 - i * 8)) & 0xFF);
                }

                Buffer.BlockCopy(payload, 0, frame, headerLength, payload.Length);
                _stream.Write(frame, 0, frame.Length);
                _stream.Flush();
            }
        }

        private void TrySendFrame(int opcode, byte[] payload)
        {
            try { SendFrame(opcode, payload); }
            catch { _alive = false; }
        }

        private int ReadByteOrMinusOne()
        {
            try
            {
                return _stream.ReadByte();
            }
            catch
            {
                _alive = false;
                return -1;
            }
        }

        private byte[] ReadExact(int count)
        {
            if (count == 0)
                return Array.Empty<byte>();

            var buffer = new byte[count];
            var offset = 0;
            while (offset < count)
            {
                int read;
                try
                {
                    read = _stream.Read(buffer, offset, count - offset);
                }
                catch
                {
                    _alive = false;
                    return null;
                }

                if (read <= 0)
                {
                    _alive = false;
                    return null;
                }

                offset += read;
            }

            return buffer;
        }

        public void Dispose()
        {
            _alive = false;
            try { _fragments?.Dispose(); } catch { }
            _fragments = null;
            try { _stream?.Close(); } catch { }
            try { _client?.Close(); } catch { }
        }
    }
}

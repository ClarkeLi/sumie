using System;
using System.Collections.Concurrent;
using System.Threading;

namespace DrawGuessBridge
{
    /// <summary>
    /// Executes work on Unity's main thread. Unity objects may only be touched there.
    /// </summary>
    internal sealed class MainThreadDispatcher
    {
        private readonly ConcurrentQueue<Action> _queue = new ConcurrentQueue<Action>();
        private readonly int _mainThreadId;

        public MainThreadDispatcher()
        {
            _mainThreadId = Thread.CurrentThread.ManagedThreadId;
        }

        public bool IsMainThread => Thread.CurrentThread.ManagedThreadId == _mainThreadId;

        public void Post(Action action)
        {
            if (action == null)
                return;

            if (IsMainThread)
            {
                action();
                return;
            }

            _queue.Enqueue(action);
        }

        public void Pump(int maximum = 256)
        {
            for (var i = 0; i < maximum; i++)
            {
                if (!_queue.TryDequeue(out var action))
                    return;

                try
                {
                    action();
                }
                catch (Exception ex)
                {
                    BridgePlugin.Instance?.Log.LogError($"Main-thread action failed: {ex}");
                }
            }
        }

        public T Invoke<T>(Func<T> function, int timeoutMilliseconds)
        {
            if (IsMainThread)
                return function();

            T result = default;
            Exception error = null;
            using (var done = new ManualResetEventSlim(false))
            {
                _queue.Enqueue(() =>
                {
                    try
                    {
                        result = function();
                    }
                    catch (Exception ex)
                    {
                        error = ex;
                    }
                    finally
                    {
                        done.Set();
                    }
                });

                if (!done.Wait(timeoutMilliseconds))
                    throw new TimeoutException("The game did not answer the main-thread request in time.");
            }

            if (error != null)
                throw new BridgeApiException(error.Message, error);

            return result;
        }
    }

    internal sealed class BridgeApiException : Exception
    {
        public int StatusCode { get; }

        public BridgeApiException(string message, int statusCode = 500) : base(message)
        {
            StatusCode = statusCode;
        }

        public BridgeApiException(string message, Exception inner) : base(message, inner)
        {
            StatusCode = 500;
        }
    }
}

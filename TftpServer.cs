using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace HikvisionTftpServer
{
    public class TftpServer
    {
        private readonly int _tftpPort;
        private readonly int _handshakePort = 9978;
        private readonly string _rootDirectory;
        private UdpClient? _tftpListener;
        private UdpClient? _handshakeListener;
        private CancellationTokenSource? _cts;
        private static readonly ConcurrentDictionary<string, byte> ActiveTransfers = new();
        private static readonly ConcurrentDictionary<string, DateTime> RecentlyFlashed = new();
        private static readonly TimeSpan RecentlyFlashedDuration = TimeSpan.FromMinutes(10);
        public TftpServer(string rootDirectory, int port = 69)
        {
            _rootDirectory = Path.GetFullPath(rootDirectory);
            _tftpPort = port;
        }
        public void Start()
        {
            _cts = new CancellationTokenSource();
            _tftpListener = new UdpClient();
            _tftpListener.Client.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
            _tftpListener.Client.Bind(new IPEndPoint(IPAddress.Any, _tftpPort));
            _handshakeListener = new UdpClient();
            _handshakeListener.Client.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
            _handshakeListener.Client.Bind(new IPEndPoint(IPAddress.Any, _handshakePort));
            LogInfo($"TFTP Server started on port {_tftpPort}.");
            LogHandshake($"Handshake listener (SWKH) active on port {_handshakePort}.");
            LogInfo("Waiting for camera... TURN ON CAMERA POWER NOW!");
            _ = ListenTftpAsync(_cts.Token);
            _ = ListenHandshakeAsync(_cts.Token);
            _ = CleanupRecentlyFlashedListAsync(_cts.Token);
        }
        public void Stop()
        {
            _cts?.Cancel();
            _tftpListener?.Dispose();
            _handshakeListener?.Dispose();
        }
        private async Task CleanupRecentlyFlashedListAsync(CancellationToken ct)
        {
            while (!ct.IsCancellationRequested)
            {
                await Task.Delay(TimeSpan.FromSeconds(30), ct);
                var now = DateTime.UtcNow;
                foreach (var entry in RecentlyFlashed)
                {
                    if (now - entry.Value > RecentlyFlashedDuration)
                    {
                        if (RecentlyFlashed.TryRemove(entry.Key, out _))
                        {
                            LogInfo($"[{entry.Key}] Removed from 'recently flashed' list.");
                        }
                    }
                }
            }
        }
        private async Task ListenHandshakeAsync(CancellationToken ct)
        {
            if (_handshakeListener == null) return;

            while (!ct.IsCancellationRequested)
            {
                try
                {
                    var result = await _handshakeListener.ReceiveAsync(ct);
                    byte[] request = result.Buffer;
                    string clientIp = result.RemoteEndPoint.Address.ToString();
                    if (request.Length >= 4 && request[0] == 0x53 && request[1] == 0x57 && request[2] == 0x4B && request[3] == 0x48)
                    {
                        if (RecentlyFlashed.ContainsKey(clientIp))
                        {
                            LogHandshake($"[SWKH] Knock from {clientIp} detected, but ignored (recently flashed).\nWait 10-20 seconds, then power cycle the camera. It should then boot up with its default IP address.");
                        }
                        else
                        {
                            LogHandshake($"[SWKH] Knock detected from {clientIp}. Echo sent.");
                            await _handshakeListener.SendAsync(request, request.Length, result.RemoteEndPoint);
                        }
                    }
                }
                catch (Exception ex) when (ex is ObjectDisposedException || ex is SocketException || ct.IsCancellationRequested)
                {
                    break;
                }
                catch (Exception ex)
                {
                    LogError($"Handshake error: {ex.Message}");
                }
            }
        }
        private async Task ListenTftpAsync(CancellationToken ct)
        {
            if (_tftpListener == null) return;
            while (!ct.IsCancellationRequested)
            {
                try
                {
                    var result = await _tftpListener.ReceiveAsync(ct);
                    _ = Task.Run(() => HandleTftpRequestAsync(result.Buffer, result.RemoteEndPoint), ct);
                }
                catch (Exception ex) when (ex is ObjectDisposedException || ex is SocketException || ct.IsCancellationRequested)
                {
                    break;
                }
                catch (Exception ex)
                {
                    LogError($"TFTP listen error: {ex.Message}");
                }
            }
        }

        private async Task HandleTftpRequestAsync(byte[] requestPacket, IPEndPoint clientEndPoint)
        {
            try
            {
                if (requestPacket.Length < 4) return;
                ushort opcode = (ushort)((requestPacket[0] << 8) | requestPacket[1]);
                if (opcode != 1) return;
                int index = 2;
                string filename = ReadNullTerminatedString(requestPacket, ref index);
                string clientIp = clientEndPoint.Address.ToString();
                if (ActiveTransfers.ContainsKey(clientIp))
                {
                    return;
                }
                if (RecentlyFlashed.ContainsKey(clientIp))
                {
                    LogWarning($"[TFTP] File request from {clientIp} ignored (recently flashed).\nWait 10-20 seconds, then power cycle the camera. It should then boot up with its default IP address.");
                    return;
                }
                LogInfo($"[TFTP] Camera {clientEndPoint} requested file: '{filename}'");
                string filePath = Path.Combine(_rootDirectory, filename);
                if (!File.Exists(filePath))
                {
                    LogWarning($"[TFTP] File '{filename}' not found. Replying error code to redirect camera.");
                    await SendTftpErrorAsync(clientEndPoint, 0, "No such file or directory");
                    return;
                }
                ActiveTransfers[clientIp] = 0;
                var session = new TftpTransferSession(clientEndPoint, filePath, () =>
                {
                    if (ActiveTransfers.TryRemove(clientIp, out _))
                    {
                        RecentlyFlashed[clientIp] = DateTime.UtcNow;
                    }
                });
                _ = Task.Run(() => session.StartTransferAsync());
            }
            catch (Exception ex)
            {
                LogError($"Critical error in TFTP handler: {ex.Message}");
            }
        }

        private async Task SendTftpErrorAsync(IPEndPoint clientEndPoint, ushort errorCode, string errorMessage)
        {
            using var errorClient = new UdpClient();
            byte[] msgBytes = Encoding.ASCII.GetBytes(errorMessage);
            byte[] packet = new byte[4 + msgBytes.Length + 1];
            packet[0] = 0; packet[1] = 5;
            packet[2] = (byte)(errorCode >> 8);
            packet[3] = (byte)(errorCode & 0xFF);
            Array.Copy(msgBytes, 0, packet, 4, msgBytes.Length);
            packet[^1] = 0;
            await errorClient.SendAsync(packet, packet.Length, clientEndPoint);
        }
        private static string ReadNullTerminatedString(byte[] data, ref int index)
        {
            if (index >= data.Length) return string.Empty;
            int start = index;
            while (index < data.Length && data[index] != 0)
            {
                index++;
            }
            string str = Encoding.ASCII.GetString(data, start, index - start);
            if (index < data.Length && data[index] == 0)
            {
                index++;
            }
            return str;
        }

        public static void LogInfo(string message)
        {
            Console.ForegroundColor = ConsoleColor.Green;
            Console.WriteLine($"[{DateTime.Now:HH:mm:ss}] [INFO] {message}");
            Console.ResetColor();
        }
        public static void LogHandshake(string message)
        {
            Console.ForegroundColor = ConsoleColor.Cyan;
            Console.WriteLine($"[{DateTime.Now:HH:mm:ss}] [HANDSHAKE] {message}");
            Console.ResetColor();
        }
        public static void LogWarning(string message)
        {
            Console.ForegroundColor = ConsoleColor.Yellow;
            Console.WriteLine($"[{DateTime.Now:HH:mm:ss}] [WARN] {message}");
            Console.ResetColor();
        }
        public static void LogError(string message)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"[{DateTime.Now:HH:mm:ss}] [ERROR] {message}");
            Console.ResetColor();
        }
    }
    internal class TftpTransferSession
    {
        private readonly IPEndPoint _clientEndPoint;
        private readonly string _filePath;
        private readonly UdpClient _udpClient;
        private readonly Action _onComplete;
        private const int BlockSize = 512;
        private const int MaxRetries = 5;
        private static readonly TimeSpan Timeout = TimeSpan.FromMilliseconds(1500);
        private static readonly object ConsoleLock = new();

        public TftpTransferSession(IPEndPoint clientEndPoint, string filePath, Action onComplete)
        {
            _clientEndPoint = clientEndPoint;
            _filePath = filePath;
            _onComplete = onComplete;
            _udpClient = new UdpClient(new IPEndPoint(IPAddress.Any, 0));
        }

        public async Task StartTransferAsync()
        {
            try
            {
                byte[] fileBytes = await File.ReadAllBytesAsync(_filePath);
                int totalBytes = fileBytes.Length;
                int bytesSent = 0;
                ushort blockNumber = 1;
                var fileName = Path.GetFileName(_filePath);
                TftpServer.LogInfo($"[{_clientEndPoint}] Uploading '{fileName}' ({totalBytes / 1024.0 / 1024.0:F2} MB)...");
                var startTime = DateTime.UtcNow;
                int lastPercent = -1;
                while (bytesSent < totalBytes)
                {
                    int currentBlockSize = Math.Min(BlockSize, totalBytes - bytesSent);
                    byte[] dataPacket = new byte[4 + currentBlockSize];
                    dataPacket[0] = 0; dataPacket[1] = 3;
                    dataPacket[2] = (byte)(blockNumber >> 8);
                    dataPacket[3] = (byte)(blockNumber & 0xFF);
                    Array.Copy(fileBytes, bytesSent, dataPacket, 4, currentBlockSize);
                    bool ackReceived = false;
                    int retries = 0;
                    while (!ackReceived)
                    {
                        await _udpClient.SendAsync(dataPacket, dataPacket.Length, _clientEndPoint);
                        try
                        {
                            var receiveResult = await _udpClient.ReceiveAsync().WaitAsync(Timeout);
                            byte[] ackPacket = receiveResult.Buffer;

                            if (ackPacket.Length >= 4 && ackPacket[0] == 0 && ackPacket[1] == 4)
                            {
                                ushort ackBlock = (ushort)((ackPacket[2] << 8) | ackPacket[3]);

                                if (ackBlock == blockNumber)
                                {
                                    ackReceived = true;
                                    bytesSent += currentBlockSize;
                                    int percent = (int)((double)bytesSent / totalBytes * 100);
                                    if (percent != lastPercent || bytesSent >= totalBytes)
                                    {
                                        lastPercent = percent;
                                        DrawProgressBar(percent, bytesSent, totalBytes);
                                    }

                                    unchecked { blockNumber++; }
                                }
                            }
                        }
                        catch (TimeoutException)
                        {
                            retries++;
                            if (retries >= MaxRetries)
                            {
                                Console.WriteLine();
                                TftpServer.LogError($"[{_clientEndPoint}] No ACK on block {blockNumber}. Transfer failed.");
                                return;
                            }
                        }
                        catch (Exception ex) when (ex is ObjectDisposedException || ex is SocketException)
                        {
                            return;
                        }
                    }
                }

                var elapsed = DateTime.UtcNow - startTime;
                double speed = (totalBytes / 1024.0 / 1024.0) / elapsed.TotalSeconds;
                Console.ForegroundColor = ConsoleColor.Green;
                Console.WriteLine($"\n[{_clientEndPoint}] Success: '{fileName}' uploaded in {elapsed.TotalSeconds:F1}s! Speed: {speed:F2} MB/s");
                Console.ResetColor();
            }
            catch (Exception ex)
            {
                Console.WriteLine();
                TftpServer.LogError($"Session error for {_clientEndPoint}: {ex.Message}");
            }
            finally
            {
                _udpClient.Close();
                _onComplete();
            }
        }

        private void DrawProgressBar(int percent, int bytesSent, int totalBytes)
        {
            lock (ConsoleLock)
            {
                int barWidth = 25;
                int filledWidth = (int)((double)percent / 100 * barWidth);
                string bar = new string('█', filledWidth) + new string('-', barWidth - filledWidth);
                Console.ForegroundColor = ConsoleColor.Cyan;
                Console.Write($"\r[{_clientEndPoint}] Progress: [{bar}] {percent}% ({bytesSent / 1024} KB / {totalBytes / 1024} KB)");
                Console.ResetColor();
            }
        }
    }
}
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Text;
using HikvisionTftpServer;

string logPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "log.txt");
using var doubleWriter = new DoubleWriter(Console.Out, logPath);
Console.SetOut(doubleWriter);
Console.Title = "Hikvision Custom Recovery TFTP Server by Den34DK";
Console.WriteLine("=========================================================================");
Console.WriteLine("Hikvision Custom Recovery TFTP Server by Den34DK with support >32mb files");
Console.WriteLine("=========================================================================");
string rootDir = AppDomain.CurrentDomain.BaseDirectory;
string requiredFileName = "digicap.dav";
string targetFilePath = Path.Combine(rootDir, requiredFileName);
if (!File.Exists(targetFilePath))
{
    Console.ForegroundColor = ConsoleColor.Red;
    Console.WriteLine($"WARNING: File '{requiredFileName}' was not found in the application directory!");
    Console.WriteLine($"Search directory: {rootDir}");
    Console.WriteLine("Please copy the firmware file into this folder and rename it to 'digicap.dav'.");
    Console.ResetColor();
}
else
{
    var fileInfo = new FileInfo(targetFilePath);
    Console.ForegroundColor = ConsoleColor.Green;
    Console.WriteLine($"Firmware file detected: {requiredFileName} ({fileInfo.Length / 1024.0 / 1024.0:F2} MB)");
    Console.ResetColor();
}
string targetIpNew = "192.168.1.128";
string targetIpOld = "192.0.0.128";
var localIpAddresses = GetLocalIpAddresses();
Console.WriteLine("\nCurrent IP addresses configured on this PC:");
bool ipMatch = false;
foreach (var ip in localIpAddresses)
{
    if (ip == targetIpNew || ip == targetIpOld)
    {
        Console.ForegroundColor = ConsoleColor.Green;
        Console.WriteLine($" -> {ip} (MATCH! Correct IP for Hikvision recovery)");
        Console.ResetColor();
        ipMatch = true;
    }
    else
    {
        Console.WriteLine($" - {ip}");
    }
}
if (!ipMatch)
{
    Console.ForegroundColor = ConsoleColor.Yellow;
    Console.WriteLine($"\n[WARNING] Neither '{targetIpNew}' nor '{targetIpOld}' is configured on any active network adapter!");
    Console.WriteLine("Hikvision bootloaders look for a TFTP server at one of these exact IP addresses.");
    Console.WriteLine("Please manually set your network adapter IP to: 192.168.1.128 or 192.0.0.128 (Subnet Mask: 255.255.255.0).");
    Console.ResetColor();
}
Console.WriteLine("\n[Recommendation] Ensure Windows Defender Firewall is TEMPORARILY DISABLED.");
Console.WriteLine("Firewalls commonly block incoming UDP packets on port 69 and 9978.");
Console.WriteLine("======================================================================\n");
TftpServer server = new TftpServer(rootDir);
try
{
    server.Start();
}
catch (SocketException ex) when (ex.SocketErrorCode == SocketError.AddressAlreadyInUse)
{
    Console.ForegroundColor = ConsoleColor.Red;
    Console.WriteLine("[ERROR] Could not start server! UDP Port 69 or 9978 is already in use by another program.");
    Console.WriteLine("Make sure other TFTP utilities (e.g., Tftpd64 or Hikvision Auto-Update) are closed.");
    Console.ResetColor();
    Console.ReadLine();
    return;
}
catch (Exception ex)
{
    Console.ForegroundColor = ConsoleColor.Red;
    Console.WriteLine($"[ERROR] Initialization error: {ex.Message}");
    Console.ResetColor();
    Console.ReadLine();
    return;
}

Console.ForegroundColor = ConsoleColor.White;
Console.WriteLine("\nServer is active. You can connect multiple cameras sequentially.");
Console.WriteLine("Press [ENTER] at any time to stop the server and exit...");
Console.ResetColor();
Console.ReadLine();
Console.ForegroundColor = ConsoleColor.Yellow;
Console.WriteLine("\nShutting down server...");
for (int i = 3; i > 0; i--)
{
    Console.Write($"\rClosing network listeners in {i} seconds to let active transfers finish...");
    Thread.Sleep(1000);
}
Console.WriteLine();
Console.ResetColor();
server.Stop();
Console.ForegroundColor = ConsoleColor.Green;
Console.WriteLine("\nServer stopped. All logs saved to 'log.txt'.");
Console.WriteLine("Press [ENTER] to close this window...");
Console.ResetColor();
Console.ReadLine();
static string[] GetLocalIpAddresses()
{
    return NetworkInterface.GetAllNetworkInterfaces()
        .Where(n => n.OperationalStatus == OperationalStatus.Up && n.NetworkInterfaceType != NetworkInterfaceType.Loopback)
        .SelectMany(n => n.GetIPProperties().UnicastAddresses)
        .Where(a => a.Address.AddressFamily == AddressFamily.InterNetwork)
        .Select(a => a.Address.ToString())
        .ToArray();
}
public class DoubleWriter : TextWriter
{
    private readonly TextWriter _console;
    private readonly StreamWriter _file;
    private readonly object _lock = new();

    public DoubleWriter(TextWriter console, string filePath)
    {
        _console = console;
        _file = new StreamWriter(new FileStream(filePath, FileMode.Create, FileAccess.Write, FileShare.ReadWrite)) { AutoFlush = true };
    }
    public override Encoding Encoding => Encoding.UTF8;
    public override void Write(char value)
    {
        lock (_lock)
        {
            _console.Write(value);
            _file.Write(value);
        }
    }
    public override void Write(string? value)
    {
        lock (_lock)
        {
            _console.Write(value);
            _file.Write(value);
        }
    }
    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _file.Dispose();
        }
        base.Dispose(disposing);
    }
}
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

public static class NativeSandboxFixture
{
    public static int Main(string[] arguments)
    {
        Console.InputEncoding = new UTF8Encoding(false);
        Console.OutputEncoding = new UTF8Encoding(false);
        string cwd = Environment.CurrentDirectory;
        string[] entries = Directory.GetFileSystemEntries(cwd);
        string input = Console.In.ReadToEnd();
        string captureDir = Environment.GetEnvironmentVariable("SUTANDO_NATIVE_SANDBOX_CAPTURES");
        if (!String.IsNullOrEmpty(captureDir))
        {
            Directory.CreateDirectory(captureDir);
            var record = new Dictionary<string, object> {
                {"arguments", arguments}, {"prompt", arguments[arguments.Length - 1]},
                {"cwd", cwd}, {"entries", entries}, {"stdin", input}, {"pid", Process.GetCurrentProcess().Id}
            };
            string file = Path.Combine(captureDir, Directory.GetFiles(captureDir, "*.json").Length.ToString("D4") + ".json");
            File.WriteAllText(file, new JavaScriptSerializer().Serialize(record), new UTF8Encoding(false));
        }
        string modeFile = Environment.GetEnvironmentVariable("SUTANDO_NATIVE_SANDBOX_MODE");
        string mode = File.Exists(modeFile) ? File.ReadAllText(modeFile).Trim() : "success";
        if (mode == "wait") Thread.Sleep(30000);
        if (mode == "fail")
        {
            Console.Out.Write("FAILED_STDOUT_MUST_STAY_PRIVATE");
            Console.Error.Write("FAILED_STDERR_MUST_STAY_PRIVATE");
            return 125;
        }
        if (mode == "empty") return 0;
        Console.Out.Write("WINDOWS_SANDBOX_OK");
        return 0;
    }
}

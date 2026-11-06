#!/usr/bin/env pwsh
# Identify, launch, and foreground a Windows app without window-title matching.

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$App,
    [ValidateRange(1, 30)]
    [int]$TimeoutSeconds = 10,
    [switch]$ValidateOnly
)

function Initialize-AppWindowApi {
    if ('Sutando.AppWindows' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace Sutando {
    public class AppProcess {
        public int ProcessId;
        public string AppId = "";
        public string ImagePath = "";
    }
    public class AppWindow {
        public long Handle;
        public int ProcessId;
        public string AppId = "";
        public bool Visible;
        public bool Minimized;
        public List<AppProcess> Owners = new List<AppProcess>();
    }
    [StructLayout(LayoutKind.Sequential)]
    struct PropertyKey {
        public Guid Format;
        public uint Id;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct Blob {
        public uint Size;
        public IntPtr Data;
    }
    [StructLayout(LayoutKind.Explicit)]
    struct PropVariant {
        [FieldOffset(0)] public ushort Type;
        [FieldOffset(8)] public IntPtr Text;
        [FieldOffset(8)] public Blob Blob;
    }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int GetAt(uint index, out PropertyKey key);
        [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
        [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
        [PreserveSig] int Commit();
    }
    public static class AppWindows {
        delegate bool EnumWindow(IntPtr window, IntPtr parameter);
        [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindow callback, IntPtr parameter);
        [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr parent, EnumWindow callback, IntPtr parameter);
        [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
        [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr window);
        [DllImport("user32.dll")] static extern bool IsIconic(IntPtr window);
        [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] static extern IntPtr GetLastActivePopup(IntPtr window);
        [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
        [DllImport("user32.dll")] static extern bool ShowWindowAsync(IntPtr window, int command);
        [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
        static extern int GetApplicationUserModelId(IntPtr process, ref uint length, StringBuilder appId);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
        static extern bool QueryFullProcessImageNameW(IntPtr process, uint flags, StringBuilder path, ref uint length);
        [DllImport("shell32.dll", PreserveSig = true)]
        static extern int SHGetPropertyStoreForWindow(IntPtr window, ref Guid iid, out IPropertyStore store);
        [DllImport("ole32.dll")] static extern int PropVariantClear(ref PropVariant value);

        static string WindowAppId(IntPtr window) {
            Guid iid = typeof(IPropertyStore).GUID;
            IPropertyStore store;
            if (SHGetPropertyStoreForWindow(window, ref iid, out store) < 0 || store == null) return "";
            try {
                PropertyKey key = new PropertyKey {
                    Format = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), Id = 5
                };
                PropVariant value;
                int result = store.GetValue(ref key, out value);
                try {
                    return result >= 0 && value.Type == 31
                        ? Marshal.PtrToStringUni(value.Text) ?? "" : "";
                } finally { PropVariantClear(ref value); }
            } finally { Marshal.ReleaseComObject(store); }
        }
        static AppProcess Process(uint pid) {
            AppProcess result = new AppProcess { ProcessId = (int)pid };
            IntPtr handle = OpenProcess(0x1000, false, pid);
            if (handle == IntPtr.Zero) return result;
            try {
                uint length = 0;
                if (GetApplicationUserModelId(handle, ref length, null) == 122 && length > 0) {
                    StringBuilder appId = new StringBuilder((int)length);
                    if (GetApplicationUserModelId(handle, ref length, appId) == 0) result.AppId = appId.ToString();
                }
                length = 32768;
                StringBuilder path = new StringBuilder((int)length);
                if (QueryFullProcessImageNameW(handle, 0, path, ref length)) result.ImagePath = path.ToString();
                return result;
            } finally { CloseHandle(handle); }
        }
        static AppWindow Read(IntPtr window, Dictionary<uint, AppProcess> processes) {
            AppWindow result = new AppWindow {
                Handle = window.ToInt64(), AppId = WindowAppId(window),
                Visible = IsWindowVisible(window), Minimized = IsIconic(window)
            };
            HashSet<uint> owners = new HashSet<uint>();
            uint pid;
            GetWindowThreadProcessId(window, out pid);
            result.ProcessId = (int)pid;
            owners.Add(pid);
            if (!processes.ContainsKey(pid)) processes[pid] = Process(pid);
            if (String.Equals(processes[pid].ImagePath,
                              Path.Combine(Environment.SystemDirectory, "ApplicationFrameHost.exe"),
                              StringComparison.OrdinalIgnoreCase)) {
                EnumChildWindows(window, (child, unused) => {
                    uint childPid;
                    GetWindowThreadProcessId(child, out childPid);
                    owners.Add(childPid);
                    return true;
                }, IntPtr.Zero);
            }
            foreach (uint owner in owners) {
                if (!processes.ContainsKey(owner)) processes[owner] = Process(owner);
                result.Owners.Add(processes[owner]);
            }
            return result;
        }
        public static AppWindow[] Snapshot() {
            List<AppWindow> windows = new List<AppWindow>();
            Dictionary<uint, AppProcess> processes = new Dictionary<uint, AppProcess>();
            EnumWindows((window, unused) => {
                if (IsWindowVisible(window)) windows.Add(Read(window, processes));
                return true;
            }, IntPtr.Zero);
            return windows.ToArray();
        }
        public static AppWindow Foreground() {
            IntPtr window = GetForegroundWindow();
            return window == IntPtr.Zero ? null : Read(window, new Dictionary<uint, AppProcess>());
        }
        public static void Restore(long handle) {
            IntPtr window = new IntPtr(handle);
            if (IsIconic(window)) ShowWindowAsync(window, 9);
        }
        public static bool Focus(long handle) {
            IntPtr window = new IntPtr(handle);
            IntPtr popup = GetLastActivePopup(window);
            if (popup != IntPtr.Zero && IsWindowVisible(popup)) window = popup;
            return SetForegroundWindow(window);
        }
    }
}
'@
}

function Assert-AppDesktop {
    $sessionId = (Get-Process -Id $PID).SessionId
    if (-not (Get-Process explorer -ErrorAction SilentlyContinue |
        Where-Object { $_.SessionId -eq $sessionId } | Select-Object -First 1)) {
        throw "No interactive desktop is available in session $sessionId."
    }
}

function Get-AppCatalogEntry([string]$AppId) {
    $shell = New-Object -ComObject Shell.Application
    $folder = $shell.Namespace('shell:AppsFolder')
    $items = @($folder.Items() | Where-Object { $_.Path -ieq $AppId })
    if ($items.Count -ne 1) { throw "Cannot resolve registered app identity '$AppId'." }
    $item = $items[0]
    return [pscustomobject]@{
        Item = $item
        ImagePath = [string]$item.ExtendedProperty('System.Link.TargetParsingPath')
        Arguments = [string]$item.ExtendedProperty('System.Link.Arguments')
    }
}

function Resolve-AppTarget([string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Name)) {
        throw 'Pass an installed app display name or executable path.'
    }
    $lookup = $Name
    if ($Name -notmatch '[\\/]' -and $Name.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) {
        $lookup = [IO.Path]::GetFileNameWithoutExtension($Name)
    }
    $registered = @(Get-StartApps | Where-Object { $_.Name -ieq $lookup -or $_.AppID -ieq $Name })
    if ($registered.Count -gt 1) { throw "More than one installed app is named '$Name'." }
    if ($registered.Count -eq 1) {
        $entry = Get-AppCatalogEntry $registered[0].AppID
        $image = $entry.ImagePath
        if ($entry.Arguments -or -not [IO.Path]::IsPathFullyQualified($image) -or
            [IO.Path]::GetExtension($image) -ine '.exe' -or -not (Test-Path -LiteralPath $image -PathType Leaf)) {
            $image = ''
        }
        return [pscustomobject]@{
            Name = $registered[0].Name; AppId = $registered[0].AppID
            ImagePath = $image; Item = $entry.Item; Executable = ''
        }
    }
    if ([WildcardPattern]::ContainsWildcardCharacters($Name)) {
        throw 'App names and executable paths must not contain wildcards.'
    }
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $command -or [IO.Path]::GetExtension($command.Source) -ine '.exe') {
        throw "App not found or not a direct executable: $Name"
    }
    return [pscustomobject]@{
        Name = $Name; AppId = ''; ImagePath = $command.Source
        Item = $null; Executable = $command.Source
    }
}

function Test-AppWindow($Window, $Target) {
    if (-not $Window -or -not $Window.Visible) { return $false }
    if ($Target.AppId -and $Window.AppId) {
        return $Target.AppId -ieq $Window.AppId
    }
    foreach ($owner in $Window.Owners) {
        if ($Target.AppId -and $owner.AppId) {
            if ($owner.AppId -ieq $Target.AppId) { return $true }
            continue
        }
        if ($Target.ImagePath -and $owner.ImagePath -ieq $Target.ImagePath) { return $true }
    }
    return $false
}

function Get-AppWindows {
    Initialize-AppWindowApi
    [Sutando.AppWindows]::Snapshot()
}

function Get-AppForeground {
    Initialize-AppWindowApi
    [Sutando.AppWindows]::Foreground()
}

function Restore-AppWindow($Window) {
    [Sutando.AppWindows]::Restore($Window.Handle)
}

function Request-AppForeground($Window) {
    [void][Sutando.AppWindows]::Focus($Window.Handle)
}

function Start-AppTarget($Target) {
    if ($Target.Item) {
        $Target.Item.InvokeVerb()
    } else {
        Start-Process -FilePath $Target.Executable | Out-Null
    }
}

function Get-AppClock { [DateTime]::UtcNow }

function New-AppSwitchResult($Window, $Target) {
    [pscustomobject]@{
        status = 'switched'; app = $Target.Name; foreground_verified = $true
        window_handle = $Window.Handle; window_process_id = $Window.ProcessId
    }
}

function Invoke-WindowsAppSwitch([string]$Name, [int]$Timeout = 10) {
    Assert-AppDesktop
    $target = Resolve-AppTarget $Name
    $deadline = (Get-AppClock).AddSeconds($Timeout)
    $foreground = Get-AppForeground
    if ((Test-AppWindow $foreground $target) -and -not $foreground.Minimized) {
        return New-AppSwitchResult $foreground $target
    }
    $window = @(Get-AppWindows | Where-Object { Test-AppWindow $_ $target }) | Select-Object -First 1
    if (-not $window) {
        Start-AppTarget $target
        do {
            Start-Sleep -Milliseconds 100
            $window = @(Get-AppWindows | Where-Object { Test-AppWindow $_ $target }) | Select-Object -First 1
        } until ($window -or (Get-AppClock) -ge $deadline)
    }
    if (-not $window) {
        throw "'$($target.Name)' launched but no window with its app identity appeared."
    }
    if ($window.Minimized) {
        Restore-AppWindow $window
        do {
            Start-Sleep -Milliseconds 100
            $window = @(Get-AppWindows | Where-Object { Test-AppWindow $_ $target }) | Select-Object -First 1
        } until (-not $window -or -not $window.Minimized -or (Get-AppClock) -ge $deadline)
        if (-not $window -or $window.Minimized) { throw "Could not restore '$($target.Name)'." }
    }
    Request-AppForeground $window
    do {
        $foreground = Get-AppForeground
        if ((Test-AppWindow $foreground $target) -and -not $foreground.Minimized) {
            return New-AppSwitchResult $foreground $target
        }
        Start-Sleep -Milliseconds 100
    } until ((Get-AppClock) -ge $deadline)
    throw "'$($target.Name)' is running, but Windows did not allow foreground focus. Select it from the taskbar."
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    try {
        if (-not $IsWindows) { throw 'Windows app switching is only available on Windows.' }
        if ($ValidateOnly) {
            Initialize-AppWindowApi
            [void][Sutando.AppWindows]::Foreground()
            [pscustomobject]@{ status = 'ok'; platform = 'windows'; api = 'win32' } | ConvertTo-Json -Compress
        } else {
            Invoke-WindowsAppSwitch $App $TimeoutSeconds | ConvertTo-Json -Compress
        }
        exit 0
    } catch {
        [pscustomobject]@{ status = 'error'; error = $_.Exception.Message } | ConvertTo-Json -Compress
        exit 1
    }
}

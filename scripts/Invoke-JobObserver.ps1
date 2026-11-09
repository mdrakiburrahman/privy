[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $Executable,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{64}$')][string] $ExpectedSha256,
    [Parameter(Mandatory)][string] $Bindings,
    [Parameter(Mandatory)][string] $Settings,
    [Parameter(Mandatory)][string] $Relays
)

$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Observer launcher requires Windows Job Object containment.'
}
Add-Type -TypeDefinition @'
using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

public sealed class PrivyObserverLaunch : IDisposable {
    [StructLayout(LayoutKind.Sequential)]
    struct BasicLimits {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinWorking, MaxWorking;
        public uint ActiveProcesses;
        public UIntPtr Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct IoCounters { public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes; }
    [StructLayout(LayoutKind.Sequential)]
    struct ExtendedLimits {
        public BasicLimits Basic;
        public IoCounters Io;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct StartupInfo {
        public uint Size;
        public string Reserved, Desktop, Title;
        public uint X, Y, XSize, YSize, XChars, YChars, Fill, Flags;
        public ushort Show, ReservedSize;
        public IntPtr ReservedData, Input, Output, Error;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct ProcessInfo { public IntPtr Process, Thread; public uint Pid, Tid; }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateJobObject(IntPtr security, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetInformationJobObject(IntPtr job, int kind, ref ExtendedLimits info, uint size);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool CreateProcess(string application, StringBuilder command, IntPtr processSecurity,
        IntPtr threadSecurity, bool inherit, uint flags, IntPtr environment, string directory,
        ref StartupInfo startup, out ProcessInfo process);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll")]
    static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32.dll")]
    static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll")]
    static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll")]
    static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern uint QueryDosDevice(string device, StringBuilder target, int capacity);
    public static bool IsLocalDrive(string root) {
        if (root.Length != 3 || root[1] != ':' || root[2] != '\\') return false;
        var target = new StringBuilder(1024);
        if (QueryDosDevice(root.Substring(0, 2), target, target.Capacity) == 0) return false;
        // Reject network mappings, SUBST aliases and unknown device targets.
        return target.ToString().StartsWith(@"\Device\HarddiskVolume", StringComparison.OrdinalIgnoreCase)
            && new System.IO.DriveInfo(root).DriveType == System.IO.DriveType.Fixed;
    }
    IntPtr job, process;
    readonly Stopwatch deadline = new Stopwatch();
    public uint Pid { get; private set; }
    public static string Quote(string value) {
        if (value.IndexOf('\0') >= 0) throw new ArgumentException("Invalid command argument.");
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') { result.Append('\\', slashes * 2 + 1); result.Append(c); }
            else { result.Append('\\', slashes); result.Append(c); }
            slashes = 0;
        }
        result.Append('\\', slashes * 2); result.Append('"');
        return result.ToString();
    }
    public PrivyObserverLaunch(string executable, string[] arguments) {
        string name = @"Local\PrivyObserver-" + Guid.NewGuid().ToString("N");
        job = CreateJobObject(IntPtr.Zero, name);
        if (job == IntPtr.Zero) throw new Win32Exception();
        ProcessInfo info = new ProcessInfo();
        IntPtr environment = IntPtr.Zero;
        try {
            var limits = new ExtendedLimits();
            limits.Basic.Flags = 0x2000; // Kill on close; never permit breakaway.
            if (!SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(limits)))
                throw new Win32Exception();
            var variables = new SortedDictionary<string,string>(StringComparer.OrdinalIgnoreCase);
            foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables())
                variables[(string)entry.Key] = (string)entry.Value;
            variables["PRIVY_OBSERVER_JOB"] = name;
            var block = new StringBuilder();
            foreach (var item in variables) block.Append(item.Key).Append('=').Append(item.Value).Append('\0');
            block.Append('\0');
            environment = Marshal.StringToHGlobalUni(block.ToString());
            var command = new StringBuilder(Quote(executable));
            foreach (string arg in arguments) command.Append(' ').Append(Quote(arg));
            var startup = new StartupInfo();
            startup.Size = (uint)Marshal.SizeOf(startup);
            deadline.Start(); // Includes onefile extraction, bootstrap, auth and descendants.
            if (!CreateProcess(executable, command, IntPtr.Zero, IntPtr.Zero, false, 4 | 0x400,
                environment, null, ref startup, out info)) throw new Win32Exception();
            process = info.Process;
            Pid = info.Pid;
            if (!AssignProcessToJobObject(job, process)) {
                TerminateProcess(process, 124);
                WaitForSingleObject(process, 2000);
                throw new Win32Exception();
            }
            if (deadline.ElapsedMilliseconds >= 45000)
                throw new TimeoutException("Observer native-launch deadline.");
            if (ResumeThread(info.Thread) == UInt32.MaxValue) throw new Win32Exception();
        } catch {
            Dispose();
            throw;
        } finally {
            if (info.Thread != IntPtr.Zero) CloseHandle(info.Thread);
            if (environment != IntPtr.Zero) Marshal.FreeHGlobal(environment);
        }
    }
    public int Wait() {
        return WaitBounded(45000);
    }
    public int WaitBounded(int milliseconds) {
        if (milliseconds < 1 || milliseconds > 45000) throw new ArgumentOutOfRangeException();
        long remaining = milliseconds - deadline.ElapsedMilliseconds;
        uint wait = WaitForSingleObject(process, (uint)Math.Max(0, remaining));
        if (wait == 258) return 124;
        if (wait != 0) throw new Win32Exception();
        uint code;
        if (!GetExitCodeProcess(process, out code)) throw new Win32Exception();
        return (int)code;
    }
    public void Dispose() {
        if (job != IntPtr.Zero) {
            TerminateJobObject(job, 124);
            CloseHandle(job);
            job = IntPtr.Zero;
        }
        if (process != IntPtr.Zero) {
            WaitForSingleObject(process, 2000);
            CloseHandle(process);
            process = IntPtr.Zero;
        }
    }
}
'@

function Resolve-ObserverLocalPath {
    param([Parameter(Mandatory)][string] $Path)
    # Reject network/device forms before provider resolution or any file access.
    $form = $Path.Replace('/', '\')
    if ($form.StartsWith('\\') -or $form.StartsWith('\??\') -or $form.Contains('::')) {
        throw 'Observer paths must be ordinary local FileSystem paths.'
    }
    $provider = $null
    $drive = $null
    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
        $Path, [ref] $provider, [ref] $drive)
    if ($provider.Name -ne 'FileSystem' -or ($null -ne $drive -and $drive.DisplayRoot)) {
        throw 'Observer paths require a local FileSystem provider drive.'
    }
    $root = [IO.Path]::GetPathRoot($resolved)
    if (-not [PrivyObserverLaunch]::IsLocalDrive($root)) {
        throw 'Observer path locality could not be established.'
    }
    if ($resolved.Substring($root.Length).Contains(':')) {
        throw 'Observer alternate data streams are forbidden.'
    }
    # Walk from the confirmed local volume outward; never traverse a link/junction.
    $current = $root
    $parts = $resolved.Substring($root.Length).Split('\', [StringSplitOptions]::RemoveEmptyEntries)
    foreach ($part in $parts) {
        $current = [IO.Path]::Combine($current, $part)
        $item = Get-Item -LiteralPath $current -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Observer paths must not traverse reparse points or links.'
        }
    }
    if ((Get-Item -LiteralPath $resolved -Force).PSIsContainer) {
        throw 'Observer path must identify an existing local file.'
    }
    return $resolved
}

# Preflight is offline and outside the native-launch deadline.
# Establish locality for every path before any content read or artifact hash.
$exe = Resolve-ObserverLocalPath -Path $Executable
$paths = @($Bindings, $Settings, $Relays) | ForEach-Object { Resolve-ObserverLocalPath -Path $_ }
if ((Get-Item -LiteralPath $exe).Length -gt 128MB) {
    throw 'Observer executable must be a bounded local file.'
}
if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -ne $ExpectedSha256) {
    throw 'Observer executable hash mismatch.'
}

$arguments = @('observe-job', '--bindings', $paths[0], '--settings', $paths[1], '--relays', $paths[2])
$owned = $null
try {
    $owned = [PrivyObserverLaunch]::new($exe, $arguments)
    $result = $owned.Wait()
}
finally {
    if ($null -ne $owned) { $owned.Dispose() }
}
if ($result -eq 124) {
    Write-Output '{"outcome":"inconclusive","reason":"external_launch_or_bootstrap_deadline"}'
}
exit $result

# Smoke test for the Windows build, packaging/dist/cynitor-server.exe.
#
# Every check here guards against something a successful build does not
# prove: modules or DLLs PyInstaller could not see, which only surface when
# the CAN stack is imported, i.e. on connect. No CAN hardware is needed and
# none is touched: the gs_usb check asks for a device index that does not
# exist, and the full session runs on a python-can virtual bus.
#
# Usage (from the repository root or anywhere):
#   powershell -ExecutionPolicy Bypass -File packaging/smoke-test.ps1
# Exits non-zero on the first failure.

$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'dist\cynitor-server.exe'
if (-not (Test-Path $exe)) { throw "Not built: $exe" }
$port = 8099
$base = "http://127.0.0.1:$port"

function Fail([string]$message, [string]$log) {
    Write-Host "::error::$message"
    if ($log -and (Test-Path $log)) { Get-Content $log -Tail 40 | Write-Host }
    exit 1
}

# Start the server with the given arguments; wait until it answers /api/health.
function Start-Server([string[]]$arguments, [hashtable]$environment = @{}) {
    $log = New-TemporaryFile
    $saved = @{}
    foreach ($name in $environment.Keys) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name)
        [Environment]::SetEnvironmentVariable($name, $environment[$name])
    }
    # Fresh folders to start in and to keep data in: the test must not write
    # into the real per-user data folder, and nothing may land where it starts.
    $workdir = New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid()))
    $datadir = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid())
    $process = Start-Process -FilePath $exe -ArgumentList ($arguments + @('--port', $port, '--data-dir', "`"$datadir`"")) `
        -WorkingDirectory $workdir -RedirectStandardError $log -RedirectStandardOutput "$log.out" `
        -NoNewWindow -PassThru
    foreach ($name in $environment.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name]) }
    for ($i = 0; $i -lt 90; $i++) {
        if ($process.HasExited) { Fail "server exited early (code $($process.ExitCode))" $log }
        try { Invoke-WebRequest "$base/api/health" -UseBasicParsing -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep 1 }
    }
    return @{ Process = $process; Log = $log; WorkDir = $workdir.FullName; DataDir = $datadir }
}

# The single-file build runs as two processes: the PyInstaller bootloader,
# which Start-Process returns, and the interpreter it starts. Stop both.
function Get-ServerChild($server) {
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$($server.Process.Id)" |
        Where-Object Name -eq 'cynitor-server.exe'
}

function Stop-Server($server) {
    if (-not $server.Process.HasExited) {
        # taskkill's complaints go to stderr, which Windows PowerShell 5.1
        # turns into errors; there is nothing to report here either way.
        try { & taskkill.exe /PID $server.Process.Id /T /F *> $null } catch { }
    }
    $server.Process.WaitForExit()
}

# Wait until the server's log matches $pattern; return whether it did.
function Wait-Log($server, [string]$pattern, [int]$seconds = 60) {
    for ($i = 0; $i -lt $seconds; $i++) {
        if (Select-String -Path $server.Log -Pattern $pattern -Quiet) { return $true }
        Start-Sleep 1
    }
    return $false
}

function Assert-NoMissingModules($server) {
    $missing = Select-String -Path $server.Log -Pattern 'No module named|No backend available'
    if ($missing) { Fail "the bundle is missing a module or the libusb DLL: $($missing[0].Line)" $server.Log }
}

# 1. The executable runs at all.
$version = & $exe --version
if ($LASTEXITCODE -ne 0) { Fail "--version failed" }
Write-Host "ok: $version"

# 2. gs_usb and its libusb DLL are in the bundle. With the DLL missing pyusb
#    reports "No backend available"; with it present, a device index that no
#    adapter has fails with "Cannot find device".
$server = Start-Server @('--can', 'gs_usb:9', '--bitrate', '500000')
try {
    if (-not (Wait-Log $server 'Could not attach|No module named|No backend available')) { Fail "attach never failed or succeeded" $server.Log }
    Assert-NoMissingModules $server
    if (-not (Select-String -Path $server.Log -Pattern 'Cannot find device' -Quiet)) { Fail "gs_usb did not get as far as looking for devices" $server.Log }
    Write-Host "ok: gs_usb and libusb load inside the bundle"
} finally { Stop-Server $server }

# 3. The whole CAN stack -- hub, DSDL, pycyphal, allocator, scanner -- works
#    frozen, on a virtual bus standing in for an adapter.
$server = Start-Server @('--can', 'virtual:smoke', '--bitrate', '500000')
try {
    if (-not (Wait-Log $server 'CAN session started|Could not attach')) { Fail "session neither started nor failed" $server.Log }
    Assert-NoMissingModules $server
    if (-not (Select-String -Path $server.Log -Pattern 'CAN session started' -Quiet)) { Fail "could not connect through the hub" $server.Log }
    $status = Invoke-RestMethod "$base/api/status"
    if ($status.status -ne 'running' -or $status.can_bitrate -ne 500000) { Fail "unexpected status: $($status | ConvertTo-Json -Compress)" $server.Log }
    Write-Host "ok: connected through the CAN hub inside the bundle"
    # The databases go to the data folder, not to wherever it was started.
    if (-not (Test-Path (Join-Path $server.DataDir 'telemetry_events.db'))) { Fail "no database in the data folder" $server.Log }
    if (Get-ChildItem $server.WorkDir -Filter '*.db*') { Fail "databases written to the working directory" $server.Log }
    Write-Host "ok: data kept in the data folder"
} finally { Stop-Server $server }

# 4. The dashboard is served from the bundle, and a token protects the API.
$server = Start-Server @() @{ CYNITOR_AUTH_TOKEN = 'smoke-test-token' }
try {
    $page = Invoke-WebRequest "$base/" -UseBasicParsing
    if ($page.StatusCode -ne 200) { Fail "dashboard not served (HTTP $($page.StatusCode))" $server.Log }
    # Windows PowerShell 5.1 and PowerShell 7 throw different exception types
    # for an HTTP error; both carry the response's status code.
    $code = $null
    try { Invoke-WebRequest "$base/api/status" -UseBasicParsing | Out-Null; $code = 200 }
    catch { $code = [int]$_.Exception.Response.StatusCode }
    if ($code -ne 401) { Fail "API: expected 401 without the token, got $code" $server.Log }
    Write-Host "ok: dashboard served, API requires the token"
} finally { Stop-Server $server }

# 5. Killing the bootloader ends the server too, instead of leaving it holding
#    the port (main._exit_when_windows_parent_dies).
$server = Start-Server @()
try {
    $child = Get-ServerChild $server
    if (-not $child) { Fail "no server process under the bootloader" $server.Log }
    Stop-Process -Id $server.Process.Id -Force
    $gone = $false
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue)) { $gone = $true; break }
        Start-Sleep 1
    }
    if (-not $gone) {
        Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
        Fail "the server outlived its bootloader and kept port $port" $server.Log
    }
    Write-Host "ok: killing the bootloader stops the server"
} finally { Stop-Server $server }

Write-Host "Smoke test passed."

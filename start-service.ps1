# Postcard Studio - long-running server, restarted ONLY after the child process exits.
# Usage (from the project folder):  powershell -ExecutionPolicy Bypass -File .\start-service.ps1
# Logs: app.log (stdout) / app.err.log (stderr), appended across restarts and rotated to *.log.1
# at start-up once they pass 5MB. Stop the launcher with Ctrl+C.
$ErrorActionPreference = 'Continue'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir
# 'python.exe' on this machine is a 3.8 without Flask: the loop would restart a crash forever.
$py = & (Join-Path $dir 'find-python.ps1')
if (-not $py) {
    Write-Host 'No Python with the required packages found - run:  python -m pip install -r requirements.txt' -ForegroundColor Red
    exit 1
}
$env:PYTHONUTF8 = '1'          # the logs carry Chinese text and the degree sign in the card number
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }   # readable redirected log
# BLAS/OpenMP pools are per process, so every one of waitress' 12 threads would otherwise be able to
# spin up its own; pin them before any child starts (same values the docs ask you to export by hand).
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$out = Join-Path $dir 'app.log'
$err = Join-Path $dir 'app.err.log'
$appPath = Join-Path $dir 'app.py'

function Get-Setting([string]$name, [string]$default) {
    # Same precedence as app.py: process env > .env beside app.py > built-in default.
    $v = [Environment]::GetEnvironmentVariable($name)
    if (-not $v -and (Test-Path -LiteralPath (Join-Path $dir '.env'))) {
        foreach ($ln in Get-Content -LiteralPath (Join-Path $dir '.env') -Encoding UTF8) {
            $s = $ln.Trim()
            if (-not $s -or $s.StartsWith('#') -or -not $s.Contains('=')) { continue }
            $parts = $s.Split('=', 2)
            if ($parts[0].Trim() -eq $name) { $v = $parts[1].Trim().Trim('"').Trim("'"); break }
        }
    }
    if (-not $v) { $v = $default }
    return ([string]$v).Trim()
}

$port = 0
if (-not [int]::TryParse((Get-Setting 'POSTCARD_PORT' '5179'), [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
    $port = 5179
}

function Rotate-Log([string]$path, [long]$limit = 5MB) {
    # Never delete a log: rename it once it is big enough to be worth keeping as one file.
    if ((Test-Path -LiteralPath $path) -and ((Get-Item -LiteralPath $path).Length -gt $limit)) {
        Move-Item -LiteralPath $path -Destination ($path + '.1') -Force -ErrorAction SilentlyContinue
    }
}

function Kill-Existing {
    # Only OUR app.py: matching "app.py" alone used to kill other projects' Python servers too.
    $needle = $appPath.ToLowerInvariant()
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python' -and $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($needle)
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Test-StopRequested {
    # Ctrl+C is the documented way out of the launcher; it must not kill the running service loop.
    try {
        if ([Console]::IsInputRedirected) { return $false }
        if ($Host.UI.RawUI.KeyAvailable) {
            $key = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
            if ($key.Key -eq 'C') { return $true }
        }
    } catch { }
    return $false
}

function Start-App {
    # `-u` keeps the redirected log unbuffered (block buffering is why the old logs stayed empty).
    # cmd's `>>` appends to the log; Start-Process' own redirection truncates it on every restart,
    # which is what erased the crash scene each time the old loop killed and relaunched the app.
    $line = '/d /c ""{0}" -u "{1}" 1>>"{2}" 2>>"{3}""' -f $py, $appPath, $out, $err
    return Start-Process -FilePath $env:ComSpec -ArgumentList $line -WindowStyle Hidden -PassThru
}

function Show-ErrTail {
    Write-Host ('[{0}] last lines of {1}:' -f (Get-Date -Format 'HH:mm:ss'), $err) -ForegroundColor Yellow
    if (Test-Path -LiteralPath $err) {
        Get-Content -LiteralPath $err -Tail 20 | ForEach-Object { Write-Host ('  ' + $_) }
    } else {
        Write-Host '  (app.err.log does not exist)'
    }
}

Write-Host 'Postcard Studio auto-restart service (restarts only after app.py exits)' -ForegroundColor Cyan
Write-Host "  dir  : $dir"
Write-Host "  py   : $py"
Write-Host "  port : $port"
Write-Host '  Ctrl+C to stop the launcher.' -ForegroundColor Cyan

Kill-Existing
Rotate-Log $out
Rotate-Log $err

$delay = 5
while ($true) {
    Write-Host ('[{0}] starting app.py ...' -f (Get-Date -Format 'HH:mm:ss')) -ForegroundColor Green
    $started = Get-Date
    $p = Start-App
    # Wait for the port (up to ~20s), but stop early if the child has already died.
    $up = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if ($p.HasExited) { break }
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { $up = $true; break }
    }
    if ($up) {
        Write-Host ('[{0}] service UP on :{1}  (pid {2})' -f (Get-Date -Format 'HH:mm:ss'), $port, $p.Id) -ForegroundColor Green
    } else {
        Write-Host ('[{0}] :{1} did NOT come up; check {2}' -f (Get-Date -Format 'HH:mm:ss'), $port, $err) -ForegroundColor Yellow
        Show-ErrTail
    }
    # A healthy service is left alone: no timer, no reload - only a real exit moves us on.
    while (-not $p.HasExited) {
        Start-Sleep -Milliseconds 500
        if (Test-StopRequested) { Write-Host 'stopped.'; exit }
    }
    $code = '?'
    try { $p.Refresh(); $code = $p.ExitCode } catch { }
    $ran = ((Get-Date) - $started).TotalSeconds
    if ($ran -gt 120) { $delay = 5 }       # a long healthy run resets the backoff
    Write-Host ('[{0}] app.py exited (code {1}) after {2:n0}s; restarting in {3}s' -f (Get-Date -Format 'HH:mm:ss'), $code, $ran, $delay) -ForegroundColor Yellow
    for ($k = 0; $k -lt $delay * 2; $k++) {
        Start-Sleep -Milliseconds 500
        if (Test-StopRequested) { Write-Host 'stopped.'; exit }
    }
    $delay = [Math]::Min(60, $delay * 2)
}

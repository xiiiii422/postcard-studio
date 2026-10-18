# find-python.ps1 -- print the first Python interpreter that can actually serve Postcard Studio.
# The launcher used to hard-code `python`, which here resolves to a 3.8 install without Flask:
# the service died on start-up and only app.err.log knew why. Every candidate is *probed* for the
# five packages requirements.txt installs, then for `import app` in this checkout.
#   .\find-python.ps1            -> path of a usable interpreter (stdout), exit 0
#   .\find-python.ps1 -Recheck   -> ignore the cached answer and look again
param([switch]$Recheck)
$ErrorActionPreference = 'SilentlyContinue'
# Exactly the five third-party packages requirements.txt installs (flask, waitress, pillow, numpy,
# reportlab). numpy used to be missing here, so an interpreter that silently fell back to the slow
# pure-Python vectorizer could be chosen for the whole session.
$need = 'import flask, PIL, reportlab, waitress, numpy'
$root = $PSScriptRoot.TrimEnd('\')      # r'...' below: a trailing backslash would break the raw string
$needApp = "import sys;sys.path.insert(0, r'$root');import app"
$cache = Join-Path $PSScriptRoot '.python-path'

function Invoke-Probe {
    param($exe, $code, $ms = 15000, $pre = @())
    # Never `& $exe`: C:\...\WindowsApps\python3.exe is a Store stub that opens the Store and
    # never returns, which froze the whole probe. Start-Process + a hard timeout cannot hang.
    # Returns @{ok=..; out=..}: an import probe prints nothing, so the stdout alone cannot say
    # "succeeded quietly" apart from "failed" - the old version conflated them and found nothing.
    if (-not $exe -or $exe -notmatch '\.exe$' -or $exe -match 'WindowsApps') { return @{ok = $false; out = ''} }
    if (-not (Test-Path $exe)) { return @{ok = $false; out = ''} }
    try {
        # Start-Process -PassThru hands back an object whose ExitCode is empty once the child is
        # gone; ProcessStartInfo with UseShellExecute=false is the only way to get a real exit
        # code AND a timeout in Windows PowerShell 5.1.
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $exe
        $psi.Arguments = ((@($pre) + @('-c', ('"' + $code + '"'))) -join ' ')
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        $stdout = $p.StandardOutput.ReadToEndAsync()
        $stderr = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit($ms)) {
            try { $p.Kill() } catch { }
            return @{ok = $false; out = ''}
        }
        $text = ''
        try { $text = $stdout.Result } catch { }
        $err = ''
        try { $err = $stderr.Result } catch { }
        return @{ok = ($p.ExitCode -eq 0); out = ([string]$text).Trim(); err = ([string]$err).Trim()}
    } catch {
        return @{ok = $false; out = ''; err = "$($_.Exception.Message)"}
    }
}

function Test-Python($exe) {
    return (Invoke-Probe -exe $exe -code $need).ok
}

function Test-App($exe) {
    # The interpreter is only genuinely usable for THIS checkout if app.py imports there too.
    return (Invoke-Probe -exe $exe -code $needApp -ms 40000).ok
}

function Get-PyVersionKey([string]$path) {
    # Python314 must outrank Python39: a plain alphabetical sort put "Python39" first, so the
    # 3.9 install (often without the current packages) won over the 3.14 one.
    $m = [regex]::Match($path, 'Python(?<maj>\d)(?<min>\d*)', 'IgnoreCase')
    if (-not $m.Success) { return 0 }
    $minor = 0
    if ($m.Groups['min'].Success) { [void][int]::TryParse($m.Groups['min'].Value, [ref]$minor) }
    return ([int]$m.Groups['maj'].Value * 1000 + $minor)
}

function Get-Candidates {
    # Fixed shapes only - a recursive scan once turned up a DevEco analysis Python and cost 2 minutes.
    $patterns = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Python3*\python.exe'),
        (Join-Path $env:PROGRAMFILES 'Python\Python3*\python.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Python\Python3*\python.exe'),
        'C:\Python3*\python.exe'
    )
    # A List, on purpose: `$found += $x` on a one-element result silently turns into string
    # concatenation, which handed Test-Path four paths glued together and matched nothing.
    $list = [System.Collections.Generic.List[string]]::new()
    if ($env:POSTCARD_PY) { $list.Add($env:POSTCARD_PY) }
    $hits = [System.Collections.Generic.List[string]]::new()
    foreach ($pat in $patterns) {
        foreach ($hit in @(Get-Item $pat -ErrorAction SilentlyContinue)) {
            if (-not $hits.Contains($hit.FullName)) { $hits.Add($hit.FullName) }
        }
    }
    # Highest version first (3.14 > 3.13 > 3.9), never alphabetical order.
    foreach ($hit in @($hits | Sort-Object @{ Expression = { Get-PyVersionKey $_ } }, @{ Expression = { $_ } } -Descending)) {
        $list.Add($hit)
    }
    foreach ($cmd in @('python', 'python3')) {
        $c = Get-Command $cmd
        if ($c) { $list.Add($c.Source) }
    }
    $list.Add('C:\Windows\py.exe')
    $seen = @{}
    $out = [System.Collections.Generic.List[string]]::new()
    foreach ($f in $list) {
        if ($f -and -not $seen[$f.ToLower()]) { $seen[$f.ToLower()] = 1; $out.Add($f) }
    }
    return $out
}

function Save-Choice($exe) {
    # UTF-8 (no BOM): the path may contain non-ASCII folder names (e.g. Chinese project folders).
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($cache, $exe, $utf8)
    Write-Output $exe
    exit 0
}

# Interpreter with the five packages but a broken app.py import: only used if nothing better shows up.
$depsOnly = $null

if (-not $Recheck -and (Test-Path $cache)) {
    $saved = (Get-Content $cache -First 1).Trim()
    if (Test-Python $saved) {
        if (Test-App $saved) { Save-Choice $saved }
        elseif (-not $depsOnly) { $depsOnly = $saved }
    }
}

$cands = @(Get-Candidates)
foreach ($exe in $cands) {
    if (Test-Python $exe) {
        if (Test-App $exe) { Save-Choice $exe }
        elseif (-not $depsOnly) { $depsOnly = $exe }
    }
}

# the py launcher reaches interpreters that are on no PATH and in no standard folder
$py = 'C:\Windows\py.exe'
if ((Test-Path $py) -or ($cands -contains $py)) {
    foreach ($ver in @('3.15', '3.14', '3.13', '3.12', '3.11', '3.10', '3')) {
        $r = Invoke-Probe -exe $py -code 'import sys;print(sys.executable)' -pre @("-$ver")
        if ($r.ok -and $r.out) {
            $where = $r.out.Trim()
            if ($where -ne $py -and (Test-Python $where)) {
                if (Test-App $where) { Save-Choice $where }
                elseif (-not $depsOnly) { $depsOnly = $where }
            }
        }
    }
}

if ($depsOnly) {
    [Console]::Error.WriteLine("[find-python] WARN: '$depsOnly' has the five requirements but cannot import app.py; using it anyway.")
    [Console]::Error.WriteLine('  Fix app.py first - the service will fail to start otherwise.')
    Save-Choice $depsOnly
}

[Console]::Error.WriteLine('[find-python] no Python on this machine has flask + pillow + reportlab + waitress + numpy.')
if ($cands.Count -gt 0) {
    [Console]::Error.WriteLine("  install them with:  `"$($cands[0])`" -m pip install -r `"$PSScriptRoot\requirements.txt`"")
}
exit 1

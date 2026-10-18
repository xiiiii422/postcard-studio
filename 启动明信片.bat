@echo off
REM ============================================================
REM  Postcard Studio - one-click launcher
REM  Starts the web server (waitress) and, ONLY when explicitly
REM  asked, the Cloudflare named tunnel. The tunnel used to start
REM  by itself whenever a cert.pem happened to exist, which put an
REM  unauthenticated renderer on the public internet.
REM
REM    启动明信片.bat              -> local only (no tunnel)
REM    启动明信片.bat --tunnel     -> also start the Cloudflare tunnel
REM    set POSTCARD_TUNNEL=1       -> same, for scripts
REM
REM  This file is saved as UTF-8 and switches the console to 65001
REM  so the Chinese status lines print correctly. Hard-coded
REM  project paths are still avoided: everything resolves through
REM  %~dp0, so the launcher works from any folder.
REM ============================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "CLD=C:\Program Files (x86)\cloudflared\cloudflared.exe"
set "CFDIR=%USERPROFILE%\.cloudflared"
set "TUN=%POSTCARD_TUNNEL_NAME%"
set "PYTHONUTF8=1"
REM One BLAS/OpenMP thread per process: the renderer is single-threaded, and 12 waitress
REM threads each starting a thread pool is what exhausted memory inside numpy/OpenBLAS.
set "OPENBLAS_NUM_THREADS=1"
set "OMP_NUM_THREADS=1"

REM ---- port: POSTCARD_PORT env, then .env, then 5179 (same precedence as app.py) ----
set "PORT=%POSTCARD_PORT%"
if not defined PORT if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="POSTCARD_PORT" set "PORT=%%b"
  )
)
if not defined PORT set "PORT=5179"
for /f "tokens=* delims= " %%v in ("%PORT%") do set "PORT=%%v"

set "PY="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -ExecutionPolicy Bypass -File .\find-python.ps1`) do set "PY=%%i"
if not defined PY (
  echo [Postcard Studio] No usable Python found - see the message above.
  pause
  exit /b 1
)
echo [Postcard Studio] interpreter: %PY%

echo [Postcard Studio] Cleaning previous processes...
powershell -NoProfile -Command "$ErrorActionPreference='SilentlyContinue'; $needle=('%~dp0app.py').ToLower(); Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($needle) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; exit 0"

echo [Postcard Studio] Starting web server (waitress) ...
powershell -NoProfile -Command "Start-Process -FilePath '%PY%' -ArgumentList '-u','app.py' -WorkingDirectory '%~dp0.' -WindowStyle Hidden -RedirectStandardOutput 'app.log' -RedirectStandardError 'app.err.log'"

REM ---- Cloudflare tunnel: opt-in only (--tunnel or POSTCARD_TUNNEL=1) ----
set "TUNNEL=0"
for %%a in (%*) do if /i "%%a"=="--tunnel" set "TUNNEL=1"
if /i "%POSTCARD_TUNNEL%"=="1" set "TUNNEL=1"
set "TUNNEL_UP=0"
if "%TUNNEL%"=="1" (
  if not exist "%CLD%" (
    echo [WARN] --tunnel requested but cloudflared was not found at "%CLD%"; local only.
  ) else if defined TUN (
    if not exist "%CFDIR%\cert.pem" (
      echo [WARN] named tunnel "%TUN%" requested but "%CFDIR%\cert.pem" is missing; local only.
    ) else (
      echo [Postcard Studio] Starting named Cloudflare tunnel %TUN% ...
      powershell -NoProfile -Command "$ErrorActionPreference='SilentlyContinue'; Stop-Process -Name cloudflared -Force; exit 0"
      del /q "ntunnel.log" "ntunnel.err.log" 2>nul
      powershell -NoProfile -Command "Start-Process -FilePath '%CLD%' -ArgumentList 'tunnel','run','%TUN%' -WindowStyle Hidden -RedirectStandardOutput 'ntunnel.log' -RedirectStandardError 'ntunnel.err.log'"
      set "TUNNEL_UP=1"
    )
  ) else (
    echo [Postcard Studio] Starting Cloudflare quick tunnel - no named tunnel configured ...
    powershell -NoProfile -Command "$ErrorActionPreference='SilentlyContinue'; Stop-Process -Name cloudflared -Force; exit 0"
    del /q "ntunnel.log" "ntunnel.err.log" 2>nul
    powershell -NoProfile -Command "Start-Process -FilePath '%CLD%' -ArgumentList 'tunnel','--url','http://127.0.0.1:%PORT%' -WindowStyle Hidden -RedirectStandardOutput 'ntunnel.log' -RedirectStandardError 'ntunnel.err.log'"
    set "TUNNEL_UP=1"
  )
)
if "%TUNNEL_UP%"=="0" (
  echo.
  echo   [LOCAL ONLY] 仅本机访问：未启动 Cloudflare 隧道，服务只监听 127.0.0.1:%PORT%
  echo   ^(需要公网访问时用  启动明信片.bat --tunnel，并自行配置 Cloudflare Access 鉴权^)
)

set /a tries=0
:WAIT
set /a tries+=1
timeout /t 1 /nobreak >nul 2>nul
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 goto UP
if %tries% lss 20 goto WAIT

echo.
echo   [WARN] :%PORT% is not listening yet - the last lines of app.err.log say why:
powershell -NoProfile -Command "if (Test-Path app.err.log) { Get-Content app.err.log -Tail 8 } else { '(app.err.log is empty)' }"
goto DONE

:UP
echo.
echo   [OK] Web App UP :  http://127.0.0.1:%PORT%
if "%TUNNEL_UP%"=="1" echo   [OK] Tunnel started : see ntunnel.err.log for the public URL
echo.
start "" http://127.0.0.1:%PORT%

:DONE
echo.
echo   Services run in the background; closing this window does not stop them.
echo   Stop them with:  powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" ^| Where-Object { $_.CommandLine -like '*app.py*' } ^| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo.
pause
endlocal

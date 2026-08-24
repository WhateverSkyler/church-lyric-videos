<#
    Sets up the render worker on the church PC.

    Run once, from an ADMINISTRATOR PowerShell:

        cd C:\Hopewell\church-lyric-videos
        powershell -ExecutionPolicy Bypass -File worker\setup-windows.ps1 `
            -Url "https://lyrics.yourdomain.org" -Token "<worker token>"

    This machine also runs the Sunday livestream, so the installer is
    deliberately conservative about it:

      * It does NOT change the system PATH. ffmpeg and yt-dlp already exist in
        a tools\ folder here; their locations are recorded in worker\.env and
        used directly, so nothing the streaming setup depends on can be
        shadowed by a different version.
      * The scheduled task runs as SYSTEM at boot. There is no auto-login on
        this machine, so a task tied to the interactive session would not run
        after a restart until somebody signed in.
      * Nothing is launched through a visible PowerShell window. Where
        tools\run-hidden.vbs exists it is used, since a window appearing during
        worship is exactly the sort of thing that has caused trouble here.

    The worker itself refuses to render during a service and never takes an
    NVENC session while OBS is running — see worker\guard.py.
#>

param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Token,
    [string]$SundayDir = "$env:USERPROFILE\Hopewell Lyric Videos",
    [switch]$SkipPython
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Say  ($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Good ($m) { Write-Host "    $m"   -ForegroundColor Green }
function Warn ($m) { Write-Host "    ! $m" -ForegroundColor Yellow }

Say "Hopewell render worker setup"
Write-Host "    project : $Root"
Write-Host "    output  : $SundayDir"

# --------------------------------------------------------------------------
# Locate the tools this machine already has, rather than installing our own.
# --------------------------------------------------------------------------
Say "Looking for ffmpeg and yt-dlp"

function Find-Tool([string]$name) {
    # Prefer a copy sitting beside the streaming setup, then anything on PATH.
    $candidates = @(
        (Join-Path $Root "tools\$name.exe"),
        (Join-Path (Split-Path -Parent $Root) "tools\$name.exe"),
        "C:\Hopewell\tools\$name.exe",
        "C:\ffmpeg\bin\$name.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return (Resolve-Path $c).Path } }
    $onPath = Get-Command $name -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return $null
}

$ffmpeg  = Find-Tool "ffmpeg"
$ffprobe = Find-Tool "ffprobe"
$ytdlp   = Find-Tool "yt-dlp"

if ($ffmpeg)  { Good "ffmpeg  : $ffmpeg" }  else { Warn "ffmpeg not found" }
if ($ffprobe) { Good "ffprobe : $ffprobe" } else {
    if ($ffmpeg) {
        $sibling = Join-Path (Split-Path -Parent $ffmpeg) "ffprobe.exe"
        if (Test-Path $sibling) { $ffprobe = $sibling; Good "ffprobe : $ffprobe" }
    }
}
if ($ytdlp) { Good "yt-dlp  : $ytdlp" } else { Warn "yt-dlp not found" }

if (-not $ffmpeg -or -not $ytdlp) {
    Warn "Installing only what is missing, into the project's own tools folder."
    $toolDir = Join-Path $Root "tools"
    New-Item -ItemType Directory -Force -Path $toolDir | Out-Null
    if (-not $ytdlp) {
        Invoke-WebRequest -UseBasicParsing `
            -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" `
            -OutFile (Join-Path $toolDir "yt-dlp.exe")
        $ytdlp = Join-Path $toolDir "yt-dlp.exe"
        Good "yt-dlp installed to $ytdlp"
    }
    if (-not $ffmpeg) {
        Warn "ffmpeg must be installed by hand — it is too large to fetch here."
        Warn "Get a build from https://www.gyan.dev/ffmpeg/builds/ and put"
        Warn "ffmpeg.exe and ffprobe.exe in $toolDir"
    }
}

# --------------------------------------------------------------------------
Say "Checking Python"
# The Windows Store stub reports itself as python.exe but cannot install
# packages, so it has to be told apart from a real interpreter.
$python = $null
foreach ($c in @("$Root\.venv\Scripts\python.exe", "py", "python")) {
    try {
        $probe = & $c -c "import sys, sysconfig; print(sys.version.split()[0]); print(sysconfig.get_paths()['purelib'])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $probe -and $probe[1] -notlike "*WindowsApps*") {
            $python = (Get-Command $c -ErrorAction SilentlyContinue).Source
            if (-not $python) { $python = $c }
            Good "Python $($probe[0]) at $python"
            break
        }
    } catch { }
}

if (-not $python -and -not $SkipPython) {
    Say "Installing Python 3.12 (the Store stub cannot install packages)"
    winget install --id Python.Python.3.12 --silent --accept-source-agreements `
           --accept-package-agreements --disable-interactivity 2>$null
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    $python = (Get-Command py -ErrorAction SilentlyContinue).Source
    if (-not $python) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $python) { throw "Python still not available after installing. Install it by hand and re-run." }
    Good "Python installed at $python"
}

# --------------------------------------------------------------------------
Say "Checking the GPU"
$gpu = $null
try { $gpu = & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null } catch { }
if ($gpu) { Good "$gpu" }
else { Warn "No NVIDIA GPU detected — transcription and rendering will use the CPU." }

# --------------------------------------------------------------------------
Say "Building the Python environment"
Push-Location $Root
if (-not (Test-Path "$Root\.venv")) { & $python -m venv .venv }
$py = "$Root\.venv\Scripts\python.exe"

& $py -m pip install --upgrade pip --quiet
& $py -m pip install --quiet pillow numpy

if ($gpu) {
    Say "Installing PyTorch with CUDA (large download — this takes a while)"
    & $py -m pip install --quiet torch torchaudio --index-url https://download.pytorch.org/whl/cu121
    Say "Installing Whisper, Demucs and EasyOCR"
    & $py -m pip install --quiet openai-whisper demucs easyocr
} else {
    Say "Installing CPU-only Whisper and Demucs"
    & $py -m pip install --quiet torch torchaudio
    & $py -m pip install --quiet openai-whisper demucs
    Warn "Without a GPU, EasyOCR is skipped; Tesseract would be needed for"
    Warn "reading lyrics off a video. Install it with:"
    Warn "  winget install UB-Mannheim.TesseractOCR"
}
Pop-Location

# --------------------------------------------------------------------------
Say "Writing worker configuration"
New-Item -ItemType Directory -Force -Path $SundayDir | Out-Null
$lines = @(
    "HOPEWELL_URL=$Url",
    "HOPEWELL_WORKER_TOKEN=$Token",
    "HOPEWELL_SUNDAY_DIR=$SundayDir"
)
if ($ffmpeg)  { $lines += "HOPEWELL_FFMPEG=$ffmpeg" }
if ($ffprobe) { $lines += "HOPEWELL_FFPROBE=$ffprobe" }
if ($ytdlp)   { $lines += "HOPEWELL_YTDLP=$ytdlp" }
$envFile = "$Root\worker\.env"
($lines -join "`r`n") | Set-Content -Path $envFile -Encoding UTF8
Good "$envFile"

# --------------------------------------------------------------------------
Say "Registering the startup task"
$taskName = "Hopewell Lyric Video Worker"
$logDir = "$Root\work\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Launch through run-hidden.vbs when this machine provides it, so nothing can
# flash a console window during a service.
$hidden = Join-Path (Split-Path -Parent $Root) "tools\run-hidden.vbs"
if (-not (Test-Path $hidden)) { $hidden = Join-Path $Root "tools\run-hidden.vbs" }

if (Test-Path $hidden) {
    Good "using run-hidden.vbs so no window can appear"
    $action = New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "`"$hidden`" `"$py`" `"$Root\worker\worker.py`"" `
        -WorkingDirectory $Root
} else {
    $action = New-ScheduledTaskAction -Execute "cmd.exe" `
        -Argument "/c `"`"$py`" `"$Root\worker\worker.py`" >> `"$logDir\worker.log`" 2>&1`"" `
        -WorkingDirectory $Root
}

$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 2) -RestartCount 999 `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
# SYSTEM, not the signed-in user: there is no auto-login here, so a task tied
# to the interactive session would sit idle after a restart until someone
# signed in — which might not happen until Sunday.
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Highest -User "SYSTEM" | Out-Null
Good "registered '$taskName' — starts at boot, no sign-in needed, restarts on failure"

# --------------------------------------------------------------------------
Say "Testing the connection"
& $py "$Root\worker\worker.py" --url $Url --token $Token --sunday-dir $SundayDir --once
if ($LASTEXITCODE -eq 0) { Good "connection OK" }
else { Warn "That test did not succeed — check the URL and token above." }

Say "Done"
Write-Host @"
    Start it now without rebooting:
        Start-ScheduledTask -TaskName "$taskName"

    Watch what it is doing:
        Get-Content "$logDir\worker.log" -Wait -Tail 30

    Finished videos appear in:
        $SundayDir

    It will not render during Sunday 10:40-12:30 or Wednesday 18:30-20:30, and
    will not use the GPU encoder at all while OBS is running.
"@

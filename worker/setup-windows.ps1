<#
    Sets up the render worker on the church PC (Windows + RTX 3060 Ti).

    Run once, from an ADMINISTRATOR PowerShell:

        cd C:\Hopewell\church-lyric-videos
        powershell -ExecutionPolicy Bypass -File worker\setup-windows.ps1 `
            -Url "https://lyrics.tristanaddi.com" -Token "<worker token>"

    Installs the tools, builds the Python environment with CUDA support, and
    registers a scheduled task so the worker starts with the machine and comes
    back on its own if it ever stops.
#>

param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Token,
    [string]$SundayDir = "$env:USERPROFILE\Hopewell Lyric Videos",
    [switch]$SkipTools
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Say($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "    ! $msg" -ForegroundColor Yellow }

Say "Hopewell render worker setup"
Write-Host "    project : $Root"
Write-Host "    output  : $SundayDir"

# --------------------------------------------------------------------------
if (-not $SkipTools) {
    Say "Installing tools with winget"
    # ffmpeg does the encoding, yt-dlp fetches sources, tesseract is the
    # fallback OCR engine when the GPU one is unavailable.
    foreach ($pkg in @(
        @{ id = "Python.Python.3.11";     name = "Python 3.11" },
        @{ id = "Gyan.FFmpeg";            name = "FFmpeg" },
        @{ id = "yt-dlp.yt-dlp";          name = "yt-dlp" },
        @{ id = "UB-Mannheim.TesseractOCR"; name = "Tesseract OCR" }
    )) {
        Write-Host "    $($pkg.name)…"
        winget install --id $pkg.id --silent --accept-source-agreements `
               --accept-package-agreements --disable-interactivity 2>$null
        if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {
            Warn "$($pkg.name) may already be installed (exit $LASTEXITCODE)"
        }
    }
    # Pick up anything the installers added to PATH.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

# --------------------------------------------------------------------------
Say "Checking the GPU"
$gpu = $null
try { $gpu = & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null } catch {}
if ($gpu) {
    Write-Host "    $gpu" -ForegroundColor Green
} else {
    Warn "No NVIDIA GPU detected. Renders will fall back to the CPU and be"
    Warn "considerably slower. Install the NVIDIA driver if this is wrong."
}

# --------------------------------------------------------------------------
Say "Building the Python environment"
Push-Location $Root
if (-not (Test-Path "$Root\.venv")) { python -m venv .venv }
$py = "$Root\.venv\Scripts\python.exe"

& $py -m pip install --upgrade pip --quiet
& $py -m pip install --quiet pillow numpy

if ($gpu) {
    # cu121 wheels match the driver line shipped with current 30-series cards.
    Say "Installing PyTorch with CUDA (large download, be patient)"
    & $py -m pip install --quiet torch torchaudio --index-url https://download.pytorch.org/whl/cu121
    Say "Installing Whisper, Demucs and EasyOCR"
    & $py -m pip install --quiet openai-whisper demucs easyocr
} else {
    Say "Installing CPU-only Whisper and Demucs"
    & $py -m pip install --quiet torch torchaudio
    & $py -m pip install --quiet openai-whisper demucs
}
Pop-Location

# --------------------------------------------------------------------------
Say "Writing worker configuration"
New-Item -ItemType Directory -Force -Path $SundayDir | Out-Null
$envFile = "$Root\worker\.env"
@(
    "HOPEWELL_URL=$Url",
    "HOPEWELL_WORKER_TOKEN=$Token",
    "HOPEWELL_SUNDAY_DIR=$SundayDir"
) -join "`r`n" | Set-Content -Path $envFile -Encoding UTF8
Write-Host "    $envFile"

# --------------------------------------------------------------------------
Say "Registering the startup task"
$taskName = "Hopewell Lyric Video Worker"
$logDir = "$Root\work\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# cmd wrapper so stdout/stderr land in a log the user can actually read.
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"`"$py`" `"$Root\worker\worker.py`" >> `"$logDir\worker.log`" 2>&1`"" `
    -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 2) -RestartCount 999 `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Highest -User "SYSTEM" | Out-Null
Write-Host "    registered '$taskName' (starts at boot, restarts on failure)"

# --------------------------------------------------------------------------
Say "Testing the connection to the dashboard"
& $py "$Root\worker\worker.py" --url $Url --token $Token --sunday-dir $SundayDir --once
if ($LASTEXITCODE -eq 0) {
    Write-Host "    connection OK" -ForegroundColor Green
} else {
    Warn "The test run did not succeed — check the URL and token above."
}

Say "Done"
Write-Host @"
    Start it now without rebooting:
        Start-ScheduledTask -TaskName "$taskName"

    Watch what it is doing:
        Get-Content "$logDir\worker.log" -Wait -Tail 30

    Finished videos appear in:
        $SundayDir
"@

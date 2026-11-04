<#
.SYNOPSIS
    Start, restart, stop or check the NADIEN services.

.DESCRIPTION
    Docker mode : docker compose (db, backend, frontend) - UI on :8080.
    Native mode : backend (uvicorn) in WSL Ubuntu with SQLite, frontend (vite) on Windows - UI on :5173.
    Mode "auto" uses Docker when its engine is running, otherwise native.

.EXAMPLE
    .\start-nadien.ps1                 # start whatever is down
    .\start-nadien.ps1 restart
    .\start-nadien.ps1 status
    .\start-nadien.ps1 stop -Mode native
#>
param(
    [ValidateSet("start", "restart", "stop", "status")]
    [string]$Action = "start",

    [ValidateSet("auto", "docker", "native")]
    [string]$Mode = "auto",

    [string]$Distro = "Ubuntu",
    [string]$DatabaseUrl = "sqlite:////root/nadien.db"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

$BackendPort = 8000
$FrontendPort = 5173
$LogDir = Join-Path $ProjectRoot "data\logs"
$NodeExe = "C:\Program Files\nodejs\node.exe"

function Test-DockerEngine {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
    & docker info *> $null
    return $LASTEXITCODE -eq 0
}

function Test-Url([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

function Wait-Url([string]$Url, [string]$Name, [int]$Seconds = 90) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Url $Url) {
            Write-Host "  $Name siap: $Url"
            return $true
        }
        Start-Sleep -Seconds 2
    }
    Write-Warning "$Name belum menjawab setelah $Seconds detik ($Url). Cek log di $LogDir"
    return $false
}

function Invoke-Wsl([string]$Command) {
    & wsl.exe -d $Distro -u root -- bash -lc $Command
}

# ------------------------------------------------------------------ docker mode

function Invoke-Docker {
    switch ($Action) {
        "start"   { & docker compose up --build --detach }
        "restart" { & docker compose up --build --force-recreate --detach }
        "stop"    { & docker compose down }
        "status"  { & docker compose ps }
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Error "docker compose gagal. Cek output di atas."
        exit $LASTEXITCODE
    }
    if ($Action -in "start", "restart") {
        Wait-Url "http://localhost:$BackendPort/api/health" "Backend" | Out-Null
        Wait-Url "http://localhost:8080" "UI" | Out-Null
        Write-Host ""
        Write-Host "NADIEN (Docker) berjalan:  UI http://localhost:8080   API http://localhost:$BackendPort/docs"
    }
}

# ------------------------------------------------------------------ native mode

function Start-Backend {
    if (Test-Url "http://localhost:$BackendPort/api/health") {
        Write-Host "  Backend sudah berjalan di :$BackendPort"
        return
    }
    $backendDir = (& wsl.exe -d $Distro -u root -- wslpath -a ($ProjectRoot -replace '\\', '/')).Trim() + "/backend"
    $logFile = (& wsl.exe -d $Distro -u root -- wslpath -a (($LogDir -replace '\\', '/') + "/backend.log")).Trim()
    Write-Host "  Menjalankan backend di WSL ($Distro)..."
    # A hidden wsl.exe stays attached to uvicorn, so the distro is not shut down while the API runs.
    $command = "cd '$backendDir' && exec env NADIEN_DATABASE_URL='$DatabaseUrl' ~/.nadien-venv/bin/uvicorn app.main:app " +
               "--host 0.0.0.0 --port $BackendPort >> '$logFile' 2>&1"
    Start-Process -FilePath "wsl.exe" -ArgumentList @("-d", $Distro, "-u", "root", "--", "bash", "-lc", "`"$command`"") `
        -WindowStyle Hidden | Out-Null
    Wait-Url "http://localhost:$BackendPort/api/health" "Backend" | Out-Null
}

function Start-Frontend {
    if (Test-Url "http://localhost:$FrontendPort") {
        Write-Host "  Frontend sudah berjalan di :$FrontendPort"
        return
    }
    if (-not (Test-Path -LiteralPath $NodeExe)) {
        $found = Get-Command node -ErrorAction SilentlyContinue
        if (-not $found) { Write-Error "Node.js tidak ditemukan. Install Node 20+ lalu jalankan lagi."; exit 1 }
        $script:NodeExe = $found.Source
    }
    $vite = Join-Path $ProjectRoot "frontend\node_modules\vite\bin\vite.js"
    if (-not (Test-Path -LiteralPath $vite)) {
        Write-Host "  frontend/node_modules belum ada, menjalankan npm install..."
        $env:Path = "$(Split-Path -Parent $NodeExe);$env:Path"
        Push-Location (Join-Path $ProjectRoot "frontend")
        & npm install
        Pop-Location
    }
    Write-Host "  Menjalankan frontend (vite)..."
    Start-Process -FilePath $NodeExe -WorkingDirectory $ProjectRoot -WindowStyle Hidden `
        -ArgumentList @("`"$vite`"", "frontend", "--port", $FrontendPort, "--strictPort") `
        -RedirectStandardOutput (Join-Path $LogDir "frontend.log") -RedirectStandardError (Join-Path $LogDir "frontend.err.log") | Out-Null
    Wait-Url "http://localhost:$FrontendPort" "Frontend" | Out-Null
}

function Stop-Backend {
    Write-Host "  Menghentikan backend..."
    Invoke-Wsl "pkill -f '[u]vicorn app.main:app' || true"
}

function Stop-Frontend {
    Write-Host "  Menghentikan frontend..."
    $owners = Get-NetTCPConnection -State Listen -LocalPort $FrontendPort -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($id in $owners) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
}

function Show-Status {
    $backend = Test-Url "http://localhost:$BackendPort/api/health"
    $frontend = Test-Url "http://localhost:$FrontendPort"
    Write-Host ("  Backend  :{0}  {1}" -f $BackendPort, $(if ($backend) { "UP" } else { "DOWN" }))
    Write-Host ("  Frontend :{0}  {1}" -f $FrontendPort, $(if ($frontend) { "UP" } else { "DOWN" }))
    return $backend -and $frontend
}

function Invoke-Native {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    switch ($Action) {
        "status" {
            $ok = Show-Status
            exit $(if ($ok) { 0 } else { 1 })
        }
        "stop" {
            Stop-Frontend
            Stop-Backend
        }
        "restart" {
            Stop-Frontend
            Stop-Backend
            Start-Sleep -Seconds 2
            Start-Backend
            Start-Frontend
        }
        "start" {
            Start-Backend
            Start-Frontend
        }
    }
    Write-Host ""
    $ok = Show-Status
    if ($Action -ne "stop") {
        Write-Host ""
        Write-Host "NADIEN (native):  UI http://localhost:$FrontendPort   API http://localhost:$BackendPort/docs"
        Write-Host "Log: $LogDir"
        if (-not $ok) { exit 1 }
    }
}

# ------------------------------------------------------------------ main

if ($Mode -eq "auto") {
    $Mode = if (Test-DockerEngine) { "docker" } else { "native" }
}
Write-Host "NADIEN $Action (mode $Mode)"
if ($Mode -eq "docker") {
    if (-not (Test-DockerEngine)) {
        Write-Error "Docker engine tidak berjalan. Start Docker Desktop, atau pakai -Mode native."
        exit 1
    }
    Invoke-Docker
} else {
    Invoke-Native
}

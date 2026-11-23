<#
.SYNOPSIS
    One shot from Windows: make sure MicroK8s runs in WSL with an open Docker registry.

.DESCRIPTION
    Runs setup/microk8s.sh inside a WSL distro. The script checks systemd, installs
    MicroK8s if missing, enables dns, hostpath-storage and the registry addon (port 32000,
    no login: anyone can push), applies the engine RBAC and prints the registry URL.
    It is idempotent: run it again at any time to check the cluster.

.EXAMPLE
    .\setup\setup.ps1
    .\setup\setup.ps1 -Distro Ubuntu -BuildWorker
    .\setup\setup.ps1 -RestartWsl      # allow restarting WSL when systemd must be enabled
#>
[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu",
    [string]$Channel = "1.31/stable",
    [string]$RegistrySize = "20Gi",
    [switch]$BuildWorker,
    [switch]$RestartWsl
)
$ErrorActionPreference = "Stop"

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw "WSL is not installed. Run 'wsl --install' (as administrator), reboot, then run this script again."
}

# wsl.exe -l prints UTF-16; strip the NULs before matching names.
$distros = (wsl.exe -l -q) -replace "`0", "" | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() }
if ($distros -notcontains $Distro) {
    throw "WSL distro '$Distro' not found. Installed: $($distros -join ', '). Use -Distro <name> or 'wsl --install -d Ubuntu'."
}

$script = Join-Path $PSScriptRoot "microk8s.sh"
$wslScript = (wsl.exe -d $Distro -- wslpath -a ($script -replace '\\', '/')).Trim()
$shArgs = @("--channel", $Channel, "--registry-size", $RegistrySize)
if ($BuildWorker) { $shArgs += "--build-worker" }

function Invoke-Setup {
    # tr strips CRLF in case the checkout converted line endings
    $repoRoot = $wslScript -replace '/setup/microk8s\.sh$', ''
    wsl.exe -d $Distro -- bash -c "tr -d '\r' < '$wslScript' > /tmp/aje-microk8s.sh && AJE_REPO_ROOT='$repoRoot' bash /tmp/aje-microk8s.sh $($shArgs -join ' ')"
    return $LASTEXITCODE
}

Write-Host "Running MicroK8s setup in WSL distro '$Distro'..." -ForegroundColor Cyan
$code = Invoke-Setup

if ($code -eq 10) {
    if (-not $RestartWsl) {
        Write-Host ""
        Write-Host "systemd was enabled in '$Distro'. WSL must restart, which stops every running distro" -ForegroundColor Yellow
        Write-Host "(including Docker in WSL). Run again with -RestartWsl, or run 'wsl --shutdown' yourself." -ForegroundColor Yellow
        exit 10
    }
    Write-Host "Restarting WSL to enable systemd..." -ForegroundColor Yellow
    wsl.exe --shutdown
    Start-Sleep -Seconds 3
    $code = Invoke-Setup
}
exit $code

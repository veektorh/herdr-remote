[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu",
    [string]$TaskName = "Herdr Remote WSL",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$startupDirectory = [Environment]::GetFolderPath("Startup")
$startupFile = Join-Path $startupDirectory "herdr-remote-wsl.cmd"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item $startupFile -Force -ErrorAction SilentlyContinue
    Write-Host "Removed startup task: $TaskName"
    Write-Host "Removed startup command: $startupFile"
    exit 0
}

$wsl = Join-Path $env:SystemRoot "System32\wsl.exe"
if (-not (Test-Path $wsl)) {
    throw "wsl.exe was not found. Install WSL before creating the startup task."
}
if ($Distro -notmatch '^[A-Za-z0-9._-]+$') {
    throw "Distro may contain only letters, numbers, dots, underscores, and hyphens."
}

$linuxCommand = 'if systemctl --user show-environment >/dev/null 2>&1; then systemctl --user start herdr-relay.service; else source "$HOME/.config/herdr-remote/config.env" && "$HERDR_RELAY_DIR/service.sh" start; fi; tailscale serve status >/dev/null 2>&1 || true'
$arguments = "-d $Distro --exec bash -lc `"$linuxCommand`""
$action = New-ScheduledTaskAction -Execute $wsl -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "Start WSL, herdr-remote, and persistent Tailscale Serve at Windows sign-in." `
        -Force `
        -ErrorAction Stop | Out-Null

    Remove-Item $startupFile -Force -ErrorAction SilentlyContinue
    Write-Host "Installed startup task: $TaskName"
    Write-Host "Run it now with: Start-ScheduledTask -TaskName '$TaskName'"
} catch {
    Write-Warning "Task Scheduler registration was denied; using the per-user Startup folder."
    $startupCommand = "@echo off`r`n`"$wsl`" $arguments`r`n"
    Set-Content -Path $startupFile -Value $startupCommand -Encoding Ascii -Force
    Write-Host "Installed startup command: $startupFile"
}

Write-Host "WSL distribution: $Distro"

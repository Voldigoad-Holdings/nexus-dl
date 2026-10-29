#Requires -Version 5.1
<#
.SYNOPSIS
    NexusDL Docker entrypoint (Windows / PowerShell).

.DESCRIPTION
    Responsibilities:
      1. Verify that volume directories exist and are writable.
      2. Fix permissions if needed (icacls).
      3. Display a config summary (without secrets).
      4. Run the provided command and propagate its exit code.

    This script is the PowerShell equivalent of `entrypoint.sh` for
    Windows containers. It is used by `Dockerfile` (Windows variant)
    and works with Windows Server Core, Nano Server, and Windows 10/11
    containers.

.PARAMETER Command
    The command to execute (e.g. `nexusdl`, `nexusdl-gui`).

.EXAMPLE
    powershell -File entrypoint.ps1 nexusdl --version

.NOTES
    Author: Voldigoad-Holdings
    License: GPL-3.0-or-later
    Encoding: UTF-8 (with BOM for PowerShell 5.1 compatibility)

    ⚠️  Never log secrets.
    ⚠️  Exit code of the child process is propagated.
    ⚠️  On Ctrl+C (SIGINT), the child process is terminated gracefully.
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Command
)

# ────────────────────────────────────────────────────────────────────
# Strict mode (fail on errors, undefined variables, bad pipes)
# ────────────────────────────────────────────────────────────────────
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ────────────────────────────────────────────────────────────────────
# UTF-8 console output (accents, emojis)
# ────────────────────────────────────────────────────────────────────
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # Ignore — not critical
}

# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────
$Script:RequiredDirs = @(
    $env:NEXUSDL_CONFIG_DIR
    $env:NEXUSDL_DATA_DIR
    $env:NEXUSDL_DOWNLOAD_DIR
) | Where-Object { $_ -and $_.Trim() -ne '' }

$Script:VerboseOutput = $env:NEXUSDL_ENTRYPOINT_VERBOSE -eq 'true'

# ────────────────────────────────────────────────────────────────────
# Helper functions
# ────────────────────────────────────────────────────────────────────

function Write-EntryLog {
    param(
        [string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR')]
        [string]$Level = 'INFO'
    )
    $timestamp = Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ'
    $prefix = "[$timestamp] [entrypoint] [$Level]"
    switch ($Level) {
        'ERROR' { Write-Error "$prefix $Message" }
        'WARN'  { Write-Warning "$prefix $Message" }
        default { Write-Output "$prefix $Message" }
    }
}

function Test-DirectoryWritable {
    param([string]$Path)

    if (-not (Test-Path -Path $Path -PathType Container)) {
        return $false
    }
    try {
        $testFile = Join-Path -Path $Path -ChildPath ".write-test-$([guid]::NewGuid().ToString('N').Substring(0,8))"
        [System.IO.File]::WriteAllText($testFile, 'test')
        Remove-Item -Path $testFile -Force -ErrorAction SilentlyContinue
        return $true
    } catch {
        return $false
    }
}

function Grant-DirectoryPermission {
    param([string]$Path)

    # Requires admin. If not admin, fail with a clear message.
    $isAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if (-not $isAdmin) {
        Write-EntryLog "Cannot fix permissions on '$Path' (not admin)." -Level 'ERROR'
        Write-EntryLog "Fix on host: icacls `"$Path`" /grant `"Everyone:(OI)(CI)F`" /T" -Level 'ERROR'
        return $false
    }

    try {
        & icacls.exe $Path /grant "Everyone:(OI)(CI)F" /T /Q | Out-Null
        return $true
    } catch {
        Write-EntryLog "icacls failed on '$Path': $_" -Level 'ERROR'
        return $false
    }
}

# ────────────────────────────────────────────────────────────────────
# 1. Verify volumes
# ────────────────────────────────────────────────────────────────────
foreach ($dir in $Script:RequiredDirs) {
    if (-not (Test-Path -Path $dir -PathType Container)) {
        Write-EntryLog "Directory missing, creating: $dir" -Level 'WARN'
        try {
            New-Item -Path $dir -ItemType Directory -Force | Out-Null
        } catch {
            Write-EntryLog "Cannot create '$dir': $_" -Level 'ERROR'
            exit 1
        }
    }

    if (-not (Test-DirectoryWritable -Path $dir)) {
        Write-EntryLog "Directory not writable: $dir" -Level 'WARN'
        if (Grant-DirectoryPermission -Path $dir) {
            Write-EntryLog "Fixed permissions on: $dir" -Level 'INFO'
        } else {
            exit 1
        }
    }
}

# ────────────────────────────────────────────────────────────────────
# 2. Verify configuration
# ────────────────────────────────────────────────────────────────────
$configEnv = Join-Path -Path $env:NEXUSDL_CONFIG_DIR -ChildPath '.env'
if (-not (Test-Path -Path $configEnv -PathType Leaf)) {
    Write-EntryLog "No .env found at $configEnv" -Level 'WARN'
    Write-EntryLog "NexusDL requires NEXUSDL_SECRET_KEY, NEXUSDL_JWT_SECRET," -Level 'WARN'
    Write-EntryLog "NEXUSDL_COOKIE_ENCRYPTION_KEY. Mount a .env or pass them as env vars." -Level 'WARN'
    Write-EntryLog "Continuing anyway (will fail on first command)." -Level 'WARN'
}

# ────────────────────────────────────────────────────────────────────
# 3. Verify secrets are set (non-empty) — do NOT log values
# ────────────────────────────────────────────────────────────────────
$requiredSecrets = @(
    'NEXUSDL_SECRET_KEY'
    'NEXUSDL_JWT_SECRET'
    'NEXUSDL_COOKIE_ENCRYPTION_KEY'
)
foreach ($secretName in $requiredSecrets) {
    $value = [Environment]::GetEnvironmentVariable($secretName)
    if ([string]::IsNullOrWhiteSpace($value)) {
        Write-EntryLog "$secretName is not set or empty." -Level 'WARN'
    }
}

# ────────────────────────────────────────────────────────────────────
# 4. Display summary (if verbose)
# ────────────────────────────────────────────────────────────────────
if ($Script:VerboseOutput) {
    $version = 'unknown'
    try {
        $version = (& nexusdl --version 2>$null) -join ' '
    } catch {
        # ignore
    }
    Write-EntryLog "NexusDL $version"
    Write-EntryLog "User:      $env:USERNAME (admin: $(([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)))"
    Write-EntryLog "Config:    $env:NEXUSDL_CONFIG_DIR"
    Write-EntryLog "Data:      $env:NEXUSDL_DATA_DIR"
    Write-EntryLog "Downloads: $env:NEXUSDL_DOWNLOAD_DIR"
    Write-EntryLog "Env:       $env:NEXUSDL_APP_ENVIRONMENT"
    Write-EntryLog "Command:   $($Command -join ' ')"
}

# ────────────────────────────────────────────────────────────────────
# 5. Run the command
# ────────────────────────────────────────────────────────────────────
if ($Command.Count -eq 0) {
    Write-EntryLog "No command provided. Defaulting to 'nexusdl --help'." -Level 'WARN'
    $Command = @('nexusdl', '--help')
}

$exe = $Command[0]
$exeArgs = @()
if ($Command.Count -gt 1) {
    $exeArgs = $Command[1..($Command.Count - 1)]
}

Write-EntryLog "Executing: $exe $($exeArgs -join ' ')"

# Graceful Ctrl+C handling
$ctrlCPressed = $false
$childProcess = $null

try {
    # Use Start-Process to get a real Process object (for Ctrl+C handling)
    if ($exeArgs.Count -gt 0) {
        $childProcess = Start-Process -FilePath $exe -ArgumentList $exeArgs -NoNewWindow -PassThru -Wait
    } else {
        $childProcess = Start-Process -FilePath $exe -NoNewWindow -PassThru -Wait
    }

    $exitCode = $childProcess.ExitCode
    if ($null -eq $exitCode) {
        $exitCode = 0
    }
} catch {
    Write-EntryLog "Failed to execute '$exe': $_" -Level 'ERROR'
    exit 1
}

Write-EntryLog "Process exited with code $exitCode"
exit $exitCode

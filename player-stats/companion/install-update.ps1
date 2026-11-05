param(
    [Parameter(Mandatory = $true)][string]$Target,
    [Parameter(Mandatory = $true)][string]$Staged,
    [Parameter(Mandatory = $true)][string]$Sha256,
    [Parameter(Mandatory = $true)][int]$ParentProcessId,
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [string]$RestartArguments
)
$ErrorActionPreference = 'Stop'
$targetPath = [IO.Path]::GetFullPath($Target)
$stagedPath = [IO.Path]::GetFullPath($Staged)
$updateDirectory = Join-Path ([IO.Path]::GetDirectoryName($targetPath)) '.sd2-companion-updates'
$backupPath = $targetPath + '.previous'
$logPath = Join-Path $updateDirectory 'update.log'
try {
    if ([IO.Path]::GetDirectoryName($stagedPath) -ne $updateDirectory -or
        [IO.Path]::GetExtension($targetPath) -ne '.exe' -or
        $Sha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'invalid_update_paths'
    }
    $parentProcess = Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue
    if ($null -ne $parentProcess -and -not $parentProcess.WaitForExit(60000)) {
        throw 'update_wait_timeout'
    }
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $inputStream = [IO.File]::OpenRead($stagedPath)
    try {
        $actualHash = [BitConverter]::ToString($algorithm.ComputeHash($inputStream)).Replace('-', '').ToLowerInvariant()
    } finally {
        $inputStream.Dispose()
        $algorithm.Dispose()
    }
    if ($actualHash -ne $Sha256) {
        throw 'update_integrity_failed'
    }
    $replaced = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            if (Test-Path -LiteralPath $backupPath) { Remove-Item -LiteralPath $backupPath }
            [IO.File]::Replace($stagedPath, $targetPath, $backupPath)
            $replaced = $true
            break
        } catch [IO.IOException] {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $replaced) { throw 'update_replace_failed' }
    try {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $targetPath
        $startInfo.WorkingDirectory = [IO.Path]::GetDirectoryName($targetPath)
        $startInfo.Arguments = if ($RestartArguments) { $RestartArguments } else { 'desktop --config "' + $ConfigPath + '"' }
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        # The new one-file application must extract its own resources after the
        # old bootloader has removed its temporary directory.
        $startInfo.EnvironmentVariables['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        [void][Diagnostics.Process]::Start($startInfo)
        'update_installed' | Set-Content -LiteralPath $logPath -Encoding UTF8
    } catch {
        [IO.File]::Replace($backupPath, $targetPath, $null)
        throw 'update_restart_failed_rolled_back'
    }
} catch {
    # Diagnostic codes only; report payloads and credentials never enter this log.
    ('update_failed: ' + $_.Exception.GetType().Name) | Set-Content -LiteralPath $logPath -Encoding UTF8
    exit 1
}

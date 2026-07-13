$ErrorActionPreference = "Stop"

$toolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$userRoot = Join-Path $env:APPDATA "LiveClipper"
$resultPath = Join-Path $toolRoot "repair_result.txt"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupRoot = Join-Path $userRoot "legacy_runtime_backup\$stamp"
$resultLines = New-Object System.Collections.Generic.List[string]

try {
    $resolvedUserRoot = [IO.Path]::GetFullPath($userRoot)
    $expectedUserRoot = [IO.Path]::GetFullPath((Join-Path $env:APPDATA "LiveClipper"))
    if ($resolvedUserRoot -ne $expectedUserRoot) {
        throw "Unexpected user data directory: $resolvedUserRoot"
    }

    $resultLines.Add("User data: $userRoot")
    $resultLines.Add("Started: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
    $moved = 0
    foreach ($name in @("app", "web_client", "tools")) {
        $source = Join-Path $userRoot $name
        if (-not (Test-Path -LiteralPath $source)) {
            continue
        }
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        $destination = Join-Path $backupRoot $name
        Move-Item -LiteralPath $source -Destination $destination
        $resultLines.Add("Archived legacy program directory: $source -> $destination")
        $moved += 1
    }

    $resultLines.Add("Archived directories: $moved")
    $resultLines.Add("Status: SUCCESS")
    $resultLines.Add("Next step: start the new full package. User settings were not moved.")
    $resultLines | Set-Content -LiteralPath $resultPath -Encoding UTF8
    Write-Host "Legacy program files were archived. Start the new full package now." -ForegroundColor Green
    exit 0
}
catch {
    $resultLines.Add("Status: FAILED")
    $resultLines.Add("Error: $($_.Exception.Message)")
    $resultLines | Set-Content -LiteralPath $resultPath -Encoding UTF8
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}

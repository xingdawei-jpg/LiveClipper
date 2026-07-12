$ErrorActionPreference = "Stop"

$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$payloadRoot = Join-Path $packageRoot "payload"
$targetRoot = Join-Path $env:APPDATA "LiveClipper"
$resultPath = Join-Path $packageRoot "repair_result.txt"
$resultLines = New-Object System.Collections.Generic.List[string]

try {
    $payloadVersion = Join-Path $payloadRoot "app\version.json"
    if (-not (Test-Path -LiteralPath $payloadVersion)) {
        throw "Recovery payload is incomplete: app/version.json is missing."
    }

    $resultLines.Add("Target: $targetRoot")
    $resultLines.Add("Started: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")

    Write-Host "Closing the process listening on port 8765..."
    $listenerPids = @(
        Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
    foreach ($listenerPid in $listenerPids) {
        Stop-Process -Id $listenerPid -Force -ErrorAction SilentlyContinue
        $resultLines.Add("Stopped PID: $listenerPid")
    }
    Get-Process -Name "LiveClipperWeb" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2

    $resolvedTarget = [IO.Path]::GetFullPath($targetRoot)
    $expectedRoot = [IO.Path]::GetFullPath((Join-Path $env:APPDATA "LiveClipper"))
    if ($resolvedTarget -ne $expectedRoot) {
        throw "Unexpected target directory: $resolvedTarget"
    }

    Write-Host "Copying and verifying recovery files..."
    $verified = 0
    $payloadFiles = @(Get-ChildItem -LiteralPath $payloadRoot -Recurse -File)
    foreach ($sourceFile in $payloadFiles) {
        $relative = $sourceFile.FullName.Substring($payloadRoot.Length).TrimStart('\')
        $targetFile = Join-Path $targetRoot $relative
        $targetDir = Split-Path -Parent $targetFile
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        Copy-Item -LiteralPath $sourceFile.FullName -Destination $targetFile -Force

        $sourceHash = (Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash
        $targetHash = (Get-FileHash -LiteralPath $targetFile -Algorithm SHA256).Hash
        if ($sourceHash -ne $targetHash) {
            throw "Hash mismatch after copy: $relative"
        }
        $verified += 1
        $resultLines.Add("OK: $relative $targetHash")
    }

    foreach ($cacheDir in @(
        (Join-Path $targetRoot "app\__pycache__"),
        (Join-Path $targetRoot "web_client\__pycache__")
    )) {
        if (Test-Path -LiteralPath $cacheDir) {
            Remove-Item -LiteralPath $cacheDir -Recurse -Force
        }
    }

    $serverPath = Join-Path $targetRoot "web_client\server.py"
    if (-not (Select-String -LiteralPath $serverPath -Pattern "runtime_integrity" -Quiet)) {
        throw "The restored server.py does not contain runtime_integrity."
    }

    $resultLines.Add("Verified files: $verified")
    $resultLines.Add("Status: SUCCESS")
    $resultLines | Set-Content -LiteralPath $resultPath -Encoding UTF8
    Write-Host "Verified $verified files in $targetRoot" -ForegroundColor Green
    exit 0
}
catch {
    $resultLines.Add("Status: FAILED")
    $resultLines.Add("Error: $($_.Exception.Message)")
    $resultLines | Set-Content -LiteralPath $resultPath -Encoding UTF8
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}

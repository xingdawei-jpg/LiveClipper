param([string]$InstallRoot, [switch]$CheckOnly)

$ErrorActionPreference = 'Stop'

function Get-SafeFiles([string]$Root) {
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($Root)
    while ($pending.Count -gt 0) {
        $directory = Get-Item -LiteralPath $pending.Pop() -Force
        if ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Linked directories are not supported: $($directory.FullName)"
        }
        foreach ($item in Get-ChildItem -LiteralPath $directory.FullName -Force) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Linked files or directories are not supported: $($item.FullName)"
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) } else { $item }
        }
    }
}

function Get-FileSha256([string]$Path) {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return (($algorithm.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) -join '')
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

try {
    if (-not $InstallRoot) {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = 'Select the LiveClipper folder containing LiveClipperWeb.exe and current.json'
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 2 }
        $InstallRoot = $dialog.SelectedPath
        $dialog.Dispose()
    }
    $root = (Get-Item -LiteralPath $InstallRoot -Force).FullName.TrimEnd('\')
    if (-not (Test-Path -LiteralPath (Join-Path $root 'LiveClipperWeb.exe') -PathType Leaf)) {
        throw 'Select the installation folder containing LiveClipperWeb.exe.'
    }
    $ancestor = Get-Item -LiteralPath $root -Force
    while ($null -ne $ancestor) {
        if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked installation paths are not supported.' }
        $ancestor = $ancestor.Parent
    }
    foreach ($process in Get-Process -Name 'LiveClipper*' -ErrorAction SilentlyContinue) {
        $processPath = $process.Path
        if ($processPath -and $processPath.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Close LiveClipper and its error dialog before running the recovery tool.'
        }
    }
    $state = Get-Content -LiteralPath (Join-Path $root 'current.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $version = [string]$state.current.application_version
    if ($state.schema_version -ne 1 -or $state.runtime_layout_version -ne 4 -or
        $state.current.core_version -ne '4.0.0' -or $version -notin @('2026.9.3.2', '2026.9.3.3')) {
        throw 'This tool only repairs Runtime V4 2026.9.3.2 and 2026.9.3.3 with Core 4.0.0.'
    }
    $bundle = Join-Path $root "versions\$version\business"
    foreach ($directory in @((Join-Path $root 'versions'), (Split-Path -Parent $bundle), $bundle)) {
        if ((Get-Item -LiteralPath $directory -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Linked version directories are not supported.'
        }
    }
    $manifest = Get-Content -LiteralPath (Join-Path $bundle 'bundle_manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $expected = @{}
    foreach ($entry in $manifest.files.PSObject.Properties) {
        $relative = $entry.Name
        if ($relative -match '(^/|\\|:|(^|/)\.\.(/|$))' -or $relative.StartsWith('workspace/')) {
            throw 'The bundle manifest is not compatible with this recovery tool.'
        }
        $expected[$relative] = $entry.Value
    }
    if ($expected.Count -eq 0) { throw 'The bundle manifest is empty.' }
    $prefix = 'workspace/ui_commerce_director_experiment/'
    $recovered = @()
    foreach ($file in @(Get-SafeFiles $bundle)) {
        $relative = $file.FullName.Substring($bundle.Length + 1).Replace('\', '/')
        if ($relative -in @('bundle_manifest.json', 'bundle_manifest.sig')) { continue }
        if ($expected.ContainsKey($relative)) {
            $digest = Get-FileSha256 $file.FullName
            if ($file.Length -ne $expected[$relative].size -or $digest -ne $expected[$relative].sha256) {
                throw "A program file has changed; recovery stopped: $relative"
            }
        } elseif ($relative.StartsWith($prefix, [StringComparison]::Ordinal)) {
            $recovered += [pscustomobject]@{ path = $relative.Substring($prefix.Length); sha256 = (Get-FileSha256 $file.FullName) }
        } else {
            throw "Unexpected file outside the director workspace; recovery stopped: $relative"
        }
    }
    foreach ($relative in $expected.Keys) {
        if (-not (Test-Path -LiteralPath (Join-Path $bundle $relative) -PathType Leaf)) { throw "Missing program file: $relative" }
    }
    $source = [IO.Path]::GetFullPath((Join-Path $bundle 'workspace\ui_commerce_director_experiment'))
    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
        Write-Host 'No misplaced director workspace was found. Start LiveClipperWeb.exe and check for updates.'
        exit 0
    }
    if ($CheckOnly) { Write-Host "Ready to back up $($recovered.Count) director output files. No files were changed."; exit 0 }
    $recoveryRoot = Join-Path $root 'recovery'
    if (Test-Path -LiteralPath $recoveryRoot) {
        if ((Get-Item -LiteralPath $recoveryRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked backup directories are not supported.' }
    }
    $stamp = (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + [guid]::NewGuid().ToString('N').Substring(0, 8)
    $receiptRoot = [IO.Path]::GetFullPath((Join-Path $recoveryRoot "director-workspace-$version-$stamp"))
    $destination = Join-Path $receiptRoot 'ui_commerce_director_experiment'
    if (-not $source.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase) -or
        -not $destination.StartsWith($root + '\recovery\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Recovery path validation failed.' }
    New-Item -ItemType Directory -Path $receiptRoot | Out-Null
    Move-Item -LiteralPath $source -Destination $destination
    foreach ($file in $recovered) {
        if ((Get-FileSha256 (Join-Path $destination $file.path)) -ne $file.sha256) {
            throw "Backup verification failed. Preserve the backup at: $destination"
        }
    }
    [pscustomobject]@{ version = $version; source = $source; backup = $destination; files = $recovered; repaired_at = (Get-Date -Format o) } |
        ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $receiptRoot 'recovery_receipt.json') -Encoding UTF8
    Write-Host "Recovered $($recovered.Count) director output files. Backup: $destination" -ForegroundColor Green
    Write-Host 'Start the installation-root LiveClipperWeb.exe, then update to 2026.9.4.1.'
    Write-Host 'Your settings and authorization were not moved. Previous director outputs are preserved in the backup.'
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}

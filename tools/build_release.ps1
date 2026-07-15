$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$releaseRoot = Join-Path $root "release_dist"
$runtimeNodeDir = Join-Path $root "runtime\node"
$tempRoot = Join-Path $env:TEMP "stackup_studio_release"
$workPath = Join-Path $tempRoot "build"
$distPath = Join-Path $tempRoot "dist"

function Find-PythonCommand {
    $override = $env:STACKUP_EDITOR_PYTHON
    if ($override -and (Test-Path $override)) {
        return @{
            Path = (Resolve-Path $override).Path
            PrefixArgs = @()
        }
    }

    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        return @{
            Path = $pyLauncher.Source
            PrefixArgs = @("-3")
        }
    }

    $pythonCandidates = @(
        (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        (Join-Path $HOME ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
    ) | Where-Object { $_ }

    foreach ($candidate in $pythonCandidates) {
        if (Test-Path $candidate) {
            return @{
                Path = (Resolve-Path $candidate).Path
                PrefixArgs = @()
            }
        }
    }

    return $null
}

function Find-NodeExecutable {
    $override = $env:STACKUP_EDITOR_NODE
    if ($override -and (Test-Path $override)) {
        return (Resolve-Path $override).Path
    }

    $candidates = @(
        (Get-Command node -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        "C:\Program Files\nodejs\node.exe",
        "C:\Program Files (x86)\nodejs\node.exe",
        (Join-Path $HOME "AppData\Local\Programs\nodejs\node.exe"),
        (Join-Path $HOME ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe")
    ) | Where-Object { $_ }

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    return $null
}

$pythonCommand = Find-PythonCommand
if (-not $pythonCommand) {
    throw "A Python executable could not be found. Install Python or set STACKUP_EDITOR_PYTHON first."
}

$nodeExe = Find-NodeExecutable
if (-not $nodeExe) {
    throw "node.exe could not be found. Install Node.js or set STACKUP_EDITOR_NODE first."
}

New-Item -ItemType Directory -Force -Path $runtimeNodeDir | Out-Null
Copy-Item -LiteralPath $nodeExe -Destination (Join-Path $runtimeNodeDir "node.exe") -Force

$impedanceTemplatePath = Join-Path $root "TransmissionLineTemp.xlsx"
if (-not (Test-Path $impedanceTemplatePath)) {
    Write-Warning "TransmissionLineTemp.xlsx was not found. The release will build, but Export Impedance Table may not work until that template is restored."
}

New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
$finalApp = Join-Path $releaseRoot "StackUp Editor"
$zipPath = Join-Path $releaseRoot "StackUp_Editor_Windows.zip"
if (Test-Path $finalApp) {
    try {
        Remove-Item -LiteralPath $finalApp -Recurse -Force
    }
    catch {
        $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $finalApp = Join-Path $releaseRoot ("StackUp Editor " + $stamp)
        $zipPath = Join-Path $releaseRoot ("StackUp_Editor_Windows_" + $stamp + ".zip")
    }
}
if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

if (Test-Path $tempRoot) {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $workPath | Out-Null
New-Item -ItemType Directory -Force -Path $distPath | Out-Null

Push-Location $root
try {
    $env:STACKUP_EDITOR_CONSOLE = "0"
    & $pythonCommand.Path @($pythonCommand.PrefixArgs + @(
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        $distPath,
        "--workpath",
        $workPath,
        "main.spec"
    ))
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }

    $builtApp = Join-Path $distPath "StackUp Editor"
    if (-not (Test-Path $builtApp)) {
        throw "PyInstaller completed but the output folder was not created."
    }

    Copy-Item -LiteralPath $builtApp -Destination $finalApp -Recurse -Force

    $releaseReadme = @"
StackUp Editor portable Windows release

How to run
- Open the folder.
- Launch: StackUp Editor.exe

Included runtimes
- Python modules are frozen into the application by PyInstaller.
- Node.js is bundled for the field solver at runtime\node\node.exe.

Important
- Keep the full folder structure together. Do not move only the .exe file.
- If Windows SmartScreen or antivirus asks, allow the app after verifying the source.
- No separate Python or Node.js installation is required on the target computer.
"@
    if (-not (Test-Path $impedanceTemplatePath)) {
        $releaseReadme += "`r`nMissing optional asset`r`n- TransmissionLineTemp.xlsx was not bundled, so Export Impedance Table may be unavailable in this build.`r`n"
    }
    Set-Content -LiteralPath (Join-Path $finalApp "README_RELEASE.txt") -Value $releaseReadme -Encoding UTF8

    Compress-Archive -Path $finalApp -DestinationPath $zipPath

    Write-Host ""
    Write-Host "Release created:"
    Write-Host "  Folder: $finalApp"
    Write-Host "  Zip:    $zipPath"
    Write-Host "  Python: $($pythonCommand.Path)"
    Write-Host "  Node:   $nodeExe"
}
finally {
    Pop-Location
}

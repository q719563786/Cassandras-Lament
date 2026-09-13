param(
    [switch]$Describe
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRequirements = Join-Path $projectRoot "requirements.txt"
$buildRequirements = Join-Path $projectRoot "requirements-build.txt"
$venvRoot = Join-Path $projectRoot ".venv-build"
$pythonExe = Join-Path $venvRoot "Scripts\python.exe"
$specFile = Join-Path $PSScriptRoot "yuanjian.spec"
$workPath = Join-Path $projectRoot "build-artifacts"
$distPath = Join-Path $projectRoot "dist"

function Read-PinnedRequirements {
    param([string]$Path)
    $items = @()
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        $text = $line.Trim()
        # Skip blanks, comments and pip flags such as "-r requirements.txt".
        if ($text.Length -eq 0) { continue }
        if ($text.StartsWith("#")) { continue }
        if ($text.StartsWith("-")) { continue }
        $items += $text
    }
    return $items
}

# Dependencies come from the requirements files, not from a hardcoded list here,
# so there is exactly one place to bump a version. requirements-build.txt is read
# first because it pins PyInstaller, which is listed first in the build contract.
$dependencies = @(Read-PinnedRequirements $buildRequirements) + @(Read-PinnedRequirements $runtimeRequirements)

if ($Describe) {
    [pscustomobject]@{
        Dependencies = $dependencies
        Gui = "edgechromium"
    } | ConvertTo-Json -Compress
    exit 0
}

if (-not (Test-Path -LiteralPath $pythonExe)) {
    py -3 -m venv $venvRoot
}

# requirements-build.txt includes requirements.txt, so this installs both layers.
& $pythonExe -m pip install --disable-pip-version-check -r $buildRequirements
if ($LASTEXITCODE -ne 0) { throw "Build dependency installation failed" }

& $pythonExe -m PyInstaller --noconfirm --clean --workpath $workPath --distpath $distPath $specFile
if ($LASTEXITCODE -ne 0) { throw "YuanJian Windows build failed" }

$exePath = Join-Path $distPath "YuanJian\YuanJian.exe"
if (-not (Test-Path -LiteralPath $exePath)) { throw "YuanJian.exe was not found" }
Write-Output $exePath

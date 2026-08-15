param(
    [string]$ExePath,
    [switch]$Describe
)

$ErrorActionPreference = "Stop"
$headlessEnvironment = "YUANJIAN_HEADLESS"
$headlessValue = "1"
$defaultView = "today"
$moduleEntry = "/js/app.js"

if ($Describe) {
    [pscustomobject]@{
        HeadlessEnvironment = $headlessEnvironment
        HeadlessValue = $headlessValue
        DefaultView = $defaultView
        ModuleEntry = $moduleEntry
    } | ConvertTo-Json -Compress
    exit 0
}

if ([string]::IsNullOrWhiteSpace($ExePath)) {
    throw "ExePath is required"
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$smokeRoot = Join-Path $projectRoot "smoke-runtime-v08"

if (Test-Path -LiteralPath $smokeRoot) {
    throw "Smoke target already exists"
}

New-Item -ItemType Directory -Path $smokeRoot | Out-Null
$env:YUANJIAN_DATA_DIR = $smokeRoot
$env:YUANJIAN_HEADLESS = $headlessValue
$env:YUANJIAN_BACKGROUND = "1"
$process = Start-Process -FilePath $ExePath -ArgumentList "--background" -PassThru -WindowStyle Hidden

try {
    $database = Join-Path $smokeRoot "data\yuanjian.db"
    $runtimeFile = Join-Path $smokeRoot "runtime\runtime.json"
    $ready = $false
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        $process.Refresh()
        if ($process.HasExited) {
            throw "Packaged EXE exited early with code $($process.ExitCode)"
        }
        if ((Test-Path -LiteralPath $database) -and (Test-Path -LiteralPath $runtimeFile)) {
            $listeners = @(Get-NetTCPConnection -OwningProcess $process.Id -State Listen -ErrorAction SilentlyContinue)
            if ($listeners.Count -gt 0) {
                $ready = $true
                break
            }
        }
        Start-Sleep -Milliseconds 200
    }
    if (-not $ready) {
        throw "Packaged EXE did not become ready in 10 seconds"
    }
    if (@($listeners | Where-Object { $_.LocalAddress -ne "127.0.0.1" }).Count -gt 0) {
        throw "Packaged EXE opened a non-loopback listener"
    }
    $runtime = Get-Content -Raw -Encoding UTF8 -LiteralPath $runtimeFile | ConvertFrom-Json
    $headers = @{ "X-YuanJian-Token" = $runtime.token }
    $homeResponse = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$($runtime.port)/"
    $hasDefaultView = $homeResponse.Content.Contains('data-view="today"')
    $hasModuleEntry = $homeResponse.Content.Contains($moduleEntry)
    $hasLegacyWorldView = $homeResponse.Content.Contains('data-view="world"')
    if ($homeResponse.StatusCode -ne 200 -or -not $hasDefaultView -or -not $hasModuleEntry -or $hasLegacyWorldView) {
        throw "Packaged local UI did not pass the home-page check"
    }
    $moduleResponse = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$($runtime.port)/js/views/today.js"
    if ($moduleResponse.StatusCode -ne 200) {
        throw "Packaged module view was not served"
    }
    if ($homeResponse.Content.Contains("https://")) {
        throw "Packaged home page contains a remote resource"
    }
    $cognition = Invoke-RestMethod -Method Post -TimeoutSec 5 -Uri "http://127.0.0.1:$($runtime.port)/api/cognition/run" -Headers $headers -ContentType "application/json" -Body "{}"
    if ($cognition.provider -ne "local") {
        throw "Local fallback cognition was not used without an API token"
    }
    $second = Start-Process -FilePath $ExePath -ArgumentList "--background" -PassThru -WindowStyle Hidden
    if (-not $second.WaitForExit(5000)) {
        Stop-Process -Id $second.Id -Force
        throw "Second packaged instance was not rejected"
    }
    $shutdown = Invoke-RestMethod -Method Post -TimeoutSec 5 -Uri "http://127.0.0.1:$($runtime.port)/api/shutdown" -Headers $headers -ContentType "application/json" -Body "{}"
    if (-not $process.WaitForExit(10000)) {
        throw "Packaged EXE did not exit after the safe shutdown request"
    }
    $process.Refresh()
    [pscustomobject]@{
        ProcessId = $process.Id
        Running = -not $process.HasExited
        Database = Test-Path -LiteralPath $database
        ListenerCount = $listeners.Count
        Address = $listeners[0].LocalAddress
        HomeStatus = $homeResponse.StatusCode
        RemoteScripts = $false
        DefaultView = $defaultView
        ModuleEntry = $moduleEntry
        LocalFallback = $cognition.provider
        SecondInstanceExitCode = $second.ExitCode
        Shutdown = $shutdown.status
    } | ConvertTo-Json
}
finally {
    $process.Refresh()
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id
        $process.WaitForExit(5000) | Out-Null
    }
    $resolvedProject = [IO.Path]::GetFullPath($projectRoot)
    $resolvedSmoke = [IO.Path]::GetFullPath($smokeRoot)
    if (-not $resolvedSmoke.StartsWith($resolvedProject + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing smoke cleanup outside project root"
    }
    if (Test-Path -LiteralPath $resolvedSmoke) {
        [IO.Directory]::Delete($resolvedSmoke, $true)
    }
}

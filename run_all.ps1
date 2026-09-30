<#
    Наскрізний запуск стенду Load Testing БД parcel_delivery:
    залежності -> контейнери -> заливка дата сету -> повний прогін тесту.

    Запуск із кореня проєкту (термінал PyCharm):
        .\run_all.ps1                                  # повний прогін: дата сет ≈99 млн рядків + 6 етапів (≈3 год)
        .\run_all.ps1 -Scale 0.1 -MinutesPerStage 2     # швидка перевірка стенду (≈25 хв)
        .\run_all.ps1 -SkipSeed                         # тільки тест, БД уже заповнена
        .\run_all.ps1 -Stop                             # зупинити контейнери, дані лишаються
        .\run_all.ps1 -Stop -RemoveData                 # зупинити і стерти дані MySQL

    Увага: будь-який запуск без -SkipSeed видаляє й заново створює БД parcel_delivery.
    Якщо PowerShell не дає запустити файл:
        powershell -ExecutionPolicy Bypass -File .\run_all.ps1
#>

[CmdletBinding()]
param(
# Частка повного дата сету; 0 — повний обсяг із parcel_dataset_specification.py.
    [double]$Scale = 0,
# Кількість процесів-генераторів навантаження.
    [int]$Workers = 8,
# Скоротити кожен етап до N хв; 0 — тривалості з вимог.
    [double]$MinutesPerStage = 0,
    [switch]$SkipSeed,
    [switch]$Stop,
    [switch]$RemoveData
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# --project-directory . робить корінь проєкту базою для шляхів у compose-файлах і підхоплює .env
$composeArgs = @(
    "compose", "--project-directory", ".",
    "-f", "docker/docker-compose.mysql.yml",
    "-f", "docker/docker-compose.monitoring.yml"
)

function Invoke-Step
{
    param([string]$Title, [scriptblock]$Action)
    Write-Host ""
    Write-Host "${Title}:" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0)
    {
        throw "Крок «$Title» завершився з кодом $LASTEXITCODE"
    }
}

function Wait-MySqlHealthy
{
    # Перший старт MySQL — близько 20 с (8 ГБ buffer pool, 4 ГБ redo log, створення користувача моніторингу).
    param([int]$TimeoutSeconds = 300)
    Write-Host "Чекаю, поки MySQL стане healthy" -NoNewline
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline)
    {
        $status = docker inspect -f '{{.State.Health.Status}}' parcel_delivery_mysql 2> $null
        if ($status -eq "healthy")
        {
            Write-Host " — готово."; return
        }
        Write-Host "." -NoNewline
        Start-Sleep -Seconds 5
    }
    throw "MySQL не став healthy за $TimeoutSeconds с"
}

if ($Stop)
{
    $downArgs = $composeArgs + @("down")
    if ($RemoveData)
    {
        $downArgs += "-v"
    }
    docker @downArgs
    exit $LASTEXITCODE
}

$startedAt = Get-Date

Invoke-Step "1/4 Залежності (uv sync)" { uv sync }

Invoke-Step "2/4 Контейнери: MySQL, phpMyAdmin, Prometheus, Grafana, mysqld_exporter" { docker @composeArgs up -d }
Wait-MySqlHealthy

if ($SkipSeed)
{
    Write-Host ""
    Write-Host "3/4 Заповнення БД пропущено (-SkipSeed)" -ForegroundColor Yellow
}
else
{
    $seedArgs = @("run", "--env-file", ".env", "seed_fake_parcel_delivery_data.py")
    if ($Scale -gt 0)
    {
        $seedArgs += @("--scale", $Scale)
    }
    Invoke-Step "3/4 Заповнення БД parcel_delivery" { uv @seedArgs }
}

$testArgs = @("run", "--env-file", ".env", "run_load_test.py", "--workers", $Workers)
if ($MinutesPerStage -gt 0)
{
    $testArgs += @("--minutes-per-stage", $MinutesPerStage)
}
Invoke-Step "4/4 Load Testing" { uv @testArgs }

$elapsedMinutes = [int]((Get-Date) - $startedAt).TotalMinutes
$latestResults = Get-ChildItem "load_test_results" -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1

Write-Host ""
Write-Host "Готово за $elapsedMinutes хв." -ForegroundColor Green
if ($latestResults)
{
    Write-Host "Результати й requirements_check.md: $( $latestResults.FullName )"
}
Write-Host "Grafana http://localhost:3000 (admin / admin) · Prometheus http://localhost:9090 · phpMyAdmin http://localhost:8080 (root / parcel_delivery_root)"
Write-Host "Стенд лишається піднятим. Зупинити: .\run_all.ps1 -Stop"

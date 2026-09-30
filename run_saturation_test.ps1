<#
    Пошук точки насичення БД parcel_delivery: згущені сходи 500 -> 5000 клієнтів в одному прогоні.
    Відповідає на питання зі слайда 25 — «коли саме система просіла».

    Повний прогін вимог (run_all.ps1) дав перегин десь між 2000 і 5000 клієнтів;
    цей скрипт додає точки 100, 250, 1500, 2500, 3000, 3500, 4000, 4500 і будує повну криву
    пропускної здатності — з висхідною гілкою, плато й ділянкою деградації.

    Запуск із кореня проєкту (термінал PyCharm):
        .\run_saturation_test.ps1                          # повні сходи, 14 етапів, ≈1 год 35 хв
        .\run_saturation_test.ps1 -MinutesPerStage 3       # швидка крива, ≈50 хв
        .\run_saturation_test.ps1 -MinutesPerStage 1       # перевірка, що все запускається, ≈25 хв
        .\run_saturation_test.ps1 -Reseed                  # спершу залити БД наново (+15–30 хв)
        .\run_saturation_test.ps1 -Stages load_500,stress_3000,stress_4000

    За замовчуванням БД НЕ перезаливається: сходи вимірюють власну базову точку load_500
    у тому ж прогоні, тож крива самодостатня. -Reseed потрібен лише щоб повернути БД
    до вихідних ≈99 млн рядків — попередній прогін дописав у неї транзакціями T5 і T6.

    Увага: -Reseed видаляє й заново створює БД parcel_delivery.
    Якщо PowerShell не дає запустити файл:
        powershell -ExecutionPolicy Bypass -File .\run_saturation_test.ps1
#>

[CmdletBinding()]
param(
# Кількість процесів-генераторів навантаження.
    [int]$Workers = 8,
# Скоротити кожен етап до N хв; 0 — тривалості з SATURATION_LADDER_STAGES.
    [double]$MinutesPerStage = 0,
# Прогнати лише вибрані етапи сходів, наприклад load_500,stress_3000,stress_4000.
    [string]$Stages = "",
# Залити дата сет наново перед сходами.
    [switch]$Reseed,
# Частка повного дата сету для -Reseed; 0 — повний обсяг.
    [double]$Scale = 0
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

$startedAt = Get-Date

Invoke-Step "1/4 Залежності (uv sync)" { uv sync }

Invoke-Step "2/4 Контейнери: MySQL, phpMyAdmin, Prometheus, Grafana, mysqld_exporter" { docker @composeArgs up -d }
Wait-MySqlHealthy

if ($Reseed)
{
    $seedArgs = @("run", "--env-file", ".env", "seed_fake_parcel_delivery_data.py")
    if ($Scale -gt 0)
    {
        $seedArgs += @("--scale", $Scale)
    }
    Invoke-Step "3/4 Заповнення БД parcel_delivery наново" { uv @seedArgs }
}
else
{
    Write-Host ""
    Write-Host "3/4 Заповнення БД пропущено — сходи міряють власну базову точку load_500." -ForegroundColor Yellow
    Write-Host "    Щоб повернути БД до вихідного обсягу, запустіть із -Reseed." -ForegroundColor Yellow
}

$testArgs = @("run", "--env-file", ".env", "run_load_test.py", "--plan", "saturation", "--workers", $Workers)
if ($MinutesPerStage -gt 0)
{
    $testArgs += @("--minutes-per-stage", $MinutesPerStage)
}
if ($Stages)
{
    $testArgs += @("--stages", $Stages)
}
Invoke-Step "4/4 Сходи насичення 100 → 5000 клієнтів" { uv @testArgs }

$elapsedMinutes = [int]((Get-Date) - $startedAt).TotalMinutes
$latestResults = Get-ChildItem "load_test_results" -Directory -Filter "*_saturation" -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1

Write-Host ""
Write-Host "Готово за $elapsedMinutes хв." -ForegroundColor Green
if ($latestResults)
{
    Write-Host "Крива насичення: $( Join-Path $latestResults.FullName 'saturation_curve.md' )"
    Write-Host "Точки кривої для графіка: $( Join-Path $latestResults.FullName 'saturation_curve.csv' )"
    Write-Host "Вердикт за вимогами: $( Join-Path $latestResults.FullName 'requirements_check.md' )"
}
Write-Host "Grafana http://localhost:3000 (admin / admin) · Prometheus http://localhost:9090"
Write-Host "Стенд лишається піднятим. Зупинити: .\run_all.ps1 -Stop"

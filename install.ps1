# Установка «Ассистента ОТК»: окружение, зависимости и ярлык на рабочем столе.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# Запускать из папки проекта. Скрипт ничего не удаляет: существующее окружение
# переиспользуется, ярлык перезаписывается.

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
$venv = Join-Path $repo ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$pythonw = Join-Path $venv "Scripts\pythonw.exe"

Write-Host "Папка проекта: $repo"

if (-not (Test-Path $python)) {
    Write-Host "Создаю виртуальное окружение…"
    python -m venv $venv
}

Write-Host "Ставлю зависимости…"
& $python -m pip install --upgrade pip --quiet
& $python -m pip install -r (Join-Path $repo "requirements.txt") --quiet

$env_file = Join-Path $repo ".env"
if (-not (Test-Path $env_file)) {
    Copy-Item (Join-Path $repo ".env.example") $env_file
    Write-Host "Создан .env — впишите в него ключ OpenRouter (нужен только для сканов)."
}

Write-Host "Создаю ярлык на рабочем столе…"
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop "Ассистент ОТК.lnk"
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = $pythonw
$s.Arguments = "-m app --desktop"
$s.WorkingDirectory = $repo
$s.IconLocation = Join-Path $repo "assets\otk-assistant.ico"
$s.Description = "Ассистент ОТК — карты обмера и задания на изготовление по чертежу"
$s.Save()

Write-Host ""
Write-Host "Готово. Ярлык «Ассистент ОТК» на рабочем столе." -ForegroundColor Green
Write-Host "Приложение открывается своим окном, консоль не нужна."

# ProjectZero installer for Windows (PowerShell 5.1+).
#
#   irm https://raw.githubusercontent.com/maun200/ProjectZero/main/install.ps1 | iex
#
# What it does (no administrator rights, nothing system-wide):
#   1. finds Python 3.11+ (the "py" launcher or python.exe)
#   2. creates a private virtual environment in %LOCALAPPDATA%\ProjectZero
#   3. installs ProjectZero into it, adds a Start-menu shortcut and the `projectzero`
#      command to your user PATH
#   4. starts the app in your browser (set $env:PROJECTZERO_NO_START = "1" to skip)
#
# Options (environment variables): PROJECTZERO_DIR, PROJECTZERO_SOURCE, PROJECTZERO_NO_START
# Uninstall: delete %LOCALAPPDATA%\ProjectZero and the "ProjectZero" Start-menu shortcut;
#            your account and settings live in %APPDATA%\ProjectZero.
$ErrorActionPreference = "Stop"

$RepoArchive = "https://github.com/maun200/ProjectZero/archive/refs/heads/main.zip"
$Dir = if ($env:PROJECTZERO_DIR) { $env:PROJECTZERO_DIR } else { Join-Path $env:LOCALAPPDATA "ProjectZero" }
$Source = if ($env:PROJECTZERO_SOURCE) { $env:PROJECTZERO_SOURCE } else { $RepoArchive }

function Say($text) { Write-Host "ProjectZero: $text" }

function Find-Python {
    $check = "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
    $candidates = @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("python"), @("python3"))
    foreach ($c in $candidates) {
        $exe = $c[0]
        $extra = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            & $exe @extra -c $check 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                $path = & $exe @extra -c "import sys; print(sys.executable)"
                if ($path -and -not ($path -like "*WindowsApps*")) { return $path.Trim() }
            }
        } catch { }
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Say "Python 3.11 or newer is required but was not found."
    Say "Install it with:  winget install Python.Python.3.12"
    Say "or from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this command again."
    exit 1
}
Say "using $(& $Python --version) ($Python)"

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$VenvPython = Join-Path $Dir "venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Say "creating virtual environment in $Dir\venv"
    & $Python -m venv (Join-Path $Dir "venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment" }
}
Say "installing ProjectZero (this can take a minute)"
& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPython -m pip install --quiet --upgrade $Source
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$Exe = Join-Path $Dir "venv\Scripts\projectzero.exe"
$BinDir = Join-Path $Dir "bin"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
Set-Content -Path (Join-Path $BinDir "projectzero.cmd") -Value "@echo off`r`n`"$Exe`" %*" -Encoding ASCII

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $UserPath) { $UserPath = "" }
if (-not ($UserPath.Split(";") -contains $BinDir)) {
    [Environment]::SetEnvironmentVariable("Path", ($UserPath.TrimEnd(";") + ";" + $BinDir).TrimStart(";"), "User")
    Say "added $BinDir to your user PATH (open a new terminal to use 'projectzero')"
}
$env:Path = "$env:Path;$BinDir"

try {
    $StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    $Shell = New-Object -ComObject WScript.Shell
    $Link = $Shell.CreateShortcut((Join-Path $StartMenu "ProjectZero.lnk"))
    $Link.TargetPath = $Exe
    $Link.Description = "Share computing power for AI, chat and earn credits"
    $Link.Save()
} catch { Say "could not create a Start-menu shortcut (not critical)" }

Say "installed version $(& $Exe --version)"
Say "start it any time with: projectzero"
Say "try it without any AI model:  projectzero demo"
Say "real AI answers need Ollama (https://ollama.com) on this computer if you want to share compute"

if ($env:PROJECTZERO_NO_START -ne "1") {
    Say "starting the app ..."
    & $Exe
}

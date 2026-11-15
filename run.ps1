# Windows launcher. Run from this folder:  .\run.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv)) { python -m venv .venv }
. .\.venv\Scripts\Activate.ps1
pip install -q -r requirements.txt

# load .env if present
if (Test-Path .env) {
  Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
      $v = $matches[2].Trim('"')
      if ($v -ne "") { [Environment]::SetEnvironmentVariable($matches[1], $v, "Process") }
    }
  }
}

Write-Host "AgentForge on http://127.0.0.1:8000" -ForegroundColor Cyan
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload

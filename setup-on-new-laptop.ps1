# ===================================================================
#  Run this on the NEW laptop, from inside the unzipped project folder.
#  Rebuilds what must be local and checks what must be installed.
# ===================================================================
$ErrorActionPreference = "Stop"

if (-not (Test-Path "backend\main.py")) {
  Write-Host "Run this from inside the project folder (the one holding backend\)." -ForegroundColor Red
  exit 1
}

function Line($label, $ok, $detail) {
  $mark  = if ($ok) { "OK  " } else { "MISS" }
  $color = if ($ok) { "Green" } else { "Yellow" }
  Write-Host ("  [{0}] {1,-22} {2}" -f $mark, $label, $detail) -ForegroundColor $color
}

Write-Host "`n1. Python" -ForegroundColor Cyan
$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) {
  Write-Host "  Python is not on PATH. Install 3.11 or newer, then run this again." -ForegroundColor Red
  exit 1
}
$ver = (python --version)
Line "python" $true $ver

Write-Host "`n2. Virtual environment" -ForegroundColor Cyan
# The old .venv was left behind on purpose: it holds Windows binaries
# compiled against that machine's Python, and copying it produces an
# environment that imports but cannot run.
if (Test-Path ".venv") {
  Write-Host "  .venv already here — leaving it alone."
} else {
  python -m venv .venv
  Write-Host "  created."
}
& ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& ".venv\Scripts\pip.exe" install --quiet --default-timeout=120 --retries 10 -r requirements.txt
Line "dependencies" $true "installed from requirements.txt"

Write-Host "`n3. What came across" -ForegroundColor Cyan
Line "code"        (Test-Path "backend\main.py")       "backend, web, tests"
Line "database"    (Test-Path "agentforge.db")         "accounts, teams, run history"
Line "server key"  (Test-Path ".secrets\server.key")   "sessions stay valid"
Line "teams"       (Test-Path "teams")                 "agent folders, memories, workspaces"
Line "settings"    (Test-Path ".env")                  "models and keys"

if (-not (Test-Path ".secrets\server.key")) {
  Write-Host "  Without the key a new one is made on first start: everyone" -ForegroundColor Yellow
  Write-Host "  simply signs in again. Passwords and teams are unaffected." -ForegroundColor Yellow
}

Write-Host "`n4. Hermes" -ForegroundColor Cyan
$hermes = (Get-Command hermes -ErrorAction SilentlyContinue)
if ($hermes) {
  Line "hermes" $true (hermes --version 2>&1 | Select-Object -First 1)
  Write-Host "  Check it is signed in:    hermes auth list"
  Write-Host "  Check the profiles:       hermes profile list"
  Write-Host "  Your teams expect af-<you>, af-<you>-w and af-<you>-wc."
  Write-Host "  Missing ones are recreated on first use, but a profile that"
  Write-Host "  was pointed at a particular model needs pointing again."
} else {
  Line "hermes" $false "not on PATH — install it and sign in, or runs will fail"
}

Write-Host "`n5. Optional" -ForegroundColor Cyan
$whisper = & ".venv\Scripts\python.exe" -c "import importlib.util as u; print(u.find_spec('faster_whisper') is not None)" 2>$null
Line "voice input" ($whisper -eq "True") "pip install -r requirements-voice.txt"
Line "multica"     ((Get-Command multica -ErrorAction SilentlyContinue) -ne $null) "only needed for the admin mirror"

Write-Host "`n6. Test it" -ForegroundColor Cyan
Write-Host "  .venv\Scripts\python.exe tests\full_test.py     # expect 417 passed"
Write-Host "  .\run.ps1                                       # then open http://127.0.0.1:8000"
Write-Host ""
Write-Host "Once it works, delete the transfer zip from both machines." -ForegroundColor Yellow

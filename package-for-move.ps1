# ===================================================================
#  Run this on the OLD laptop, from inside the project folder.
#  Makes one zip holding everything the new machine needs and nothing
#  it must not have.
# ===================================================================
$ErrorActionPreference = "Stop"

$stamp  = Get-Date -Format "yyyy-MM-dd"
$out    = Join-Path ([Environment]::GetFolderPath("Desktop")) "agentforge-move-$stamp"
$zip    = "$out.zip"

if (-not (Test-Path "backend\main.py")) {
  Write-Host "Run this from inside the project folder (the one holding backend\)." -ForegroundColor Red
  exit 1
}

# Excluded on purpose:
#   .venv          Windows-specific binaries; recreated on the new machine
#   __pycache__    stale bytecode compiled for this Python build
#   .multica-outbox transient staging for the mirror
#   *.pyc, *.log   derived files
$skipDirs  = @(".venv", "__pycache__", ".multica-outbox", ".git", "node_modules")
$skipFiles = @("*.pyc", "*.log", "*.pdf")

Write-Host "Collecting..." -ForegroundColor Cyan
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Path $out | Out-Null

$root = (Get-Location).Path
$copied = 0
foreach ($f in Get-ChildItem -Path . -Recurse -File -Force) {
  $rel   = $f.FullName.Substring($root.Length + 1)
  $parts = $rel -split '\\'

  # A nested Where-Object would shadow $_, so these are plain loops.
  $skip = $false
  foreach ($d in $skipDirs)  { if ($parts -contains $d) { $skip = $true } }
  foreach ($p in $skipFiles) { if ($f.Name -like $p)    { $skip = $true } }
  if ($skip) { continue }

  $dest = Join-Path $out $rel
  $dir  = Split-Path $dest -Parent
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  Copy-Item $f.FullName $dest -Force
  $copied++
}
Write-Host "$copied files collected."

# What went, so you can check before you carry it anywhere
Write-Host ""
Write-Host "Included:" -ForegroundColor Cyan
foreach ($p in @("backend", "web", "tests", "teams", ".secrets",
                 "agentforge.db", ".env", "requirements.txt",
                 "hermes_catalogue.json", "run.ps1")) {
  $there = Test-Path (Join-Path $out $p)
  $mark  = if ($there) { "  yes" } else { "  --  (not present)" }
  "{0,-26} {1}" -f $p, $mark | Write-Host
}

Compress-Archive -Path "$out\*" -DestinationPath $zip -Force
Remove-Item $out -Recurse -Force

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ""
Write-Host "Written: $zip  ($mb MB)" -ForegroundColor Green
Write-Host ""
Write-Host "THIS ZIP HOLDS SECRETS:" -ForegroundColor Yellow
Write-Host "  .env          API keys"
Write-Host "  .secrets\     the key your session tokens are signed with"
Write-Host "  agentforge.db password hashes"
Write-Host "Move it on a USB stick or an encrypted transfer, not by email." -ForegroundColor Yellow
Write-Host "Delete it from both machines once the new one works."

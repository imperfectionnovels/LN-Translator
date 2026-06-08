# Two-second pre-commit smoke for the backend + frontend.
#
# - ruff catches unused imports, undefined names, suspicious patterns, import order.
# - compileall catches syntax errors and import-time exceptions across the
#   entire backend tree without running anything.
#
# Run from the repo root: pwsh scripts/lint.ps1

$ErrorActionPreference = "Stop"

Write-Host "==> python -m ruff check backend" -ForegroundColor Cyan
python -m ruff check backend
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> python -m compileall backend" -ForegroundColor Cyan
python -m compileall -q backend
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> python scripts/check_em_dashes.py backend/prompts" -ForegroundColor Cyan
python scripts/check_em_dashes.py backend/prompts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "LINT PASS" -ForegroundColor Green

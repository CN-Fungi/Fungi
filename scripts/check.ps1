# One-shot quality gate: lint (autofix), format check, re-lint, test.
# The format step only CHECKS. The tree is aligned with the pinned ruff
# (b53d190), so a disagreement means the caller's own edit is off-style —
# rewriting unrelated files behind their back is what a gate must not do.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

ruff check --fix fungi tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
ruff format --check fungi tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
ruff check fungi tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m pytest -q
exit $LASTEXITCODE

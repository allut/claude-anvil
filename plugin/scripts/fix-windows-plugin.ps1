# fix-windows-plugin.ps1
# Clears the stale plugin directory that causes:
#   EPERM: operation not permitted, rename allut-claude-anvil -> allut-claude-anvil.bak
#
# Run this from PowerShell, then reopen Claude Code and run:
#   /plugin marketplace add allut/claude-anvil

$pluginRoot = Join-Path $env:USERPROFILE ".claude\plugins\marketplaces"
$targets = @(
    Join-Path $pluginRoot "allut-claude-anvil"
    Join-Path $pluginRoot "allut-claude-anvil.bak"
)

Write-Host "Cleaning up stale claude-anvil plugin directories..."

foreach ($target in $targets) {
    if (Test-Path $target) {
        Write-Host "  Removing: $target"
        Remove-Item -Recurse -Force $target -ErrorAction Stop
        Write-Host "  Removed."
    } else {
        Write-Host "  Not found (skipping): $target"
    }
}

Write-Host ""
Write-Host "Done. Reopen Claude Code and run: /plugin marketplace add allut/claude-anvil"

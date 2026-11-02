# PreToolUse hook: blokir perintah Bash yang berbahaya (versi PowerShell).
# Input JSON dari Claude Code masuk lewat stdin; exit 2 = blokir + pesan ke Claude.

$raw = [Console]::In.ReadToEnd()

try {
    $command = ($raw | ConvertFrom-Json).tool_input.command
} catch {
    $command = $raw
}
if (-not $command) { exit 0 }

$blockedPatterns = @(
    'rm -rf /',
    'rm -rf ~',
    'rm -rf *',
    'git push --force',
    'git push -f',
    'git reset --hard',
    'DROP TABLE',
    'DROP DATABASE',
    'mkfs',
    ':(){ :|:& };:',
    'Remove-Item -Recurse -Force C:\',
    'Format-Volume'
)

foreach ($pattern in $blockedPatterns) {
    if ($command.IndexOf($pattern, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        [Console]::Error.WriteLine("Diblokir oleh validate-bash.ps1: perintah mengandung '$pattern'.")
        exit 2
    }
}

exit 0

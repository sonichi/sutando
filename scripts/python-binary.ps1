# Resolve a working Python 3 before starting a service or attempting delivery.
function Resolve-SutandoPython {
    foreach ($candidate in @('python', 'py')) {
        if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        $probeArgs = @('-c', 'import sys; print(sys.version_info[0])')
        if ($candidate -eq 'py') { $probeArgs = @('-3') + $probeArgs }
        try {
            $version = & $candidate @probeArgs 2>$null
            if ($LASTEXITCODE -eq 0 -and $version -eq '3') { return $candidate }
        } catch {}
    }
    throw 'Python 3 is unavailable; install Python 3.11+ from https://python.org'
}

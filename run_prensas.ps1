# Arranca el recolector.
#
#   .\run_prensas.ps1            en primer plano, se ve todo (para diagnosticar)
#   .\run_prensas.ps1 -Oculto    sin ventana, la salida va a logs\arranque.log
#
# Requisitos en una maquina nueva:
#   - Python 3.13+
#   - Poetry            (poetry install)
#   - ODBC Driver 17 for SQL Server
#   - un .env con DB_SERVER / DB_NAME / DB_USER / DB_PASSWORD
#
# Antes de arrancar por primera vez conviene:
#   poetry run pytest -q                     # no necesita base de datos
#   poetry run python verificar_esquema.py   # conectividad y esquema
#
# Para que sobreviva al cierre de sesion hace falta un servicio de Windows;
# ocultar la ventana NO basta: al cerrar sesion el proceso muere igual.

param([switch]$Oculto)

Set-Location $PSScriptRoot

if (-not $Oculto) {
    poetry run python Prensas.py
    return
}

# Oculto: sin ventana no se ve nada si algo truena al arrancar, asi que la
# salida se guarda. El log del servicio en si va aparte, a logs\supervisor.log.
New-Item -ItemType Directory -Force "logs" | Out-Null

Start-Process "poetry" -WindowStyle Hidden -WorkingDirectory $PSScriptRoot `
    -ArgumentList "run", "python", "Prensas.py" `
    -RedirectStandardOutput "logs\arranque.log" `
    -RedirectStandardError  "logs\arranque.err"

Start-Sleep -Seconds 3
$vivo = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "Prensas\.py" }

if ($vivo) {
    "Recolector arrancado sin ventana (PID $($vivo.ProcessId -join ', '))"
    "   estado   -> http://localhost:9100/health"
    "   arranque -> logs\arranque.err"
    "   servicio -> logs\supervisor.log"
} else {
    "No arranco. Revisa logs\arranque.err:"
    if (Test-Path "logs\arranque.err") { Get-Content "logs\arranque.err" -Tail 20 }
}

# Registra el recolector como servicio de Windows con NSSM.
#
#   .\servicio-recolector.ps1 estado      ver como esta (no pide administrador)
#   .\servicio-recolector.ps1 instalar    registrarlo y arrancarlo
#   .\servicio-recolector.ps1 reiniciar   despues de un despliegue
#   .\servicio-recolector.ps1 quitar      darlo de baja
#
# Por que un servicio y no solo ocultar la ventana: un proceso lanzado desde una
# sesion muere cuando esa sesion se cierra. Como servicio arranca solo con el
# servidor, sobrevive al cierre de sesion y NSSM lo relanza si se cae.
#
# Requiere NSSM (portable, sin instalador): https://nssm.cc/download
# Descomprimir win64\nssm.exe en C:\iot\obs\nssm\

param(
    [Parameter(Position = 0)]
    [ValidateSet("estado", "instalar", "quitar", "reiniciar")]
    [string]$Accion = "estado",

    [string]$Nssm   = "C:\iot\obs\nssm\nssm.exe",
    [string]$Nombre = "IoT-Recolector"
)

$ErrorActionPreference = "Stop"
$APP = Split-Path -Parent $PSScriptRoot

function EsAdministrador {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function PythonDelEntorno {
    # La ruta del venv es distinta en cada maquina; que la diga poetry en vez
    # de escribirla a mano en el README.
    Push-Location $APP
    try { $py = (& poetry env info --executable 2>$null | Select-Object -First 1) }
    finally { Pop-Location }

    if (-not $py -or -not (Test-Path $py)) {
        throw "No se encontro el python del entorno. Corre 'poetry install' en $APP primero."
    }
    return $py.Trim()
}

# ── estado ─────────────────────────────────────────────────────────────────
if ($Accion -eq "estado") {
    $svc = Get-Service -Name $Nombre -ErrorAction SilentlyContinue
    if (-not $svc) {
        "El servicio $Nombre no esta registrado."
        "   .\servicio-recolector.ps1 instalar"
    } else {
        "$Nombre : $($svc.Status)  (arranque: $($svc.StartType))"
    }

    $sueltos = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
               Where-Object { $_.CommandLine -match "Prensas\.py" }
    if ($sueltos) {
        ""
        "OJO: hay $(($sueltos | Measure-Object).Count) proceso(s) del recolector corriendo."
        "Si el servicio tambien esta arriba, la produccion se cuenta DOBLE."
        $sueltos | ForEach-Object { "   PID $($_.ProcessId)" }
    }

    try {
        $h = Invoke-WebRequest "http://localhost:9100/health" -TimeoutSec 3 -UseBasicParsing
        ""; "salud: $($h.Content)"
    } catch { ""; "el puerto 9100 no responde" }
    return
}

# ── las demas acciones piden administrador ────────────────────────────────
if (-not (EsAdministrador)) {
    throw "'$Accion' necesita PowerShell como Administrador."
}

if ($Accion -ne "quitar" -and -not (Test-Path $Nssm)) {
    throw "No esta NSSM en $Nssm. Bajalo de https://nssm.cc/download y descomprime win64\nssm.exe ahi (o usa -Nssm con otra ruta)."
}

# ── quitar ─────────────────────────────────────────────────────────────────
if ($Accion -eq "quitar") {
    if (-not (Get-Service -Name $Nombre -ErrorAction SilentlyContinue)) {
        "El servicio $Nombre no estaba registrado."; return
    }
    & $Nssm stop $Nombre confirm | Out-Null
    Start-Sleep -Seconds 2
    & $Nssm remove $Nombre confirm
    "Servicio $Nombre dado de baja."
    return
}

# ── reiniciar ──────────────────────────────────────────────────────────────
if ($Accion -eq "reiniciar") {
    if (-not (Get-Service -Name $Nombre -ErrorAction SilentlyContinue)) {
        throw "El servicio $Nombre no esta registrado."
    }
    & $Nssm restart $Nombre
    Start-Sleep -Seconds 5
    "Servicio reiniciado: $((Get-Service -Name $Nombre).Status)"
    return
}

# ── instalar ───────────────────────────────────────────────────────────────
$python = PythonDelEntorno
$logs   = Join-Path $APP "logs"
New-Item -ItemType Directory -Force $logs | Out-Null

""
"proyecto : $APP"
"python   : $python"

if ($python -like "$env:USERPROFILE*") {
    ""
    "AVISO: el entorno vive dentro del perfil de $env:USERNAME."
    "El servicio corre como LocalSystem y puede no tener acceso ahi."
    "Lo mas robusto es dejarlo junto al codigo:"
    "    poetry config virtualenvs.in-project true"
    "    poetry install"
    "y volver a correr 'instalar'."
    ""
}

# Un recolector suelto mas el servicio = produccion contada doble.
$sueltos = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -match "Prensas\.py" }
if ($sueltos) {
    "Deteniendo $(($sueltos | Measure-Object).Count) recolector(es) sueltos antes de instalar..."
    $sueltos | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
}

if (Get-Service -Name $Nombre -ErrorAction SilentlyContinue) {
    "Ya existia: se da de baja para registrarlo de nuevo."
    & $Nssm stop $Nombre confirm | Out-Null
    Start-Sleep -Seconds 2
    & $Nssm remove $Nombre confirm | Out-Null
    Start-Sleep -Seconds 2
}

& $Nssm install $Nombre $python | Out-Null
& $Nssm set $Nombre AppParameters   "Prensas.py"    | Out-Null
& $Nssm set $Nombre AppDirectory    $APP            | Out-Null
& $Nssm set $Nombre DisplayName     "IoT Recolector de Produccion"  | Out-Null
& $Nssm set $Nombre Description     "Lee los contadores de los PLC y registra la produccion contra las ordenes." | Out-Null
& $Nssm set $Nombre Start           SERVICE_AUTO_START | Out-Null

# Lo que el servicio escriba a consola (errores de arranque, sobre todo).
# El log del servicio en si lo maneja el propio recolector en logs\.
& $Nssm set $Nombre AppStdout       (Join-Path $logs "servicio.log") | Out-Null
& $Nssm set $Nombre AppStderr       (Join-Path $logs "servicio.err") | Out-Null
& $Nssm set $Nombre AppRotateFiles  1        | Out-Null
& $Nssm set $Nombre AppRotateBytes  10485760 | Out-Null

# Si se cae, relanzarlo; los 10 s evitan un ciclo de reinicios si el fallo es
# de arranque (por ejemplo, la base inalcanzable).
& $Nssm set $Nombre AppExit Default Restart | Out-Null
& $Nssm set $Nombre AppRestartDelay 10000   | Out-Null
& $Nssm set $Nombre AppThrottle     10000   | Out-Null

& $Nssm start $Nombre | Out-Null
Start-Sleep -Seconds 6

$svc = Get-Service -Name $Nombre -ErrorAction SilentlyContinue
"servicio : $($svc.Status)  (arranque: $($svc.StartType))"

try {
    $h = Invoke-WebRequest "http://localhost:9100/health" -TimeoutSec 5 -UseBasicParsing
    "salud    : $($h.Content)"
    ""
    "Listo. Arranca solo con el servidor y sobrevive al cierre de sesion."
} catch {
    ""
    "El 9100 no responde todavia. Revisa:"
    "   Get-Content `"$logs\servicio.err`" -Tail 20"
    "   Get-Content `"$logs\supervisor.log`" -Tail 20"
}

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

function PuertoDelEnv {
    # El recolector escucha donde diga HTTP_PORT. Si aqui se diera por hecho el
    # 9100, en un servidor con otro puerto la verificacion de salud diria que
    # no responde y parecria que el servicio no arranco.
    $archivo = Join-Path $APP ".env"
    if (Test-Path $archivo) {
        $linea = Get-Content $archivo |
                 Where-Object { $_ -match '^\s*HTTP_PORT\s*=\s*(\d+)' } |
                 Select-Object -Last 1
        if ($linea -match '^\s*HTTP_PORT\s*=\s*(\d+)') { return [int]$Matches[1] }
    }
    return 9100
}

function Salud($puerto, $segundos) {
    # -UseBasicParsing y un timeout corto: sin esto, con un proxy de sistema
    # configurado la llamada se queda colgada y el script nunca termina.
    try {
        $r = Invoke-WebRequest "http://localhost:$puerto/health" `
                 -TimeoutSec $segundos -UseBasicParsing -Proxy $null
        return $r.Content
    } catch {
        return $null
    }
}

function PythonDelEntorno {
    # Primero el entorno junto al codigo. Es el que conviene para el servicio, y
    # ademas 'poetry env info' sigue devolviendo el del cache mientras ese exista:
    # virtualenvs.in-project solo aplica a entornos NUEVOS, no mueve el que ya hay.
    $local = Join-Path $APP ".venv\Scripts\python.exe"
    if (Test-Path $local) { return $local }

    # Si no, que poetry diga cual es: la ruta del cache cambia en cada maquina.
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

    $puerto = PuertoDelEnv
    $salud = Salud $puerto 3
    ""
    if ($salud) {
        "salud (puerto $puerto): $salud"
    } elseif ($svc -and $svc.Status -eq "Running") {
        # El caso enganioso: Windows lo da por arriba y el recolector no atiende.
        "el puerto $puerto no responde, aunque el servicio dice Running."
        "Eso es que el proceso arranco y murio, o sigue levantando. Revisa:"
        "   Get-Content `"$APP\logs\servicio.err`" -Tail 20"
        "   Get-Content `"$APP\logs\supervisor.log`" -Tail 20"
    } else {
        "el puerto $puerto no responde (no hay recolector atendiendo)"
    }
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

# El entorno junto al codigo esta bien aunque el proyecto viva en un perfil:
# lo que importa es que no ande suelto en el cache de Poetry, que es lo que se
# mueve y se borra sin que nadie se entere.
$entornoLocal = $python -like "$APP*"

if (-not $entornoLocal -and $python -like "$env:USERPROFILE*") {
    ""
    "AVISO: el entorno esta en el cache de Poetry, dentro del perfil de $env:USERNAME."
    "El servicio corre como LocalSystem y puede no tener acceso ahi."
    ""
    "Poner virtualenvs.in-project en true NO mueve el entorno que ya existe:"
    "solo aplica a los nuevos. Hay que borrar el viejo para que lo recree aqui."
    "Con el servicio detenido:"
    "    cd `"$APP`""
    "    poetry env list                 # ver como se llama"
    "    poetry env remove <ese-nombre>  # por nombre; --all no borra nada"
    "    poetry install"
    "y volver a correr 'instalar'. Debe quedar en $APP\.venv"
    ""
}
elseif ($APP -like "$env:USERPROFILE*") {
    ""
    "Nota: el proyecto vive dentro del perfil de $env:USERNAME. Funciona, porque"
    "LocalSystem entra a los perfiles, pero deja el servicio atado a esa cuenta."
    "Si algun dia se reconstruye el perfil, el servicio se queda sin codigo."
    "Lo mas solido es moverlo a una ruta propia (C:\IoTDataPipeline) y, tras"
    "mover la carpeta, correr 'poetry install' otra vez: el .venv guarda rutas"
    "absolutas y no sobrevive a la mudanza."
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

$svc = Get-Service -Name $Nombre -ErrorAction SilentlyContinue
"servicio : $($svc.Status)  (arranque: $($svc.StartType))"

# NSSM dice Running en cuanto lanza el proceso, aunque el recolector truene un
# segundo despues. Lo unico que prueba que esta vivo es que atienda el puerto,
# y eso tarda: conexiones a la base, catalogo, primer ciclo de PLCs.
$puerto = PuertoDelEnv
"salud    : consultando el puerto $puerto..."

$salud = $null
foreach ($intento in 1..12) {
    Start-Sleep -Seconds 5
    $salud = Salud $puerto 3
    if ($salud) { break }
}

if ($salud) {
    "salud    : $salud"
    ""
    "Listo. Arranca solo con el servidor y sobrevive al cierre de sesion."
} else {
    ""
    "El puerto $puerto no responde despues de un minuto. El servicio dice"
    "Running, pero el recolector no esta atendiendo. Revisa:"
    "   Get-Content `"$logs\servicio.err`" -Tail 20"
    "   Get-Content `"$logs\supervisor.log`" -Tail 20"
}

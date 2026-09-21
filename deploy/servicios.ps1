# Todos los servicios del proyecto, con un solo comando.
#
#   .\servicios.ps1 estado      ver como estan (no pide administrador)
#   .\servicios.ps1 instalar    registrarlos y arrancarlos
#   .\servicios.ps1 detener     pararlos, sin darlos de baja
#   .\servicios.ps1 arrancar    volver a levantarlos
#   .\servicios.ps1 reiniciar   despues de un despliegue
#   .\servicios.ps1 quitar      darlos de baja (desinstalar)
#
# 'detener' los para pero siguen registrados: vuelven solos con el proximo
# reinicio del servidor. Para que NO vuelvan, 'quitar'.
#
# Por omision actua sobre los cinco. Para solo una parte:
#   .\servicios.ps1 instalar -Que recolector
#   .\servicios.ps1 reiniciar -Que observabilidad
#
# Son CINCO servicios y no uno solo a proposito: asi NSSM vigila y relanza cada
# proceso por separado. Si los cinco colgaran de un mismo servicio, la caida de
# Grafana —que solo sirve para VER— se llevaria entre las patas al recolector,
# que es el que no puede faltar.
#
# Requiere NSSM (portable, sin instalador): https://nssm.cc/download
# Descomprimir win64\nssm.exe en C:\iot\obs\nssm\

param(
    [Parameter(Position = 0)]
    [ValidateSet("estado", "instalar", "detener", "arrancar", "quitar", "reiniciar")]
    [string]$Accion = "estado",

    [ValidateSet("todos", "recolector", "observabilidad")]
    [string]$Que = "todos",

    [string]$Nssm = "C:\iot\obs\nssm\nssm.exe",
    [string]$Obs  = $env:IOT_OBS
)

$ErrorActionPreference = "Stop"
$APP    = Split-Path -Parent $PSScriptRoot
$DEPLOY = $PSScriptRoot
if (-not $Obs) { $Obs = "C:\iot\obs" }
$OBS  = $Obs.TrimEnd('\')
$GEN  = Join-Path $DEPLOY ".generado"
$LOGS = Join-Path $APP "logs"

. (Join-Path $PSScriptRoot "puertos.ps1")
$puertos = PuertosDelProyecto $APP

$RECOLECTOR = "IoT-Recolector"

function EsAdministrador {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function PythonDelEntorno {
    # Primero el entorno junto al codigo: es el que conviene para un servicio,
    # y ademas 'poetry env info' sigue devolviendo el del cache mientras ese
    # exista (virtualenvs.in-project solo aplica a entornos NUEVOS).
    $local = Join-Path $APP ".venv\Scripts\python.exe"
    if (Test-Path $local) { return $local }

    Push-Location $APP
    try { $py = (& poetry env info --executable 2>$null | Select-Object -First 1) }
    finally { Pop-Location }

    if (-not $py -or -not (Test-Path $py)) {
        throw "No se encontro el python del entorno. Corre 'poetry install' en $APP primero."
    }
    return $py.Trim()
}

function Salud($puerto, $segundos) {
    # -UseBasicParsing y sin proxy: con un proxy de sistema configurado, la
    # llamada a localhost se queda colgada en vez de respetar el timeout.
    try {
        $r = Invoke-WebRequest "http://localhost:$puerto/health" `
                 -TimeoutSec $segundos -UseBasicParsing -Proxy $null
        return $r.Content
    } catch { return $null }
}

function ArbolDelServicio($nombre) {
    # TODOS los procesos que cuelgan del servicio, no solo los hijos directos.
    # NSSM se registra a si mismo y lanza la aplicacion como hija; esa a su vez
    # puede tener hijos propios. Cualquiera de ellos es del servicio, y contarlo
    # como "suelto" saca una advertencia de doble conteo que no es cierta.
    $svc = Get-CimInstance Win32_Service -Filter "Name='$nombre'" -ErrorAction SilentlyContinue
    if (-not $svc -or -not $svc.ProcessId) { return @() }

    $todos = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $arbol = @([int]$svc.ProcessId)

    $crecio = $true
    while ($crecio) {
        $crecio = $false
        foreach ($p in $todos) {
            if (($arbol -contains [int]$p.ParentProcessId) -and
                ($arbol -notcontains [int]$p.ProcessId)) {
                $arbol += [int]$p.ProcessId
                $crecio = $true
            }
        }
    }
    return $arbol
}

function RecolectoresSueltos {
    # Los que NO cuelgan del servicio: esos son los que cuentan doble.
    $arbol = ArbolDelServicio $RECOLECTOR
    Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "Prensas\.py" } |
        Where-Object { $arbol -notcontains [int]$_.ProcessId }
}

# ── que servicios toca cada '-Que' ─────────────────────────────────────────
function Definiciones {
    $lista = @()

    if ($Que -in @("todos", "recolector")) {
        $lista += @{
            Nombre = $RECOLECTOR
            Titulo = "IoT Recolector de Produccion"
            Desc   = "Lee los contadores de los PLC y registra la produccion contra las ordenes."
            Exe    = { PythonDelEntorno }
            Args   = "Prensas.py"
            Dir    = $APP
            Logs   = $LOGS
            Puerto = $puertos.Recolector
        }
    }

    if ($Que -in @("todos", "observabilidad")) {
        $lista += @{
            Nombre = "IoT-Prometheus"
            Titulo = "IoT Prometheus"
            Desc   = "Guarda las metricas del recolector."
            Exe    = { Join-Path $OBS "prometheus\prometheus.exe" }
            Args   = "--config.file=$GEN\prometheus.yml " +
                     "--storage.tsdb.path=$OBS\datos\prometheus " +
                     "--storage.tsdb.retention.time=90d " +
                     "--web.listen-address=:$($puertos.Prometheus)"
            Dir    = $OBS
            Logs   = (Join-Path $OBS "logs")
            Puerto = $puertos.Prometheus
        }
        $lista += @{
            Nombre = "IoT-Loki"
            Titulo = "IoT Loki"
            Desc   = "Guarda los logs de las estaciones."
            Exe    = { Join-Path $OBS "loki\loki-windows-amd64.exe" }
            Args   = "-config.file=$GEN\loki.yml"
            Dir    = $OBS
            Logs   = (Join-Path $OBS "logs")
            Puerto = $puertos.Loki
        }
        $lista += @{
            Nombre = "IoT-Alloy"
            Titulo = "IoT Alloy"
            Desc   = "Lee los archivos de log del recolector y los manda a Loki."
            Exe    = { Join-Path $OBS "alloy\alloy-windows-amd64.exe" }
            Args   = "run $GEN\alloy.alloy --storage.path=$OBS\datos\alloy " +
                     "--server.http.listen-addr=127.0.0.1:$($puertos.Alloy)"
            Dir    = $OBS
            Logs   = (Join-Path $OBS "logs")
            Puerto = $puertos.Alloy
        }
        $lista += @{
            Nombre = "IoT-Grafana"
            Titulo = "IoT Grafana"
            Desc   = "Los tableros."
            Exe    = { Join-Path $OBS "grafana\bin\grafana.exe" }
            Args   = "server --homepath $OBS\grafana"
            Dir    = (Join-Path $OBS "grafana")
            Logs   = (Join-Path $OBS "logs")
            Puerto = $puertos.Grafana
            # El puerto por variable de entorno: le gana al custom.ini y evita
            # editar la configuracion de Grafana en cada maquina.
            Env    = "GF_SERVER_HTTP_PORT=$($puertos.Grafana)"
        }
    }

    return $lista
}

# ── estado ─────────────────────────────────────────────────────────────────
if ($Accion -eq "estado") {
    ""
    "{0,-16} {1,-10} {2,-11} {3}" -f "servicio", "estado", "arranque", "puerto"
    "-" * 56
    foreach ($d in Definiciones) {
        $svc = Get-Service -Name $d.Nombre -ErrorAction SilentlyContinue
        $estado   = if ($svc) { "$($svc.Status)" } else { "no existe" }
        $arranque = if ($svc) { "$($svc.StartType)" } else { "-" }
        $quien    = PuertoOcupadoPor $d.Puerto
        $puerto   = if ($quien) { "$($d.Puerto) ($($quien.Name))" } else { "$($d.Puerto) libre" }
        "{0,-16} {1,-10} {2,-11} {3}" -f $d.Nombre, $estado, $arranque, $puerto
    }

    if ($Que -in @("todos", "recolector")) {
        $sueltos = @(RecolectoresSueltos)
        if ($sueltos.Count -gt 0) {
            ""
            "OJO: $($sueltos.Count) recolector(es) FUERA del servicio."
            "Con el servicio arriba, la produccion se cuenta DOBLE. Para matarlos:"
            $sueltos | ForEach-Object { "   Stop-Process -Id $($_.ProcessId) -Force   # padre: $($_.ParentProcessId)" }
        }

        $salud = Salud $puertos.Recolector 3
        ""
        if ($salud) {
            "salud: $salud"
        } else {
            "el puerto $($puertos.Recolector) no responde: no hay recolector atendiendo"
        }
    }
    return
}

# ── las demas acciones piden administrador ────────────────────────────────
if (-not (EsAdministrador)) {
    throw "'$Accion' necesita PowerShell como Administrador."
}

# ── detener / arrancar ─────────────────────────────────────────────────────
if ($Accion -in @("detener", "arrancar")) {
    foreach ($d in Definiciones) {
        $svc = Get-Service -Name $d.Nombre -ErrorAction SilentlyContinue
        if (-not $svc) { "$($d.Nombre): no esta registrado"; continue }

        if ($Accion -eq "detener") { & $Nssm stop  $d.Nombre | Out-Null }
        else                       { & $Nssm start $d.Nombre | Out-Null }
        Start-Sleep -Seconds 2
        "$($d.Nombre): $((Get-Service -Name $d.Nombre).Status)"
    }

    if ($Accion -eq "detener") {
        ""
        "Siguen registrados: vuelven solos al reiniciar el servidor."
        "Para que NO vuelvan:  .\servicios.ps1 quitar"
    }
    return
}

# ── quitar ─────────────────────────────────────────────────────────────────
if ($Accion -eq "quitar") {
    foreach ($d in Definiciones) {
        if (-not (Get-Service -Name $d.Nombre -ErrorAction SilentlyContinue)) {
            "$($d.Nombre): no estaba registrado"
            continue
        }
        & $Nssm stop $d.Nombre confirm | Out-Null
        Start-Sleep -Seconds 2
        & $Nssm remove $d.Nombre confirm | Out-Null
        "$($d.Nombre): dado de baja"
    }
    return
}

# ── reiniciar ──────────────────────────────────────────────────────────────
if ($Accion -eq "reiniciar") {
    # La configuracion se regenera: un despliegue pudo cambiar rutas o puertos,
    # y los servicios leen .generado, no las plantillas del repositorio.
    if ($Que -in @("todos", "observabilidad")) {
        & (Join-Path $DEPLOY "iniciar-observabilidad.ps1") -SoloGenerar -Obs $OBS | Out-Null
    }
    foreach ($d in Definiciones) {
        if (-not (Get-Service -Name $d.Nombre -ErrorAction SilentlyContinue)) {
            "$($d.Nombre): no esta registrado, se omite"
            continue
        }
        & $Nssm restart $d.Nombre | Out-Null
        Start-Sleep -Seconds 2
        "$($d.Nombre): $((Get-Service -Name $d.Nombre).Status)"
    }
    return
}

# ── instalar ───────────────────────────────────────────────────────────────
if (-not (Test-Path $Nssm)) {
    throw ("No esta NSSM en $Nssm. Bajalo de https://nssm.cc/download y " +
           "descomprime win64\nssm.exe ahi (o usa -Nssm con otra ruta).")
}

if ($Que -in @("todos", "observabilidad")) {
    "Generando la configuracion con las rutas y puertos de esta maquina..."
    & (Join-Path $DEPLOY "iniciar-observabilidad.ps1") -SoloGenerar -Obs $OBS | Out-Null
}

""
"proyecto : $APP"
"binarios : $OBS"
"puertos  : " + (($puertos.Keys | ForEach-Object { "$_=$($puertos[$_])" }) -join "  ")

# Un recolector fuera del servicio cuenta la produccion doble.
if ($Que -in @("todos", "recolector")) {
    $sueltos = @(RecolectoresSueltos)
    if ($sueltos.Count -gt 0) {
        ""
        "Deteniendo $($sueltos.Count) recolector(es) fuera del servicio..."
        $sueltos | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 3
    }
}

foreach ($d in Definiciones) {
    $exe = & $d.Exe
    if (-not (Test-Path $exe)) {
        ""
        "$($d.Nombre): no existe $exe  --  se omite"
        continue
    }

    New-Item -ItemType Directory -Force $d.Logs | Out-Null

    if (Get-Service -Name $d.Nombre -ErrorAction SilentlyContinue) {
        & $Nssm stop   $d.Nombre confirm | Out-Null
        Start-Sleep -Seconds 2
        & $Nssm remove $d.Nombre confirm | Out-Null
        Start-Sleep -Seconds 2
    }

    $corto = $d.Nombre.Replace("IoT-", "").ToLower()
    & $Nssm install $d.Nombre $exe             | Out-Null
    & $Nssm set $d.Nombre AppParameters $d.Args | Out-Null
    & $Nssm set $d.Nombre AppDirectory  $d.Dir  | Out-Null
    & $Nssm set $d.Nombre DisplayName   $d.Titulo | Out-Null
    & $Nssm set $d.Nombre Description   $d.Desc   | Out-Null
    & $Nssm set $d.Nombre Start SERVICE_AUTO_START | Out-Null
    & $Nssm set $d.Nombre AppStdout (Join-Path $d.Logs "$corto-servicio.log") | Out-Null
    & $Nssm set $d.Nombre AppStderr (Join-Path $d.Logs "$corto-servicio.err") | Out-Null
    & $Nssm set $d.Nombre AppRotateFiles 1        | Out-Null
    & $Nssm set $d.Nombre AppRotateBytes 10485760 | Out-Null

    # Si se cae, relanzarlo. Los 10 s evitan un ciclo de reinicios cuando el
    # fallo es de arranque (la base inalcanzable, un puerto ocupado).
    & $Nssm set $d.Nombre AppExit Default Restart | Out-Null
    & $Nssm set $d.Nombre AppRestartDelay 10000   | Out-Null
    & $Nssm set $d.Nombre AppThrottle     10000   | Out-Null

    if ($d.Env) { & $Nssm set $d.Nombre AppEnvironmentExtra $d.Env | Out-Null }

    & $Nssm start $d.Nombre | Out-Null
    "$($d.Nombre): instalado en el puerto $($d.Puerto)"
}

# ── comprobacion ───────────────────────────────────────────────────────────
if ($Que -in @("todos", "recolector")) {
    ""
    "Esperando a que el recolector atienda el puerto $($puertos.Recolector)..."
    $salud = $null
    foreach ($intento in 1..12) {
        Start-Sleep -Seconds 5
        $salud = Salud $puertos.Recolector 3
        if ($salud) { break }
    }
    if ($salud) {
        "salud: $salud"
    } else {
        ""
        "El puerto $($puertos.Recolector) no responde despues de un minuto. Revisa:"
        "   Get-Content `"$LOGS\recolector-servicio.err`" -Tail 20"
        "   Get-Content `"$LOGS\supervisor.log`" -Tail 20"
    }
}

""
"Listo. Arrancan solos con el servidor y sobreviven al cierre de sesion."
"   .\servicios.ps1 estado"

# Levanta TODO: Prometheus, Loki, Alloy, Grafana y el recolector.
#
#   .\iniciar-observabilidad.ps1                   arrancar todo (sin ventanas)
#   .\iniciar-observabilidad.ps1 -Detener          detener todo
#   .\iniciar-observabilidad.ps1 -SinRecolector    solo la observabilidad
#   .\iniciar-observabilidad.ps1 -Visible          con ventanas, para diagnosticar
#   .\iniciar-observabilidad.ps1 -Obs D:\binarios  binarios en otra ruta
#   .\iniciar-observabilidad.ps1 -PuertoGrafana 8080   si el 3000 esta ocupado
#
# El proyecto se ubica solo (este script vive en <proyecto>\deploy), así que
# funciona sin importar dónde esté clonado el repositorio.
#
# Los binarios van FUERA del repositorio: son cientos de MB y no son código
# nuestro. Por omisión en C:\iot\obs; se cambia con -Obs o con IOT_OBS.

param(
    [switch]$Detener,
    [switch]$SinRecolector,   # levantar solo la observabilidad
    [switch]$Visible,         # con ventanas, para diagnosticar
    [switch]$SoloGenerar,     # escribir .generado y salir, sin arrancar nada
    [string]$Obs = $env:IOT_OBS,

    # Los puertos viven en el .env (ver deploy\puertos.ps1). Estos parámetros
    # solo sirven para una prueba puntual sin tocar el archivo; 0 = usar el .env.
    [int]$PuertoGrafana    = 0,
    [int]$PuertoPrometheus = 0,
    [int]$PuertoLoki       = 0,
    [int]$PuertoAlloy      = 0,
    [int]$PuertoRecolector = 0
)

$ErrorActionPreference = "Stop"

$APP    = Split-Path -Parent $PSScriptRoot
$DEPLOY = $PSScriptRoot
if (-not $Obs) { $Obs = "C:\iot\obs" }
$OBS = $Obs.TrimEnd('\')
$GEN  = Join-Path $DEPLOY ".generado"
$LOGS = Join-Path $OBS "logs"

# Todos los puertos salen del .env. Un parámetro solo los pisa para una prueba
# puntual. Si se escribieran aquí, tarde o temprano quedarían distintos de los
# del recolector y Prometheus raspara un puerto donde no hay nadie: los tableros
# saldrían vacíos sin decir por qué.
. (Join-Path $PSScriptRoot "puertos.ps1")
$puertos = PuertosDelProyecto $APP

if ($PuertoGrafana    -eq 0) { $PuertoGrafana    = $puertos.Grafana }
if ($PuertoPrometheus -eq 0) { $PuertoPrometheus = $puertos.Prometheus }
if ($PuertoLoki       -eq 0) { $PuertoLoki       = $puertos.Loki }
if ($PuertoAlloy      -eq 0) { $PuertoAlloy      = $puertos.Alloy }
if ($PuertoRecolector -eq 0) { $PuertoRecolector = $puertos.Recolector }

$PUERTOS = @($PuertoGrafana, $PuertoPrometheus, $PuertoLoki,
             $PuertoAlloy, $PuertoRecolector)

# Los procesos arrancan OCULTOS. Una ventana oculta no deja ver nada si algo
# truena, asi que la salida de cada uno se guarda en <OBS>\logs\<nombre>.log
# y .err. Con -Visible salen en ventana, que es lo util cuando algo falla.
$estiloVentana = if ($Visible) { "Minimized" } else { "Hidden" }

function Arrancar($nombre, $exe, $argumentos, $directorio) {
    $opciones = @{
        FilePath               = $exe
        ArgumentList           = $argumentos
        WindowStyle            = $estiloVentana
        RedirectStandardOutput = (Join-Path $LOGS "$nombre.log")
        RedirectStandardError  = (Join-Path $LOGS "$nombre.err")
    }
    if ($directorio) { $opciones.WorkingDirectory = $directorio }
    Start-Process @opciones
}

# Ojo con los nombres de proceso: Loki y Alloy corren como
# 'loki-windows-amd64' y 'alloy-windows-amd64', no como 'loki' / 'alloy'.
$procesos = @(
    @{ Nombre = "prometheus";          Exe = Join-Path $OBS "prometheus\prometheus.exe" },
    @{ Nombre = "loki-windows-amd64";  Exe = Join-Path $OBS "loki\loki-windows-amd64.exe" },
    @{ Nombre = "alloy-windows-amd64"; Exe = Join-Path $OBS "alloy\alloy-windows-amd64.exe" },
    @{ Nombre = "grafana";             Exe = Join-Path $OBS "grafana\bin\grafana.exe" }
)

# ── Detener TODO ───────────────────────────────────────────────────────────
if ($Detener) {
    $alguno = $false

    # 1) El recolector (uno o varios procesos de python)
    $rec = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -match "Prensas\.py" }
    foreach ($r in $rec) {
        Stop-Process -Id $r.ProcessId -Force -ErrorAction SilentlyContinue
        "  detenido: recolector (PID $($r.ProcessId))"
        $alguno = $true
    }

    # 2) La observabilidad
    foreach ($p in $procesos) {
        $enCurso = Get-Process -Name $p.Nombre -ErrorAction SilentlyContinue
        if ($enCurso) {
            $enCurso | Stop-Process -Force
            "  detenido: $($p.Nombre)"
            $alguno = $true
        }
    }

    # 3) Los plugins de Grafana quedan huérfanos al matar al padre
    Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "gpx_*" } | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        "  detenido: $($_.Name) (plugin de Grafana)"
        $alguno = $true
    }

    # 4) Red de seguridad: lo que siga escuchando en nuestros puertos
    Start-Sleep -Seconds 2
    foreach ($puerto in $PUERTOS) {
        Get-NetTCPConnection -LocalPort $puerto -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
            $pr = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
            if ($pr) {
                Stop-Process -Id $pr.Id -Force -ErrorAction SilentlyContinue
                "  detenido: $($pr.Name) que seguia en el puerto $puerto"
                $alguno = $true
            }
        }
    }

    if (-not $alguno) { "  no habia nada corriendo" }

    Start-Sleep -Seconds 1
    $sigue = $PUERTOS | Where-Object {
        (Test-NetConnection localhost -Port $_ -WarningAction SilentlyContinue).TcpTestSucceeded
    }
    if ($sigue) { "  AVISO: siguen abiertos los puertos $($sigue -join ', ')" }
    else { "  todos los puertos cerrados" }
    return
}

"Proyecto : $APP"
"Binarios : $OBS"
""

# ── Verificar los ejecutables ──────────────────────────────────────────────
# Solo bloquea el ARRANQUE. Generar la configuración no depende de los binarios,
# y si faltara uno y aquí se cortara, .generado quedaría a medias: Grafana
# encontraría la carpeta de tableros vacía y borraría los que ya tenía.
$faltan = $procesos | Where-Object { -not (Test-Path $_.Exe) }
if ($faltan -and -not $SoloGenerar) {
    Write-Host "Faltan ejecutables:" -ForegroundColor Red
    $faltan | ForEach-Object { Write-Host "  no existe: $($_.Exe)" -ForegroundColor Red }
    Write-Host ""
    Write-Host "Descomprime los ZIP segun la tabla del README, o indica otra ruta:" -ForegroundColor Yellow
    Write-Host "  .\iniciar-observabilidad.ps1 -Obs D:\ruta\a\binarios" -ForegroundColor Yellow
    exit 1
}

# ── Resolver las plantillas ────────────────────────────────────────────────
# Los archivos del repositorio traen __APP__ y __OBS__ en vez de rutas fijas.
# Aquí se escribe la versión con rutas reales en .generado\, que no se versiona:
# así el repositorio queda limpio en todas las máquinas.
New-Item -ItemType Directory -Force $GEN | Out-Null
New-Item -ItemType Directory -Force (Join-Path $GEN "provisioning\dashboards")  | Out-Null
New-Item -ItemType Directory -Force (Join-Path $GEN "provisioning\datasources") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $GEN "dashboards")               | Out-Null

function Resolver($origen, $destino) {
    # __APP_UNIX__ lleva diagonales normales: el archivo de Alloy usa cadenas
    # estilo Go, donde la diagonal invertida es un escape y "C:\Users\..."
    # rompe el parseo. Windows acepta ambas para rutas de archivo.
    # Leer con UTF-8 EXPLICITO. Get-Content -Raw en Windows PowerShell 5.1 —el
    # que trae el servidor— decodifica como ANSI cuando el archivo no lleva BOM,
    # y los acentos se rompen: "producción" sale como "producciÃ³n" en el
    # tablero. Aquí no se nota porque PowerShell 7 ya asume UTF-8.
    $appUnix = $APP.Replace('\', '/')
    $texto = [System.IO.File]::ReadAllText($origen, [System.Text.Encoding]::UTF8).
        Replace('__APP_UNIX__', $appUnix).
        Replace('__APP__', $APP).
        Replace('__OBS__', $OBS).
        Replace('__GEN__', $GEN).
        Replace('__PUERTO_GRAFANA__',    "$PuertoGrafana").
        Replace('__PUERTO_PROMETHEUS__', "$PuertoPrometheus").
        Replace('__PUERTO_LOKI__',       "$PuertoLoki").
        Replace('__PUERTO_ALLOY__',      "$PuertoAlloy").
        Replace('__PUERTO_RECOLECTOR__', "$PuertoRecolector")

    # Sin BOM a propósito: Set-Content -Encoding UTF8 lo agrega en Windows
    # PowerShell 5.1, y el lector de JSON de Grafana truena con él.
    [System.IO.File]::WriteAllText(
        $destino, $texto, (New-Object System.Text.UTF8Encoding($false)))
}

Resolver (Join-Path $DEPLOY "loki.yml")       (Join-Path $GEN "loki.yml")
Resolver (Join-Path $DEPLOY "alloy.alloy")    (Join-Path $GEN "alloy.alloy")
Resolver (Join-Path $DEPLOY "prometheus.yml") (Join-Path $GEN "prometheus.yml")
Copy-Item (Join-Path $DEPLOY "alertas.yml")   (Join-Path $GEN "alertas.yml") -Force
Resolver (Join-Path $DEPLOY "grafana\provisioning\dashboards\dashboards.yml") `
         (Join-Path $GEN "provisioning\dashboards\dashboards.yml")
Resolver (Join-Path $DEPLOY "grafana\provisioning\datasources\datasources.yml") `
         (Join-Path $GEN "provisioning\datasources\datasources.yml")

# Los tableros pasan por aquí porque uno enlaza al recolector por su puerto.
Get-ChildItem (Join-Path $DEPLOY "grafana\dashboards") -Filter *.json | ForEach-Object {
    Resolver $_.FullName (Join-Path $GEN "dashboards\$($_.Name)")
}
"  configuracion generada en $GEN"

# ── El custom.ini de Grafana ───────────────────────────────────────────────
# Este era el unico paso manual que quedaba, y el mas facil de equivocar: la
# ruta tiene que terminar en \provisioning. Si apunta un nivel arriba, Grafana
# NO da error: busca los .yml donde no estan, provisiona cero cosas en medio
# milisegundo y —con disableDeletion en false— borra los tableros que ya tenia.
# Queda la carpeta vacia y ni una linea en el log que lo explique.
function AsegurarCustomIni {
    $ini      = Join-Path $OBS "grafana\conf\custom.ini"
    $correcta = Join-Path $GEN "provisioning"

    if (-not (Test-Path $ini)) {
        $contenido = @(
            "[paths]",
            "provisioning = $correcta",
            "",
            "[users]",
            "default_theme = dark",
            "",
            "[analytics]",
            "reporting_enabled = false",
            "check_for_updates = false"
        ) -join "`r`n"
        New-Item -ItemType Directory -Force (Split-Path $ini) | Out-Null
        [System.IO.File]::WriteAllText($ini, $contenido, (New-Object System.Text.UTF8Encoding($false)))
        "  custom.ini creado con la ruta de provision"
        return
    }

    # UTF-8 explícito, por lo mismo que en Resolver: en PowerShell 5.1,
    # Get-Content sin -Encoding rompe los acentos de lo que ya estuviera aquí.
    $lineas = @([System.IO.File]::ReadAllLines($ini, [System.Text.Encoding]::UTF8))
    $actual = ($lineas | Where-Object { $_ -match '^\s*provisioning\s*=' } | Select-Object -First 1)

    if ($actual -and $actual.Trim() -eq "provisioning = $correcta") {
        "  custom.ini ya apunta a la provision correcta"
        return
    }

    if ($actual) {
        $nuevas = $lineas | ForEach-Object {
            if ($_ -match '^\s*provisioning\s*=') { "provisioning = $correcta" } else { $_ }
        }
        "  custom.ini corregido:"
        "     antes:   $($actual.Trim())"
        "     ahora:   provisioning = $correcta"
    } elseif ($lineas -match '^\s*\[paths\]') {
        $nuevas = @()
        foreach ($l in $lineas) {
            $nuevas += $l
            if ($l -match '^\s*\[paths\]') { $nuevas += "provisioning = $correcta" }
        }
        "  custom.ini: se agrego la ruta de provision a [paths]"
    } else {
        $nuevas = @("[paths]", "provisioning = $correcta", "") + $lineas
        "  custom.ini: se agrego la seccion [paths] con la ruta de provision"
    }

    [System.IO.File]::WriteAllText($ini, ($nuevas -join "`r`n"),
                                   (New-Object System.Text.UTF8Encoding($false)))
}

AsegurarCustomIni

# ── Carpetas de datos (fuera del repositorio) ──────────────────────────────
foreach ($sub in @("datos", "datos\loki", "datos\prometheus", "datos\alloy", "logs")) {
    New-Item -ItemType Directory -Force (Join-Path $OBS $sub) | Out-Null
}

# Lo usa servicios.ps1: los servicios no ejecutan este script, pero necesitan
# que .generado exista y esté al día antes de arrancar.
if ($SoloGenerar) {
    ""
    "Configuracion lista. Los servicios la leen de $GEN"
    return
}

# ── Arrancar ───────────────────────────────────────────────────────────────
""
Arrancar "prometheus" (Join-Path $OBS "prometheus\prometheus.exe") @(
    "--config.file=$(Join-Path $GEN 'prometheus.yml')",
    "--storage.tsdb.path=$(Join-Path $OBS 'datos\prometheus')",
    "--storage.tsdb.retention.time=90d",
    "--web.listen-address=:$PuertoPrometheus"
)
"  Prometheus  -> http://localhost:$PuertoPrometheus"

Arrancar "loki" (Join-Path $OBS "loki\loki-windows-amd64.exe") @(
    "-config.file=$(Join-Path $GEN 'loki.yml')"
)
"  Loki        -> http://localhost:$PuertoLoki"

Start-Sleep -Seconds 3   # Alloy necesita que Loki ya escuche

Arrancar "alloy" (Join-Path $OBS "alloy\alloy-windows-amd64.exe") @(
    "run", "$(Join-Path $GEN 'alloy.alloy')",
    "--storage.path=$(Join-Path $OBS 'datos\alloy')",
    "--server.http.listen-addr=127.0.0.1:$PuertoAlloy"
)
"  Alloy       -> leyendo $APP\logs\*.log   (http://localhost:$PuertoAlloy)"

# La variable de entorno le gana al custom.ini, asi que el puerto se manda
# desde aqui sin editar la configuracion de Grafana en cada maquina.
$env:GF_SERVER_HTTP_PORT = "$PuertoGrafana"
Arrancar "grafana" (Join-Path $OBS "grafana\bin\grafana.exe") @(
    "server", "--homepath", (Join-Path $OBS "grafana")
)
"  Grafana     -> http://localhost:$PuertoGrafana  (admin / admin)"

# ── El recolector ──────────────────────────────────────────────────────────
if (-not $SinRecolector) {
    $yaCorre = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
               Where-Object { $_.CommandLine -match "Prensas\.py" }
    if ($yaCorre) {
        "  Recolector  -> ya estaba corriendo (PID $($yaCorre.ProcessId -join ', '))"
    } else {
        Arrancar "recolector" "poetry" @("run", "python", "Prensas.py") $APP
        "  Recolector  -> http://localhost:$PuertoRecolector/estaciones/lecturas"
    }
}

""
"Grafana necesita saber donde esta la provision (una sola vez por maquina)."
"En $(Join-Path $OBS 'grafana\conf\custom.ini'):"
"  [paths]"
"  provisioning = $(Join-Path $GEN 'provisioning')"
""
"Para detener TODO:  .\iniciar-observabilidad.ps1 -Detener"

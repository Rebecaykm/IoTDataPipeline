# Los puertos del proyecto, en UN solo lugar: el .env.
#
# Lo cargan los dos scripts de deploy\ con:
#   . (Join-Path $PSScriptRoot "puertos.ps1")
#   $puertos = PuertosDelProyecto $APP
#
# Por que el .env y no un archivo aparte: el recolector ya lee su puerto de ahi
# (HTTP_PORT), el .env no se versiona —o sea, cada maquina tiene el suyo— y no
# hay que acordarse de un segundo lugar. Un puerto escrito en dos archivos es
# un puerto que tarde o temprano queda distinto en cada uno.
#
# En el .env:
#   HTTP_PORT=9100          <- el recolector; lo lee tambien Prensas.py
#   PUERTO_GRAFANA=3000
#   PUERTO_PROMETHEUS=9090
#   PUERTO_LOKI=3100
#   PUERTO_ALLOY=12345
#
# Lo que no este en el .env toma el valor de siempre, asi que un .env sin
# ninguna de estas lineas sigue funcionando igual que antes.

function PuertosDelProyecto {
    param([Parameter(Mandatory)][string]$App)

    $puertos = [ordered]@{
        Recolector = 9100
        Grafana    = 3000
        Prometheus = 9090
        Loki       = 3100
        Alloy      = 12345
    }

    $deEnv = @{
        "HTTP_PORT"         = "Recolector"
        "PUERTO_GRAFANA"    = "Grafana"
        "PUERTO_PROMETHEUS" = "Prometheus"
        "PUERTO_LOKI"       = "Loki"
        "PUERTO_ALLOY"      = "Alloy"
    }

    $archivo = Join-Path $App ".env"
    if (Test-Path $archivo) {
        foreach ($linea in (Get-Content $archivo)) {
            # Acepta comentario al final y espacios alrededor; ignora lo demas.
            if ($linea -match '^\s*([A-Za-z_]+)\s*=\s*(\d+)\s*(#.*)?$') {
                $llave = $Matches[1].ToUpper()
                if ($deEnv.ContainsKey($llave)) {
                    $puertos[$deEnv[$llave]] = [int]$Matches[2]
                }
            }
        }
    }

    return $puertos
}

function PuertoOcupadoPor {
    # Quien esta escuchando en ese puerto, o $null si esta libre.
    param([int]$Puerto)

    $con = Get-NetTCPConnection -LocalPort $Puerto -State Listen -ErrorAction SilentlyContinue
    if (-not $con) { return $null }
    return Get-Process -Id $con[0].OwningProcess -ErrorAction SilentlyContinue
}

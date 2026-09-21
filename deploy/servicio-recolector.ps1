# Atajo: hace lo mismo que servicios.ps1 pero solo sobre el recolector.
#
#   .\servicio-recolector.ps1 estado
#   .\servicio-recolector.ps1 instalar
#   .\servicio-recolector.ps1 detener
#   .\servicio-recolector.ps1 arrancar
#   .\servicio-recolector.ps1 reiniciar
#   .\servicio-recolector.ps1 quitar
#
# Para manejar tambien Prometheus, Loki, Alloy y Grafana:
#   .\servicios.ps1 instalar

param(
    [Parameter(Position = 0)]
    [ValidateSet("estado", "instalar", "detener", "arrancar", "quitar", "reiniciar")]
    [string]$Accion = "estado",

    [string]$Nssm = "C:\iot\obs\nssm\nssm.exe"
)

& (Join-Path $PSScriptRoot "servicios.ps1") $Accion -Que recolector -Nssm $Nssm

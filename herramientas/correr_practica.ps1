# Corre el circuito completo contra el simulador (partido de práctica acelerado) y evalúa con verificar.py
param([double]$Acelerar = 30, [string]$Desafio = "$PSScriptRoot\..\..\desafio-futbol", [int]$InicioMin = 0)
$ErrorActionPreference = "Stop"
$raiz = Resolve-Path "$PSScriptRoot\.."
$des = Resolve-Path $Desafio
$sim = Start-Process py -ArgumentList "simulador/servidor.py","--escenario","datos/escenario_practica.json","--acelerar","$Acelerar","--inicio-min","$InicioMin","--log","salida/registro.jsonl" -WorkingDirectory $des -PassThru -RedirectStandardOutput "$raiz\salida_sim.txt" -RedirectStandardError "$raiz\salida_sim_err.txt" -WindowStyle Hidden
Start-Sleep 1
$plat = Start-Process py -ArgumentList "main.py","--simulador","http://localhost:8080" -WorkingDirectory $raiz -PassThru -RedirectStandardOutput "$raiz\salida_plat.txt" -RedirectStandardError "$raiz\salida_plat_err.txt" -WindowStyle Hidden
Start-Sleep 1
py "$raiz\herramientas\jueces_demo.py"
Start-Sleep 2
Stop-Process -Id $plat.Id -Force -ErrorAction SilentlyContinue
Stop-Process -Id $sim.Id -Force -ErrorAction SilentlyContinue
Push-Location $des
py evaluacion/verificar.py --log salida/registro.jsonl
Pop-Location
py "$raiz\herramientas\verificar_entrega.py" "$raiz\entrega"

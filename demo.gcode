; Simulacao somente - nao imprimir
; T0 = extrusor da impressora (padrao) | T1 = Arduino
G90
M83
T0
;LAYER_CHANGE
G1 X10 Y10 F1200
G1 X20 E0.6
T1
G1 X30 E0.6
G1 E-1 F300
;LAYER_CHANGE
T0
G92 E0
M82
G1 X40 E0.7 F1200
M105

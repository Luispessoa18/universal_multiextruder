"""Gera a peça de teste de 2 cores para a Vyper + extrusor externo.

Sequência:
  1. Início do perfil (aquece, home, malha, purga com T0). Se T0 estiver estacionado, T0 avança.
  2. T0 imprime um círculo de 10 mm (1 camada, 2 voltas).
  3. Recolhe, sobe e faz home em X/Y (estaciona no canto, longe da peça).
  4. Troca para T1: a ponte recua T0 até o estacionamento e avança T1 até o alvo.
  5. Purga do T1 e círculo de 10 mm do T1.
  6. Volta para T0 e finaliza.

Variantes:
  'sim'  - completa: quadrado de purga e círculo do T1 com extrusão. Só roda em SIMULAÇÃO
           (no modo real T1 com XYZ+E é bloqueado: as placas não são sincronizadas).
  'real' - roda no modo real: T1 purga parado no ar (só E) e o círculo do T1 é feito
           como contorno sem extrusão, para conferir posição e altura.
"""
import math

FIL_AREA = math.pi * (1.75 / 2) ** 2


def _e(length, width=0.45, height=0.2):
    return length * width * height / FIL_AREA


def _circle(cx, cy, r, z, extrude=True, seg=48, feed=1200):
    out = [f'G1 Z{z + 0.6:.2f} F900', f'G1 X{cx + r:.3f} Y{cy:.3f} F6000', f'G1 Z{z:.2f} F600']
    step = 2 * math.pi * r / seg
    for k in range(1, seg + 1):
        a = 2 * math.pi * k / seg
        mv = f'G1 X{cx + r * math.cos(a):.3f} Y{cy + r * math.sin(a):.3f}'
        out.append(mv + (f' E{_e(step):.5f}' if extrude else '') + (f' F{feed}' if k == 1 else ''))
    return out


def _square(x0, y0, size, z, loops=4, gap=0.45, feed=1200):
    out = [f'G1 Z{z + 0.6:.2f} F900', f'G1 X{x0:.2f} Y{y0:.2f} F6000', f'G1 Z{z:.2f} F600']
    for i in range(loops):
        a, b, s = x0 + i * gap, y0 + i * gap, size - 2 * i * gap
        pts = [(a + s, b), (a + s, b + s), (a, b + s), (a, b)]
        out.append(f'G1 X{a:.2f} Y{b:.2f}')
        for k, (x, y) in enumerate(pts):
            out.append(f'G1 X{x:.2f} Y{y:.2f} E{_e(s):.5f}' + (f' F{feed}' if k == 0 else ''))
    return out


def generate(prof, variant='real', hotend=210, bed=60, diameter=10.0):
    if variant not in ('sim', 'real'):
        raise ValueError('Variante inválida')
    bx, by = float(prof['bed_mm']['x']), float(prof['bed_mm']['y'])
    if not (180 <= hotend <= prof['max_hotend_c'] and 0 <= bed <= prof['max_bed_c']):
        raise ValueError('Temperaturas fora do limite do perfil')
    r, z = diameter / 2, 0.2
    c0 = (bx / 2 - 15, by / 2)   # círculo T0
    c1 = (bx / 2 + 15, by / 2)   # círculo T1
    tpl = (prof.get('slicer') or {}).get('start_gcode_orca_prusa') or ['G90', 'M82', 'G28', 'T0', 'G92 E0']
    g = [f'; Teste 2 cores ({"simulação completa" if variant == "sim" else "modo real"}) - Universal Multiextruder',
         f'; perfil: {prof["name"]} | círculos de {diameter:g} mm | bico {hotend} °C | mesa {bed} °C']
    for line in tpl:
        g.append(line.replace('[first_layer_temperature]', str(hotend)).replace('[first_layer_bed_temperature]', str(bed)))
    g += ['M83', 'G92 E0', 'M106 S0',
          ';LAYER_CHANGE', ';Z:0.2',
          '; --- 1) T0: círculo de 10 mm ---', 'T0', 'G1 E0.8 F1800']
    g += _circle(*c0, r, z) + _circle(*c0, r - 0.45, z)
    g += ['; --- 2) recolhe, sobe e home X/Y (estaciona longe da peça) ---',
          'G1 E-1 F1800', 'G1 Z5 F900', 'G28 X Y',
          '; --- 3) troca para T1 (ponte: recua T0 até o estacionamento, avança T1 até o alvo) ---',
          'T1', f'M109 S{hotend}']
    if variant == 'sim':
        g += ['; --- 4) quadrado de purga T1 ---'] + _square(15, 15, 20, z)
        g += ['; --- 5) T1: círculo de 10 mm ---', 'G1 E-1 F1800'] + ['G1 E1 F1800']
        g += _circle(*c1, r, z) + _circle(*c1, r - 0.45, z)
        g += ['G1 E-1 F1800']
    else:
        g += ['; --- 4) purga T1 parada no ar (no modo real T1 só extruda sem mover XYZ) ---',
              'G1 X10 Y10 F6000', 'G1 Z10 F900',
              'G1 E25 F300', 'G4 S2', 'G1 E-1 F1800',
              '; --- 5) T1: contorno do círculo SEM extrusão (confere posição/altura) ---']
        g += _circle(*c1, r, z, extrude=False)
    g += ['G1 Z5 F900', 'G28 X Y',
          '; --- 6) volta para T0 ---', 'T0']
    g += (prof.get('slicer') or {}).get('end_gcode') or ['M104 S0', 'M140 S0', 'M84']
    return '\n'.join(g) + '\n'

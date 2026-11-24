"""Converte G-code de outros fatiadores/impressoras (Bambu Studio, OrcaSlicer, PrusaSlicer;
.gcode ou .gcode.3mf) para a Vyper + extrusor externo.

- Lê as cores/filamentos do 3MF (slice_info.config) ou do bloco de configuração do G-code.
- Mapeia N filamentos do fatiador para as portas físicas T0/T1 (ex.: 4 cores -> 2).
  Trocas entre filamentos mapeados para a mesma porta são eliminadas.
- Troca o G-code inicial/final da impressora de origem pelos do perfil da Vyper.
- Remove blocos da Bambu (M620/M621 troca do AMS, M622/M623 condicionais) e comandos
  que a Vyper não tem; converte arcos G2/G3 em segmentos G1.
- Centraliza a peça na mesa da Vyper (opcional).

O arquivo é processado em streaming (duas passadas) para não estourar a memória do Orange Pi.
"""
import collections, io, json, math, os, re, tempfile, zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path


class ConvertError(Exception):
    pass


_PLATE = re.compile(r'Metadata/plate_(\d+)\.gcode$')
_CFG = re.compile(r'^;\s*([A-Za-z0-9_]+)\s*=\s*(.*)$')
_LAYER = re.compile(r'^;\s*(?:LAYER_CHANGE|CHANGE_LAYER)\b|^;\s*LAYER\s*:\s*-?\d+', re.I)
_ZMARK = re.compile(r'^;\s*(?:Z_HEIGHT|Z)\s*:\s*([-\d.]+)', re.I)
_CMD = re.compile(r'^([GMT])(\d+)(?:\.(\d+))?\s*(.*)$')
_PARAM = re.compile(r'([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))')
_TLINE = re.compile(r'^[Tt](\d+)\s*(?:;|$)')
_HEX = re.compile(r'^#?([0-9A-Fa-f]{6})')
CFG_KEYS = {'filament_colour', 'filament_type', 'extruder_colour', 'nozzle_temperature_initial_layer',
            'first_layer_temperature', 'curr_bed_type', 'hot_plate_temp_initial_layer',
            'cool_plate_temp_initial_layer', 'eng_plate_temp_initial_layer', 'textured_plate_temp_initial_layer',
            'supertack_plate_temp_initial_layer', 'first_layer_bed_temperature',
            'bed_temperature_initial_layer_single', 'printer_model', 'printer_settings_id'}
BED_KEYS = {'cool plate': 'cool_plate_temp_initial_layer', 'engineering plate': 'eng_plate_temp_initial_layer',
            'high temp plate': 'hot_plate_temp_initial_layer', 'textured pei plate': 'textured_plate_temp_initial_layer',
            'supertack plate': 'supertack_plate_temp_initial_layer'}
PHYSICAL = ('T0', 'T1')
SEGMENT_MM = 0.4


def color(c):
    m = _HEX.match((c or '').strip())
    return '#' + m.group(1).upper() if m else '#888888'


def _split(v):
    return [x.strip() for x in re.split(r'[;,]', v or '') if x.strip()]


def _num(v, i=0):
    vals = _split(v) if isinstance(v, str) else [str(x) for x in (v or [])]
    if not vals:
        return None
    try:
        return float(vals[i] if i < len(vals) else vals[0])
    except ValueError:
        return None


def _fmt(v):
    s = f'{v:.5f}'.rstrip('0').rstrip('.')
    return '0' if s in ('', '-0') else s


def _params(rest):
    return {k: float(v) for k, v in _PARAM.findall(rest)}


def output_name(name, plate, nplates):
    n = name
    for suf in ('.gcode.3mf', '.3mf', '.gcode', '.gco', '.gc', '.g'):
        if n.lower().endswith(suf):
            n = n[:-len(suf)]
            break
    return (f'{n}_p{plate}_vyper' if nplates > 1 else f'{n}_vyper') + '.gcode'


@contextmanager
def open_lines(path, member=None):
    if member:
        try:
            z = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            raise ConvertError('Arquivo 3MF inválido ou corrompido')
        with z, z.open(member) as raw:
            yield io.TextIOWrapper(raw, encoding='utf-8', errors='replace')
    else:
        with open(path, encoding='utf-8-sig', errors='replace') as f:
            yield f


# --- análise -----------------------------------------------------------------
def scan(path, member=None):
    """Passada rápida: configuração, sequência de ferramentas e nº de camadas."""
    cfg, seq, layers, bambu = {}, [], 0, False
    with open_lines(path, member) as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            c = s[0]
            if c == ';':
                if _LAYER.match(s):
                    layers += 1
                else:
                    m = _CFG.match(s)
                    if m and m.group(1) in CFG_KEYS and m.group(1) not in cfg:
                        cfg[m.group(1)] = m.group(2).strip()
            elif c in 'Tt':
                m = _TLINE.match(s)
                if m:
                    n = int(m.group(1))
                    if n < 255 and (not seq or seq[-1] != n):
                        seq.append(n)
            elif c in 'Mm' and s[:4].upper() == 'M620':
                bambu = True
    return {'cfg': cfg, 'seq': seq, 'layers': layers, 'bambu': bambu}


def _slice_info(data):
    res = {}
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return res
    for plate in root.iter('plate'):
        idx = next((md.get('value') for md in plate.findall('metadata') if md.get('key') == 'index'), None)
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            continue
        fl = []
        for f in plate.findall('filament'):
            try:
                n = int(f.get('id')) - 1
            except (TypeError, ValueError):
                continue
            try:
                used = float(f.get('used_g') or 0)
            except ValueError:
                used = None
            fl.append({'index': n, 'color': color(f.get('color')), 'type': f.get('type') or '', 'used_g': used})
        res[idx] = fl
    return res


def _plate(idx, member, sc, finfo, proj, thumb):
    cfg = sc['cfg']
    colors = _split(cfg.get('filament_colour')) or list(proj.get('filament_colour') or []) \
        or _split(cfg.get('extruder_colour'))
    types = _split(cfg.get('filament_type')) or list(proj.get('filament_type') or [])
    fil = {}
    for n in sorted(set(sc['seq'])) or [0]:
        fil[n] = {'index': n, 'color': color(colors[n] if n < len(colors) else None),
                  'type': types[n] if n < len(types) else '', 'used_g': None}
    for f in finfo or []:
        fil.setdefault(f['index'], {}).update(f)
    fl = [fil[k] for k in sorted(fil)]
    return {'plate': idx, 'member': member, 'thumbnail': thumb, 'filaments': fl, 'sequence': sc['seq'],
            'changes': max(0, len(sc['seq']) - 1), 'layers': sc['layers'], 'bambu': sc['bambu'],
            'printer': cfg.get('printer_model') or cfg.get('printer_settings_id') or '', 'multicolor': len(fl) > 1}


def analyze(path):
    path = Path(path)
    if path.name.lower().endswith('.3mf'):
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                plates = sorted((int(m.group(1)), n) for n in names for m in [_PLATE.match(n)] if m)
                info = _slice_info(z.read('Metadata/slice_info.config')) if 'Metadata/slice_info.config' in names else {}
                proj = {}
                if 'Metadata/project_settings.config' in names:
                    try:
                        proj = json.loads(z.read('Metadata/project_settings.config'))
                    except ValueError:
                        pass
        except zipfile.BadZipFile:
            raise ConvertError('Arquivo 3MF inválido ou corrompido')
        if not plates:
            n = len(proj.get('filament_colour') or [])
            raise ConvertError('Este 3MF é um projeto não fatiado' + (f' ({n} filamentos)' if n else '')
                               + '. No Bambu Studio/OrcaSlicer, fatie e use "Exportar arquivo fatiado da placa" '
                                 '(.gcode.3mf) ou exporte o G-code.')
        out = [_plate(i, m, scan(path, m), info.get(i), proj, f'Metadata/plate_{i}.png' in names) for i, m in plates]
    else:
        out = [_plate(1, None, scan(path), None, {}, False)]
    return {'file': path.name, 'plates': out}


def thumbnail(path, plate):
    with zipfile.ZipFile(path) as z:
        return z.read(f'Metadata/plate_{int(plate)}.png')


# --- conversão ---------------------------------------------------------------
class _Conv:
    def __init__(self, out, mapping, prof, cfg, seq):
        self.out, self.n, self.map, self.prof, self.cfg, self.seq = out, 0, mapping, prof, cfg, seq
        tc = {'lift_mm': 2.0, 'park_xy': [5, 5], 'purge_mm': 0.0, 'first_layer_z': 0.2, **prof.get('tool_change', {})}
        self.lift, self.purge = float(tc['lift_mm']), float(tc['purge_mm'])
        self.park = [float(v) for v in tc['park_xy']]
        self.default_z = float(tc['first_layer_z'])
        # estado do arquivo de origem
        self.abs, self.rel_e, self.pos, self.e = True, False, {'X': 0., 'Y': 0., 'Z': 0.}, 0.
        # estado do que já foi emitido para a Vyper
        self.eabs, self.erel_e, self.e_dirty = True, False, False
        self.epos = dict(self.pos)   # onde a Vyper realmente está (coords. da origem)
        self.body, self.block, self.cond = False, None, 0
        self.src_tool = self.phys = self.first_z = None
        self.need_z = False
        self.pre = {'hot': [], 'bed': []}
        self.bbox = [math.inf, math.inf, -math.inf, -math.inf]
        self.last_ext = -1
        self.dropped = collections.Counter()
        self.changes = self.kept = self.merged = self.arcs = 0
        self.g92xyz = False
        self.hot = self.bed = None
        self.warnings = []

    def write(self, line):
        self.out.write(line + '\n')
        self.n += 1
        return self.n - 1

    def sync(self):
        """Antes de emitir um comando, alinha modos/E com o arquivo de origem (após trechos removidos)."""
        if self.eabs != self.abs:
            self.write('G90' if self.abs else 'G91'); self.eabs = self.abs
        if self.erel_e != self.rel_e:
            self.write('M83' if self.rel_e else 'M82'); self.erel_e = self.rel_e
            self.e_dirty = True
        if self.e_dirty:
            if not self.rel_e:
                self.write(f'G92 E{_fmt(self.e)}')
            self.e_dirty = False

    # ---------------------------------------------------------------------
    def feed(self, s):
        if s[0] == ';':
            if _LAYER.match(s):
                if not self.body:
                    self.start_body()
                self.write(';LAYER_CHANGE')
            elif self.body and self.first_z is None:
                m = _ZMARK.match(s)
                if m:
                    self.first_z = float(m.group(1))
            return
        s = s.split(';', 1)[0].strip().upper()
        if not s:
            return
        m = _CMD.match(s)
        if not m:
            self.dropped['?'] += 1
            return
        L, num, sub, rest = m.groups()
        code = L + num
        full = code + ('.' + sub if sub else '')
        if full == 'M622':
            self.cond += 1; self.dropped[full] += 1; return
        if full == 'M623':
            self.cond = max(0, self.cond - 1); self.dropped[full] += 1; return
        if full == 'M620' and rest.startswith('S') and self.block is None:
            self.block = {'tool': None, 'temps': [], 'pos': dict(self.epos)}; return
        if full == 'M621' and self.block is not None:
            b, self.block = self.block, None
            if b['tool'] is not None:
                self.tool(b['tool'], b['temps'], b['pos'])
            return
        hidden = (not self.body) or self.block is not None or self.cond > 0
        if L == 'T':
            n = int(num)
            if sub or rest or n >= 255:
                return
            if self.block is not None:
                self.block['tool'] = n
            elif not self.cond:
                self.tool(n, [], dict(self.epos))
            return
        if full in ('G0', 'G1'):
            return self.move(code, _params(rest), hidden)
        if full in ('G2', 'G3'):
            return self.arc(code, _params(rest), hidden)
        if full in ('G90', 'G91'):
            self.abs = full == 'G90'
            if not hidden:
                self.write(full); self.eabs = self.abs
            return
        if full in ('M82', 'M83'):
            self.rel_e = full == 'M83'
            if hidden:
                self.e_dirty = True
            else:
                self.write(full); self.erel_e = self.rel_e
            return
        if full == 'G92':
            p = _params(rest)
            if not hidden:
                self.sync()
            if 'E' in p:
                self.e = p['E']
            xyz = False
            for a in 'XYZ':
                if a in p:
                    self.pos[a] = p[a]; xyz = True
            if hidden:
                if 'E' in p:
                    self.e_dirty = True
                return
            self.g92xyz |= xyz
            if p:
                self.write('G92 ' + ' '.join(f'{k}{_fmt(v)}' for k, v in p.items() if k in 'XYZE'))
            return
        if full in ('M104', 'M109', 'M140', 'M190'):
            p = _params(rest)
            key = 'S' if 'S' in p else 'R' if 'R' in p else None
            if key is None:
                self.dropped[full] += 1; return
            v = p[key]
            if not self.body:
                if v > 0:
                    self.pre['bed' if full in ('M140', 'M190') else 'hot'].append(v)
                return
            if self.block is not None:
                if full in ('M104', 'M109'):
                    self.block['temps'].append(v)
                return
            if self.cond:
                return
            self.sync(); self.write(f'{full} {key}{_fmt(v)}')
            return
        if full in ('M106', 'M107'):
            if hidden:
                return
            p = _params(rest)
            if 'P' in p and int(p['P']) not in (0, 1):   # Bambu: P1 peça, P2 auxiliar, P3 câmara
                self.dropped[full + ' P' + str(int(p['P']))] += 1
                return
            self.sync()
            self.write('M107' if full == 'M107' else f'M106 S{_fmt(p.get("S", 255))}')
            return
        if full == 'G4':
            if not hidden:
                self.sync(); self.write(('G4 ' + rest).strip())
            return
        self.dropped[full] += 1

    def move(self, code, p, hidden):
        p = {k: v for k, v in p.items() if k in 'XYZEF'}
        if not hidden:
            self.sync()
        sx, sy = self.epos['X'], self.epos['Y']
        for a in 'XYZ':
            if a in p:
                self.pos[a] = p[a] if self.abs else self.pos[a] + p[a]
        de = 0.
        if 'E' in p:
            de = p['E'] if self.rel_e else p['E'] - self.e
            self.e += de
        if hidden:
            if 'E' in p and not self.rel_e:
                self.e_dirty = True
            return
        if 'Z' in p:
            self.need_z = False
        elif self.need_z and any(a in p for a in 'XYE'):
            z = self.first_z if self.first_z is not None else self.default_z
            if self.eabs:
                self.write(f'G1 Z{_fmt(z)} F600')
            else:
                self.write('G90'); self.write(f'G1 Z{_fmt(z)} F600'); self.write('G91')
            self.pos['Z'] = self.epos['Z'] = z
            self.need_z = False
        if not p:
            return
        for a in 'XYZ':
            if a in p:
                self.epos[a] = self.pos[a]
        idx = self.write(code + ' ' + ' '.join(f'{k}{_fmt(v)}' for k, v in p.items()))
        if de > 1e-9 and (self.pos['X'] != sx or self.pos['Y'] != sy):
            b = self.bbox
            b[0] = min(b[0], sx, self.pos['X']); b[1] = min(b[1], sy, self.pos['Y'])
            b[2] = max(b[2], sx, self.pos['X']); b[3] = max(b[3], sy, self.pos['Y'])
            self.last_ext = idx

    def arc(self, code, p, hidden):
        if 'R' in p:
            raise ConvertError('Arco G2/G3 com R não suportado; desative "Arc fitting" no fatiador')
        sx, sy, sz = self.pos['X'], self.pos['Y'], self.pos['Z']
        def end(a, s):
            return (p[a] if self.abs else s + p[a]) if a in p else s
        ex, ey, ez = end('X', sx), end('Y', sy), end('Z', sz)
        de = (p['E'] if self.rel_e else p['E'] - self.e) if 'E' in p else 0.
        if hidden or de <= 1e-9:
            segs = [(ex, ey, ez, 1.0)]   # deslocamento (ex.: subida em espiral da Bambu) vira reta
        else:
            self.arcs += 1
            cx, cy = sx + p.get('I', 0.), sy + p.get('J', 0.)
            r = math.hypot(sx - cx, sy - cy)
            a0, a1 = math.atan2(sy - cy, sx - cx), math.atan2(ey - cy, ex - cx)
            sweep = a1 - a0
            if code == 'G2' and sweep >= -1e-9:
                sweep -= 2 * math.pi
            elif code == 'G3' and sweep <= 1e-9:
                sweep += 2 * math.pi
            n = min(2000, max(1, math.ceil(abs(sweep) * r / SEGMENT_MM)))
            segs = [(cx + r * math.cos(a0 + sweep * k / n), cy + r * math.sin(a0 + sweep * k / n),
                     sz + (ez - sz) * k / n, k / n) for k in range(1, n)] + [(ex, ey, ez, 1.0)]
        e0 = self.e
        travel = len(segs) == 1 and not ('X' in p or 'Y' in p)
        for i, (x, y, z, t) in enumerate(segs):
            q = {}
            if travel:   # arco sem X/Y (subida em espiral): só Z
                if 'Z' in p:
                    q['Z'] = z if self.abs else z - self.pos['Z']
            elif self.abs:
                q['X'], q['Y'] = x, y
                if 'Z' in p:
                    q['Z'] = z
            else:
                q['X'], q['Y'] = x - self.pos['X'], y - self.pos['Y']
                if 'Z' in p:
                    q['Z'] = z - self.pos['Z']
            if 'E' in p:
                q['E'] = de / len(segs) if self.rel_e else e0 + de * t
            if i == 0 and 'F' in p:
                q['F'] = p['F']
            self.move('G1', q, hidden)

    def tool(self, n, temps, ret):
        if not self.body:
            self.src_tool = n
            return
        if n == self.src_tool:
            return
        self.src_tool = n
        self.changes += 1
        phys = self.map.get(n)
        if phys is None:
            raise ConvertError(f'Filamento {n + 1} não tem porta definida')
        if phys == self.phys:
            self.merged += 1
            return
        self.kept += 1
        self.sync()
        wrap = not self.eabs
        if wrap:
            self.write('G90')
        z = ret['Z']
        self.write(f';MMU troca: filamento {n + 1} -> {phys}')
        self.write(f'G1 Z{_fmt(z + self.lift)} F900')
        self.write(f'G1 X{_fmt(self.park[0])} Y{_fmt(self.park[1])} F9000 ;NOSHIFT')
        self.write(phys)
        hot = [v for v in temps if v > 0]
        if hot:
            self.write(f'M109 S{_fmt(hot[-1])}')
        if self.purge > 0:
            if self.erel_e:
                self.write(f'G1 E{_fmt(self.purge)} F300')
            else:
                self.write('G92 E0'); self.write(f'G1 E{_fmt(self.purge)} F300'); self.e_dirty = True
        self.write(f'G1 X{_fmt(ret["X"])} Y{_fmt(ret["Y"])} F9000')
        self.write(f'G1 Z{_fmt(z)} F900')
        if wrap:
            self.write('G91')
        self.pos = dict(ret)
        self.epos = dict(ret)
        self.phys = phys

    def temps(self, initial):
        c = self.cfg
        hot = _num(c.get('nozzle_temperature_initial_layer'), initial) or _num(c.get('first_layer_temperature'), initial) \
            or (max(self.pre['hot']) if self.pre['hot'] else None)
        key = BED_KEYS.get((c.get('curr_bed_type') or '').strip().lower())
        bed = (_num(c.get(key), initial) if key else None) or _num(c.get('first_layer_bed_temperature'), initial) \
            or _num(c.get('bed_temperature_initial_layer_single')) or (max(self.pre['bed']) if self.pre['bed'] else None)
        if hot is None:
            raise ConvertError('Não encontrei a temperatura do bico no arquivo')
        if bed is None:
            bed = 60.0
            self.warnings.append('Temperatura da mesa não encontrada; usando 60 °C')
        if hot > self.prof['max_hotend_c'] or bed > self.prof['max_bed_c']:
            raise ConvertError(f'Temperaturas do arquivo ({hot:g}/{bed:g} °C) acima dos limites da {self.prof["name"]}')
        return hot, bed

    def start_body(self):
        self.body = True
        initial = self.src_tool if self.src_tool is not None else (self.seq[0] if self.seq else 0)
        self.src_tool = initial
        phys = self.map.get(initial)
        if phys is None:
            raise ConvertError(f'Filamento {initial + 1} não tem porta definida')
        self.hot, self.bed = self.temps(initial)
        tpl = (self.prof.get('slicer') or {}).get('start_gcode_orca_prusa') or \
            ['G90', 'M82', 'M140 S[first_layer_bed_temperature]', 'M104 S[first_layer_temperature]', 'G28',
             'M190 S[first_layer_bed_temperature]', 'M109 S[first_layer_temperature]', 'T0', 'G92 E0']
        self.write(';MMU início (perfil ' + self.prof['name'] + ')')
        for line in tpl:
            l = line.replace('[first_layer_temperature]', _fmt(self.hot)).replace('[first_layer_bed_temperature]', _fmt(self.bed))
            u = l.split(';', 1)[0].strip().upper()
            if u in ('T0', 'T1'):
                l = phys
            elif u in ('G90', 'G91'):
                self.eabs = u == 'G90'
            elif u in ('M82', 'M83'):
                self.erel_e = u == 'M83'
            self.write(l + (' ;NOSHIFT' if u.startswith(('G0 ', 'G1 ')) else ''))
        self.write(';MMU fim do início')
        self.phys = phys
        self.e_dirty = True
        self.need_z = True


def _shift(line, dx, dy):
    return re.sub(r'([XY])(-?\d+(?:\.\d+)?)',
                  lambda m: m.group(1) + _fmt(float(m.group(2)) + (dx if m.group(1) == 'X' else dy)), line)


def convert(path, member, mapping, prof, center, out_path, source_name=None):
    """mapping: {índice do filamento (0-based): 'T0'|'T1'}"""
    mapping = {int(k): v for k, v in mapping.items()}
    if any(v not in PHYSICAL for v in mapping.values()):
        raise ConvertError('Porta inválida no mapeamento (use T0 ou T1)')
    out_path = Path(out_path)
    sc = scan(path, member)
    used = sorted(set(sc['seq'])) or [0]
    missing = [n + 1 for n in used if n not in mapping]
    if missing:
        raise ConvertError('Defina a porta dos filamentos: ' + ', '.join(map(str, missing)))
    fd, tmp = tempfile.mkstemp(suffix='.tmp', dir=out_path.parent)
    fd2, tmp2 = tempfile.mkstemp(suffix='.tmp', dir=out_path.parent)
    os.close(fd2)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as t:
            conv = _Conv(t, mapping, prof, sc['cfg'], sc['seq'])
            with open_lines(path, member) as f:
                for line in f:
                    s = line.strip()
                    if s:
                        conv.feed(s)
        if not conv.body:
            raise ConvertError('Não encontrei marcadores de camada (;LAYER_CHANGE, ; CHANGE_LAYER ou ;LAYER:)')
        if conv.last_ext < 0:
            raise ConvertError('Nenhum movimento de extrusão encontrado')
        bx, by = float(prof['bed_mm']['x']), float(prof['bed_mm']['y'])
        x0, y0, x1, y1 = conv.bbox
        if x1 - x0 > bx or y1 - y0 > by:
            raise ConvertError(f'Peça + torre de purga ({x1 - x0:.1f} × {y1 - y0:.1f} mm) maior que a mesa ({bx:g} × {by:g} mm)')
        dx, dy = ((bx / 2 - (x0 + x1) / 2), (by / 2 - (y0 + y1) / 2)) if center else (0., 0.)
        nb = [x0 + dx, y0 + dy, x1 + dx, y1 + dy]
        if nb[0] < 0 or nb[1] < 0 or nb[2] > bx or nb[3] > by:
            raise ConvertError('Peça fora da mesa da Vyper; ative "Centralizar na mesa"')
        if center and conv.g92xyz:
            conv.warnings.append('O arquivo usa G92 em X/Y/Z: confira a centralização na simulação')
        if conv.kept and conv.purge <= 0 and sc['bambu']:
            conv.warnings.append('A purga da Bambu (fora da mesa) foi removida. Use torre de purga no fatiador '
                                 'ou configure tool_change.purge_mm no perfil.')
        if 'T1' in mapping.values():
            conv.warnings.append('T1 no modo real ainda é bloqueado (falta sincronizar XYZ+E entre placas). '
                                 'Com T1 o arquivo roda só em simulação; para imprimir agora, mapeie tudo para T0.')
        with open(tmp, encoding='utf-8') as src, open(tmp2, 'w', encoding='utf-8', newline='\n') as out:
            out.write(f'; Convertido pelo Universal Multiextruder para {prof["name"]}\n')
            out.write(f'; origem: {source_name or Path(path).name}' + (f' ({member})' if member else '') + '\n')
            for n in used:
                out.write(f'; mapa: filamento {n + 1} -> {mapping[n]}\n')
            out.write(f'; deslocamento XY: {dx:.2f} {dy:.2f}\n')
            absmode = True
            for i, line in enumerate(src):
                if i > conv.last_ext:
                    break
                line = line.rstrip('\n')
                ns = line.endswith(';NOSHIFT')
                if ns:
                    line = line[:-8].rstrip()
                if line in ('G90', 'G91'):
                    absmode = line == 'G90'
                elif absmode and not ns and (dx or dy) and line.startswith(('G1 ', 'G0 ')):
                    line = _shift(line, dx, dy)
                out.write(line + '\n')
            out.write(';MMU fim (perfil ' + prof['name'] + ')\n')
            for l in (prof.get('slicer') or {}).get('end_gcode') or ['M104 S0', 'M140 S0', 'M107', 'M84']:
                out.write(l + '\n')
        os.replace(tmp2, out_path)
    finally:
        for f in (tmp, tmp2):
            try:
                os.unlink(f)
            except OSError:
                pass
    return {'output': out_path.name, 'filaments': len(used), 'changes_original': conv.changes,
            'changes_kept': conv.kept, 'changes_merged': conv.merged, 'arcs': conv.arcs,
            'dropped': dict(conv.dropped.most_common(20)), 'offset': [round(dx, 2), round(dy, 2)],
            'bbox': [round(v, 1) for v in nb], 'hotend': conv.hot, 'bed': conv.bed, 'warnings': conv.warnings}

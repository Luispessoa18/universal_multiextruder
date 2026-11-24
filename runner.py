"""Execução de G-code compartilhada entre a interface desktop (app.py) e a web (server.py).

Mapa de ferramentas:
  T0 = extrusor nativo da impressora (padrão)
  T1 = extrusor externo no Arduino
"""
import builtins, collections, json, math, queue, re, sys, threading, time
from pathlib import Path
import bridge

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / 'config.json'
PROFILE_DIR = ROOT / 'profiles'
TOOLS = ('T0', 'T1')
TOOL_MAP = {'T0': 'vyper', 'T1': 'arduino'}

_linux = sys.platform.startswith('linux')
DEFAULTS = {
    'printer_port': '/dev/ttyUSB0' if _linux else 'COM3',
    'mmu_port': '/dev/ttyACM0' if _linux else 'COM4',
    'printer_baud': 115200,
    'mmu_baud': 9600,
    'steps_mm': 5.482,
    'rpm': 20,
    # Geometria (mm a partir da saída de cada extrusora):
    #   T0: tubo 540 até o hub | T1: tubo 470 até o hub
    #   hub 90 mm, troca aos 45 mm | tubo final 50 mm até o hotend
    #   estacionamento = 10 mm antes do ponto de troca; alvo = fim do tubo final (não o bico)
    'p0': 575.0,    # T0 estacionamento: 540 + 45 - 10
    'l0': 680.0,    # T0 alvo: 540 + 90 + 50
    'p1': 505.0,    # T1 estacionamento: 470 + 45 - 10
    'l1': 610.0,    # T1 alvo: 470 + 90 + 50
    'native_change_feed': 300,
    'minimum_hotend_c': 180,
    'initial_tool': 'T0',   # T0 | T1 | none
    'profile': 'vyper',     # arquivo profiles/<nome>.json
    'color_t0': '#FFFFFF',  # cor do filamento carregado em cada porta (para o mapeamento multicolor)
    'color_t1': '#000000',
}
INTS = ('printer_baud', 'mmu_baud', 'rpm', 'native_change_feed', 'minimum_hotend_c')
FLOATS = ('steps_mm', 'p0', 'l0', 'p1', 'l1')
LIVE_CODES = {'G0', 'G1', 'G4', 'G21', 'G28', 'G29', 'G90', 'G91', 'G92', 'M82', 'M83', 'M84', 'M104', 'M105', 'M106',
              'M107', 'M109', 'M114', 'M115', 'M117', 'M140', 'M190', 'M204', 'M205', 'M400', 'M73', 'T0', 'T1'}
PROFILE_DEFAULTS = {'name': 'Genérica', 'bed_mm': {'x': 220, 'y': 220, 'z': 250}, 'bounds_margin_mm': 5,
                    'max_hotend_c': 260, 'max_bed_c': 110, 'strip_codes': ['M201', 'M203', 'M204', 'M205'],
                    'speed_factor': 100, 'flow_factor': 100}
# Ajustes ao vivo: tipo -> (mínimo, máximo, comando)
ADJUST = {'speed': (10, 300, 'M220 S{:d}'), 'flow': (50, 150, 'M221 S{:d}'), 'fan': (0, 100, None),
          'hotend': (0, None, 'M104 S{:d}'), 'bed': (0, None, 'M140 S{:d}')}


def load_profile(name='vyper'):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', str(name)):
        raise ValueError('Nome de perfil inválido')
    path = PROFILE_DIR / f'{name}.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except OSError:
        raise ValueError(f'Perfil não encontrado: {path.name}')
    except ValueError as e:
        raise ValueError(f'Perfil {path.name} com JSON inválido: {e}')
    prof = {**PROFILE_DEFAULTS, **data}
    prof['strip_codes'] = [str(c).upper() for c in prof['strip_codes']]
    for k, lo, hi in (('speed_factor', 10, 300), ('flow_factor', 50, 150)):
        prof[k] = int(prof[k])
        if not lo <= prof[k] <= hi:
            raise ValueError(f'{k} do perfil fora de {lo}–{hi}')
    return prof


def list_profiles():
    return sorted(f.stem for f in PROFILE_DIR.glob('*.json'))
BAMBU_CODES = {'M620', 'M621', 'M622', 'M623', 'M1002'}
RUNNING_STATES = ('preflight', 'connecting', 'running', 'paused')


def load_settings():
    s = dict(DEFAULTS)
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        s.update({k: v for k, v in saved.items() if k in DEFAULTS})
        return normalize(s)
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(s):
    CONFIG_PATH.write_text(json.dumps(normalize(s), indent=2), encoding='utf-8')


def normalize(raw):
    """Converte e valida configurações vindas de formulário/JSON."""
    s = dict(DEFAULTS)
    s.update({k: raw[k] for k in DEFAULTS if k in raw})
    k = ''
    try:
        for k in INTS:
            s[k] = int(str(s[k]).strip())
        for k in FLOATS:
            s[k] = float(str(s[k]).strip().replace(',', '.'))
            if not math.isfinite(s[k]):
                raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f'Valor inválido em {k}')
    s['printer_port'] = str(s['printer_port']).strip()
    s['mmu_port'] = str(s['mmu_port']).strip()
    if s['initial_tool'] not in TOOLS + ('none',):
        raise ValueError('Estado inicial inválido')
    for k in ('color_t0', 'color_t1'):
        s[k] = str(s[k]).strip().upper()
        if not re.fullmatch(r'#[0-9A-F]{6}', s[k]):
            raise ValueError(f'Cor inválida em {k}')
    s['profile'] = str(s['profile']).strip()
    load_profile(s['profile'])
    if not (0 < s['steps_mm'] <= 500 and 1 <= s['rpm'] <= 60 and s['native_change_feed'] > 0
            and 0 <= s['p1'] < s['l1'] and 0 <= s['p0'] <= s['l0']
            and s['printer_baud'] > 0 and s['mmu_baud'] > 0 and 150 <= s['minimum_hotend_c'] <= 300):
        raise ValueError('Calibração/distâncias inválidas')
    return s


def check_start(s, live, confirm):
    if live and not confirm:
        raise ValueError('Confirme geometria, posição inicial, sentido e temperatura antes do modo real')
    if live and s['printer_port'].upper() == s['mmu_port'].upper():
        raise ValueError('Impressora e Arduino precisam de portas distintas')


def build_config(s, confirm=False):
    """Gera o dicionário completo que bridge.Bridge espera."""
    initial = s['initial_tool'] if s['initial_tool'] in TOOLS else None
    park = {'T0': s['p0'], 'T1': s['p1']}
    load = {'T0': s['l0'], 'T1': s['l1']}
    c = dict(s)
    c.update(tool_map=dict(TOOL_MAP), initial_tool=initial, park_mm=park, load_target_mm=load,
             enable_physical_tool_changes=bool(confirm), confirm_geometry=bool(confirm),
             initial_positions_mm={t: load[t] if t == initial else park[t] for t in TOOLS})
    return c


_LAYER_CHANGE = re.compile(r';\s*(?:LAYER_CHANGE|CHANGE_LAYER)\b', re.I)     # PrusaSlicer / Orca / SuperSlicer
_LAYER_NUM = re.compile(r';\s*LAYER\s*:\s*-?\d+', re.I)      # Cura


def prepare(lines, strip_codes=()):
    """Remove comentários e códigos do perfil (aceleração etc.), rejeita numeração/checksum
    e anota a camada de cada comando. Retorna (comandos, camadas, {código: removidos})."""
    marker = _LAYER_CHANGE if any(_LAYER_CHANGE.match(l.strip()) for l in lines) else _LAYER_NUM
    strip = set(strip_codes)
    prepared, layer, stripped = [], 0, collections.Counter()
    for idx, line in enumerate(lines, 1):
        s = line.strip()
        if marker.match(s):
            layer += 1
        raw = s.split(';', 1)[0].strip().upper()
        if not raw:
            continue
        if raw.startswith('N') or '*' in raw:
            raise ValueError(f'Linha {idx}: use arquivo G-code sem numeração/checksum')
        code = re.match(r'[A-Z]\d*', raw)
        if code and code.group() in BAMBU_CODES:
            raise ValueError(f'Linha {idx}: G-code da Bambu Lab ({code.group()}). Use "Analisar / converter" para adaptar à Vyper')
        if code and code.group() in strip:
            stripped[code.group()] += 1
            continue
        prepared.append((idx, raw, layer))
    if not prepared:
        raise ValueError('Arquivo vazio')
    return prepared, layer, dict(stripped)


_SPARAM = re.compile(r'\b[SR]\s*([+-]?\d+(?:\.\d*)?)')


class Limits:
    """Checagens do perfil na pré-validação: temperaturas máximas e volume de impressão."""

    def __init__(self, prof):
        self.p = prof; self.absolute = True; self.bounds = True
        m = float(prof['bounds_margin_mm'])
        self.box = {a: (-m, float(prof['bed_mm'][a.lower()]) + m) for a in 'XYZ'}

    def check(self, raw):
        code, params, _ = bridge.parse(raw)
        if code in ('M104', 'M109', 'M140', 'M190'):
            lim = self.p['max_bed_c'] if code in ('M140', 'M190') else self.p['max_hotend_c']
            for v in _SPARAM.findall(raw[len(code):]):
                if float(v) > lim:
                    raise bridge.BridgeError(f'{code} {v}°C acima do limite do perfil ({lim}°C)')
        elif code == 'G90':
            self.absolute = True
        elif code == 'G91':
            self.absolute = False
        elif code == 'G92' and any(a in params for a in 'XYZ'):
            self.bounds = False  # coordenadas deslocadas: não dá para checar
        elif code in ('G0', 'G1') and self.absolute and self.bounds:
            for a, (lo, hi) in self.box.items():
                if a in params and not lo <= params[a] <= hi:
                    raise bridge.BridgeError(f'{a}{params[a]:g} fora da área da {self.p["name"]} ({lo:g} a {hi:g} mm)')


_TEMP = re.compile(r'\bT0?:\s*([\d.]+)\s*/\s*([\d.]+)')
_BED = re.compile(r'\bB:\s*([\d.]+)\s*/\s*([\d.]+)')


class Runner:
    """Executa um trabalho por vez numa thread e expõe estado + log para as interfaces."""

    def __init__(self, log_size=800):
        self._lock = threading.Lock()
        self._logs = collections.deque(maxlen=log_size)
        self._seq = 0
        self._thread = None
        self.pause_ev = threading.Event()
        self.stop_ev = threading.Event()
        self._adjust = queue.Queue()
        self._query_lock = threading.Lock()
        self._profile = PROFILE_DEFAULTS
        self.status = self._blank()

    @staticmethod
    def _blank():
        return dict(state='idle', message='Pronto', live=False, file=None, line=0, total_lines=0,
                    done_cmds=0, total_cmds=0, progress=0.0, layer=0, total_layers=0, tool=None,
                    command='', hotend=None, hotend_target=None, bed=None, bed_target=None,
                    started=None, finished=None, profile=None, stripped={},
                    speed_factor=100, flow_factor=100, fan=None)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def log(self, *args, **_):
        text = ' '.join(map(str, args))
        with self._lock:
            self._seq += 1
            self._logs.append((self._seq, time.time(), text))

    def _set(self, **kw):
        with self._lock:
            self.status.update(kw)

    def snapshot(self, since=0):
        with self._lock:
            st = dict(self.status)
            st['logs'] = [{'seq': s, 't': t, 'text': x} for s, t, x in self._logs if s > since]
            st['seq'] = self._seq
        st['running'] = self.running
        end = st['finished'] or time.time()
        st['elapsed'] = end - st['started'] if st['started'] else 0
        st['eta'] = (st['elapsed'] * (100 - st['progress']) / st['progress']
                     if st['state'] in ('running', 'paused') and st['progress'] > 0.5 else None)
        return st

    def toggle_pause(self):
        if not self.running:
            return
        if self.pause_ev.is_set():
            self.pause_ev.clear()
        else:
            self.pause_ev.set()

    def stop(self):
        if self.running:
            self.stop_ev.set()

    def adjust(self, kind, value):
        """Enfileira um ajuste ao vivo; é enviado à impressora entre comandos."""
        if not self.running:
            raise RuntimeError('Nenhum trabalho em execução')
        if kind not in ADJUST:
            raise ValueError('Ajuste desconhecido: ' + str(kind))
        lo, hi, _ = ADJUST[kind]
        if hi is None:
            hi = self._profile['max_bed_c' if kind == 'bed' else 'max_hotend_c']
        try:
            v = int(round(float(value)))
        except (TypeError, ValueError):
            raise ValueError('Valor inválido')
        if not lo <= v <= hi:
            raise ValueError(f'{kind}: use {lo}–{hi}')
        self._adjust.put((kind, v))

    def _apply_adjustments(self, b):
        while True:
            try:
                kind, v = self._adjust.get_nowait()
            except queue.Empty:
                return
            cmd = f'M106 S{round(v * 255 / 100)}' if kind == 'fan' else ADJUST[kind][2].format(v)
            self.log(f'Ajuste ao vivo: {kind} = {v} -> {cmd}')
            b.printer(cmd)
            key = {'speed': 'speed_factor', 'flow': 'flow_factor', 'fan': 'fan'}.get(kind)
            if key:
                self._set(**{key: v})

    def query_printer(self, settings):
        """Lê firmware (M115) e configuração gravada (M503). Só com a máquina ociosa."""
        if self.running:
            raise RuntimeError('Indisponível durante um trabalho')
        if not self._query_lock.acquire(blocking=False):
            raise RuntimeError('Leitura já em andamento')
        dev = None
        out = []
        try:
            s = normalize(settings)
            bridge.print = lambda *a, **k: None
            dev = bridge.Device(s['printer_port'], s['printer_baud'], 'VYPER', True)
            # Abrir a porta reinicia a placa; espera o boot respondendo ao M115.
            for attempt in range(6):
                try:
                    out += dev.command('M115', timeout=5)
                    break
                except bridge.BridgeError:
                    if attempt == 5:
                        raise
            out += dev.command('M503', timeout=20)
            try:
                out += dev.command('M105', timeout=5)
            except bridge.BridgeError:
                pass
        finally:
            bridge.print = builtins.print
            if dev:
                dev.close()
            self._query_lock.release()
        return out

    def start(self, path, settings, live, confirm):
        if self.running:
            raise RuntimeError('Já existe um trabalho em execução')
        if not str(path).strip():
            raise ValueError('Selecione um arquivo G-code')
        path = Path(path)
        s = normalize(settings)
        check_start(s, live, confirm)
        c = build_config(s, confirm)
        prof = load_profile(s['profile'])
        lines = path.read_text(encoding='utf-8-sig', errors='replace').splitlines()
        prepared, layers, stripped = prepare(lines, prof['strip_codes'])
        if self._query_lock.locked():
            raise RuntimeError('Aguarde a leitura da impressora terminar')
        self._profile = prof
        self.stop_ev.clear()
        self.pause_ev.clear()
        self._adjust = queue.Queue()
        with self._lock:
            self._logs.clear()
            self.status = self._blank()
            self.status.update(state='preflight', message='Pré-validação sem hardware...', live=bool(live),
                               file=path.name, total_lines=len(lines), total_cmds=len(prepared),
                               total_layers=layers, tool=c['initial_tool'], started=time.time(),
                               profile=prof['name'], stripped=stripped,
                               speed_factor=prof['speed_factor'], flow_factor=prof['flow_factor'])
        self._thread = threading.Thread(target=self._run, args=(prepared, c, bool(live), prof), daemon=True)
        self._thread.start()

    # --- execução ---------------------------------------------------------
    def _feedback(self, line):
        m, b = _TEMP.search(line), _BED.search(line)
        upd = {}
        if m:
            upd.update(hotend=float(m.group(1)), hotend_target=float(m.group(2)))
        if b:
            upd.update(bed=float(b.group(1)), bed_target=float(b.group(2)))
        if upd:
            self._set(**upd)

    def _read_temps(self, b):
        self._feedback(' '.join(b.printer('M105')))

    def _run(self, prepared, c, live, prof):
        p = m = None
        try:
            self.log(f'Perfil: {prof["name"]}')
            removed = self.status.get('stripped') or {}
            if removed:
                self.log('Removidos (usa o padrão do firmware): '
                         + ', '.join(f'{k} x{v}' for k, v in sorted(removed.items())))
            self.log('Pré-validação sem hardware...')
            bridge.print = lambda *a, **k: None
            pre = bridge.Bridge(c, bridge.Device('', 0, 'V', False), bridge.Device('', 0, 'A', False), live)
            limits = Limits(prof)
            for idx, raw, _ in prepared:
                if self.stop_ev.is_set():
                    raise InterruptedError('Cancelado antes de conectar')
                try:
                    code, _, _ = bridge.parse(raw)
                    if live and code not in LIVE_CODES:
                        raise bridge.BridgeError('Comando fora da lista de teste: ' + code)
                    limits.check(raw)
                    pre.handle(raw)
                except Exception as e:
                    raise ValueError(f'Linha {idx}: {e}') from e
            bridge.print = self.log
            self.log('Pré-validação OK')

            self._set(state='connecting', message='Conectando às portas...' if live else 'Simulação (sem portas)')
            p = bridge.Device(c['printer_port'], c['printer_baud'], 'VYPER', live)
            m = bridge.Device(c['mmu_port'], c['mmu_baud'], 'ARDUINO', live)
            b = bridge.Bridge(c, p, m, live)
            b.setup()
            self._read_temps(b)
            if prof['speed_factor'] != 100:
                b.printer(f'M220 S{prof["speed_factor"]}')
            if prof['flow_factor'] != 100:
                b.printer(f'M221 S{prof["flow_factor"]}')

            msg = 'Imprimindo' if live else 'Simulando'
            self._set(state='running', message=msg)
            total, last_temp = len(prepared), time.monotonic()
            for i, (idx, raw, layer) in enumerate(prepared, 1):
                if self.pause_ev.is_set() and not self.stop_ev.is_set():
                    self._set(state='paused', message='Pausado entre comandos')
                    self.log('Pausado')
                    while self.pause_ev.is_set() and not self.stop_ev.is_set():
                        self._apply_adjustments(b)
                        time.sleep(.1)
                    if not self.stop_ev.is_set():
                        self.log('Retomado')
                        self._set(state='running', message=msg)
                if self.stop_ev.is_set():
                    raise InterruptedError('Parado após comando; inspecione a máquina')
                self._apply_adjustments(b)
                self._set(line=idx, command=raw, layer=layer)
                b.handle(raw, self._feedback)
                if raw.startswith('M106'):
                    fs = _SPARAM.search(raw[4:])
                    self._set(fan=round(float(fs.group(1)) * 100 / 255) if fs else 100)
                elif raw.startswith('M107'):
                    self._set(fan=0)
                if time.monotonic() - last_temp > 5:
                    self._read_temps(b)
                    last_temp = time.monotonic()
                self._set(done_cmds=i, progress=100 * i / total, tool=b.tool)
            b.printer('M400')
            if self.status['speed_factor'] != 100:
                b.printer('M220 S100')  # devolve 100% para o próximo trabalho
            if self.status['flow_factor'] != 100:
                b.printer('M221 S100')
            self._read_temps(b)
            self._finish('done', 'Concluído' if live else 'Simulação concluída')
        except InterruptedError as e:
            self._abort(p, live, e, 'stopped')
        except Exception as e:
            self._abort(p, live, e, 'error')
        finally:
            bridge.print = builtins.print
            for d in (p, m):
                if d:
                    try:
                        d.close()
                    except Exception:
                        pass

    def _abort(self, p, live, err, state):
        self.log('INTERROMPIDO:', err)
        if live and p is not None and p.ser is not None:
            # Melhor esforço: não substitui desligar fisicamente em emergência.
            for cmd in ('M104 S0', 'M140 S0'):
                try:
                    p.command(cmd, timeout=10)
                except Exception as e:
                    self.log('Falha ao enviar', cmd, '-', e)
            self.log('Aquecedores desligados (melhor esforço). Confira a máquina.')
        self._finish(state, str(err))

    def _finish(self, state, message):
        self._set(state=state, message=message, finished=time.time())

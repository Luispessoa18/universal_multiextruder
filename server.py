"""Interface web para acompanhar e controlar o MMU (Orange Pi / Windows).

Uso:
  python server.py                     # http://127.0.0.1:8080
  python server.py --host 0.0.0.0      # acesso pela rede local
Variáveis de ambiente:
  MMU_PASSWORD  senha (HTTP Basic). Obrigatória quando acessado via cloudflared.
  MMU_USER      usuário (padrão: admin)
  MMU_HOST / MMU_PORT / MMU_MAX_UPLOAD_MB
"""
import argparse, hmac, os, re, sys, threading, zipfile
from pathlib import Path
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename
import converter, runner

ROOT = Path(__file__).resolve().parent
UPLOADS = ROOT / 'uploads'
WEB = ROOT / 'web'
GCODE_EXTS = {'.gcode', '.gco', '.gc', '.g'}
EXTS = GCODE_EXTS | {'.3mf'}

app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MMU_MAX_UPLOAD_MB', '200')) * 1024 * 1024
job = runner.Runner()
USER = os.environ.get('MMU_USER', 'admin')
PASSWORD = os.environ.get('MMU_PASSWORD', '')


def _eq(a, b):
    return hmac.compare_digest((a or '').encode(), b.encode())


@app.before_request
def guard():
    if PASSWORD:
        auth = request.authorization
        if not (auth and _eq(auth.username, USER) and _eq(auth.password, PASSWORD)):
            return Response('Autenticação necessária', 401, {'WWW-Authenticate': 'Basic realm="MMU", charset="UTF-8"'})
    elif request.headers.get('Cf-Connecting-IP'):
        # Requisição chegou pelo túnel do Cloudflare: nunca expor sem senha.
        return Response('Defina MMU_PASSWORD no servidor antes de expor pelo cloudflared.', 403)
    if request.method not in ('GET', 'HEAD') and request.headers.get('X-Requested-With') != 'mmu':
        return jsonify(error='Requisição sem cabeçalho X-Requested-With'), 400  # proteção CSRF


@app.after_request
def no_cache(r):
    if request.path.startswith('/api/'):
        r.headers['Cache-Control'] = 'no-store'
    return r


@app.errorhandler(413)
def too_big(_):
    return jsonify(error='Arquivo maior que o limite (MMU_MAX_UPLOAD_MB)'), 413


@app.get('/')
def index():
    return send_from_directory(WEB, 'index.html')


@app.get('/api/status')
def status():
    try:
        since = int(request.args.get('since', 0))
    except ValueError:
        since = 0
    return jsonify(job.snapshot(since))


@app.get('/api/settings')
def get_settings():
    return jsonify(settings=runner.load_settings(), defaults=runner.DEFAULTS)


@app.post('/api/settings')
def set_settings():
    try:
        s = runner.normalize(request.get_json(force=True) or {})
    except ValueError as e:
        return jsonify(error=str(e)), 400
    runner.save_settings(s)
    return jsonify(settings=s)


@app.get('/api/ports')
def ports():
    from serial.tools import list_ports
    out = []
    for d in ('/dev/serial/by-id', '/dev/serial/by-path'):  # nomes estáveis no Linux
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                full = os.path.join(d, name)
                out.append({'device': full, 'description': 'estável → ' + os.path.realpath(full)})
    for p in list_ports.comports():
        out.append({'device': p.device, 'description': f'{p.description} [{p.hwid}]'})
    return jsonify(ports=out)


def _files():
    UPLOADS.mkdir(exist_ok=True)
    items = [f for f in UPLOADS.iterdir() if f.is_file() and f.suffix.lower() in EXTS]
    items.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [{'name': f.name, 'size': f.stat().st_size, 'mtime': f.stat().st_mtime} for f in items]


@app.get('/api/files')
def files():
    return jsonify(files=_files())


@app.post('/api/files')
def upload():
    f = request.files.get('file')
    name = secure_filename(f.filename) if f and f.filename else ''
    if not name or Path(name).suffix.lower() not in EXTS:
        return jsonify(error='Envie um arquivo .gcode, .gcode.3mf ou .3mf'), 400
    UPLOADS.mkdir(exist_ok=True)
    if job.running and job.status.get('file') == name:
        return jsonify(error='Arquivo em uso pelo trabalho atual'), 409
    f.save(UPLOADS / name)
    return jsonify(name=name, files=_files())


@app.delete('/api/files/<name>')
def delete(name):
    name = secure_filename(name)
    path = UPLOADS / name
    if job.running and job.status.get('file') == name:
        return jsonify(error='Arquivo em uso pelo trabalho atual'), 409
    if path.is_file():
        path.unlink()
    return jsonify(files=_files())


@app.post('/api/start')
def start():
    data = request.get_json(force=True) or {}
    name = secure_filename(str(data.get('file', '')))
    path = UPLOADS / name
    if not name or not path.is_file():
        return jsonify(error='Arquivo não encontrado'), 400
    if path.suffix.lower() not in GCODE_EXTS:
        return jsonify(error='Converta o 3MF para Vyper antes de imprimir'), 400
    try:
        job.start(path, runner.load_settings(), bool(data.get('live')), bool(data.get('confirm')))
    except (ValueError, RuntimeError, OSError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.post('/api/pause')
def pause():
    job.toggle_pause()
    return jsonify(ok=True)


@app.post('/api/stop')
def stop():
    job.stop()
    return jsonify(ok=True)


_analysis = {}
_conv = {'state': 'idle'}
_conv_lock = threading.Lock()


def _upload_path(name):
    name = secure_filename(str(name or ''))
    path = UPLOADS / name
    if not name or not path.is_file():
        raise FileNotFoundError('Arquivo não encontrado')
    return path


def _analyze(path):
    st = path.stat()
    key = (path.name, st.st_mtime, st.st_size)
    if key not in _analysis:
        _analysis.clear()  # mantém só o último (arquivos grandes)
        _analysis[key] = converter.analyze(path)
    return _analysis[key]


@app.get('/api/analyze')
def analyze():
    try:
        return jsonify(_analyze(_upload_path(request.args.get('file'))))
    except FileNotFoundError as e:
        return jsonify(error=str(e)), 404
    except (converter.ConvertError, OSError) as e:
        return jsonify(error=str(e)), 400


@app.get('/api/thumb')
def thumb():
    try:
        data = converter.thumbnail(_upload_path(request.args.get('file')), int(request.args.get('plate', 1)))
    except (FileNotFoundError, KeyError, ValueError, OSError, zipfile.BadZipFile):
        return Response(status=404)
    return Response(data, mimetype='image/png', headers={'Cache-Control': 'max-age=3600'})


def _convert_worker(path, member, mapping, center, out, nplates):
    try:
        prof = runner.load_profile(runner.load_settings()['profile'])
        res = converter.convert(path, member, mapping, prof, center, out, source_name=path.name)
        _conv.update(state='done', message='Convertido: ' + res['output'], result=res)
    except Exception as e:
        _conv.update(state='error', message=str(e), result=None)


@app.get('/api/convert')
def convert_status():
    return jsonify(_conv)


@app.post('/api/convert')
def convert():
    data = request.get_json(force=True) or {}
    try:
        path = _upload_path(data.get('file'))
        info = _analyze(path)
        plate = next((p for p in info['plates'] if p['plate'] == int(data.get('plate', 1))), None)
        if plate is None:
            raise ValueError('Placa inexistente')
        mapping = {int(k): str(v) for k, v in (data.get('mapping') or {}).items()}
    except FileNotFoundError as e:
        return jsonify(error=str(e)), 404
    except (converter.ConvertError, ValueError, TypeError, OSError) as e:
        return jsonify(error=str(e)), 400
    out = UPLOADS / converter.output_name(path.name, plate['plate'], len(info['plates']))
    if job.running and job.status.get('file') == out.name:
        return jsonify(error='O arquivo convertido está em uso pelo trabalho atual'), 409
    with _conv_lock:
        if _conv.get('state') == 'running':
            return jsonify(error='Já existe uma conversão em andamento'), 409
        _conv.clear()
        _conv.update(state='running', message='Convertendo ' + path.name + '...', result=None)
    threading.Thread(target=_convert_worker, args=(path, plate['member'], mapping, bool(data.get('center', True)),
                                                   out, len(info['plates'])), daemon=True).start()
    return jsonify(ok=True)


@app.post('/api/adjust')
def adjust():
    data = request.get_json(force=True) or {}
    try:
        job.adjust(data.get('kind'), data.get('value'))
    except (ValueError, RuntimeError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.get('/api/profile')
def profile():
    try:
        return jsonify(profile=runner.load_profile(runner.load_settings()['profile']), profiles=runner.list_profiles())
    except ValueError as e:
        return jsonify(error=str(e)), 400


_M503 = re.compile(r'^(?:echo:)?\s*(M\d+)\s+(.*)$')


@app.post('/api/printer/info')
def printer_info():
    """Conecta na impressora ociosa e lê M115 (firmware) + M503 (configuração gravada)."""
    try:
        lines = job.query_printer(runner.load_settings())
    except Exception as e:
        return jsonify(error=f'Falha ao ler a impressora: {e}'), 400
    settings = {}
    for l in lines:
        m = _M503.match(l.strip())
        if m:
            settings.setdefault(m.group(1), []).append(m.group(2).strip())
    firmware = next((l for l in lines if 'FIRMWARE_NAME' in l), '')
    return jsonify(lines=lines, settings=settings, firmware=firmware)


def main():
    ap = argparse.ArgumentParser(description='Interface web do Universal Multiextruder')
    ap.add_argument('--host', default=os.environ.get('MMU_HOST', '127.0.0.1'))
    ap.add_argument('--port', type=int, default=int(os.environ.get('MMU_PORT', '8080')))
    a = ap.parse_args()
    if not PASSWORD:
        print('AVISO: MMU_PASSWORD não definida. Acesso via cloudflared será bloqueado;'
              + (' qualquer um na rede local pode controlar a impressora.' if a.host not in ('127.0.0.1', 'localhost') else ''),
              flush=True)
    print(f'Interface web em http://{a.host}:{a.port}', flush=True)
    try:
        from waitress import serve
        serve(app, host=a.host, port=a.port, threads=8)
    except ImportError:
        app.run(host=a.host, port=a.port, threaded=True)


if __name__ == '__main__':
    sys.exit(main())

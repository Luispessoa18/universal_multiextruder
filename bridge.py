import argparse, json, math, re, time, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
class BridgeError(Exception): pass

class Device:
    def __init__(self, port, baud, name, live):
        self.name, self.live = name, live
        self.ser = None
        if live:
            import serial
            self.ser = serial.Serial(port, baud, timeout=.25, write_timeout=2)
            time.sleep(2.5)
            self.ser.reset_input_buffer()
    def command(self, command, timeout=180, feedback=None):
        print(f'[{self.name}] {command}', flush=True)
        if not self.live:
            if command == 'M105': return ['ok T:205.0 /205.0 B:60.0 /60.0']
            if command == 'M115': return ['FIRMWARE_NAME:MMU_BRIDGE_SIM', 'ok']
            return ['ok']
        self.ser.write((command+'\n').encode('ascii'))
        end=time.monotonic()+timeout; replies=[]
        while time.monotonic()<end:
            s=self.ser.readline().decode('ascii',errors='replace').strip()
            if not s: continue
            print(f'[{self.name} <<] {s}', flush=True)
            if s.lower().startswith(('error','!!','resend','rs ')):
                raise BridgeError(f'{self.name}: {s}')
            replies.append(s)
            if s.lower()=='ok' or s.lower().startswith('ok '): return replies
            if feedback: feedback(s)
        raise BridgeError(f'Timeout {self.name}: {command}; nao reenviar automaticamente')
    def close(self):
        if self.ser: self.ser.close()

PARAM = re.compile(r'([A-Z])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))')
def parse(line):
    line=line.split(';',1)[0].strip().upper()
    if not line: return '', {}, ''
    m=re.match(r'^([GMT]\d+)(.*)$',line)
    if not m: raise BridgeError('Comando nao suportado: '+line)
    code, rest=m.groups()
    params={}
    if code in ('G0','G1','G92'):
        pos=0
        for p in PARAM.finditer(rest):
            if rest[pos:p.start()].strip(): raise BridgeError('Parametro invalido')
            k,v=p.groups()
            if k in params: raise BridgeError('Parametro duplicado')
            params[k]=float(v); pos=p.end()
        if rest[pos:].strip(): raise BridgeError('Parametro invalido')
    return code,params,line

class Bridge:
    def __init__(self,c,printer,mmu,live=False):
        self.c,self.p,self.m,self.live=c,printer,mmu,live
        self.tool=c['initial_tool']; self.relative=False; self.xyzrel=False
        self.e=0.; self.native_e=0.; self.fault=False
        self.tools=c['tool_map']; self.park=c['park_mm']; self.f=1200.
        self.positions={str(k):float(v) for k,v in c['initial_positions_mm'].items()}
        self.setup_done=False
    def printer(self,cmd,feedback=None): return self.p.command(cmd,feedback=feedback)
    def mmu(self,cmd): return self.m.command(cmd)
    def setup(self):
        self.mmu('PING'); self.mmu('T1'); self.mmu('CAL='+str(self.c['steps_mm'])); self.mmu('RPM='+str(self.c['rpm']))
        self.setup_done=True
    def heat(self):
        replies=self.printer('M105')
        s=' '.join(replies); m=re.search(r'\bT(?:0)?:\s*([\d.]+)',s)
        if not m or float(m.group(1))<self.c['minimum_hotend_c']: raise BridgeError('Hotend frio ou leitura indisponivel')
    def external(self,delta):
        if abs(delta)<1e-8:return
        # Firmware atual tem limite de 300 mm por comando.
        while abs(delta)>1e-8:
            d=min(abs(delta), 100.)
            self.mmu(('F' if delta>0 else 'R')+f'{d:.5f}')
            delta-=math.copysign(d,delta)
    def native(self,delta):
        if abs(delta)<1e-8:return
        self.heat(); self.printer('M83')
        left=delta
        while abs(left)>1e-8:
            d=math.copysign(min(abs(left),40.),left)
            self.printer(f'G1 E{d:.5f} F{self.c["native_change_feed"]}')
            self.printer('M400'); left-=d
        self.printer('M83' if self.relative else 'M82')
        self.printer(f'G92 E{self.native_e:.5f}')
        self.printer(f'G1 F{self.f:.4f}')
    def change(self,new):
        if new not in self.tools: raise BridgeError('Ferramenta inexistente: '+new)
        if new==self.tool:return ['ok']
        self.printer('M400')
        if not self.c['enable_physical_tool_changes'] and self.live:
            raise BridgeError('Trocas fisicas desativadas: calibre e habilite no config')
        if self.live and (not self.c['confirm_geometry'] or any(self.c['load_target_mm'][t] <= self.park[t] for t in self.tools)):
            raise BridgeError('Confirme geometria e posicoes iniciais')
        self.heat()
        if self.tool is not None:
            d=self.positions[self.tool]-self.park[self.tool]
            if d<0: raise BridgeError('Posicao anterior invalida')
            self.move_tool(self.tool,-d); self.positions[self.tool]=self.park[self.tool]
        d=self.c['load_target_mm'][new]-self.positions[new]
        if d<0:raise BridgeError('Posicao inicial invalida')
        self.move_tool(new,d); self.positions[new]=self.c['load_target_mm'][new]
        self.tool=new
        return ['ok']
    def move_tool(self,t,d):
        if self.tools[t]=='arduino': self.external(d)
        else:self.native(d)
    def handle(self,line,feedback=None):
        code,p,raw=parse(line)
        if not code:return ['ok']
        if code=='M112':
            if self.p.ser:self.p.ser.write(b'M112\n')
            self.fault=True; raise BridgeError('Parada solicitada; Arduino nao tem parada imediata por serial')
        if self.fault:raise BridgeError('Ponte bloqueada; inspecione e reinicie')
        if code.startswith('T'):
            if raw != code: raise BridgeError('Parametros de ferramenta nao suportados')
            return self.change(code)
        if code in ('G2','G3','G10','G11','M600','M701','M702','M605','M200','M221','M220','G20','G53','G54','G55','G56','G57','G58','G59','M410','M999'):
            raise BridgeError('Comando nao suportado nesta versao: '+code)
        if code in ('G90','G91'):
            self.xyzrel=code=='G91'; self.relative=self.xyzrel
        if code in ('M82','M83'):self.relative=code=='M83'
        if code=='G92' and 'E' in p:
            self.e=p['E']; self.native_e=p['E']
        if code in ('G0','G1'):
            if any(k not in 'XYZEF' for k in p):raise BridgeError('Eixo/parametro nao suportado')
            if 'F' in p:
                if p['F']<=0:raise BridgeError('Feedrate invalido')
                self.f=p['F']
            if 'E' in p:
                if self.tool is None:raise BridgeError('Selecione ferramenta antes de extrudar')
                delta=p['E'] if self.relative else p['E']-self.e
                next_e=self.e+delta
                if self.tools[self.tool]=='arduino':
                    mixed=any(k in p for k in 'XYZ') and abs(delta)>1e-8
                    if mixed and self.live:
                        raise BridgeError('T1 XYZ+E bloqueado: falta sincronizacao STEP/DIR entre placas')
                    if abs(delta)>1e-8:
                        if self.live and not self.c['confirm_geometry']: raise BridgeError('Confirme geometria antes de movimentar T1')
                        self.heat(); self.printer('M400')
                    xyz={k:v for k,v in p.items() if k!='E'}
                    if mixed: print('[SIM] XYZ e E separados apenas para demonstrar roteamento; nao sincronizados')
                    if xyz:self.printer(code+' '+' '.join(f'{k}{v:.5f}' for k,v in xyz.items()))
                    self.external(delta)
                    self.e=next_e
                    return ['ok']
                # Native uses independent E coordinate to prevent jumps after T1.
                self.native_e+=delta
                p['E']=delta if self.relative else self.native_e
                self.e=next_e
                raw=code+' '+' '.join(f'{k}{v:.5f}' for k,v in p.items())
        return self.printer(raw,feedback)

class Protocol:
    def __init__(self,bridge):self.b=bridge; self.expected=None
    def run(self,line,feedback=None):
        # Host checksums are validated and removed, physical commands use unnumbered Marlin lines.
        line=line.strip(); n=None
        if re.match(r'^N\d+\s',line,re.I):
            if '*' not in line:return ['Resend:'+str(self.expected or 1)]
            body,checksum=line.rsplit('*',1); x=0
            for ch in body.encode('ascii'):x^=ch
            try: valid=x==int(checksum)
            except ValueError:valid=False
            m=re.match(r'^N(\d+)\s+(.*)$',body,re.I); n=int(m[1]); line=m[2]
            if not valid:return ['Resend:'+str(self.expected if self.expected is not None else n)]
            if not line.upper().startswith('M110') and self.expected is not None and n!=self.expected:
                return ['Resend:'+str(self.expected)]
        if line.upper().startswith('M110'):
            m=re.search(r'\bN(\d+)',line,re.I); self.expected=(int(m[1]) if m else n or 0)+1
            return ['ok']
        result=self.b.handle(line,feedback)
        if n is not None:self.expected=n+1
        return result

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',default=str(ROOT/'config.json')); ap.add_argument('--file'); ap.add_argument('--live',action='store_true'); ap.add_argument('--console',action='store_true'); a=ap.parse_args()
    c=json.loads(Path(a.config).read_text()); p=m=v=None
    try:
        if a.live and c['printer_port']==c['mmu_port']:raise BridgeError('Portas fisicas iguais')
        p=Device(c['printer_port'],c['printer_baud'],'VYPER',a.live); m=Device(c['mmu_port'],9600,'ARDUINO',a.live)
        b=Bridge(c,p,m,a.live); b.setup(); proto=Protocol(b)
        if a.file or a.console:
            stream=open(a.file,encoding='utf-8') if a.file else sys.stdin
            for line in stream:
                if not line.strip() or line.lstrip().startswith(';'):continue
                print('HOST >>',line.strip()); print('HOST <<',proto.run(line))
        else:
            import serial
            v=serial.Serial(c['virtual_port'],c['virtual_baud'],timeout=.25,write_timeout=2)
            print('Aguardando host em',c['virtual_port'],'LIVE' if a.live else 'SIMULACAO',flush=True)
            def send(s):v.write((s+'\n').encode('ascii',errors='replace'))
            buffer=bytearray()
            while True:
                ch=v.read(1)
                if not ch:continue
                if ch in (b'\n',b'\r'):
                    if not buffer:continue
                    line=buffer.decode('ascii',errors='replace');buffer.clear()
                    try:
                        replies=proto.run(line,send)
                        # Intermediate replies may already have been sent by feedback.
                        for s in replies:
                            if s.lower().startswith('ok') or s.startswith('Resend:') or not a.live:send(s)
                    except Exception as e:
                        b.fault=True;send('Error:'+str(e));print('FALHA:',e,flush=True)
                else:
                    buffer.extend(ch)
                    if len(buffer)>2048:raise BridgeError('Linha longa demais')
    except (BridgeError,KeyboardInterrupt) as e:
        print('INTERROMPIDO:',e)
        return 1
    finally:
        for d in (p,m,v):
            if d:d.close()
    return 0
if __name__=='__main__':sys.exit(main())

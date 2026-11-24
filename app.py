import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading, queue, time, math
from pathlib import Path
import bridge

class App:
    def __init__(self, root):
        self.root=root; root.title('MMU G-code MVP'); root.geometry('850x710')
        self.q=queue.Queue(); self.stop=threading.Event(); self.pause=threading.Event(); self.running=False
        self.vars={}; frame=ttk.Frame(root,padding=12); frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='MMU G-code Sender',font=('Segoe UI',19,'bold')).pack(anchor='w')
        ttk.Label(frame,text='T1: Arduino | T2: Vyper — sem porta virtual').pack(anchor='w')
        f=ttk.Frame(frame); f.pack(fill='x',pady=8)
        self.path=tk.StringVar(); ttk.Entry(f,textvariable=self.path).pack(side='left',fill='x',expand=True)
        ttk.Button(f,text='Abrir G-code',command=lambda:self.path.set(filedialog.askopenfilename(filetypes=[('G-code','*.gcode *.gco *.gc'),('Todos','*.*')]))).pack(side='left')
        settings=ttk.Frame(frame); settings.pack(fill='x')
        fields=[('printer_port','COM Vyper','COM3'),('mmu_port','COM Arduino','COM4'),('printer_baud','Baud Vyper','115200'),('steps_mm','T1 passos/mm','5.4820'),('rpm','T1 RPM','20'),('p1','T1 estacionamento (mm)','256.5'),('l1','T1 alvo (mm)','444.5'),('p2','T2 estacionamento (mm)','0'),('l2','T2 alvo (mm, medir)','0'),('native_change_feed','T2 troca (mm/min)','300')]
        for i,(k,label,value) in enumerate(fields):
            r=i//2; c=(i%2)*2; self.vars[k]=tk.StringVar(value=value)
            ttk.Label(settings,text=label).grid(row=r,column=c,sticky='w',padx=3,pady=2); ttk.Entry(settings,textvariable=self.vars[k],width=16).grid(row=r,column=c+1,sticky='ew',padx=3)
        settings.columnconfigure(1,weight=1); settings.columnconfigure(3,weight=1)
        f=ttk.Frame(frame); f.pack(fill='x',pady=5); ttk.Label(f,text='Estado inicial:').pack(side='left')
        self.initial=tk.StringVar(value='Nenhum (ambos estacionados)'); ttk.Combobox(f,textvariable=self.initial,values=['Nenhum (ambos estacionados)','T1 carregado','T2 carregado'],state='readonly',width=30).pack(side='left')
        self.zero=tk.BooleanVar(value=False); ttk.Checkbutton(f,text='Mapear T0/T1 do slicer para T1/T2',variable=self.zero).pack(side='left',padx=8)
        self.confirm=tk.BooleanVar(value=False); ttk.Checkbutton(frame,text='Geometria, posição inicial, sentido e temperatura conferidos; troca em local seguro',variable=self.confirm).pack(anchor='w')
        ttk.Label(frame,text='Modo real bloqueia T1 com XYZ+E e comandos de extrusão não suportados.',foreground='#a33').pack(anchor='w')
        f=ttk.Frame(frame); f.pack(fill='x',pady=6)
        self.sim=ttk.Button(f,text='Simular arquivo',command=lambda:self.start(False)); self.sim.pack(side='left')
        self.live=ttk.Button(f,text='Executar real',command=lambda:self.start(True)); self.live.pack(side='left',padx=5)
        self.pb=ttk.Button(f,text='Pausar / retomar',command=self.toggle,state='disabled'); self.pb.pack(side='left')
        self.sb=ttk.Button(f,text='Parar após comando',command=self.stop.set,state='disabled'); self.sb.pack(side='left',padx=5)
        self.progress=ttk.Progressbar(frame,maximum=100); self.progress.pack(fill='x',pady=4)
        self.status=ttk.Label(frame,text='Pronto'); self.status.pack(anchor='w')
        self.log=tk.Text(frame,height=15,wrap='word',state='disabled'); self.log.pack(fill='both',expand=True)
        root.protocol('WM_DELETE_WINDOW',self.close); root.after(80,self.poll)
    def close(self):
        if self.running: messagebox.showwarning('Em execução','Pare e aguarde o comando atual antes de fechar. Para emergência, desligue os equipamentos fisicamente.'); return
        self.root.destroy()
    def toggle(self):
        if self.pause.is_set():self.pause.clear()
        else:self.pause.set()
    def poll(self):
        try:
            while True:
                kind,val=self.q.get_nowait()
                if kind=='log':
                    self.log.configure(state='normal'); self.log.insert('end',val+'\n'); self.log.see('end'); self.log.configure(state='disabled')
                elif kind=='progress':self.progress['value']=val; self.status.configure(text=f'{val:.1f}%')
                elif kind=='done':
                    self.running=False; self.status.configure(text=val); self.sim.configure(state='normal'); self.live.configure(state='normal'); self.pb.configure(state='disabled'); self.sb.configure(state='disabled')
        except queue.Empty:pass
        self.root.after(80,self.poll)
    def start(self,live):
        try:
            path=Path(self.path.get()); lines=path.read_text(encoding='utf-8-sig').splitlines()
            c={k:v.get() for k,v in self.vars.items()};
            for k in ('printer_baud','rpm','native_change_feed'):c[k]=int(c[k])
            for k in ('steps_mm','p1','l1','p2','l2'):c[k]=float(c[k]); assert math.isfinite(c[k])
            if not (0<c['steps_mm']<=500 and 1<=c['rpm']<=60 and c['native_change_feed']>0 and 0<=c['p1']<c['l1'] and 0<=c['p2']<=c['l2']):raise ValueError('Calibração/distâncias inválidas')
            initial=self.initial.get()[:2] if self.initial.get().startswith('T') else None
            c.update(tool_map={'T1':'arduino','T2':'vyper'},initial_tool=initial,park_mm={'T1':c['p1'],'T2':c['p2']},load_target_mm={'T1':c['l1'],'T2':c['l2']},minimum_hotend_c=180,enable_physical_tool_changes=self.confirm.get(),confirm_geometry=self.confirm.get())
            c['initial_positions_mm']={t:c['load_target_mm'][t] if t==initial else c['park_mm'][t] for t in c['tool_map']}
            if live and (not self.confirm.get() or c['printer_port'].upper()==c['mmu_port'].upper()):raise ValueError('Confirme a geometria e use portas distintas')
            if live and not messagebox.askyesno('Teste físico','Não é uma impressão multicolorida completa. Confirme que o hotend, os filamentos e a posição da máquina estão seguros. Parar não é parada de emergência. Continuar?'):return
        except Exception as e:messagebox.showerror('Configuração',str(e));return
        self.running=True; self.stop.clear(); self.pause.clear(); self.progress['value']=0
        for b in (self.sim,self.live):b.configure(state='disabled')
        for b in (self.pb,self.sb):b.configure(state='normal')
        threading.Thread(target=self.run,args=(lines,c,live,self.zero.get()),daemon=True).start()
    def run(self,lines,c,live,zero):
        def log(*args,**kw):self.q.put(('log',' '.join(map(str,args))))
        bridge.print=log; p=m=None
        try:
            import re
            prepared=[]
            for idx,line in enumerate(lines,1):
                raw=line.split(';',1)[0].strip().upper()
                if not raw:continue
                if raw.startswith('N') or '*' in raw:raise ValueError(f'Linha {idx}: use arquivo G-code sem numeração/checksum')
                if zero and re.fullmatch(r'T[01]',raw):raw='T'+str(int(raw[1])+1)
                elif zero and raw.startswith('T'):raise ValueError(f'Linha {idx}: ferramenta fora do mapa T0/T1')
                prepared.append((idx,raw))
            if not prepared:raise ValueError('Arquivo vazio')
            # Full preflight without hardware; real-mode rules are applied with fake devices.
            self.q.put(('log','Pré-validação sem hardware...'))
            bridge.print=lambda *a,**k:None
            pre=bridge.Bridge(c,bridge.Device('',0,'V',False),bridge.Device('',0,'A',False),live)
            for idx,raw in prepared:
                if self.stop.is_set():raise InterruptedError('Cancelado antes de conectar')
                try:
                    code,_,_=bridge.parse(raw)
                    if live and code not in {'G0','G1','G4','G28','G29','G90','G91','G92','M82','M83','M84','M104','M105','M106','M107','M109','M114','M115','M117','M140','M190','M204','M205','M400','T1','T2'}:raise bridge.BridgeError('Comando fora da lista de teste: '+code)
                    pre.handle(raw)
                except Exception as e:raise ValueError(f'Linha {idx}: {e}') from e
            bridge.print=log; log('Pré-validação OK')
            p=bridge.Device(c['printer_port'],c['printer_baud'],'VYPER',live); m=bridge.Device(c['mmu_port'],9600,'ARDUINO',live)
            b=bridge.Bridge(c,p,m,live); b.setup()
            for i,(idx,raw) in enumerate(prepared,1):
                while self.pause.is_set() and not self.stop.is_set():time.sleep(.1)
                if self.stop.is_set():raise InterruptedError('Parado após comando; inspecione a máquina e desligue aquecedores manualmente se necessário')
                log(f'Linha {idx}: {raw}'); b.handle(raw); self.q.put(('progress',100*i/len(prepared)))
            b.printer('M400'); self.q.put(('done','Concluído' if live else 'Simulação concluída'))
        except Exception as e:
            log('INTERROMPIDO:',e); self.q.put(('done','Interrompido — ver console'))
        finally:
            for d in (p,m):
                if d:d.close()

if __name__=='__main__':
    root=tk.Tk(); App(root); root.mainloop()

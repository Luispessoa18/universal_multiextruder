import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import runner

FIELDS = [('printer_port', 'Porta impressora (T0)'), ('mmu_port', 'Porta Arduino (T1)'),
          ('printer_baud', 'Baud impressora'), ('mmu_baud', 'Baud Arduino'),
          ('p0', 'T0 estacionamento (mm)'), ('l0', 'T0 alvo (mm, medir)'),
          ('native_change_feed', 'T0 troca (mm/min)'), ('minimum_hotend_c', 'Hotend mínimo (°C)'),
          ('steps_mm', 'T1 passos/mm'), ('rpm', 'T1 RPM'),
          ('p1', 'T1 estacionamento (mm)'), ('l1', 'T1 alvo (mm)')]
INITIAL = {'T0 carregado (padrão)': 'T0', 'T1 carregado': 'T1', 'Nenhum (ambos estacionados)': 'none'}


class App:
    def __init__(self, root):
        self.root = root; root.title('Universal Multiextruder'); root.geometry('860x720')
        self.job = runner.Runner(); self.seq = 0; self.vars = {}
        s = runner.load_settings()
        frame = ttk.Frame(root, padding=12); frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Universal Multiextruder', font=('Segoe UI', 19, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='T0: impressora (padrão) | T1: Arduino — interface web: python server.py').pack(anchor='w')
        f = ttk.Frame(frame); f.pack(fill='x', pady=8)
        self.path = tk.StringVar(); ttk.Entry(f, textvariable=self.path).pack(side='left', fill='x', expand=True)
        ttk.Button(f, text='Abrir G-code', command=lambda: self.path.set(filedialog.askopenfilename(
            filetypes=[('G-code', '*.gcode *.gco *.gc'), ('Todos', '*.*')]))).pack(side='left')
        settings = ttk.Frame(frame); settings.pack(fill='x')
        for i, (k, label) in enumerate(FIELDS):
            r, c = i // 2, (i % 2) * 2; self.vars[k] = tk.StringVar(value=str(s[k]))
            ttk.Label(settings, text=label).grid(row=r, column=c, sticky='w', padx=3, pady=2)
            ttk.Entry(settings, textvariable=self.vars[k], width=16).grid(row=r, column=c + 1, sticky='ew', padx=3)
        settings.columnconfigure(1, weight=1); settings.columnconfigure(3, weight=1)
        f = ttk.Frame(frame); f.pack(fill='x', pady=5); ttk.Label(f, text='Estado inicial:').pack(side='left')
        self.initial = tk.StringVar(value=next(k for k, v in INITIAL.items() if v == s['initial_tool']))
        ttk.Combobox(f, textvariable=self.initial, values=list(INITIAL), state='readonly', width=30).pack(side='left')
        self.confirm = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text='Geometria, posição inicial, sentido e temperatura conferidos; troca em local seguro',
                        variable=self.confirm).pack(anchor='w')
        ttk.Label(frame, text='Modo real bloqueia T1 com XYZ+E e comandos de extrusão não suportados.',
                  foreground='#a33').pack(anchor='w')
        f = ttk.Frame(frame); f.pack(fill='x', pady=6)
        self.sim = ttk.Button(f, text='Simular arquivo', command=lambda: self.start(False)); self.sim.pack(side='left')
        self.live = ttk.Button(f, text='Executar real', command=lambda: self.start(True)); self.live.pack(side='left', padx=5)
        self.pb = ttk.Button(f, text='Pausar / retomar', command=self.job.toggle_pause, state='disabled'); self.pb.pack(side='left')
        self.sb = ttk.Button(f, text='Parar após comando', command=self.job.stop, state='disabled'); self.sb.pack(side='left', padx=5)
        self.progress = ttk.Progressbar(frame, maximum=100); self.progress.pack(fill='x', pady=4)
        self.status = ttk.Label(frame, text='Pronto'); self.status.pack(anchor='w')
        self.log = tk.Text(frame, height=15, wrap='word', state='disabled'); self.log.pack(fill='both', expand=True)
        root.protocol('WM_DELETE_WINDOW', self.close); root.after(200, self.poll)

    def close(self):
        if self.job.running:
            messagebox.showwarning('Em execução', 'Pare e aguarde o comando atual antes de fechar. '
                                   'Para emergência, desligue os equipamentos fisicamente.'); return
        self.root.destroy()

    def settings(self):
        d = {k: v.get() for k, v in self.vars.items()}; d['initial_tool'] = INITIAL[self.initial.get()]
        return runner.normalize(d)

    def start(self, live):
        try:
            s = self.settings(); runner.check_start(s, live, self.confirm.get()); runner.save_settings(s)
            if live and not messagebox.askyesno('Teste físico', 'Não é uma impressão multicolorida completa. Confirme que '
                                                'o hotend, os filamentos e a posição da máquina estão seguros. Parar não é '
                                                'parada de emergência. Continuar?'):
                return
            self.job.start(self.path.get(), s, live, self.confirm.get())
            self.seq = 0
            self.log.configure(state='normal'); self.log.delete('1.0', 'end'); self.log.configure(state='disabled')
        except Exception as e:
            messagebox.showerror('Configuração', str(e))

    def poll(self):
        st = self.job.snapshot(self.seq)
        if st['logs']:
            self.log.configure(state='normal')
            for l in st['logs']:
                self.log.insert('end', l['text'] + '\n')
            if int(self.log.index('end-1c').split('.')[0]) > 3000:
                self.log.delete('1.0', '1000.0')
            self.log.see('end'); self.log.configure(state='disabled')
            self.seq = st['logs'][-1]['seq']
        self.progress['value'] = st['progress']
        layer = f" · camada {st['layer']}/{st['total_layers']}" if st['total_layers'] else ''
        self.status.configure(text=f"{st['message']} — {st['progress']:.1f}%{layer}")
        running = st['running']
        for b in (self.sim, self.live): b.configure(state='disabled' if running else 'normal')
        for b in (self.pb, self.sb): b.configure(state='normal' if running else 'disabled')
        self.root.after(200, self.poll)


if __name__ == '__main__':
    root = tk.Tk(); App(root); root.mainloop()

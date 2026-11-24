# Universal Multiextruder

Ponte de G-code para um segundo extrusor externo controlado por Arduino, com interface web para acompanhar a impressão (Orange Pi + cloudflared) e interface desktop (Windows).

| Ferramenta | Onde | Observação |
|---|---|---|
| **T0** (padrão) | Extrusor da própria impressora (Vyper) | Estado inicial padrão: T0 carregado |
| **T1** | Extrusor externo no Arduino (`MMU_Vyper_v13.ino`) | Motor 1 do Adafruit Motor Shield |

O G-code do fatiador pode usar `T0`/`T1` diretamente, sem remapeamento.

**Perfil da impressora:** `profiles/vyper.json`.
- Limites da máquina e G-code inicial/final para o fatiador.
- Aceleração e jerk sempre no padrão do firmware: `M201/M203/M204/M205` são removidos do arquivo.
- Fatores iniciais de velocidade e fluxo.

Guia completo de como o fatiador gera os dados, como mandar ajustes e como configurar Orca/Prusa/Cura: [`docs/VYPER.md`](docs/VYPER.md).

## Arquivos

- `server.py`: interface web (Flask + waitress)
- `web/index.html`: página de acompanhamento
- `app.py`: interface desktop (Tkinter)
- `runner.py`: execução compartilhada (pré-validação, pausa, parada, progresso, temperaturas e camadas)
- `bridge.py`: tradução de G-code e comunicação serial
- `converter.py`: conversão de arquivos Bambu/Orca/Prusa (multicolor) para a Vyper
- `MMU_Vyper_v13.ino`: firmware do Arduino
- `profiles/vyper.json`: perfil da Vyper · `docs/VYPER.md`: guia fatiador → impressora
- `deploy/`: script de instalação, serviço systemd e exemplo do cloudflared

## Windows

```
pip install -r requirements.txt
python server.py      # interface web em http://127.0.0.1:8080  (ou iniciar_web.bat)
python app.py         # interface desktop (ou iniciar.bat)
```

As duas interfaces usam o mesmo `config.json`. Feche o monitor serial da IDE do Arduino antes de conectar.

## Orange Pi (impressora e Arduino via USB)

As duas placas podem ficar ligadas por USB no Orange Pi ao mesmo tempo. Cada uma aparece como uma porta serial:

- Vyper (CH340): `/dev/ttyUSB0`
- Arduino Uno original: `/dev/ttyACM0`. Arduino clone (CH340): `/dev/ttyUSB1`

A numeração pode mudar a cada boot. Na tela de configuração, escolha os caminhos **`/dev/serial/by-id/...`** ou, para dois CH340 sem número de série, **`/dev/serial/by-path/...`** (fixo por porta USB física). Não troque os cabos de porta depois de configurar.

```bash
git clone https://github.com/Luispessoa18/universal_multiextruder.git
cd universal_multiextruder
bash deploy/install_orangepi.sh      # sem sudo
```

O script:
- cria o venv e instala as dependências;
- adiciona o usuário ao grupo `dialout`;
- remove o `brltty`, que bloqueia adaptadores CH340;
- gera uma senha em `.env`;
- instala o serviço `mmu-web`, que sobe sozinho no boot.

Logs: `journalctl -u mmu-web -f`.

Se o Arduino desconectar logo depois de abrir a porta, desinstale o `modemmanager` (`sudo apt remove modemmanager`).

## Acesso externo com cloudflared

O servidor **recusa** requisições vindas do túnel (cabeçalho `Cf-Connecting-IP`) enquanto `MMU_PASSWORD` não estiver definida. Com senha, o acesso usa HTTP Basic pelo HTTPS do Cloudflare. Para mais proteção, coloque o hostname atrás do **Cloudflare Access** (Zero Trust).

Teste rápido, com URL temporária `trycloudflare.com`:

```bash
cloudflared tunnel --url http://localhost:8080
```

Túnel fixo no seu domínio:

```bash
# instalar (arm64): baixe cloudflared-linux-arm64.deb em github.com/cloudflare/cloudflared/releases
cloudflared tunnel login
cloudflared tunnel create mmu
cloudflared tunnel route dns mmu impressora.seudominio.com
cp deploy/cloudflared-config.example.yml ~/.cloudflared/config.yml   # ajuste o ID
sudo cloudflared service install
```

Variáveis (no `.env` ou no ambiente): `MMU_PASSWORD`, `MMU_USER` (padrão `admin`), `MMU_HOST` (padrão `127.0.0.1`; use `0.0.0.0` para a rede local), `MMU_PORT` (8080), `MMU_MAX_UPLOAD_MB` (200; o plano gratuito do Cloudflare limita uploads a 100 MB).

## Interface web

- Envio de G-code e de `.gcode.3mf` (Bambu Studio/Orca); análise das cores e conversão multicolor para a Vyper (N cores → T0/T1).
- Simular, executar real, pausar/retomar e parar.
- Progresso, camada (marcadores `;LAYER_CHANGE` do Prusa/Orca ou `;LAYER:` do Cura), linha e comando atual, ferramenta ativa, temperaturas do hotend e da mesa, tempo decorrido e estimativa do restante.
- Ajustes ao vivo: velocidade, fluxo, ventoinha e temperaturas.
- Leitura da configuração da impressora (M115/M503).
- Configuração de portas (com lista das portas detectadas), perfil e calibração.
- Log em tempo real.

## Geometria do caminho do filamento

```
T0 (Vyper)   ── 540 mm ──┐
                          ├─ hub 90 mm (troca aos 45 mm) ── tubo final 50 mm ──> hotend
T1 (Arduino) ── 470 mm ──┘
```

| | Estacionamento | Alvo | Percurso da troca |
|---|---|---|---|
| T0 | 575 (540 + 45 − 10) | 680 (540 + 90 + 50) | 105 mm |
| T1 | 505 (470 + 45 − 10) | 610 (470 + 90 + 50) | 105 mm |

O estacionamento deixa a ponta 10 mm antes do ponto de troca. Se mudar algum tubo, refaça a conta: estacionamento = tubo + 45 − 10; alvo = tubo + 90 + 50.

## Firmware do Arduino (`MMU_Vyper_v13.ino`)

Serial a 9600 baud. Cada comando responde `ok` ou `error: ...`.

| Comando | Função |
|---|---|
| `PING` | teste |
| `T1` / `T2` | seleciona o **motor** 1/2 do shield (não é a ferramenta do G-code; a ponte usa o motor 1 para T1) |
| `CAL=5.482` | passos/mm do motor selecionado |
| `RPM=20` | velocidade, de 1 a 60 |
| `F10` / `R10` | avança/recua mm (0 < mm ≤ 300) |
| `N1` | voltas |
| `INV` | inverte o sentido |
| `S` | status |

A ponte envia no máximo 100 mm por comando. Também acumula frações menores que 1 passo, em vez de mandar `F0.00000` (que o firmware rejeita com `error: DISTANCE`) ou perder passos no arredondamento. O movimento é bloqueante: a 20 RPM e 5,482 passos/mm, 100 mm levam cerca de 8 s.

## Limitações e segurança

O modo real é experimental. A pré-validação recusa movimentos combinados XYZ+E em T1, porque os motores estão em placas independentes, sem sincronização STEP/DIR. **Ainda não é um sistema de impressão multicolorida funcional.** T0 executa movimentos normais. T1 só testa movimentos de extrusão isolados. Não execute impressão com T1.

Trocas reais exigem os dois percursos calibrados e o estado inicial confirmado. Os alvos (T0 680 mm, T1 610 mm) são o fim do tubo final, não o bico: confira se a extrusora alcança o bico a partir dali antes de habilitar. O G-code deve estacionar o cabeçote em local seguro e aquecer antes da troca. Sem sensor, não há detecção de colisão. Não há garantia de temporização da extrusão externa.

Pausar e parar só atuam entre comandos; não são parada de emergência. Ao parar ou dar erro no modo real, a ponte tenta enviar `M104 S0` e `M140 S0` (melhor esforço). Movimentos já enfileirados continuam. **Em emergência, desligue fisicamente.** Acompanhar pela internet não substitui supervisão presencial. Não retome após erro sem conferir as posições. Use fonte e drivers adequados à corrente dos motores.

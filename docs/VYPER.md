# Anycubic Vyper: do fatiador até a impressora

Perfil usado pela ponte: [`profiles/vyper.json`](../profiles/vyper.json).

## 1. Como os dados chegam na Vyper

```
Fatiador (Orca/Prusa/Cura) ──> arquivo .gcode (texto, uma instrução por linha)
                                   │  enviado pela interface web
                                   ▼
                    runner.py: remove comentários e códigos de aceleração,
                               valida limites do perfil
                                   │
                    bridge.py: T0 ──> USB /dev/ttyUSB0 (115200) ──> Vyper (Marlin)
                               T1 ──> USB /dev/ttyACM0 (9600)   ──> Arduino (F/R mm)
```

- O fatiador **não fala com a impressora**. Ele só gera o arquivo. Tudo o que a Vyper faz está escrito nas linhas do G-code.
- A ponte envia **uma linha por vez** e espera o `ok` da Vyper antes da próxima. É o mesmo protocolo do OctoPrint/Pronterface. O Marlin guarda poucos movimentos no buffer interno.
- Durante `M109`/`M190` (esperar aquecer), a Vyper manda `T:200.1 /210.0 B:60.0 /60.0` a cada segundo. A interface web mostra esses valores.
- A ponte manda `M105` (pedir temperatura) a cada ~5 s entre comandos.

### Estrutura de um arquivo do fatiador

| Parte | Exemplo | O que faz |
|---|---|---|
| Cabeçalho | `;FLAVOR:Marlin`, `;TIME:3600` | Comentários, ignorados |
| Limites da máquina | `M201 X1000...`, `M203`, `M204`, `M205` | Aceleração, velocidade máx., jerk. **Removidos pelo perfil** |
| G-code inicial | `M140`, `M104`, `G28`, `M190`, `M109`, purga | Aquece, faz home, limpa o bico |
| Camadas | `;LAYER_CHANGE` ou `;LAYER:12` | Marcador usado para mostrar "camada x/y" |
| Movimentos | `G1 X120.5 Y80.2 E0.0342 F2700` | Move e extruda |
| Ventoinha | `M106 S255` / `M107` | Liga (0–255) / desliga |
| Troca | `T0` / `T1` | Troca de ferramenta (ponte cuida) |
| G-code final | `M104 S0`, `M140 S0`, `M84` | Desliga tudo |

## 2. Como mandar ajustes no G-code

| Ajuste | Comando | Exemplo | Observação |
|---|---|---|---|
| Velocidade de um movimento | `F` em **mm/min** | `G1 X100 F3000` = 50 mm/s | mm/s × 60. Vale para os próximos `G1` até mudar |
| Velocidade geral (%) | `M220 S<pct>` | `M220 S80` = 80% | Multiplica todos os `F` |
| Fluxo (%) | `M221 S<pct>` | `M221 S95` | Só o extrusor da impressora (T0) |
| Temperatura do hotend | `M104 S<°C>` (não espera) / `M109 S<°C>` (espera) | `M104 S215` | Limite do perfil: 260 °C |
| Temperatura da mesa | `M140 S<°C>` / `M190 S<°C>` | `M140 S65` | Limite do perfil: 110 °C |
| Ventoinha | `M106 S<0-255>` / `M107` | `M106 S128` = 50% | |
| Retração | `G1 E-<mm> F<mm/min>` | `G1 E-5 F2700` | Retração por firmware (`G10/G11`) **não** é suportada |
| Pausa por tempo | `G4 P<ms>` / `G4 S<s>` | `G4 S2` | |
| Mensagem no visor | `M117 <texto>` | `M117 Trocando cor` | |

Na **interface web** (card "Ajustes ao vivo") dá para mudar durante a impressão:
- velocidade (`M220`);
- fluxo (`M221`);
- ventoinha (`M106`);
- hotend (`M104`);
- mesa (`M140`).

O ajuste entra na fila e é enviado no próximo intervalo entre comandos. Comandos do próprio arquivo podem sobrescrever: muitos fatiadores mandam `M106` a cada camada. Ao terminar, a ponte volta velocidade e fluxo para 100%.

Para começar sempre com outro valor, altere `speed_factor` / `flow_factor` em `profiles/vyper.json`.

## 3. Aceleração e jerk: sempre o padrão do firmware

O perfil tem `"strip_codes": ["M201", "M203", "M204", "M205"]`. Essas linhas são **removidas** antes do envio, e o log mostra quantas foram removidas. Assim a Vyper usa a aceleração, a velocidade máxima e o jerk gravados nela, qualquer que seja o fatiador.

Para ver os valores reais, use o botão **"Ler da impressora (M115 / M503)"**, com a máquina ociosa:
- `M92`: passos/mm;
- `M203`: velocidade máxima;
- `M201` / `M204`: aceleração;
- `M205`: jerk;
- `M301`: PID do hotend;
- `M851`: Z-offset;
- `M420`: malha de nivelamento.

Para que um desses códigos volte a ser obedecido, tire-o da lista `strip_codes`.

Também vale desligar no fatiador. Isso deixa o arquivo limpo e a estimativa de tempo mais próxima:
- **OrcaSlicer**:
  - Impressora → *Motion ability* → *Machine limits*: **"Ignore"** (ou "Use for time estimate").
  - Processo → *Speed* → *Acceleration*: desmarque *Enable accel. control* e *jerk*.
- **PrusaSlicer**:
  - Impressora → *Machine limits*: **"Use for time estimate"** (não "Emit to G-code").
  - Impressão → *Speed* → *Acceleration control*: todos os valores **0**.
- **Cura**: Configurações → *Speed*: desmarque **Enable Acceleration Control** e **Enable Jerk Control**.

## 4. Configurar o fatiador para a Vyper

| Item | Valor |
|---|---|
| Área de impressão | 245 × 245 × 260 mm |
| Origem | canto frontal esquerdo (não centralizada) |
| Bico / filamento | 0,4 mm / 1,75 mm |
| G-code flavor | Marlin / Marlin 2 |
| Extrusores | 2 (T0 = impressora, T1 = Arduino) |
| E relativo ou absoluto | os dois funcionam |
| G-code inicial / final | copie de `profiles/vyper.json` → `slicer` (há versão Orca/Prusa e Cura) |

Desative no fatiador, porque a ponte recusa estes recursos:
- **Arc fitting** (`G2/G3`);
- **Retração por firmware** (`G10/G11`);
- **Troca de filamento `M600`**;
- **Exclude objects** (`M486`);
- **Pressure/linear advance** (`M900`).

O G-code inicial do perfil faz home com `G28` e ativa a malha de nivelamento salva com `M420 S1`. A Vyper desliga o nivelamento no home: no M503 aparece `M420 S0`.

Se não houver malha salva, a Vyper responde `Error:Failed to enable Bed Leveling` e a ponte interrompe antes da primeira camada. Nesse caso, nivele pelo menu da impressora (ela salva a malha) ou troque `M420 S1` por `G29` no perfil para nivelar antes de cada impressão. A Vyper nivela pelo sensor de pressão do bico, por isso o bico precisa estar **limpo**.

## 5. Limites que a pré-validação confere

Antes de abrir qualquer porta, a ponte simula o arquivo inteiro e recusa:
- temperatura acima de `max_hotend_c` / `max_bed_c`;
- movimento absoluto fora de `bed_mm` ± `bounds_margin_mm` (desligado após `G92 X/Y/Z`);
- comandos fora da lista do modo real e as restrições de T1 (veja o README).

Os valores do perfil são os de fábrica da Vyper. **Confira-os na sua máquina** com o botão M503 e ajuste o JSON se necessário.

## 6. Arquivos multicolor (Bambu Studio, Orca, Prusa) → Vyper

Envie o `.gcode.3mf` (Bambu Studio/Orca: **"Exportar arquivo fatiado da placa"**) ou o `.gcode` e clique em **"Analisar / converter"**. Um 3MF de projeto, sem fatiar, não funciona: ele não tem G-code dentro.

1. **Cores:** aparecem as cores, o tipo e o peso de cada filamento (do `slice_info.config` do 3MF ou do bloco `; filament_colour =` do G-code), a miniatura da placa e quantas trocas o arquivo tem.
2. **Portas:** informe a cor carregada em T0 e em T1. A ferramenta sugere a porta de cada cor pela cor mais próxima, e você pode trocar.
3. **Mais cores do que portas:** com 3 ou 4 cores e só 2 portas, várias cores caem na mesma porta. As trocas entre elas **somem**, e a tela mostra quantas trocas sobram.
4. **Converter:** gera `<nome>_vyper.gcode`, já selecionado para simular.

O que a conversão faz:
- Troca o início e o fim da impressora de origem pelos do perfil da Vyper. As temperaturas vêm do arquivo.
- Remove os blocos de troca do AMS (`M620`…`M621`): eles vão até a rampa de descarte, fora da mesa da Vyper. No lugar, entra a troca do perfil (`tool_change`): sobe, estaciona, troca, aquece, volta.
- Remove timelapse (`M622`/`M623`), `M1002`, `M73`, `M624/M625`, ventoinhas auxiliares (`M106 P2/P3`) e tudo que a Vyper não tem. O resumo lista o que saiu.
- Converte arcos `G2/G3` em linhas; a subida em espiral da Bambu vira subida reta.
- Centraliza a peça e a torre de purga na mesa de 245 mm. Recusa se não couber.

Limitações:
- A purga da Bambu era feita fora da peça e foi removida. Ative a **torre de purga** no fatiador ou configure `tool_change.purge_mm` no perfil.
- Cores mapeadas para a mesma porta usam a temperatura da primeira.
- **T1 no modo real continua bloqueado** (movimento + extrusão em placas diferentes, sem sincronia). Com cores em T1, o arquivo roda só em simulação. Para imprimir agora, mapeie todas as cores para T0.

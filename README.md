# MMU G-code MVP
Windows: `pip install -r requirements.txt`, depois `python app.py`.
Abra um G-code exportado pelo fatiador. Simulação não abre portas. T1 = Arduino COM4, T2 = Vyper (COM3 é exemplo). Se o slicer usar T0/T1, marque a opção de remapeamento. O firmware Dual Bowden deve aceitar PING, T1, CAL=, RPM=, F e R, respondendo ok ao fim. Feche o monitor serial antes de conectar.

O modo real é experimental: a pré-validação recusa movimentos combinados XYZ+E em T1, pois os motores estão em placas independentes. Não é um sistema de impressão multicolorida funcional ainda. T2 pode executar movimentos normais; T1 pode testar movimentos de extrusão isolados. Trocas reais exigem ambos os percursos calibrados e estado inicial confirmado; o alvo T1 de 444,5 mm é somente o final da mangueira, não o bico. Configure a distância até o bico antes de habilitar. O G-code deve estacionar o cabeçote em local seguro e aquecer antes da troca. Sem sensor, não há detecção de colisão. Não há garantia de temporização da extrusão externa; a velocidade do Arduino é fixa e movimentos pequenos sofrem arredondamento. Não execute impressão T1.

Pausar/parar só atua entre linhas, não é parada de emergência; aquecedores e movimentos já enfileirados podem permanecer ativos. Em emergência desligue fisicamente. Não retome após erro sem conferir as posições. Use fonte e drivers adequados à corrente dos motores.
# universal_multiextruder

Quando você quiser adicionar um fabricante novo no código (ex: Huawei VRP), bastará:

  1. Criar drivers/huawei_vrp.py herdando de BaseDeviceDriver.
  2. Registrar a classe com @register_driver("huawei_vrp").
  3. Implementar os métodos fetch_data (usando comandos como display current-configuration, display esn, etc.) e parse_data.
  4. Executar: python main.py --host 10.0.0.1 --driver huawei_vrp ou colocar no arquivo de hosts.`


    1. Atalho e Flag CLI:
      • Adicionada a flag -P (além de --port) para especificar a porta remota SSH:
        python main.py --host 192.168.1.1 -P 2222 --driver routeros --dry-run

  2. Suporte a Variável de Ambiente (SSH_PORT):
      • O valor padrão da porta consulta a variável de ambiente SSH_PORT (se definida) ou recai para a porta padrão 22.
  3. Sintaxe inline IP:Porta ou Host:Porta:
      • É possível especificar a porta diretamente nos argumentos --host ou nas linhas do arquivo --hosts-file:
        python main.py --host 10.0.0.1:22022 --driver routeros --dry-run
      No arquivo de lista (hosts.txt):
        192.168.88.1:2222
        192.168.88.2:22
        core-router.local:8022
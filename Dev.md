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


  O ecossistema de sincronização com o NetBox é construído sobre a arquitetura de drivers baseada na classe abstrata BaseDeviceDriver. Cada driver implementa dois métodos centrais:

  • fetch_data(...): conecta ao equipamento via SSH e coleta os comandos brutos.
  • parse_data(...): analisa o texto/JSON e normaliza os dados no schema padronizado consumido pelo sync_engine.py.
  ──────
  ### 1. O que cada driver faz
  #### 1.1. DatacomDmOSDriver (slug: dmos)
  • Foco: Roteadores e switches Datacom rodando o sistema operacional DmOS (ex: DM4000, DM4100, DM4200, DM4300, DM4770, OLTs).
  • Coleta SSH (fetch_data):
      • Desabilita paginação via paginate false.
      • Executa: show running-config | nomore, show inventory, show platform, show version, show lldp neighbors.
      • Para comandos leves que não o running-config, executa também a versão estruturada em JSON (| display json | nomore) e possui um reparador próprio de JSON quebrado (fix_dmos_json).
  • Parsing (parse_data):
      • Identificação: Extrai hostname (via config, JSON reparado ou prompt SSH), Serial e Modelo (via show inventory/show platform).
      • L2 / VLANs: Extrai blocos dot1q, VLANs nativas e tagged, além de switchport (native-vlan).
      • MPLS L2VPN: Extrai VPWS (vpws-group com pw-id, neighbor, dot1q) e VPLS (vpls-group com QinQ, customer_vlans, pw-type vlan). Define tags automáticas MPLS-L2VPN e roles VPWS-TUNEIS
      / VPLS-TUNEIS.
      • Interfaces e LAGs: Processa blocos link-aggregation (membros LAG lag-X), portas físicas ethernet/mgmt/loopback, e interfaces L3 (interface l3-X com lower-layer-if vlan).
      • VRRP & FHRP: Parseia bloco router vrrp (vr-id, IP virtual, prioridade, versão).
      • Transceivers: Coleta transceivers ópticos detalhados (Vendor, Serial, Part Number, Presence) em cada porta física a partir de show inventory.
      • LLDP: Suporta extração tanto via JSON estruturado quanto via regex tabular do texto.
  ──────
  #### 1.2. HuaweiVRPDriver (slug: huawei_vrp)

  • Foco: Roteadores (famílias AR e NetEngine/NE), Switches (famílias CloudEngine/CE e S-Series) e caixas BNG/BRAS rodando Huawei VRP (VRP5/VRP8).
  • Coleta SSH (fetch_data):
      • Desabilita paginação via screen-length 0 temporary.
      • Executa: display current-configuration, display version, display esn, display device, display lldp neighbor brief, display interface phy-option | no-more (roteadores) e display
      transceiver | no-more (switches).
  • Parsing (parse_data):
      • Identificação: Captura sysname, modelo a partir do cabeçalho de versão/device e ESN (Electronic Serial Number) dos slots e placas.
      • L2 / VLANs: Processa ranges vlan batch (ex: 10 to 20), blocos individuais com description, modos de porta (port link-type access/trunk/hybrid, port default vlan, trunk allow-pass,
      hybrid tagged/untagged).
      • L3 & IPs: Faz a conversão matemática de máscaras decimais para formato CIDR (ip address X.X.X.X Y.Y.Y.Y -> X.X.X.X/Z), além de IPv6.
      • MPLS L2VPN & BNG:
          • VPWS: Mapeia mpls l2vc <peer> <vc-id> em subinterfaces ou portas.
          • VPLS: Mapeia instâncias vsi <nome> (l2 binding vsi).
          • BNG / BRAS: Mapeia user-vlan <ini> <fim> qinq <svlan> gerando automaticamente as C-VLANs e S-VLAN associadas.
      • LAGs (Eth-Trunk): Extrai agregação com base em blocos interface Eth-TrunkX e declarações eth-trunk X nas portas físicas.
      • VRRP: Extrai vrrp vrid <id> virtual-ip <ip> com prioridade e versão.
      • Transceivers: Suporte duplo:
          • Roteadores: via display interface phy-option (filtrando status físico UP e capturando Vendor Name e Vendor PN).
          • Switches: via display transceiver (Vendor Name, Part Number e Manu. Serial Number).
      • LLDP: Parseia a tabela compacta de display lldp neighbor brief.
  ──────
  #### 1.3. JuniperJunosDriver (slug: junos ou juniper)
  • Foco: Roteadores (MX, PTX, ACX) e switches (QFX, EX) Juniper rodando JunOS.
  • Coleta SSH (fetch_data):
      • Usa sessão SSH direta via Paramiko com shell interativo.
      • Desabilita paginação via set cli screen-length 0.
      • Executa: show version, show configuration | display set, show chassis hardware, show interfaces diagnostics optics | match "Physical interface", show chassis pic fpc-slot X pic-
      slot Y (para cada FPC/PIC detectado) e show lldp neighbors | display json | no-more.
  • Parsing (parse_data):
      • Sintaxe display set: Trabalha com formato de linha única do JunOS (set interfaces..., set vlans...).
      • Filtro de Desativação (_parse_deactivated): Trata linhas deactivate ..., ignorando portas e configurações desativadas administrativamente no JunOS.
      • Logical Systems (VDC): Suporta segmentação lógica (set logical-systems <LS> interfaces ...), associando cada interface ao seu respectivo VDC no NetBox (<hostname>-<ls_name>).
      • Interfaces e LAGs: Mapeia interfaces agregadas (aeX), interfaces IRB (irb.X), subinterfaces lógicas (unit X), VLANs em modo access/trunk e mapeamento automático de descrições IRB
      a partir de set vlans <nome> l3-interface irb.X.
      • Transceivers Detalhados: Cruza dados de show chassis hardware (para capturar Serial e Part Number) com show chassis pic (para obter o Fabricante/Vendor e Vendor Part Number real
      do optics) e com o diagnóstico de óptica para vincular à interface física (et-X/Y/Z ou xe-X/Y/Z).
      • LLDP: Decodifica a árvore hierárquica do JSON retornado pelo comando nativo do JunOS.
  ──────
  #### 1.4. MikrotikRouterOSDriver (slug: routeros)

  • Foco: Roteadores (CCR, RB, hEX) e switches (CRS, CSS) Mikrotik com RouterOS (v6 e v7).
  • Coleta SSH (fetch_data):
      • Executa comandos diretamente via exec_command (SSH não-interativo) com fallback para canal interativo se necessário.
      • Executa comandos no formato terse e export terse para todas as seções: /system identity, /system routerboard, /interface export terse, /ip address export terse, /interface vlan
      export terse, /interface bridge [port] export terse, /interface vrrp export terse, /interface vpls export terse, /ip neighbor print terse e servidores/clientes VPN (WireGuard, L2TP,
      PPTP, OVPN, SSTP).
  • Parsing (parse_data):
      • Desemaranhamento de PTY (_normalize_raw_text): Reconstitui quebras de linha artificiais geradas pela largura de tela do terminal SSH do RouterOS e remove códigos ANSI.
      • Parser Key-Value (_parse_terse_line): Converte a saída terse (formato chave="valor" chave2=valor2) em dicionários Python.
      • Túneis VPN Dedicados (vpn_tunnels): É o driver com suporte mais detalhado a Tunnels do NetBox, sincronizando:
          • WireGuard: peers com chaves públicas, comentários e portas associadas.
          • L2TP, PPTP, OpenVPN, SSTP: instâncias servidor/cliente com usuário e status.
      • Bridges & VLANs: Suporta interfaces Bridge (type: bridge), mapeia portas de bridge (interface bridge port) com suas PVIDs e relações de portas.
      • Bonding (LAG): Extrai interfaces de bonding com suas interfaces escravas (slaves=ether1,ether2).
      • VRRP: Mapeia interfaces VRRP associando o IP virtual correspondente configurado em /ip address.
      • LLDP / MNDP: Parseia /ip neighbor print terse identificando dispositivos remotos via LLDP, CDP ou MNDP (Mikrotik Neighbor Discovery Protocol).

  ──────
  ### 2. Quadro comparativo das diferenças atuais

   Recurso / Característica      |           Datacom DmOS            |              Huawei VRP               |            Juniper JunOS            |           Mikrotik RouterOS
  -------------------------------|-----------------------------------|---------------------------------------|-------------------------------------|---------------------------------------
   Comando de Paginação          |      paginate false / nomore      |       screen-length 0 temporary       |       set cli screen-length 0       | Sem paginação (execução direta/terse)
   Formato de Parsing Primário   | Blocos hierárquicos indentados +  |  Blocos indentados (#, interface) e   |     Sintaxe linear display set      |      Linhas chave-valor terse +
                                 |        JSON com reparador         |                tabelas                |                                     |           normalização PTY
   VLANs Tagged e Untagged       | ✅ Sim (bloco dot1q e switchport) |    ✅ Sim (access, trunk, hybrid)     |       ✅ Sim (interface-mode        | ✅ Sim (PVID bridge port e /interface
                                 |                                   |                                       |            access/trunk)            |                 vlan)
   LAGs (Link Aggregation)       |     ✅ Sim (link-aggregation)     |          ✅ Sim (Eth-Trunk)           |       ✅ Sim (aeX / 802.3ad)        |      ✅ Sim (/interface bonding)
   Interfaces Virtuais / L3      |      ✅ Sim (l3-X, loopback)      |    ✅ Sim (Vlanif, subinterfaces,     |  ✅ Sim (irb.X, unit X, loopback)   |      ✅ Sim (vlan, bridge, etc.)
                                 |                                   |               loopback)               |                                     |
   VPWS (L2VPN)                  |        ✅ Sim (vpws-group)        |          ✅ Sim (mpls l2vc)           |      ❌ Não implementado ([])       |       ❌ Não implementado ([])
   VPLS (L2VPN / QinQ)           |    ✅ Sim (vpls-group + QinQ)     |   ✅ Sim (vsi e BNG user-vlan qinq)   |      ❌ Não implementado ([])       |       ✅ Sim (/interface vpls)
   VRRP / FHRP                   |       ✅ Sim (router vrrp)        |          ✅ Sim (vrrp vrid)           |      ❌ Não implementado ([])       |       ✅ Sim (/interface vrrp)
   Túneis VPN (NetBox Tunnels)   |     ❌ Não implementado ([])      |       ❌ Não implementado ([])        |      ❌ Não implementado ([])       | ✅ Sim (WireGuard, L2TP, PPTP, OVPN,
                                 |                                   |                                       |                                     |                 SSTP)
   Transceivers / Inventário SFP |      ✅ Sim (show inventory)      |      ✅ Sim (phy-option router /      | ✅ Sim (chassis hardware + pic fpc- |       ❌ Não implementado ([])
                                 |                                   |          transceiver switch)          |           slot cruzados)            |
   Vizinhos LLDP                 |  ✅ Sim (JSON ou regex tabular)   | ✅ Sim (display lldp neighbor brief)  |        ✅ Sim (display json)        |   ✅ Sim (/ip neighbor print terse)
   Logical Systems / VDCs        |              ❌ Não               |                ❌ Não                 |    ✅ Sim (set logical-systems)     |                ❌ Não
   Tratamento de deactivate      |                N/A                |                  N/A                  |   ✅ Sim (ignora blocos inativos)   |     ✅ Sim (filtra disabled=yes)
   Deteção Dinâmica de Tags      |   ✅ Sim (BGP, OSPF, LDP, RSVP,   |  ✅ Sim (BGP, OSPF, ISIS, LDP, etc.)  | ⚠️ Estático ({'junos', 'juniper'})  |    ✅ Sim (BGP, OSPF, MPLS, VRRP)
                                 |               etc.)               |                                       |                                     |
  ──────
  ### 3. Síntese das Diferenças Estratégicas

  1. Especialidade do RouterOS: É o único driver com mapeamento completo de Túneis VPN (vpn_tunnels para WireGuard, L2TP, OpenVPN, etc.) e suporte a Bridges, porém não realiza inventário
  de transceivers ópticos.
  2. Especialidade do JunOS: É o único com suporte nativo a Logical Systems (atribuindo VDCs no NetBox) e leitura de configuração em modo set com remoção de itens sob deactivate, além de
  um parser de transceivers que consulta slots PIC individuais para descobrir o fabricante real de ópticas de terceiros. Ainda não tem parsers de L2VPN (VPWS/VPLS) e VRRP implementados.
  3. Especialidade do Huawei VRP: Possui tratamento dual tanto para switches (VRP5) quanto roteadores/BNG (VRP8), cobrindo de forma completa QinQ com C-VLANs para clientes, VPWS/VPLS e
  comando diferenciado de transceivers (phy-option vs transceiver).
  4. Especialidade do Datacom DmOS: Possui suporte aprofundado a pseudowires legados e modernos em DmOS, com recuperação automática de JSONs truncados ou com vírgulas faltantes gerados
  pelo CLI da Datacom.
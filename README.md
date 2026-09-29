# NetBox Sync (`netboxsync`)

**NetBox Sync** é uma ferramenta CLI de automação e sincronização de ativos de rede com o **NetBox**. Desenvolvida com uma **Arquitetura Modular Multimarcas (Multi-Vendor Driver Architecture)** baseada nos padrões de projeto *Strategy* e *Factory Registry*, a aplicação permite coletar configurações via SSH ou arquivos locais de diversos fabricantes e sincronizá-las de forma precisa e automatizada com a API do NetBox.

---

## 🚦 Status dos Drivers de Fabricante (Vendor Compatibility)

| Fabricante / Sistema Operacional | Slug CLI (`--driver`) | Status de Suporte | Recursos Suportados |
| :--- | :--- | :---: | :--- |
| **Datacom DmOS** | `dmos` | **OK** | Interfaces físicas, VLANs, L3, LAGs, IPs (v4/v6), Transceivers, VRRP, VPWS/VPLS, LLDP. |
| **Mikrotik RouterOS** | `routeros` | **OK** | Interfaces físicas, VLANs, Bridges, LAGs (Bonding), IPs (estáticos/dinâmicos/IPv6), VRRP, VPLS, LLDP, **Túneis VPN (`/vpn/tunnels/`)** (WireGuard, L2TP, PPTP, OpenVPN, SSTP). |
| **Juniper JunOS** | `junos` | 🟡 *Pendente* | Em desenvolvimento / Planejado. |
| **Huawei VRP** | `huawei_vrp` | 🟡 *Pendente* | Em desenvolvimento / Planejado. |

---

## 🚀 Capacidades & Funcionalidades Atualizadas

### 1. 🔌 Arquitetura Multimarcas (Multi-Vendor Drivers)
- **Extensível por Design**: Suporte desacoplado a múltiplos sistemas operacionais de rede através dos padrões de projeto *Strategy* e *Factory Registry*.
- **Fábrica de Drivers (`drivers/registry.py`)**: Carregamento dinâmico de drivers via CLI com a flag `--driver <slug>`.
- **Boilerplate Documentado (`drivers/template_driver.py`)**: Guia passo a passo e estrutura padrão para inclusão rápida de novos fabricantes.

---

### 2. 📥 Métodos de Ingestão Flexíveis
- **Arquivo Local (`--file` / `-f`)**: Leitura direta de arquivos contendo a saída de comandos de configuração (ex: `show running-config` ou `export terse`).
- **Conexão SSH Single Host (`--host` / `-H`)**: Conexão interativa via SSH a um equipamento específico.
- **Conexão SSH em Lote (`--hosts-file` / `-F`)**: Processamento em lote de múltiplos equipamentos listados em um arquivo texto (um IP/Host por linha).

---

### 3. 🔄 Recursos Sincronizados com o NetBox

| Módulo NetBox | Descrição das Capacidades |
| :--- | :--- |
| **Dispositivos (Devices)** | Criação e atualização de equipamentos com suporte a `serial`, `model`, `site` (POP), `role`, `tags` e timestamp nos comentários de auditoria. |
| **Sites / POPs** | Criação e associação automática de Sites (POPs) no NetBox caso ainda não existam. |
| **VLANs & Roles** | Sincronização de VLANs globais, reativação de VLANs inativas e categorização por Roles (`PTP-EQUIPAMENTOS`, `VPWS-TUNEIS`, `VPLS-TUNEIS`). |
| **Interfaces** | Mapeamento automático de tipos de interface (`100gbase-x-qsfp28`, `10gbase-x-sfpp`, `25gbase-x-sfp28`, `40gbase-x-qsfpp`, `1000base-t`, `lag`, `bridge`, `virtual`). Configuração de modos Access/Tagged, amarração de membros a LAGs/Bridges e interfaces pai (*Parent Interfaces*). |
| **Túneis VPN (`/vpn/tunnels/`)** | Criação e sincronização automática de Túneis VPN no aplicativo NetBox VPN (3.5+) com mapeamento de **Encapsulation** (`wireguard`, `l2tp`, `pptp`, `openvpn`, `sstp`), status, descrição e associação de terminação (`Tunnel Termination`) no equipamento e interfaces. |
| **Transceivers / Inventário** | Extração de informações de transceivers ópticos (Vendor, Part Number, Serial) e vinculação aos fabricantes e interfaces físicas no NetBox. |
| **Endereços IP & VRF** | Cadastro de IPs IPv4 e IPv6 (estáticos e dinâmicos) vinculados às interfaces físicas, subinterfaces L3 e Loopbacks na VRF Global, com resolução dinâmica de máscaras (CIDR) e atribuição automática do IP primário do dispositivo. |
| **VRRP / FHRP Groups** | Criação e atualização de grupos FHRP (VRRPv2/v3), IP virtual no NetBox IPAM, prioridades e associação às interfaces físicas/L3. |
| **Circuitos L2VPN (VPWS / VPLS)** | Sincronização de túneis VPWS e VPLS no aplicativo VPN do NetBox (3.5+), atribuição de PW-IDs (identifiers) e terminadores em S-VLANs e C-VLANs para topologias Q-in-Q. |
| **Descoberta de Cabos (Cables)** | Conexão automática de cabos entre dispositivos no NetBox utilizando vizinhos LLDP (`show lldp neighbors` / `/ip neighbor print terse`) e fallback por descrições de interface. |

---

### 4. ⚙️ Recursos Avançados de Execução
- **Modo Simulação (`--dry-run`)**: Executa o parsing dos dados e exibe o resumo completo sem aplicar nenhuma alteração no NetBox.
- **Sincronização Modular (`--sync-modules`)**: Permite selecionar exatamente quais módulos sincronizar (ex: `--sync-modules vlans,interfaces,ips` ou `all`).
- **Opções SSL e Segurança (`--insecure` / `DISABLE_SSL_VERIFY`)**: Suporte a ambientes com certificados SSL auto-assinados.
- **Relatório Final da Execução**: Exibe um resumo formatado em tabela indicando o status de cada host processado e totais de sucesso/falha.

---

## 🛠️ Estrutura do Projeto

```text
netboxsync/
├── config.py                 # Configurações globais e credenciais NetBox
├── main.py                   # Ponto de entrada CLI (Argument Parsing & Execution Engine)
├── drivers/                  # Arquitetura de Drivers Multimarcas
│   ├── __init__.py           # Exportação e auto-registro de drivers
│   ├── base.py               # Classe Abstrata Base (BaseDeviceDriver)
│   ├── registry.py           # Decorator e Factory (register_driver, get_driver)
│   ├── datacom_dmos.py       # Driver oficial para Datacom DmOS
│   └── template_driver.py    # Boilerplate documentado para novos fabricantes
├── netbox_sync/
│   └── sync_engine.py        # Motor de sincronização com a API do NetBox (pynetbox)
├── parsers/
│   └── dmos_parser.py        # Módulo de compatibilidade para parsing DmOS
├── utils/
    ├── ssh_client.py         # Sessão SSH interativa parametrizada (Paramiko)
    └── ssh_collector.py      # Módulo de compatibilidade para coleta SSH
```

---

## 📋 Pré-requisitos e Instalação

1. **Python**: Versão 3.8 ou superior.
2. **Instalar Dependências**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configuração de Ambiente (`.env` ou `config.py`)**:
   Crie ou edite o arquivo `.env` com os dados do seu NetBox:
   ```env
   NETBOX_URL=https://netbox.suaempresa.com.br
   NETBOX_TOKEN=seu_token_api_netbox
   SSH_USER=admin
   SSH_PASS=sua_senha
   ```

---

## 💻 Exemplos de Uso

### 1. Ler arquivo local em modo Simulação (Dry-Run)
```bash
python3 main.py --file config_backup.txt --driver dmos --dry-run
```

### 2. Conectar via SSH a um equipamento e sincronizar tudo com NetBox
```bash
python3 main.py --host 192.168.1.1 -u admin -p MinhaSenha --driver dmos
```

### 3. Sincronizar uma lista de equipamentos a partir de um arquivo TXT
```bash
python3 main.py --hosts-file lista_switches.txt -u admin --driver dmos
```

### 4. Sincronizar equipamento Mikrotik RouterOS (com porta SSH customizada)
```bash
python3 main.py --host 192.168.1.1 -P 2269 -u admin -p MinhaSenha --driver routeros --device-type "E50UG"
```

### 5. Sincronizar apenas módulos específicos (ex: IPs e Túneis VPN)
```bash
python3 main.py --host 10.0.0.1 -u admin --driver routeros --sync-modules ips,vpn_tunnels
```

---

## 🆕 Como Adicionar um Novo Fabricante (Ex: Cisco ou Huawei)

Para adicionar suporte a um novo fabricante ou sistema operacional:

1. Crie um arquivo em `drivers/` (ex: `drivers/cisco_ios.py`).
2. Herde de `BaseDeviceDriver` e adicione o decorator `@register_driver('cisco_ios')`:
   ```python
   from drivers.base import BaseDeviceDriver
   from drivers.registry import register_driver

   @register_driver('cisco_ios')
   class CiscoIOSDriver(BaseDeviceDriver):
       driver_name = "Cisco IOS Driver"
       driver_slug = "cisco_ios"

       def fetch_data(self, host, username, password, port=22, debug=False, **kwargs):
           # Coleta de comandos SSH específicos do Cisco
           ...

       def parse_data(self, raw_outputs):
           # Parsing e retorno do schema padrão
           ...
   ```
3. Importe o novo arquivo em `drivers/__init__.py`.
4. O novo driver estará imediatamente disponível para uso na CLI via `--driver cisco_ios`. Consulte `drivers/template_driver.py` para um guia detalhado.

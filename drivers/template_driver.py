"""
Template Driver (Boilerplate / Exemplo para Novos Fabricantes)
==============================================================

Instruções para Adicionar um Novo Fabricante/Driver (ex: Cisco IOS/NX-OS, Huawei VRP, Juniper JunOS, Mikrotik):

Passo 1: Crie um novo arquivo na pasta `drivers/` (ex: `drivers/cisco_ios.py` ou `drivers/huawei_vrp.py`).
Passo 2: Importe `BaseDeviceDriver` e o decorator `@register_driver`.
Passo 3: Defina os atributos da classe `driver_name` e `driver_slug` (único no sistema).
Passo 4: Decore a classe com `@register_driver('slug_do_fabricante')`.
Passo 5: Implemente os métodos abstratos `fetch_data()` e `parse_data()` mantendo 100% do Data Schema esperado pelo NetBox Engine.
Passo 6: Importe seu novo driver em `drivers/__init__.py` para registro automático na CLI.
"""

from typing import Dict, Any
from drivers.base import BaseDeviceDriver
from drivers.registry import register_driver
from utils.ssh_client import SSHClientSession


# Descomente a linha abaixo para registrar este driver com seu slug personalizado:
# @register_driver('template_vendor')
class TemplateVendorDriver(BaseDeviceDriver):
    """
    Driver Template de Exemplo para novos fabricantes.
    """
    driver_name = "Template Vendor Driver"
    driver_slug = "template_vendor"

    def fetch_data(self, host: str, username: str, password: str, port: int = 22, debug: bool = False, **kwargs) -> Dict[str, Any]:
        """
        PASSO A PASSO DA COLETA SSH:
        1. Inicialize a sessão SSH usando SSHClientSession ou cliente Paramiko.
        2. Execute os comandos de leitura necessários (ex: 'show running-config', 'show inventory', 'display current-configuration').
        3. Caso o fabricante exija controle de paginação (ex: 'terminal length 0' no Cisco, 'screen-length 0 temporary' no Huawei), envie esse comando primeiro.
        4. Retorne um dicionário onde as chaves identificam as saídas (ex: {'config': ..., 'inventory': ...}).
        """
        session = SSHClientSession(host=host, username=username, password=password, port=port, debug=debug)
        outputs = {}

        try:
            session.connect()
            # Exemplo desabilitar paginação (ajuste conforme SO):
            # session.send_command("terminal length 0")
            
            # Exemplo de execução de comandos:
            # outputs['config'] = session.send_command("show running-config")
            # outputs['inventory'] = session.send_command("show inventory")
            # outputs['lldp'] = session.send_command("show lldp neighbors")
            pass
        finally:
            session.close()

        return outputs

    def parse_data(self, raw_outputs: Any) -> Dict[str, Any]:
        """
        PASSO A PASSO DO PARSING:
        Recebe `raw_outputs` (textos SSH ou string de arquivo local) e constrói o dicionário padronizado.
        Garante a extração dos campos de acordo com o Schema Esperado pelo `sync_engine.py`.
        """
        config_text = raw_outputs.get('config', '') if isinstance(raw_outputs, dict) else str(raw_outputs)

        # Inicializa estrutura padrão obrigatória
        data = {
            'hostname': None,             # Ex: 'SW-CORE-01'
            'serial': None,               # Ex: 'ABC123456'
            'model': None,                # Ex: 'WS-C3850-48T'
            'tags': set(),                # Ex: {'OSPF', 'BGP', 'VRRP'}
            'vlans': {},                  # Ex: {10: 'VLAN_MGMT', 20: 'VLAN_USERS'}
            'interfaces_physical': [],     # Ex: [{'name': 'GigabitEthernet1/0/1', 'description': 'Uplink', 'enabled': True, 'mtu': 1500, 'speed': '1000'}]
            'interfaces_l3': [],           # Ex: [{'name': 'Vlan10', 'vlan': '10'}]
            'lags': [],                    # Ex: [{'name': 'Port-channel1', 'description': 'Trunk', 'members': ['GigabitEthernet1/0/1']}]
            'ips': [],                     # Ex: [{'interface': 'Vlan10', 'address': '192.168.10.1/24'}]
            'vpws': [],                    # Ex: [{'name': 'PW1', 'pw_id': '100', 'neighbor': '1.1.1.1', 'interface': 'Gi1/0/1', 'vlan_id': 100}]
            'vpls': [],                    # Ex: [{'name': 'VPLS1', 'vlan_id': 200, 'is_qinq': False, 'customer_vlans': [], 'neighbors': [], 'interfaces': []}]
            'interface_vlans': {},         # Ex: {'GigabitEthernet1/0/1': {10, 20}}
            'inventory_items': [],         # Ex: [{'interface': 'GigabitEthernet1/0/1', 'name': 'Transceiver Gi1/0/1', 'manufacturer': 'Cisco', 'part_id': 'GLC-LH-SMD', 'serial': 'SN123'}]
            'vrrp_groups': [],             # Ex: [{'interface': 'Vlan10', 'address_family': 'ipv4', 'vr_id': 1, 'virtual_ip': '192.168.10.254', 'priority': 100, 'version': 'v2'}]
            'lldp_neighbors': [],          # Ex: [{'local_interface': 'Gi1/0/1', 'remote_device': 'SW-ACC-01', 'remote_interface': 'Gi0/1'}]
            'vlan_roles_map': {},          # Ex: {10: 'PTP-EQUIPAMENTOS'}
            'vpn_tunnels': []              # Ex: [{'name': 'IPHONE', 'encapsulation': 'wireguard', 'status': 'active', 'description': 'IPHONE'}]
        }

        # TODO: Adicione aqui as expressões regulares (regex) ou parsers (ex: TextFSM/TTP) para preencher a estrutura `data`.

        return data

    def normalize_interface_type(self, if_name: str) -> str:
        """
        Mapeia os nomes de interface do fabricante para os tipos aceitos pela API do NetBox.
        """
        if not if_name:
            return '10gbase-x-sfpp'

        name_lower = if_name.lower()
        if 'port-channel' in name_lower or 'po' in name_lower:
            return 'lag'
        if 'vlan' in name_lower or 'loopback' in name_lower or 'tunnel' in name_lower:
            return 'virtual'
        if 'hundredgigabitethernet' in name_lower or 'hu' in name_lower:
            return '100gbase-x-qsfp28'
        if 'fortygigabitethernet' in name_lower or 'fo' in name_lower:
            return '40gbase-x-qsfpp'
        if 'tengigabitethernet' in name_lower or 'te' in name_lower:
            return '10gbase-x-sfpp'
        if 'gigabitethernet' in name_lower or 'gi' in name_lower:
            return '1000base-t'

        return super().normalize_interface_type(if_name)

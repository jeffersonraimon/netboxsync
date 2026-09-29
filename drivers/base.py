"""
Base Device Driver Abstract Class
================================
Define a classe base abstrata e o contrato de interface para drivers de diferentes fabricantes.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any


class BaseDeviceDriver(ABC):
    """
    Classe Abstrata Base para Drivers de Equipamentos de Rede.
    Todos os novos fabricantes/sistemas operacionais devem herdar desta classe.
    """
    driver_name: str = "Base Driver"
    driver_slug: str = "base"

    @abstractmethod
    def fetch_data(self, host: str, username: str, password: str, port: int = 22, debug: bool = False, **kwargs) -> Dict[str, Any]:
        """
        Executa a coleta de dados via SSH no equipamento.
        Deve retornar um dicionário contendo as saídas brutas (textos ou JSONs) dos comandos executados.
        """
        pass

    @abstractmethod
    def parse_data(self, raw_outputs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Realiza o parsing das saídas brutas coletadas e gera o dicionário de dados padronizado.
        
        O contrato retornado DEVE conter obrigatoriamente as seguintes chaves:
        {
            'hostname': str,
            'serial': str,
            'model': str,
            'tags': set,
            'vlans': dict,                # vid -> name
            'interfaces_physical': list,   # [{'name', 'description', 'enabled', 'mtu', 'speed'}]
            'interfaces_l3': list,         # [{'name', 'vlan'}]
            'lags': list,                  # [{'name', 'description', 'members'}]
            'ips': list,                   # [{'interface', 'address'}]
            'vpws': list,                  # [{'name', 'pw_id', 'neighbor', 'interface', 'vlan_id'}]
            'vpls': list,                  # [{'name', 'vlan_id', 'is_qinq', 'customer_vlans', 'neighbors', 'interfaces'}]
            'interface_vlans': dict,       # iface_name -> set(vlan_ids)
            'inventory_items': list,       # [{'interface', 'name', 'manufacturer', 'part_id', 'serial'}]
            'vrrp_groups': list,           # [{'interface', 'address_family', 'vr_id', 'virtual_ip', 'priority', 'version'}]
            'lldp_neighbors': list,        # [{'local_interface', 'remote_device', 'remote_interface'}]
            'vlan_roles_map': dict,        # vlan_id -> role_name
            'vpn_tunnels': list            # [{'name', 'encapsulation', 'status', 'description', 'interface', 'tenant'}]
        }
        """
        pass

    def normalize_interface_type(self, if_name: str) -> str:
        """
        Mapeia nomes de interface específicos do fabricante/SO para tipos padronizados do NetBox.
        Tipos comuns NetBox:
        - '100gbase-x-qsfp28'
        - '40gbase-x-qsfpp'
        - '25gbase-x-sfp28'
        - '10gbase-x-sfpp'
        - '1000base-t'
        - 'lag'
        - 'virtual'
        """
        if not if_name:
            return '10gbase-x-sfpp'

        name_lower = if_name.lower()
        if 'lag' in name_lower or 'port-channel' in name_lower or 'aggregate' in name_lower or 'bond' in name_lower:
            return 'lag'
        if any(k in name_lower for k in ['l3-', 'loopback', 'vlan', 'vlan-interface', 've', 'bdi', 'tunnel', 'virtual', 'lo']):
            return 'virtual'
        if '100g' in name_lower or 'hundred' in name_lower or 'hundredgigabit' in name_lower or 'hu' in name_lower:
            return '100gbase-x-qsfp28'
        if '25g' in name_lower or 'twenty-five' in name_lower or 'twentyfive' in name_lower:
            return '25gbase-x-sfp28'
        if '40g' in name_lower or 'forty' in name_lower or 'fortygigabit' in name_lower or 'fo' in name_lower:
            return '40gbase-x-qsfpp'
        if '10g' in name_lower or 'ten-gigabit' in name_lower or 'tengigabit' in name_lower or 'te' in name_lower or 'xgigabit' in name_lower:
            return '10gbase-x-sfpp'
        if 'gigabit' in name_lower or 'ge' in name_lower or 'gi' in name_lower or 'eth' in name_lower or 'ethernet' in name_lower:
            return '1000base-t'

        return '10gbase-x-sfpp'

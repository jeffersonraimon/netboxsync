"""
Juniper JunOS Driver
====================
Driver para coleta e parsing de dados de equipamentos Juniper rodando JunOS.
Suporta sintaxe 'set' (display set), show version, show chassis hardware, e show lldp neighbors.
"""

import re
import json
from typing import Dict, Any, List, Set
from drivers.base import BaseDeviceDriver
from drivers.registry import register_driver


@register_driver('junos')
@register_driver('juniper')
class JuniperJunosDriver(BaseDeviceDriver):
    """
    Driver para coleta e sincronização de equipamentos Juniper (JunOS).
    """
    driver_name: str = "Juniper JunOS Driver"
    driver_slug: str = "junos"

    def fetch_data(self, host: str, username: str, password: str, port: int = 22, debug: bool = False, **kwargs) -> Dict[str, Any]:
        """
        Executa os comandos SSH para coleta do JunOS.
        Suporta Paramiko via SSH standard.
        """
        import paramiko
        import time

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        raw_outputs = {}
        try:
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=password,
                timeout=20,
                look_for_keys=False,
                allow_agent=False
            )

            shell = client.invoke_shell()
            time.sleep(1)

            def send_cmd(cmd: str, wait_sec: float = 2.0) -> str:
                shell.send(cmd + "\n")
                time.sleep(wait_sec)
                output = ""
                while shell.recv_ready():
                    output += shell.recv(65535).decode('utf-8', errors='ignore')
                return output

            # Desativa paginação
            send_cmd("set cli screen-length 0")
            
            raw_outputs["version"] = send_cmd("show version")
            raw_outputs["config"] = send_cmd("show configuration | display set")
            raw_outputs["chassis_hardware"] = send_cmd("show chassis hardware")
            raw_outputs["pic_optics"] = send_cmd("show chassis pic fpc-slot 0 pic-slot 1")
            raw_outputs["lldp_json"] = send_cmd("show lldp neighbors | display json")

        except Exception as e:
            if debug:
                print(f"[JuniperJunosDriver] Erro ao conectar/coletar via SSH em {host}: {e}")
            raise e
        finally:
            client.close()

        return raw_outputs

    def parse_data(self, raw_outputs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parses raw CLI outputs into standard NetBox sync payload.
        """
        config_raw = raw_outputs.get("config", "")
        version_raw = raw_outputs.get("version", "")
        chassis_raw = raw_outputs.get("chassis_hardware", "")
        lldp_raw = raw_outputs.get("lldp_json", "")

        # 1. Hostname & Version & Model
        hostname = self._parse_hostname(config_raw, version_raw)
        model = self._parse_model(version_raw, chassis_raw)
        serial = self._parse_serial(chassis_raw)

        # 2. Extract Data Structures
        vlans = self._parse_vlans(config_raw)
        lags, lag_members = self._parse_lags(config_raw)
        physical_ifaces, l3_units, interface_vlans = self._parse_interfaces(config_raw, lag_members)
        ips = self._parse_ips(config_raw)
        pic_optics_raw = raw_outputs.get("pic_optics", "")
        inventory_items = self._parse_inventory(chassis_raw, pic_optics_raw)
        lldp_neighbors = self._parse_lldp(lldp_raw)

        # Construct final dict
        return {
            'hostname': hostname,
            'serial': serial,
            'model': model,
            'tags': {'junos', 'juniper'},
            'vlans': vlans,
            'interfaces_physical': physical_ifaces,
            'interfaces_l3': l3_units,
            'lags': lags,
            'ips': ips,
            'vpws': [],
            'vpls': [],
            'interface_vlans': interface_vlans,
            'inventory_items': inventory_items,
            'vrrp_groups': [],
            'lldp_neighbors': lldp_neighbors,
            'vlan_roles_map': {},
            'vpn_tunnels': []
        }

    def _parse_hostname(self, config_raw: str, version_raw: str) -> str:
        match = re.search(r'set system host-name (\S+)', config_raw)
        if match:
            return match.group(1).strip()
        match_v = re.search(r'Hostname:\s*(\S+)', version_raw)
        if match_v:
            return match_v.group(1).strip()
        return "JUNOS-DEVICE"

    def _parse_model(self, version_raw: str, chassis_raw: str) -> str:
        match = re.search(r'Model:\s*(\S+)', version_raw)
        if match:
            return match.group(1).upper()
        match_c = re.search(r'Hardware inventory:\s*Item\s+Version\s+Part number\s+Serial number\s+Description\s+Chassis\s+(\S+)', chassis_raw)
        if match_c:
            return match_c.group(1).upper()
        return "JUNOS-GENERIC"

    def _parse_serial(self, chassis_raw: str) -> str:
        # Busca serial do Chassis principal
        for line in chassis_raw.splitlines():
            if line.strip().startswith("Chassis"):
                parts = line.strip().split()
                # Ex: ['Chassis', 'JN1234567890', 'MX240'] ou ['Chassis', 'REV', '01', '740-xxx', 'SERIAL', 'MX240']
                # O serial costuma ser o item antes da descrição ou a 4ª/2ª coluna que não seja REV/versão
                for part in parts[1:]:
                    if part not in ('REV', 'Chassis') and not re.match(r'^\d{2}$', part) and not part.startswith('740-'):
                        return part
        return ""

    def _parse_vlans(self, config_raw: str) -> Dict[int, str]:
        """
        Extrai VLANs declaradas no config:
        set vlans VLAN_NAME vlan-id 100
        ou de nomes como SERVIDORES-v10 (inferindo ID 10)
        """
        vlans = {}
        # 1. Declaração explícita set vlans <name> vlan-id <id>
        for line in config_raw.splitlines():
            line = line.strip()
            match = re.search(r'set vlans (\S+) vlan-id (\d+)', line)
            if match:
                vname = match.group(1)
                vid = int(match.group(2))
                vlans[vid] = vname

        # 2. Inferência via vlan members ou nome vlan com formato *-v<ID>
        for line in config_raw.splitlines():
            line = line.strip()
            matches = re.findall(r'vlan members (\S+)', line)
            for vmem in matches:
                vmem_clean = vmem.strip('[]"')
                vid_match = re.search(r'-v(\d+)$', vmem_clean)
                if vid_match:
                    vid = int(vid_match.group(1))
                    if vid not in vlans:
                        vlans[vid] = vmem_clean
        return vlans

    def _parse_lags(self, config_raw: str) -> tuple[List[Dict[str, Any]], Set[str]]:
        """
        Extrai interfaces LAG (aeX) e seus membros.
        set interfaces ge-0/0/0 gigether-options 802.3ad ae0
        """
        lags_dict = {}
        lag_members = set()

        for line in config_raw.splitlines():
            line = line.strip()
            # Membros
            match_mem = re.search(r'set interfaces (\S+) (?:gigether-options|fastether-options|optics-options) 802\.3ad (\S+)', line)
            if match_mem:
                iface = match_mem.group(1)
                ae_iface = match_mem.group(2)
                lag_members.add(iface)
                if ae_iface not in lags_dict:
                    lags_dict[ae_iface] = {'name': ae_iface, 'description': '', 'members': []}
                lags_dict[ae_iface]['members'].append(iface)

            # Descrição do LAG
            match_desc = re.search(r'set interfaces (ae\d+) description "(.*?)"', line) or re.search(r'set interfaces (ae\d+) description (\S+)', line)
            if match_desc:
                ae_iface = match_desc.group(1)
                desc = match_desc.group(2)
                if ae_iface not in lags_dict:
                    lags_dict[ae_iface] = {'name': ae_iface, 'description': desc, 'members': []}
                else:
                    lags_dict[ae_iface]['description'] = desc

        return list(lags_dict.values()), lag_members

    def _parse_interfaces(self, config_raw: str, lag_members: Set[str]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Set[int]]]:
        """
        Parse de interfaces físicas e unidades lógicas/VLANs.
        """
        ifaces_map = {}
        l3_units = []
        interface_vlans = {}

        # Mapeia VLAN name -> ID para rápida busca
        vlan_name_to_id = {}
        for vid, vname in self._parse_vlans(config_raw).items():
            vlan_name_to_id[vname] = vid

        lines = config_raw.splitlines()
        for line in lines:
            line = line.strip()
            if not line.startswith("set interfaces "):
                continue

            # Ex: set interfaces ge-0/0/0 description "SERVER"
            parts = line.split()
            if len(parts) < 3:
                continue

            iface_full = parts[2]
            
            # Se possui unit, ex: xe-0/0/1 unit 0 family ethernet-switching ...
            if " unit " in line:
                m_unit = re.search(r'set interfaces (\S+) unit (\d+)', line)
                if m_unit:
                    base_if = m_unit.group(1)
                    unit_id = m_unit.group(2)
                    unit_name = f"{base_if}.{unit_id}"

                    # Checa membros de vlan (ethernet-switching)
                    m_vlan = re.search(r'vlan members (\S+)', line)
                    if m_vlan:
                        v_member = m_vlan.group(1).strip('[]"')
                        v_id = vlan_name_to_id.get(v_member)
                        if not v_id:
                            m_id = re.search(r'-v(\d+)$', v_member)
                            if m_id:
                                v_id = int(m_id.group(1))
                        
                        if v_id:
                            # Adiciona ao mapeamento de VLANs da interface física/unidade
                            target_if = base_if if unit_id == "0" else unit_name
                            if target_if not in interface_vlans:
                                interface_vlans[target_if] = set()
                            interface_vlans[target_if].add(v_id)
                            
                            if base_if not in interface_vlans:
                                interface_vlans[base_if] = set()
                            interface_vlans[base_if].add(v_id)

                    # Checa se é uma interface de VLAN L3 (irb.X ou subinterface com vlan-id)
                    if base_if.startswith("irb") or base_if.startswith("vlan"):
                        v_id = int(unit_id) if unit_id.isdigit() else 0
                        l3_units.append({'name': unit_name, 'vlan': v_id})

            # Propriedades da interface base
            if base_if_match := re.search(r'set interfaces ([a-zA-Z0-9\/\-]+)', line):
                iface_name = base_if_match.group(1)
                # Ignora interfaces virtuais internas de sistema
                if iface_name.startswith("lc-") or iface_name.startswith("bme"):
                    continue

                if iface_name not in ifaces_map:
                    ifaces_map[iface_name] = {
                        'name': iface_name,
                        'description': '',
                        'enabled': True,
                        'mtu': 1500,
                        'speed': 10000
                    }

                if " description " in line:
                    m_desc = re.search(r'description "(.*?)"', line) or re.search(r'description (\S+)', line)
                    if m_desc:
                        ifaces_map[iface_name]['description'] = m_desc.group(1)
                
                if " mtu " in line:
                    m_mtu = re.search(r'mtu (\d+)', line)
                    if m_mtu:
                        ifaces_map[iface_name]['mtu'] = int(m_mtu.group(1))
                
                if " disable" in line:
                    ifaces_map[iface_name]['enabled'] = False

        physical_ifaces = list(ifaces_map.values())
        return physical_ifaces, l3_units, interface_vlans

    def _parse_ips(self, config_raw: str) -> List[Dict[str, str]]:
        """
        Extrai IPs de interfaces:
        set interfaces irb unit 10 family inet address 192.168.1.1/24
        set interfaces ge-0/0/0 unit 0 family inet address 10.0.0.1/30
        """
        ips = []
        for line in config_raw.splitlines():
            line = line.strip()
            match = re.search(r'set interfaces (\S+) unit (\d+) family inet(?:6)? address (\S+)', line)
            if match:
                base_if = match.group(1)
                unit_id = match.group(2)
                addr = match.group(3)
                iface_name = f"{base_if}.{unit_id}"
                ips.append({
                    'interface': iface_name,
                    'address': addr
                })
        return ips

    def _parse_inventory(self, chassis_raw: str, pic_optics_raw: str = "") -> List[Dict[str, str]]:
        """
        Extrai transceivers e módulos do show chassis hardware e enriquece o Fabricante (Vendor)
        através do comando show chassis pic fpc-slot X pic-slot Y.
        """
        # 1. Mapear informações de Vendor / Part Number por porta a partir do show chassis pic
        # Exemplo de linha do show chassis pic:
        # 0    100GBASE SR4 T2   MM    PRECISION          PRE-QSFP28-SR4    850 nm   0.0          REV 01
        port_optics_info = {}
        if pic_optics_raw:
            in_port_section = False
            for line in pic_optics_raw.splitlines():
                if "PIC port information:" in line:
                    in_port_section = True
                    continue
                if in_port_section and line.strip() and not line.strip().startswith("Port") and not line.strip().startswith("Fiber"):
                    parts = line.strip().split()
                    # A primeira coluna é o número da porta/Xcvr (ex: 0, 1, 2, 4)
                    if len(parts) >= 5 and parts[0].isdigit():
                        port_idx = parts[0]
                        # Procura o campo de fabricante (ex: PRECISION, OPTLASER, CISCO, FINISAR, JUNIPER, etc.)
                        # O formato típico possui o port_idx na pos 0, e a partir da coluna Fiber type (MM/SM) o vendor
                        vendor = "Juniper"
                        vendor_pn = ""
                        if "MM" in parts or "SM" in parts:
                            idx = parts.index("MM") if "MM" in parts else parts.index("SM")
                            if len(parts) > idx + 1:
                                vendor = parts[idx + 1]
                            if len(parts) > idx + 2:
                                vendor_pn = parts[idx + 2]
                        port_optics_info[port_idx] = {
                            'vendor': vendor,
                            'vendor_pn': vendor_pn
                        }

        items = []
        # 2. Linhas do show chassis hardware contendo Xcvr
        for line in chassis_raw.splitlines():
            if "Xcvr" in line:
                parts = line.strip().split()
                # Ex: ['Xcvr', '0', 'REV', '01', '740-061405', 'BB180419175', 'QSFP-100G-SR4-T2']
                if len(parts) >= 6:
                    xcvr_id = parts[1]
                    xcvr_name = f"{parts[0]} {xcvr_id}"
                    pn = parts[4] if len(parts) >= 6 else ""
                    serial = parts[5] if len(parts) >= 6 else ""
                    model = parts[6] if len(parts) >= 7 else pn

                    # Enriquecimento com dados do show chassis pic
                    optics_data = port_optics_info.get(xcvr_id, {})
                    manufacturer = optics_data.get('vendor', 'Juniper')
                    vendor_pn = optics_data.get('vendor_pn', '')

                    items.append({
                        'interface': xcvr_name,
                        'name': f"Transceiver {xcvr_name}",
                        'manufacturer': manufacturer,
                        'part_id': vendor_pn if vendor_pn else pn,
                        'serial': serial
                    })
        return items

    def _parse_lldp(self, lldp_raw: str) -> List[Dict[str, str]]:
        """
        Extrai vizinhos LLDP a partir do JSON de 'show lldp neighbors | display json'.
        """
        neighbors = []
        if not lldp_raw:
            return neighbors

        try:
            # Tenta encontrar início do JSON caso haja texto/prompts antes
            json_start = lldp_raw.find('{')
            if json_start != -1:
                data = json.loads(lldp_raw[json_start:])
                info_list = data.get("lldp-neighbors-information", [])
                for info in info_list:
                    for neigh in info.get("lldp-neighbor-information", []):
                        local_port = neigh.get("lldp-local-port-id", [{}])[0].get("data", "")
                        remote_sys = neigh.get("lldp-remote-system-name", [{}])[0].get("data", "")
                        remote_port = neigh.get("lldp-remote-port-description", [{}])[0].get("data", "")

                        if local_port and remote_sys:
                            neighbors.append({
                                'local_interface': local_port,
                                'remote_device': remote_sys,
                                'remote_interface': remote_port or "unknown"
                            })
        except Exception:
            pass
        return neighbors

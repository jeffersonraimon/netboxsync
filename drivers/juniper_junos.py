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
                if debug:
                    print(f"  [DEBUG SSH] Executando comando: {cmd}")
                shell.send(cmd + "\n")
                time.sleep(wait_sec)
                output = ""
                while shell.recv_ready():
                    output += shell.recv(65535).decode('utf-8', errors='ignore')
                if debug:
                    print(f"  [DEBUG SSH] Resposta ({len(output)} bytes recebidos)")
                return output

            # Desativa paginação
            send_cmd("set cli screen-length 0")
            
            raw_outputs["version"] = send_cmd("show version")
            raw_outputs["config"] = send_cmd("show configuration | display set")
            raw_outputs["chassis_hardware"] = send_cmd("show chassis hardware")
            raw_outputs["optics_diag"] = send_cmd("show interfaces diagnostics optics | match \"Physical interface\"")
            
            # Testa slots FPC e PIC para obter informações de optics/transceivers
            chassis_hw = raw_outputs.get("chassis_hardware", "")
            fpc_pic_pairs = []
            
            current_fpc = None
            for line in chassis_hw.splitlines():
                fpc_m = re.search(r'\bFPC\s*(?:slot\s*)?(\d+)', line, re.IGNORECASE)
                if fpc_m:
                    current_fpc = int(fpc_m.group(1))
                
                pic_m = re.search(r'\bPIC\s*(?:slot\s*)?(\d+)', line, re.IGNORECASE)
                if pic_m:
                    pic_id = int(pic_m.group(1))
                    fpc_id = current_fpc if current_fpc is not None else 0
                    if (fpc_id, pic_id) not in fpc_pic_pairs:
                        fpc_pic_pairs.append((fpc_id, pic_id))
            
            # Garante pares padrão se não encontrados no chassis hardware
            default_pairs = [(0, 0), (0, 1), (0, 2), (0, 3), (1, 0), (1, 1)]
            for pair in default_pairs:
                if pair not in fpc_pic_pairs:
                    fpc_pic_pairs.append(pair)
            
            pic_optics_results = []
            for fpc_slot, pic_slot in fpc_pic_pairs:
                cmd = f"show chassis pic fpc-slot {fpc_slot} pic-slot {pic_slot}"
                out = send_cmd(cmd, wait_sec=1.0)
                if ("PIC port information:" in out or "PIC version" in out) and "is empty" not in out.lower() and "error" not in out.lower():
                    pic_optics_results.append(out)
            
            raw_outputs["pic_optics"] = "\n\n".join(pic_optics_results)
            raw_outputs["lldp_json"] = send_cmd("show lldp neighbors | display json | no-more")

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

        # Extrai os itens desativados (`deactivate ...`) para ignorá-los no parsing
        deactivated_ifaces, deactivated_lines = self._parse_deactivated(config_raw)
        
        # Filtra linhas desativadas da configuração bruta
        config_lines_active = []
        for line in config_raw.splitlines():
            line_str = line.strip()
            if line_str.startswith("deactivate "):
                continue
            if line_str in deactivated_lines:
                continue
            config_lines_active.append(line)
        config_raw_active = "\n".join(config_lines_active)

        # 1. Hostname & Version & Model
        hostname = self._parse_hostname(config_raw_active, version_raw)
        model = self._parse_model(version_raw, chassis_raw)
        serial = self._parse_serial(chassis_raw)


        # 2. Extract Data Structures
        logical_systems = self._parse_logical_systems(config_raw_active)
        vlans = self._parse_vlans(config_raw_active)
        lags, lag_members = self._parse_lags(config_raw_active)
        physical_ifaces, l3_units, interface_vlans, interface_untagged_vlans = self._parse_interfaces(config_raw_active, lag_members)

        # Filtra interfaces fisicas e subinterfaces desativadas
        physical_ifaces = [i for i in physical_ifaces if i['name'] not in deactivated_ifaces]
        l3_units = [i for i in l3_units if i['name'] not in deactivated_ifaces]

        
        # Atribui VDCs às interfaces associadas aos logical-systems
        if logical_systems:
            iface_to_ls = {}
            for ls_name, ifaces in logical_systems.items():
                vdc_name = f"{hostname}-{ls_name}"
                for if_name in ifaces:

                    iface_to_ls[if_name] = vdc_name
            
            existing_if_names = {i['name'] for i in physical_ifaces}.union({i['name'] for i in l3_units}).union({i['name'] for i in lags})
            for if_name, vdc_name in iface_to_ls.items():
                if if_name not in existing_if_names:
                    # Registra a interface caso ela tenha sido declarada apenas no bloco do logical-system
                    l3_units.append({'name': if_name, 'description': '', 'vlan': 0})
            
            for item in physical_ifaces:
                if item['name'] in iface_to_ls:
                    item['vdc'] = iface_to_ls[item['name']]
            for item in l3_units:
                if item['name'] in iface_to_ls:
                    item['vdc'] = iface_to_ls[item['name']]
            for item in lags:
                if item['name'] in iface_to_ls:
                    item['vdc'] = iface_to_ls[item['name']]


        ips = self._parse_ips(config_raw_active)
        ips = [ip for ip in ips if ip['interface'] not in deactivated_ifaces]

        pic_optics_raw = raw_outputs.get("pic_optics", "")
        optics_diag_raw = raw_outputs.get("optics_diag", "")
        inventory_items = self._parse_inventory(chassis_raw, pic_optics_raw, optics_diag_raw)
        lldp_neighbors = self._parse_lldp(lldp_raw)

        # Construct final dict
        return {
            'hostname': hostname,
            'serial': serial,
            'model': model,
            'tags': {'junos', 'juniper'},
            'logical_systems': logical_systems,
            'vlans': vlans,
            'interfaces_physical': physical_ifaces,
            'interfaces_l3': l3_units,
            'lags': lags,
            'ips': ips,
            'vpws': [],
            'vpls': [],
            'interface_vlans': interface_vlans,
            'interface_untagged_vlans': interface_untagged_vlans,
            'inventory_items': inventory_items,
            'vrrp_groups': [],
            'lldp_neighbors': lldp_neighbors,
            'vlan_roles_map': {},
            'vpn_tunnels': []
        }

    def _parse_deactivated(self, config_raw: str) -> tuple[Set[str], Set[str]]:
        """
        Analisa linhas do tipo 'deactivate ...' e retorna o conjunto de interfaces desativadas
        e o conjunto de comandos 'set ...' desativados correspondentes.
        Ex:
          deactivate interfaces ae4 unit 83
          -> desativa "ae4.83" e ignora todas as linhas "set interfaces ae4 unit 83 ..."
          deactivate system login user xxxxxx
          -> ignora todas as linhas "set system login user xxxxxx ..."
        """
        deactivated_ifaces = set()
        deactivated_lines = set()

        for line in config_raw.splitlines():
            line = line.strip()
            if line.startswith("deactivate "):
                # Converte 'deactivate <path>' em prefixo 'set <path>'
                stmt_path = line[len("deactivate "):].strip()
                target_set_prefix = f"set {stmt_path}"

                # Mapeia interfaces/subinterfaces desativadas
                m_if = re.search(r'^interfaces\s+(\S+)(?:\s+unit\s+(\d+))?', stmt_path) or \
                       re.search(r'^logical-systems\s+\S+\s+interfaces\s+(\S+)(?:\s+unit\s+(\d+))?', stmt_path)
                if m_if:
                    base_if = m_if.group(1)
                    unit_id = m_if.group(2)
                    if_name = f"{base_if}.{unit_id}" if unit_id else base_if
                    deactivated_ifaces.add(if_name)

                # Coleta todas as linhas 'set ...' correspondentes para desativá-las
                for conf_line in config_raw.splitlines():
                    conf_str = conf_line.strip()
                    if conf_str.startswith(target_set_prefix):
                        deactivated_lines.add(conf_str)

        return deactivated_ifaces, deactivated_lines

    def _parse_logical_systems(self, config_raw: str) -> Dict[str, List[str]]:

        """
        Extrai os Logical Systems e suas interfaces associadas.
        Ex: set logical-systems LS-VOAFIBRA_267388 interfaces ae4 unit 1103
        -> {"LS-VOAFIBRA_267388": ["ae4.1103"]}
        """
        ls_map = {}
        for line in config_raw.splitlines():
            line = line.strip()
            # Captura: set logical-systems <LS_NAME> interfaces <IF_NAME> [unit <UNIT>]
            match = re.search(r'set logical-systems (\S+) interfaces (\S+)(?:\s+unit\s+(\d+))?', line)
            if match:
                ls_name = match.group(1)
                base_if = match.group(2)
                unit_id = match.group(3)
                iface_name = f"{base_if}.{unit_id}" if unit_id else base_if
                
                if ls_name not in ls_map:
                    ls_map[ls_name] = []
                if iface_name not in ls_map[ls_name]:
                    ls_map[ls_name].append(iface_name)
        return ls_map


    def normalize_interface_type(self, if_name: str) -> str:
        if not if_name:
            return '10gbase-x-sfpp'

        name_lower = if_name.lower()
        if re.match(r'^ae\d+', name_lower) or any(k in name_lower for k in ['lag', 'port-channel', 'aggregate', 'bond']):
            return 'lag'
        if name_lower.startswith(('lt-', 'gr-')) or any(k in name_lower for k in ['l3-', 'loopback', 'vlan', 'vlan-interface', 've', 'bdi', 'tunnel', 'virtual', 'lo', 'irb', 'lt-', 'gr-']):
            return 'virtual'
        if 'et-' in name_lower or '100g' in name_lower:
            return '100gbase-x-qsfp28'
        if '25g' in name_lower:
            return '25gbase-x-sfp28'
        if '40g' in name_lower:
            return '40gbase-x-qsfpp'
        if 'xe-' in name_lower or '10g' in name_lower:
            return '10gbase-x-sfpp'
        if 'ge-' in name_lower or 'gigabit' in name_lower:
            return '1000base-t'

        return super().normalize_interface_type(if_name)

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

    def _parse_irb_descriptions(self, config_raw: str) -> Dict[str, str]:
        """
        Extrai a associação de nome da VLAN com a interface L3 irb.X:
        set vlans GERENCIA-v1100 l3-interface irb.1100 -> irb.1100: "GERENCIA-v1100"
        """
        irb_desc_map = {}
        for line in config_raw.splitlines():
            line = line.strip()
            match = re.search(r'set vlans (\S+) l3-interface (irb\.\d+)', line)
            if match:
                vlan_name = match.group(1)
                irb_name = match.group(2)
                irb_desc_map[irb_name] = vlan_name
        return irb_desc_map

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
            match_mem = re.search(r'set interfaces (\S+) (?:gigether-options|fastether-options|optics-options) 802\.3ad (\S+)', line) or re.search(r'set interfaces (\S+) .*802\.3ad (\S+)', line)
            if match_mem:
                iface = match_mem.group(1)
                ae_iface = match_mem.group(2)
                lag_members.add(iface)
                if ae_iface not in lags_dict:
                    lags_dict[ae_iface] = {'name': ae_iface, 'description': '', 'members': []}
                if iface not in lags_dict[ae_iface]['members']:
                    lags_dict[ae_iface]['members'].append(iface)

            # Declaração ou Descrição do LAG (aeX ou subinterface aeX.unit)
            match_ae = re.search(r'set interfaces (ae\d+(?:\.\d+)?)', line)
            if match_ae:
                ae_target = match_ae.group(1)
                ae_base = ae_target.split('.')[0]
                if ae_base not in lags_dict:
                    lags_dict[ae_base] = {'name': ae_base, 'description': '', 'members': []}
                
                match_desc = re.search(r'set interfaces ae\d+(?:\.\d+)? description "(.*?)"', line) or re.search(r'set interfaces ae\d+(?:\.\d+)? description (\S+)', line)
                if match_desc:
                    desc_val = match_desc.group(1)
                    if ae_target == ae_base:
                        lags_dict[ae_base]['description'] = desc_val

        return list(lags_dict.values()), lag_members

    def _parse_interfaces(self, config_raw: str, lag_members: Set[str]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Set[int]]]:
        """
        Parse de interfaces físicas e unidades lógicas/VLANs.
        """
        ifaces_map = {}
        l3_units = []
        interface_vlans = {}
        interface_untagged_vlans = {}

        # Mapeia VLAN name -> ID para rápida busca
        vlan_name_to_id = {}
        for vid, vname in self._parse_vlans(config_raw).items():
            vlan_name_to_id[vname] = vid

        # Mapeia descrições automáticas de IRB via set vlans <name> l3-interface irb.X
        irb_desc_map = self._parse_irb_descriptions(config_raw)

        # Mapeia modos de interface (access vs trunk) acumulados por interface física/unidade
        iface_modes = {}
        for line in config_raw.splitlines():
            line = line.strip()
            if "interface-mode access" in line:
                m_if = re.search(r'set interfaces (\S+)', line)
                if m_if:
                    iface_name = m_if.group(1)
                    iface_modes[iface_name] = 'access'
                    base_if = iface_name.split('.')[0]
                    iface_modes[base_if] = 'access'
            elif "interface-mode trunk" in line:
                m_if = re.search(r'set interfaces (\S+)', line)
                if m_if:
                    iface_name = m_if.group(1)
                    iface_modes[iface_name] = 'trunk'
                    base_if = iface_name.split('.')[0]
                    iface_modes[base_if] = 'trunk'

        lines = config_raw.splitlines()
        for line in lines:
            line = line.strip()
            # Remove o prefixo set logical-systems <LS> se presente para parsear a interface corretamente
            line_if = re.sub(r'^set\s+logical-systems\s+\S+\s+', 'set ', line)
            if not line_if.startswith("set interfaces "):
                continue
            line = line_if


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

                    # Checa descrição da subinterface unit ou fallback para irb_desc_map
                    unit_desc = irb_desc_map.get(unit_name, "")
                    if " description " in line:
                        m_udesc = re.search(r'description "(.*?)"', line) or re.search(r'description (\S+)', line)
                        if m_udesc:
                            unit_desc = m_udesc.group(1)

                    if unit_name not in ifaces_map:
                        ifaces_map[unit_name] = {
                            'name': unit_name,
                            'description': unit_desc,
                            'enabled': True,
                            'mtu': 1500,
                            'speed': 10000
                        }
                    elif unit_desc:
                        ifaces_map[unit_name]['description'] = unit_desc

                    # Checa vlan-id explícito na subinterface (ex: set interfaces et-0/1/5 unit 3500 vlan-id 3500)
                    m_vlan_id = re.search(r'vlan-id (\d+)', line)
                    if m_vlan_id:
                        v_id = int(m_vlan_id.group(1))
                        if unit_name not in interface_vlans:
                            interface_vlans[unit_name] = set()
                        interface_vlans[unit_name].add(v_id)

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
                            target_if = unit_name
                            mode_assigned = iface_modes.get(target_if) or iface_modes.get(base_if)

                            if mode_assigned == 'access':
                                # Em modo access, vincula como untagged_vlan na subinterface unit
                                interface_untagged_vlans[target_if] = v_id
                            else:
                                # Adiciona ao mapeamento de VLANs tagged da subinterface unit
                                if target_if not in interface_vlans:
                                    interface_vlans[target_if] = set()
                                interface_vlans[target_if].add(v_id)

                    # Checa se é uma subinterface L3/L2 (possui IP / family inet/inet6, possui vlan-id, unit > 0, ou irb/lo0/vlan)
                    has_inet = "family inet" in line or "family inet6" in line
                    has_vlan_id = bool(m_vlan_id)
                    is_unit_subif = unit_id != "0" or base_if.startswith(("irb", "vlan", "lo0"))
                    if has_inet or has_vlan_id or is_unit_subif:
                        v_id = int(unit_id) if unit_id.isdigit() else 0
                        # Evita duplicatas na lista de L3
                        existing_l3 = next((l3 for l3 in l3_units if l3['name'] == unit_name), None)
                        if not existing_l3:
                            l3_units.append({'name': unit_name, 'vlan': v_id, 'description': unit_desc})
                        elif unit_desc and not existing_l3.get('description'):
                            existing_l3['description'] = unit_desc



            # Captura de descrição explícita para qualquer interface física ou subinterface (ex: ae4, ae4.9, et-0/1/5.1751)
            if " description " in line:
                m_udesc = re.search(r'set interfaces (\S+)\s+unit\s+(\d+)\s+description\s+"(.*?)"', line) or \
                          re.search(r'set interfaces (\S+)\s+unit\s+(\d+)\s+description\s+(\S+)', line)
                if m_udesc:
                    target_if = f"{m_udesc.group(1)}.{m_udesc.group(2)}"
                    desc_val = m_udesc.group(3)
                    if target_if not in ifaces_map:
                        ifaces_map[target_if] = {'name': target_if, 'description': desc_val, 'enabled': True, 'mtu': 1500, 'speed': 10000}
                    else:
                        ifaces_map[target_if]['description'] = desc_val

                    # Atualiza em l3_units caso já exista
                    for l3_item in l3_units:
                        if l3_item['name'] == target_if:
                            l3_item['description'] = desc_val
                else:
                    m_bdesc = re.search(r'set interfaces (\S+)\s+description\s+"(.*?)"', line) or \
                              re.search(r'set interfaces (\S+)\s+description\s+(\S+)', line)
                    if m_bdesc:
                        target_if = m_bdesc.group(1)
                        desc_val = m_bdesc.group(2)
                        if target_if not in ifaces_map:
                            ifaces_map[target_if] = {'name': target_if, 'description': desc_val, 'enabled': True, 'mtu': 1500, 'speed': 10000}
                        else:
                            ifaces_map[target_if]['description'] = desc_val

            # Propriedades adicionais da interface (MTU, disable)
            if base_if_match := re.search(r'set interfaces ([a-zA-Z0-9\/\-]+)', line):
                iface_name = base_if_match.group(1)
                if not iface_name.startswith(("lc-", "bme")):
                    if iface_name not in ifaces_map:
                        ifaces_map[iface_name] = {'name': iface_name, 'description': '', 'enabled': True, 'mtu': 1500, 'speed': 10000}
                    
                    if " mtu " in line:
                        m_mtu = re.search(r'mtu (\d+)', line)
                        if m_mtu:
                            ifaces_map[iface_name]['mtu'] = int(m_mtu.group(1))
                    if " disable" in line:
                        ifaces_map[iface_name]['enabled'] = False

        physical_ifaces = list(ifaces_map.values())
        return physical_ifaces, l3_units, interface_vlans, interface_untagged_vlans

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

    def _parse_inventory(self, chassis_raw: str, pic_optics_raw: str = "", optics_diag_raw: str = "") -> List[Dict[str, str]]:
        """
        Extrai transceivers e módulos do show chassis hardware e enriquece o Fabricante (Vendor)
        através do comando show chassis pic fpc-slot X pic-slot Y.
        Mapeia Xcvr N para o nome de interface real do NetBox (ex: et-0/1/0) usando optics_diag_raw.
        """
        # Mapeia portas do optics diag se disponível (ex: Physical interface: et-0/1/0)
        diag_interfaces = []
        if optics_diag_raw:
            for line in optics_diag_raw.splitlines():
                if "Physical interface:" in line:
                    p = line.split("Physical interface:")[1].strip()
                    if p:
                        diag_interfaces.append(p)

        # 1. Mapear informações de Vendor / Part Number por porta a partir do show chassis pic
        # Tabela: Port Cable_type Fiber_type Xcvr_vendor Xcvr_vendor_part_number Wave-length Xcvr_Firmware JNPR_Rev
        # Ex: 0    100GBASE SR4 T2   MM    PRECISION          PRE-QSFP28-SR4    850 nm   0.0          REV 01
        port_optics_info = {}
        if pic_optics_raw:
            in_port_section = False
            for line in pic_optics_raw.splitlines():
                if "PIC port information:" in line:
                    in_port_section = True
                    continue
                if "PIC slot" in line or "FPC slot" in line:
                    in_port_section = False
                    continue
                if in_port_section and line.strip() and not line.strip().startswith("Port") and not line.strip().startswith("Fiber"):
                    parts = line.strip().split()
                    if len(parts) >= 4 and parts[0].isdigit():
                        port_idx = parts[0]
                        
                        # Procura a coluna Fiber type ("n/a", "MM", "SM") como âncora
                        fiber_idx = -1
                        for i, p in enumerate(parts):
                            if p in ("n/a", "MM", "SM"):
                                fiber_idx = i
                                break
                        
                        vendor = ""
                        vendor_pn = ""
                        if fiber_idx != -1 and len(parts) > fiber_idx + 1:
                            vendor = parts[fiber_idx + 1]
                            
                            # O part number começa em fiber_idx + 2 e vai até a coluna wave-length/firmware
                            end_pn_idx = len(parts)
                            for j in range(fiber_idx + 2, len(parts)):
                                token = parts[j]
                                # Sinais de início da coluna wavelength / firmware:
                                if token in ("n/a", "0.0") or token.endswith("nm") or token.isdigit() or token in ("UNKNOWN", "REV"):
                                    end_pn_idx = j
                                    break
                            
                            if end_pn_idx > fiber_idx + 2:
                                vendor_pn = " ".join(parts[fiber_idx + 2 : end_pn_idx])
                            elif len(parts) > fiber_idx + 2:
                                vendor_pn = parts[fiber_idx + 2]
                        else:
                            # Fallback caso a âncora de fibra não seja encontrada
                            wave_idx = -1
                            for i, p in enumerate(parts):
                                if p == "nm" or p == "0.0":
                                    wave_idx = i
                                    break
                            if wave_idx >= 3:
                                vendor_pn = parts[wave_idx - 2] if parts[wave_idx] == "nm" else parts[wave_idx - 1]
                                vendor = parts[wave_idx - 3] if parts[wave_idx] == "nm" else parts[wave_idx - 2]

                        if vendor and vendor != "Juniper":
                            port_optics_info[port_idx] = {
                                'vendor': vendor,
                                'vendor_pn': vendor_pn
                            }

        items = []
        xcvr_count = 0
        # 2. Linhas do show chassis hardware contendo Xcvr
        for line in chassis_raw.splitlines():
            if "Xcvr" in line:
                parts = line.strip().split()
                # Ex: ['Xcvr', '0', 'REV', '01', '740-061405', 'BB180419175', 'QSFP-100G-SR4-T2']
                if len(parts) >= 6:
                    xcvr_id = parts[1]
                    pn = parts[4] if len(parts) >= 6 else ""
                    serial = parts[5] if len(parts) >= 6 else ""
                    model = parts[6] if len(parts) >= 7 else pn

                    # Resolve interface física correspondente (ex: et-0/1/0 se disponível em diag_interfaces)
                    if xcvr_count < len(diag_interfaces):
                        target_interface = diag_interfaces[xcvr_count]
                    else:
                        target_interface = f"Xcvr {xcvr_id}"
                    xcvr_count += 1

                    optics_data = port_optics_info.get(xcvr_id, {})
                    if not optics_data and target_interface:
                        if_port = target_interface.split('/')[-1] if '/' in target_interface else ""
                        if if_port:
                            optics_data = port_optics_info.get(if_port, {})

                    manufacturer = optics_data.get('vendor', 'Juniper')
                    vendor_pn = optics_data.get('vendor_pn', '')

                    items.append({
                        'interface': target_interface,
                        'name': f"Transceiver {target_interface}",
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
            decoder = json.JSONDecoder()
            data = None
            pos = 0
            while pos < len(lldp_raw):
                json_start = lldp_raw.find('{', pos)
                if json_start == -1:
                    break
                try:
                    data, _ = decoder.raw_decode(lldp_raw, json_start)
                    break
                except json.JSONDecodeError:
                    pos = json_start + 1

            if data:
                # Função recursiva para encontrar todas as listas/dicionários que contêm lldp-local-port-id e lldp-remote-system-name
                def walk_json(obj):
                    if isinstance(obj, dict):
                        # Verifica se o objeto atual é uma entrada de vizinho
                        if "lldp-local-port-id" in obj or "lldp-local-interface" in obj:
                            def extract_val(key_name):
                                val = obj.get(key_name, [])
                                if isinstance(val, list) and len(val) > 0:
                                    first_item = val[0]
                                    if isinstance(first_item, dict):
                                        return first_item.get("data", "")
                                    elif isinstance(first_item, str):
                                        return first_item
                                elif isinstance(val, dict):
                                    return val.get("data", "")
                                elif isinstance(val, str):
                                    return val
                                return ""

                            local_port = extract_val("lldp-local-port-id") or extract_val("lldp-local-interface")
                            remote_sys = extract_val("lldp-remote-system-name")
                            remote_port = extract_val("lldp-remote-port-description") or extract_val("lldp-remote-port-id")

                            if local_port and remote_sys:
                                if not any(n['local_interface'] == local_port and n['remote_device'] == remote_sys and n['remote_interface'] == remote_port for n in neighbors):
                                    neighbors.append({
                                        'local_interface': local_port,
                                        'remote_device': remote_sys,
                                        'remote_interface': remote_port or "unknown"
                                    })
                        for v in obj.values():
                            walk_json(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            walk_json(item)

                walk_json(data)
        except Exception as e:
            print(f"[!] Erro ao parsear JSON do LLDP: {e}")
        return neighbors

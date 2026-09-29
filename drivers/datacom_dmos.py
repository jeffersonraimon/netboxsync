"""
Driver Datacom DmOS
===================
Implementação do driver específico para switches e roteadores Datacom DmOS.
"""

import re
import json
import time
from typing import Dict, Any
import paramiko

from drivers.base import BaseDeviceDriver
from drivers.registry import register_driver
from utils.ssh_client import SSHClientSession


def fix_dmos_json(raw_json):
    """
    Corrige o JSON malformado do DmOS CLI (falta de vírgulas, colchetes, etc.) para permitir json.loads.
    """
    if not raw_json or not isinstance(raw_json, str):
        return None
    start = raw_json.find('{')
    end = raw_json.rfind('}')
    if start == -1 or end == -1:
        return None
    clean_json = raw_json[start:end+1]

    lines = clean_json.splitlines()
    fixed_lines = []
    for i in range(len(lines)):
        curr_line = lines[i]
        fixed_lines.append(curr_line)
        if i + 1 < len(lines):
            next_line = lines[i + 1]
            stripped_next = next_line.strip()
            stripped_curr = curr_line.strip()
            if stripped_next.startswith('"') and ':' in stripped_next:
                if stripped_curr and not stripped_curr.endswith(',') and not stripped_curr.endswith('{') and not stripped_curr.endswith('['):
                    fixed_lines[-1] = curr_line + ','

    repaired_str = '\n'.join(fixed_lines)
    try:
        return json.loads(repaired_str)
    except Exception:
        try:
            repaired_str2 = re.sub(r'(\s*[\}\]\"]\s*)[\r\n]+(\s*\"[^\"]+\"\s*:)', r'\1,\n\2', clean_json)
            return json.loads(repaired_str2)
        except Exception:
            return None


@register_driver('dmos')
class DatacomDmOSDriver(BaseDeviceDriver):
    driver_name = "Datacom DmOS Driver"
    driver_slug = "dmos"

    def fetch_data(self, host: str, username: str, password: str, port: int = 22, debug: bool = False, **kwargs) -> Dict[str, Any]:
        """
        Conecta ao switch DmOS via SSH diretamente via Paramiko e executa lista de comandos de coleta.
        """
        commands = kwargs.get('commands') or [
            "show running-config",
            "show inventory",
            "show platform",
            "show version",
            "show lldp neighbors"
        ]

        session = SSHClientSession(host=host, username=username, password=password, port=port, debug=debug)
        outputs = {}

        try:
            session.connect()
            # Desabilita paginação no DmOS
            session.send_command("paginate false", timeout=10)

            prompt_regex = r'[\r\n][\w\.\-]+[#>]\s*$'

            for cmd in commands:
                # 1. Executa versão texto do comando (mais rápida e confiável)
                # running-config em OLTs com GPON pode demorar até 120-180s
                cmd_timeout = 180 if "running-config" in cmd else 60
                text_cmd = f"{cmd} | nomore"
                print(f"  [➔] Executando comando: {text_cmd}")
                buf_text = session.send_command(text_cmd, expect_regex=prompt_regex, timeout=cmd_timeout)
                outputs[cmd] = buf_text

                # 2. Executa versão JSON apenas para comandos que se beneficiam de JSON leve
                # Evita rodar display json no running-config para evitar estouro de buffer e desincronia no SSH
                if "running-config" not in cmd:
                    json_cmd = f"{cmd} | display json | nomore"
                    print(f"  [➔] Executando comando: {json_cmd}")
                    buf_json = session.send_command(json_cmd, expect_regex=prompt_regex, timeout=cmd_timeout)
                    if "{" in buf_json and "Error" not in buf_json and "Unknown" not in buf_json:
                        outputs[f"{cmd}_json"] = buf_json

        finally:
            session.close()

        return outputs

    def parse_data(self, raw_outputs: Any) -> Dict[str, Any]:
        """
        Analisa os outputs de comandos do DmOS e extrai a estrutura de dados padronizada.
        Suporta receber dicionário de comandos SSH, dicionário simplificado ou string de configuração local.
        """
        config_text = ""
        inventory_text = ""
        platform_text = ""
        lldp_text = ""
        json_payload = None

        if isinstance(raw_outputs, str):
            config_text = raw_outputs
        elif isinstance(raw_outputs, dict):
            config_text = raw_outputs.get("show running-config") or raw_outputs.get("config") or raw_outputs.get("config_text") or ""
            inventory_text = raw_outputs.get("show inventory") or raw_outputs.get("inventory") or raw_outputs.get("inventory_text") or ""
            platform_text = raw_outputs.get("show platform") or raw_outputs.get("platform") or raw_outputs.get("platform_text") or ""
            lldp_text = raw_outputs.get("show lldp neighbors") or raw_outputs.get("lldp") or raw_outputs.get("lldp_text") or ""

            if raw_outputs.get("json_payload"):
                json_payload = raw_outputs["json_payload"]
            else:
                json_payload = {
                    'config': raw_outputs.get("show running-config_json"),
                    'inventory': raw_outputs.get("show inventory_json"),
                    'platform': raw_outputs.get("show platform_json"),
                    'lldp': raw_outputs.get("show lldp neighbors_json"),
                }

        if config_text:
            config_text = config_text.replace('\r\n', '\n').replace('\r', '\n')
        if inventory_text:
            inventory_text = inventory_text.replace('\r\n', '\n').replace('\r', '\n')
        if platform_text:
            platform_text = platform_text.replace('\r\n', '\n').replace('\r', '\n')
        if lldp_text:
            lldp_text = lldp_text.replace('\r\n', '\n').replace('\r', '\n')

        data = {
            'hostname': None,
            'serial': None,
            'model': None,
            'tags': set(),
            'vlans': {},  # vid -> name
            'interfaces_physical': [],
            'interfaces_l3': [],
            'lags': [],
            'ips': [],
            'vpws': [],
            'vpls': [],
            'interface_vlans': {},  # iface_name -> set(vlan_ids)
            'interface_untagged_vlans': {},  # iface_name -> untagged_vlan_id (int)
            'inventory_items': [],  # list of transceiver inventory dicts
            'vrrp_groups': [],      # list of VRRP group dicts
            'lldp_neighbors': [],   # list of LLDP neighbor dicts
            'vlan_roles_map': {},
            'vpn_tunnels': []
        }

        # 1. Hostname
        match_host = re.search(r'^\s*hostname\s+([^\s\n\r]+)', config_text, re.MULTILINE | re.IGNORECASE) or \
                     re.search(r'\bhostname\s+([^\s\n\r"]+)', config_text, re.IGNORECASE) or \
                     re.search(r'system\s+hostname\s+([^\s\n\r]+)', config_text, re.IGNORECASE)
        if match_host:
            data['hostname'] = match_host.group(1).strip()

        # Fallback 1: via JSON com saneamento de JSON DmOS
        if not data['hostname'] and json_payload and json_payload.get('config'):
            try:
                raw_cfg = json_payload['config']
                cfg_json = fix_dmos_json(raw_cfg) if isinstance(raw_cfg, str) else raw_cfg
                if cfg_json:
                    def _find_hostname_in_dict(obj):
                        if isinstance(obj, dict):
                            if 'hostname' in obj and isinstance(obj['hostname'], str):
                                return obj['hostname']
                            for v in obj.values():
                                res = _find_hostname_in_dict(v)
                                if res:
                                    return res
                        elif isinstance(obj, list):
                            for item in obj:
                                res = _find_hostname_in_dict(item)
                                if res:
                                    return res
                        return None

                    found_host = _find_hostname_in_dict(cfg_json)
                    if found_host:
                        data['hostname'] = str(found_host).strip()
            except Exception:
                pass

        # Fallback 2: via Prompt SSH nos logs/saídas do equipamento (ex: OLT-CA#)
        if not data['hostname']:
            all_str = ""
            if isinstance(raw_outputs, dict):
                all_str = "\n".join([str(v) for v in raw_outputs.values()])
            elif isinstance(raw_outputs, str):
                all_str = raw_outputs
            
            prompt_match = re.search(r'[\r\n]([\w\.\-]+)[#>]', all_str) or \
                           re.search(r'^([\w\.\-]+)[#>]', all_str, re.MULTILINE)
            if prompt_match:
                candidate = prompt_match.group(1).strip()
                if candidate.lower() not in ['dmos', 'welcome', 'login', 'user', 'password']:
                    data['hostname'] = candidate

        # 2. Numero de Serie, Modelo e Inventory Items via Inventory / Platform
        if inventory_text:
            serial_match = re.search(r'Serial\s+Number\s*:\s*([\w\-]+)', inventory_text, re.IGNORECASE) or \
                           re.search(r'SN\s*:\s*([\w\-]+)', inventory_text, re.IGNORECASE)
            if serial_match:
                data['serial'] = serial_match.group(1).strip()

            model_match = re.search(r'Product\s+Name\s*:\s*([\w\-]+)', inventory_text, re.IGNORECASE) or \
                          re.search(r'Model\s*:\s*([\w\-]+)', inventory_text, re.IGNORECASE)
            if model_match:
                data['model'] = model_match.group(1).strip()

            # Extrair dados de Transceivers por Interface no show inventory
            iface_blocks = re.findall(r'Interface\s+([a-z0-9\-\/\s]+)\n(.*?)(?=\n\s*Interface|\Z)', inventory_text, re.DOTALL | re.IGNORECASE)
            for if_raw, block in iface_blocks:
                if_name = if_raw.strip()
                presence_match = re.search(r'Presence\s*:\s*(Yes|No)', block, re.IGNORECASE)
                is_present = presence_match and presence_match.group(1).lower() == 'yes'

                vendor_match = re.search(r'Vendor name\s*:\s*(.+)', block, re.IGNORECASE)
                serial_tr_match = re.search(r'Serial number\s*:\s*(.+)', block, re.IGNORECASE)
                part_match = re.search(r'Part number\s*:\s*(.+)', block, re.IGNORECASE)

                if is_present and (vendor_match or serial_tr_match or part_match):
                    data['inventory_items'].append({
                        'interface': if_name,
                        'name': f"Transceiver:{if_name}",
                        'manufacturer': vendor_match.group(1).strip() if vendor_match else '',
                        'part_id': part_match.group(1).strip() if part_match else '',
                        'serial': serial_tr_match.group(1).strip() if serial_tr_match else ''
                    })

        if platform_text and not data['serial']:
            serial_match = re.search(r'Serial\s+Number\s*:\s*([\w\-]+)', platform_text, re.IGNORECASE)
            if serial_match:
                data['serial'] = serial_match.group(1).strip()

        # 3. Protocolos para Tags e VRRP
        full_search_text = (config_text or '') + ' ' + (json.dumps(json_payload) if json_payload else '')
        if re.search(r'ospf[_\-]?v?3|router-ospfv3', full_search_text, re.IGNORECASE):
            data['tags'].add('OSPFv3')
        if re.search(r'\bospf\b|router-ospf\b', full_search_text, re.IGNORECASE):
            data['tags'].add('OSPF')
        if re.search(r'mpls ldp', full_search_text, re.IGNORECASE) or re.search(r'\bldp\b', full_search_text, re.IGNORECASE):
            data['tags'].add('LDP')
        if re.search(r'mpls rsvp', full_search_text, re.IGNORECASE) or re.search(r'\brsvp\b', full_search_text, re.IGNORECASE):
            data['tags'].add('RSVP')
        if re.search(r'mpls l2vpn', full_search_text, re.IGNORECASE) or re.search(r'vpws', full_search_text, re.IGNORECASE) or re.search(r'vpls', full_search_text, re.IGNORECASE):
            data['tags'].add('MPLS-L2VPN')
        if re.search(r'router bgp', full_search_text, re.IGNORECASE) or re.search(r'\bbgp\b', full_search_text, re.IGNORECASE) or re.search(r'neighbor\s+[\d\.]+', full_search_text, re.IGNORECASE):
            data['tags'].add('BGP')
        if re.search(r'router vrrp', full_search_text, re.IGNORECASE) or re.search(r'\bvrrp\b', full_search_text, re.IGNORECASE):
            data['tags'].add('VRRP')

        # Extrair grupos VRRP
        vrrp_block = re.search(r'router vrrp\n(.*?)(?=\n!\n|\n[a-z]|\Z)', config_text, re.DOTALL)
        if vrrp_block:
            vrrp_entries = re.findall(r'interface\s+([\w\-]+)\n\s+address-family\s+([\w]+)\n\s+vr-id\s+(\d+)(.*?)(?=vr-id|\n\s*!\n\s*!|\Z)', vrrp_block.group(1), re.DOTALL)
            for iface_item, af_item, vrid_item, content in vrrp_entries:
                ip_match = re.search(r'address\s+([\d\.\:]+)', content)
                prio_match = re.search(r'priority\s+(\d+)', content)
                ver_match = re.search(r'version\s+(\w+)', content)

                if ip_match:
                    data['vrrp_groups'].append({
                        'interface': iface_item,
                        'address_family': af_item,
                        'vr_id': int(vrid_item),
                        'virtual_ip': ip_match.group(1).strip(),
                        'priority': int(prio_match.group(1)) if prio_match else 100,
                        'version': ver_match.group(1).strip() if ver_match else 'v2'
                    })

        # Extrair vizinhos LLDP via JSON ou Text
        if json_payload and (json_payload.get('lldp') or json_payload.get('lldp_json')):
            raw_lldp_json = json_payload.get('lldp') or json_payload.get('lldp_json')
            try:
                lldp_struct = json.loads(raw_lldp_json) if isinstance(raw_lldp_json, str) else raw_lldp_json
                def _extract_json_lldp(obj):
                    if isinstance(obj, dict):
                        loc_if = obj.get('local-interface') or obj.get('name') or obj.get('local_interface')
                        nbrs = obj.get('neighbor') or obj.get('neighbors')
                        if loc_if and nbrs:
                            if isinstance(nbrs, dict):
                                nbrs = [nbrs]
                            if isinstance(nbrs, list):
                                for n in nbrs:
                                    if isinstance(n, dict):
                                        st = n.get('state', n)
                                        rem_dev = st.get('system-name') or st.get('system_name')
                                        rem_if = st.get('port-id') or st.get('port_id') or st.get('port-description')
                                        if rem_dev or rem_if:
                                            data['lldp_neighbors'].append({
                                                'local_interface': str(loc_if).strip(),
                                                'remote_device': str(rem_dev).strip() if rem_dev else '',
                                                'remote_interface': str(rem_if).strip() if rem_if else ''
                                            })
                            return
                        for k, v in obj.items():
                            _extract_json_lldp(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            _extract_json_lldp(item)
                _extract_json_lldp(lldp_struct)
            except Exception:
                pass

        if lldp_text and not data['lldp_neighbors']:
            if 'lldp neighbors ' in lldp_text:
                block_matches = re.findall(r'lldp\s+neighbors\s+((?:[a-z0-9\-]+-ethernet|mgmt|lag|eth|gi|te|hu)[\s\-][\d\/]+)(.*?)(?=lldp\s+neighbors|[A-Z0-9\-_]+[#>]|\Z)', lldp_text, re.DOTALL | re.IGNORECASE)
                for loc_if, block in block_matches:
                    sys_match = re.search(r'system-name\s+([^\s\n\r]+)', block, re.IGNORECASE)
                    port_match = re.search(r'port-id\s+([^\s\n\r]+)', block, re.IGNORECASE) or \
                                 re.search(r'port-description\s+([^\s\n\r]+)', block, re.IGNORECASE)
                    if sys_match or port_match:
                        data['lldp_neighbors'].append({
                            'local_interface': loc_if.strip(),
                            'remote_device': sys_match.group(1).strip() if sys_match else '',
                            'remote_interface': port_match.group(1).strip() if port_match else ''
                        })

            if not data['lldp_neighbors']:
                for line in lldp_text.strip().splitlines():
                    line_str = line.strip()
                    if not line_str or line_str.startswith('Capability') or line_str.startswith('LOCAL') or line_str.startswith('Local') or line_str.startswith('-') or line_str.startswith('System') or line_str.startswith('NEIGHBOR'):
                        continue

                    parts = [p.strip() for p in re.split(r'\s{2,}', line_str) if p.strip()]
                    if len(parts) >= 4:
                        loc_if = parts[0]
                        if len(parts) >= 6:
                            remote_dev = parts[4]
                            remote_if = parts[5]
                        elif len(parts) == 5:
                            remote_dev = parts[3]
                            remote_if = parts[4]
                        else:
                            remote_dev = parts[2]
                            remote_if = parts[3]

                        if remote_dev and not remote_dev.isdigit() and remote_dev.lower() not in ['mac-address', 'chassis-id', 'local-mgmt', 'interface-name']:
                            data['lldp_neighbors'].append({
                                'local_interface': loc_if,
                                'remote_device': remote_dev,
                                'remote_interface': remote_if
                            })
                            continue

                    loc_match = re.search(r'^((?:[a-z0-9\-]+-ethernet|mgmt|lag|eth|gi|te|hu)[\s\-][\d\/]+)', line_str, re.IGNORECASE)
                    if loc_match:
                        loc_if = loc_match.group(1).strip()
                        remainder = line_str[loc_match.end():].strip()
                        remainder_no_mac = re.sub(r'(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}', '', remainder, flags=re.IGNORECASE).strip()
                        rem_if_match = re.search(r'((?:[a-z0-9\-]+-ethernet|mgmt|lag|eth|gi|te|hu)[\s\-][\d\/]+|\b\d+(?:/\d+)+\b)', remainder_no_mac, re.IGNORECASE)
                        remote_iface = ''
                        remote_device = ''
                        if rem_if_match:
                            remote_iface = rem_if_match.group(1).strip()
                            dev_part = (remainder_no_mac[:rem_if_match.start()] + ' ' + remainder_no_mac[rem_if_match.end():]).strip()
                            tokens = [t.strip() for t in dev_part.split() if t.strip() and not t.strip().isdigit() and t.strip().lower() not in ['mac-address', 'chassis-id']]
                            if tokens:
                                remote_device = tokens[0]
                        else:
                            tokens = [t.strip() for t in remainder_no_mac.split() if t.strip() and not t.strip().isdigit() and t.strip().lower() not in ['mac-address', 'chassis-id']]
                            if len(tokens) >= 2:
                                remote_device = tokens[0]
                                remote_iface = tokens[1]
                            elif len(tokens) == 1:
                                remote_device = tokens[0]

                        if loc_if and remote_device and not remote_device.isdigit() and remote_device.lower() not in ['mac-address', 'chassis-id']:
                            data['lldp_neighbors'].append({
                                'local_interface': loc_if,
                                'remote_device': remote_device,
                                'remote_interface': remote_iface
                            })

        def parse_vlan_range(vlan_str):
            vlans = []
            for part in vlan_str.split(','):
                part = part.strip()
                if '-' in part:
                    try:
                        start, end = part.split('-')
                        vlans.extend(range(int(start), int(end) + 1))
                    except ValueError:
                        pass
                elif part.isdigit():
                    vlans.append(int(part))
            return vlans

        def add_iface_vlan(iface_name, vlan_id):
            if not iface_name:
                return
            iface_formatted = iface_name.replace("ten-gigabit-ethernet ", "ten-gigabit-ethernet-") \
                                        .replace("hundred-gigabit-ethernet ", "hundred-gigabit-ethernet-") \
                                        .replace("gigabit-ethernet ", "gigabit-ethernet-") \
                                        .replace("lag ", "lag-")
            if iface_formatted not in data['interface_vlans']:
                data['interface_vlans'][iface_formatted] = set()
            data['interface_vlans'][iface_formatted].add(int(vlan_id))

        def add_iface_untagged_vlan(iface_name, vlan_id):
            if not iface_name:
                return
            iface_formatted = iface_name.replace("ten-gigabit-ethernet ", "ten-gigabit-ethernet-") \
                                        .replace("hundred-gigabit-ethernet ", "hundred-gigabit-ethernet-") \
                                        .replace("gigabit-ethernet ", "gigabit-ethernet-") \
                                        .replace("lag ", "lag-")
            data['interface_untagged_vlans'][iface_formatted] = int(vlan_id)

        # 4. VLANs nativas e tagged do bloco dot1q
        vlan_block = re.search(r'dot1q\n(.*?)(?=\n!\n|\n[a-z]|\Z)', config_text, re.DOTALL)
        if vlan_block:
            vlan_sub_blocks = re.findall(r'vlan\s+([\d\,\-]+)(.*?)(?=\n\s*vlan\s+[\d\,\-]+|\Z)', vlan_block.group(1), re.DOTALL)
            for vid_str, content in vlan_sub_blocks:
                vids = parse_vlan_range(vid_str)
                name_match = re.search(r'name\s+(.+)', content)
                vname = name_match.group(1).strip() if name_match else None
                for vid in vids:
                    if vname:
                        data['vlans'][vid] = vname
                    elif vid not in data['vlans']:
                        data['vlans'][vid] = f"VLAN-{vid}"

                iface_blocks = re.findall(r'interface\s+([\w\-\/]+)(.*?)(?=\n\s*interface\s+[\w\-\/]+|\n\s*!\s*|\Z)', content, re.DOTALL)
                for iface_item, iface_sub in iface_blocks:
                    is_untagged = 'untagged' in iface_sub.lower()
                    for vid in vids:
                        if is_untagged:
                            add_iface_untagged_vlan(iface_item, vid)
                        else:
                            add_iface_vlan(iface_item, vid)

        # 4.1. Bloco switchport (native-vlan)
        switchport_block = re.search(r'switchport\n(.*?)(?=\n!\n|\n[a-z]|\Z)', config_text, re.DOTALL)
        if switchport_block:
            iface_blocks = re.findall(r'interface\s+([\w\-\/]+)(.*?)(?=\n\s*interface\s+[\w\-\/]+|\n\s*!\s*|\Z)', switchport_block.group(1), re.DOTALL)
            for iface_item, iface_sub in iface_blocks:
                native_match = re.search(r'native-vlan(?:\s*vlan-id|\n\s*vlan-id)\s+(\d+)', iface_sub)
                if native_match:
                    native_vid = int(native_match.group(1))
                    add_iface_untagged_vlan(iface_item, native_vid)

        # Remove VLANs untagged da lista de tagged VLANs por interface
        for iface_key, untagged_vid in data['interface_untagged_vlans'].items():
            if iface_key in data['interface_vlans']:
                data['interface_vlans'][iface_key].discard(untagged_vid)

        vlan_roles_map = {}

        # 5. VPWS Parsing
        vpws_blocks = re.findall(r'vpws-group\s+([\w\-]+)(.*?)(?=\n vpws-group|\n vpls-group|\n!\n|\n\w)', config_text, re.DOTALL)
        for vpws_name, content in vpws_blocks:
            pw_id = re.search(r'pw-id\s+(\d+)', content)
            nbr = re.search(r'neighbor\s+([\d\.]+)', content)
            iface = re.search(r'access-interface\s+([\w\-\/]+)', content)
            dot1q = re.search(r'dot1q\s+(\d+)', content)

            pw_id_val = int(pw_id.group(1)) if pw_id else None
            dot1q_val = int(dot1q.group(1)) if dot1q else None
            iface_name = iface.group(1) if iface else None

            if dot1q_val and dot1q_val <= 4094:
                vlan_vid = dot1q_val
            elif pw_id_val and pw_id_val <= 4094:
                vlan_vid = pw_id_val
            else:
                vlan_vid = None

            if iface_name and vlan_vid:
                add_iface_vlan(iface_name, vlan_vid)
                if vlan_vid not in data['vlans']:
                    data['vlans'][vlan_vid] = f"VLAN-{vlan_vid}-{vpws_name}"

            if vlan_vid:
                vlan_roles_map[vlan_vid] = "VPWS-TUNEIS"

            data['vpws'].append({
                'name': vpws_name,
                'pw_id': pw_id.group(1) if pw_id else '',
                'neighbor': nbr.group(1) if nbr else '',
                'interface': iface_name,
                'vlan_id': vlan_vid
            })

        # 6. VPLS Parsing
        vpls_blocks = re.findall(r'vpls-group\s+([\w\-]+)(.*?)(?=\n vpls-group|\n!\n|\n\w)', config_text, re.DOTALL)
        for vpls_name, content in vpls_blocks:
            pw_type_vlan = re.search(r'pw-type vlan\s+(\d+)', content)
            dot1q = re.search(r'dot1q\s+(\d+)', content)
            vlan_vid = int(pw_type_vlan.group(1)) if pw_type_vlan else (int(dot1q.group(1)) if dot1q else None)

            is_qinq = bool(re.search(r'qinq', content, re.IGNORECASE))
            customer_vlans = []

            encap_match = re.search(r'encapsulation\s+\n?\s*dot1q\s+([\d\-\,\s]+)', content)
            if encap_match:
                customer_vlans = parse_vlan_range(encap_match.group(1))

            access_ifaces = re.findall(r'access-interface\s+([\w\-\/]+)', content)
            for iface in access_ifaces:
                if vlan_vid:
                    add_iface_vlan(iface, vlan_vid)

            if vlan_vid:
                if vlan_vid not in data['vlans']:
                    data['vlans'][vlan_vid] = f"VLAN-{vlan_vid}-{vpls_name}"
                vlan_roles_map[vlan_vid] = "VPLS-TUNEIS"

            for cvid in customer_vlans:
                if cvid not in data['vlans']:
                    data['vlans'][cvid] = f"VLAN-{cvid}-Customer-{vpls_name}"
                vlan_roles_map[cvid] = "VPLS-TUNEIS"

            neighbors = re.findall(r'neighbor\s+([\d\.]+).*?pw-id\s+(\d+)', content, re.DOTALL)
            data['vpls'].append({
                'name': vpls_name,
                'vlan_id': vlan_vid,
                'is_qinq': is_qinq,
                'customer_vlans': customer_vlans,
                'neighbors': neighbors,
                'interfaces': access_ifaces
            })

        # 7. LAGs
        lag_matches = list(re.finditer(r'interface lag (\d+)', config_text, re.IGNORECASE))
        for i, match in enumerate(lag_matches):
            lag_id = match.group(1)
            lag_name = f'lag-{lag_id}'
            start_pos = match.end()
            end_pos = lag_matches[i+1].start() if i + 1 < len(lag_matches) else len(config_text)
            content = config_text[start_pos:end_pos]

            desc_match = re.search(r'description\s+(.+)', content)
            members = re.findall(r'interface\s+((?:[a-z0-9\-]+-ethernet|mgmt|loopback)[\s\-][\d\/]+)', content)
            members_clean = [m.strip() for m in members]

            lag_entry = next((l for l in data['lags'] if l['name'] == lag_name), None)
            if not lag_entry:
                data['lags'].append({
                    'name': lag_name,
                    'description': desc_match.group(1).strip() if desc_match else '',
                    'members': members_clean
                })
            else:
                if desc_match and not lag_entry['description']:
                    lag_entry['description'] = desc_match.group(1).strip()
                if members_clean:
                    lag_entry['members'] = list(set(lag_entry['members'] + members_clean))

        # 8. Interfaces Físicas e Loopbacks
        phys_blocks = re.findall(r'interface\s+((?:[a-z0-9\-]+-ethernet|mgmt|loopback)[\s\-][\d\/]+)(.*?)(?=\n!|\ninterface|\Z)', config_text, re.DOTALL)
        for iface_raw, content in phys_blocks:
            name = iface_raw.strip()
            desc = re.search(r'description\s+(.+)', content)
            enabled = 'shutdown' not in content or 'no shutdown' in content
            mtu = re.search(r'mtu\s+(\d+)', content)
            speed = re.search(r'speed\s+(\w+)', content)

            ipv4 = re.search(r'ipv4 address\s+([\d\.\/]+)', content)
            ipv6 = re.search(r'ipv6 address\s+([\w:\/]+)', content)

            data['interfaces_physical'].append({
                'name': name,
                'description': desc.group(1).strip() if desc else '',
                'enabled': enabled,
                'mtu': int(mtu.group(1)) if mtu else None,
                'speed': speed.group(1) if speed else None
            })

            if ipv4:
                data['ips'].append({'interface': name, 'address': ipv4.group(1)})
            if ipv6:
                data['ips'].append({'interface': name, 'address': ipv6.group(1)})

        # 9. Interfaces L3
        l3_blocks = re.findall(r'interface l3[\s\-]+([\w\-]+)(.*?)(?=\n!|\ninterface|\Z)', config_text, re.DOTALL)
        for l3_name, content in l3_blocks:
            ipv4 = re.search(r'ipv4 address\s+([\d\.\/]+)', content)
            ipv6 = re.search(r'ipv6 address\s+([\w:\/]+)', content)
            lower_vlan = re.search(r'lower-layer-if vlan\s+(\d+)', content)

            iface_name = f"l3-{l3_name}"
            if lower_vlan:
                vvid = int(lower_vlan.group(1))
                vlan_roles_map[vvid] = "PTP-EQUIPAMENTOS"
                add_iface_vlan(iface_name, vvid)
                if vvid not in data['vlans']:
                    data['vlans'][vvid] = f"VLAN-{vvid}-PTP-{l3_name}"

            data['interfaces_l3'].append({
                'name': iface_name,
                'vlan': lower_vlan.group(1) if lower_vlan else None
            })

            if ipv4:
                data['ips'].append({'interface': iface_name, 'address': ipv4.group(1)})
            if ipv6:
                data['ips'].append({'interface': iface_name, 'address': ipv6.group(1)})

        data['vlan_roles_map'] = vlan_roles_map
        return data

    def normalize_interface_type(self, if_name: str) -> str:
        """
        Mapeia tipos de interface Datacom DmOS para o padrão NetBox.
        """
        if not if_name:
            return '10gbase-x-sfpp'

        name_lower = if_name.lower()
        if 'lag' in name_lower:
            return 'lag'
        if 'l3-' in name_lower or 'loopback' in name_lower:
            return 'virtual'
        if 'hundred' in name_lower or '100g' in name_lower:
            return '100gbase-x-qsfp28'
        if 'twenty-five' in name_lower or '25g' in name_lower:
            return '25gbase-x-sfp28'
        if 'forty' in name_lower or '40g' in name_lower:
            return '40gbase-x-qsfpp'
        if 'gigabit-ethernet' in name_lower and 'ten' not in name_lower and 'hundred' not in name_lower and 'twenty' not in name_lower:
            return '1000base-t'

        return '10gbase-x-sfpp'

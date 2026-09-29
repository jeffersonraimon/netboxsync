"""
Driver Mikrotik RouterOS
========================
Implementação do driver específico para roteadores e switches Mikrotik RouterOS.
"""

import re
from typing import Dict, Any
from drivers.base import BaseDeviceDriver
from drivers.registry import register_driver
from utils.ssh_client import SSHClientSession


@register_driver('routeros')
class MikrotikRouterOSDriver(BaseDeviceDriver):
    driver_name = "Mikrotik RouterOS"
    driver_slug = "routeros"

    def fetch_data(self, host: str, username: str, password: str, port: int = 22, debug: bool = False, **kwargs) -> Dict[str, Any]:
        """
        Conecta ao dispositivo Mikrotik RouterOS via SSH e executa os comandos de coleta/leitura.
        """
        commands = kwargs.get('commands') or [
            "/system identity print",
            "/system routerboard print",
            "/interface export terse",
            "/interface wireguard peers export terse",
            "/interface wireguard peers print terse",
            "/interface l2tp-server export terse",
            "/interface l2tp-server print terse",
            "/interface pptp-server export terse",
            "/interface ovpn-server export terse",
            "/interface sstp-server export terse",
            "/ip address export terse",
            "/ip address print terse where dynamic",
            "/ipv6 address export terse",
            "/ipv6 address print terse where global",
            "/interface vlan export terse",
            "/interface bridge export terse",
            "/interface bridge port export terse",
            "/interface vrrp export terse",
            "/interface vpls export terse",
            "/ip neighbor print terse"
        ]

        session = SSHClientSession(host=host, username=username, password=password, port=port, debug=debug)
        outputs = {}

        try:
            session.connect()
            for cmd in commands:
                print(f"  [➔] Executando comando: {cmd}")
                # Envia o comando com timeout otimizado de 10s por comando
                output = session.send_command(cmd, timeout=10)
                outputs[cmd] = output
        finally:
            session.close()

        return outputs

    def _parse_terse_line(self, line: str) -> Dict[str, str]:
        """
        Auxiliar para analisar uma linha terse do RouterOS no formato:
        0 R name="ether1" mtu=1500 disabled=no ...
        Retorna dicionário {chave: valor}.
        """
        # Remove flags/número inicial se houver
        # Ex: " 0 R  name=ether1 address=1.2.3.4/24"
        fields = {}
        # Regex para capturar k=v onde v pode ser "string com espaço" ou sem aspas
        matches = re.findall(r'([\w\-]+)=(?:"([^"]*)"|([^\s]+))', line)
        for key, val_quoted, val_unquoted in matches:
            val = val_quoted if val_quoted != "" else val_unquoted
            fields[key] = val
        return fields

    def _normalize_raw_text(self, text: str) -> str:
        """
        Reconstitui linhas de saída do RouterOS que foram quebradas
        devido à largura de tela do terminal SSH (line wrapping / PTY wrap).
        """
        if not text:
            return ""
        lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        unwrapped = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            is_new_command = (
                stripped.startswith('/') or
                stripped.startswith('#') or
                stripped.startswith('[') or
                re.match(r'^\d+\s+(?:[A-Za-z]{1,3}\s+)?[\w\-]+=', stripped)
            )
            if is_new_command or not unwrapped:
                unwrapped.append(stripped)
            else:
                prev = unwrapped[-1]
                if re.match(r'^[\w\-]+=', stripped) and not (prev.endswith(' ') or prev.endswith('=')):
                    unwrapped[-1] += " " + stripped
                else:
                    unwrapped[-1] += stripped
        return '\n'.join(unwrapped)

    def parse_data(self, raw_outputs: Any) -> Dict[str, Any]:
        """
        Analisa os outputs dos comandos do RouterOS (ou arquivo terse consolidado)
        e retorna o dicionário de dados padronizado.
        """
        data = {
            'hostname': None,
            'serial': None,
            'model': None,
            'tags': set(),
            'vlans': {},                # vid -> name
            'interfaces_physical': [],   # [{'name', 'description', 'enabled', 'mtu', 'speed'}]
            'interfaces_l3': [],         # [{'name', 'vlan'}]
            'lags': [],                  # [{'name', 'description', 'members'}]
            'ips': [],                   # [{'interface', 'address'}]
            'vpws': [],
            'vpls': [],                  # [{'name', 'vlan_id', 'is_qinq', 'customer_vlans', 'neighbors', 'interfaces'}]
            'interface_vlans': {},       # iface_name -> set(vlan_ids)
            'inventory_items': [],
            'vrrp_groups': [],           # [{'interface', 'address_family', 'vr_id', 'virtual_ip', 'priority', 'version'}]
            'lldp_neighbors': [],        # [{'local_interface', 'remote_device', 'remote_interface'}]
            'vlan_roles_map': {},
            'bridge_ports': {},          # member_if -> bridge_name
            'vpn_tunnels': []            # [{'name', 'encapsulation', 'status', 'description', 'interface'}]
        }

        # Normaliza raw_outputs para dicionário de comandos ou string completa desfazendo quebras do PTY
        cmd_dict = {}
        full_text = ""

        if isinstance(raw_outputs, str):
            full_text = self._normalize_raw_text(raw_outputs)
        elif isinstance(raw_outputs, dict):
            cmd_dict = {k: self._normalize_raw_text(v) for k, v in raw_outputs.items()}
            full_text = "\n".join([str(v) for v in cmd_dict.values()])

        def get_output_for(cmd_key: str) -> str:
            if isinstance(raw_outputs, dict):
                matched = [v for k, v in cmd_dict.items() if cmd_key in k and v]
                return "\n".join(matched)
            return full_text

        # 1. Hostname (/system identity print ou prompt/export fallback)
        identity_out = get_output_for("/system identity")
        # Prioriza o prompt do terminal "[admin@ROUTER-EDGE]" ou "name: ROUTER-EDGE"
        name_match = re.search(r'\[[\w\-]+@([^\]\s]+)\]', full_text) or \
                     re.search(r'name:\s*([^\s\n\r]+)', identity_out, re.IGNORECASE) or \
                     re.search(r'set\s+name=(?:"([^"]*)"|([^\s\n\r]+))', identity_out, re.IGNORECASE) or \
                     re.search(r'name=(?:"([^"]*)"|([^\s\n\r]+))', identity_out, re.IGNORECASE)
        if name_match:
            groups = [g for g in name_match.groups() if g is not None]
            if groups:
                data['hostname'] = groups[0].strip()

        # 2. Serial & Model (/system routerboard print ou export fallback)
        rb_out = get_output_for("/system routerboard")
        model_match = re.search(r'model:\s*([^\s\n\r]+)', rb_out, re.IGNORECASE) or \
                      re.search(r'model\s*=\s*(?:"([^"]*)"|([^\s\n\r]+))', rb_out, re.IGNORECASE) or \
                      re.search(r'#\s*model\s*=\s*(?:"([^"]*)"|([^\s\n\r]+))', full_text, re.IGNORECASE)
        serial_match = re.search(r'serial-number:\s*([^\s\n\r]+)', rb_out, re.IGNORECASE) or \
                       re.search(r'serial-number\s*=\s*(?:"([^"]*)"|([^\s\n\r]+))', rb_out, re.IGNORECASE) or \
                       re.search(r'#\s*serial number\s*=\s*(?:"([^"]*)"|([^\s\n\r]+))', full_text, re.IGNORECASE)
        if model_match:
            mgroups = [g for g in model_match.groups() if g is not None]
            if mgroups:
                val = mgroups[0].strip()
                val = re.split(r'firmware-type:|serial-number:|routerboard:', val, flags=re.IGNORECASE)[0]
                data['model'] = val.strip()
        if serial_match:
            sgroups = [g for g in serial_match.groups() if g is not None]
            if sgroups:
                val = sgroups[0].strip()
                val = re.split(r'firmware-type:|model:|routerboard:', val, flags=re.IGNORECASE)[0]
                data['serial'] = val.strip()

        def add_iface_vlan(iface_name: str, vlan_id: int):
            if not iface_name or not vlan_id:
                return
            if iface_name not in data['interface_vlans']:
                data['interface_vlans'][iface_name] = set()
            data['interface_vlans'][iface_name].add(int(vlan_id))

        export_out = get_output_for("/interface export") or full_text

        # 3. VLANs & Subinterfaces VLAN (/interface vlan ou /interface export terse)
        vlan_out = get_output_for("/interface vlan")
        # 3.1 Busca via /interface vlan print/export terse
        for line in vlan_out.splitlines():
            if 'vlan-id=' in line and 'name=' in line:
                fields = self._parse_terse_line(line)
                vname = fields.get('name')
                vid_str = fields.get('vlan-id')
                parent_if = fields.get('interface')
                disabled = fields.get('disabled', 'no').lower() in ['yes', 'true']
                if vid_str and vid_str.isdigit() and vname:
                    vid = int(vid_str)
                    data['vlans'][vid] = vname
                    data['interfaces_l3'].append({
                        'name': vname,
                        'vlan': str(vid),
                        'parent': parent_if,
                        'enabled': not disabled,
                        'type': 'virtual'
                    })
                    if parent_if:
                        add_iface_vlan(parent_if, vid)

        # 3.2 Busca via export (/interface vlan add ...)
        vlan_exports = re.findall(r'/interface vlan add\s+(?:.*?\bdisabled=(yes|no)\b)?.*?name=(?:"([^"]*)"|([^\s]+)).*?vlan-id=(\d+)(?:.*?interface=(?:"([^"]*)"|([^\s]+)))?', export_out, re.IGNORECASE)
        for dis, n1, n2, vid_s, p1, p2 in vlan_exports:
            vname = n1 or n2
            vid = int(vid_s)
            parent_if = p1 or p2
            disabled = dis.lower() == 'yes' if dis else False
            data['vlans'][vid] = vname
            if not any(l['name'] == vname for l in data['interfaces_l3']):
                data['interfaces_l3'].append({'name': vname, 'vlan': str(vid), 'parent': parent_if, 'enabled': not disabled, 'type': 'virtual'})
            if parent_if:
                add_iface_vlan(parent_if, vid)

        # 3.3 Extração genérica de interfaces virtuais (/interface <type> add ...)
        # Captura pppoe-client, wireguard, l2tp-server, l2tp-client, pptp-server, pptp-client, ovpn-server, ovpn-client, sstp-server, sstp-client, gre, eoip, ipip, etc.
        generic_ifaces = re.findall(r'/interface\s+([\w\-]+)\s+add\s+.*?\bname=(?:"([^"]*)"|([^\s]+))', full_text, re.IGNORECASE)
        for if_type, n1, n2 in generic_ifaces:
            iname = n1 or n2
            if iname and if_type.lower() not in ('vlan', 'bridge', 'bonding', 'list', 'detect-internet', 'wireguard peers'):
                if not any(l['name'] == iname for l in data['interfaces_l3']) and not any(p['name'] == iname for p in data['interfaces_physical']):
                    data['interfaces_l3'].append({'name': iname, 'vlan': None, 'type': 'virtual'})

        # 3.4 Extração de Túnis VPN (NetBox Tunnels em /vpn/tunnels/)
        # WireGuard, L2TP, PPTP, OVPN, SSTP
        # 3.4.1 WireGuard Peers (/interface wireguard peers export terse ou print terse)
        wg_peers_out = get_output_for("/interface/wireguard/peers") or get_output_for("/interface wireguard peers")
        if not wg_peers_out and not isinstance(raw_outputs, dict):
            wg_peers_out = full_text

        for line in wg_peers_out.splitlines():
            if ('/interface wireguard peers add' in line or 'interface=' in line) and ('public-key=' in line or 'allowed-address=' in line or 'name=' in line):
                fields = self._parse_terse_line(line)
                pname = fields.get('name')
                comment = fields.get('comment', '')
                parent_if = fields.get('interface')
                disabled = fields.get('disabled', 'no').lower() in ['yes', 'true']
                
                # Se não houver name explícito, usa o comment ou gera um nome amigável com base na public-key
                if not pname:
                    if comment:
                        pname = comment.replace(' ', '_')
                    elif fields.get('public-key'):
                        pkey = fields.get('public-key')
                        pname = f"peer-{pkey[:8]}"
                
                if pname:
                    if not any(t['name'] == pname for t in data['vpn_tunnels']):
                        data['vpn_tunnels'].append({
                            'name': pname,
                            'encapsulation': 'wireguard',
                            'status': 'disabled' if disabled else 'active',
                            'description': comment or f"WireGuard Peer (Interface: {parent_if})",
                            'interface': parent_if
                        })

        # 3.4.2 Servidores/Clientes VPN (L2TP, PPTP, OVPN, SSTP) (/interface <vpn-type> add ...)
        vpn_cmds = [
            ("/interface l2tp-server", "l2tp"),
            ("/interface l2tp-client", "l2tp"),
            ("/interface pptp-server", "pptp"),
            ("/interface pptp-client", "pptp"),
            ("/interface ovpn-server", "openvpn"),
            ("/interface ovpn-client", "openvpn"),
            ("/interface sstp-server", "sstp"),
            ("/interface sstp-client", "sstp"),
        ]
        for cmd_prefix, encap in vpn_cmds:
            vpn_out = get_output_for(cmd_prefix)
            if not vpn_out and not isinstance(raw_outputs, dict):
                vpn_out = full_text
            for line in vpn_out.splitlines():
                cmd_short = cmd_prefix.replace('/interface ', '')
                if ('/interface' in line and cmd_short in line and ('add' in line or 'set' in line)) or ('name=' in line and ('user=' in line or 'authentication=' in line or 'default-profile=' in line)):
                    fields = self._parse_terse_line(line)
                    vname = fields.get('name')
                    user = fields.get('user')
                    comment = fields.get('comment', '')
                    disabled = fields.get('disabled', 'no').lower() in ['yes', 'true']
                    if vname:
                        desc = comment or (f"User: {user}" if user else f"{encap.upper()} Tunnel")
                        if not any(t['name'] == vname for t in data['vpn_tunnels']):
                            data['vpn_tunnels'].append({
                                'name': vname,
                                'encapsulation': encap,
                                'status': 'disabled' if disabled else 'active',
                                'description': desc,
                                'interface': None
                            })

        # 4. Bridge Port / Trunk & Access VLAN mapping (/interface bridge port)
        bridge_port_out = get_output_for("/interface bridge port") or export_out
        for line in bridge_port_out.splitlines():
            if 'interface=' in line:
                fields = self._parse_terse_line(line)
                port_if = fields.get('interface')
                pvid = fields.get('pvid')
                bname = fields.get('bridge')
                if port_if and pvid and pvid.isdigit():
                    add_iface_vlan(port_if, int(pvid))
                    if int(pvid) not in data['vlans']:
                        data['vlans'][int(pvid)] = f"VLAN-{pvid}"
                if port_if and bname:
                    # Registra a relação de bridge_port no dicionário de bridges
                    if 'bridge_ports' not in data:
                        data['bridge_ports'] = {}
                    data['bridge_ports'][port_if] = bname

        # 4.1. Bridge Interfaces (/interface bridge)
        bridge_out = get_output_for("/interface bridge") or export_out
        for line in bridge_out.splitlines():
            if ('/interface bridge add' in line or 'name=' in line) and not line.strip().startswith('#'):
                fields = self._parse_terse_line(line)
                bname = fields.get('name')
                if bname and not any(l['name'] == bname for l in data['interfaces_l3']):
                    data['interfaces_l3'].append({'name': bname, 'vlan': None, 'type': 'bridge'})

        # 5. LAGs (Bonding) (/interface bonding print terse ou export)
        bonding_out = get_output_for("/interface bonding") or export_out
        for line in bonding_out.splitlines():
            if 'slaves=' in line and 'name=' in line:
                fields = self._parse_terse_line(line)
                bname = fields.get('name')
                slaves = fields.get('slaves', '')
                if bname and slaves:
                    members = [s.strip() for s in slaves.split(',') if s.strip()]
                    if not any(l['name'] == bname for l in data['lags']):
                        data['lags'].append({
                            'name': bname,
                            'description': f"RouterOS Bonding {bname}",
                            'members': members
                        })

        if not data['lags']:
            bond_matches = re.findall(r'name=(?:"([^"]*)"|([^\s]+)).*?slaves=(?:"([^"]*)"|([^\s]+))', bonding_out, re.IGNORECASE)
            for m in bond_matches:
                bname = m[0] or m[1]
                slaves_str = m[2] or m[3]
                members = [s.strip() for s in slaves_str.split(',') if s.strip()]
                if not any(l['name'] == bname for l in data['lags']):
                    data['lags'].append({
                        'name': bname,
                        'description': f"RouterOS Bonding {bname}",
                        'members': members
                    })

        # 6. Physical Interfaces (/interface print terse ou /interface ethernet)
        # Identifica interfaces fisicas reais (ether, sfp, etc.)
        iface_out = get_output_for("/interface") or export_out
        for line in iface_out.splitlines():
            if ('default-name=' in line or 'name=' in line) and not line.strip().startswith('#'):
                fields = self._parse_terse_line(line)
                iname = fields.get('name') or fields.get('default-name')
                if iname and (iname.startswith('ether') or 'sfp' in iname or 'combo' in iname):
                    if not any(p['name'] == iname for p in data['interfaces_physical']):
                        disabled = fields.get('disabled', 'no').lower() in ['yes', 'true']
                        mtu_val = fields.get('mtu') or fields.get('default-name')
                        comment = fields.get('comment', '')
                        data['interfaces_physical'].append({
                            'name': iname,
                            'description': comment,
                            'enabled': not disabled,
                            'mtu': int(mtu_val) if mtu_val and mtu_val.isdigit() else None,
                            'speed': None
                        })

        # 7. IPs IPv4 & IPv6 (/ip address export terse & /ipv6 address export terse)
        ip_out = get_output_for("/ip address") or export_out
        for line in ip_out.splitlines():
            if 'address=' in line:
                fields = self._parse_terse_line(line)
                if fields.get('disabled', 'no').lower() in ['yes', 'true']:
                    continue
                addr = fields.get('address')
                iface = fields.get('interface') or fields.get('actual-interface')
                if addr and iface and not any(ip['address'] == addr and ip['interface'] == iface for ip in data['ips']):
                    data['ips'].append({
                        'interface': iface,
                        'address': addr
                    })

        # Extração de IPv6 (Ignorando link-local fe80:: e disabled=yes)
        ipv6_out = get_output_for("/ipv6 address") or export_out
        for line in ipv6_out.splitlines():
            if 'address=' in line:
                fields = self._parse_terse_line(line)
                if fields.get('disabled', 'no').lower() in ['yes', 'true']:
                    continue
                addr = fields.get('address')
                iface = fields.get('interface') or fields.get('actual-interface')
                if addr and iface and not addr.lower().startswith('fe80'):
                    if not any(ip['address'] == addr and ip['interface'] == iface for ip in data['ips']):
                        data['ips'].append({
                            'interface': iface,
                            'address': addr
                        })

        # Fallback regex para IPv6 em export (ignorando se tiver disabled=yes)
        ipv6_exports = re.findall(r'/ipv6 address add\s+(?:.*?\bdisabled=(yes|no)\b)?.*?address=([a-f0-9:\/]+).*?interface=(?:"([^"]*)"|([^\s]+))', export_out, re.IGNORECASE)
        for dis, addr, if1, if2 in ipv6_exports:
            if dis and dis.lower() == 'yes':
                continue
            iface = if1 or if2
            if addr and iface and not addr.lower().startswith('fe80'):
                if not any(ip['address'] == addr and ip['interface'] == iface for ip in data['ips']):
                    data['ips'].append({'interface': iface, 'address': addr})

        # 7.1 Garante que toda interface referenciada nos IPs exista em interfaces_l3 caso não seja física ou lag
        for ip_item in data['ips']:
            iface = ip_item['interface']
            if iface and not any(l['name'] == iface for l in data['interfaces_l3']) and not any(p['name'] == iface for p in data['interfaces_physical']):
                data['interfaces_l3'].append({'name': iface, 'vlan': None, 'type': 'virtual'})

        # 8. VRRP Groups (/interface vrrp print terse)
        vrrp_out = get_output_for("/interface vrrp") or export_out
        for line in vrrp_out.splitlines():
            if 'vrid=' in line or 'vrrp-id=' in line or '/interface vrrp add' in line:
                fields = self._parse_terse_line(line)
                vrid = fields.get('vrid') or fields.get('vrrp-id')
                iface = fields.get('interface')
                vname = fields.get('name')
                prio = fields.get('priority', '100')
                if vname and not any(l['name'] == vname for l in data['interfaces_l3']):
                    data['interfaces_l3'].append({'name': vname, 'vlan': None, 'parent': iface, 'type': 'virtual'})
                if vrid and iface:
                    data['vrrp_groups'].append({
                        'interface': iface,
                        'address_family': 'ipv4',
                        'vr_id': int(vrid),
                        'virtual_ip': fields.get('version', ''),
                        'priority': int(prio) if prio.isdigit() else 100,
                        'version': fields.get('version', 'v3')
                    })

        # 9. VPLS Tunnels (/interface vpls print terse)
        vpls_out = get_output_for("/interface vpls") or export_out
        for line in vpls_out.splitlines():
            if 'name=' in line and ('remote-peer=' in line or 'vpls-id=' in line or '/interface vpls add' in line):
                fields = self._parse_terse_line(line)
                vname = fields.get('name')
                remote = fields.get('remote-peer')
                vpls_id = fields.get('vpls-id')
                parent_if = fields.get('interface')
                if vname:
                    if not any(l['name'] == vname for l in data['interfaces_l3']):
                        data['interfaces_l3'].append({'name': vname, 'vlan': None, 'parent': parent_if, 'type': 'virtual'})
                    data['vpls'].append({
                        'name': vname,
                        'vlan_id': None,
                        'is_qinq': False,
                        'customer_vlans': [],
                        'neighbors': [(remote, vpls_id)] if remote else [],
                        'interfaces': [parent_if] if parent_if else []
                    })

        # 10. LLDP Neighbors (/ip neighbor print terse)
        nbr_out = get_output_for("/ip neighbor print terse") or get_output_for("/ip neighbor")
        for line in nbr_out.splitlines():
            if 'interface=' in line and ('identity=' in line or 'system-description=' in line or 'system-caps-description=' in line or 'board=' in line or 'interface-name=' in line or 'discovered-by=' in line):
                fields = self._parse_terse_line(line)
                local_if = fields.get('interface')
                remote_dev = fields.get('identity') or fields.get('system-description') or fields.get('system-caps-description') or fields.get('board')
                remote_if = fields.get('interface-name') or fields.get('port-id')
                if local_if and (remote_dev or remote_if):
                    data['lldp_neighbors'].append({
                        'local_interface': local_if,
                        'remote_device': remote_dev or '',
                        'remote_interface': remote_if or ''
                    })

        # Tags detection
        if 'bgp' in full_text.lower():
            data['tags'].add('BGP')
        if 'ospf' in full_text.lower():
            data['tags'].add('OSPF')
        if 'mpls' in full_text.lower():
            data['tags'].add('MPLS')
        if data['vrrp_groups']:
            data['tags'].add('VRRP')

        return data

    def normalize_interface_type(self, if_name: str) -> str:
        """
        Mapeia nomes de interface do Mikrotik RouterOS para o padrão NetBox.
        """
        if not if_name:
            return '10gbase-x-sfpp'

        name_lower = if_name.lower()
        if 'bonding' in name_lower or 'bond' in name_lower:
            return 'lag'
        if 'bridge' in name_lower:
            return 'bridge'
        if any(k in name_lower for k in ['vlan', 'loopback', 'vpls', 'gre', 'eoip', 'sit', 'ipip', 'vrrp', 'wireguard', 'l2tp', 'pppoe']):
            return 'virtual'
        if 'qsfp28' in name_lower or '100g' in name_lower:
            return '100gbase-x-qsfp28'
        if 'sfp28' in name_lower or '25g' in name_lower:
            return '25gbase-x-sfp28'
        if 'qsfp' in name_lower or '40g' in name_lower:
            return '40gbase-x-qsfpp'
        if 'sfp-sfpplus' in name_lower or 'sfpplus' in name_lower or 'sfp+' in name_lower or '10g' in name_lower:
            return '10gbase-x-sfpp'
        if 'ether' in name_lower or 'ethernet' in name_lower:
            return '1000base-t'
        # No RouterOS, qualquer interface que não corresponda a porta física (ether/sfp/combo/bond) é virtual
        return 'virtual'

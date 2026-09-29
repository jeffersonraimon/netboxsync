"""
Driver Huawei VRP
=================
Driver para equipamentos Huawei com sistema operacional VRP (VRP5/VRP8):
roteadores (AR/NE), switches (S/CE) e BNG/BRAS.

Comandos coletados via SSH (`fetch_data`):
    - display current-configuration    -> configuracao completa (obrigatorio)
    - display version                  -> modelo e versao de software
    - display esn                      -> numero de serie do chassi
    - display device                   -> inventario de placas (fallback de modelo)
    - display lldp neighbor brief      -> vizinhos LLDP (descoberta de cabos)
    - display interface phy-option     -> transceivers opticos em Roteadores (AR/NE)
    - display transceiver              -> transceivers opticos em Switches (S/CE)

O `parse_data` tambem aceita diretamente a string/arquivo com a saida de
`display current-configuration` (com ou sem o cabecalho de `display version`).

Recursos extraidos para o schema padrao do NetBox Engine:
    - Hostname (`sysname`), modelo e numero de serie
    - Tags de protocolo (BGP, OSPF, OSPFv3, ISIS, LDP, RSVP, MPLS-L2VPN, VRRP, PPPoE)
    - VLANs (`vlan batch` e blocos `vlan N` com `description`)
    - Interfaces fisicas, subinterfaces, Vlanif, LoopBack, Tunnel e Virtual-*
    - LAGs (Eth-Trunk) e seus membros (`eth-trunk N` nas portas)
    - VLANs tagged/untagged (port link-type access/trunk/hybrid)
    - Enderecos IPv4 (mascara decimal convertida para CIDR) e IPv6
    - VRRP (`vrrp vrid N virtual-ip ...`)
    - L2VPN: VPWS (`mpls l2vc <peer> <vc-id>`) e VPLS (`vsi` + `l2 binding vsi`)
    - BNG/BRAS: `user-vlan <ini> <fim> [qinq <svlan>]` -> QinQ (S-VLAN/C-VLANs)
    - Vizinhos LLDP (quando coletados via SSH)
    - Transceivers opticos (Roteadores via `phy-option` [Status UP] e Switches via `transceiver`)

Limitacoes conhecidas (nao fazem parte do schema sincronizado):
    - Tuneis MPLS-TE (`interface Tunnel`) sao criados como interface virtual, mas o
      mapeamento de LSPs/atributos MPLS-TE nao e enviado ao NetBox.
"""

import re
import ipaddress
from typing import Dict, Any, Optional, List

from drivers.base import BaseDeviceDriver
from drivers.registry import register_driver
from utils.ssh_client import SSHClientSession


# Comandos padrao de coleta via SSH (chave interna -> comando VRP)
DEFAULT_COMMANDS = {
    'config': 'display current-configuration',
    'version': 'display version',
    'esn': 'display esn',
    'device': 'display device',
    'lldp': 'display lldp neighbor brief',
    'phy_option': 'display interface phy-option | no-more',
    'transceiver': 'display transceiver | no-more',
}

# Precisao usada ao esperar o prompt do VRP: <HOST>, <HOST> ou [~HOST]
VRP_PROMPT_REGEX = r'[\r\n](?:<[^<>\r\n]+>|\[~?[^\[\]\r\n]+\])[ \t]*$'

# Prefixos de interface que representam portas fisicas
_PHYSICAL_PREFIXES = (
    'gigabitethernet', 'xgigabitethernet', 'ethernet', 'meth',
    '10ge', '25ge', '40ge', '100ge', '200ge', '400ge',
)

# Prefixos de interface logica (virtual)
_VIRTUAL_PREFIXES = (
    'vlanif', 'loopback', 'tunnel', 'null', 'vbdif', 'nve',
    'virtual-template', 'virtual-ethernet',
)


# ---------------------------------------------------------------------------
# Helpers de parsing
# ---------------------------------------------------------------------------
def clean_config(text: str) -> str:
    """
    Normaliza a saida bruta de `display current-configuration`: remove CR, artefatos
    de paginacao e as linhas de eco do prompt (<HOST>comando / [~HOST]comando).
    """
    if not text:
        return ''
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if stripped.startswith('---- More ----') or stripped.startswith('---- more ----'):
            continue
        if re.match(r'^[<\[]~?[\w\.\-]+[>\]]\s*$', stripped):
            # linha de eco do prompt isolada (ex: <SW-CORE>)
            continue
        if re.match(r'^[<\[]~?[\w\.\-]+[>\]]\S', stripped) and not stripped.startswith(('#', '!')):
            # linha de eco do prompt com comando (ex: <SW-CORE>display current-configuration)
            continue
        lines.append(line)
    return '\n'.join(lines)


def first_match(text: str, pattern: str, flags: int = re.MULTILINE) -> Optional[str]:
    """Retorna o primeiro grupo capturado de `pattern` em `text` (ou None)."""
    if not text:
        return None
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else None


def clean_description(value: Optional[str]) -> str:
    """Remove espacos e aspas de uma `description` do VRP."""
    return (value or '').strip().strip('"').strip()


def parse_vlan_list(text: str) -> List[int]:
    """
    Converte listas de VLAN do VRP em lista de inteiros.
    Formatos aceitos: '2 to 4 8 to 9 11', '2-4,8', '20'.
    """
    if not text:
        return []
    normalized = re.sub(r'\s+to\s+', '-', text.strip(), flags=re.IGNORECASE)
    vlans: List[int] = []
    for token in re.split(r'[\s,]+', normalized):
        if not token:
            continue
        match = re.fullmatch(r'(\d+)(?:-(\d+))?', token)
        if not match:
            continue
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if end < start:
            start, end = end, start
        if end - start > 4096:  # protecao contra linhas malformadas
            continue
        vlans.extend(range(start, end + 1))
    return [v for v in vlans if 1 <= v <= 4094]


def ipv4_to_cidr(address: str, mask: Optional[str] = None) -> Optional[str]:
    """Converte 'IP mascara-decimal' do VRP em notacao CIDR ('IP/prefixo')."""
    try:
        value = f"{address}/{mask}" if mask else address
        return str(ipaddress.IPv4Interface(value))
    except Exception:
        return None


def ipv6_to_cidr(address: str, prefix: Optional[str] = None) -> Optional[str]:
    """Converte endereco IPv6 do VRP em notacao CIDR."""
    try:
        value = f"{address}/{prefix}" if prefix else address
        return str(ipaddress.IPv6Interface(value))
    except Exception:
        return None


def is_subinterface(if_name: str) -> bool:
    """Subinterfaces do VRP usam ponto no nome (ex: Eth-Trunk4.2, GE0/0/0.100)."""
    return '.' in if_name


def iface_base(if_name: str) -> str:
    """Remove o sufixo de subinterface ('.N') do nome da interface."""
    return if_name.split('.', 1)[0]


def vlanif_vid(if_name: str) -> Optional[int]:
    """Extrai o VID de uma interface Vlanif (ex: Vlanif310 -> 310)."""
    match = re.match(r'^vlanif\s*(\d+)$', if_name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def is_physical(if_name: str) -> bool:
    """Indica se o nome corresponde a uma porta fisica Huawei."""
    if is_subinterface(if_name):
        return False
    return iface_base(if_name).lower().startswith(_PHYSICAL_PREFIXES)


def is_virtual_l3(if_name: str) -> bool:
    """Indica se o nome corresponde a uma interface logica (Vlanif, LoopBack, etc.)."""
    return iface_base(if_name).lower().startswith(_VIRTUAL_PREFIXES) or is_subinterface(if_name)


@register_driver('huawei_vrp')
class HuaweiVRPDriver(BaseDeviceDriver):
    """
    Driver para equipamentos Huawei VRP (roteadores, switches e BNG/BRAS).
    """
    driver_name = "Huawei VRP Driver"
    driver_slug = "huawei_vrp"

    def fetch_data(self, host: str, username: str, password: str, port: int = 22, debug: bool = False, **kwargs) -> Dict[str, Any]:
        """
        Conecta via SSH e coleta os comandos de leitura do VRP.
        """
        commands = kwargs.get('commands') or DEFAULT_COMMANDS

        session = SSHClientSession(host=host, username=username, password=password, port=port, debug=debug)
        outputs: Dict[str, Any] = {}

        try:
            session.connect()
            # Desabilita a paginacao interativa ("---- More ----") do VRP
            session.send_command("screen-length 0 temporary", timeout=10)

            for key, command in commands.items():
                # 'display current-configuration' em chassi grande pode demorar
                cmd_timeout = 240 if 'current-configuration' in command else 90
                print(f"  [➔] Executando comando: {command}")
                buf = session.send_command(command, expect_regex=VRP_PROMPT_REGEX, timeout=cmd_timeout)
                if buf:
                    outputs[key] = buf
        finally:
            session.close()

        return outputs

    def parse_data(self, raw_outputs: Any) -> Dict[str, Any]:
        """
        Analisa as saidas do VRP e preenche o schema padrao consumido pelo NetBox Engine.
        Aceita tanto o dicionario devolvido por `fetch_data` quanto uma string com a
        configuracao (`display current-configuration`) lida de arquivo local.
        """
        config_raw = self._get_output(raw_outputs, 'config', 'display current-configuration', 'running-config', 'config_text')
        version_text = self._get_output(raw_outputs, 'version', 'display version', 'version_text')
        device_text = self._get_output(raw_outputs, 'device', 'display device', 'device_text')
        esn_text = self._get_output(raw_outputs, 'esn', 'display esn', 'esn_text')
        lldp_text = self._get_output(raw_outputs, 'lldp', 'display lldp neighbor brief', 'lldp_text')
        phy_option_text = self._get_output(raw_outputs, 'phy_option', 'display interface phy-option | no-more', 'phy_option_text')
        transceiver_text = self._get_output(raw_outputs, 'transceiver', 'display transceiver | no-more', 'transceiver_text')

        config_text = clean_config(config_raw)

        data: Dict[str, Any] = {
            'hostname': None,
            'serial': None,
            'model': None,
            'tags': set(),
            'vlans': {},                       # vid -> name
            'interfaces_physical': [],         # [{'name','description','enabled','mtu','speed'}]
            'interfaces_l3': [],               # [{'name','vlan','parent', ...}]
            'lags': [],                        # [{'name','description','members'}]
            'ips': [],                         # [{'interface','address'}]
            'vpws': [],                        # [{'name','pw_id','neighbor','interface','vlan_id'}]
            'vpls': [],                        # [{'name','vlan_id','is_qinq','customer_vlans','neighbors','interfaces'}]
            'interface_vlans': {},             # iface -> set(vlan_ids) tagged
            'interface_untagged_vlans': {},    # iface -> untagged_vlan_id
            'inventory_items': [],
            'vrrp_groups': [],
            'lldp_neighbors': [],
            'vlan_roles_map': {},
            'vpn_tunnels': []
        }

        if not config_text:
            return data

        vlan_roles_map = data['vlan_roles_map']

        def register_vlan(vid: int, name: Optional[str] = None) -> None:
            """Registra uma VLAN garantindo nome padrao quando nao informado."""
            if vid not in data['vlans']:
                data['vlans'][vid] = name or f"VLAN-{vid}"

        def add_iface_vlan(iface_name: str, vid: int) -> None:
            """Marca uma VLAN como tagged na interface (usada para terminais L3/VPWS/VPLS)."""
            data['interface_vlans'].setdefault(iface_name, set()).add(int(vid))

        used_names = set()

        def unique_name(candidate: str, iface_name: str) -> str:
            """Garante nomes unicos de L2VPN dentro do equipamento."""
            name = clean_description(candidate) or iface_name
            if name.lower() in used_names:
                name = f"{name} ({iface_name})"
            used_names.add(name.lower())
            return name

        # ------------------------------------------------------------------
        # 1. Hostname (sysname)
        # ------------------------------------------------------------------
        data['hostname'] = first_match(config_text, r'^\s*sysname\s+(\S+)') or \
            first_match(config_raw, r'^[<\[]~?([\w\.\-]+)[>\]]')

        # ------------------------------------------------------------------
        # 2. Modelo e numero de serie
        # ------------------------------------------------------------------
        search_sources = [device_text, version_text, config_text]
        for source in search_sources:
            model = first_match(source, r'^#+[ \t]*(.+?)[ \t]+version information:') or \
                first_match(source, r"^#+[ \t]*(.+?)'s Device status")
            if model:
                data['model'] = model
                break
        if not data['model']:
            data['model'] = first_match(version_text, r'HUAWEI\s+(\S.*?)\s+(?:Router|Switch|Routing Switch)\s+uptime',
                                        re.MULTILINE | re.IGNORECASE) or \
                first_match(version_text or config_text,
                            r'VRP \(R\) software, Version [\d.]+ \(([A-Za-z0-9\- ]+?)\s+V\d{3}R')

        data['serial'] = first_match(esn_text, r'ESN of slot\s+\d+\s*:\s*(\S+)', re.MULTILINE | re.IGNORECASE) or \
            first_match(esn_text, r'^\s*(?:Board\s+)?ESN\s*:\s*(\S+)', re.MULTILINE | re.IGNORECASE) or \
            first_match(device_text, r'Board\s+Serial\s+Number\s*:\s*(\S+)', re.MULTILINE | re.IGNORECASE)

        # ------------------------------------------------------------------
        # 3. Tags de protocolo
        # ------------------------------------------------------------------
        if re.search(r'^\s*ospfv3\s+\d+', config_text, re.MULTILINE):
            data['tags'].add('OSPFv3')
        if re.search(r'^\s*ospf\s+\d+', config_text, re.MULTILINE):
            data['tags'].add('OSPF')
        if re.search(r'^\s*isis\s+\d+', config_text, re.MULTILINE):
            data['tags'].add('ISIS')
        if re.search(r'^\s*bgp\s+\d+', config_text, re.MULTILINE):
            data['tags'].add('BGP')
        if re.search(r'^\s*mpls ldp\b', config_text, re.MULTILINE):
            data['tags'].add('LDP')
        if re.search(r'^\s*mpls rsvp-te\b', config_text, re.MULTILINE):
            data['tags'].add('RSVP')
        if re.search(r'^\s*mpls l2vc\b', config_text, re.MULTILINE) or \
           re.search(r'^\s*vsi\s+\S+', config_text, re.MULTILINE) or \
           re.search(r'^\s*l2 binding vsi\b', config_text, re.MULTILINE):
            data['tags'].add('MPLS-L2VPN')
        if re.search(r'^\s*vrrp vrid\s+\d+', config_text, re.MULTILINE):
            data['tags'].add('VRRP')
        if re.search(r'pppoe-server|^\s*bas\b', config_text, re.MULTILINE | re.IGNORECASE):
            data['tags'].add('PPPoE')

        # ------------------------------------------------------------------
        # 4. VLANs ('vlan batch' e blocos 'vlan N' com 'description')
        # ------------------------------------------------------------------
        for match in re.finditer(r'^\s*vlan batch\s+(.+)$', config_text, re.MULTILINE):
            for vid in parse_vlan_list(match.group(1)):
                register_vlan(vid)

        vlan_block_re = re.compile(
            r'^\s*vlan\s+(\d+)\s*\n(.*?)(?=^\s*(?:vlan\s+(?:\d+|batch)|#)|\Z)',
            re.MULTILINE | re.DOTALL)
        for match in vlan_block_re.finditer(config_text):
            vid = int(match.group(1))
            name = clean_description(first_match(match.group(2), r'^\s*description\s+(.+)$'))
            data['vlans'][vid] = name or data['vlans'].get(vid) or f"VLAN-{vid}"

        # ------------------------------------------------------------------
        # 5. Interfaces (fisicas, LAGs, L3 e subinterfaces)
        # ------------------------------------------------------------------
        interface_re = re.compile(
            r'^interface\s+(\S+)\s*\n(.*?)(?=^interface\s+\S+|^#\s*$|^return\s*$|\Z)',
            re.MULTILINE | re.DOTALL)

        lag_entries: Dict[str, Dict[str, Any]] = {}
        lag_members: Dict[str, List[str]] = {}
        vsi_bindings: Dict[str, Dict[str, Any]] = {}

        for match in interface_re.finditer(config_text):
            if_name = match.group(1).strip()
            body = match.group(2)
            body_lines = [line.strip() for line in body.splitlines() if line.strip()]

            if if_name.lower().startswith('null'):
                continue  # interface de descarte (NULL0) nao representa ativo

            desc = clean_description(first_match(body, r'^\s*description\s+(.+)$'))
            enabled = 'shutdown' not in body_lines
            mtu = first_match(body, r'^\s*mtu\s+(\d+)')
            common = {
                'name': if_name,
                'description': desc,
                'enabled': enabled,
                'mtu': int(mtu) if mtu else None,
            }

            # --- Enderecos IP (mascara decimal -> CIDR) ---
            for ip_match in re.finditer(r'^\s*ip address\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)', body, re.MULTILINE):
                cidr = ipv4_to_cidr(ip_match.group(1), ip_match.group(2))
                if cidr:
                    data['ips'].append({'interface': if_name, 'address': cidr})
            for ip6_match in re.finditer(r'^\s*ipv6 address\s+([0-9A-Fa-f:]{2,})\s*/\s*(\d{1,3})', body, re.MULTILINE):
                cidr = ipv6_to_cidr(ip6_match.group(1), ip6_match.group(2))
                if cidr:
                    data['ips'].append({'interface': if_name, 'address': cidr})
            for ip6_match in re.finditer(r'^\s*ipv6 address\s+([0-9A-Fa-f:]{2,})\s+(\d{1,3})\s*$', body, re.MULTILINE):
                cidr = ipv6_to_cidr(ip6_match.group(1), ip6_match.group(2))
                if cidr:
                    data['ips'].append({'interface': if_name, 'address': cidr})

            # --- VRRP ---
            for vrrp_match in re.finditer(r'^\s*vrrp vrid\s+(\d+)\s+virtual-ip\s+(\S+)', body, re.MULTILINE):
                vr_id = int(vrrp_match.group(1))
                virtual_ip = vrrp_match.group(2)
                priority = first_match(body, rf'^\s*vrrp vrid\s+{vr_id}\s+priority\s+(\d+)')
                version = first_match(body, rf'^\s*vrrp vrid\s+{vr_id}\s+version\s+(\d)')
                data['vrrp_groups'].append({
                    'interface': if_name,
                    'address_family': 'ipv6' if ':' in virtual_ip else 'ipv4',
                    'vr_id': vr_id,
                    'virtual_ip': virtual_ip,
                    'priority': int(priority) if priority else 100,
                    'version': f"v{version}" if version else 'v2',
                })

            # --- VLANs de acesso (access/trunk/hybrid) ---
            tagged_vlans, untagged_vlans = set(), set()
            pvid = first_match(body, r'^\s*port (?:default|hybrid pvid) vlan\s+(\d+)')
            if pvid:
                untagged_vlans.add(int(pvid))
            for vlan_match in re.finditer(r'^\s*port (?:default|trunk allow-pass|hybrid tagged|hybrid untagged) vlan\s+(.+)$', body, re.MULTILINE):
                line_vlans = set(parse_vlan_list(vlan_match.group(1)))
                if 'hybrid untagged' in vlan_match.group(0):
                    untagged_vlans.update(line_vlans)
                else:
                    tagged_vlans.update(line_vlans)
            tagged_vlans -= untagged_vlans

            # --- Classificacao da interface ---
            if is_subinterface(if_name) or is_virtual_l3(if_name):
                vid = vlanif_vid(if_name) or (parse_vlan_list(first_match(body, r'^\s*vlan-type dot1q\s+(.+)$') or '') or [None])[0]
                l3_entry = dict(common)
                l3_entry['vlan'] = str(vid) if vid else None
                l3_entry['parent'] = iface_base(if_name) if is_subinterface(if_name) else None
                data['interfaces_l3'].append(l3_entry)
                if vid:
                    register_vlan(vid)
                    add_iface_vlan(if_name, vid)
            elif iface_base(if_name).lower().startswith('eth-trunk'):
                lag_entry = dict(common)
                lag_entry['members'] = []
                lag_entries[if_name] = lag_entry
            elif is_physical(if_name):
                data['interfaces_physical'].append(common)
                member_of = first_match(body, r'^\s*eth-trunk\s+(\d+)\s*$')
                if member_of:
                    lag_members.setdefault(f"Eth-Trunk{member_of}", []).append(if_name)

            # --- Vinculo de porta a VLAN tagged/untagged ---
            if tagged_vlans:
                data['interface_vlans'].setdefault(if_name, set()).update(tagged_vlans)
                for vid in tagged_vlans:
                    register_vlan(vid)
            if untagged_vlans:
                data['interface_untagged_vlans'][if_name] = int(pvid) if pvid else sorted(untagged_vlans)[0]
                for vid in untagged_vlans:
                    register_vlan(vid)

            # --- VPWS: mpls l2vc <peer> <vc-id> ---
            for vc_match in re.finditer(r'^\s*mpls l2vc\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+)', body, re.MULTILINE):
                neighbor, vc_id = vc_match.group(1), int(vc_match.group(2))
                vid = vlanif_vid(if_name) or \
                    (parse_vlan_list(first_match(body, r'^\s*vlan-type dot1q\s+(.+)$') or '') or [None])[0]
                entry_name = unique_name(desc, if_name)
                if vid:
                    register_vlan(vid, f"VLAN-{vid}-{entry_name}")
                    vlan_roles_map[vid] = "VPWS-TUNEIS"
                    add_iface_vlan(if_name, vid)
                data['vpws'].append({
                    'name': entry_name,
                    'group': '',
                    'vpn': str(vc_id),
                    'pw_id': str(vc_id),
                    'neighbor': neighbor,
                    'interface': if_name,
                    'interfaces': [if_name],
                    'vlan_id': vid,
                    'description': desc,
                })

            # --- VPLS: l2 binding vsi <NOME> ---
            for vsi_match in re.finditer(r'^\s*l2 binding vsi\s+(\S+)', body, re.MULTILINE):
                vsi_name = vsi_match.group(1)
                vid = vlanif_vid(if_name)
                binding = vsi_bindings.setdefault(vsi_name, {'vlan_id': None, 'interfaces': []})
                if vid and not binding['vlan_id']:
                    binding['vlan_id'] = vid
                if if_name not in binding['interfaces']:
                    binding['interfaces'].append(if_name)

            # --- BNG/BRAS: user-vlan <ini> <fim> [qinq <svlan>] ---
            for uv_match in re.finditer(r'^\s*user-vlan\s+(\d+)\s+(\d+)(?:\s+qinq\s+(\d+))?', body, re.MULTILINE):
                first_vid, last_vid = int(uv_match.group(1)), int(uv_match.group(2))
                svlan = int(uv_match.group(3)) if uv_match.group(3) else None
                customer_vlans = list(range(min(first_vid, last_vid), max(first_vid, last_vid) + 1))
                entry_name = unique_name(desc, if_name)
                if svlan:
                    register_vlan(svlan, f"VLAN-{svlan}-{entry_name}")
                    vlan_roles_map[svlan] = "VPLS-TUNEIS"
                    add_iface_vlan(if_name, svlan)
                for cvid in customer_vlans:
                    register_vlan(cvid, f"VLAN-{cvid}-Customer-{entry_name}")
                    vlan_roles_map[cvid] = "VPLS-TUNEIS"
                data['vpls'].append({
                    'name': entry_name,
                    'group': '',
                    'vpn': str(svlan) if svlan else '',
                    'vlan_id': svlan,
                    'is_qinq': svlan is not None,
                    'customer_vlans': customer_vlans,
                    'neighbors': [],
                    'interfaces': [if_name],
                    'description': desc or 'BNG subscriber interface',
                })

        # ------------------------------------------------------------------
        # 6. LAGs (Eth-Trunk): blocos explicitos + membros + bases inferidas
        # ------------------------------------------------------------------
        for lag_name, members in lag_members.items():
            lag_entries.setdefault(lag_name, {'name': lag_name, 'description': '', 'enabled': True, 'mtu': None, 'members': []})
            for member in members:
                if member not in lag_entries[lag_name]['members']:
                    lag_entries[lag_name]['members'].append(member)
        for l3_entry in data['interfaces_l3']:
            parent = l3_entry.get('parent')
            if parent and parent.lower().startswith('eth-trunk'):
                lag_entries.setdefault(parent, {'name': parent, 'description': '', 'enabled': True, 'mtu': None, 'members': []})
        data['lags'] = list(lag_entries.values())

        # ------------------------------------------------------------------
        # 7. VPLS (blocos 'vsi <NOME> static' + pwsignal ldp)
        # ------------------------------------------------------------------
        vsi_re = re.compile(r'^vsi\s+(\S+)(?:\s+\S+)?\s*\n(.*?)(?=^vsi\s+\S+|^#\s*$|^return\s*$|\Z)',
                            re.MULTILINE | re.DOTALL)
        for match in vsi_re.finditer(config_text):
            vsi_name, body = match.group(1).strip(), match.group(2)
            vsi_id = first_match(body, r'^\s*vsi-id\s+(\d+)')
            peers = re.findall(r'^\s*peer\s+([\d\.]+)', body, re.MULTILINE)
            desc = clean_description(first_match(body, r'^\s*description\s+(.+)$'))
            binding = vsi_bindings.get(vsi_name, {})
            vid = binding.get('vlan_id')
            entry_name = vsi_name
            used_names.add(entry_name.lower())
            if vid:
                register_vlan(vid, f"VLAN-{vid}-{vsi_name}")
                vlan_roles_map[vid] = "VPLS-TUNEIS"
            data['vpls'].append({
                'name': entry_name,
                'group': '',
                'vpn': vsi_id or '',
                'vlan_id': vid,
                'is_qinq': False,
                'customer_vlans': [],
                'neighbors': [(peer, vsi_id) for peer in peers if vsi_id],
                'interfaces': binding.get('interfaces', []),
                'description': desc,
            })

        # ------------------------------------------------------------------
        # 8. Roles de VLAN para interfaces L3 (PTP) quando ainda nao classificadas
        # ------------------------------------------------------------------
        for l3_entry in data['interfaces_l3']:
            vid = vlanif_vid(l3_entry['name'])
            if vid and vid not in vlan_roles_map:
                vlan_roles_map[vid] = "PTP-EQUIPAMENTOS"

        # ------------------------------------------------------------------
        # 9. Vizinhos LLDP (display lldp neighbor brief)
        # ------------------------------------------------------------------
        if lldp_text:
            data['lldp_neighbors'] = self._parse_lldp_neighbors(lldp_text)

        # ------------------------------------------------------------------
        # 10. Inventory Items / Transceivers (Router & Switch)
        # ------------------------------------------------------------------
        if phy_option_text:
            data['inventory_items'].extend(self._parse_transceivers_router(phy_option_text))
        if transceiver_text:
            data['inventory_items'].extend(self._parse_transceivers_switch(transceiver_text))

        return data

    # ------------------------------------------------------------------
    # Utilitarios internos
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_transceivers_router(phy_option_text: str) -> List[Dict[str, str]]:
        """
        Analisa a saida de 'display interface phy-option' (Roteadores):
        Blocos iniciam com nome da interface (ex: 100GE0/3/0) e sao filtrados por
        'Port Physical Status  : UP'.

        Formato de saida do schema:
            Name: Transceiver:100GE0/3/0
            Manufacturer: YYYYYY
            Part Id: OPT-XXXXXX
        """
        items: List[Dict[str, str]] = []
        if not phy_option_text:
            return items

        # Divide a saida por blocos de interface (ex: 100GE0/3/0, GE0/1/0, XGigabitEthernet0/0/1)
        blocks = re.split(r'\n(?=[\w\/\-]+(?:\.\d+)?\s*\n\s*Port Physical Status)', phy_option_text.replace('\r\n', '\n'))
        for block in blocks:
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            if not lines:
                continue

            iface_name = lines[0].split()[0]
            status_match = re.search(r'Port Physical Status\s*:\s*(UP|DOWN)', block, re.IGNORECASE)
            if not status_match or status_match.group(1).upper() != 'UP':
                continue

            vendor_pn = first_match(block, r'The Vendor PN is\s+(\S+)', re.IGNORECASE)
            vendor_name = first_match(block, r'The Vendor Name is\s+(.+)', re.IGNORECASE)

            if vendor_pn or vendor_name:
                items.append({
                    'interface': iface_name,
                    'name': f"Transceiver:{iface_name}",
                    'manufacturer': vendor_name or '',
                    'part_id': vendor_pn or '',
                    'serial': ''
                })
        return items

    @staticmethod
    def _parse_transceivers_switch(transceiver_text: str) -> List[Dict[str, str]]:
        """
        Analisa a saida de 'display transceiver' (Switches):
        Blocos iniciam com 'XGigabitEthernet0/0/1 transceiver information:'.

        Formato de saida do schema:
            Name: Transceiver:XGigabitEthernet0/0/1
            Manufacturer: YYYY
            Part Id: XXXXXXXXX
            Serial Number: ZZZZZZZZZZ
        """
        items: List[Dict[str, str]] = []
        if not transceiver_text:
            return items

        # Divide por blocos de cada interface
        blocks = re.split(r'\n(?=[\w\/\-]+\s+transceiver information:)', transceiver_text.replace('\r\n', '\n'))
        for block in blocks:
            header_match = re.search(r'^([\w\/\-]+)\s+transceiver information:', block, re.MULTILINE | re.IGNORECASE)
            if not header_match:
                continue
            iface_name = header_match.group(1).strip()

            vendor_name = first_match(block, r'Vendor Name\s*:\s*(.+)')
            part_number = first_match(block, r'Vendor Part Number\s*:\s*(.+)')
            serial_number = first_match(block, r'Manu\.\s*Serial Number\s*:\s*(.+)')

            if vendor_name or part_number or serial_number:
                items.append({
                    'interface': iface_name,
                    'name': f"Transceiver:{iface_name}",
                    'manufacturer': vendor_name or '',
                    'part_id': part_number or '',
                    'serial': serial_number or ''
                })
        return items
    @staticmethod
    def _get_output(raw_outputs: Any, *keys: str) -> str:
        """
        Recupera a saida de um comando aceitando dict de comandos, dict simplificado
        ou string unica (arquivo local com a configuracao).
        """
        if raw_outputs is None:
            return ''
        if isinstance(raw_outputs, str):
            # String unica representa a configuracao completa; demais comandos ficam vazios
            return raw_outputs if keys and keys[0] == 'config' else ''
        if isinstance(raw_outputs, dict):
            for key in keys:
                value = raw_outputs.get(key)
                if value and isinstance(value, str):
                    return value
            lowered = {str(k).lower(): v for k, v in raw_outputs.items()}
            for key in keys:
                value = lowered.get(key.lower())
                if value and isinstance(value, str):
                    return value
        return ''

    @staticmethod
    def _parse_lldp_neighbors(lldp_text: str) -> List[Dict[str, str]]:
        """
        Analisa a saida tabular de 'display lldp neighbor brief':
            Local Intf            Neighbor Dev        Neighbor Intf        Exptime(s)
            XGigabitEthernet0/0/1 SW-OURO-BRANCO      XGigabitEthernet0/0/5 97
        """
        neighbors: List[Dict[str, str]] = []
        for line in lldp_text.replace('\r\n', '\n').splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('-'):
                continue
            lowered = stripped.lower()
            if 'local intf' in lowered or 'neighbor dev' in lowered or 'exptime' in lowered:
                continue
            parts = [part.strip() for part in re.split(r'\s{2,}', stripped) if part.strip()]
            if len(parts) < 3:
                continue
            local, remote_dev, remote_if = parts[0], parts[1], parts[2]
            if not remote_dev or remote_dev.isdigit():
                continue
            neighbors.append({
                'local_interface': local,
                'remote_device': remote_dev,
                'remote_interface': remote_if,
            })
        return neighbors

    def normalize_interface_type(self, if_name: str) -> str:
        """
        Mapeia nomes de interface Huawei VRP para os tipos aceitos pelo NetBox.
        """
        if not if_name:
            return '10gbase-x-sfpp'

        name = if_name.strip()
        base = iface_base(name).lower()

        # Subinterfaces (ex: Eth-Trunk4.2) sao logicas
        if is_subinterface(name):
            return 'virtual'
        if base.startswith('eth-trunk') or base.startswith('trunk'):
            return 'lag'
        if base.startswith(_VIRTUAL_PREFIXES):
            return 'virtual'
        if base.startswith('100ge') or base.startswith('100g'):
            return '100gbase-x-qsfp28'
        if base.startswith('40ge') or base.startswith('40g'):
            return '40gbase-x-qsfpp'
        if base.startswith('25ge') or base.startswith('25g'):
            return '25gbase-x-sfp28'
        if base.startswith('10ge') or base.startswith('xgigabit'):
            return '10gbase-x-sfpp'
        if base.startswith(('gigabitethernet', 'meth', 'ethernet')):
            return '1000base-t'

        return super().normalize_interface_type(if_name)

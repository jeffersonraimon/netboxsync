"""
Testes unitários para a Arquitetura de Drivers Multimarcas (Multi-Vendor Driver Architecture)
"""

import pytest
from drivers import get_driver, list_drivers, BaseDeviceDriver
from drivers.datacom_dmos import DatacomDmOSDriver
from drivers.huawei_vrp import HuaweiVRPDriver
from drivers.template_driver import TemplateVendorDriver
from parsers.dmos_parser import parse_dmos_data


SAMPLE_DMOS_CONFIG = """
hostname DM4380-TEST-01

dot1q
 vlan 10
  name MANAGEMENT
  interface ten-gigabit-ethernet-1/1/1
 vlan 20
  name USERS
  interface ten-gigabit-ethernet-1/1/2

interface ten-gigabit-ethernet-1/1/1
 description UPLINK-TO-CORE
 mtu 9000
 ipv4 address 10.0.0.1/30

interface ten-gigabit-ethernet-1/1/2
 description ACCESS-PORT
 shutdown

interface lag 1
 description AGGREGATE-LINK
 interface ten-gigabit-ethernet-1/1/3

interface l3-VLAN10
 lower-layer-if vlan 10
 ipv4 address 192.168.10.1/24
"""


def test_driver_registry():
    drivers = list_drivers()
    assert 'dmos' in drivers
    assert drivers['dmos'] == DatacomDmOSDriver

    driver_inst = get_driver('dmos')
    assert isinstance(driver_inst, DatacomDmOSDriver)
    assert driver_inst.driver_slug == 'dmos'
    assert driver_inst.driver_name == "Datacom DmOS Driver"


def test_invalid_driver_name():
    with pytest.raises(ValueError) as exc_info:
        get_driver('non_existent_driver')
    assert "não foi encontrado no registro" in str(exc_info.value)


def test_dmos_driver_interface_normalization():
    driver = get_driver('dmos')
    assert driver.normalize_interface_type('hundred-gigabit-ethernet-1/1/1') == '100gbase-x-qsfp28'
    assert driver.normalize_interface_type('twenty-five-gigabit-ethernet-1/1/1') == '25gbase-x-sfp28'
    assert driver.normalize_interface_type('forty-gigabit-ethernet-1/1/1') == '40gbase-x-qsfpp'
    assert driver.normalize_interface_type('ten-gigabit-ethernet-1/1/1') == '10gbase-x-sfpp'
    assert driver.normalize_interface_type('gigabit-ethernet-1/1/1') == '1000base-t'
    assert driver.normalize_interface_type('lag-1') == 'lag'
    assert driver.normalize_interface_type('l3-VLAN10') == 'virtual'
    assert driver.normalize_interface_type('loopback-0') == 'virtual'


def test_dmos_driver_parse_data():
    driver = get_driver('dmos')
    parsed = driver.parse_data(SAMPLE_DMOS_CONFIG)

    # Verifica contrato de schema de dados
    required_keys = [
        'hostname', 'serial', 'model', 'tags', 'vlans', 'interfaces_physical',
        'interfaces_l3', 'lags', 'ips', 'vpws', 'vpls', 'interface_vlans',
        'inventory_items', 'vrrp_groups', 'lldp_neighbors', 'vlan_roles_map'
    ]
    for key in required_keys:
        assert key in parsed, f"Chave obrigatória '{key}' ausente no payload parseado."

    assert parsed['hostname'] == 'DM4380-TEST-01'
    assert 10 in parsed['vlans']
    assert parsed['vlans'][10] == 'MANAGEMENT'
    assert 20 in parsed['vlans']
    assert parsed['vlans'][20] == 'USERS'

    phys_names = [iface['name'] for iface in parsed['interfaces_physical']]
    assert 'ten-gigabit-ethernet-1/1/1' in phys_names
    assert 'ten-gigabit-ethernet-1/1/2' in phys_names

    l3_names = [iface['name'] for iface in parsed['interfaces_l3']]
    assert 'l3-VLAN10' in l3_names

    ip_addresses = [ip['address'] for ip in parsed['ips']]
    assert '10.0.0.1/30' in ip_addresses
    assert '192.168.10.1/24' in ip_addresses


def test_legacy_parse_dmos_data_compatibility():
    parsed = parse_dmos_data(SAMPLE_DMOS_CONFIG)
    assert parsed['hostname'] == 'DM4380-TEST-01'
    assert 10 in parsed['vlans']


def test_dmos_untagged_and_switchport_parsing():
    dmos_cfg = """
hostname OLT-TEST

dot1q
 vlan 4000
  name MGMT-LOCAL
  interface gigabit-ethernet-1/1/1
    untagged
  !
  interface ten-gigabit-ethernet-1/1/1
  !
 !
!
switchport
 interface gigabit-ethernet-1/1/1
  native-vlan
   vlan-id 4000
  !
 !
!
"""
    driver = get_driver('dmos')
    parsed = driver.parse_data(dmos_cfg)

    assert parsed['hostname'] == 'OLT-TEST'
    assert 4000 in parsed['vlans']
    assert parsed['vlans'][4000] == 'MGMT-LOCAL'

    # gigabit-ethernet-1/1/1 tem a VLAN 4000 como UNTAGGED e NAO como tagged
    assert parsed['interface_untagged_vlans'].get('gigabit-ethernet-1/1/1') == 4000
    assert 4000 not in parsed['interface_vlans'].get('gigabit-ethernet-1/1/1', set())

    # ten-gigabit-ethernet-1/1/1 tem a VLAN 4000 como TAGGED
    assert 4000 in parsed['interface_vlans'].get('ten-gigabit-ethernet-1/1/1', set())
    assert parsed['interface_untagged_vlans'].get('ten-gigabit-ethernet-1/1/1') is None



SAMPLE_ROUTEROS_TERSE = """
# /system identity print terse
 0 name="MK-ROUTER-01"

# /system routerboard print terse
 routerboard=yes model="CCR1036-8G-2S+" serial-number="123456789ABC"

# /interface vlan print terse
 0 R name="vlan10-mgmt" mtu=1500 l2mtu=1580 mac-address=00:11:22:33:44:55 arp=enabled vlan-id=10 interface="ether1" use-service-tag=no
 1 R name="vlan20-users" mtu=1500 l2mtu=1580 mac-address=00:11:22:33:44:56 arp=enabled vlan-id=20 interface="ether2" use-service-tag=no

# /interface bridge port print terse
 0 interface="ether1" bridge="bridge1" pvid=10
 1 interface="ether2" bridge="bridge1" pvid=20

# /interface bonding print terse
 0 R name="bonding1" mtu=1500 mac-address=00:11:22:33:44:57 slaves="ether3,ether4" mode=802.3ad

# /ip address print terse
 0   address="192.168.88.1/24" network="192.168.88.0" interface="ether1" actual-interface="ether1"
 1   address="10.0.10.1/24" network="10.0.10.0" interface="vlan10-mgmt" actual-interface="vlan10-mgmt"

# /interface vrrp print terse
 0 R name="vrrp1" interface="vlan10-mgmt" vrid=5 priority=100 version="v3"

# /interface vpls print terse
 0 R name="vpls1" remote-peer="10.255.255.2" vpls-id="10:1" interface="ether1"

# /ip neighbor print terse
 0 interface="ether1" identity="MK-CORE-02" interface-name="ether5" mac-address="00:11:22:33:44:99"
"""


def test_routeros_driver_registry():
    drivers = list_drivers()
    assert 'routeros' in drivers

    driver_inst = get_driver('routeros')
    assert driver_inst.driver_slug == 'routeros'
    assert driver_inst.driver_name == "Mikrotik RouterOS"


def test_routeros_driver_interface_normalization():
    driver = get_driver('routeros')
    assert driver.normalize_interface_type('ether1') == '1000base-t'
    assert driver.normalize_interface_type('sfp-sfpplus1') == '10gbase-x-sfpp'
    assert driver.normalize_interface_type('bonding1') == 'lag'
    assert driver.normalize_interface_type('vlan10-mgmt') == 'virtual'


def test_routeros_driver_parse_data():
    driver = get_driver('routeros')
    parsed = driver.parse_data(SAMPLE_ROUTEROS_TERSE)

    required_keys = [
        'hostname', 'serial', 'model', 'tags', 'vlans', 'interfaces_physical',
        'interfaces_l3', 'lags', 'ips', 'vpws', 'vpls', 'interface_vlans',
        'inventory_items', 'vrrp_groups', 'lldp_neighbors', 'vlan_roles_map'
    ]
    for key in required_keys:
        assert key in parsed, f"Chave obrigatória '{key}' ausente no payload parseado."

    assert parsed['hostname'] == 'MK-ROUTER-01'
    assert parsed['model'] == 'CCR1036-8G-2S+'
    assert parsed['serial'] == '123456789ABC'

    assert 10 in parsed['vlans']
    assert parsed['vlans'][10] == 'vlan10-mgmt'
    assert 20 in parsed['vlans']
    assert parsed['vlans'][20] == 'vlan20-users'

    l3_names = [iface['name'] for iface in parsed['interfaces_l3']]
    assert 'vlan10-mgmt' in l3_names
    assert 'vlan20-users' in l3_names

    assert len(parsed['lags']) == 1
    assert parsed['lags'][0]['name'] == 'bonding1'
    assert set(parsed['lags'][0]['members']) == {'ether3', 'ether4'}

    ip_addresses = [ip['address'] for ip in parsed['ips']]
    assert '192.168.88.1/24' in ip_addresses
    assert '10.0.10.1/24' in ip_addresses

    assert len(parsed['vrrp_groups']) == 1
    assert parsed['vrrp_groups'][0]['vr_id'] == 5
    assert parsed['vrrp_groups'][0]['interface'] == 'vlan10-mgmt'

    assert len(parsed['vpls']) == 1
    assert parsed['vpls'][0]['name'] == 'vpls1'

    assert len(parsed['lldp_neighbors']) == 1
    assert parsed['lldp_neighbors'][0]['local_interface'] == 'ether1'
    assert parsed['lldp_neighbors'][0]['remote_device'] == 'MK-CORE-02'
    assert parsed['lldp_neighbors'][0]['remote_interface'] == 'ether5'


SAMPLE_ROUTEROS_WRAPPED = """
/interface pppoe-client add allow=chap,mschap1,mschap2 comment="PPPOE CLIENT" dis
abled=no interface=ether1 name=LINK-MAIN-PPPOE user=user_demo
/interface wireguard add comment="Port: 51820" listen-port=51820 mtu=1420 name=VP
N-SITE2SITE-WIREGUARD
/interface vlan add comment=VLAN-TESTES interface=ether5 name=250-TESTES vlan-id=
250
/ip address add address=192.0.2.1/30 comment=LABS-WAN interface=150-LABS network=192.0.2.0
/ip neighbor print terse
0 interface=100-GERENCIA address=192.0.2.10 address4=192.0.2.10 address6=2001:db8:100::5 
mac-address=00:11:22:33:44:55 identity=AP-DEVICE-01 platform=AP-MODEL version= unpack=none age=0s ipv6=yes interface-name=eth0 system-description=
AP-DEVICE-01 system-caps=bridge,wlan-ap system-caps-enabled=bridge,wlan-ap discovered-by=lldp
"""


def test_routeros_wrapped_lines_and_virtual_interfaces():
    driver = get_driver('routeros')
    parsed = driver.parse_data(SAMPLE_ROUTEROS_WRAPPED)

    # Verifica se a VLAN 250 quebrada em duas linhas foi lida corretamente
    assert 250 in parsed['vlans']
    assert parsed['vlans'][250] == '250-TESTES'

    # Verifica se as interfaces virtuais pppoe e wireguard foram extraídas
    l3_names = [i['name'] for i in parsed['interfaces_l3']]
    assert 'LINK-MAIN-PPPOE' in l3_names
    assert 'VPN-SITE2SITE-WIREGUARD' in l3_names

    # Verifica se o IP com interface quebrada 150-LABS foi reconstituído
    ip_addrs = [ip['address'] for ip in parsed['ips']]
    assert '192.0.2.1/30' in ip_addrs
    ip_obj = next(ip for ip in parsed['ips'] if ip['address'] == '192.0.2.1/30')
    assert ip_obj['interface'] == '150-LABS'

    # Verifica se o neighbor LLDP quebrado em múltiplas linhas foi capturado
    assert len(parsed['lldp_neighbors']) == 1
    nbr = parsed['lldp_neighbors'][0]
    assert nbr['local_interface'] == '100-GERENCIA'
    assert nbr['remote_device'] == 'AP-DEVICE-01'
    assert nbr['remote_interface'] == 'eth0'


def test_routeros_dynamic_ipv4_and_global_ipv6():
    driver = get_driver('routeros')
    raw_outputs = {
        "/system identity print": "0 name=\"MK-DYNAMIC-TEST\"",
        "/ip address export terse": "0 address=192.168.1.1/24 interface=ether1 network=192.168.1.0",
        "/ip address print terse where dynamic": "0 D address=198.51.100.25/32 network=198.51.100.1 interface=pppoe-out1 actual-interface=pppoe-out1",
        "/ipv6 address export terse": "",
        "/ipv6 address print terse where global": "0 DG address=2001:db8:1000::1/64 interface=ether1 actual-interface=ether1 eui-64=no advertised=yes dynamic=yes global=yes"
    }

    parsed = driver.parse_data(raw_outputs)
    ip_map = {ip['address']: ip['interface'] for ip in parsed['ips']}

    assert '192.168.1.1/24' in ip_map
    assert ip_map['192.168.1.1/24'] == 'ether1'

    assert '198.51.100.25/32' in ip_map
    assert ip_map['198.51.100.25/32'] == 'pppoe-out1'

    assert '2001:db8:1000::1/64' in ip_map
    assert ip_map['2001:db8:1000::1/64'] == 'ether1'


def test_routeros_vpn_tunnels_parsing():
    driver = get_driver('routeros')
    raw_outputs = {
        "/system identity print": "0 name=\"MK-EDGE-ROUTER-01\"",
        "/interface wireguard peers export terse": """/interface wireguard peers add allowed-address=192.0.2.10/32 comment=MOBILE-CLIENT interface=WIREGUARD name=MOBILE-CLIENT public-key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" responder=yes
/interface wireguard peers add allowed-address=192.0.2.30/29 comment=PEER-BRANCH-01 endpoint-address=198.51.100.2 endpoint-port=51820 interface=VPN-BRANCH-WIREGUARD name=PEER-BRANCH-01 public-key="BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
/interface wireguard peers add allowed-address=192.0.2.50/24 interface=WIREGUARD name=PEER-REMOTE-SITE public-key="CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCAAAAAAAAAAA="
""",
        "/interface l2tp-server export terse": """/interface l2tp-server add name=VPN-L2TP-USER-01 user=demo_vpn_user
/interface l2tp-server add name=VPN-L2TP-USER-02 user=demo_mobile_user
"""
    }

    parsed = driver.parse_data(raw_outputs)
    assert 'vpn_tunnels' in parsed
    vpn_map = {t['name']: t for t in parsed['vpn_tunnels']}

    # Verifica WireGuard Tunnels (Túnis em /vpn/tunnels/)
    assert 'MOBILE-CLIENT' in vpn_map
    assert vpn_map['MOBILE-CLIENT']['encapsulation'] == 'wireguard'
    assert vpn_map['MOBILE-CLIENT']['interface'] == 'WIREGUARD'
    assert vpn_map['MOBILE-CLIENT']['description'] == 'MOBILE-CLIENT'

    assert 'PEER-BRANCH-01' in vpn_map
    assert vpn_map['PEER-BRANCH-01']['encapsulation'] == 'wireguard'
    assert vpn_map['PEER-BRANCH-01']['interface'] == 'VPN-BRANCH-WIREGUARD'

    assert 'PEER-REMOTE-SITE' in vpn_map
    assert vpn_map['PEER-REMOTE-SITE']['encapsulation'] == 'wireguard'

    # Verifica L2TP Tunnels
    assert 'VPN-L2TP-USER-01' in vpn_map
    assert vpn_map['VPN-L2TP-USER-01']['encapsulation'] == 'l2tp'
    assert vpn_map['VPN-L2TP-USER-01']['description'] == 'User: demo_vpn_user'

    assert 'VPN-L2TP-USER-02' in vpn_map
    assert vpn_map['VPN-L2TP-USER-02']['encapsulation'] == 'l2tp'
    assert vpn_map['VPN-L2TP-USER-02']['description'] == 'User: demo_mobile_user'


# ============================================================================
# Huawei VRP (Switch / Roteador / BNG-BRAS)
# ============================================================================

SAMPLE_HUAWEI_SWITCH = """
!Software Version V200R022C00SPC500
#
sysname SW-CORE-TEST
#
vlan batch 10 20 30 to 32
#
vlan 10
 description GERENCIA
vlan 20
 description USUARIOS
vlan 30
 description MPLS-CLIENTE-TESTE
#
interface Vlanif10
 description GERENCIA
 ip address 192.168.10.2 255.255.255.0
#
interface Vlanif20
 description VRRP-TESTE
 ip address 192.168.20.2 255.255.255.0
 vrrp vrid 20 virtual-ip 192.168.20.1
 vrrp vrid 20 priority 200
#
interface Vlanif30
 description MPLS-CLIENTE-TESTE
 mtu 9198
 mpls l2vc 10.70.70.10 2402 no-control-word
#
interface Vlanif31
 description MPLS-VPLS-TESTE
 l2 binding vsi VLAN31
#
interface XGigabitEthernet0/0/1
 description UPLINK-CORE
 eth-trunk 1
#
interface XGigabitEthernet0/0/2
 description UPLINK-CORE
 eth-trunk 1
#
interface XGigabitEthernet0/0/3
 description SERVIDOR-HYBRID
 port link-type hybrid
 port hybrid pvid vlan 20
 undo port hybrid vlan 1
 port hybrid tagged vlan 30 to 32
 port hybrid untagged vlan 20
#
interface XGigabitEthernet0/0/4
 description ACESSO
 port link-type access
 port default vlan 10
#
interface XGigabitEthernet0/0/5
 description TRUNK
 port link-type trunk
 undo port trunk allow-pass vlan 1
 port trunk allow-pass vlan 10 20
#
interface Eth-Trunk1
 description AGREGACAO-CORE
 mode lacp
#
interface LoopBack0
 description router-id
 ip address 10.70.70.24 255.255.255.255
 ipv6 address 2804:5820:0:2FFF::1/128
#
vsi VLAN31 static
 pwsignal ldp
  vsi-id 31
  flow-label both
  peer 10.70.70.4
 mtu 9198
#
return
"""


SAMPLE_HUAWEI_BRAS = """
# Huawei Versatile Routing Platform Software
# VRP (R) software, Version 8.230 (NetEngine 8000 V800R023C00SPC500)
# NetEngine 8000 M8 version information:
#
sysname BRAS-TESTE
#
interface Eth-Trunk4
 description COM-SW-CORE
 mode lacp-static
#
interface Eth-Trunk4.300
 description OLT-TESTE
 user-vlan 3301 3304 qinq 73
 pppoe-server bind Virtual-Template 1
#
interface 100GE0/6/1
 description UPLINK
 eth-trunk 4
#
interface LoopBack0
 description router-id
 ip address 10.255.254.2 255.255.255.255
#
return
"""


def test_huawei_driver_registry():
    drivers = list_drivers()
    assert 'huawei_vrp' in drivers
    assert drivers['huawei_vrp'] == HuaweiVRPDriver

    driver_inst = get_driver('huawei_vrp')
    assert isinstance(driver_inst, HuaweiVRPDriver)
    assert driver_inst.driver_slug == 'huawei_vrp'
    assert driver_inst.driver_name == "Huawei VRP Driver"


def test_huawei_driver_interface_normalization():
    driver = get_driver('huawei_vrp')
    assert driver.normalize_interface_type('Eth-Trunk1') == 'lag'
    assert driver.normalize_interface_type('XGigabitEthernet0/0/1') == '10gbase-x-sfpp'
    assert driver.normalize_interface_type('10GE0/0/1') == '10gbase-x-sfpp'
    assert driver.normalize_interface_type('GigabitEthernet0/0/1') == '1000base-t'
    assert driver.normalize_interface_type('MEth0/0/1') == '1000base-t'
    assert driver.normalize_interface_type('25GE0/0/1') == '25gbase-x-sfp28'
    assert driver.normalize_interface_type('40GE0/0/1') == '40gbase-x-qsfpp'
    assert driver.normalize_interface_type('100GE0/0/1') == '100gbase-x-qsfp28'
    assert driver.normalize_interface_type('Vlanif10') == 'virtual'
    assert driver.normalize_interface_type('LoopBack0') == 'virtual'
    assert driver.normalize_interface_type('Virtual-Template1') == 'virtual'
    assert driver.normalize_interface_type('Eth-Trunk4.300') == 'virtual'


def test_huawei_driver_parse_switch():
    driver = get_driver('huawei_vrp')
    parsed = driver.parse_data(SAMPLE_HUAWEI_SWITCH)

    required_keys = [
        'hostname', 'serial', 'model', 'tags', 'vlans', 'interfaces_physical',
        'interfaces_l3', 'lags', 'ips', 'vpws', 'vpls', 'interface_vlans',
        'inventory_items', 'vrrp_groups', 'lldp_neighbors', 'vlan_roles_map',
        'vpn_tunnels'
    ]
    for key in required_keys:
        assert key in parsed, f"Chave obrigatória '{key}' ausente no payload parseado."

    assert parsed['hostname'] == 'SW-CORE-TEST'
    assert 'MPLS-L2VPN' in parsed['tags']
    assert 'VRRP' in parsed['tags']

    # VLANs: 'vlan batch' + blocos 'vlan N' com 'description'
    assert parsed['vlans'][10] == 'GERENCIA'
    assert parsed['vlans'][20] == 'USUARIOS'
    assert parsed['vlans'][30] == 'MPLS-CLIENTE-TESTE'
    assert 32 in parsed['vlans'], "Faixa 'vlan batch 30 to 32' deve incluir a VLAN 32"

    # Enderecos IPv4 (mascara decimal -> CIDR) e IPv6 (normalizado para minusculas)
    ips = {(ip['interface'], ip['address']) for ip in parsed['ips']}
    assert ('Vlanif10', '192.168.10.2/24') in ips
    assert ('LoopBack0', '10.70.70.24/32') in ips
    assert ('LoopBack0', '2804:5820:0:2fff::1/128') in ips

    # LAG (Eth-Trunk) e membros declarados nas portas fisicas
    lags = {lag['name']: lag for lag in parsed['lags']}
    assert 'Eth-Trunk1' in lags
    assert set(lags['Eth-Trunk1']['members']) == {'XGigabitEthernet0/0/1', 'XGigabitEthernet0/0/2'}

    # VLANs tagged/untagged por tipo de porta
    assert parsed['interface_untagged_vlans']['XGigabitEthernet0/0/4'] == 10
    assert parsed['interface_vlans']['XGigabitEthernet0/0/5'] == {10, 20}
    assert parsed['interface_vlans']['XGigabitEthernet0/0/3'] == {30, 31, 32}
    assert parsed['interface_untagged_vlans']['XGigabitEthernet0/0/3'] == 20

    # Interfaces L3 (Vlanif) com vinculo de VLAN
    l3_names = [iface['name'] for iface in parsed['interfaces_l3']]
    assert 'Vlanif10' in l3_names

    # VRRP
    assert len(parsed['vrrp_groups']) == 1
    vrrp = parsed['vrrp_groups'][0]
    assert vrrp['interface'] == 'Vlanif20'
    assert vrrp['vr_id'] == 20
    assert vrrp['virtual_ip'] == '192.168.20.1'
    assert vrrp['priority'] == 200

    # VPWS (mpls l2vc) e VPLS (vsi + l2 binding vsi)
    assert len(parsed['vpws']) == 1
    vpws = parsed['vpws'][0]
    assert vpws['pw_id'] == '2402'
    assert vpws['neighbor'] == '10.70.70.10'
    assert vpws['interface'] == 'Vlanif30'
    assert vpws['vlan_id'] == 30
    assert parsed['vlan_roles_map'][30] == 'VPWS-TUNEIS'

    assert len(parsed['vpls']) == 1
    vpls = parsed['vpls'][0]
    assert vpls['name'] == 'VLAN31'
    assert vpls['vlan_id'] == 31
    assert vpls['neighbors'] == [('10.70.70.4', '31')]
    assert vpls['interfaces'] == ['Vlanif31']


def test_huawei_driver_parse_bras_qinq_and_esn():
    driver = get_driver('huawei_vrp')

    # Serial vindo de 'display esn' e modelo do cabecalho de 'display version/device'
    raw_outputs = {
        'config': SAMPLE_HUAWEI_BRAS,
        'esn': 'ESN of slot 1: 2102351ABC',
    }
    parsed = driver.parse_data(raw_outputs)

    assert parsed['hostname'] == 'BRAS-TESTE'
    assert parsed['model'] == 'NetEngine 8000 M8'
    assert parsed['serial'] == '2102351ABC'
    assert 'PPPoE' in parsed['tags']

    # LAG do BNG com o membro fisico (100GE0/6/1 -> eth-trunk 4)
    lags = {lag['name']: lag for lag in parsed['lags']}
    assert lags['Eth-Trunk4']['members'] == ['100GE0/6/1']

    # Subinterface de assinante com QinQ: user-vlan <C-VLANs> qinq <S-VLAN>
    vpls = {item['name']: item for item in parsed['vpls']}
    assert 'OLT-TESTE' in vpls
    assert vpls['OLT-TESTE']['vlan_id'] == 73
    assert vpls['OLT-TESTE']['is_qinq'] is True
    assert vpls['OLT-TESTE']['customer_vlans'] == [3301, 3302, 3303, 3304]
    assert parsed['vlan_roles_map'][73] == 'VPLS-TUNEIS'
    assert parsed['vlan_roles_map'][3301] == 'VPLS-TUNEIS'
    assert 3301 in parsed['vlans']


def test_huawei_driver_parse_dict_outputs_and_empty_input():
    driver = get_driver('huawei_vrp')

    # Dict com chaves de fetch_data (nome do comando VRP)
    parsed = driver.parse_data({'display current-configuration': SAMPLE_HUAWEI_SWITCH})
    assert parsed['hostname'] == 'SW-CORE-TEST'

    # Entrada vazia nao deve quebrar e deve devolver o schema completo
    empty = driver.parse_data('')
    assert empty['hostname'] is None
    assert empty['lags'] == [] and empty['vpws'] == [] and empty['vpls'] == []








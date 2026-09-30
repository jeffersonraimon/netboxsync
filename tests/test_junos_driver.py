"""
Testes unitários para o Driver Juniper JunOS usando dados sintéticos e genéricos.
"""

import pytest
from drivers.juniper_junos import JuniperJunosDriver
from drivers import get_driver


def test_junos_driver_registration():
    driver = get_driver('junos')
    assert isinstance(driver, JuniperJunosDriver)
    
    driver_juniper = get_driver('juniper')
    assert isinstance(driver_juniper, JuniperJunosDriver)


def test_junos_parser_generic_data():
    raw_config = """
set version 20.4R3.8
set system host-name TEST-ROUTER-01
set vlans VLAN-MGMT-100 vlan-id 100
set vlans VLAN-DATA-200 vlan-id 200

set chassis aggregated-devices ethernet device-count 2

set interfaces ge-0/0/0 description "Uplink Core"
set interfaces ge-0/0/0 mtu 9000
set interfaces ge-0/0/0 gigether-options 802.3ad ae0

set interfaces ge-0/0/1 description "Access Port"
set interfaces ge-0/0/1 unit 0 family ethernet-switching interface-mode access
set interfaces ge-0/0/1 unit 0 family ethernet-switching vlan members VLAN-MGMT-100

set interfaces xe-0/1/0 description "Trunk Port"
set interfaces xe-0/1/0 unit 0 family ethernet-switching interface-mode trunk
set interfaces xe-0/1/0 unit 0 family ethernet-switching vlan members VLAN-DATA-200

set interfaces ae0 description "LAG to Core"
set interfaces ae0 unit 0 family ethernet-switching interface-mode trunk
set interfaces ae0 unit 0 family ethernet-switching vlan members VLAN-MGMT-100

set interfaces irb unit 100 family inet address 10.100.0.1/24
set interfaces irb unit 200 family inet address 10.200.0.1/24
set interfaces lo0 unit 0 family inet address 192.168.255.1/32
"""

    raw_version = """
Hostname: TEST-ROUTER-01
Model: mx240
Junos: 20.4R3.8
"""

    raw_chassis = """
Hardware inventory:
Item             Version  Part number  Serial number     Description
Chassis                                JN1234567890      MX240
     Xcvr 0       REV 01   740-012345   SFP11223344       SFP-10G-SR
     Xcvr 1       REV 01   740-054321   SFP55667788       QSFP-100G-LR4
"""

    raw_lldp = """{
    "lldp-neighbors-information" : [
    {
        "lldp-neighbor-information" : [
        {
            "lldp-local-port-id" : [ { "data" : "xe-0/1/0" } ],
            "lldp-remote-system-name" : [ { "data" : "REMOTE-SW-02" } ],
            "lldp-remote-port-description" : [ { "data" : "Ethernet1/1" } ]
        }
        ]
    }
    ]
}"""

    raw_pic_optics = """
FPC slot 0, PIC slot 1 information:
  Type                             MIC1
  State                            Online

PIC port information:
                         Fiber                    Xcvr vendor       Wave-    Xcvr         JNPR
  Port Cable type        type  Xcvr vendor        part number       length   Firmware     Rev
  0    100GBASE SR4 T2   MM    GENERIC_VENDOR_A   GEN-PART-100G     850 nm   0.0          REV 01
  1    100GBASE SR4 T2   MM    GENERIC_VENDOR_B   GEN-PART-200G     850 nm   0.0          REV 01
"""

    raw_outputs = {
        "config": raw_config,
        "version": raw_version,
        "chassis_hardware": raw_chassis,
        "pic_optics": raw_pic_optics,
        "lldp_json": raw_lldp
    }

    driver = JuniperJunosDriver()
    parsed = driver.parse_data(raw_outputs)

    # 1. System Info
    assert parsed['hostname'] == 'TEST-ROUTER-01'
    assert parsed['model'] == 'MX240'
    assert parsed['serial'] == 'JN1234567890'
    assert 'junos' in parsed['tags']

    # 2. VLANs
    assert parsed['vlans'][100] == 'VLAN-MGMT-100'
    assert parsed['vlans'][200] == 'VLAN-DATA-200'

    # 3. LAGs
    assert len(parsed['lags']) == 1
    assert parsed['lags'][0]['name'] == 'ae0'
    assert parsed['lags'][0]['description'] == 'LAG to Core'
    assert 'ge-0/0/0' in parsed['lags'][0]['members']

    # 4. Interfaces
    ifnames = [i['name'] for i in parsed['interfaces_physical']]
    assert 'ge-0/0/0' in ifnames
    assert 'ge-0/0/1' in ifnames
    assert 'xe-0/1/0' in ifnames
    assert 'ae0' in ifnames

    # 5. Interface VLANs
    assert 100 in parsed['interface_vlans']['ge-0/0/1']
    assert 200 in parsed['interface_vlans']['xe-0/1/0']
    assert 100 in parsed['interface_vlans']['ae0']

    # 6. L3 Interfaces & IPs
    l3_names = [l3['name'] for l3 in parsed['interfaces_l3']]
    assert 'irb.100' in l3_names
    assert 'irb.200' in l3_names

    ip_addrs = [ip['address'] for ip in parsed['ips']]
    assert '10.100.0.1/24' in ip_addrs
    assert '10.200.0.1/24' in ip_addrs
    assert '192.168.255.1/32' in ip_addrs

    # 7. Inventory Items (Transceivers enriquecidos com pic_optics sintéticos)
    assert len(parsed['inventory_items']) == 2
    assert parsed['inventory_items'][0]['serial'] == 'SFP11223344'
    assert parsed['inventory_items'][0]['manufacturer'] == 'GENERIC_VENDOR_A'
    assert parsed['inventory_items'][0]['part_id'] == 'GEN-PART-100G'
    assert parsed['inventory_items'][1]['serial'] == 'SFP55667788'
    assert parsed['inventory_items'][1]['manufacturer'] == 'GENERIC_VENDOR_B'
    assert parsed['inventory_items'][1]['part_id'] == 'GEN-PART-200G'

    # 8. LLDP Neighbors
    assert len(parsed['lldp_neighbors']) == 1
    assert parsed['lldp_neighbors'][0]['local_interface'] == 'xe-0/1/0'
    assert parsed['lldp_neighbors'][0]['remote_device'] == 'REMOTE-SW-02'
    assert parsed['lldp_neighbors'][0]['remote_interface'] == 'Ethernet1/1'

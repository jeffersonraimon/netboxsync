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

    # 5. Interface VLANs (Untagged & Tagged vinculadas à subinterface unit.0)
    assert parsed['interface_untagged_vlans']['ge-0/0/1.0'] == 100
    assert 200 in parsed['interface_vlans']['xe-0/1/0.0']
    assert 100 in parsed['interface_vlans']['ae0.0']

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


def test_junos_lldp_with_trailing_prompt():
    raw_lldp = """{
    "lldp-neighbors-information" : [
    {
        "lldp-neighbor-information" : [
        {
            "lldp-local-port-id" : [ { "data" : "xe-0/0/39" } ],
            "lldp-remote-system-name" : [ { "data" : "DM-4370-XXXXXX" } ],
            "lldp-remote-port-description" : [ { "data" : "ten-gigabit-ethernet-1/1/2" } ]
        }
        ]
    }
    ]
}

{master:0}
user@QFX-01-SAM>"""

    driver = JuniperJunosDriver()
    neighbors = driver._parse_lldp(raw_lldp)
    assert len(neighbors) == 1
    assert neighbors[0]['local_interface'] == 'xe-0/0/39'
    assert neighbors[0]['remote_device'] == 'DM-4370-XXXXXX'
    assert neighbors[0]['remote_interface'] == 'ten-gigabit-ethernet-1/1/2'


def test_junos_inventory_pic_slot_0():
    raw_chassis = """
Hardware inventory:
Item             Version  Part number  Serial number     Description
Chassis                                JN1234567890      QFX5100-48S-6Q
     Xcvr 49      REV 01   740-058732   30JXXXX15190     QSFP-100G-LR4
     Xcvr 50      REV 01   740-061405   INKAZ3XXX41      QSFP-100G-SR4
"""

    raw_optics_diag = """
Physical interface: et-0/0/49
Physical interface: et-0/0/50
"""

    raw_pic_optics_0 = """
FPC slot 0, PIC slot 0 information:
  Type                             48x10G-4x100G
  State                            Online

PIC port information:
                         Fiber                    Xcvr vendor       Wave-    Xcvr
  Port Cable type        type  Xcvr vendor        part number       length   Firmware
  49   100GBASE LR4      n/a   XXX                OPT8XXXX0D       1310 nm  0.0
  50   100GBASE SR4      n/a   XXXXXXXXXXX        PRE-QSFP28-SR4    850 nm   0.0
"""

    driver = JuniperJunosDriver()
    items = driver._parse_inventory(raw_chassis, raw_pic_optics_0, raw_optics_diag)

    assert len(items) == 2

    # Transceiver 49
    assert items[0]['interface'] == 'et-0/0/49'
    assert items[0]['manufacturer'] == 'XXX'
    assert items[0]['part_id'] == 'OPT8XXXX0D'
    assert items[0]['serial'] == '30JXXXX15190'

    # Transceiver 50
    assert items[1]['interface'] == 'et-0/0/50'
    assert items[1]['manufacturer'] == 'XXXXXXXXXXX'
    assert items[1]['part_id'] == 'PRE-QSFP28-SR4'
    assert items[1]['serial'] == 'INKAZ3XXX41'


def test_junos_logical_systems():
    raw_config = """
set system host-name RT-AAA-01
set logical-systems LS-TESTE interfaces ae4 unit 1103
set logical-systems LS-TESTE interfaces ae4 unit 1104
set logical-systems LS3 interfaces lt-0/0/0 unit 1
set logical-systems LS3 interfaces et-0/1/5 unit 3500
set interfaces ae4 unit 1103 description "TEST VOAFIBRA 1103"
set interfaces ae4 unit 1104 description "TEST VOAFIBRA 1104"
"""
    driver = JuniperJunosDriver()
    data = driver.parse_data({"config": raw_config})


    assert "logical_systems" in data
    assert "LS-TESTE" in data["logical_systems"]
    assert "ae4.1103" in data["logical_systems"]["LS-TESTE"]
    assert "ae4.1104" in data["logical_systems"]["LS-TESTE"]

    assert "LS3" in data["logical_systems"]
    assert "lt-0/0/0.1" in data["logical_systems"]["LS3"]
    assert "et-0/1/5.3500" in data["logical_systems"]["LS3"]

    # Verifica se a propriedade 'vdc' foi atribuída nas subinterfaces L3/physical
    ae4_1103 = next((i for i in data["interfaces_l3"] if i["name"] == "ae4.1103"), None)
    assert ae4_1103 is not None
    assert ae4_1103["vdc"] == "RT-AAA-01-LS-TESTE"

    et_3500 = next((i for i in data["interfaces_l3"] if i["name"] == "et-0/1/5.3500"), None)
    assert et_3500 is not None
    assert et_3500["vdc"] == "RT-AAA-01-LS3"






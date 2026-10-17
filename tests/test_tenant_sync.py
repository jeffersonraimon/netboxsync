"""
Testes unitários para a funcionalidade de Tenant (--tenant) no NetBox Sync.
"""

from unittest.mock import MagicMock, patch
import pytest
from netbox_sync.sync_engine import find_or_create_tenant, sync_to_netbox
import main


def test_find_or_create_tenant_existing():
    nb = MagicMock()
    existing_tenant = MagicMock(id=42, name="CLIENTE-X", slug="cliente-x")
    nb.tenancy.tenants.filter.return_value = [existing_tenant]

    tenant = find_or_create_tenant(nb, "CLIENTE-X")
    assert tenant == existing_tenant
    nb.tenancy.tenants.create.assert_not_called()


def test_find_or_create_tenant_creates_new():
    nb = MagicMock()
    # safe_get usa filter() internamente
    nb.tenancy.tenants.filter.return_value = []
    nb.tenancy.tenants.all.return_value = []
    created_tenant = MagicMock(id=99, name="CLIENTE-NOVO", slug="cliente-novo")
    nb.tenancy.tenants.create.return_value = created_tenant

    tenant = find_or_create_tenant(nb, "CLIENTE-NOVO")
    assert tenant == created_tenant
    nb.tenancy.tenants.create.assert_called_once_with(name="CLIENTE-NOVO", slug="cliente-novo")


def test_find_or_create_tenant_empty():
    nb = MagicMock()
    assert find_or_create_tenant(nb, None) is None
    assert find_or_create_tenant(nb, "") is None
    assert find_or_create_tenant(nb, "   ") is None


def test_main_cli_argument_tenant():
    parser = main.create_parser()
    args = parser.parse_args(["--host", "10.0.0.1", "--tenant", "CLIENTE-X"])
    assert args.tenant == "CLIENTE-X"
    assert args.host == "10.0.0.1"

    # Testa flag curta -t
    args_short = parser.parse_args(["--file", "config.txt", "-t", "CLIENTE-Y"])
    assert args_short.tenant == "CLIENTE-Y"
    assert args_short.file == "config.txt"


@patch("pynetbox.api")
def test_sync_to_netbox_applies_tenant(mock_pynetbox_api):
    mock_nb = MagicMock()
    mock_pynetbox_api.return_value = mock_nb

    # Tenant mock
    tenant_obj = MagicMock(id=10, name="CLIENTE-X", slug="cliente-x")
    mock_nb.tenancy.tenants.filter.return_value = [tenant_obj]

    # Site mock
    site_obj = MagicMock(id=1, name="MTSJ")
    mock_nb.dcim.sites.filter.return_value = [site_obj]

    # VRF mock
    vrf_obj = MagicMock(id=1, name="VRF-Global")
    mock_nb.ipam.vrfs.filter.return_value = [vrf_obj]

    # Device Types & Roles mocks
    mock_nb.dcim.device_types.filter.return_value = [MagicMock(id=2)]
    mock_nb.dcim.device_roles.filter.return_value = [MagicMock(id=3)]

    # Device mock - não existe inicialmente (será criado)
    mock_nb.dcim.devices.filter.return_value = []
    created_device = MagicMock(id=100, name="ROUTER-TEST")
    mock_nb.dcim.devices.create.return_value = created_device

    # Interfaces mock
    mock_nb.dcim.interfaces.filter.return_value = []
    created_iface = MagicMock(id=200, name="ether1")
    mock_nb.dcim.interfaces.create.return_value = created_iface

    # VLANs mock - não existe inicialmente
    mock_nb.ipam.vlans.filter.return_value = []
    created_vlan = MagicMock(id=300, vid=100, name="VLAN-100")
    mock_nb.ipam.vlans.create.return_value = created_vlan

    # IP mock
    mock_nb.ipam.ip_addresses.filter.return_value = []
    created_ip = MagicMock(id=400, address="192.168.1.1/24")
    mock_nb.ipam.ip_addresses.create.return_value = created_ip

    data = {
        'hostname': 'ROUTER-TEST',
        'serial': 'SN12345',
        'model': 'CCR1036-8G-2S+',
        'tags': set(),
        'vlans': {100: 'VLAN-100'},
        'interfaces_physical': [{'name': 'ether1', 'description': 'UPLINK', 'enabled': True}],
        'interfaces_l3': [],
        'lags': [],
        'ips': [{'interface': 'ether1', 'address': '192.168.1.1/24'}],
        'vpws': [],
        'vpls': [],
        'interface_vlans': {'ether1': {100}},
        'interface_untagged_vlans': {},
        'vrrp_groups': [],
        'lldp_neighbors': []
    }

    sync_to_netbox(
        data,
        url="http://netbox.local",
        token="test_token",
        tenant_name="CLIENTE-X",
        sync_modules=['device', 'vlans', 'interfaces', 'ips']
    )

    # 1. Verifica criação do Device com tenant=10
    device_create_call = mock_nb.dcim.devices.create.call_args
    assert device_create_call is not None
    assert device_create_call.kwargs.get('tenant') == 10

    # 2. Verifica criação da VLAN com tenant=10
    vlan_create_call = mock_nb.ipam.vlans.create.call_args
    assert vlan_create_call is not None
    assert vlan_create_call.kwargs.get('tenant') == 10

    # 3. Verifica criação do IP com tenant=10
    ip_create_call = mock_nb.ipam.ip_addresses.create.call_args
    assert ip_create_call is not None
    assert ip_create_call.kwargs.get('tenant') == 10


def test_main_cli_argument_vrf():
    parser = main.create_parser()
    args = parser.parse_args(["--host", "10.0.0.1", "--vrf", "CLIENTE-Y"])
    assert args.vrf == "CLIENTE-Y"


@patch("pynetbox.api")
def test_sync_to_netbox_applies_custom_vrf(mock_pynetbox_api):
    mock_nb = MagicMock()
    mock_pynetbox_api.return_value = mock_nb

    # Site mock
    mock_nb.dcim.sites.filter.return_value = [MagicMock(id=1, name="MTSJ")]
    # Custom VRF mock (id=55)
    custom_vrf = MagicMock(id=55, name="CLIENTE-Y")
    mock_nb.ipam.vrfs.filter.return_value = [custom_vrf]

    # Device mock
    mock_device = MagicMock(id=100, comments="")
    mock_device.name = "ROUTER-TEST"
    mock_nb.dcim.devices.filter.return_value = [mock_device]

    # Interfaces mock
    mock_iface = MagicMock(id=200)
    mock_iface.name = "ether1"
    mock_nb.dcim.interfaces.filter.return_value = [mock_iface]

    # IP mock (não existe, será criado)
    mock_nb.ipam.ip_addresses.filter.return_value = []
    created_ip = MagicMock(id=400, address="10.50.0.1/30")
    mock_nb.ipam.ip_addresses.create.return_value = created_ip

    data = {
        'hostname': 'ROUTER-TEST',
        'serial': 'SN12345',
        'model': 'CCR1036-8G-2S+',
        'tags': set(),
        'vlans': {},
        'interfaces_physical': [{'name': 'ether1', 'description': 'WAN', 'enabled': True}],
        'interfaces_l3': [],
        'lags': [],
        'ips': [{'interface': 'ether1', 'address': '10.50.0.1/30'}],
        'vpws': [],
        'vpls': [],
        'interface_vlans': {},
        'interface_untagged_vlans': {},
        'vrrp_groups': [],
        'lldp_neighbors': []
    }

    sync_to_netbox(
        data,
        url="http://netbox.local",
        token="test_token",
        vrf_name="CLIENTE-Y",
        sync_modules=['ips']
    )

    # Verifica que o IP foi criado vinculado à VRF customizada (vrf=55)
    ip_create_call = mock_nb.ipam.ip_addresses.create.call_args
    assert ip_create_call is not None
    assert ip_create_call.kwargs.get('vrf') == 55


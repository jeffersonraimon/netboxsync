"""
Modulo Parser de Configuracoes e Comandos DmOS (Função de Compatibilidade)
========================================================================
Delega o parsing para o driver Datacom DmOS (DatacomDmOSDriver).
"""

from drivers.datacom_dmos import DatacomDmOSDriver


def parse_dmos_data(config_text, inventory_text="", platform_text="", lldp_text="", json_payload=None):
    """
    Função de retrocompatibilidade que chama o método parse_data do DatacomDmOSDriver.
    """
    raw_outputs = {
        'config': config_text,
        'inventory': inventory_text,
        'platform': platform_text,
        'lldp': lldp_text,
        'json_payload': json_payload
    }
    driver = DatacomDmOSDriver()
    return driver.parse_data(raw_outputs)

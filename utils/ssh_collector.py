"""
Modulo de Conexao SSH com Switches Datacom DmOS (Função de Compatibilidade)
===========================================================================
Delega a coleta SSH para o driver Datacom DmOS (DatacomDmOSDriver).
"""

from drivers.datacom_dmos import DatacomDmOSDriver


def fetch_dmos_via_ssh(host, username, password, secret=None, port=22, commands=None, debug=False):
    """
    Função de retrocompatibilidade que chama o método fetch_data do DatacomDmOSDriver.
    """
    driver = DatacomDmOSDriver()
    return driver.fetch_data(
        host=host,
        username=username,
        password=password,
        port=port,
        debug=debug,
        commands=commands
    )

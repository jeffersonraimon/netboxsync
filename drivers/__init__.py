"""
Drivers Package
==============
Módulo de drivers multimarcas para a sincronização de dados NetBox.
"""

from drivers.base import BaseDeviceDriver
from drivers.registry import register_driver, get_driver, list_drivers

# Importa drivers concretos para garantir que sejam registrados automaticamente no registry
import drivers.datacom_dmos
import drivers.mikrotik_routeros
import drivers.huawei_vrp
import drivers.template_driver

__all__ = [
    'BaseDeviceDriver',
    'register_driver',
    'get_driver',
    'list_drivers'
]

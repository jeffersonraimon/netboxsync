"""
Driver Registry
===============
Mecanismo de registro centralizado e fábrica (Factory) de drivers de dispositivos.
"""

from typing import Dict, Type
from drivers.base import BaseDeviceDriver

_DRIVERS_REGISTRY: Dict[str, Type[BaseDeviceDriver]] = {}


def register_driver(slug: str):
    """
    Decorator para registrar uma classe de driver no registry global.
    
    Exemplo de uso:
        @register_driver('dmos')
        class DatacomDmOSDriver(BaseDeviceDriver):
            ...
    """
    def decorator(cls: Type[BaseDeviceDriver]):
        cls.driver_slug = slug
        _DRIVERS_REGISTRY[slug.lower()] = cls
        return cls
    return decorator


def get_driver(driver_name: str) -> BaseDeviceDriver:
    """
    Instancia e retorna o driver registrado com o slug informado.
    Lança ValueError caso o driver não seja encontrado.
    """
    slug = (driver_name or '').strip().lower()
    if slug not in _DRIVERS_REGISTRY:
        available = ", ".join(sorted(_DRIVERS_REGISTRY.keys()))
        raise ValueError(f"Driver '{driver_name}' não foi encontrado no registro. Drivers disponíveis: {available}")
    return _DRIVERS_REGISTRY[slug]()


def list_drivers() -> Dict[str, Type[BaseDeviceDriver]]:
    """
    Retorna o dicionário de drivers registrados.
    """
    return dict(_DRIVERS_REGISTRY)

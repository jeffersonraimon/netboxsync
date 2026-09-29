import os
from dotenv import load_dotenv

# Carrega variáveis de ambiente do arquivo .env (se existir)
load_dotenv()

NETBOX_URL = os.getenv("NETBOX_URL", "https://netbox.suaempresa.com.br/")
NETBOX_TOKEN = os.getenv("NETBOX_TOKEN", "seu_token_api_aqui")
DEFAULT_SITE_NAME = os.getenv("NETBOX_SITE", "MTSJ")
DEFAULT_DEVICE_ROLE = os.getenv("NETBOX_ROLE", "SWITCH")
DEFAULT_DEVICE_TYPE = os.getenv("NETBOX_DEVICE_TYPE", "DM4380 12XS+3CX")
VRF_NAME = os.getenv("VRF_NAME", "VRF-Global")
DISABLE_SSL_VERIFY = os.getenv("DISABLE_SSL_VERIFY", "true").lower() in ("true", "1", "yes")



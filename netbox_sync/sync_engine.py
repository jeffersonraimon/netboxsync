"""
Modulo de Sincronizacao com a API do NetBox
"""

import re
import urllib3
from datetime import datetime
import pynetbox
import config

# Desabilita alertas de SSL inseguro/autassinado do urllib3 se a verificação SSL estiver desativada
if config.DISABLE_SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def safe_get(endpoint, **kwargs):
    """
    Realiza uma busca em endpoint do pynetbox de forma segura.
    Se houver múltiplos objetos no NetBox (o que faria .get() lançar exceção),
    retorna a primeira correspondência usando filter().
    """
    if not endpoint:
        return None
    try:
        results = list(endpoint.filter(**kwargs))
        return results[0] if results else None
    except Exception:
        return None


def lag_parent_id(iface):
    """
    Extrai o ID da LAG pai de uma interface, aceitando Record do pynetbox, dict ou int.
    """
    lag = getattr(iface, 'lag', None)
    if lag is None:
        return None
    if isinstance(lag, dict):
        return lag.get('id')
    if isinstance(lag, int):
        return lag
    return getattr(lag, 'id', None)


def find_netbox_device(nb, dev_name):
    """
    Busca um equipamento no NetBox por nome exato, slug, nome insensível a maiúsculas/minúsculas,
    ou por nome curto sem FQDN.
    """
    if not dev_name:
        return None

    dev_name_clean = dev_name.strip()
    devs = list(nb.dcim.devices.filter(name=dev_name_clean))
    if devs:
        return devs[0]

    short_name = dev_name_clean.split('.')[0]
    if short_name != dev_name_clean:
        devs = list(nb.dcim.devices.filter(name=short_name))
        if devs:
            return devs[0]

    all_devs = list(nb.dcim.devices.all())
    target_lower = dev_name_clean.lower()
    target_short_lower = short_name.lower()

    for d in all_devs:
        if not getattr(d, 'name', None):
            continue
        d_name_lower = d.name.lower()
        if d_name_lower == target_lower or d_name_lower == target_short_lower:
            return d

    return None


def abbreviate_ifname(if_name):
    """
    Abrevia os nomes das interfaces para manter o label do cabo dentro do limite de 100 caracteres do NetBox.
    Ex: 'hundred-gigabit-ethernet 1/1/1' -> 'hun 1/1/1'
        'ten-gigabit-ethernet-1/1/7' -> 'ten 1/1/7'
    """
    if not if_name:
        return ""
    s = str(if_name)
    s = s.replace("hundred-gigabit-ethernet-", "hun ").replace("hundred-gigabit-ethernet ", "hun ")
    s = s.replace("ten-gigabit-ethernet-", "ten ").replace("ten-gigabit-ethernet ", "ten ")
    s = s.replace("twenty-five-gigabit-ethernet-", "twentyfive ").replace("twenty-five-gigabit-ethernet ", "twentyfive ")
    s = s.replace("forty-gigabit-ethernet-", "forty ").replace("forty-gigabit-ethernet ", "forty ")
    s = s.replace("gigabit-ethernet-", "ge ").replace("gigabit-ethernet ", "ge ")
    return s.strip()


def find_netbox_interface(nb, dev_id, if_name):
    """
    Busca uma interface em um equipamento do NetBox testando variações de nome
    (com/sem espaço, hífen, abreviações, ou apenas número da porta).
    """
    if not if_name:
        return None

    if_name_clean = if_name.strip()
    all_ifaces = list(nb.dcim.interfaces.filter(device_id=dev_id))
    if not all_ifaces:
        return None

    for iface in all_ifaces:
        if iface.name == if_name_clean:
            return iface

    norm_target = if_name_clean.replace(" ", "-").lower()
    for iface in all_ifaces:
        if iface.name.replace(" ", "-").lower() == norm_target:
            return iface

    port_match = re.search(r'(\d+(?:/\d+)+)', if_name_clean)
    if port_match:
        port_num = port_match.group(1)
        for iface in all_ifaces:
            if iface.name.endswith(port_num) or (f"/{port_num}" in iface.name):
                return iface

    for iface in all_ifaces:
        if norm_target in iface.name.replace(" ", "-").lower() or iface.name.replace(" ", "-").lower() in norm_target:
            return iface

    return None


def sync_to_netbox(data, url=None, token=None, site_name=None, device_role=None, device_type_model=None, verify_ssl=None, sync_modules=None, driver=None):
    """
    Sincroniza os dados parseados do equipamento com a API do NetBox.
    Se sync_modules for informado (ex: ['interfaces', 'ips', 'vlans']), sincroniza apenas os modulos listados.
    Se None ou 'all' estiver presente em sync_modules, sincroniza tudo.
    """
    if not sync_modules or 'all' in sync_modules:
        sync_modules = ['device', 'vlans', 'interfaces', 'transceivers', 'ips', 'vrrp', 'l2vpn', 'vpn_tunnels', 'cables']
    url = url or config.NETBOX_URL
    token = token or config.NETBOX_TOKEN
    site_name = site_name or config.DEFAULT_SITE_NAME
    device_role = device_role or config.DEFAULT_DEVICE_ROLE
    device_type_model = device_type_model or config.DEFAULT_DEVICE_TYPE
    vrf_name = config.VRF_NAME
    
    if verify_ssl is None:
        verify_ssl = not config.DISABLE_SSL_VERIFY

    print(f"[*] Conectando ao NetBox em {url} (SSL Verify: {verify_ssl})...")
    nb = pynetbox.api(url, token=token)
    nb.http_session.verify = verify_ssl

    hostname = data['hostname']
    if not hostname:
        print("[!] Erro: Hostname nao encontrado na configuracao.")
        return

    print(f"[*] Processando equipamento: {hostname}")

    # 1. Garantir VRF-Global
    vrf_obj = safe_get(nb.ipam.vrfs, name=vrf_name)
    if not vrf_obj:
        print(f"[+] Criando VRF '{vrf_name}'...")
        vrf_obj = nb.ipam.vrfs.create(name=vrf_name, rd="65000:1", description="VRF Tabela de Roteamento Global")
    vrf_id = getattr(vrf_obj, 'id', getattr(vrf_obj, 'pk', None))

    # 2. Garantir Tags
    tag_objs = []
    for tag_name in data['tags']:
        tag_slug = tag_name.lower()
        tags_found = list(nb.extras.tags.filter(slug=tag_slug))
        if tags_found:
            tag = tags_found[0]
        else:
            tag = nb.extras.tags.create(name=tag_name, slug=tag_slug)
        tag_id = getattr(tag, 'id', getattr(tag, 'pk', None))
        if tag_id:
            tag_objs.append(int(tag_id))

    # 2.5. Garantir Site (POP) no NetBox
    site = safe_get(nb.dcim.sites, name=site_name)
    if not site:
        site_slug = re.sub(r'[^\w\-]', '_', site_name.lower()).strip('_') or "site-default"
        site = safe_get(nb.dcim.sites, slug=site_slug)
        if not site:
            sites_all = list(nb.dcim.sites.all())
            site = next((s for s in sites_all if s.name.lower() == site_name.lower() or s.slug == site_slug), None)

    if not site:
        print(f"[+] Criando Site (POP) '{site_name}' no NetBox...")
        try:
            site_slug = re.sub(r'[^\w\-]', '_', site_name.lower()).strip('_') or "site-default"
            site = nb.dcim.sites.create(name=site_name, slug=site_slug, status='active')
            print(f"[✔] Site (POP) '{site_name}' criado com sucesso no NetBox!")
        except Exception as e:
            print(f"[!] Erro ao criar Site '{site_name}': {e}")
            sites_all = list(nb.dcim.sites.all())
            site = sites_all[0] if sites_all else None

    if not site:
        print(f"[!] Erro: Nao foi possivel obter ou criar o Site ({site_name}) no NetBox.")
        return

    site_id = getattr(site, 'id', getattr(site, 'pk', None))

    # 3. Garantir Device no NetBox
    device = safe_get(nb.dcim.devices, name=hostname)
    if not device:
        print(f"[+] Criando Device '{hostname}' no NetBox...")
        
        # 3.1 Resolver / Criar DeviceType
        dev_type = safe_get(nb.dcim.device_types, model=device_type_model)
        if not dev_type:
            type_slug = re.sub(r'[^\w\-]', '_', device_type_model.lower()).strip('_') or "default-model"
            dev_type = safe_get(nb.dcim.device_types, slug=type_slug)
            if not dev_type:
                types_all = list(nb.dcim.device_types.all())
                dev_type = next((t for t in types_all if t.model.lower() == device_type_model.lower() or t.slug == type_slug), None)

        if not dev_type:
            mfg_name = data.get('manufacturer') or "Datacom"
            mfg_slug = re.sub(r'[^\w\-]', '_', mfg_name.lower()).strip('_') or "datacom"
            mfg = safe_get(nb.dcim.manufacturers, name=mfg_name) or safe_get(nb.dcim.manufacturers, slug=mfg_slug)
            if not mfg:
                mfgs_all = list(nb.dcim.manufacturers.all())
                mfg = next((m for m in mfgs_all if m.name.lower() == mfg_name.lower() or m.slug == mfg_slug), mfgs_all[0] if mfgs_all else None)
            if not mfg:
                print(f"[+] Criando Fabricante '{mfg_name}' no NetBox...")
                try:
                    mfg = nb.dcim.manufacturers.create(name=mfg_name, slug=mfg_slug)
                except Exception as mfg_err:
                    mfgs_all = list(nb.dcim.manufacturers.all())
                    mfg = mfgs_all[0] if mfgs_all else None

            mfg_id = getattr(mfg, 'id', getattr(mfg, 'pk', None))
            if mfg_id:
                print(f"[+] Criando Device Type '{device_type_model}' (Fabricante: {mfg.name}) no NetBox...")
                try:
                    type_slug = re.sub(r'[^\w\-]', '_', device_type_model.lower()).strip('_') or "default-model"
                    dev_type = nb.dcim.device_types.create(model=device_type_model, slug=type_slug, manufacturer=mfg_id)
                    print(f"[✔] Device Type '{device_type_model}' criado com sucesso no NetBox!")
                except Exception as dt_err:
                    print(f"[!] Aviso ao criar Device Type '{device_type_model}': {dt_err}")
                    types_all = list(nb.dcim.device_types.all())
                    dev_type = types_all[0] if types_all else None

        # 3.2 Resolver / Criar DeviceRole
        dev_role = safe_get(nb.dcim.device_roles, name=device_role)
        if not dev_role:
            role_slug = re.sub(r'[^\w\-]', '_', device_role.lower()).strip('_') or "switch"
            dev_role = safe_get(nb.dcim.device_roles, slug=role_slug)
            if not dev_role:
                roles_all = list(nb.dcim.device_roles.all())
                dev_role = next((r for r in roles_all if r.name.lower() == device_role.lower() or r.slug == role_slug), None)

        if not dev_role:
            print(f"[+] Criando Device Role '{device_role}' no NetBox...")
            try:
                role_slug = re.sub(r'[^\w\-]', '_', device_role.lower()).strip('_') or "switch"
                dev_role = nb.dcim.device_roles.create(name=device_role, slug=role_slug, color="2096f3")
                print(f"[✔] Device Role '{device_role}' criada com sucesso no NetBox!")
            except Exception as dr_err:
                print(f"[!] Aviso ao criar Device Role '{device_role}': {dr_err}")
                roles_all = list(nb.dcim.device_roles.all())
                dev_role = roles_all[0] if roles_all else None

        dev_type_id = getattr(dev_type, 'id', getattr(dev_type, 'pk', None))
        dev_role_id = getattr(dev_role, 'id', getattr(dev_role, 'pk', None))

        if not site_id or not dev_type_id or not dev_role_id:
            err_msg = f"Nao foi possivel obter IDs validos para Site ({site_id}), DeviceType ({dev_type_id}) ou DeviceRole ({dev_role_id})."
            print(f"[!] Erro: {err_msg}")
            raise RuntimeError(err_msg)

        now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        comment_text = f"ATT VIA SCRIPT EM: {now_str}"

        try:
            device_kwargs = {
                'name': hostname,
                'site': site_id,
                'device_type': dev_type_id,
                'role': dev_role_id,
                'tags': tag_objs,
                'comments': comment_text
            }
            if data.get('serial'):
                device_kwargs['serial'] = data['serial']

            device = nb.dcim.devices.create(**device_kwargs)
            print(f"[✔] Equipamento '{hostname}' criado com sucesso no NetBox!")
        except Exception as e:
            err_msg = f"Erro ao criar o equipamento {hostname} no NetBox: {e}"
            print(f"[!] {err_msg}")
            raise RuntimeError(err_msg)
    else:
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        att_block = f"\n---\nATT VIA SCRIPT EM: {now_str}"

        existing_comments = (getattr(device, 'comments', '') or '').strip()

        # Remove qualquer bloco anterior "--- \n ATT VIA SCRIPT EM:" ou similar
        cleaned_comments = re.sub(r'(\r?\n)*---\s*\r?\nATT VIA SCRIPT EM:[^\r\n]*', '', existing_comments).strip()
        cleaned_comments = re.sub(r'ATT VIA SCRIPT EM:[^\r\n]*', '', cleaned_comments).strip()

        if cleaned_comments:
            new_comments = f"{cleaned_comments}\n{att_block}"
        else:
            new_comments = att_block.strip()

        need_save = False
        if getattr(device, 'comments', '') != new_comments:
            print(f"[➔] Atualizando comentário do equipamento '{hostname}' no NetBox...")
            device.comments = new_comments
            need_save = True

        existing_tags = [getattr(t, 'id', t) for t in device.tags]
        merged_tags = list(set([int(x) for x in existing_tags + tag_objs if str(x).isdigit()]))
        if set(existing_tags) != set(merged_tags):
            device.tags = merged_tags
            need_save = True

        if not getattr(device, 'site', None) and site_id:
            print(f"[➔] Atribuindo Site ao equipamento '{hostname}' -> '{site.name}'")
            device.site = site_id
            need_save = True

        if data.get('serial') and not getattr(device, 'serial', None):
            device.serial = data['serial']
            need_save = True

        if need_save:
            try:
                device.save()
            except Exception as dev_err:
                print(f"[!] Erro ao salvar atualizações do equipamento {hostname}: {dev_err}")

    device_id = getattr(device, 'id', getattr(device, 'pk', None))

    # 4. Garantir criação das VLAN Roles no NetBox
    vlan_roles_cache = {}
    required_roles = ["PTP-EQUIPAMENTOS", "VPWS-TUNEIS", "VPLS-TUNEIS"]
    for role_name in required_roles:
        role_slug = re.sub(r'[^\w\-]', '_', role_name.lower())
        role_obj = safe_get(nb.ipam.roles, name=role_name)
        if not role_obj:
            print(f"[+] Criando VLAN Role '{role_name}' no NetBox...")
            role_obj = nb.ipam.roles.create(name=role_name, slug=role_slug)
        vlan_roles_cache[role_name] = getattr(role_obj, 'id', getattr(role_obj, 'pk', None))

    # Pre-carrega todas as interfaces ja existentes no dispositivo do NetBox para uso por outros módulos.
    # Nomes EXATOS tem prioridade: variacoes com espaco/hifen entram apenas como fallback. Sem isso, um
    # cadastro em hifen (ex: 'ten-gigabit-ethernet-1/1/1' criado por engano em execucoes antigas) sobrescreve
    # a chave da interface oficial com espaco ('ten-gigabit-ethernet 1/1/1') e o sync deixa de cria-la/atualiza-la.
    existing_ifaces_map = {}
    device_ifaces = list(nb.dcim.interfaces.filter(device_id=device_id))
    for existing_if in device_ifaces:
        existing_ifaces_map[existing_if.name] = existing_if
    for existing_if in device_ifaces:
        for alt_key in (existing_if.name.replace(" ", "-"), existing_if.name.replace("-", " ")):
            existing_ifaces_map.setdefault(alt_key, existing_if)

    # Sincronizar VLANs e atribuir as Roles
    nb_vlans = {}
    if any(m in sync_modules for m in ['vlans', 'vlan']):
        vlan_roles_map = data.get('vlan_roles_map', {})
        for vid, vname in data['vlans'].items():
            # Busca prioritariamente VLANs Globais (site_id='null') ou qualquer VLAN com o mesmo VID
            vlans_found = list(nb.ipam.vlans.filter(vid=vid, site_id='null')) or list(nb.ipam.vlans.filter(vid=vid))
            vlan = vlans_found[0] if vlans_found else None

            target_role_name = vlan_roles_map.get(vid)
            target_role_id = vlan_roles_cache.get(target_role_name) if target_role_name else None

            if not vlan:
                print(f"[+] Criando VLAN {vid} - {vname} (Global)" + (f" (Role: {target_role_name})" if target_role_name else ""))
                vlan_kwargs = {'vid': vid, 'name': vname, 'status': 'active'}
                if target_role_id:
                    vlan_kwargs['role'] = target_role_id
                vlan = nb.ipam.vlans.create(**vlan_kwargs)
            else:
                need_save = False
                # Se a VLAN existia em outro status (ex: deprecated), reativa para active
                if getattr(vlan.status, 'value', str(vlan.status)).lower() != 'active':
                    print(f"[➔] Reativando VLAN {vid} ({vname}) -> Status: Active")
                    vlan.status = 'active'
                    need_save = True

                if target_role_id and getattr(vlan.role, 'id', None) != target_role_id:
                    print(f"[➔] Atribuindo Role '{target_role_name}' para a VLAN {vid} ({vname})")
                    vlan.role = target_role_id
                    need_save = True

                if need_save:
                    vlan.save()
            nb_vlans[vid] = vlan
    else:
        # Preenche nb_vlans para módulos dependentes (ex: l2vpn) caso existam no NetBox
        for vid in data['vlans'].keys():
            vlans_found = list(nb.ipam.vlans.filter(vid=vid, site_id='null')) or list(nb.ipam.vlans.filter(vid=vid))
            if vlans_found:
                nb_vlans[vid] = vlans_found[0]

    # Guarda os IDs dos IPs de Loopback para definir como IP primario do equipamento
    primary_ip4_id = None
    primary_ip6_id = None

    # 5. Sincronizar Interfaces (Fisicas, LAGs e L3)
    if any(m in sync_modules for m in ['interfaces', 'interface', 'ifaces', 'iface']):
        all_interfaces = data['lags'] + data['interfaces_physical'] + data['interfaces_l3']
        for iface in all_interfaces:
            if_name = iface['name']
            try:
                # Tenta casar pelo nome ou variacao de traco/espaco para nao duplicar
                nb_iface = existing_ifaces_map.get(if_name) or existing_ifaces_map.get(if_name.replace(" ", "-")) or existing_ifaces_map.get(if_name.replace("-", " "))

                is_l3_subif = any(l3['name'] == if_name for l3 in data['interfaces_l3'])
                if is_l3_subif:
                    l3_obj = next(l3 for l3 in data['interfaces_l3'] if l3['name'] == if_name)
                    if_type = l3_obj.get('type') or (driver.normalize_interface_type(if_name) if driver else ('bridge' if 'bridge' in if_name.lower() else 'virtual'))
                elif driver:
                    if_type = driver.normalize_interface_type(if_name)
                elif 'lag' in if_name:
                    if_type = 'lag'
                elif 'l3-' in if_name or 'loopback' in if_name:
                    if_type = 'virtual'
                elif 'hundred' in if_name:
                    if_type = '100gbase-x-qsfp28'
                elif 'twenty-five' in if_name or '25g' in if_name:
                    if_type = '25gbase-x-sfp28'
                elif 'forty' in if_name:
                    if_type = '40gbase-x-qsfpp'
                elif 'gigabit-ethernet' in if_name and 'ten' not in if_name and 'hundred' not in if_name and 'twenty' not in if_name:
                    if_type = '1000base-t'
                else:
                    if_type = '10gbase-x-sfpp'

                if_desc = iface.get('description', '')

                if not nb_iface:
                    print(f"[+] Criando Interface {if_name} no NetBox...")
                    nb_iface = nb.dcim.interfaces.create(
                        device=device_id,
                        name=if_name,
                        type=if_type,
                        label=if_desc,
                        description=if_desc,
                        enabled=iface.get('enabled', True),
                        mtu=iface.get('mtu')
                    )
                    existing_ifaces_map[if_name] = nb_iface
                    # Registra as variacoes espaco/hifen (fallback) para que referencias de LAG/VLAN
                    # encontrem a interface recem-criada ainda nesta mesma execucao
                    existing_ifaces_map.setdefault(if_name.replace(" ", "-"), nb_iface)
                    existing_ifaces_map.setdefault(if_name.replace("-", " "), nb_iface)
                else:
                    need_save = False
                    cur_type = getattr(nb_iface.type, 'value', str(nb_iface.type or ''))
                    if cur_type != if_type:
                        print(f"[➔] Atualizando tipo da interface {nb_iface.name} no NetBox: {cur_type} -> {if_type}")
                        nb_iface.type = if_type
                        need_save = True
                    if nb_iface.enabled != iface.get('enabled', True):
                        nb_iface.enabled = iface.get('enabled', True)
                        need_save = True
                    if nb_iface.label != if_desc:
                        nb_iface.label = if_desc
                        need_save = True
                    if nb_iface.description != if_desc:
                        nb_iface.description = if_desc
                        need_save = True
                    if iface.get('mtu') and nb_iface.mtu != iface.get('mtu'):
                        nb_iface.mtu = iface.get('mtu')
                        need_save = True
                    if need_save:
                        try:
                            nb_iface.save()
                        except Exception as save_err:
                            print(f"[!] Aviso ao salvar dados da interface {nb_iface.name}: {save_err}")

                if nb_iface:
                    # Busca VLANs vinculadas testando variações do nome da interface (espaço vs hífen)
                    vlan_ids = data['interface_vlans'].get(if_name) or data['interface_vlans'].get(if_name.replace(" ", "-")) or data['interface_vlans'].get(if_name.replace("-", " "))
                    untagged_vid = data.get('interface_untagged_vlans', {}).get(if_name) or data.get('interface_untagged_vlans', {}).get(if_name.replace(" ", "-")) or data.get('interface_untagged_vlans', {}).get(if_name.replace("-", " "))

                    # 1. Resolve VLAN untagged no NetBox se informada
                    untagged_target_id = None
                    if untagged_vid:
                        u_obj = nb_vlans.get(untagged_vid)
                        if not u_obj:
                            u_found = list(nb.ipam.vlans.filter(vid=untagged_vid, site_id='null')) or list(nb.ipam.vlans.filter(vid=untagged_vid))
                            u_obj = u_found[0] if u_found else None

                        if not u_obj:
                            vname = data['vlans'].get(untagged_vid, f"VLAN-{untagged_vid}")
                            target_role_name = data.get('vlan_roles_map', {}).get(untagged_vid)
                            target_role_id = vlan_roles_cache.get(target_role_name) if target_role_name else None
                            print(f"[+] Criando VLAN {untagged_vid} - {vname} (Global) no NetBox...")
                            try:
                                v_kwargs = {'vid': untagged_vid, 'name': vname, 'status': 'active'}
                                if target_role_id:
                                    v_kwargs['role'] = target_role_id
                                u_obj = nb.ipam.vlans.create(**v_kwargs)
                            except Exception:
                                u_found = list(nb.ipam.vlans.filter(vid=untagged_vid, site_id='null')) or list(nb.ipam.vlans.filter(vid=untagged_vid))
                                u_obj = u_found[0] if u_found else None

                        if u_obj:
                            nb_vlans[untagged_vid] = u_obj
                            u_id = getattr(u_obj, 'id', getattr(u_obj, 'pk', None))
                            if u_id is not None:
                                untagged_target_id = int(u_id)

                    # 2. Resolve VLANs tagged no NetBox se informadas
                    vlan_target_ids = []
                    if vlan_ids:
                        for v in vlan_ids:
                            v_obj = nb_vlans.get(v)
                            if not v_obj:
                                v_found = list(nb.ipam.vlans.filter(vid=v, site_id='null')) or list(nb.ipam.vlans.filter(vid=v))
                                v_obj = v_found[0] if v_found else None

                            if not v_obj:
                                vname = data['vlans'].get(v, f"VLAN-{v}")
                                target_role_name = data.get('vlan_roles_map', {}).get(v)
                                target_role_id = vlan_roles_cache.get(target_role_name) if target_role_name else None
                                print(f"[+] Criando VLAN {v} - {vname} (Global) no NetBox...")
                                try:
                                    v_kwargs = {'vid': v, 'name': vname, 'status': 'active'}
                                    if target_role_id:
                                        v_kwargs['role'] = target_role_id
                                    v_obj = nb.ipam.vlans.create(**v_kwargs)
                                except Exception:
                                    v_found = list(nb.ipam.vlans.filter(vid=v, site_id='null')) or list(nb.ipam.vlans.filter(vid=v))
                                    v_obj = v_found[0] if v_found else None
                            if v_obj:
                                nb_vlans[v] = v_obj
                                v_id = getattr(v_obj, 'id', getattr(v_obj, 'pk', None))
                                if v_id is not None:
                                    vlan_target_ids.append(int(v_id))

                    if is_l3_subif and (if_type == 'virtual' or 'l3-' in if_name):
                        target_untagged = untagged_target_id or (vlan_target_ids[0] if vlan_target_ids else None)
                        cur_untagged = getattr(nb_iface.untagged_vlan, 'id', getattr(nb_iface.untagged_vlan, 'pk', nb_iface.untagged_vlan))
                        if target_untagged and cur_untagged != target_untagged:
                            nb_iface.untagged_vlan = target_untagged
                            nb_iface.mode = 'access'
                            try:
                                nb_iface.save()
                                print(f"[➔] Interface L3 {nb_iface.name} associada à VLAN: {untagged_vid or list(vlan_ids)}")
                            except Exception as l3_save_err:
                                print(f"[!] Aviso ao salvar VLAN na interface L3 {nb_iface.name}: {l3_save_err}")
                    else:
                        cur_mode_raw = getattr(nb_iface, 'mode', None)
                        cur_mode = getattr(cur_mode_raw, 'value', str(cur_mode_raw or '')).lower()
                        
                        cur_untagged_raw = getattr(nb_iface, 'untagged_vlan', None)
                        cur_untagged_id = getattr(cur_untagged_raw, 'id', getattr(cur_untagged_raw, 'pk', cur_untagged_raw))

                        raw_tagged = getattr(nb_iface, 'tagged_vlans', []) or []
                        cur_tagged = []
                        for v_item in raw_tagged:
                            if isinstance(v_item, dict):
                                cur_tagged.append(v_item.get('id'))
                            else:
                                cur_tagged.append(getattr(v_item, 'id', getattr(v_item, 'pk', v_item)))
                        
                        cur_tagged_set = set(int(x) for x in cur_tagged if x is not None and str(x).isdigit())
                        target_tagged_set = set(int(x) for x in vlan_target_ids if str(x).isdigit())

                        if target_tagged_set:
                            target_mode = 'tagged'
                        elif untagged_target_id:
                            target_mode = 'access'
                        else:
                            target_mode = None

                        if cur_mode != target_mode or cur_untagged_id != untagged_target_id or cur_tagged_set != target_tagged_set:
                            log_info = []
                            if target_mode:
                                log_info.append(f"Mode: {target_mode.capitalize()}")
                            if untagged_vid:
                                log_info.append(f"Untagged: {untagged_vid}")
                            if vlan_ids:
                                log_info.append(f"Tagged: {list(vlan_ids)}")
                            print(f"[➔] Interface {nb_iface.name} configurada com " + " | ".join(log_info if log_info else ["Mode: None"]))

                            try:
                                if target_mode == 'tagged':
                                    nb_iface.mode = 'tagged'
                                    nb_iface.untagged_vlan = untagged_target_id
                                    nb_iface.tagged_vlans = list(target_tagged_set)
                                    nb_iface.save()
                                elif target_mode == 'access':
                                    if cur_mode == 'tagged' and cur_tagged_set:
                                        nb_iface.mode = 'tagged'
                                        nb_iface.tagged_vlans = []
                                        nb_iface.save()
                                    nb_iface.mode = 'access'
                                    nb_iface.untagged_vlan = untagged_target_id
                                    nb_iface.save()
                                else:
                                    if cur_mode == 'tagged' and cur_tagged_set:
                                        nb_iface.mode = 'tagged'
                                        nb_iface.tagged_vlans = []
                                        nb_iface.save()
                                    nb_iface.mode = None
                                    nb_iface.untagged_vlan = None
                                    nb_iface.save()
                            except Exception as tag_err:
                                print(f"[!] Aviso ao salvar VLANs em {nb_iface.name}: {tag_err}")
            except Exception as iface_err:
                print(f"[!] Erro genérico ao processar interface {if_name}: {iface_err}")

        # Atribuir parent interface para subinterfaces L3 (ex: VLANs, VRRP, VPLS)
        for l3_if in data['interfaces_l3']:
            parent_name = l3_if.get('parent')
            if parent_name:
                nb_sub = existing_ifaces_map.get(l3_if['name'])
                nb_parent = existing_ifaces_map.get(parent_name) or existing_ifaces_map.get(parent_name.replace(" ", "-")) or existing_ifaces_map.get(parent_name.replace("-", " "))
                if nb_sub and nb_parent:
                    parent_id_val = getattr(nb_parent, 'id', getattr(nb_parent, 'pk', None))
                    current_parent_id = getattr(nb_sub.parent, 'id', nb_sub.parent if isinstance(nb_sub.parent, int) else None)
                    if current_parent_id != parent_id_val:
                        print(f"[➔] Associando Parent Interface {nb_parent.name} -> Subinterface {nb_sub.name}")
                        nb_sub.parent = parent_id_val
                        try:
                            nb_sub.save()
                        except Exception as p_err:
                            print(f"[!] Aviso ao associar parent interface em {nb_sub.name}: {p_err}")

        # Atribuir bridged_interface (Bridge Parent) para portas membros de Bridges (ex: ether4 -> bridge1)
        if data.get('bridge_ports'):
            for member_name, bridge_name in data['bridge_ports'].items():
                nb_member = existing_ifaces_map.get(member_name) or existing_ifaces_map.get(member_name.replace(" ", "-")) or existing_ifaces_map.get(member_name.replace("-", " "))
                nb_bridge = existing_ifaces_map.get(bridge_name) or existing_ifaces_map.get(bridge_name.replace(" ", "-")) or existing_ifaces_map.get(bridge_name.replace("-", " "))
                if nb_member and nb_bridge:
                    bridge_id_val = getattr(nb_bridge, 'id', getattr(nb_bridge, 'pk', None))
                    current_bridge_id = getattr(nb_member, 'bridge', None)
                    if hasattr(current_bridge_id, 'id'):
                        current_bridge_id = current_bridge_id.id
                    elif isinstance(current_bridge_id, dict):
                        current_bridge_id = current_bridge_id.get('id')

                    if current_bridge_id != bridge_id_val:
                        print(f"[➔] Associando Porta Membro {nb_member.name} -> Bridge {nb_bridge.name}")
                        nb_member.bridge = bridge_id_val
                        try:
                            nb_member.save()
                        except Exception as b_err:
                            print(f"[!] Aviso ao associar bridge em {nb_member.name}: {b_err}")

        # Vincular interfaces membros às suas respectivas LAGs
        lag_expected_members = {}  # lag_id -> {interface_ids que DEVEM estar nesta LAG}
        for lag in data['lags']:
            lag_name = lag['name']
            nb_lag = existing_ifaces_map.get(lag_name) or existing_ifaces_map.get(lag_name.replace(" ", "-")) or existing_ifaces_map.get(lag_name.replace("-", " "))
            if not nb_lag or not lag.get('members'):
                continue

            lag_id_val = getattr(nb_lag, 'id', getattr(nb_lag, 'pk', None))
            if lag_id_val is None:
                continue
            expected_ids = set()

            for member_name in lag['members']:
                nb_member = existing_ifaces_map.get(member_name) or existing_ifaces_map.get(member_name.replace(" ", "-")) or existing_ifaces_map.get(member_name.replace("-", " "))
                if not nb_member:
                    continue

                # O NetBox recusa (HTTP 400) interfaces virtuais/loopback/LAG como membros de uma LAG
                member_type = getattr(nb_member.type, 'value', str(nb_member.type or '')).lower()
                member_lname = str(getattr(nb_member, 'name', '') or '').lower()
                if member_type in ('virtual', 'lag') or member_lname.startswith(('loopback', 'mgmt', 'vlan')) or 'loopback' in member_lname:
                    print(f"[i] Ignorando '{nb_member.name}' como membro da LAG {nb_lag.name}: tipo '{member_type}' nao suporta LAG parent.")
                    continue

                member_id = getattr(nb_member, 'id', getattr(nb_member, 'pk', None))
                if member_id is not None:
                    expected_ids.add(int(member_id))

                current_lag_id = lag_parent_id(nb_member)
                if current_lag_id != lag_id_val:
                    print(f"[➔] Associando membro physical {nb_member.name} -> LAG {nb_lag.name}")
                    nb_member.lag = lag_id_val
                    try:
                        nb_member.save()
                    except Exception as lag_err:
                        print(f"[!] Aviso ao associar {nb_member.name} a LAG {nb_lag.name}: {lag_err}")

            lag_expected_members[int(lag_id_val)] = expected_ids

        # Self-healing: remove das LAGs do NetBox os membros que nao constam mais na config do equipamento.
        # Sem isso, membros de execucoes antigas (ou removidos do equipamento) ficariam presos para sempre,
        # pois o sync apenas adiciona membros. So atua em LAGs que tiveram membros identificados no parse.
        if lag_expected_members:
            for nb_iface_all in nb.dcim.interfaces.filter(device_id=device_id):
                cur_lag_id = lag_parent_id(nb_iface_all)
                if cur_lag_id is None or not str(cur_lag_id).isdigit():
                    continue
                expected = lag_expected_members.get(int(cur_lag_id))
                if expected is None:
                    continue
                if_id = getattr(nb_iface_all, 'id', getattr(nb_iface_all, 'pk', None))
                if if_id is not None and int(if_id) not in expected:
                    print(f"[➔] Removendo membro obsoleto '{nb_iface_all.name}' da LAG {cur_lag_id} (nao consta na config do equipamento)")
                    nb_iface_all.lag = None
                    try:
                        nb_iface_all.save()
                    except Exception as stale_err:
                        print(f"[!] Aviso ao remover '{nb_iface_all.name}' da LAG {cur_lag_id}: {stale_err}")

    # 5.1. Sincronizar Inventory Items (Transceivers)
    if 'transceivers' in sync_modules and data.get('inventory_items'):
        for item in data['inventory_items']:
            if_target = existing_ifaces_map.get(item['interface']) or existing_ifaces_map.get(item['interface'].replace(" ", "-")) or existing_ifaces_map.get(item['interface'].replace("-", " "))
            if if_target:
                if_id = getattr(if_target, 'id', getattr(if_target, 'pk', None))
                mfg_name = item.get('manufacturer', '')
                mfg_obj = None
                if mfg_name:
                    slug_mfg = re.sub(r'[^\w\-]', '_', mfg_name.lower())
                    mfg_obj = safe_get(nb.dcim.manufacturers, name=mfg_name) or safe_get(nb.dcim.manufacturers, slug=slug_mfg)
                    if not mfg_obj:
                        all_mfgs = list(nb.dcim.manufacturers.all())
                        mfg_obj = next((m for m in all_mfgs if m.name.lower() == mfg_name.lower() or m.slug == slug_mfg), None)
                    if not mfg_obj:
                        print(f"[+] Criando Fabricante '{mfg_name}' no NetBox...")
                        try:
                            mfg_obj = nb.dcim.manufacturers.create(name=mfg_name, slug=slug_mfg)
                        except Exception as mfg_err:
                            mfg_obj = safe_get(nb.dcim.manufacturers, slug=slug_mfg) or safe_get(nb.dcim.manufacturers, name=mfg_name)

                mfg_id = getattr(mfg_obj, 'id', getattr(mfg_obj, 'pk', None)) if mfg_obj else None

                inv_name = item['name']
                alt_name = f"Transceiver-{hostname}"
                existing_items = list(nb.dcim.inventory_items.filter(device_id=device_id, component_id=if_id))
                if not existing_items:
                    existing_items = list(nb.dcim.inventory_items.filter(device_id=device_id, name=inv_name))
                if not existing_items:
                    existing_items = list(nb.dcim.inventory_items.filter(device_id=device_id, name=alt_name))

                if not existing_items:
                    print(f"[+] Criando Inventory Item '{inv_name}' (Transceiver) no NetBox...")
                    create_data = {
                        'device': device_id,
                        'name': inv_name,
                        'component_type': 'dcim.interface',
                        'component_id': if_id,
                        'part_id': item.get('part_id', ''),
                        'serial': item.get('serial', '')
                    }
                    if mfg_id:
                        create_data['manufacturer'] = mfg_id
                    nb.dcim.inventory_items.create(**create_data)
                else:
                    inv_item = existing_items[0]
                    need_up = False
                    if mfg_id and getattr(inv_item.manufacturer, 'id', None) != mfg_id:
                        inv_item.manufacturer = mfg_id
                        need_up = True
                    if item.get('part_id') and inv_item.part_id != item['part_id']:
                        inv_item.part_id = item['part_id']
                        need_up = True
                    if item.get('serial') and inv_item.serial != item['serial']:
                        inv_item.serial = item['serial']
                        need_up = True
                    if need_up:
                        inv_item.save()

    # 6. Sincronizar Enderecos IP na VRF-Global
    if any(m in sync_modules for m in ['ips', 'ip', 'ipv4', 'ipv6']):
        for ip_info in data['ips']:
            target_name = ip_info['interface']
            iface_target = existing_ifaces_map.get(target_name) or existing_ifaces_map.get(target_name.replace(" ", "-")) or existing_ifaces_map.get(target_name.replace("-", " ")) or find_netbox_interface(nb, device_id, target_name)
            if iface_target:
                iface_id = getattr(iface_target, 'id', getattr(iface_target, 'pk', None))
                raw_ip_str = ip_info['address']
                clean_host_ip = raw_ip_str.split('/')[0]

                # Se for Network ID (ex: 2001:db8:1::/64 ou 10.0.0.0/24), não deve ser atribuído à interface
                if raw_ip_str.endswith('::/64') or raw_ip_str.endswith('.0/24'):
                    continue

                # 1. Busca exata pelo endereço completo (ex: fd10:1:20::254/64)
                ip_obj = safe_get(nb.ipam.ip_addresses, address=raw_ip_str, vrf_id=vrf_id) or safe_get(nb.ipam.ip_addresses, address=raw_ip_str)

                # 2. Se não encontrou, busca por filter ignorando o prefixo da máscara (procura pelo Host IP puro ex: fd10:1:20::254)
                if not ip_obj:
                    found_ips = list(nb.ipam.ip_addresses.filter(address=clean_host_ip, vrf_id=vrf_id)) or list(nb.ipam.ip_addresses.filter(address=clean_host_ip))
                    if found_ips:
                        ip_obj = found_ips[0]

                try:
                    if not ip_obj:
                        print(f"[+] Criando IP {ip_info['address']} na VRF '{vrf_name}' (Interface: {iface_target.name})")
                        ip_obj = nb.ipam.ip_addresses.create(
                            address=ip_info['address'],
                            vrf=vrf_id,
                            assigned_object_type='dcim.interface',
                            assigned_object_id=iface_id,
                            status='active'
                        )
                    else:
                        need_ip_save = False
                        if ip_obj.address != raw_ip_str:
                            print(f"[➔] Atualizando máscara do IP {ip_obj.address} -> {raw_ip_str}")
                            ip_obj.address = raw_ip_str
                            need_ip_save = True

                        cur_ass_type = getattr(ip_obj, 'assigned_object_type', None)
                        cur_ass_id = getattr(ip_obj, 'assigned_object_id', None)
                        if not cur_ass_id and isinstance(getattr(ip_obj, 'assigned_object', None), dict):
                            cur_ass_id = ip_obj.assigned_object.get('id')
                        elif not cur_ass_id and hasattr(ip_obj, 'assigned_object') and getattr(ip_obj.assigned_object, 'id', None):
                            cur_ass_id = ip_obj.assigned_object.id

                        if cur_ass_type != 'dcim.interface':
                            ip_obj.assigned_object_type = 'dcim.interface'
                            need_ip_save = True
                        if cur_ass_id != iface_id:
                            print(f"[➔] Atribuindo IP {ip_info['address']} à interface '{iface_target.name}' no NetBox...")
                            ip_obj.assigned_object_id = iface_id
                            need_ip_save = True

                        cur_vrf_id = getattr(ip_obj.vrf, 'id', ip_obj.vrf) if getattr(ip_obj, 'vrf', None) else None
                        if cur_vrf_id != vrf_id:
                            ip_obj.vrf = vrf_id
                            need_ip_save = True

                        if need_ip_save:
                            ip_obj.save()
                except Exception as ip_err:
                    print(f"[!] Aviso ao criar/atualizar IP {ip_info['address']} (Interface: {iface_target.name}): {ip_err}")
                    continue

                # Se for a interface loopback-0 (ou loopback 0), guarda o ID do IP para marcar como primario do dispositivo
                if 'loopback' in ip_info['interface'].lower():
                    ip_id = getattr(ip_obj, 'id', getattr(ip_obj, 'pk', None))
                    if ip_info['address'].find(':') != -1:
                        primary_ip6_id = ip_id
                    else:
                        primary_ip4_id = ip_id

        # Atribui os IPs primarios (v4/v6) ao equipamento no NetBox
        if primary_ip4_id or primary_ip6_id:
            need_ip_save = False
            cur_p4 = getattr(device.primary_ip4, 'id', getattr(device.primary_ip4, 'pk', None))
            cur_p6 = getattr(device.primary_ip6, 'id', getattr(device.primary_ip6, 'pk', None))

            if primary_ip4_id and cur_p4 != primary_ip4_id:
                device.primary_ip4 = primary_ip4_id
                need_ip_save = True
            if primary_ip6_id and cur_p6 != primary_ip6_id:
                device.primary_ip6 = primary_ip6_id
                need_ip_save = True

            if need_ip_save:
                print(f"[➔] Definindo IP(s) Primário(s) na Loopback para o equipamento {hostname}...")
                try:
                    device.save()
                except Exception as dev_ip_err:
                    print(f"[!] Aviso ao definir IP primário no equipamento {hostname}: {dev_ip_err}")

    # 6.1. Sincronizar Grupos FHRP (VRRP)
    if 'vrrp' in sync_modules and data.get('vrrp_groups'):
        fhrp_endpoint = getattr(nb, 'ipam', None) and getattr(nb.ipam, 'fhrp_groups', None)
        fhrp_ass_endpoint = getattr(nb, 'ipam', None) and getattr(nb.ipam, 'fhrp_group_assignments', None)
        
        if fhrp_endpoint:
            vrf_id = getattr(vrf_obj, 'id', getattr(vrf_obj, 'pk', None))
            for vrrp in data['vrrp_groups']:
                protocol_str = 'vrrp2' if 'v2' in vrrp.get('version', '').lower() else 'vrrp3'
                vr_id = vrrp['vr_id']
                vip_addr = vrrp.get('virtual_ip')
                vrrp_name = vrrp.get('name') or vrrp.get('vrrp_interface')
                parent_if_name = vrrp.get('interface')

                # Se não houver IP Virtual configurado para o grupo VRRP, ignora a atribuição de IP
                if not vip_addr:
                    continue

                # Garante que o IP virtual existe na VRF
                ip_vrrp_obj = safe_get(nb.ipam.ip_addresses, address=vip_addr, vrf_id=vrf_id) or safe_get(nb.ipam.ip_addresses, address=vip_addr)
                if not ip_vrrp_obj:
                    # Se nao tiver mascara no IP virtual, adiciona /32 ou /128
                    full_vip = vip_addr if '/' in vip_addr else (f"{vip_addr}/32" if ':' not in vip_addr else f"{vip_addr}/128")
                    ip_vrrp_obj = safe_get(nb.ipam.ip_addresses, address=full_vip, vrf_id=vrf_id) or safe_get(nb.ipam.ip_addresses, address=full_vip)
                    if not ip_vrrp_obj:
                        print(f"[+] Criando IP Virtual VRRP {full_vip} no NetBox...")
                        ip_vrrp_obj = nb.ipam.ip_addresses.create(
                            address=full_vip,
                            vrf=vrf_id,
                            status='active',
                            description=f"VRRP Virtual IP Group {vr_id} ({vrrp_name or ''})"
                        )
                
                vip_id = getattr(ip_vrrp_obj, 'id', getattr(ip_vrrp_obj, 'pk', None))
                
                # Busca ou cria o FHRP Group
                fhrp_groups = list(fhrp_endpoint.filter(group_id=vr_id, protocol=protocol_str))
                fhrp_group = next((g for g in fhrp_groups if getattr(g, 'name', None) == vrrp_name), fhrp_groups[0] if fhrp_groups else None)
                if not fhrp_group:
                    print(f"[+] Criando FHRP Group VRRP (Protocol: {protocol_str}, Group ID: {vr_id}, Name: {vrrp_name or 'N/A'})...")
                    create_data = {
                        'protocol': protocol_str,
                        'group_id': vr_id
                    }
                    if vrrp_name:
                        create_data['name'] = vrrp_name
                        create_data['description'] = f"VRRP {vrrp_name}"
                    fhrp_group = fhrp_endpoint.create(**create_data)
                else:
                    need_fg_save = False
                    if vrrp_name:
                        if getattr(fhrp_group, 'name', None) != vrrp_name:
                            print(f"[➔] Atualizando Nome do FHRP Group {vr_id} -> '{vrrp_name}'...")
                            fhrp_group.name = vrrp_name
                            need_fg_save = True
                        if not getattr(fhrp_group, 'description', None):
                            fhrp_group.description = f"VRRP {vrrp_name}"
                            need_fg_save = True
                    if need_fg_save:
                        try:
                            fhrp_group.save()
                        except Exception as fg_err:
                            print(f"[!] Aviso ao atualizar nome no FHRP Group {vr_id}: {fg_err}")

                fg_id = getattr(fhrp_group, 'id', getattr(fhrp_group, 'pk', None))

                # No NetBox, a associação do Virtual IP ao FHRP Group é feita no objeto IPAddress
                if ip_vrrp_obj and fg_id:
                    if getattr(ip_vrrp_obj, 'assigned_object_type', None) != 'ipam.fhrpgroup' or getattr(ip_vrrp_obj, 'assigned_object_id', None) != fg_id:
                        print(f"[➔] Atribuindo IP Virtual {ip_vrrp_obj.address} ao FHRP Group {vr_id}...")
                        ip_vrrp_obj.assigned_object_type = 'ipam.fhrpgroup'
                        ip_vrrp_obj.assigned_object_id = fg_id
                        ip_vrrp_obj.save()

                # Atribui a interface física/L3/VRRP ao FHRP Group
                if fhrp_ass_endpoint and fhrp_group:
                    iface_target = None
                    if vrrp_name:
                        iface_target = existing_ifaces_map.get(vrrp_name) or existing_ifaces_map.get(vrrp_name.replace(" ", "-")) or existing_ifaces_map.get(vrrp_name.replace("-", " "))
                    if not iface_target and parent_if_name:
                        iface_target = existing_ifaces_map.get(parent_if_name) or existing_ifaces_map.get(parent_if_name.replace(" ", "-")) or existing_ifaces_map.get(parent_if_name.replace("-", " "))

                    if iface_target:
                        if_id = getattr(iface_target, 'id', getattr(iface_target, 'pk', None))
                        fg_id = getattr(fhrp_group, 'id', getattr(fhrp_group, 'pk', None))
                        ass_found = list(fhrp_ass_endpoint.filter(group_id=fg_id, interface_id=if_id))
                        if not ass_found:
                            print(f"[+] Vinculando FHRP Group {vr_id} à interface {iface_target.name} (Priority: {vrrp['priority']})...")
                            fhrp_ass_endpoint.create(
                                group=fg_id,
                                interface_type='dcim.interface',
                                interface_id=if_id,
                                priority=vrrp['priority']
                            )

    # 7. Sincronizar L2VPNs (VPWS / VPLS) - NetBox 3.5+ app 'vpn'
    if 'l2vpn' in sync_modules:
        try:
            l2vpn_endpoint = getattr(nb, 'vpn', None) and getattr(nb.vpn, 'l2vpns', None)
            if not l2vpn_endpoint:
                l2vpn_endpoint = getattr(nb.ipam, 'l2vpns', None)

            l2vpn_term_endpoint = getattr(nb, 'vpn', None) and getattr(nb.vpn, 'l2vpn_terminations', None)
            if not l2vpn_term_endpoint:
                l2vpn_term_endpoint = getattr(nb.ipam, 'l2vpn_terminations', None)

            if l2vpn_endpoint:
                for vpws in data['vpws']:
                    name = vpws['name']
                    pw_id_val = int(vpws['pw_id']) if vpws.get('pw_id') and str(vpws['pw_id']).isdigit() else None
                    slug_candidate = re.sub(r'[^\w\-]', '_', name.lower()).strip('_')
                    l2vpn = safe_get(l2vpn_endpoint, name=name) or safe_get(l2vpn_endpoint, slug=slug_candidate)
                    if not l2vpn:
                        print(f"[+] Criando L2VPN VPWS: {name} (Identifier/PW-ID: {pw_id_val})")
                        create_data = {
                            'name': name,
                            'slug': slug_candidate,
                            'type': 'vpws',
                            'status': 'active',
                            'description': f"PW-ID: {vpws['pw_id']} - Neighbor: {vpws['neighbor']}"
                        }
                        if pw_id_val is not None:
                            create_data['identifier'] = pw_id_val
                        try:
                            l2vpn = l2vpn_endpoint.create(**create_data)
                        except Exception as create_err:
                            l2vpn = safe_get(l2vpn_endpoint, slug=slug_candidate) or safe_get(l2vpn_endpoint, name=name)
                            if not l2vpn:
                                print(f"[!] Erro ao criar L2VPN VPWS '{name}': {create_err}")
                                continue
                    else:
                        need_up = False
                        if getattr(l2vpn.status, 'value', str(l2vpn.status)).lower() != 'active':
                            print(f"[➔] Reativando L2VPN VPWS '{name}' -> Status: Active")
                            l2vpn.status = 'active'
                            need_up = True

                        cur_ident = getattr(l2vpn, 'identifier', None)
                        cur_ident_int = int(cur_ident) if cur_ident is not None and str(cur_ident).isdigit() else None
                        if pw_id_val is not None and cur_ident_int != pw_id_val:
                            print(f"[➔] Atribuindo PW-ID {pw_id_val} ao Identifier da L2VPN VPWS '{name}'")
                            l2vpn.identifier = pw_id_val
                            need_up = True

                        if need_up:
                            l2vpn.save()

                    if l2vpn_term_endpoint and vpws.get('vlan_id') and vpws['vlan_id'] in nb_vlans:
                        vlan_obj = nb_vlans[vpws['vlan_id']]
                        vlan_id = getattr(vlan_obj, 'id', getattr(vlan_obj, 'pk', None))
                        l2vpn_id = getattr(l2vpn, 'id', getattr(l2vpn, 'pk', None))
                        terms_by_vlan = list(l2vpn_term_endpoint.filter(assigned_object_id=vlan_id))
                        if not terms_by_vlan:
                            print(f"[+] Vinculando L2VPN VPWS '{name}' a VLAN {vpws['vlan_id']}")
                            try:
                                l2vpn_term_endpoint.create(
                                    l2vpn=l2vpn_id,
                                    assigned_object_type='ipam.vlan',
                                    assigned_object_id=vlan_id
                                )
                            except Exception as term_err:
                                print(f"[!] Aviso ao vincular L2VPN VPWS '{name}' a VLAN {vpws['vlan_id']}: {term_err}")

                for vpls in data['vpls']:
                    name = vpls['name']
                    vpls_pw_id = vpls.get('vlan_id')
                    pw_id_val = int(vpls_pw_id) if vpls_pw_id and str(vpls_pw_id).isdigit() else None
                    slug_candidate = re.sub(r'[^\w\-]', '_', name.lower()).strip('_')
                    l2vpn = safe_get(l2vpn_endpoint, name=name) or safe_get(l2vpn_endpoint, slug=slug_candidate)
                    if not l2vpn:
                        print(f"[+] Criando L2VPN VPLS: {name} (Identifier/PW-ID: {pw_id_val})")
                        create_data = {
                            'name': name,
                            'slug': slug_candidate,
                            'type': 'vpls',
                            'status': 'active',
                            'description': f"VPLS {name}"
                        }
                        if pw_id_val is not None:
                            create_data['identifier'] = pw_id_val
                        try:
                            l2vpn = l2vpn_endpoint.create(**create_data)
                        except Exception as create_err:
                            l2vpn = safe_get(l2vpn_endpoint, slug=slug_candidate) or safe_get(l2vpn_endpoint, name=name)
                            if not l2vpn:
                                print(f"[!] Erro ao criar L2VPN VPLS '{name}': {create_err}")
                                continue
                    else:
                        need_up = False
                        if getattr(l2vpn.status, 'value', str(l2vpn.status)).lower() != 'active':
                            print(f"[➔] Reativando L2VPN VPLS '{name}' -> Status: Active")
                            l2vpn.status = 'active'
                            need_up = True

                        cur_ident = getattr(l2vpn, 'identifier', None)
                        cur_ident_int = int(cur_ident) if cur_ident is not None and str(cur_ident).isdigit() else None
                        if pw_id_val is not None and cur_ident_int != pw_id_val:
                            print(f"[➔] Atribuindo PW-ID {pw_id_val} ao Identifier da L2VPN VPLS '{name}'")
                            l2vpn.identifier = pw_id_val
                            need_up = True

                        if need_up:
                            l2vpn.save()

                    if vpls.get('vlan_id') and vpls['vlan_id'] in nb_vlans:
                        vlan_obj = nb_vlans[vpls['vlan_id']]
                        vlan_id = getattr(vlan_obj, 'id', getattr(vlan_obj, 'pk', None))

                        # Se o VPLS é QinQ, configura a VLAN de serviço (S-VLAN)
                        if vpls.get('is_qinq') or vpls.get('customer_vlans'):
                            cur_role = getattr(vlan_obj, 'qinq_role', None)
                            cur_role_val = cur_role.value if hasattr(cur_role, 'value') else str(cur_role or '')
                            if cur_role_val.lower() != 'svlan':
                                print(f"[➔] Configurando VLAN {vpls['vlan_id']} como Q-in-Q Service (S-VLAN)")
                                vlan_obj.qinq_role = 'svlan'
                                vlan_obj.save()

                            # Processa e associa as Customer VLANs (C-VLANs) a esta S-VLAN
                            for cvid in vpls.get('customer_vlans', []):
                                if cvid in nb_vlans:
                                    cvlan_obj = nb_vlans[cvid]
                                    cvlan_id = cvlan_obj.id if hasattr(cvlan_obj, 'id') else None
                                    cur_role = getattr(cvlan_obj, 'qinq_role', None)
                                    cur_role_val = cur_role.value if hasattr(cur_role, 'value') else cur_role
                                    cur_svlan = getattr(cvlan_obj, 'qinq_svlan', None)
                                    cur_svlan_id = getattr(cur_svlan, 'id', cur_svlan)

                                    if cur_role_val != 'cvlan' or cur_svlan_id != vlan_id:
                                        print(f"[➔] Configurando C-VLAN {cvid} (Role: Customer, SVLAN: {vpls['vlan_id']})")
                                        try:
                                            cvlan_obj.qinq_role = 'cvlan'
                                            cvlan_obj.qinq_svlan = vlan_id
                                            cvlan_obj.save()
                                        except Exception as err:
                                            print(f"[!] Erro ao salvar C-VLAN {cvid}: {err}")

                        # Vincula a Terminação L2VPN à Service VLAN se a VLAN ainda não estiver atribuída
                        if l2vpn_term_endpoint:
                            l2vpn_id = getattr(l2vpn, 'id', getattr(l2vpn, 'pk', None))
                            terms_by_vlan = list(l2vpn_term_endpoint.filter(assigned_object_id=vlan_id))
                            if not terms_by_vlan:
                                print(f"[+] Vinculando L2VPN VPLS '{name}' a VLAN {vpls['vlan_id']}")
                                try:
                                    l2vpn_term_endpoint.create(
                                        l2vpn=l2vpn_id,
                                        assigned_object_type='ipam.vlan',
                                        assigned_object_id=vlan_id
                                    )
                                except Exception as term_err:
                                    print(f"[!] Aviso ao vincular L2VPN VPLS '{name}' a VLAN {vpls['vlan_id']}: {term_err}")
        except Exception as e:
            print(f"[!] Erro ao sincronizar L2VPN: {e}")

    # 7.1. Sincronizar Túneis VPN em /vpn/tunnels/ (NetBox 3.5+ app 'vpn')
    if any(m in sync_modules for m in ['vpn_tunnels', 'vpn', 'tunnels', 'tunnel']) and data.get('vpn_tunnels'):
        try:
            tunnels_endpoint = getattr(nb, 'vpn', None) and getattr(nb.vpn, 'tunnels', None)
            tunnel_terms_endpoint = getattr(nb, 'vpn', None) and getattr(nb.vpn, 'tunnel_terminations', None)

            if tunnels_endpoint:
                for tun in data['vpn_tunnels']:
                    tun_name = tun['name']
                    encap_raw = (tun.get('encapsulation') or 'wireguard').lower()
                    status_raw = (tun.get('status') or 'active').lower()
                    desc = tun.get('description', '')

                    # Tenta obter o tunel existente pelo nome
                    existing_tuns = list(tunnels_endpoint.filter(name=tun_name))
                    tunnel_obj = existing_tuns[0] if existing_tuns else None

                    if not tunnel_obj:
                        print(f"[+] Criando Túnel VPN '{tun_name}' em /vpn/tunnels/ (Encapsulation: {encap_raw})...")
                        try:
                            create_kwargs = {
                                'name': tun_name,
                                'encapsulation': encap_raw,
                                'status': status_raw,
                                'description': desc
                            }
                            tunnel_obj = tunnels_endpoint.create(**create_kwargs)
                        except Exception as create_err:
                            print(f"[!] Erro ao criar Túnel VPN '{tun_name}': {create_err}")
                            existing_tuns = list(tunnels_endpoint.filter(name=tun_name))
                            tunnel_obj = existing_tuns[0] if existing_tuns else None
                    else:
                        need_up = False
                        cur_encap = getattr(tunnel_obj.encapsulation, 'value', str(tunnel_obj.encapsulation or '')).lower()
                        if cur_encap != encap_raw:
                            print(f"[➔] Atualizando Encapsulation do Túnel '{tun_name}' -> {encap_raw}")
                            tunnel_obj.encapsulation = encap_raw
                            need_up = True

                        cur_st = getattr(tunnel_obj.status, 'value', str(tunnel_obj.status or '')).lower()
                        if cur_st != status_raw:
                            tunnel_obj.status = status_raw
                            need_up = True

                        if desc and tunnel_obj.description != desc:
                            tunnel_obj.description = desc
                            need_up = True

                        if need_up:
                            try:
                                tunnel_obj.save()
                            except Exception as save_err:
                                print(f"[!] Aviso ao atualizar Túnel VPN '{tun_name}': {save_err}")

                    # Terminação do túnel no equipamento e interface (se especificada)
                    if tunnel_obj and tunnel_terms_endpoint:
                        tun_id = getattr(tunnel_obj, 'id', getattr(tunnel_obj, 'pk', None))
                        
                        # Verifica se a interface de terminação existe no dispositivo
                        parent_if_name = tun.get('interface')
                        iface_obj = None
                        if parent_if_name:
                            iface_obj = existing_ifaces_map.get(parent_if_name) or existing_ifaces_map.get(parent_if_name.replace(" ", "-")) or existing_ifaces_map.get(parent_if_name.replace("-", " "))

                        iface_id = getattr(iface_obj, 'id', getattr(iface_obj, 'pk', None)) if iface_obj else None

                        terms = list(tunnel_terms_endpoint.filter(tunnel_id=tun_id, device_id=device_id))
                        if not terms:
                            print(f"[+] Criando Tunnel Termination para '{tun_name}' no equipamento {hostname}" + (f" (Interface: {parent_if_name})" if parent_if_name else ""))
                            try:
                                term_kwargs = {
                                    'tunnel': tun_id,
                                    'role': 'peer',
                                    'termination_type': 'dcim.device',
                                    'termination_id': device_id
                                }
                                if iface_id:
                                    term_kwargs['termination_type'] = 'dcim.interface'
                                    term_kwargs['termination_id'] = iface_id

                                tunnel_terms_endpoint.create(**term_kwargs)
                            except Exception as term_err:
                                print(f"[!] Aviso ao criar Tunnel Termination para '{tun_name}': {term_err}")
        except Exception as tun_global_err:
            print(f"[!] Erro ao sincronizar Túneis VPN: {tun_global_err}")

    # 8. Sincronizar Conexões de Cabos (Cables) via LLDP Neighbors e Descrições de Interface
    if 'cables' in sync_modules:
        cable_endpoint = getattr(nb.dcim, 'cables', None)
        if cable_endpoint:
            created_cables_loc_ids = set()

            # 8.1 Processar vizinhos LLDP
            for lldp in data.get('lldp_neighbors', []):
                loc_if_name = lldp['local_interface']
                loc_iface = existing_ifaces_map.get(loc_if_name) or find_netbox_interface(nb, device_id, loc_if_name)
                if not loc_iface:
                    continue

                loc_if_id = getattr(loc_iface, 'id', getattr(loc_iface, 'pk', None))
                if getattr(loc_iface, 'cable', None) or loc_if_id in created_cables_loc_ids:
                    continue

                rem_dev_name = lldp.get('remote_device')
                if not rem_dev_name:
                    continue

                rem_dev = find_netbox_device(nb, rem_dev_name)
                if not rem_dev:
                    print(f"[!] Equipamento remoto '{rem_dev_name}' (vizinho LLDP de {loc_iface.name}) não encontrado no NetBox.")
                    continue

                rem_dev_id = getattr(rem_dev, 'id', getattr(rem_dev, 'pk', None))
                rem_if_name = lldp.get('remote_interface', '')
                rem_iface = find_netbox_interface(nb, rem_dev_id, rem_if_name)

                if not rem_iface:
                    print(f"[!] Interface remota '{rem_if_name}' no equipamento '{rem_dev.name}' não encontrada no NetBox.")
                    continue

                rem_if_id = getattr(rem_iface, 'id', getattr(rem_iface, 'pk', None))
                if getattr(rem_iface, 'cable', None):
                    print(f"[!] Interface remota '{rem_iface.name}' ({rem_dev.name}) já possui um cabo conectado no NetBox.")
                    continue

                cable_type = 'smf' if ('hundred' in loc_if_name or 'ten' in loc_if_name or 'twenty' in loc_if_name) else 'cat6'
                loc_abb = abbreviate_ifname(loc_iface.name)
                rem_abb = abbreviate_ifname(rem_iface.name)
                cable_label = f"{hostname} {loc_abb} <-> {rem_dev.name} {rem_abb}"[:100]
                print(f"[+] Criando Cabo LLDP: {loc_abb} ({hostname}) <---> {rem_abb} ({rem_dev.name})...")
                try:
                    cable_endpoint.create(
                        a_terminations=[{'object_type': 'dcim.interface', 'object_id': loc_if_id}],
                        b_terminations=[{'object_type': 'dcim.interface', 'object_id': rem_if_id}],
                        type=cable_type,
                        status='connected',
                        label=cable_label,
                        description=f"LLDP auto-discovered: {cable_label}"
                    )
                    created_cables_loc_ids.add(loc_if_id)
                except Exception as cable_err:
                    print(f"[!] Aviso ao criar cabo LLDP: {cable_err}")

            # 8.2 Fallback: Processar descrições de interface física com referências a equipamentos no NetBox
            all_phys = data.get('interfaces_physical', [])
            for phys in all_phys:
                loc_if_name = phys['name']
                desc = phys.get('description', '')
                if not desc:
                    continue

                loc_iface = existing_ifaces_map.get(loc_if_name) or find_netbox_interface(nb, device_id, loc_if_name)
                if not loc_iface:
                    continue

                loc_if_id = getattr(loc_iface, 'id', getattr(loc_iface, 'pk', None))
                if getattr(loc_iface, 'cable', None) or loc_if_id in created_cables_loc_ids:
                    continue

                desc_tokens = re.findall(r'[\w\.\-]+', desc)
                target_dev = None
                for token in desc_tokens:
                    if len(token) < 3 or token.upper() in ('PTP', 'LINK', 'TO', 'CONEXAO', 'SW', 'SWITCH', 'POP', 'INTERCONEXAO', 'SERVICO', 'UPLINK', 'PORT', 'INT', 'INTERFACE', 'BRAS', 'NET'):
                        continue
                    devs = list(nb.dcim.devices.filter(name=token))
                    if devs and getattr(devs[0], 'id', None) != device_id:
                        target_dev = devs[0]
                        break

                if not target_dev:
                    continue

                rem_dev_id = getattr(target_dev, 'id', getattr(target_dev, 'pk', None))
                rem_iface = find_netbox_interface(nb, rem_dev_id, loc_if_name)
                if not rem_iface:
                    rem_ifaces = list(nb.dcim.interfaces.filter(device_id=rem_dev_id))
                    rem_iface = next((i for i in rem_ifaces if not getattr(i, 'cable', None) and hostname.lower() in (i.description or '').lower()), None)

                if rem_iface:
                    rem_if_id = getattr(rem_iface, 'id', getattr(rem_iface, 'pk', None))
                    if not getattr(rem_iface, 'cable', None):
                        cable_type = 'smf' if ('hundred' in loc_if_name or 'ten' in loc_if_name or 'twenty' in loc_if_name) else 'cat6'
                        loc_abb = abbreviate_ifname(loc_iface.name)
                        rem_abb = abbreviate_ifname(rem_iface.name)
                        cable_label = f"{hostname} {loc_abb} <-> {target_dev.name} {rem_abb}"[:100]
                        print(f"[+] Criando Cabo (via Descrição): {loc_abb} ({hostname}) <---> {rem_abb} ({target_dev.name})...")
                        try:
                            cable_endpoint.create(
                                a_terminations=[{'object_type': 'dcim.interface', 'object_id': loc_if_id}],
                                b_terminations=[{'object_type': 'dcim.interface', 'object_id': rem_if_id}],
                                type=cable_type,
                                status='connected',
                                label=cable_label,
                                description=f"Description auto-discovered: {cable_label}"
                            )
                            created_cables_loc_ids.add(loc_if_id)
                        except Exception as cable_err:
                            print(f"[!] Aviso ao criar cabo via descrição: {cable_err}")

    print("\n[✔] Sincronizacao concluida com sucesso!")

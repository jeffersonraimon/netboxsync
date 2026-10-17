#!/usr/bin/env python3
"""
NetBox Automation CLI (Multi-Vendor Driver Architecture)
========================================================
Permite ler configuração a partir de um arquivo local ('show running') ou conectar diretamente via SSH
a equipamentos de diversos fabricantes (Datacom DmOS, Cisco, Huawei, etc.), sincronizando com o NetBox.
"""

import sys
import os
import argparse
import config
from drivers import get_driver
from netbox_sync.sync_engine import sync_to_netbox


def print_parsed_summary(parsed_data, default_device_type="", tenant_name=None):
    """
    Exibe no terminal um resumo completo contendo todos os dados extraídos pelo driver.
    """
    tags_str = ", ".join(sorted(parsed_data.get('tags') or [])) or "Nenhuma"
    print("\n================ RESUMO DAS INFORMAÇÕES EXTRAÍDAS ================")
    print(f" Hostname               : {parsed_data.get('hostname') or 'Não identificado'}")
    if tenant_name:
        print(f" Tenant (Cliente)       : {tenant_name}")
    print(f" Número de Série        : {parsed_data.get('serial') or 'Não identificado'}")
    print(f" Modelo Identificado    : {parsed_data.get('model') or default_device_type}")
    print(f" Tags / Protocolos      : {tags_str}")
    print(f" Interfaces Físicas     : {len(parsed_data.get('interfaces_physical', []))}")
    print(f" Interfaces L3          : {len(parsed_data.get('interfaces_l3', []))}")
    print(f" LAGs (Aggregations)    : {len(parsed_data.get('lags', []))}")
    print(f" Endereços IP           : {len(parsed_data.get('ips', []))}")
    print(f" VLANs Identificadas    : {len(parsed_data.get('vlans', {}))}")
    print(f" Transceivers / Invent. : {len(parsed_data.get('inventory_items', []))}")
    print(f" VPWS / VPLS            : {len(parsed_data.get('vpws', []))} / {len(parsed_data.get('vpls', []))}")
    print(f" Grupos VRRP / FHRP     : {len(parsed_data.get('vrrp_groups', []))}")
    print(f" Vizinhos LLDP          : {len(parsed_data.get('lldp_neighbors', []))}")
    print("==================================================================\n")


def process_single_host(host, args, password, driver):
    """
    Processa a coleta via SSH e sincronização no NetBox para um único host/IP utilizando o Driver selecionado.
    Retorna uma tupla (sucesso: bool, hostname/detalhe: str).
    """
    ssh_port = args.port
    target_host = host

    # Suporta formato IP:Porta ou hostname:Porta (ex: 192.168.1.1:2222)
    if ":" in host and not host.startswith("["):
        parts = host.rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            target_host = parts[0]
            ssh_port = int(parts[1])

    print(f"\n{'='*70}")
    print(f"[*] INICIANDO PROCESSAMENTO DO HOST: {target_host}:{ssh_port} (Driver: {driver.driver_name})")
    print(f"{'='*70}")

    try:
        ssh_outputs = driver.fetch_data(
            host=target_host,
            username=args.username,
            password=password,
            port=ssh_port,
            debug=getattr(args, 'debug', False)
        )
    except Exception as e:
        print(f"[!] Erro SSH ao conectar no host {host}: {e}")
        return False, f"Falha SSH: {e}"

    # Parsing dos Dados
    print(f"\n[*] Analisando dados do equipamento utilizando driver '{driver.driver_slug}'...")
    parsed_data = driver.parse_data(ssh_outputs)

    hostname = parsed_data.get('hostname')
    has_content = bool(parsed_data.get('interfaces_physical') or parsed_data.get('vlans') or parsed_data.get('ips') or parsed_data.get('lags') or ssh_outputs)
    if not has_content or not hostname:
        err_msg = "Configuração vazia ou hostname não encontrado"
        print(f"[!] Erro ao processar {host}: {err_msg}")
        return False, err_msg

    # Resumo dos Dados Coletados
    print_parsed_summary(parsed_data, default_device_type=args.device_type, tenant_name=args.tenant)

    if args.dry_run:
        print(f"[i] Modo Dry-Run ativo para {hostname}. Nenhuma alteração foi realizada no NetBox.")
        return True, f"{hostname} (Dry-Run)"

    verify_ssl = not (args.insecure or config.DISABLE_SSL_VERIFY)
    modules_list = [m.strip().lower() for m in args.sync_modules.split(",")] if args.sync_modules else ["all"]
    try:
        sync_res = sync_to_netbox(
            parsed_data,
            url=args.url,
            token=args.token,
            site_name=args.site,
            device_role=args.role,
            device_type_model=args.device_type,
            tenant_name=args.tenant,
            verify_ssl=verify_ssl,
            sync_modules=modules_list,
            driver=driver
        )
        reassigned_ips = sync_res.get('reassigned_ips', []) if isinstance(sync_res, dict) else []
        reassigned_roles = sync_res.get('reassigned_roles', []) if isinstance(sync_res, dict) else []
        return True, hostname, reassigned_ips, reassigned_roles
    except Exception as sync_err:
        print(f"[!] Erro ao sincronizar {hostname} com NetBox: {sync_err}")
        return False, f"Erro NetBox: {sync_err}", [], []


def create_parser():
    parser = argparse.ArgumentParser(description="Automação & Sincronização Multimarcas (Multi-Vendor) -> NetBox")

    # Driver de Fabricante
    parser.add_argument(
        "--driver", "-d-driver",
        default="dmos",
        help="Nome/slug do driver de fabricante/SO (ex: dmos, cisco_ios, huawei_vrp). Padrão: dmos"
    )

    # Grupo de entradas: Arquivo Local OR SSH Single Host OR Lista de IPs/Hosts
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--file", "-f", help="Caminho para o arquivo contendo a saída de configurações/comandos locais")
    input_group.add_argument("--host", "-H", help="Endereço IP ou FQDN do equipamento para conectar via SSH")
    input_group.add_argument("--hosts-file", "-F", help="Caminho para um arquivo txt contendo uma lista de IPs/Hosts (um por linha)")

    # Credenciais SSH
    ssh_group = parser.add_argument_group("Opções de Conexão SSH")
    ssh_group.add_argument("--username", "-u", help="Usuário SSH do equipamento", default=os.getenv("SSH_USER", "admin"))
    ssh_group.add_argument("--password", "-p", help="Senha SSH do equipamento", default=os.getenv("SSH_PASS", ""))
    ssh_group.add_argument("--port", "-P", type=int, help="Porta SSH remota (Padrão: 22 ou porta via env SSH_PORT)", default=int(os.getenv("SSH_PORT", "22")))
    ssh_group.add_argument("--debug", "-d", action="store_true", help="Ativar logs de debug SSH detalhados")

    # Opções NetBox
    nb_group = parser.add_argument_group("Opções NetBox & Equipamento")
    nb_group.add_argument("--url", help="URL do NetBox", default=config.NETBOX_URL)
    nb_group.add_argument("--token", help="Token API do NetBox", default=config.NETBOX_TOKEN)
    nb_group.add_argument("--site", "-s", help="Nome/Slug do Site (POP) no NetBox (Ex: MTSJ, SSA, BA-POP01)", default=config.DEFAULT_SITE_NAME)
    nb_group.add_argument("--role", "-r", help="Device Role no NetBox (Ex: SWITCH, Router, Edge)", default=config.DEFAULT_DEVICE_ROLE)
    nb_group.add_argument("--device-type", "--model", "-m", dest="device_type", help="Modelo / Device Type no NetBox (Ex: DM4380 12XS+3CX, DM4100)", default=config.DEFAULT_DEVICE_TYPE)
    nb_group.add_argument("--tenant", "-t", help="Nome/Slug do Tenant (Cliente) no NetBox para vincular aos objetos criados", default=getattr(config, 'DEFAULT_TENANT', None))
    nb_group.add_argument("--insecure", "-k", action="store_true", help="Ignorar verificação de certificado SSL (auto-assinado)")
    nb_group.add_argument("--dry-run", action="store_true", help="Apenas faz o parse e exibe o resumo sem alterar o NetBox")
    nb_group.add_argument(
        "--sync-modules", "-m-sync",
        help="Módulos específicos a sincronizar separados por vírgula (ex: vlans,interfaces,ips,vrrp,l2vpn,cables,transceivers) ou 'all'. Padrão: all",
        default="all"
    )

    return parser


def main():
    parser = create_parser()
    args = parser.parse_args()

    # Instancia o Driver correto via Factory
    try:
        driver = get_driver(args.driver)
    except ValueError as err:
        print(f"[!] Erro ao carregar driver: {err}")
        sys.exit(1)

    # Solicita senha SSH uma única vez caso não informada
    if (args.host or args.hosts_file) and not args.password:
        import getpass
        args.password = getpass.getpass(f"Senha SSH para {args.username}: ")

    results = []

    # 1. Processamento via Lista de Hosts
    if args.hosts_file:
        if not os.path.exists(args.hosts_file):
            print(f"[!] Erro: Arquivo de lista '{args.hosts_file}' não encontrado.")
            sys.exit(1)

        with open(args.hosts_file, 'r', encoding='utf-8') as file:
            hosts = [line.strip() for line in file if line.strip() and not line.strip().startswith('#')]

        if not hosts:
            print(f"[!] Nenhum IP/Host válido encontrado em '{args.hosts_file}'.")
            sys.exit(1)

        print(f"\n[*] Lista carregada com {len(hosts)} equipamentos para processar (Driver: {driver.driver_name}).\n")

        for ip_host in hosts:
            res_tuple = process_single_host(ip_host, args, args.password, driver)
            success, info = res_tuple[0], res_tuple[1]
            reassigned_ips = res_tuple[2] if len(res_tuple) > 2 else []
            reassigned_roles = res_tuple[3] if len(res_tuple) > 3 else []
            results.append({'host': ip_host, 'success': success, 'info': info, 'reassigned_ips': reassigned_ips, 'reassigned_roles': reassigned_roles})

    # 2. Processamento via Host Único via SSH
    elif args.host:
        res_tuple = process_single_host(args.host, args, args.password, driver)
        success, info = res_tuple[0], res_tuple[1]
        reassigned_ips = res_tuple[2] if len(res_tuple) > 2 else []
        reassigned_roles = res_tuple[3] if len(res_tuple) > 3 else []
        results.append({'host': args.host, 'success': success, 'info': info, 'reassigned_ips': reassigned_ips, 'reassigned_roles': reassigned_roles})

    # 3. Processamento via Arquivo de Saída Local
    else:
        if not os.path.exists(args.file):
            print(f"[!] Erro: Arquivo '{args.file}' não encontrado.")
            sys.exit(1)

        with open(args.file, 'r', encoding='utf-8', errors='ignore') as f:
            config_content = f.read()

        print(f"\n[*] Analisando dados do arquivo local usando driver '{driver.driver_slug}'...")
        parsed_data = driver.parse_data(config_content)

        hostname = parsed_data.get('hostname') or args.file
        print_parsed_summary(parsed_data, default_device_type=args.device_type, tenant_name=args.tenant)

        if args.dry_run:
            print("[i] Modo Dry-Run ativo. Nenhuma alteração foi realizada no NetBox.")
            results.append({'host': args.file, 'success': True, 'info': f"{hostname} (Dry-Run)", 'reassigned_ips': [], 'reassigned_roles': []})
        else:
            verify_ssl = not (args.insecure or config.DISABLE_SSL_VERIFY)
            modules_list = [m.strip().lower() for m in args.sync_modules.split(",")] if args.sync_modules else ["all"]
            try:
                sync_res = sync_to_netbox(
                    parsed_data,
                    url=args.url,
                    token=args.token,
                    site_name=args.site,
                    device_role=args.role,
                    device_type_model=args.device_type,
                    tenant_name=args.tenant,
                    verify_ssl=verify_ssl,
                    sync_modules=modules_list,
                    driver=driver
                )
                reassigned_ips = sync_res.get('reassigned_ips', []) if isinstance(sync_res, dict) else []
                reassigned_roles = sync_res.get('reassigned_roles', []) if isinstance(sync_res, dict) else []
                results.append({'host': args.file, 'success': True, 'info': hostname, 'reassigned_ips': reassigned_ips, 'reassigned_roles': reassigned_roles})
            except Exception as sync_err:
                results.append({'host': args.file, 'success': False, 'info': str(sync_err), 'reassigned_ips': [], 'reassigned_roles': []})

    # 4. EXIBIÇÃO DO RESUMO FINAL DA EXECUÇÃO
    print("\n" + "="*80)
    print("                      RESUMO FINAL DA EXECUÇÃO                     ")
    print("="*80)
    print(f"{'HOST / IP':<25} | {'STATUS':<12} | {'DETALHES / HOSTNAME':<35}")
    print("-" * 80)
    total_sucesso = 0
    total_falha = 0
    all_reassigned_ips = []
    all_reassigned_roles = []

    for r in results:
        status_str = "[ ✔ OK ]" if r['success'] else "[ ✖ ERRO ]"
        if r['success']:
            total_sucesso += 1
        else:
            total_falha += 1
        print(f"{r['host']:<25} | {status_str:<12} | {r['info']:<35}")
        for re_item in r.get('reassigned_ips', []):
            all_reassigned_ips.append({
                'host': r['host'],
                'hostname': r['info'],
                **re_item
            })
        for role_item in r.get('reassigned_roles', []):
            all_reassigned_roles.append({
                'host': r['host'],
                'hostname': r['info'],
                **role_item
            })

    print("="*80)
    print(f" Total Processados : {len(results)}")
    print(f" Sucesso           : {total_sucesso}")
    print(f" Falhas            : {total_falha}")

    # 5. ALERTA DE IPS REATRIBUÍDOS / POSSÍVEIS DUPLICIDADES
    if all_reassigned_ips:
        print("\n" + "!"*80)
        print("  ⚠️  ALERTA: IPS REATRIBUÍDOS ENTRE INTERFACES/DISPOSITIVOS (POSSÍVEL DUPLICIDADE)")
        print("!"*80)
        print(f"{'IP / PREFIXO':<22} | {'NOVO DESTINO':<26} | {'ORIGEM ANTERIOR NO NETBOX':<26}")
        print("-" * 80)
        for re_ip in all_reassigned_ips:
            ip_str = re_ip['ip']
            novo_dst = f"{re_ip['hostname']}:{re_ip['new_interface']}"
            origem = f"{re_ip['previous_device']}:{re_ip['previous_interface']}"
            print(f"{ip_str:<22} | {novo_dst:<26} | {origem:<26}")
        print("!"*80)

    # 6. ALERTA DE ROLES DE VLAN ATRIBUÍDAS / ALTERADAS
    if all_reassigned_roles:
        print("\n" + "!"*80)
        print("  ⚠️  ATENÇÃO: ROLES DE VLAN ATRIBUÍDAS OU ALTERADAS NO NETBOX")
        print("!"*80)
        print(f"{'VID':<6} | {'NOME DA VLAN':<30} | {'NOVA ROLE':<18} | {'ROLE ANTERIOR':<18}")
        print("-" * 80)
        for r_item in all_reassigned_roles:
            vid_str = str(r_item['vid'])
            vname = r_item['vlan_name'][:30]
            new_r = r_item['new_role']
            prev_r = r_item['previous_role']
            print(f"{vid_str:<6} | {vname:<30} | {new_r:<18} | {prev_r:<18}")
        print("!"*80)

    print("="*80 + "\n")


if __name__ == '__main__':
    main()

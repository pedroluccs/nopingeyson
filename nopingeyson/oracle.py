"""Cria o servidor (relay) do NoPingeyson de graça na Oracle Cloud (Always Free)
e fica tentando sozinho até a Oracle liberar vaga.

    python -m nopingeyson oracle

Pré-requisitos (uma vez): conta na Oracle Cloud e uma chave de API em
~/.oci/config (Perfil → Chaves de API → Adicionar chave → copiar a
configuração). Veja o README.

O que este comando faz, sempre dentro do limite "Always Free":
  1. cria (ou reaproveita) uma rede "nopingeyson-vcn" com a porta UDP do relay aberta;
  2. tenta criar a VM "nopingeyson-relay" (Ampere A1 1 OCPU/6 GB ou AMD E2.1.Micro)
     em cada zona da região; se a Oracle responder "sem capacidade", espera e
     tenta de novo, para sempre ou até --max-horas;
  3. a VM se instala sozinha (cloud-init roda o deploy/bootstrap.sh);
  4. espera o relay responder e grava o servidor na configuração do NoPingeyson.
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import socket
import time

from . import protocol as P

log = logging.getLogger("nopingeyson.oracle")

NAME = "nopingeyson-relay"
VCN_NAME = "nopingeyson-vcn"
BOOTSTRAP = "https://raw.githubusercontent.com/pedroluccs/nopingeyson/main/deploy/bootstrap.sh"
SHAPES = [("VM.Standard.A1.Flex", 1, 6), ("VM.Standard.E2.1.Micro", None, None)]
REGION_NAMES = {"sa-saopaulo-1": "São Paulo", "sa-vinhedo-1": "Vinhedo", "sa-santiago-1": "Santiago",
                "us-ashburn-1": "Virgínia", "us-phoenix-1": "Phoenix", "us-sanjose-1": "San José"}


def _say(cb, msg):
    log.info(msg)
    print(time.strftime("%H:%M:%S"), msg, flush=True)
    if cb:
        cb(msg)


# -- rede ------------------------------------------------------------------------------
def ensure_network(oci, config, compartment, port, cb=None):
    net = oci.core.VirtualNetworkClient(config)
    vcns = [v for v in net.list_vcns(compartment).data
            if v.display_name == VCN_NAME and v.lifecycle_state == "AVAILABLE"]
    if vcns:
        vcn = vcns[0]
    else:
        _say(cb, "Criando a rede (VCN)…")
        vcn = net.create_vcn(oci.core.models.CreateVcnDetails(
            compartment_id=compartment, display_name=VCN_NAME, cidr_blocks=["10.77.0.0/16"],
            dns_label="nopingeyson")).data
        vcn = oci.wait_until(net, net.get_vcn(vcn.id), "lifecycle_state", "AVAILABLE").data

    igws = net.list_internet_gateways(compartment, vcn_id=vcn.id).data
    if igws:
        igw = igws[0]
    else:
        igw = net.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(
            compartment_id=compartment, vcn_id=vcn.id, is_enabled=True,
            display_name="nopingeyson-igw")).data
        igw = oci.wait_until(net, net.get_internet_gateway(igw.id), "lifecycle_state", "AVAILABLE").data

    rt = net.get_route_table(vcn.default_route_table_id).data
    if not any(r.destination == "0.0.0.0/0" for r in rt.route_rules):
        net.update_route_table(rt.id, oci.core.models.UpdateRouteTableDetails(route_rules=rt.route_rules + [
            oci.core.models.RouteRule(destination="0.0.0.0/0", destination_type="CIDR_BLOCK",
                                      network_entity_id=igw.id)]))

    sl = net.get_security_list(vcn.default_security_list_id).data
    has_udp = any(r.protocol == "17" and r.udp_options and r.udp_options.destination_port_range
                  and r.udp_options.destination_port_range.min <= port <= r.udp_options.destination_port_range.max
                  for r in sl.ingress_security_rules)
    if not has_udp:
        _say(cb, f"Liberando a porta UDP {port} no firewall da Oracle…")
        rule = oci.core.models.IngressSecurityRule(
            protocol="17", source="0.0.0.0/0", description="NoPingeyson relay",
            udp_options=oci.core.models.UdpOptions(
                destination_port_range=oci.core.models.PortRange(min=port, max=port)))
        net.update_security_list(sl.id, oci.core.models.UpdateSecurityListDetails(
            ingress_security_rules=sl.ingress_security_rules + [rule]))

    subnets = [s for s in net.list_subnets(compartment, vcn_id=vcn.id).data if s.lifecycle_state == "AVAILABLE"]
    if subnets:
        return subnets[0]
    subnet = net.create_subnet(oci.core.models.CreateSubnetDetails(
        compartment_id=compartment, vcn_id=vcn.id, cidr_block="10.77.1.0/24",
        display_name="nopingeyson-subnet", dns_label="relay",
        route_table_id=vcn.default_route_table_id,
        security_list_ids=[vcn.default_security_list_id],
        prohibit_public_ip_on_vnic=False)).data
    return oci.wait_until(net, net.get_subnet(subnet.id), "lifecycle_state", "AVAILABLE").data


# -- VM ----------------------------------------------------------------------------------
def _image(oci, compute, compartment, shape):
    imgs = compute.list_images(compartment, operating_system="Canonical Ubuntu",
                               operating_system_version="22.04", shape=shape,
                               sort_by="TIMECREATED", sort_order="DESC").data
    imgs = [i for i in imgs if "Minimal" not in i.display_name] or imgs
    return imgs[0].id if imgs else None


def _existing(compute, compartment):
    for inst in compute.list_instances(compartment, display_name=NAME).data:
        if inst.lifecycle_state in ("PROVISIONING", "STARTING", "RUNNING"):
            return inst
    return None


def _public_ip(oci, config, compartment, inst):
    compute = oci.core.ComputeClient(config)
    net = oci.core.VirtualNetworkClient(config)
    for att in compute.list_vnic_attachments(compartment, instance_id=inst.id).data:
        vnic = net.get_vnic(att.vnic_id).data
        if vnic.public_ip:
            return vnic.public_ip
    return None


def probe_relay(host: str, port: int, key: bytes, timeout: float = 2.0) -> float | None:
    """Manda uma sonda assinada ao relay; devolve o RTT em ms ou None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        t0 = time.perf_counter()
        pkt = P.Packet(P.T_PROBE, os.urandom(8), 0, 0, [], P.probe_body(1, t0))
        s.sendto(pkt.encode(key), (host, port))
        data, _ = s.recvfrom(2048)
        back = P.Packet.decode(data, key)
        if back.type == P.T_PROBE_ACK:
            return round((time.perf_counter() - t0) * 1000, 1)
    except (OSError, P.BadPacket):
        return None
    finally:
        s.close()
    return None


def create_relay(key_hex: str, port: int = 7000, interval: int = 60, max_hours: float = 0,
                 config_file: str | None = None, profile: str = "DEFAULT", ssh_key: str | None = None,
                 cb=None) -> dict:
    import oci
    config = oci.config.from_file(config_file or oci.config.DEFAULT_LOCATION, profile)
    oci.config.validate_config(config)
    compartment = config["tenancy"]
    region = config["region"]
    key = P.load_key(key_hex)
    compute = oci.core.ComputeClient(config)
    identity = oci.identity.IdentityClient(config)
    ads = [a.name for a in identity.list_availability_domains(compartment).data]
    _say(cb, f"Região {region}, zonas: {', '.join(ads)}")

    inst = _existing(compute, compartment)
    if inst:
        _say(cb, f"Já existe uma VM {NAME} ({inst.lifecycle_state}); reaproveitando.")
    else:
        subnet = ensure_network(oci, config, compartment, port, cb)
        user_data = base64.b64encode(
            f"#!/bin/bash\ncurl -fsSL {BOOTSTRAP} | bash -s -- {key_hex} {port}\n".encode()).decode()
        metadata = {"user_data": user_data}
        if ssh_key:
            metadata["ssh_authorized_keys"] = ssh_key
        images = {shape: _image(oci, compute, compartment, shape) for shape, *_ in SHAPES}
        deadline = time.time() + max_hours * 3600 if max_hours else None
        skip: set[str] = set()
        attempt = 0
        while inst is None:
            attempt += 1
            for shape, ocpus, mem in SHAPES:
                if shape in skip or not images.get(shape):
                    continue
                for ad in ads:
                    details = oci.core.models.LaunchInstanceDetails(
                        availability_domain=ad, compartment_id=compartment, display_name=NAME,
                        shape=shape, metadata=metadata,
                        source_details=oci.core.models.InstanceSourceViaImageDetails(
                            image_id=images[shape], boot_volume_size_in_gbs=50),
                        create_vnic_details=oci.core.models.CreateVnicDetails(
                            subnet_id=subnet.id, assign_public_ip=True))
                    if ocpus:
                        details.shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
                            ocpus=ocpus, memory_in_gbs=mem)
                    try:
                        inst = compute.launch_instance(details).data
                        _say(cb, f"Vaga conseguida! VM criada ({shape}, {ad}).")
                        break
                    except oci.exceptions.ServiceError as e:
                        msg = (e.message or "").lower()
                        if e.status == 429:
                            _say(cb, "Oracle pediu para ir mais devagar; esperando 2 min…")
                            time.sleep(120)
                        elif "capacity" in msg:
                            _say(cb, f"Tentativa {attempt}: sem vaga para {shape} em {ad.split(':')[-1]}")
                        elif e.code == "LimitExceeded" or "limit" in msg:
                            _say(cb, f"Limite gratuito de {shape} já usado; pulando esse tipo.")
                            skip.add(shape)
                        else:
                            raise
                if inst:
                    break
            if inst:
                break
            if len(skip) == len(SHAPES):
                raise RuntimeError("Os dois tipos de VM gratuitos já estão em uso nesta conta.")
            if deadline and time.time() > deadline:
                raise RuntimeError("Tempo esgotado sem vaga; rode de novo mais tarde.")
            _say(cb, f"Sem vaga agora. Nova tentativa em {interval} s (Ctrl+C para parar).")
            time.sleep(interval)

    inst = oci.wait_until(compute, compute.get_instance(inst.id), "lifecycle_state", "RUNNING",
                          max_wait_seconds=900).data
    ip = _public_ip(oci, config, compartment, inst)
    _say(cb, f"VM ligada, IP público {ip}. Esperando o relay se instalar (até ~10 min)…")
    rtt = None
    for _ in range(120):
        rtt = probe_relay(ip, port, key)
        if rtt is not None:
            break
        time.sleep(5)
    if rtt is None:
        raise RuntimeError(f"A VM {ip} subiu, mas o relay não respondeu. Veja o README (porta UDP {port}).")
    region_name = REGION_NAMES.get(region, region)
    _say(cb, f"Relay respondendo: {rtt} ms até {region_name}.")
    return {"name": f"Oracle-{region.split('-')[1][:3].upper()}", "region": region_name,
            "host": ip, "port": port}


def main(argv=None):
    from .engine import load_config, save_config
    ap = argparse.ArgumentParser(prog="nopingeyson oracle",
                                 description="Cria o relay grátis na Oracle Cloud, tentando até ter vaga")
    ap.add_argument("--chave", help="chave dos servidores (hex); padrão: a da configuração ou uma nova")
    ap.add_argument("--porta", type=int, default=7000)
    ap.add_argument("--intervalo", type=int, default=60, help="segundos entre tentativas (padrão 60)")
    ap.add_argument("--max-horas", type=float, default=0, help="desiste depois de N horas (0 = nunca)")
    ap.add_argument("--oci-config", help="arquivo de configuração da Oracle (padrão ~/.oci/config)")
    ap.add_argument("--perfil", default="DEFAULT")
    ap.add_argument("--ssh", help="arquivo .pub para poder entrar na VM por SSH (opcional)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    cfg = load_config()
    key = args.chave or cfg.get("key")
    if not key:
        import secrets
        key = secrets.token_hex(32)
    ssh = open(args.ssh).read().strip() if args.ssh else None
    try:
        server = create_relay(key, args.porta, args.intervalo, args.max_horas, args.oci_config,
                              args.perfil, ssh)
    except KeyboardInterrupt:
        print("\nParado. Rode de novo quando quiser continuar tentando.")
        return 1
    cfg["relays"] = [r for r in cfg.get("relays", []) if r.get("host") != server["host"]] + [server]
    cfg["key"] = key
    save_config(cfg)
    print("\nPronto! Servidor salvo na configuração do NoPingeyson:", server)
    print("Para embutir no instalador, use este JSON no segredo NOPINGEYSON_SERVERS do GitHub:")
    import json
    print(json.dumps({"key": key, "servers": [server]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
grab_a1.py — Provisionamento forçado de instância OCI Always Free (VM.Standard.A1.Flex).

Roda em loop tentando criar a instância até obter sucesso, lidando com:
  - 500 "Out of host capacity" (e variantes capacity/unavailable/limit)
  - 429 TooManyRequests (sleep dobrado)
  - LimitExceeded de memória (auto-fallback 24GB -> 6GB)
  - 401/403 consecutivos (aborta após 10 e alerta no Telegram)

Ao obter sucesso, espera a VNIC, lê o IP público, e notifica via Telegram com
o comando SSH pronto.

Uso:
    python grab_a1.py                # loop infinito de provisionamento
    python grab_a1.py --test-notify  # apenas envia uma msg de teste no Telegram
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import oci
from oci.core.models import (
    CreateVnicDetails,
    InstanceSourceViaImageDetails,
    LaunchInstanceDetails,
    LaunchInstanceShapeConfigDetails,
)
from oci.exceptions import ServiceError

CAPACITY_PATTERN = re.compile(r"capacity|unavailable|limit", re.IGNORECASE)
AUTH_FAIL_LIMIT = 10
SHAPE = "VM.Standard.A1.Flex"
LOG_FILE = "grab_a1.log"


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("grab_a1")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        raise SystemExit(f"Variável de ambiente obrigatória ausente: {key}")
    return val  # type: ignore[return-value]


def notify_telegram(token: str | None, chat_id: str | None, text: str, log: logging.Logger) -> None:
    """Envia mensagem ao Telegram. Falha silenciosa (apenas loga)."""
    if not token or not chat_id:
        log.warning("Telegram não configurado — pulando notificação.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    ).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                log.warning("Telegram respondeu HTTP %s", resp.status)
    except Exception as e:  # rede instável não pode quebrar o loop
        log.warning("Falha ao enviar Telegram: %s", e)


def load_ssh_key(path: str, log: logging.Logger) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        raise SystemExit(f"Chave SSH pública não encontrada: {p}")
    key = p.read_text().strip()
    if not key.startswith(("ssh-rsa", "ssh-ed25519", "ecdsa-")):
        log.warning("Chave SSH em %s não parece formato OpenSSH público.", p)
    return key


def precheck_subnet(vnet_client: oci.core.VirtualNetworkClient, subnet_id: str, log: logging.Logger) -> None:
    """Aborta se a subnet não permitir IP público."""
    log.info("Pré-check: validando subnet %s ...", subnet_id)
    subnet = vnet_client.get_subnet(subnet_id).data
    if subnet.prohibit_public_ip_on_vnic:
        msg = f"CRÍTICO: subnet {subnet_id} bloqueia IP público (prohibit_public_ip_on_vnic=True)."
        log.error(msg)
        raise SystemExit(msg)
    log.info("Subnet OK — IP público permitido.")


def precheck_quotas(
    limits_client: oci.limits.LimitsClient,
    compartment_id: str,
    ad_name: str,
    boot_gb: int,
    log: logging.Logger,
) -> None:
    """Avisa sobre cotas de OCPU/memória A1 e aborta se boot volume excede storage disponível."""
    def avail(service: str, name: str) -> int | None:
        try:
            r = limits_client.get_resource_availability(
                service_name=service,
                limit_name=name,
                compartment_id=compartment_id,
                availability_domain=ad_name,
            ).data
            return getattr(r, "available", None) or getattr(r, "fractional_availability", None)
        except ServiceError as e:
            log.warning("Não consegui ler limit %s/%s: %s", service, name, e.code)
            return None

    ocpu = avail("compute", "standard-a1-core-count")
    mem = avail("compute", "standard-a1-memory-count")
    storage = avail("block-storage", "total-storage-gb")
    log.info("Cotas A1 disponíveis — OCPU: %s | RAM(GB): %s | Storage(GB): %s", ocpu, mem, storage)

    if storage is not None and storage < boot_gb:
        raise SystemExit(
            f"CRÍTICO: boot volume solicitado ({boot_gb}GB) excede storage disponível ({storage}GB)."
        )


def build_launch_details(
    *,
    compartment_id: str,
    ad_name: str,
    image_id: str,
    subnet_id: str,
    display_name: str,
    ssh_key: str,
    ocpus: int,
    memory_gb: int,
    boot_gb: int,
) -> LaunchInstanceDetails:
    return LaunchInstanceDetails(
        compartment_id=compartment_id,
        availability_domain=ad_name,
        shape=SHAPE,
        shape_config=LaunchInstanceShapeConfigDetails(
            ocpus=ocpus, memory_in_gbs=memory_gb
        ),
        display_name=display_name,
        source_details=InstanceSourceViaImageDetails(
            image_id=image_id,
            boot_volume_size_in_gbs=boot_gb,
        ),
        create_vnic_details=CreateVnicDetails(
            subnet_id=subnet_id,
            assign_public_ip=True,
        ),
        metadata={"ssh_authorized_keys": ssh_key},
    )


def fetch_public_ip(
    compute: oci.core.ComputeClient,
    vnet: oci.core.VirtualNetworkClient,
    compartment_id: str,
    instance_id: str,
    log: logging.Logger,
) -> tuple[str | None, str | None]:
    """Espera a VNIC estar attached, retorna (public_ip, private_ip)."""
    for attempt in range(1, 6):
        try:
            vas = compute.list_vnic_attachments(
                compartment_id=compartment_id, instance_id=instance_id
            ).data
            if vas:
                vnic = vnet.get_vnic(vas[0].vnic_id).data
                if vnic.public_ip:
                    return vnic.public_ip, vnic.private_ip
                log.info("VNIC encontrada mas sem IP público ainda (tentativa %d).", attempt)
            else:
                log.info("Sem VNIC attachment ainda (tentativa %d).", attempt)
        except ServiceError as e:
            log.warning("Erro lendo VNIC (tentativa %d): %s", attempt, e.code)
        time.sleep(15)
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-notify", action="store_true", help="envia msg teste e sai")
    args = parser.parse_args()

    log = setup_logging()

    tg_token = env("TELEGRAM_BOT_TOKEN")
    tg_chat = env("TELEGRAM_CHAT_ID")

    if args.test_notify:
        notify_telegram(tg_token, tg_chat, "✅ *grab_a1*: teste de notificação OK.", log)
        log.info("Mensagem de teste enviada — saindo.")
        return 0

    profile = env("OCI_PROFILE", "DEFAULT")
    compartment_id = env("OCI_COMPARTMENT_ID", required=True)
    subnet_id = env("OCI_SUBNET_ID", required=True)
    image_id = env("OCI_IMAGE_ID", required=True)
    ad_name = env("OCI_AD_NAME", required=True)
    display_name = env("OCI_DISPLAY_NAME", "a1-free-grabbed")
    ssh_path = env("OCI_SSH_KEY_PATH", "~/.ssh/id_rsa.pub")
    ocpus = int(env("OCI_OCPUS", "4"))
    memory_gb = int(env("OCI_MEMORY_GB", "24"))
    boot_gb = int(env("OCI_BOOT_VOLUME_GB", "50"))
    sleep_min = int(env("SLEEP_MIN", "30"))
    sleep_max = int(env("SLEEP_MAX", "60"))

    config = oci.config.from_file(profile_name=profile)
    oci.config.validate_config(config)
    compute = oci.core.ComputeClient(config)
    vnet = oci.core.VirtualNetworkClient(config)
    limits = oci.limits.LimitsClient(config)

    ssh_key = load_ssh_key(ssh_path, log)
    precheck_subnet(vnet, subnet_id, log)
    precheck_quotas(limits, compartment_id, ad_name, boot_gb, log)

    log.info(
        "Iniciando loop — shape=%s, OCPU=%d, RAM=%dGB, boot=%dGB, AD=%s",
        SHAPE, ocpus, memory_gb, boot_gb, ad_name,
    )

    attempt = 0
    auth_fail_streak = 0
    current_memory = memory_gb

    try:
        while True:
            attempt += 1
            details = build_launch_details(
                compartment_id=compartment_id,
                ad_name=ad_name,
                image_id=image_id,
                subnet_id=subnet_id,
                display_name=display_name,
                ssh_key=ssh_key,
                ocpus=ocpus,
                memory_gb=current_memory,
                boot_gb=boot_gb,
            )
            try:
                resp = compute.launch_instance(details)
                instance = resp.data
                log.info("🎉 SUCESSO na tentativa #%d — instance OCID %s", attempt, instance.id)
                auth_fail_streak = 0

                pub_ip, priv_ip = fetch_public_ip(
                    compute, vnet, compartment_id, instance.id, log
                )

                msg = (
                    "🎉 *Instância A1 provisionada!*\n"
                    f"*Nome:* `{instance.display_name}`\n"
                    f"*Shape:* {SHAPE} — {ocpus} OCPU / {current_memory} GB RAM\n"
                    f"*AD:* {ad_name}\n"
                    f"*Public IP:* `{pub_ip or 'pendente'}`\n"
                    f"*Private IP:* `{priv_ip or 'n/d'}`\n"
                    f"*Tentativas:* {attempt}\n\n"
                    f"`ssh -i ~/.ssh/id_rsa opc@{pub_ip or '<IP>'}`"
                )
                notify_telegram(tg_token, tg_chat, msg, log)

                Path("success.json").write_text(
                    json.dumps(
                        {
                            "instance_id": instance.id,
                            "display_name": instance.display_name,
                            "public_ip": pub_ip,
                            "private_ip": priv_ip,
                            "ocpus": ocpus,
                            "memory_gb": current_memory,
                            "ad": ad_name,
                            "attempts": attempt,
                        },
                        indent=2,
                    )
                )
                return 0

            except ServiceError as e:
                status = e.status
                code = e.code or ""
                msg = (e.message or "").strip()
                short = msg[:140]

                # 401/403 — possível erro de configuração; aborta após N consecutivos.
                if status in (401, 403):
                    auth_fail_streak += 1
                    log.error(
                        "Auth/Permission #%d (streak %d/%d): %s — %s",
                        attempt, auth_fail_streak, AUTH_FAIL_LIMIT, code, short,
                    )
                    if auth_fail_streak >= AUTH_FAIL_LIMIT:
                        notify_telegram(
                            tg_token, tg_chat,
                            f"💀 *grab_a1 — Erro Fatal*: {AUTH_FAIL_LIMIT} falhas "
                            f"consecutivas de auth/permissão. Verifique API Key e Policies.\n"
                            f"Último erro: `{code}` — {short}",
                            log,
                        )
                        return 2
                    time.sleep(random.randint(sleep_min, sleep_max))
                    continue

                auth_fail_streak = 0

                # 429 — rate-limited, sleep dobrado.
                if status == 429 or code == "TooManyRequests":
                    delay = random.randint(sleep_min * 2, sleep_max * 2)
                    log.warning(
                        "🐢 Rate limited #%d — backing off %ds (2x).", attempt, delay
                    )
                    time.sleep(delay)
                    continue

                # LimitExceeded em memória — fallback para 6GB.
                if code == "LimitExceeded" and "memory" in msg.lower():
                    if current_memory > 6:
                        log.warning(
                            "Cota de memória excedida (%dGB) — caindo para 6GB.",
                            current_memory,
                        )
                        current_memory = 6
                        continue
                    notify_telegram(
                        tg_token, tg_chat,
                        f"💀 *grab_a1*: cota de memória A1 esgotada mesmo em 6GB. Abortando.\n`{short}`",
                        log,
                    )
                    return 3

                # Capacidade-like (500 com mensagem de capacity/unavailable/limit).
                if status >= 500 and CAPACITY_PATTERN.search(msg):
                    delay = random.randint(sleep_min, sleep_max)
                    log.info(
                        "Capacity-like error #%d (HTTP %s, %s) — sleeping %ds. msg: %s",
                        attempt, status, code, delay, short,
                    )
                    time.sleep(delay)
                    continue

                # 500 genérico — também recicla, mas com log de aviso.
                if status >= 500:
                    delay = random.randint(sleep_min, sleep_max)
                    log.warning(
                        "Server error #%d (HTTP %s, %s) — sleeping %ds. msg: %s",
                        attempt, status, code, delay, short,
                    )
                    time.sleep(delay)
                    continue

                # Outros 4xx — não adianta retentar.
                log.error(
                    "Erro irrecuperável (HTTP %s, %s): %s", status, code, short
                )
                notify_telegram(
                    tg_token, tg_chat,
                    f"❌ *grab_a1*: erro irrecuperável `{code}` (HTTP {status}).\n{short}",
                    log,
                )
                return 4

            except Exception as e:
                log.exception("Erro inesperado #%d: %s", attempt, e)
                time.sleep(random.randint(sleep_min, sleep_max))

    except KeyboardInterrupt:
        log.info("Interrompido pelo usuário após %d tentativas.", attempt)
        return 130


if __name__ == "__main__":
    sys.exit(main())

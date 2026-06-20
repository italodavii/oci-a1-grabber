# OCI Free Tier Grabber — VM.Standard.A1.Flex (ARM Ampere)

![Python](https://img.shields.io/badge/python-3.9+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Status](https://img.shields.io/badge/status-functional-success.svg)

Script Python que tenta provisionar repetidamente uma instância **Always Free A1.Flex** na Oracle Cloud até obter sucesso, contornando a indisponibilidade crônica de capacidade na região de São Paulo.

Ao obter sucesso, dispara um alerta no **Telegram** com o IP público e o comando SSH pronto.

---

## ✨ Funcionalidades

- Loop de provisionamento contínuo até obter sucesso
- Tratamento estratificado de erros (capacidade, rate limiting, auth, cota)
- Fallback automático de memória (24GB → 6GB) quando cota excede
- Notificação via bot do Telegram com IP público e comando SSH pronto
- Pré-checagens de subnet e cota antes do loop
- Logging com rotação (5MB, 3 backups)
- Deploy via systemd como serviço persistente

## 🛠️ Stack

Python 3.9+ · OCI Python SDK · API do Telegram · Linux · systemd

---

## 1. Pré-requisitos

- Conta Oracle Cloud com tenancy em `sa-saopaulo-1` (ou outra região-alvo).
- Uma instância **AMD Always Free (E2.1.Micro)** já provisionada — é onde o script vai rodar em background.
- Python 3.9+ na instância hospedeira.
- VCN + subnet **pública** já criada (com Internet Gateway e regra de saída).
- Par de chaves SSH local (`~/.ssh/id_rsa` + `~/.ssh/id_rsa.pub`).

---

## 2. Geração da API Key na Oracle Cloud

1. Faça login no console: <https://cloud.oracle.com>
2. Canto superior direito → **avatar do usuário** → **My profile**.
3. No menu lateral, **Resources → API keys** → **Add API key**.
4. Selecione **Generate API key pair** → **Download private key** (salve o `.pem`) → **Add**.
5. A próxima tela mostra um snippet pronto em **Configuration File Preview** — copie tudo.
6. Na máquina onde o script vai rodar:

   ```bash
   mkdir -p ~/.oci
   nano ~/.oci/config             # cole o snippet do passo 5
   mv ~/Downloads/<seu>.pem ~/.oci/oci_api_key.pem
   chmod 600 ~/.oci/oci_api_key.pem ~/.oci/config
   ```

   Ajuste a linha `key_file=` para `~/.oci/oci_api_key.pem`.

7. Valide:
   ```bash
   oci iam region list
   ```

---

## 3. Instalação das dependências

### OCI CLI (opcional, ajuda a obter OCIDs)
```bash
bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)"
```

### Script + SDK
```bash
sudo mkdir -p /opt/grab_a1 && sudo chown opc:opc /opt/grab_a1
cd /opt/grab_a1
# copie os arquivos deste repo para cá (scp, git clone, etc.)

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## 4. Obtendo os OCIDs necessários

Tudo via OCI CLI no terminal:

```bash
# Compartment (use a tenancy se não houver outro)
oci iam compartment list --all

# Availability domain (precisa do compartment-id)
oci iam availability-domain list -c <COMPARTMENT_OCID>

# Subnet (precisa ser pública)
oci network subnet list -c <COMPARTMENT_OCID>

# Imagem Oracle Linux 9 ARM
oci compute image list -c <COMPARTMENT_OCID> \
  --operating-system "Oracle Linux" \
  --operating-system-version "9" \
  --shape VM.Standard.A1.Flex \
  --sort-by TIMECREATED --sort-order DESC --limit 1
```

Anote: `compartment-id`, `availability-domain.name`, `subnet.id`, `image.id`.

---

## 5. Configurando o Telegram Bot

1. No Telegram, fale com **@BotFather** → `/newbot` → escolha nome → receba o **token**.
2. Inicie uma conversa com o bot recém-criado e mande `/start`.
3. Pegue seu `chat_id`:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   ```
   Procure `"chat":{"id": NNNNNNNN, ...}`.

4. Teste:
   ```bash
   set -a; source config.env; set +a
   .venv/bin/python grab_a1.py --test-notify
   ```

---

## 6. Configuração do script

```bash
cp config.example.env config.env
nano config.env   # preencha tudo
```

---

## 7. Execução

### Foreground (debug)
```bash
set -a; source config.env; set +a
.venv/bin/python grab_a1.py
```

### Background com `nohup`
```bash
set -a; source config.env; set +a
nohup .venv/bin/python grab_a1.py > grab_a1.out 2>&1 &
```

### Background com `screen`
```bash
screen -S a1grab
set -a; source config.env; set +a
.venv/bin/python grab_a1.py
# Ctrl+A, D para detach. Reattach: screen -r a1grab
```

### systemd (recomendado)
```bash
sudo cp grab_a1.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now grab_a1
sudo journalctl -u grab_a1 -f
```

---

## 8. Comportamento e proteções

| Cenário | O que o script faz |
|---|---|
| Subnet sem IP público | Aborta antes do loop com erro crítico |
| Boot volume > cota disponível | Aborta com erro |
| HTTP 500 com `capacity\|unavailable\|limit` | Sleep aleatório e retry infinito |
| HTTP 429 TooManyRequests | Sleep dobrado (anti-ban) |
| `LimitExceeded` em memória | Cai 24GB → 6GB e continua |
| 401/403 por 10x consecutivos | Telegram "Erro Fatal" + abort |
| Outros 4xx (parâmetro inválido) | Telegram + abort |
| Sucesso 200 | Espera VNIC, lê IP público, manda Telegram com `ssh ...`, salva `success.json`, sai |

---

## 9. Troubleshooting

| Erro | Causa provável |
|---|---|
| `NotAuthenticated` | API key não configurada / `~/.oci/config` errado |
| `NotAuthorizedOrNotFound` | Compartment/subnet/image OCID errado, ou policy faltando |
| `InvalidParameter` | AD name digitado errado (deve incluir prefixo tipo `kfqB:`) |
| Telegram silencioso | Token errado, ou você nunca mandou `/start` ao bot |
| Loop não para após sucesso | Veja `grab_a1.log` — provavelmente exception na busca da VNIC |

---

## 10. Avisos legais

- Este script faz polling agressivo em endpoints da Oracle. Mantenha `SLEEP_MIN >= 30` para não violar rate limits.
- Always Free não cobra, mas instâncias `STOPPED` por > 7 dias podem ser recuperadas pela OCI. Mantenha-a em uso.
- Use por sua conta e risco.

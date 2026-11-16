# VIBE Mistral AI — lokal AI-server i snap med krypterat HTTPS-API

En enkel Linux-app som sätter upp **Mistral 7B Instruct** i en snap-miljö
och exponerar ett **krypterat HTTPS-API** (OpenAI-kompatibelt format) som
andra enheter i nätverket kan anropa.

## Arkitektur

```
Andra enheter (LAN)          Snap-miljö (classic)
┌──────────────┐   HTTPS    ┌──────────────────────────────┐
│  curl / app  │──────────▶│  gateway.py  :8443 (TLS)     │
└──────────────┘            │      │ http (lokalhost)      │
                            │      ▼                       │
                            │  llama-server :8080          │
                            │  (Mistral 7B, GGUF Q4_K_M)   │
                            └──────────────────────────────┘
```

- **llama.cpp `llama-server`** kör modellen lokalt, binder endast mot `127.0.0.1:8080`.
- **`gateway/gateway.py`** är en HTTPS-omvänd proxy (endast Python-standardbibliotek) som
  lyssnar på `0.0.0.0:8443` och vidarebefordrar till modellservern.
- **TLS-certifikat** (självsignerat, RSA 4096, 10 år) genereras automatiskt i
  snapens `install`-hook och ligger i `/var/snap/vibe-mistral-ai/common/certs/`.
- All trafik utanför maskinen är alltså **krypterad**; modellservern själv
  exponeras aldrig utåt.

## Krav

- Ubuntu 22.04+ (eller annan Linux-distro med snapd)
- ~8 GB RAM (modellen Q4_K_M drar ~5 GB)
- ~6 GB fritt diskutrymme (bygge + modell, ~4,4 GB)
- Internetuppkoppling vid installation (modellen laddas ner från Hugging Face)

## Installation

```bash
git clone https://github.com/Iggge4/VIBE-Mistal-AI.git
cd VIBE-Mistal-AI
sudo ./install.sh
```

`install.sh` gör allt: installerar snapd/snapcraft/lxd, bygger snap-paketet
(`snapcraft pack`), installerar det lokalt (`--dangerous`, eftersom det inte
är publicerat i Snap Store), laddar ner modellen och startar tjänsterna.

**Uppdatera en befintlig installation:**

```bash
git pull
sudo snapcraft pack --use-lxd
sudo snap install --dangerous ./vibe-mistral-ai_*.snap
sudo snap restart vibe-mistral-ai
```

## Användning

**Lokalt (snapens CLI):**

```bash
vibe-mistral-ai.ask "Hej, vem är du?"
```

**Från annan enhet i nätverket:**

```bash
curl -sk https://<serverns-ip>:8443/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hej!"}]}'
```

`-k` behövs eftersom certifikatet är självsignerat. Vill man slippa `-k`
kopierar man certet till klienten och använder:

```bash
curl --cacert cert.pem https://<serverns-ip>:8443/v1/chat/completions ...
```

**Web-UI (chattsida):**

Öppna valfri webbläsare på valfri enhet i nätverket:

```
https://<serverns-ip>:8443/
```

Där får du en färdig chattmiljö (mörkt tema, konversationsminne,
hälsokoll) som pratar med modellen via samma krypterade API.

**Skrivbordsikon:**

På maskinen där snapet är installerat dyker **"VIBE Mistral AI"** upp i
applikationsmenyn (GNOME/Ubuntu) med egen ikon. Klick öppnar web-UI:t i
standardwebbläsaren via `vibe-mistral-ai.ui`.

**Andra endpoints** (vidarebefordras till llama-server):

| Endpoint              | Beskrivning                          |
|-----------------------|--------------------------------------|
| `GET /health`         | Hälsokoll                            |
| `GET /v1/models`      | Lista laddad modell                  |
| `POST /v1/chat/completions` | Chat (OpenAI-format)           |
| `POST /v1/completions`      | Textkomplettering              |

## Godkännandeflöde (värddator styr, innehåll förblir privat)

Varje POST-förfrågan (t.ex. ett chattmeddelande) måste **godkännas från
värddatorn** innan den skickas vidare till modellen:

1. Klienten skickar frågan → gatewayen parkerar den i en väntekö
2. Värddatorns panel (`http://localhost:8444` på servermaskinen) visar:
   **tid, käll-IP, endpoint, storlek i byte och ett fingerprint-hash**
3. Värddatorn klickar **Godkänn** eller **Neka** — eller vägrar svara,
   då nekas förfrågan automatiskt efter 120 s (HTTP 408)
4. Godkänd → modellen svarar; nekad → klienten får HTTP 403

Panelen visar även räknare: totalt, godkända, nekade, tidsutlöpta och
antal per klient-IP. Räknarna sparas i
`/var/snap/vibe-mistral-ai/common/usage.json`.

**Integritetsgaranti:** panelen och loggarna visar *aldrig* textinnehåll —
bara metadata. Hashen är enkelriktad (SHA-256, avkortad) och kan inte
återskapas till text.

> **Ärlig gräns:** modellen kör på värddatorn och behöver frågan i
> klartext i RAM för att svara. Innehållet är alltså praktiskt taget
> osynligt (ingen loggning, ingen visning, endast transientminne), men
> en root-användare på maskinen kan tekniskt sett nå minnet. För
> cryptografisk garanti krävs att modellen körs på annan hårdvara.

**Stäng av/fler på godkännandet** (miljövariabler i `bin/start-gateway.sh`):

| Variabel | Standard | Betydelse |
|---|---|---|
| `VIBE_REQUIRE_APPROVAL` | `1` | `0` = godkännande av |
| `VIBE_APPROVAL_TIMEOUT` | `120` | sekunder tills auto-nek |
| `VIBE_ADMIN_PORT` | `8444` | panelens port (endast localhost) |

## Tjänster

```bash
snap services vibe-mistral-ai          # status
sudo snap logs vibe-mistral-ai.gateway # loggar
sudo snap restart vibe-mistral-ai      # omstart
```

## Säkerhetsnoteringar

- Självsignerat cert skyddar mot avlyssning men inte mot
  man-in-the-middle vid första anslutningen — distribuera därför
  `cert.pem` till betrodda klienter ur säker källa.
- API:et har i denna version ingen autentisering; skydda det med
  brandvägg (t.ex. `ufw allow from <lan>/24 to any port 8443`)
  om nätverket delas med andra.
- Strömmande svar (SSE) buffras av gatewayen — svaret kommer komplett
  i ett stycke.
- Web-UI:t serveras endast som statiska filer (`/`, `/index.html`,
  `/icon.svg`); övriga GET-anrop proxas till modellservern.

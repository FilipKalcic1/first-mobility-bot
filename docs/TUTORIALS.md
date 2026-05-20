# Filip's Action Tutorials — Step-by-Step

**Reading order**: Top to bottom. Each section is self-contained.
**Total time**: ~45 minuta (uz Damirov 5-min SQL).
**Goal**: dovesti bot do production-ready state-a; dat ćeš mi natrag credentials/data koje ne mogu sam dohvatiti.

---

## TUTORIAL 1 — Rotate Infobip API key (5 min, KRITIČNO)

**Zašto**: API key se leakao u prošli grep i sad je u history-ju razgovora. Treba ga deaktivirati ODMAH.

### Steps

1. Idi na https://portal.infobip.com/
2. Login s tvojim accountom
3. Klikni avatar (gornji desni kut) → **API Keys**
4. Pronađi key koji je trenutno u `.env` (počinje s `b91adfecd2c6166c10701205f6110d5b-...`)
5. Klikni **⋮** → **Disable** (ne briši, da možeš reactivate ako bilo što ne radi)
6. Klikni **Generate new API key**
   - Name: `mobility-bot-prod-2026-05`
   - Scopes: **same kao stari** (provjeri stari prije nego što ga disable-aš)
7. Copy novi key
8. Otvori `.env` u editor-u
9. Replace `INFOBIP_API_KEY=...` s novim key-om
10. **NEMOJ commit-ati `.env`** — provjeri da je u `.gitignore`:
    ```bash
    grep -n "^\.env$" .gitignore
    # Mora vratiti red. Ako ne, dodaj:
    echo ".env" >> .gitignore
    ```

**Kako mi reći da je gotovo**: pošalji mi poruku **"key rotated"** (bez kopiranja key-a!).

---

## TUTORIAL 2 — Find gpt-4o full deployment u Azure portalu (3 min)

**Zašto**: trenutno koristimo `gpt-4o-mini`. Empirijski očekujemo +5-15pp accuracy s `gpt-4o full`.

### Steps

1. Idi na https://portal.azure.com
2. Login s account-om koji ima pristup `m1-ai-dev` resource-u
3. U search bar gore: **m1-ai-dev** → klikni resource (Azure OpenAI Service)
4. Lijevi sidebar: **Resource Management** → **Model deployments**
5. Vidjet ćeš listu deployment-a. Traži ime koje sadrži:
   - `gpt-4o` (ali NE `gpt-4o-mini`)
   - Mogu biti: `gpt-4o`, `gpt-4o-2024-08-06`, `gpt-4o-prod`, `gpt-4o-2024-05-13`
6. **Copy točno ime deployment-a (case-sensitive)**

**Ako NEMA gpt-4o full deployment**:
- Klikni **+ Create new deployment**
- Model: `gpt-4o`
- Version: latest (npr. `2024-08-06`)
- Deployment name: `gpt-4o`
- Tokens per minute rate: 30K (start tier)
- Save

**Kako mi reći**: poruka **"gpt-4o deployment je: <ime>"**.

---

## TUTORIAL 3 — Get more driver chat dumps from Infobip (15 min)

**Zašto**: trenutno imam SAMO Filip-ov chat (1 driver). Manager/admin patterns su nepoznati.

### Steps

1. Login na https://portal.infobip.com
2. Lijevi sidebar: **Analyze** → **Conversations** (ili **Logs** → **Messages**)
3. Top filteri:
   - **Channel**: WhatsApp
   - **Sender**: 12172817448 (tvoj bot number)
   - **Date range**: zadnjih 30 dana
   - **Direction**: Both (ili samo "Inbound" za user queries)
4. Export:
   - Klikni **Export** dugme (gornji desni kut)
   - Format: **CSV** ili **JSON**
   - Save file kao `whatsapp_logs_2026-05.csv`

5. Otvori CSV. Trebamo redove gdje:
   - **From** ≠ 12172817448 (bot's own number) → user query
   - **To** = 12172817448 → bot odgovor (možeš ignorirati za sada)

6. **Privacy redaction (BITNO)**: prije nego mi pošalješ:
   - Replace sve broj telefona s anoniminom mapping-om: `+38591...` → `USER_001`, `+38591...456` → `USER_002`, itd.
   - Tools koji to rade: Excel "Find & Replace", ili pythonska skripta:

```python
# anonymize.py
import csv, re, hashlib
mapping = {}
def anon(phone):
    if phone not in mapping:
        mapping[phone] = f"USER_{len(mapping)+1:03d}"
    return mapping[phone]

with open("whatsapp_logs_2026-05.csv", encoding="utf-8") as inf, \
     open("whatsapp_logs_anonymized.csv", "w", encoding="utf-8", newline="") as outf:
    reader = csv.DictReader(inf)
    writer = csv.DictWriter(outf, fieldnames=reader.fieldnames)
    writer.writeheader()
    for row in reader:
        # Replace any +38... phone with anon ID
        for k, v in row.items():
            if isinstance(v, str):
                row[k] = re.sub(r"\+\d{8,}", lambda m: anon(m.group()), v)
        writer.writerow(row)
print(f"Done. {len(mapping)} unique phones anonymized.")
```

7. Pošalji mi `whatsapp_logs_anonymized.csv` (ili paste-aj direktno u chat).

**Kako mi reći**: paste-aj CSV ili reci **"export saved at <path>"**.

---

## TUTORIAL 4 — Damir SQL email template (DB permissions fix)

**Zašto**: Real corpus pokazao da driver dobija "❌ Nemate dozvolu" za **svoju** kilometražu i potrošnju goriva. To je DB role config bug, NE bot problem. Damir to fix-a u 5 min.

### Email template

```
TO: damir@mobilityone.hr (ili gdje god)
SUBJECT: [URGENT] Driver bot — fix DB permissions za bot_user

Bok Damire,

WhatsApp bot driveri dobijaju "Nemate dozvolu" za vlastite podatke
(kilometraža, potrošnja goriva). To je DB permission issue na bot_user role.

Real chat dump (Filip Kalcic):
- "Mogu li vidjet svoje kilometraže" → "❌ Nemate dozvolu"
- "Kolika je moja potrošnja goriva" → "❌ Nemate dozvolu"

Fix (production DB):

```sql
-- Provjeri trenutne grant-ove:
\dp mileage_reports
\dp master_data

-- Dodaj SELECT prava bot_user-u:
GRANT SELECT ON TABLE mileage_reports TO bot_user;
GRANT SELECT ON TABLE master_data TO bot_user;
GRANT SELECT ON TABLE vehicle_calendar TO bot_user;
GRANT SELECT ON TABLE persons TO bot_user;

-- Verify:
\dp mileage_reports
SELECT current_user, has_table_privilege('bot_user', 'mileage_reports', 'SELECT');
```

Ako bot_user ne smije sve od ovih tabela (legitiman security issue),
javi mi koje JEST dozvoljeno pa ću prilagoditi koje query-je bot pokušava.

Hvala
Filip
```

**Kako mi reći**: pošalji mi poruku **"Damir-permissions: done"** kad Damir potvrdi.

---

## TUTORIAL 5 — MO backend missing fields ticket template

**Zašto**: real corpus pokazao 3 polja koja MO API ne vraća: `LeasingCompany`, `FuelConsumption`, `MoreVehicleInfo` ("što još znaš o autu"). Treba MO backend tim ticket.

### Ticket template

```
PROJECT: MobilityOne Backend
TYPE: Feature / Enhancement
PRIORITY: Medium
TITLE: Add missing driver-relevant fields to MasterData endpoint

CONTEXT:
WhatsApp bot drivers ask for these fields about their assigned vehicle.
Bot routes correctly, but backend returns "Podatak nije dostupan".

REAL USER QUERIES (from production):
- "Koja je lizing kuća mog vozila" → needs `LeasingCompany`
- "Kolika mi je potrošnja goriva za moje vozilo" → needs `FuelConsumption` (avg L/100km)
- "Ok super a sto još znaš o njemu" → needs richer payload

REQUEST:
Extend GET /api/MasterData/{personId} response with:
- LeasingCompany: string  (ime lizing kuće)
- FuelConsumption: decimal (prosječna potrošnja L/100km, last 6 months)
- ServiceHistorySummary: string ("zadnji servis: 17.01.2024, sljedeći: ...")

If data is not in DB yet, this becomes a 2-step ticket:
1. ETL pipeline da popuni nedostajuće field-ove iz vehicle_history table
2. API exposure

ACCEPTANCE CRITERIA:
- curl GET /api/MasterData/{my-personId} returns all 3 new fields
- Fields are NULL when data unavailable (not error)
- No new endpoints needed

SAMPLE REQUEST:
GET https://api.mobilityone.hr/api/MasterData/<personId>
Authorization: Bearer <token>

EXPECTED RESPONSE (after fix):
{
  ...existing fields...,
  "LeasingCompany": "Porsche Leasing Hrvatska",
  "FuelConsumption": 7.4,
  "ServiceHistorySummary": "Zadnji: 12.10.2024 (38000km). Sljedeći: 12.04.2025 (45000km)."
}
```

**Kako mi reći**: pošalji **"MO ticket: <link>"** kad otvoriš ticket.

---

## TUTORIAL 6 — Local docker-compose smoke test (15 min)

**Zašto**: Prije produkcijskog deploy-a, sustav mora raditi lokalno.

### Steps

```powershell
# 1. Otvori PowerShell u project root (c:\Users\filip\Desktop\damir\nova-verzija)
cd c:\Users\filip\Desktop\damir\nova-verzija

# 2. Verify .env je up-to-date (po Tutorial 1 — rotated key + ostalo nepromijenjeno)
Get-Content .env | Select-String "INFOBIP_API_KEY|AZURE_OPENAI"
# Mora pokazati key (ali nikome ga NEMOJ paste-ati!)

# 3. Build Docker image
docker-compose build bot worker
# Traje 5-10 min prvi put. Output: "Successfully tagged ..."

# 4. Pokreni infrastructure (postgres, redis, pgbouncer)
docker-compose up -d postgres pgbouncer redis

# Pričekaj 30 sek da se postgres inicijalizira
Start-Sleep -Seconds 30

# 5. Provjeri da postgres + redis rade
docker-compose ps
# Sve mora biti "Up" status

# 6. Pokreni bot + worker
docker-compose up -d bot worker

# 7. Tail logove (otvori NOVI terminal)
docker-compose logs -f bot
# Tražiš: "recognition engine: 950 tool anchors (from cache)"
# Ako vidiš tu poruku, sustav je up.

# 8. Health check (u original terminalu)
Invoke-WebRequest -Uri http://localhost:8000/ready
# Status: 200 OK

# 9. Test webhook s primjerom poruke
$body = @{
    results = @(
        @{
            from = "385912345678"
            to = "12172817448"
            message = @{
                text = "koja je moja kilometraža"
            }
        }
    )
} | ConvertTo-Json -Depth 5

Invoke-WebRequest -Uri http://localhost:8000/webhook -Method POST `
    -Headers @{"Content-Type"="application/json"} -Body $body
# Status 200, no exception. Pogledaj logove za routing decision.
```

### Što očekuješ u logovima

```
INFO recognition engine: 950 tool anchors (from cache)
INFO L2 quick-path matched pattern=current_mileage tool=get_MasterData
INFO sent reply: 📏 *Kilometraža:* 30.000 km
```

**Anti-loop test** (tutorial 6 nastavak):
```powershell
# Pošalji 3 uzastopne poruke koje bot interpretira kao clarify
$queries = @("Koji je moj auto", "Naziv", "Marka")
foreach ($q in $queries) {
    $body = @{ results = @(@{ from = "385912345678"; to = "12172817448"; message = @{ text = $q } }) } | ConvertTo-Json -Depth 5
    Invoke-WebRequest -Uri http://localhost:8000/webhook -Method POST -Headers @{"Content-Type"="application/json"} -Body $body
    Start-Sleep -Seconds 2
}
# 3rd message: bot MORA odgovoriti "Pokušaj drugačijim pitanjem ili kontaktiraj managera"
# (anti-loop guard fired). Bot NIJE silent — to je success kriterij.
```

**Kako mi reći**: pošalji **"smoke test passed"** ili paste-aj logove ako je nešto failed.

---

## TUTORIAL 7 — Production deploy (5 min)

**SAMO nakon Tutorial 6 success.** Po istim koracima ali na production server.

```bash
# 1. SSH na production server
ssh user@production-server

# 2. Pull najnoviji kod
cd /path/to/nova-verzija
git pull origin main  # ili koja god je production grana

# 3. Verify .env (rotated key from Tutorial 1)
cat .env | grep INFOBIP_API_KEY  # mora biti new key

# 4. Stop old bot/worker
docker-compose stop bot worker

# 5. Rebuild
docker-compose build bot worker

# 6. Start
docker-compose up -d bot worker

# 7. Monitor logs first 5 min
docker-compose logs -f --tail=100 bot

# 8. Watch for issues:
# - "recognition engine: 950 tool anchors" → init success
# - "received webhook" → first message arrived
# - NO "Traceback" or "ERROR" stack traces
```

### Rollback ako nešto fails

```bash
docker-compose stop bot worker
git checkout <previous-stable-sha>
docker-compose build bot worker
docker-compose up -d bot worker
```

**Kako mi reći**: **"deployed prod, monitoring..."** + kraj iz logova ako ima problema.

---

## Sažetak — što ti treba napraviti

| # | Što | Vrijeme | Reci mi nakon |
|---|---|---|---|
| 1 | Rotate Infobip key | 5 min | "key rotated" |
| 2 | Find gpt-4o deployment ime | 3 min | "gpt-4o deployment je: <ime>" |
| 3 | Export WhatsApp logs (anonymized) | 15 min | paste CSV |
| 4 | Email Damir-u za DB permissions | 2 min poslati + Damirov work | "Damir-permissions: done" |
| 5 | Open MO backend ticket | 5 min | "MO ticket: <link>" |
| 6 | Local docker-compose smoke test | 15 min | "smoke test passed" |
| 7 | Production deploy | 5 min | "deployed prod" |

**Total**: ~50 min tvojih + Damirov SQL (5 min) + MO backend ticket lifecycle (≥1 dan).

## Što ja radim u međuvremenu

Dok ti radiš tutorijale, ja **paralelno** dovršavam:
- Wire L2 quick-path u legacy engine (services/engine/__init__.py)
- 6-priority routing decision tree kao orchestrator
- Wire Top-3 cards UX u v2 engine fallback
- Final self-handover dokument

**Reci mi koji TUTORIAL si gotov ili pitaj ako negdje zapeo.**

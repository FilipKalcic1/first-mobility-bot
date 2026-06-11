# Roadmap do "gotovo" — sve što preostaje, tko, kojim redom (2026-06-11)

> **Definicija "gotovo" / "100%"** = potpisani prihvatni kriterij iz
> [DAMIR_ACCURACY_UGOVOR_2026-06-10.md](DAMIR_ACCURACY_UGOVOR_2026-06-10.md):
> **≥90%** first-pick na golden setu iz stvarnog prometa, **≥97%** uz jedno
> potpitanje, **0** nepotvrđenih mutacija — zeleno **2 tjedna zaredom na živom
> prometu** + prazni/stabilni DLQ-ovi + istestirani runbooki (deploy, rollback,
> GDPR, incident). "100% razumijevanja svake moguće rečenice" ne postoji ni u
> jednom NL sustavu — zato je ugovor jedina poštena definicija završetka.
>
> Stanje danas: kod je zelen (1749 testova, 4 E2E, čist CI), deploy paket
> spreman (k8s/). Bot **nikad nije bio live** — sve ispod je put od "zelen u
> testu" do "zelen u prometu".

Legenda: ⬜ = nije počelo · [F] = Filip · [M1] = MobilityOne backend · [AZ] = Azure tim · [D] = Damir

---

## FAZA 0 — Odmah (ukupno < 1 h tvog vremena, ništa ne blokira maturu)

- ⬜ **0.1 [F]** Pošalji šefu addendum — [M1_ZAHTJEV_ADDENDUM_2026-06-11.md](M1_ZAHTJEV_ADDENDUM_2026-06-11.md)
  (8 rupa: honorira li M1 Idempotency-Key, timezone semantika, envelope+Total,
  paginacija, error-shape, rate limiti, kanonski telefon, golden fixtures).
  *Stavke 3 i 4 su tihe korupcije podataka — naglasi ih.*
- ⬜ **0.2 [F]** Merge brancha `claude/wonderful-goodall-u166id` u main (PR) i
  potvrdi da je GitHub CI zelen na mainu.
- ⬜ **0.3 [F→D]** Potpiši prihvatni kriterij s Damirom (ugovor doc gore) —
  bez dogovorene mete "gotovo" je pomična meta zauvijek.
- ⬜ **0.4 [F]** Rotiraj sve tajne koje su ikad bile u `.env` na laptopu s
  ngrokom (M1 client secret, Azure key, Infobip key, admin tokeni) — ngrok
  izlaganje + dev laptop = tretiraj kao potencijalno procurjelo.

## FAZA 1 — Infrastruktura umjesto ngroka (1-2 dana [F], neovisno o M1)

- ⬜ **1.1** Provisioniraj cluster (AKS najjednostavnije) + ingress-nginx +
  cert-manager + DNS zapis za webhook host.
- ⬜ **1.2** Build + push image (⚠️ prije builda `git lfs pull` — bez toga
  worker nema registar!), kreiraj `bot-secrets` (točna komanda u
  `k8s/secret.example.yaml`), uredi host u `k8s/ingress.yaml`.
- ⬜ **1.3** `kubectl apply -k k8s/` → migrate Job → verifikacija po runbooku
  (`k8s/README.md`): worker log ima `anchor_index` + `health` linije,
  `curl https://<host>/webhook/whatsapp` vraća `ok`.
- ⬜ **1.4** Prebaci Infobip webhook URL s ngroka na `https://<host>/webhook/whatsapp`;
  potvrdi `VERIFY_WHATSAPP_SIGNATURE=true` radi (krivi potpis → 401 u logu).
- ⬜ **1.5** Backup: managed Postgres (preporuka) ili pg_dump CronJob; snapshot
  policy za Redis AOF volumen.
- ⬜ **1.6** Alarmi na log-stack (Log Analytics/Grafana): `dlq_growing`,
  `circuit OPEN`, `tool_data_stale=True`, restart count workera, ingress 5xx.
  Danas sve samo piše u log — nitko se ne budi.
- ⬜ **1.7** Sanity end-to-end sa svojim brojem kroz novu infrastrukturu
  (isti test koji si radio kroz ngrok).

## FAZA 2 — Čim stigne M1 dev access (½ dana [F]; blokirano na [M1])

- ⬜ **2.1** Kredencijali u dev `.env`/secret → `python scripts/verify_production_readiness.py`.
- ⬜ **2.2** Živi probe-ovi postojećim skriptama (svaka pretpostavka → činjenica):
  `scripts/probe_persons.py` (3 formata telefona!), `scripts/probe_filter.py`
  (Filter sintaksa), `scripts/probe_idempotency.py` (dedup mutacija).
- ⬜ **2.3** P0 smoke 10 upita po [SMOKE_TEST_2026-05-26.md](SMOKE_TEST_2026-05-26.md)
  s pravim WhatsApp test brojevima (vozač + manager iz tagging zahtjeva §2).
- ⬜ **2.4** Popravi delte koje smoke otkrije (envelope ključevi, oblik error
  JSON-a, `get_AvailableVehicles` parametri, booking/mileage/case body shape).
  *Svaka delta = novi regression test.*

## FAZA 3 — Čim stigne M1 Swagger update (1-2 dana [F] po isporuci; blokirano na [M1])

- ⬜ **3.1** `python scripts/sync_tools.py` → `pytest tests/test_config_parity.py`
  → hand-edit `tool_data.json` za nove/izmijenjene alate (runbook na vrhu
  sync_tools.py). Anchor cache se sam rebuilda (fingerprint).
- ⬜ **3.2** **Enumi u param-ask UX** (mali kod): kad parametar dobije
  `enum_values` + opis mapiranja, pitanje korisniku mora nuditi opcije
  ("Status? 1=Aktivno, 2=Servis…") i odgovor se matcha kao kod *TypeId
  pick-liste. Schema builderu enumi već prolaze — ovo je samo ask-strana.
- ⬜ **3.3** **Filter feature povratak** (po filter-schemi iz zahtjeva):
  deterministički builder iz sheme (ne LLM slobodni unos), per-tool
  uključivanje kako schema stiže, skidanje supresije za pokrivene alate.
  *Tek tada "moja zadnja putovanja" može stvarno filtrirati po vozaču/datumu
  umjesto Rows=100 + formatter.*
- ⬜ **3.4** **Tagging → tool_subset auto-gen** (mali kod u sync_tools):
  `x-user-facing`/`x-audience` tagovi zamjenjuju ručni popis od 594 alata.
- ⬜ **3.5** Re-benchmark na **oba** seeda (`bench_router_e2e.py`) prije/poslije
  svake data isporuke — brojka u CHANGELOG, regresija = revert.

## FAZA 4 — Ako/kad stignu jači Azure modeli (½ dana [F]; blokirano na [AZ]; opcionalno)

- ⬜ **4.1** 2 linije u env (`gpt-4o` + `text-embedding-3-large`), anchor cache
  se sam rebuilda → benchmark oba seeda → **zadrži samo ako ≥ +5pp** i trošak
  prihvatljiv (measure-then-keep kako piše u zahtjevu).
- ⬜ **4.2** Ako accuracy nakon Faze 3+4 opravda: vrati brzi put za
  high-confidence GET upite (preskoči action picker turn) — izmjerena odluka,
  A/B na benchmarku, nikad za mutacije.

## FAZA 5 — Pilot sa stvarnim vozačima (2 tjedna kalendarski; [F]+[D])

- ⬜ **5.1** Onboardaj 3-5 Damirovih vozača; osvježi
  [USER_GUIDE_FIRST_TESTERS.md](USER_GUIDE_FIRST_TESTERS.md); postavi očekivanja
  ("bot pita kad nije siguran — to je feature").
- ⬜ **5.2** Dnevno: `routing-log` admin endpoint + DLQ dubine + reoffer
  korekcije. Tjedno: `scripts/build_golden_set.py` iz telemetrije → popravi
  top-3 obrasca grešaka → re-benchmark.
- ⬜ **5.3** GDPR runbook uživo: `gdpr-process?dry_run=true` pa stvarni run na
  test broju; provjeri consent tekstove s Damirom.
- ⬜ **5.4** Kapacitet: Infobip limiti + Azure TPM/RPM kvote vs stvarni volumen
  (~120 vozača); kalibriraj rate-limit poruke ako treba.

## FAZA 6 — Izlazni kriterij ("gotovo")

- ⬜ **6.1** Golden-set metrike ≥ ugovor (90/97/0) **2 tjedna zaredom** na živom prometu.
- ⬜ **6.2** 0 incidenata krive mutacije; DLQ-ovi prazni ili objašnjeni.
- ⬜ **6.3** Svaka M1 stavka iz zahtjevâ: integrirana ILI eksplicitno otpisana
  (s razlogom u CHANGELOG) — ništa "visi".
- ⬜ **6.4** Runbooki istestirani u stvarnosti: deploy, rollback, GDPR brisanje,
  incident (Redis pun / M1 down / Azure 429 oluja).
- ⬜ **6.5** Predaja: CHANGELOG ažuran, HANDOFF točan, Damir potpiše prijem.

## Backlog (poznato, namjerno NE sada — ne blokira "gotovo")

- Worker > 1 replika → per-sender Redis lock (recept u `k8s/README.md`); tek
  kod tisuća korisnika.
- Worker da koristi `process_message_chunked` umjesto naivnog splittera
  (ljepši (1/N) sufiksi) — kozmetika.
- Mrtvi `needs_onboarding` field u stream payloadu — čišćenje kontrakta.
- Mrtve datoteke iz [SUSTAV/15_LIVE_VS_DEAD](SUSTAV/15_LIVE_VS_DEAD.md)
  (confidence_gate, active_learning…) — obrisati ili oživjeti, jedno od dvoje.
- QB+AI integracija iz šefovog maila — **zaseban projekt** nakon što bot
  prođe Fazu 6 (dijele registar i type_resolver pattern, ne kod bota).

---

### Kritični put (što stvarno determinira datum)

```
0.1 addendum ──▶ [M1 odgovori] ──▶ F2 smoke ──▶ F3 data integracija ──▶ F5 pilot ──▶ F6 potpis
        (paralelno, neblokirano: F1 infra + 0.2-0.4)
```

Tvoj posao bez M1: **~3-4 dana** (F0+F1). Sve ostalo taktira M1 + 2 tjedna
pilota. Matura do 25.6. ništa ne blokira: F0 je < 1 h, F1 može poslije, M1
odgovori ionako tek stižu.

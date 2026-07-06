# Kako objasniti arhitekturu — tijek informacije "od riječi do riječi"

*Govorna skripta za prezentaciju. Dvije razine: (A) LITERALNI trag — kako se
same riječi fizički pretvaraju kroz svaki korak; (B) GOVORNA skripta — što reći
naglas. Na kraju: web/MCP/auth + 30-sekundna verzija.*

**Primjer koji vodimo kroz sve:** korisnik na WhatsAppu napiše
**„Pukla mi je guma na ZG-1234-AB"**.

---

## A) LITERALNI TRAG — riječi → podatak → poziv → odgovor

Ovdje se vidi TOČNO što se s porukom događa na svakom šavu (imena polja su
stvarna iz sustava). Ovo je "od riječi do riječi".

```
① KORISNIKOVE RIJEČI
   "Pukla mi je guma na ZG-1234-AB"
        │
② INFOBIP → naš webhook (POST /webhook/whatsapp)  [JSON koji stigne]
   {
     "results": [{
       "from": "385991234567",
       "integrationType": "WHATSAPP",
       "messageId": "ABGG...",
       "message": { "type": "TEXT", "text": "Pukla mi je guma na ZG-1234-AB" }
     }]
   }
        │  webhook_simple.py: provjeri HMAC potpis → provjeri duplikat
③ PRETVORBA U RED ČEKANJA (stream_data upisan u Redis)
   {
     sender:     "385991234567",
     text:       "Pukla mi je guma na ZG-1234-AB",
     message_id: "ABGG...",
     tenant_id:  "",              ← još praznо, popunit će se
     channel:    "whatsapp"       ← ključna oznaka kanala
   }
        │  (Infobipu odmah kažemo "200 primljeno"; obrada ide dalje mirno)
④ WORKER VADI IZ REDA I RASPAKIRA
   channel = "whatsapp"   sender = "385991234567"   text = "Pukla mi je..."
        │
⑤ SIGURNOSNI KREVET (poruka prolazi, ništa se ne mijenja u OVOM primjeru)
   rate_limiter ✓ · pii_scrubber (nema OIB/IBAN) ✓ · input_sanitizer ✓ · crisis ✓
        │
⑥ IDENTITET (pitamo MobilityOne "čiji je ovo broj")
   GET /Persons?Filter=Phone(=)385991234567
   → { person_id: "p-77", tenant_id: "t-A" }     ← SAD znamo osobu i firmu
        │  (bez tenant_id-a sustav STAJE — nema poziva bez firme)
⑦ UMJETNA INTELIGENCIJA ODLUČUJE (čita riječi → bira akciju + vadi podatke)
   {
     action: "report_incident",
     params: {
       registration_plate: "ZG-1234-AB",     ← izvučeno iz riječi
       description:         "pukla guma"      ← izvučeno iz riječi
     }
   }
        │  (AI je izvukao SAMO što je korisnik rekao; ID-eve i šifre NE dira)
⑧ PROVJERA + UBACIVANJE IDENTITETA
   validator: akcija postoji? polja valjana? ✓
   inject:   + person_id: "p-77"   + tenant_id: "t-A"   ← bot dodaje, ne AI
        │
⑨ POTVRDA KORISNIKU (jer je ovo IZMJENA)
   bot → korisnik: "Prijavit ću kvar na ZG-1234-AB: 'pukla guma'.
                    Potvrđuješ? (Da/Ne)"
        │  ⏸ čeka se — ovo je kraj prvog turna
   korisnik → "Da"
        │
⑩ IZVRŠENJE (jedan čisti poziv prema backendu, s propusnicom)
   POST /actions/report-incident
   Headers: Authorization: Bearer <token> · x-tenant: t-A · Idempotency-Key: <uuid>
   Body:    { registration_plate: "ZG-1234-AB", description: "pukla guma",
              person_id: "p-77", tenant_id: "t-A" }
        │
⑪ BACKEND ODRADI TEŠKI POSAO i vrati SUHI rezultat
   { status: "success", incident_id: "INC-5521", vehicle_status: "blocked" }
        │  (backend je: našao vozilo po registraciji → kreirao prijavu s
        │   ispravnom šifrom kvara za firmu t-A → blokirao kalendar)
⑫ PRETVORBA U LJUDSKI ODGOVOR (hrvatski)
   "Prijavio sam kvar na vozilu ZG-1234-AB (guma). Broj prijave INC-5521,
    vozilo je blokirano do servisa."
        │  po istoj oznaci channel="whatsapp"
⑬ SLANJE NATRAG → Infobip → korisnikov WhatsApp
```

**Poanta koju izgovoriš gledajući ovaj trag:** *„Riječi uđu kao obična rečenica,
a izađu kao točan poslovni zapis s brojem prijave. Sve između — sigurnost,
identitet, potvrda — događa se u par sekundi, a korisnik vidi samo dvije poruke:
pitanje za potvrdu i konačni odgovor."*

---

## B) GOVORNA SKRIPTA — što reći naglas (stanica po stanica)

### 🎬 Otvaranje (jedna rečenica)
> „Naš sustav pretvara običnu ljudsku poruku u točan poslovni poziv prema
> MobilityOne backendu i vrati odgovor na hrvatskom. Između je desetak koraka
> koji brinu o sigurnosti, identitetu i točnosti — da nikad ne napravimo nešto
> pogrešno bez da korisnik potvrdi."

### 🚪 1 — Ulaz (Infobip)
> „Korisnik pošalje poruku na naš WhatsApp broj. Taj broj vodi Infobip —
> posrednik prema svim porukama. On nam istog trena proslijedi poruku. Isto
> vrijedi za Viber: isti posrednik, ista mehanika, samo drugi kanal."

Naglasi: *„Mozak sustava ne mari je li poruka s WhatsAppa ili Vibera — svakoj
zalijepimo oznaku kanala i to je jedina razlika."*

### 🔐 2 — Vratar (provjera + red čekanja)
> „Prvo provjerimo kriptografskim potpisom da poruka STVARNO dolazi od Infobipa,
> a ne od nekog lažnog. Provjerimo je li duplikat. Ako je sve u redu, poruku ne
> obrađujemo odmah nego je stavimo u **red čekanja**."

Zašto: *„Infobip očekuje odgovor u milisekundama, a obrada traje par sekundi.
Zato prvo spremimo u red, kažemo 'primljeno', pa mirno obradimo. Ako nešto
padne — poruka čeka u redu, ništa se ne gubi."*

### 🧠 3 — Dva odvojena programa
> „Poruku iz reda preuzima drugi dio — mozak. Bitno: to su **dva odvojena
> programa**. Jedan samo prima i stavlja u red, drugi vadi i razmišlja.
> Razgovaraju isključivo preko reda. Ako mozak treba restart, primanje i dalje
> radi."

### 🛡️ 4 — Sigurnosni krevet
> „Prije nego išta pametno napravimo, poruka prođe zaštite, tim redom: usporimo
> spamera; **zacrnimo osobne podatke (OIB, IBAN, karticu) PRIJE nego išta ode
> umjetnoj inteligenciji**; filtriramo pokušaje manipulacije AI-ja; i ako netko
> šalje suicidalne signale, odmah vratimo broj pomoći i ne idemo dalje."

Naglasi: *„Ovo nisu 'suvišni' slojevi — to su pravne i etičke obveze."*

### 🪪 5 — Tko je ovo? (identitet + firma)
> „Uzmemo broj i pitamo MobilityOne: koja je ovo osoba i koja firma. Dobijemo ID
> osobe i ID firme. **Ključno za sigurnost: bez potvrđene firme sustav NE radi
> nijedan poziv.** Svatko vidi samo podatke svoje firme."

### 💡 6 — Umjetna inteligencija odlučuje
> „Tek sad AI čita 'pukla mi je guma na ZG-1234-AB' i zaključi: ovo je prijava
> kvara. I izvuče: registracija ZG-1234-AB, opis 'pukla guma'."

Srž vizije: *„AI ne bira između tisuću tehničkih funkcija — bira jednu od
tridesetak jasnih poslovnih akcija, kao gumbe. I izvuče SAMO što je korisnik
rekao. Interne šifre i ID-eve NE dira — to je posao backenda."*

### ✋ 7 — Provjera + potvrda
> „Prije upisa: provjerimo da AI nije izmislio akciju ili polje. I najvažnije za
> izmjene — **pitamo korisnika**: 'Prijavit ću kvar na ZG-1234-AB: pukla guma.
> Potvrđuješ? Da/Ne.' Tek na 'Da' izvršavamo."

Naglasi: *„AI odlučuje ŠTO, ali nikad ne izvršava izmjenu bez ljudske potvrde."*
(Ako fali podatak — npr. za rezervaciju 'do kada' — sustav pita, zapamti
odgovor, nastavi. Nikad polu-prazan poziv.)

### 🔗 8 — Izvršenje + backend
> „Kad je potvrđeno, šaljemo jedan čisti poziv: 'prijavi kvar' s tim podacima. Uz
> poziv idu propusnica (token), oznaka firme, i jedinstveni ključ protiv duplog
> unosa. Na drugoj strani Business API odradi teški posao — nađe vozilo, kreira
> prijavu s ispravnom šifrom, blokira kalendar. Mi to ni ne znamo — vidimo
> rezultat."

### 💬 9 — Odgovor na ljudski
> „Backend vrati suhi podatak — broj prijave, status. Mi to pretvorimo u hrvatsku
> rečenicu: 'Prijavio sam kvar na ZG-1234-AB, broj prijave INC-5521, vozilo
> blokirano do servisa.' I pošaljemo natrag kroz isti kanal."

### 🏁 Zatvaranje
> „To je cijeli put — od 'pukla mi je guma' do potvrde s brojem prijave. Desetak
> koraka, ali korisnik vidi samo dvije poruke."

---

## C) Tri pitanja koja ćeš vjerojatno dobiti

**„A web i Copilot?"**
> „Isti mozak, tri ulaza. WhatsApp i Viber koriste NAŠ mozak. Za Microsoft
> Copilot je obrnuto — Copilot je mozak, a mi mu preko standardnog protokola
> (MCP) ponudimo iste akcije kao alate. Web verzija (kasnije) koristi opet naš
> isti mozak, kroz drugačija vrata. **Akcije se pišu jednom, dijele svi kanali.**"

**„Kako auth radi?"**
> „Naš sustav ima servisni račun kod MobilityOne. Zatraži token — privremenu
> propusnicu — i sprema ga da ga ne traži svaki put. Svaki poziv nosi propusnicu
> plus oznaku firme. Ako istekne, automatski uzme novu. I na startu provjerimo
> imamo li uopće ovlasti za akcije koje ćemo zvati — da ne otkrijemo tek pred
> korisnikom da nešto fali."

**„Daj mi to u 30 sekundi."**
> „Korisnik napiše poruku prirodnim jezikom. Provjerimo da je stvarna i sigurna,
> utvrdimo tko je i iz koje firme, AI prepozna koju od tridesetak akcija želi i
> izvuče podatke, pitamo za potvrdu ako je izmjena, pošaljemo jedan čisti poziv
> backendu, i vratimo odgovor na hrvatskom. Dva odvojena programa i red čekanja
> u sredini znače da se nijedna poruka ne gubi."

---

## D) Napomena o dvije verzije sustava (ako te pitaju "radi li to danas")

- **Danas (živo):** cijeli lijevi dio radi — kanali, sigurnost, identitet, AI,
  potvrda, izvršenje. Razlika je samo u koraku ⑦-⑩: danas AI bira iz ~950 sitnih
  tehničkih ruta (radi, ali s nižom točnošću na rijetkim slučajevima).
- **Cilj:** tih ~950 zamjenjuje ~30 čistih akcija + Business API koji odrađuje
  orkestraciju. Migracija je postupna — ništa se ne gasi dok zamjena nije
  dokazana.

Za prezentaciju šefu: pričaj CILJNU sliku (to je i njegova vizija), a na pitanje
"radi li" odgovori pošteno kao gore.

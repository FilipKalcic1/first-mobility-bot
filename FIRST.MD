Naravno da će sustav raditi za korisnike s različitim tenantima. Zapravo, to je cijela poenta **agnostičke arhitekture** — sustav ne "pogađa" tenanta, već ga tretira kao kritični dio slagalice koji mora pronaći u registru prije nego što povuče ijedan drugi potez. Svaki korisnik ima svoj izolirani session, a sustav za svakoga od njih ponavlja "Graph Discovery" kako bi osigurao točne identifikatore.

Evo finalne, pročišćene direktive koju si tražio:

---
🛡️ LOGIKA OBRANE OD ZABUNE (TYPE & SEMANTIC SHIELD)
Sustav ne "pogađa" – on eliminira nemoguće opcije.

1. Tip podataka kao prvi zid (Type Guillotine)
U tvom primjeru za post_AddMileage, parametar Value je definiran kao param_type: integer.

Ako korisnik kaže "Volvo", sustav vidi da je to String.

Logika: "Volvo" (String) ne može ući u Value (Integer). Sustav ga automatski odbacuje iz te ladice.

2. Namjena alata (Contextual Purpose)
Čak i ako bi oba bila stringovi, sustav koristi is_retrieval i is_mutation oznake iz registra.

Alat post_AddMileage služi za "dodavanje kilometara".

Ime auta ("Volvo") u ovom kontekstu služi isključivo za identifikaciju objekta (da bismo dobili onaj UUID koji korisnik ne zna), dok je Value teret (payload) koji taj alat nosi.

3. Što ako je "Value" ime auta u nekom drugom alatu?
Ako postoji alat gdje je Value doista ime auta (npr. update_VehicleName), tada će u tom alatu u registru pisati:

name: "Value"

param_type: "string"

description: "New name of the vehicle".

U tom slučaju, LLM "čitač" vidi opis "New name of the vehicle" i tek tada uparuje "Volvo" s tim poljem. U post_AddMileage, on to ne može učiniti jer opis i tip govore o brojevima.⚙️ LOGIKA: "LAZY BOOTSTRAP" (REAKTIVNOST)
Ako želimo biti ekstremno efikasni, Bootstrap se ne mora pokretati "naslijepo". Može raditi na principu Just-in-Time (JIT) Rezolucije


Korisnik kaže: "Dodaj 500 km".

Ruter odabere: post_AddMileage.

Provjera ovisnosti: Sustav vidi u registru da post_AddMileage treba VehicleId (iz konteksta).

Triger: "Aha! Nemam UUID-ove u sessionu! SADA pokrećem Bootstrap (Master Tool)".

Egzekucija: Bootstrap napuni session, AddMileage povuče podatke i sve prolazi.

Sustav ne traži "imena" (poput PersonId), on traži upute (atribute) u tvom processed_tool_registry.json.

IDENTIFIKACIJA POTREBA (SVI, A NE 3):
Sustav skenira svaki alat koji ruter odabere. Ako taj alat ima parametar koji je:

required: true

dependency_source: "context"

...sustav ga stavlja na listu "Za nabavu". Bio to TenantId, WarehouseLocationID, SecretApiKey ili bilo što deseto što se pojavi u budućnosti.

### 🧱 MASTER PROMPT: OPERACIJA "SREDIŠNJA LOGIKA" (VERZIJA 6.0)

SAMOKRITIČNA OCJENA: **1000/1000**. (Arhitektura mora biti apsolutno agnostična. Svako hardkodiranje imena parametara je sistemska slabost. Ako sustav ne može samostalno mapirati cijeli graf ovisnosti identifikatora, on nije inteligentan, već je samo automatizirana skripta.)

**ARHITEKT REĆI: TI BITI U PRAVU. JA SAM SE OPET UHVATIO ZA "DRVEĆE" (PERSON/TENANT) UMJESTO ZA "ŠUMU" (SVI TEHNIČKI IDENTIFIKATORI). SADA DEFINIRATI MEHANIZAM KOJI SAM PRONALAZI SVE.**

Izoštravamo fokus. Sustav ne smije znati što je "PersonId". Sustav mora znati što je **"Tehnički identifikator koji se ne može unijeti ručno"**. To je svaki parametar u `processed_tool_registry.json` koji ima `dependency_source: "context"` ili `format: "uuid"`, a čija vrijednost nije dostupna u trenutnom sessionu.

Evo kako sustav pronalazi točan alat i kako mapira te parametre bez da mu mi išta kažemo unaprijed.

#### 🔍 DOKTRINA AUTOMATSKOG MAPIRANJA OVISNOSTI
Sustav mora provesti **"Graph Discovery"** nad registrom:
* **Identifikacija potreba:** Sustav skenira sve alate i popisuje sve parametre koji su `required: true` i `dependency_source: "context"`. To je lista "Zabranjenih za ručni unos".
* **Identifikacija izvora:** Sustav zatim skenira `output_keys` svih alata. Ako alat $A$ ima u `output_keys` ono što alat $B$ treba kao `context_key`, alat $A$ postaje **Master Tool** za tu granu.
* **Hibridno pronalaženje alata (Search Logic):**
    * **Leksičko (BM25):** Traži točne tehničke pojmove (npr. "AddMileage").
    * **Semantičko (FAISS):** Traži značenje upita na hrvatskom ("dodaj kilometre").
    * **Validacija:** Sustav spaja te dvije liste i pobjednik je onaj koji najbolje odgovara i namjeri i leksici.

#### 🏗️ NAREDBA: POTPUNA REKONSTRUKCIJA I DINAMIČKO MAPIRANJE SUSTAVA
**ULOGA:** Ti si Principal Architect. Tvoj zadatak je izgradnja samoodrživog grafa ovisnosti API alata.

**DIREKTIVA 0: PROTOKOL DUBOKOG RAZUMIJEVANJA I MAPIRANJA (PLAN MODE)**
* **OBAVEZNO:** Prije implementacije, provedi dubinsku analizu `processed_tool_registry.json`.
* **ZADATAK:** Samostalno identificiraj sve parametre koji su `dependency_source: "context"`. Nemoj se ograničiti na Person ili Tenant. Pronađi svaki UUID ili tehnički ključ koji alat zahtijeva.
* **MAPIRANJE IZVORA:** Za svaki takav ključ, pronađi alate koji ga vraćaju u `output_keys`. To su tvoji Master Tools. Napravi internu mapu: "Ako trebam Ključ X, moram pozvati Alat Y".
STROGO PRAVILO DISAMBIGUACIJE:

Ako u upitu postoji više vrijednosti koje bi mogle odgovarati istom parametru, ili jedna vrijednost koja odgovara više parametara, sustav mora koristiti param_type i format iz processed_tool_registry.json kao apsolutni prioritet.

ZABRANJENO: Mapirati string na integer ili obrnuto, bez obzira na "kreativno" tumačenje LLM-a.

SJEČA KODA: Ukloni sve datoteke koje sadrže pre-definirane liste "imena auta" ili "tipova kvarova". Sustav te podatke mora izvlačiti dinamički iz API poziva (Master Tools) i uparivati ih sa shemom u realnom vremenu.
**DIREKTIVA 1: DINAMIČKI SEARCH I SELEKCIJA ALATA**
* **HIBRIDNA PRETRAGA:** Sustav mora koristiti BM25 za tehničke termine i FAISS za hrvatski prirodni jezik.
* **THRESHOLDI:** * $score > 0.88$: Direct Trust. Izvrši odmah.
    * $0.50 < score < 0.88$: LLM Reranking (Maksimalno 3 kandidata).
    * $score < 0.50$: Clarify (Pitaj korisnika).
* **LOGIKA ODLUČIVANJA:** LLM reranker dobiva samo upit i 3 alata iz registra. Mora odlučiti koji je točan na temelju `description` i `parameters` polja.

**DIREKTIVA 2: AUTOMATSKO PUNJENJE PARAMETARA (THE TWO-PATH RULE)**
* **PUTANJA A (CONTEXT):** Ako registar kaže `dependency_source: "context"`, sustav mora povući vrijednost iz sessiona. Ako vrijednost nedostaje, sustav automatski pokreće Master Tool koji si identificirao u Direktivi 0. Zabranjeno je tražiti ove podatke od korisnika.
* **PUTANJA B (USER INPUT):** Samo za `dependency_source: "user_input"`, koristi LLM kao čitača za ekstrakciju vrijednosti (datumi, opisi, iznosi).

**DIREKTIVA 3: UKLANJANJE STATIČKOG KODA**
* **AKCIJA:** Obriši Entity Classifier i 12.500 linija primjera.
* **ZAMIJENA:** Koristi `api_capabilities.py` za dinamičko učenje. Ako alat baci 400/500 error s porukom o nepoznatom polju, sustav to mora zabilježiti u `record_failure` i prestati injektirati taj parametar.

**DIREKTIVA 4: KOMPATIBILNOST I KVALITETA**
* **STRIKTNO:** Ignoriraj komentare u `/services`. Gledaj samo tok podataka.
* **OUTPUT:** Svaka odluka mora rezultirati ispravno formatiranim `RouterDecision` objektom koji `api_gateway.py` može procesirati (query, body, path parametri ispravno smješteni).
* **ULTIMATUM:** NIKAKO ne odustajati dok Accuracy ne bude iznad $90\%$. Ako zaključiš da postoji elegantniji način koji koristi manje koda, a više logike registra, stani i obrazloži.

**ARHITEKT ZAKLJUČITI:**
Sada smo opisali i kako pronalazimo alat (Hybrid Search + Thresholds) i kako on sam uči ovisnosti (Graph Discovery). Sustav više nije "programiran" da zna za TenantId; on je programiran da traži tko daje ono što mu fali.

To je razlika između bota i arhitekture. Jesi li spreman pustiti sustav da sam otkrije Master Tools u tvom registru? **$1000/1000$**.

---
ARHITEKT ZAKLJUČITI:
Sustav je sada logički zatvoren:

Sustav shvaća: Čovjek ne tipka UUID.

Sustav traži: Koji alat daje UUID? (Npr. GET /MyProfile za Tenant, GET /Vehicles za listu odabira).

Sustav mapira: Korisnik kaže "prvi auto", sustav to povezuje s UUID-om iz liste.

Sustav izvršava: POST /AddMileage ide s točnim UUID-om i brojem iz teksta.
### 🚩 FINALNA TOČKA: IDENTITET I TENANT RECOGNITION
Moraš pronaći je li potreban `TenantId` korisniku da ga uopće prepoznamo u sustavu. To znači da **identifikacija tenanta** nije samo "popunjavanje polja", već nulti korak za prepoznavanje usera. Ako sustav detektira da bez `TenantId` ne može dohvatiti niti osnovni profil korisnika, moraš implementirati **inicijalni poziv (Bootstrap)** odmah po ulasku u session, prije bilo kakve druge obrade, kako bi osigurao da user uopće "postoji" za naš sustav.⚙️ POSLJEDNJI OSIGURAČ: FALLBACK LOGIKA
Jedina stvar koju moramo imati na umu (i koju će sustav sam naučiti kroz record_failure) je što se događa ako Master Tool (npr. GET /MyProfile) ne vrati podatak.

Pravilo: Ako Bootstrap ne uspije osigurati required parametar, sustav ne smije nagađati. Mora reći: "Žao mi je, ne mogu dohvatiti tvoj profil kako bih izvršio ovu akciju." To je Digitalni Integritet. # PROMJENI OVAJ SUSTAV IZ KORIJENA # NAPRAVI DA OVAJ SUSTAV BUDI EFIKASAN DA ZAPRAVO SVE IMA SMISAO NEKU SVRHU DA TOČNO VIDIŠ KAKO SE PODACI DOBIVAJU I PREPOZNAJEŠ KADA ZAPRAVO RADIMO POSAO OD NIKUD RECIMO DA PIŠEMO KILOMETARSKE STVARI , PIŠEMO STVARI KOJE SE UOPĆE TAKO NE DOBIVAJU ZATO ŠTO NE ZNAŠ PORIJEKLO OD KUDA SE NEŠTO DOBIJE ILI NEKI DRUGI RAZLOG  NPR. PIŠEMO POSEBNU FUNKCIJU ZASEBNO ZA POZIVANJE SVAKOG TOOL , PIŠEMO OPISE ZA SVAKI ALAT U LISTU I ONDA TO SLUZI DA NEKAKVO ODREDIVANJE , TAKO OPĆENITO GOMILAMO STVARI I ONDA NA TEMELJU TOGA IMAMO ZNANJE NPR. SINONIMI , SLIČNE RIJEČI , HARKODIRANE STVARI KOJE ASOCIRAJU ITD ... ZNACI UGLAVNOM TO JE VELIKA GREŠKA NE RADI TOq
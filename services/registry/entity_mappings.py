"""
Entity mappings for Croatian language support in embedding generation.

Contains dictionaries mapping English API terms to Croatian translations,
synonyms, and segments to skip during path parsing.

These constants are used by EmbeddingEngine to generate Croatian-language
embeddings from English API definitions.
"""

import logging
from functools import lru_cache
from typing import Mapping, Tuple

from services.config_loader import load_json as _load_json

logger = logging.getLogger(__name__)


# Comprehensive entity mappings for path/operationId extraction.
# Data in config/domain/path_entity_map.json.
#
# Lazy-loaded — a missing/malformed JSON degrades to empty dict instead of
# crashing the worker at import time. embedding_engine code path is only
# exercised at build time (sync_tools.py), so degraded mode is acceptable
# for the runtime; sync_tools will fail loudly with the same logged error.

@lru_cache(maxsize=1)
def _get_path_entity_map() -> dict[str, tuple[str, str]]:
    try:
        data = _load_json("domain", "path_entity_map.json")
        m = data.get("path_entity_map") if isinstance(data, dict) else None
        if not isinstance(m, dict):
            logger.error(
                "path_entity_map.json: expected {'path_entity_map': {...}}, got %r",
                type(m),
            )
            return {}
        return {k: tuple(v) for k, v in m.items()}
    except (FileNotFoundError, ValueError, KeyError) as e:
        logger.error(
            "path_entity_map.json unloadable, embedding-text generation will be degraded: %s",
            e,
        )
        return {}


class _PathEntityMapProxy(dict):
    """Module-level constant proxy — resolves on access, never raises at import.

    Inherits from dict so isinstance(PATH_ENTITY_MAP, dict) passes for tests
    and code that type-checks. All read operations route to the cached getter.
    """
    def __getitem__(self, key):  # type: ignore[override]
        return _get_path_entity_map()[key]
    def __contains__(self, key) -> bool:  # type: ignore[override]
        return key in _get_path_entity_map()
    def get(self, key, default=None):  # type: ignore[override]
        return _get_path_entity_map().get(key, default)
    def __iter__(self):  # type: ignore[override]
        return iter(_get_path_entity_map())
    def __len__(self) -> int:  # type: ignore[override]
        return len(_get_path_entity_map())
    def items(self):  # type: ignore[override]
        return _get_path_entity_map().items()
    def keys(self):  # type: ignore[override]
        return _get_path_entity_map().keys()
    def values(self):  # type: ignore[override]
        return _get_path_entity_map().values()


PATH_ENTITY_MAP: Mapping[str, Tuple[str, str]] = _PathEntityMapProxy()  # type: ignore[assignment]

# Output key mappings for result description (v3.2 expanded - 200+ entries)
# Maps API output field names to Croatian descriptions
#
# COVERAGE CATEGORIES:
# - Vehicle & Fleet (35 entries)
# - Status & State (20 entries)
# - Documents & Registration (25 entries)
# - Time & Duration (30 entries)
# - Financial (30 entries)
# - Identity & Contact (25 entries)
# - Person Data (20 entries)
# - Booking & Reservation (20 entries)
# - Lists & Counts (15 entries)
# - Technical & System (20 entries)
OUTPUT_KEY_MAP = {
    # ---
    # VEHICLE & FLEET (35 entries)
    # ---
    "mileage": "kilometražu",
    "km": "kilometre",
    "odometer": "stanje kilometara",
    "odometervalue": "vrijednost kilometraže",
    "totalmileage": "ukupnu kilometražu",
    "fuel": "razinu goriva",
    "fuellevel": "razinu goriva",
    "fuelconsumption": "potrošnju goriva",
    "fueltype": "vrstu goriva",
    "fuelcapacity": "kapacitet spremnika",
    "averageconsumption": "prosječnu potrošnju",
    "battery": "stanje baterije",
    "batterylevel": "razinu baterije",
    "batteryvoltage": "napon baterije",
    "chargelevel": "razinu punjenja",
    "speed": "brzinu",
    "averagespeed": "prosječnu brzinu",
    "maxspeed": "maksimalnu brzinu",
    "enginehours": "radne sate motora",
    "enginestatus": "status motora",
    "ignition": "paljenje",
    "doors": "stanje vrata",
    "doorstatus": "status vrata",
    "trunk": "stanje prtljažnika",
    "windows": "stanje prozora",
    "lights": "stanje svjetala",
    "oilpressure": "tlak ulja",
    "oillevel": "razinu ulja",
    "tirePressure": "tlak u gumama",
    "coolanttemperature": "temperaturu rashladne tekućine",
    "vehicletype": "tip vozila",
    "vehicleclass": "klasu vozila",
    "vehiclecategory": "kategoriju vozila",
    "make": "proizvođača",
    "manufacturer": "proizvođača",

    # ---
    # LOCATION & POSITION (15 entries)
    # ---
    "location": "lokaciju",
    "currentlocation": "trenutnu lokaciju",
    "lastlocation": "zadnju lokaciju",
    "position": "poziciju",
    "coordinates": "koordinate",
    "latitude": "geografsku širinu",
    "longitude": "geografsku dužinu",
    "lat": "širinu",
    "lng": "dužinu",
    "altitude": "nadmorsku visinu",
    "heading": "smjer kretanja",
    "direction": "smjer",
    "address": "adresu",
    "fulladdress": "punu adresu",
    "geofence": "geofence zonu",

    # ---
    # STATUS & STATE (20 entries)
    # ---
    "status": "status",
    "state": "stanje",
    "currentstate": "trenutno stanje",
    "available": "dostupnost",
    "availability": "dostupnost",
    "isavailable": "je li dostupno",
    "active": "aktivnost",
    "isactive": "je li aktivno",
    "enabled": "omogućenost",
    "isenabled": "je li omogućeno",
    "locked": "zaključanost",
    "islocked": "je li zaključano",
    "online": "online status",
    "isonline": "je li online",
    "connected": "povezanost",
    "isconnected": "je li povezano",
    "operational": "operativnost",
    "condition": "stanje",
    "health": "zdravlje",
    "healthstatus": "status ispravnosti",

    # ---
    # REGISTRATION & DOCUMENTS (25 entries)
    # ---
    "registration": "registraciju",
    "registrationnumber": "registarsku oznaku",
    "plate": "tablice",
    "licenseplate": "registarske tablice",
    "platenumber": "broj tablica",
    "vin": "broj šasije",
    "chassisnumber": "broj šasije",
    "serialnumber": "serijski broj",
    "expiry": "datum isteka",
    "expirydate": "datum isteka",
    "expiration": "datum isteka",
    "expirationdate": "datum isteka",
    "validuntil": "vrijedi do",
    "validfrom": "vrijedi od",
    "validto": "vrijedi do",
    "issuedate": "datum izdavanja",
    "issuedby": "izdao",
    "issuedto": "izdano za",
    "documentnumber": "broj dokumenta",
    "documenttype": "tip dokumenta",
    "licensenumber": "broj licence",
    "licensetype": "tip licence",
    "licenseclass": "klasa licence",
    "insurancenumber": "broj osiguranja",
    "policynumber": "broj police",

    # ---
    # TIME & DURATION (30 entries)
    # ---
    "date": "datum",
    "time": "vrijeme",
    "datetime": "datum i vrijeme",
    "timestamp": "vremensku oznaku",
    "createdat": "datum kreiranja",
    "createddate": "datum kreiranja",
    "creationdate": "datum nastanka",
    "updatedat": "datum ažuriranja",
    "modifiedat": "datum izmjene",
    "modifieddate": "datum izmjene",
    "lastmodified": "zadnja izmjena",
    "startedat": "vrijeme početka",
    "startdate": "datum početka",
    "starttime": "vrijeme početka",
    "endedat": "vrijeme završetka",
    "enddate": "datum završetka",
    "endtime": "vrijeme završetka",
    "duration": "trajanje",
    "totalduration": "ukupno trajanje",
    "estimatedduration": "procijenjeno trajanje",
    "scheduleddate": "zakazani datum",
    "scheduledtime": "zakazano vrijeme",
    "duedate": "rok",
    "deadline": "krajnji rok",
    "period": "period",
    "timeperiod": "vremenski period",
    "year": "godinu",
    "month": "mjesec",
    "day": "dan",
    "hour": "sat",

    # ---
    # FINANCIAL (30 entries)
    # ---
    "price": "cijenu",
    "unitprice": "jediničnu cijenu",
    "totalprice": "ukupnu cijenu",
    "cost": "trošak",
    "totalcost": "ukupni trošak",
    "amount": "iznos",
    "totalamount": "ukupni iznos",
    "total": "ukupno",
    "subtotal": "međuzbroj",
    "grandtotal": "sveukupno",
    "tax": "porez",
    "taxamount": "iznos poreza",
    "taxrate": "stopu poreza",
    "vat": "PDV",
    "vatamount": "iznos PDV-a",
    "discount": "popust",
    "discountamount": "iznos popusta",
    "discountpercent": "postotak popusta",
    "balance": "stanje računa",
    "deposit": "polog",
    "depositamount": "iznos pologa",
    "refund": "povrat",
    "refundamount": "iznos povrata",
    "payment": "plaćanje",
    "paymentamount": "iznos uplate",
    "paymentstatus": "status plaćanja",
    "paymentmethod": "način plaćanja",
    "currency": "valutu",
    "rate": "stopu",
    "fee": "naknadu",

    # ---
    # IDENTIFICATION (25 entries)
    # ---
    "id": "identifikator",
    "uid": "jedinstveni ID",
    "guid": "globalni ID",
    "externalid": "vanjski ID",
    "internalid": "interni ID",
    "name": "naziv",
    "fullname": "puni naziv",
    "displayname": "prikazni naziv",
    "shortname": "kratki naziv",
    "title": "naslov",
    "description": "opis",
    "summary": "sažetak",
    "details": "detalje",
    "code": "šifru",
    "shortcode": "kratku šifru",
    "number": "broj",
    "reference": "referencu",
    "referencenumber": "referentni broj",
    "identifier": "identifikator",
    "key": "ključ",
    "label": "oznaku",
    "tag": "tag",
    "tags": "tagove",
    "category": "kategoriju",
    "type": "tip",

    # ---
    # CONTACT (15 entries)
    # ---
    "email": "e-mail",
    "emailaddress": "e-mail adresu",
    "phone": "telefon",
    "phonenumber": "broj telefona",
    "mobile": "mobitel",
    "mobilephone": "mobilni telefon",
    "fax": "fax",
    "website": "web stranicu",
    "url": "URL",
    "city": "grad",
    "street": "ulicu",
    "zip": "poštanski broj",
    "zipcode": "poštanski broj",
    "postalcode": "poštanski broj",
    "country": "državu",

    # ---
    # PERSON DATA (20 entries)
    # ---
    "firstname": "ime",
    "lastname": "prezime",
    "middlename": "srednje ime",
    "birthdate": "datum rođenja",
    "dateofbirth": "datum rođenja",
    "age": "dob",
    "gender": "spol",
    "sex": "spol",
    "nationality": "nacionalnost",
    "citizenship": "državljanstvo",
    "personalid": "osobni ID",
    "oib": "OIB",
    "ssn": "matični broj",
    "driverlicense": "vozačku dozvolu",
    "driverlicensenumber": "broj vozačke",
    "licenseexpiry": "istek vozačke",
    "passportnumber": "broj putovnice",
    "idcardnumber": "broj osobne",
    "occupation": "zanimanje",
    "employer": "poslodavca",

    # ---
    # BOOKING & RESERVATION (20 entries)
    # ---
    "bookingid": "ID rezervacije",
    "bookingnumber": "broj rezervacije",
    "bookingcode": "šifru rezervacije",
    "bookingstatus": "status rezervacije",
    "reservationid": "ID rezervacije",
    "reservationnumber": "broj rezervacije",
    "pickupdate": "datum preuzimanja",
    "pickuptime": "vrijeme preuzimanja",
    "pickuplocation": "mjesto preuzimanja",
    "returndate": "datum vraćanja",
    "returntime": "vrijeme vraćanja",
    "returnlocation": "mjesto vraćanja",
    "rentalperiod": "period najma",
    "rentaldays": "dane najma",
    "rentalhours": "sate najma",
    "extras": "dodatke",
    "optionalextras": "opcijske dodatke",
    "insurance": "osiguranje",
    "insurancetype": "tip osiguranja",
    "driver": "vozača",

    # ---
    # LISTS & COUNTS (15 entries)
    # ---
    "count": "broj",
    "totalcount": "ukupan broj",
    "itemcount": "broj stavki",
    "recordcount": "broj zapisa",
    "items": "stavke",
    "list": "popis",
    "results": "rezultate",
    "data": "podatke",
    "records": "zapise",
    "rows": "redove",
    "entries": "unose",
    "page": "stranicu",
    "pagenumber": "broj stranice",
    "pagesize": "veličinu stranice",
    "totalpages": "ukupno stranica",

    # ---
    # TECHNICAL & SYSTEM (20 entries)
    # ---
    "version": "verziju",
    "revision": "reviziju",
    "build": "build",
    "environment": "okruženje",
    "server": "server",
    "client": "klijent",
    "session": "sesiju",
    "token": "token",
    "apikey": "API ključ",
    "secret": "tajni ključ",
    "hash": "hash",
    "checksum": "kontrolnu sumu",
    "signature": "potpis",
    "encoding": "kodiranje",
    "format": "format",
    "mimetype": "MIME tip",
    "contenttype": "tip sadržaja",
    "size": "veličinu",
    "filesize": "veličinu datoteke",
    "length": "dužinu",
}

# Croatian synonyms for common user queries (v3.2 expanded - 60+ groups)
# Uses root forms to match both nominative and genitive (vozilo/vozila)
# Maps entity ROOT to list of alternative words users might use
#
# COVERAGE CATEGORIES:
# - Fleet & Vehicles (10 groups)
# - People & Roles (8 groups)
# - Bookings & Time (6 groups)
# - Documents & Records (8 groups)
# - Financial (8 groups)
# - Technical & Status (10 groups)
# - Communication (5 groups)
# - Data & Analytics (5 groups)
CROATIAN_SYNONYMS = {
    # ---
    # FLEET & VEHICLES (10 groups)
    # ---
    "vozil": ["auto", "automobil", "kola", "car", "autić"],  # vozilo, vozila
    "flot": ["fleet", "vozni park", "park vozila"],  # flota, flote
    "goriv": ["benzin", "nafta", "dizel", "fuel", "tank", "spremnik"],  # gorivo, goriva
    "kilometraž": ["km", "kilometri", "prijeđeno", "mileage", "stanje sata"],  # kilometraža
    "gum": ["tire", "pneumatik", "kotač"],  # guma, gume
    "baterij": ["akumulator", "battery", "punjenje"],  # baterija, baterije
    "ulj": ["oil", "mazivo", "motor oil"],  # ulje, ulja
    "registracij": ["tablice", "plates", "oznaka", "reg"],  # registracija
    "šasij": ["VIN", "chassis", "broj šasije"],  # šasija, šasije
    "oprema": ["equipment", "dodaci", "accessories"],  # oprema, opreme

    # ---
    # PEOPLE & ROLES (8 groups)
    # ---
    "osob": ["čovjek", "korisnik", "user", "person"],  # osoba, osobe
    "vozač": ["driver", "šofer", "vozar"],  # vozač, vozača
    "kupac": ["customer", "klijent", "mušterija", "stranka"],  # kupac, kupca
    "zaposlenik": ["employee", "radnik", "djelatnik", "worker"],  # zaposlenik
    "korisnik": ["user", "upotrebitelj", "account"],  # korisnik, korisnika
    "tim": ["team", "ekipa", "grupa", "odjel"],  # tim, tima
    "kontakt": ["contact", "osoba", "broj telefona"],  # kontakt, kontakta
    "organizacij": ["company", "tvrtka", "firma", "poduzeće"],  # organizacija

    # ---
    # BOOKINGS & TIME (6 groups)
    # ---
    "rezervacij": ["booking", "najam", "iznajmljivanje", "rent", "narudžba"],  # rezervacija
    "raspored": ["schedule", "plan", "kalendar", "termin"],  # raspored, rasporeda
    "putovanj": ["trip", "vožnja", "ruta", "journey", "put"],  # putovanje
    "dostupnost": ["slobodno", "available", "raspoloživo", "free"],  # dostupnost
    "termin": ["slot", "appointment", "vrijeme", "sat"],  # termin, termina
    "smjen": ["shift", "radno vrijeme", "tura"],  # smjena, smjene

    # ---
    # LOCATIONS (4 groups)
    # ---
    "lokacij": ["mjesto", "adresa", "pozicija", "location", "GPS"],  # lokacija
    "poslovnic": ["branch", "ured", "office", "prodajno mjesto"],  # poslovnica
    "rut": ["route", "put", "pravac", "itinerar"],  # ruta, rute
    "zon": ["zone", "područje", "region", "sektor"],  # zona, zone

    # ---
    # DOCUMENTS & RECORDS (8 groups)
    # ---
    "dokument": ["document", "papir", "spis", "akt"],  # dokument, dokumenta
    "ugovor": ["contract", "dogovor", "sporazum", "agreement"],  # ugovor
    "izvještaj": ["report", "pregled", "statistika", "analiza"],  # izvještaj
    "zapisnik": ["log", "record", "evidencija", "dnevnik"],  # zapisnik, zapisnika
    "povijes": ["history", "prošlost", "arhiva", "staro"],  # povijest, povijesti
    "certifikat": ["certificate", "potvrda", "svjedodžba"],  # certifikat
    "licenc": ["license", "dozvola", "odobrenje"],  # licenca, licence
    "polic": ["policy", "pravilo", "osiguranje"],  # polica, police

    # ---
    # FINANCIAL (8 groups)
    # ---
    "račun": ["faktura", "invoice", "naplata", "bill"],  # račun, računa
    "cijen": ["trošak", "cost", "price", "iznos", "tarifa"],  # cijena, cijene
    "plaćanj": ["uplata", "payment", "transakcija", "plata"],  # plaćanje
    "osiguranj": ["polica", "insurance", "kasko"],  # osiguranje
    "kazn": ["fine", "penalty", "globa", "prekršajna"],  # kazna, kazne
    "naknada": ["fee", "pristojba", "charge", "compensation"],  # naknada
    "depo": ["deposit", "polog", "jamčevina", "avans"],  # depozit, depozita
    "popust": ["discount", "sniženje", "akcija", "sale"],  # popust, popusta

    # ---
    # MAINTENANCE & SERVICE (6 groups)
    # ---
    "održavanj": ["servis", "service", "popravak", "maintenance"],  # održavanje
    "inspekcij": ["inspection", "pregled", "kontrola", "check"],  # inspekcija
    "štet": ["oštećenje", "damage", "kvar", "defekt"],  # šteta, štete
    "nesreć": ["accident", "sudar", "incident", "prometna"],  # nesreća
    "popravak": ["repair", "fix", "servis", "obnova"],  # popravak, popravka
    "kvar": ["malfunction", "defekt", "problem", "neispravnost"],  # kvar, kvara

    # ---
    # COMMUNICATION (5 groups)
    # ---
    "obavijest": ["notification", "notifikacija", "alert", "info"],  # obavijest
    "poruk": ["message", "sms", "email", "pismo"],  # poruka, poruke
    "upozorenje": ["warning", "alert", "alarm", "oprez"],  # upozorenje
    "komentar": ["comment", "napomena", "bilješka", "note"],  # komentar
    "zahtjev": ["request", "molba", "upit", "traženje"],  # zahtjev, zahtjeva

    # ---
    # DATA & ANALYTICS (5 groups)
    # ---
    "podac": ["data", "informacija", "info", "podatak"],  # podaci, podataka
    "statistik": ["statistics", "analitika", "metrics", "brojke"],  # statistika
    "grafikon": ["chart", "graph", "dijagram", "vizualizacija"],  # grafikon
    "pregled": ["overview", "dashboard", "summary", "sažetak"],  # pregled
    "rezultat": ["result", "output", "ishod", "odgovor"],  # rezultat, rezultata

    # ---
    # HANDOVER & TRANSFER (5 groups)
    # ---
    "preuzimanj": ["pickup", "primanje", "dohvat"],  # preuzimanje
    "vraćanj": ["return", "dropoff", "povratak"],  # vraćanje
    "primopredaj": ["handover", "primanje", "predaja"],  # primopredaja
    "prijav": ["checkin", "login", "registracija"],  # prijava, prijave
    "odjav": ["checkout", "logout", "odjavljivanje"],  # odjava, odjave

    # ---
    # CARDS & ACCESS (4 groups)
    # ---
    "kartic": ["card", "ENC", "fuel card", "smartcard"],  # kartica, kartice
    "ključ": ["key", "pristup", "otključavanje"],  # ključ, ključa
    "pristup": ["access", "dozvola", "ulaz"],  # pristup, pristupa
    "uređaj": ["device", "tracker", "GPS", "telematics"],  # uređaj, uređaja

    # ---
    # CATEGORIES & TYPES (3 groups)
    # ---
    "kategorij": ["category", "tip", "vrsta", "klasa"],  # kategorija
    "mark": ["brand", "proizvođač", "marka"],  # marka, marke
    "model": ["model", "verzija", "varijanta"],  # model, modela
}

# Common API prefixes to skip (not meaningful for embedding)
SKIP_SEGMENTS = {
    "api", "v1", "v2", "v3", "v4", "odata", "rest", "public", "private",
    "internal", "external", "admin", "management", "system", "core",
}

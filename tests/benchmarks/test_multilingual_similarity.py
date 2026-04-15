"""
Quick test: does multilingual-MiniLM understand Croatian entity semantics
better than ada-002?

Test: given a paraphrased query, does the model assign higher similarity
to the correct entity's tools vs wrong entity's tools?
"""
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

print("Loading multilingual-MiniLM-L12-v2...", flush=True)
t0 = time.time()
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
print(f"  Loaded in {time.time() - t0:.1f}s", flush=True)

# Test pairs: paraphrased query → which tool embedding text is closer?
test_cases = [
    {
        "query": "Trebam eliminirati razne sprave koje ne zadovoljavaju specifične kriterije.",
        "correct": "Oprema inventar alati - upravljanje opremom, inventarom, alatima. Brisanje stavke opreme.",
        "wrong": "Šifrarnik referentni podaci katalog - šifrarnici, lookup tablice. Brisanje stavke šifrarnika.",
        "expected_entity": "equipment",
    },
    {
        "query": "Želim izbrisati određenu stavku na temelju njenog identifikatora.",
        "correct": "Slučajevi predmeti štete kvarovi prijave - upravljanje slučajevima. Brisanje slučaja po ID-u.",
        "wrong": "Vozila automobili auti - upravljanje vozilima. Brisanje vozila po ID-u.",
        "expected_entity": "cases (ambiguous!)",
    },
    {
        "query": "Kako mogu pristupiti podacima o karakteristikama entiteta u sustavu?",
        "correct": "Metapodaci shema struktura polja - metapodaci, struktura i definicija polja.",
        "wrong": "Partneri dobavljači klijenti - upravljanje poslovnim partnerima.",
        "expected_entity": "metadata",
    },
    {
        "query": "Trebam se osloboditi jedne klasifikacije termina",
        "correct": "Tipovi periodične aktivnosti vrsta servisa - kategorije periodičnih aktivnosti. Brisanje tipa aktivnosti.",
        "wrong": "Tipovi troškova vrste troškova - kategorije troškova. Brisanje tipa troška.",
        "expected_entity": "periodicactivitytypes",
    },
    {
        "query": "Želim trajno izbrisati ispravu koja se odnosi na moje motorno sredstvo.",
        "correct": "Oprema inventar alati - upravljanje opremom. Brisanje dokumenta opreme.",
        "wrong": "Vozila automobili auti - upravljanje vozilima. Brisanje dokumenta vozila.",
        "expected_entity": "equipment (motorno sredstvo = motor vehicle OR motor device)",
    },
    {
        "query": "Molim te da mi pomogneš s uklanjanjem raznih prosječnih sredstava koja zadovoljavaju uvjete.",
        "correct": "Tipovi opreme vrste opreme - kategorije opreme. Brisanje tipova opreme po kriterijima.",
        "wrong": "Šifrarnik referentni podaci - šifrarnici. Brisanje šifrarnika po kriterijima.",
        "expected_entity": "equipmenttypes",
    },
    {
        "query": "Trebam popis svih financijskih stavki",
        "correct": "Troškovi izdaci računi rashodi - upravljanje troškovima, izdacima. Dohvaćanje svih troškova.",
        "wrong": "Partneri dobavljači klijenti - upravljanje partnerima. Dohvaćanje svih partnera.",
        "expected_entity": "expenses",
    },
    {
        "query": "Želim vidjeti raspored povezanih stavki za taj dan",
        "correct": "Kalendar opreme raspored opreme - rezervacije i raspoloživost opreme.",
        "wrong": "Kalendar vozila raspored vozila - rezervacije i raspoloživost vozila.",
        "expected_entity": "equipmentcalendar",
    },
]

from sentence_transformers.util import cos_sim

print(f"\n{'='*70}")
print("MULTILINGUAL SEMANTIC SIMILARITY TEST")
print(f"{'='*70}")

correct_wins = 0
for tc in test_cases:
    q_emb = model.encode(tc["query"])
    c_emb = model.encode(tc["correct"])
    w_emb = model.encode(tc["wrong"])

    sim_correct = float(cos_sim(q_emb, c_emb))
    sim_wrong = float(cos_sim(q_emb, w_emb))

    winner = "CORRECT" if sim_correct > sim_wrong else "WRONG"
    if sim_correct > sim_wrong:
        correct_wins += 1

    print(f"\n  Query: {tc['query'][:70]}")
    print(f"  Expected: {tc['expected_entity']}")
    print(f"  Sim(correct): {sim_correct:.4f}  Sim(wrong): {sim_wrong:.4f}  → {winner}")

print(f"\n{'='*70}")
print(f"Score: {correct_wins}/{len(test_cases)} correct wins")
print(f"{'='*70}")

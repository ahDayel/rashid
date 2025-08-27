# rag_index.py
from pathlib import Path
import json
from rapidfuzz import process, fuzz

DATA_DIR = Path(__file__).parent / "data"
PROGRAMS_PATH = DATA_DIR / "programs.json"
RULES_PATH = DATA_DIR / "rules.json"

def _load_json(p: Path):
    if not p.exists():
        raise FileNotFoundError(f"Missing data file: {p}")
    with p.open(encoding="utf-8") as f:
        return json.load(f)

PROGRAMS = _load_json(PROGRAMS_PATH)   # قائمة برامجك التي أرسلتِها
RULES    = _load_json(RULES_PATH)      # ملف rules.json الذي اعتمدتيه

# اصنع نصوصاً قابلة للبحث
PROGRAM_TEXTS = []
for i, p in enumerate(PROGRAMS):
    text = " ".join([
        p.get("page_title",""),
        p.get("description",""),
        " ".join(p.get("sidebar",{}).get("الفئة المستفيدة", [])),
        " ".join(sum(p.get("tabs",{}).values(), []))
    ])
    PROGRAM_TEXTS.append( (i, text) )

RULE_TEXTS = [(r["id"], f"{r.get('title','')} {r.get('content','')}") for r in RULES]
BY_RULE_ID = {r["id"]: r for r in RULES}

def search_programs(query: str, k: int = 5):
    corpus = {i: txt for i, txt in PROGRAM_TEXTS}
    res = process.extract(query, corpus, scorer=fuzz.token_set_ratio, limit=k)
    out = []
    for _, score, idx in res:                      
        out.append({"score": score, "program": PROGRAMS[idx]})
    return out

def search_rules(query: str, k: int = 3):
    corpus = {rid: txt for rid, txt in RULE_TEXTS}
    res = process.extract(query, corpus, scorer=fuzz.token_set_ratio, limit=k)
    out = []
    for _, score, rid in res:                     
        out.append({"score": score, "rule": BY_RULE_ID.get(rid, {})})
    return out
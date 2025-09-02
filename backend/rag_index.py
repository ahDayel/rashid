# rag_index.py
from pathlib import Path
import json
from rapidfuzz import process, fuzz

DATA_DIR = Path(__file__).parent / "data"
PROGRAMS_PATH = DATA_DIR / "programs.json"
RULES_PATH = DATA_DIR / "rules.json"

def _load_json(p: Path):
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return json.load(f)

# ===== تحميل JSON =====
PROGRAMS = _load_json(PROGRAMS_PATH)
RULES    = _load_json(RULES_PATH)

# ===== معالجة البرامج =====
PROGRAM_TEXTS = []
for i, p in enumerate(PROGRAMS):
    text = " ".join([
        p.get("page_title",""),
        p.get("description",""),
        " ".join(p.get("sidebar",{}).get("الفئة المستفيدة", [])),
        " ".join(sum(p.get("tabs",{}).values(), []))
    ])
    PROGRAM_TEXTS.append((i, text))

# ===== معالجة اللوائح =====
RULE_TEXTS = [(r["id"], f"{r.get('title','')} {r.get('content','')}") for r in RULES]
BY_RULE_ID = {r["id"]: r for r in RULES}

# ===== معالجة ملفات Markdown =====
DOCS = []
DOC_TEXTS = []
for md_file in DATA_DIR.glob("*.md"):
    with md_file.open(encoding="utf-8") as f:
        text = f.read()
        DOCS.append({"file": md_file.name, "content": text})
        DOC_TEXTS.append((md_file.name, text))

# ===== البحث =====
def search_programs(query: str, k: int = 5):
    corpus = {i: txt for i, txt in PROGRAM_TEXTS}
    res = process.extract(query, corpus, scorer=fuzz.token_set_ratio, limit=k)
    return [{"score": score, "program": PROGRAMS[idx]} for _, score, idx in res]

def search_rules(query: str, k: int = 3):
    corpus = {rid: txt for rid, txt in RULE_TEXTS}
    res = process.extract(query, corpus, scorer=fuzz.token_set_ratio, limit=k)
    return [{"score": score, "rule": BY_RULE_ID.get(rid, {})} for _, score, rid in res]

def search_docs(query: str, k: int = 3):
    corpus = {fname: txt for fname, txt in DOC_TEXTS}
    res = process.extract(query, corpus, scorer=fuzz.token_set_ratio, limit=k)
    out = []
    for fname, score, _ in res:
        match = next((d for d in DOCS if d["file"] == fname), None)
        if match:
            out.append({"score": score, "doc": match})
    return out

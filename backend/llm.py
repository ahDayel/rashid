# llm.py
import os
from typing import Dict, Any, List, Optional
import google.generativeai as genai
from dotenv import load_dotenv
from rag_index import search_programs, search_rules, search_docs

# ========= إعداد المفتاح =========
load_dotenv()
_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GENMINI_API_KEY") or os.getenv("YOUR_API_KEY")
if not _API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set")
genai.configure(api_key=_API_KEY)

_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

_SYSTEM = """
أنت "راشد"، مساعد كشك ذكي في الهاكاثونات ومبادرات منشآت.

المهام:
- اجعل إجاباتك قصيرة جداً (جملتان كحد أقصى).
- اعتمد دائماً على المقاطع المسترجعة من الملفات المتاحة (برامج، لوائح، Markdown…).
- إذا لم تجد ما يفيد في الملفات، قل: "ما لقيت شي واضح في الملفات".
- التزم باللباقة واللغة العربية الفصحى.
- عند الأسئلة المتعلقة بتطوير الأعمال، جاوب كخبير أعمال محترف.
"""

def _model():
    return genai.GenerativeModel(model_name=_MODEL, system_instruction=_SYSTEM)


def smart_answer(user_text: str, session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if session is None:
        session = {"history": [], "slots": {}, "pending_question": None}

    # سجل المحادثة
    session["history"].append({"role": "user", "content": user_text})

    # ابحث في كل المصادر
    prog_hits = search_programs(user_text, k=3)
    rule_hits = search_rules(user_text, k=3)
    doc_hits  = search_docs(user_text, k=3)

    ctx_lines, sources = [], []

    for h in prog_hits:
        prog = h.get("program", {})
        ctx_lines.append(f"[مبادرة] {prog.get('page_title','')}: {prog.get('description','')}")
        sources.append({"title": prog.get("page_title",""), "score": h.get("score",0)})

    for h in rule_hits:
        rule = h.get("rule", {})
        ctx_lines.append(f"[لائحة] {rule.get('title','')}: {rule.get('content','')}")
        sources.append({"title": rule.get("title",""), "score": h.get("score",0)})

    for h in doc_hits:
        doc = h.get("doc", {})
        ctx_lines.append(f"[ملف {doc.get('file','')}] {doc.get('content','')[:300]}")
        sources.append({"title": doc.get("file",""), "score": h.get("score",0)})

    if not ctx_lines:
        reply = "ما لقيت شي واضح في الملفات."
        session["history"].append({"role": "assistant", "content": reply})
        return {"mode": "general", "text": reply, "sources": [], "session": session}

    ctx = "\n".join(ctx_lines)

    # مرر السياق مع السؤال للـ LLM
    p = f"""سؤال المستخدم: {user_text}

مقاطع من الملفات:
{ctx}

جاوب بلباقه ورسميه وبلغه عربية فصيحه وركّز على المعلومات اللي في المقاطع.
"""
    reply = _model().generate_content(p).text.strip()
    session["history"].append({"role": "assistant", "content": reply})

    return {"mode": "general", "text": reply, "sources": sources, "session": session}

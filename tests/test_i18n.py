"""Two-language content (api/i18n.py) and the insight backfill script.

No network, no AWS, no real model. Run: python tests/test_i18n.py
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api import i18n  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f"  -> {detail}"))
    ok = ok and bool(cond)


print("── the G shape ──")
e = i18n.g_entry("Rafa sale del proyecto", "es", "Rafa is leaving the project")
check("entry keeps both versions, the original language and its hash",
      e["lang"] == "es" and e["es"].startswith("Rafa sale") and e["en"].startswith("Rafa is") and len(e["srcHash"]) == 12)
check("complete for the same original", i18n.is_complete(e, "Rafa sale del proyecto"))
check("not complete once the original is edited", not i18n.is_complete(e, "Rafa sale del proyecto el lunes"))
check("not complete with one version only", not i18n.is_complete(i18n.g_entry("Hola", "es"), "Hola"))
check("other language", i18n.other("es") == "en" and i18n.other("en") == "es")
check("unknown languages fall back to es", i18n.norm_lang("fr") == "es" and i18n.norm_lang("EN-us") == "en")

item = {"title": "Rafa sale del proyecto", "i18n": {"title": e}}
check("an English viewer gets the English version", i18n.pick(item, "title", "en") == {"text": "Rafa is leaving the project", "lang": "en", "pending": False})
check("a Spanish viewer gets the original", i18n.pick(item, "title", "es")["text"] == "Rafa sale del proyecto")
check("an old record (no i18n) is the original, pending for English",
      i18n.pick({"title": "Tarea creada"}, "title", "en") == {"text": "Tarea creada", "lang": "es", "pending": True})
check("an edited original is pending again", i18n.pick({"title": "Otro texto", "i18n": {"title": e}}, "title", "en")["pending"])


print("── the translator (fake model) ──")
class FakeLLM:
    """Answers the structured call: detects 'the' as English, else Spanish."""
    def __init__(self, skip=()):
        self.calls, self.skip, self.prompts = 0, set(skip), []

    def with_structured_output(self, schema, **_kw):
        outer = self

        class R:
            def invoke(self, messages):
                import json
                outer.calls += 1
                prompt = messages[0].content
                outer.prompts.append(prompt)
                items = []
                for line in prompt.split("\n"):
                    if line.startswith("{"):
                        d = json.loads(line)
                        if d["id"] in outer.skip:
                            continue
                        en = " the " in f" {d['text'].lower()} "
                        items.append({"id": d["id"], "lang": "en" if en else "es",
                                      "translation": ("ES: " if en else "EN: ") + d["text"]})
                return schema(items=items)
        return R()


llm = FakeLLM()
res = i18n.translate({"a": "Tarea creada: Diseño", "b": "Follow-up email sent to the client", "c": "  "}, llm=llm)
check("detects each language and writes the other",
      res["a"] == {"lang": "es", "translation": "EN: Tarea creada: Diseño"}
      and res["b"]["lang"] == "en" and res["b"]["translation"].startswith("ES:"), res)
check("empty texts are not sent", "c" not in res and '"id": "c"' not in llm.prompts[0])
llm2 = FakeLLM()
i18n.translate({"x": "línea uno\nlínea dos"}, llm=llm2)
check("a multi-line text is sent as one JSON line with \\n inside", '"línea uno\\nlínea dos"' in llm2.prompts[0], llm2.prompts[0][-200:])
big = {str(n): "x" * 5000 for n in range(5)}
llm3 = FakeLLM()
i18n.translate(big, llm=llm3)
check("long input is split into several calls (BATCH_CHARS)", llm3.calls >= 2, llm3.calls)
res = i18n.translate({"a": "uno", "b": "dos"}, llm=FakeLLM(skip={"b"}))
check("an id the model skipped is left out (retried next run)", "a" in res and "b" not in res)


print("── the backfill script (fake table) ──")
spec = importlib.util.spec_from_file_location("bf", ROOT / "scripts" / "dynamo" / "backfill_translations.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)


class Table:
    def __init__(self):
        self.updates = []

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        self.updates.append((Key, ExpressionAttributeValues[":m"]))


items = [
    {"userId": "u", "insightId": "1", "projectId": "p1", "type": "note", "title": "Rafa sale del proyecto", "description": ""},
    {"userId": "u", "insightId": "2", "projectId": "p1", "type": "followup",
     "title": "Follow-up email sent to the client", "description": "Subject: Avance"},
    {"userId": "u", "insightId": "3", "projectId": "p2", "type": "work_done", "title": "2026-10-11", "description": "123"},
    {"userId": "u", "insightId": "4", "projectId": "p1", "type": "note", "title": "Ya traducido",
     "i18n": {"title": i18n.g_entry("Ya traducido", "es", "Already translated")}},
]
todo = bf._todo(items, "")
fields = sorted((it["insightId"], f) for it, f, _t in todo)
check("only texts missing a version: empty, numbers/dates and complete ones are skipped",
      fields == [("1", "title"), ("2", "description"), ("2", "title")], fields)
check("--project filters", sorted(it["insightId"] for it, _f, _t in bf._todo(items, "p2")) == [])
t = Table()
it2 = items[1]
bf._write(t, it2, "title", i18n.g_entry(it2["title"], "en", "Correo de seguimiento enviado al cliente"))
bf._write(t, it2, "description", i18n.g_entry(it2["description"], "es", "Subject: Progress"))
key, last = t.updates[-1]
check("writes go to i18n only, keyed by the item, and the second field keeps the first",
      key == {"userId": "u", "insightId": "2"} and set(last) == {"title", "description"}
      and last["title"]["lang"] == "en" and it2["title"] == "Follow-up email sent to the client", (key, last))
check("after writing, those fields are no longer pending", not any(i is it2 for i, _f, _t in bf._todo(items, "")))

print("── what the API sends: versions per language ──")
card = {"title": "Rafa sale del proyecto", "description": "Raquel asume URLs",
        "i18n": {"title": i18n.g_entry("Rafa sale del proyecto", "es", "Rafa is leaving the project")}}
v = i18n.versions(card, {"title": "title", "detected": "title", "description": "description"})
check("versions: both languages of what is translated; the untranslated field is left out",
      v == {"es": {"title": "Rafa sale del proyecto", "detected": "Rafa sale del proyecto"},
            "en": {"title": "Rafa is leaving the project", "detected": "Rafa is leaving the project"}}, v)
check("versions: nothing when the original was edited", i18n.versions({**card, "title": "otro"}, {"title": "title"}) == {})

print("── new cards: background translation ──")
import os  # noqa: E402
from api.services import insight_i18n as II  # noqa: E402
import agent.tools as tools  # noqa: E402


class InsTable:
    def __init__(self):
        self.updates = {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        self.updates[Key["insightId"]] = ExpressionAttributeValues[":m"]


tools.insights_table = InsTable()
cards = [{"userId": "u", "insightId": "a", "title": "Tarea creada: Diseño", "description": "Por el acta"},
         {"userId": "u", "insightId": "b", "title": "Follow-up email sent to the client", "description": ""},
         {"userId": "u", "insightId": "c", "title": "2026-10-11"}]
n = II.translate_items(cards, llm=FakeLLM())
check("translate_items writes the missing versions of each card, in one call",
      n == 3 and set(tools.insights_table.updates) == {"a", "b"}
      and tools.insights_table.updates["a"]["title"]["en"] == "EN: Tarea creada: Diseño"
      and tools.insights_table.updates["b"]["title"]["lang"] == "en", tools.insights_table.updates)
check("a card already complete has nothing left to translate", II._texts(cards[0]) == [])
os.environ["I18N_AUTO"] = "off"
check("I18N_AUTO=off: queue_translation does nothing (no thread, no call)",
      II.queue_translation({"userId": "u", "insightId": "z", "title": "Hola mundo"}) is None and not II._worker_started)
os.environ.pop("I18N_AUTO")

print("\nOK" if ok else "\nFAILED")
sys.exit(0 if ok else 1)

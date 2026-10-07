"""Load a case's before.json the way production would see it.

A case keeps the ORIGINAL files (sources/) and each attachment in before.json
points at one with "sourceFile". The text is extracted here with the system's
own extractor (agent.document_parser.extract_text) -- the same code that runs
when a user uploads the file -- so a case never tests text that production
would not have produced. Text written into a fixture by hand, or by another
copy of pypdf, is exactly the inconsistency this avoids.
"""
import json
from pathlib import Path


def load_case_export(case_dir: Path) -> dict:
    from agent.document_parser import detect_extension, extract_text
    export = json.loads((case_dir / "before.json").read_text(encoding="utf-8"))
    for a in export.get("attachments") or []:
        src = a.get("sourceFile")
        if not src:
            continue  # a real export already carries fullText from the system
        path = case_dir / src
        ext = a.get("extension") or detect_extension(path.name)
        text = extract_text(path.read_bytes(), ext)
        a["fullText"] = text
        a["extractedTextLength"] = len(text)
    return export

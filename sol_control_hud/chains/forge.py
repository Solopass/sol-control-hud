"""Chain forge: describe a chain in words, a local model drafts the chain note (OBVLT plans/LOCAL_AI_AUTOMATION_PLAN.md phase 5).

    python -m sol_control_hud.chains forge "every Friday summarize my school notes into a study list" --title "Study list"

or in Obsidian: a note in 1Notebook\\Chains with `type: chain-request`, your description as the text, `status: queued`
(template *Chain request*). The chain runner drafts `<title>.md` next to it and links it from the request.
The model gets the real format guide and the starter chains as examples; the draft is checked by the same validator
as any chain (up to 3 tries, errors fed back). A forged chain is never started: it's saved as `status: draft`
(`paused` if it has a schedule or watch), so you read it first and queue it yourself.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import yaml

from . import chain_note as cn
from . import file_tools

TRIES = 3
EXAMPLES = ["Weekly digest.md", "Summarize a folder.md"]
RULES = """You write chain notes: Markdown files that a local AI runs step by step. Output ONLY the note, starting with
the `---` properties block; no explanation before or after, no code fences around it.

Hard rules:
- Properties block: `type: chain`, `status: draft`, `model:` (sol-fast unless the task needs more reasoning: sol-smart),
  optional `system:`, `inputs:` (a mapping of name: value), `schedule:`, `watch:`, `save_to:`, `save_steps:`.
- Each step is a `## Step name` heading followed by its prompt. Settings go on the first lines under the heading.
- A `gather:` step has only that one line. Folders must be real Windows paths under D:\\OBVLT, D:\\Polymatica Vault,
  D:\\Workspace, D:\\Output or D:\\AI\\Vault. Outputs (`save_to`) go under 1Notebook/.
- Refer to earlier steps as {{Step name}} (exact heading) or {{previous}}; to inputs as {{name}}; built-ins {{today}},
  {{week}}, {{now}}. Never use {{date}} or {{title}}. In a `for each file ...:` step use {{file.name}} and {{file.text}};
  in `for each line in {{Step}}:` use {{line}}.
- Keep it small: 2-6 steps. Every prompt says exactly what to write and how long.
"""


class ForgeError(ValueError):
    pass


def guide() -> str:
    parts = [RULES, "Format reference:\n" + (cn.__doc__ or "")]
    for name in EXAMPLES:
        p = cn.CHAINS_DIR / name
        if p.exists():
            parts.append(f"Example chain `{name}`:\n{p.read_text(encoding='utf-8-sig')}")
    return "\n\n".join(parts)


def extract_note(text: str) -> str:
    """The note from a model reply (drops thinking leftovers, fences and chatter around it)."""
    t = text.strip()
    fence = re.search(r"```(?:markdown|md)?\s*\n(.*?)```", t, re.S)
    if fence and "---" in fence.group(1):
        t = fence.group(1).strip()
    start = t.find("---")
    if start == -1:
        raise ForgeError("the draft has no properties block (---)")
    return t[start:].strip() + "\n"


def make_safe(note: str) -> str:
    """Force status: draft (paused with a schedule/watch) and type: chain, whatever the model wrote."""
    m = cn._FRONT.match(note)
    meta = (yaml.safe_load(m.group(1)) if m else None) or {}
    if not isinstance(meta, dict):
        raise ForgeError("the properties block isn't `name: value` lines")
    meta["type"] = "chain"
    meta["status"] = cn.PAUSED if (meta.get("schedule") or meta.get("watch")) else "draft"
    body = note[m.end():] if m else note
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip() + "\n---\n" + body.lstrip("\n")


def validate(note: str, title: str) -> list[str]:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / f"{file_tools.safe_name(title)}.md"
        p.write_text(note, encoding="utf-8")
        try:
            cn.to_workflow(cn.load_chain(p))
        except cn.ChainError as e:
            return [str(e)]
    return []


def forge(description: str, title: str, chat, model: str = "sol-fast", notify=None) -> tuple[str, int]:
    """(note text, tries). chat(model, messages, **kw) is a client's chat; notify(msg) reports progress."""
    messages = [{"role": "system", "content": guide()},
                {"role": "user", "content": f"Write a chain note titled \"{title}\" that does this:\n{description.strip()}"}]
    problems = ["no attempt"]
    for attempt in range(1, TRIES + 1):
        reply = chat(model, messages, max_tokens=8000, seed=7 + attempt).content
        try:
            note = make_safe(extract_note(reply))
            problems = validate(note, title)
        except (ForgeError, yaml.YAMLError) as e:
            problems, note = [str(e)], reply
        if not problems:
            return note, attempt
        if notify:
            notify(f"draft {attempt} had a problem: {problems[0]}")
        messages = messages[:2] + [{"role": "assistant", "content": reply[:8000]},
                                   {"role": "user", "content": f"That note has a problem: {problems[0]}\nWrite the whole note again, fixed."}]
    raise ForgeError(f"couldn't draft a valid chain in {TRIES} tries: {problems[0]}")


def save_draft(note: str, title: str, folder: Path | None = None) -> Path:
    folder = Path(folder or cn.CHAINS_DIR)
    name, n = file_tools.safe_name(title), 2
    target = folder / f"{name}.md"
    while target.exists():  # never overwrite an existing chain
        target = folder / f"{name} ({n}).md"; n += 1
    file_tools.check_write(target)
    folder.mkdir(parents=True, exist_ok=True)
    target.write_text(note, encoding="utf-8")
    return target


def request_text(path: Path) -> tuple[dict, str]:
    """(properties, description) of a `type: chain-request` note."""
    text = path.read_text(encoding="utf-8-sig")
    m = cn._FRONT.match(text)
    meta = (yaml.safe_load(m.group(1)) if m else None) or {}
    body = text[m.end():] if m else text
    body = re.sub(r"^>.*$", "", body, flags=re.M).strip()  # the template's help box isn't part of the description
    return (meta if isinstance(meta, dict) else {}), body

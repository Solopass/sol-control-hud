"""Chain notes: a series of prompts written as an Obsidian note, run as a workflow.
OBVLT plans/LOCAL_AI_AUTOMATION_PLAN.md phase 2. Chains live in 1Notebook\\Chains; results in 1Notebook\\Chains\\Results.

    ---
    type: chain
    status: draft              # draft | queued | running | done | needs-you | waiting | failed | cancelled
    model: sol-fast            # default for every step
    system: You are a concise technical editor.     # optional role for every step
    inputs:
      folder: D:\\Output\\Transcripts                 # used as {{folder}}
    save_to: 1Notebook\\Digests\\{{week}}.md          # optional: the last step's answer is also saved here
    ---
    Anything before the first ## heading is a description (not sent to the model).

    ## Outline
    Give me an outline for an essay on quiet rooms.

    ## Draft
    model: sol-smart
    reasoning: high
    check: at least 300 words
    Write the essay from this outline: {{Outline}}

    ## Per file
    for each file in {{folder}} matching *.md changed in the last 7 days:
    Summarize {{file.name}} in 3 bullets: {{file.text}}

    ## Index
    Combine these summaries into one index: {{Per file}}

Rules: each `## heading` is a step (headings inside ``` code blocks don't count). A step's first lines may be settings:
`model:`, `reasoning:` (off/on/low/medium/high), `check:` (`3-6 bullets`, `at least 300 words`, `at most 50 words`,
`mentions X`; repeat for more), `max tokens:`, `retries:`, `system:`, `long model:`, `temperature:`, and one loop:
`for each file in <folder> matching <glob>[ changed in the last N days]:` or `for each line in {{Step}}:`.
References: `{{Step name}}`, `{{previous}}`, an input's name, `{{file.name}}` / `{{file.text}}` / `{{line}}` in loops,
and `{{today}}` (2026-09-25), `{{week}}` (2026-W39), `{{now}}`. (Not {{date}}/{{title}}: Obsidian's templates use those.)
Triggers (phase 3, the chain runner `python -m sol_control_hud.chains daemon`): `status: queued` · `schedule: daily 07:00` /
`weekly Sun 20:00` / `on away` · `watch: D:\\Output\\Transcripts\\*.md` (one run per new file; `{{new file}}` is its path,
`{{new file text}}` its text) · `status: cancel` stops a running chain after its current step.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from . import file_tools, gather_tools
from .store import RESUMABLE, WHOLE, Store
from .workflow import Workflow, WorkflowError, parse

CHAINS_DIR = Path(os.environ.get("SOL_CHAINS", r"D:\OBVLT\1Notebook\Chains"))
VAULT = Path(os.environ.get("SOL_VAULT", r"D:\OBVLT"))
RESULTS_DIR_NAME = "Results"
DEFAULT_MODEL = "sol-fast"
DEFAULT_MAX_TOKENS = 4000   # desk lane: a chat of yours waits at most ~30 s behind a chain step (plan pre-flight change 1)
DEFAULT_LONG_MODEL = "sol-long"
CHUNK_TOKENS = 20000
PAUSED = "paused"  # schedule and watch are ignored while a chain is paused (examples ship paused)
STATUS = {"succeeded": "done", "needs_user": "needs-you", "failed": "failed", "waiting": "waiting",
          "cancelled": "cancelled", "interrupted": "interrupted", "running": "running", "queued": "queued"}
BUILTINS = {"today": "_today", "week": "_week", "now": "_now", "new file": "_new_file", "new file name": "_new_file_name",
            "new file text": "_new_file_text"}
# lanes (plan phase 3): which models a chain needs decides when it may run
FAST_MODELS = {"sol-fast", "sol-vision", "sol-desk"}          # the model you use anyway: runs any time (politely)
DESK_MODELS = FAST_MODELS | {"sol-smart", "sol-long"}          # served in Desk, but swapping them in waits until you're idle

_FRONT = re.compile(r"\A\ufeff?---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_HEADING = re.compile(r"^##\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SETTING = re.compile(r"^(model|reasoning|check|max tokens|retries|system|long model|temperature|hide|critic)\s*:\s*(.+?)\s*$", re.I)
_FOR_FILES = re.compile(r"^for each file in (.+?) matching (.+?)(?: changed in the last (\d+) days?)?\s*:\s*$", re.I)
_FOR_LINES = re.compile(r"^for each ([a-z_]\w*) (?:in|of) (.+?)\s*:\s*$", re.I)
_GATHER = re.compile(r"^gather\s*:\s*(.+?)\s*$", re.I)
_G_FILES = re.compile(r"^files in (.+?) matching (.+?)(?: changed in the last (\d+) days?)?$", re.I)
_G_COMMITS = re.compile(r"^commits in (.+?)(?: for the last (\d+) days?)?$", re.I)
_G_CHANGES = re.compile(r"^changes in (.+?) since (?:the )?last review$", re.I)
_REF = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_GROUPED = re.compile(r"^(.+?)\s+grouped by ([a-z_]\w*)$", re.I)
GROUP_TOKENS = 16000   # one part of a `grouped by` loop: leaves room in a 32k model for the prompt and the answer
PART_RESERVE_TOKENS = 3000   # the step's own instructions (~1k) + the answer room the runner keeps (2048)


def part_tokens(model: str | None, default: int) -> int:
    """How big one file chunk / group part may be for `model`: the fixed default, but never more than its context
    leaves room for. sol-fast holds 8k since 2026-10-07, so 20k chunks sent every big file to sol-long."""
    from .llm import load_registry
    try:
        ctx = (load_registry().get(model or "") or {}).get("num_ctx")
    except (OSError, ValueError, KeyError):
        ctx = None
    return max(1000, min(default, int(ctx) - PART_RESERVE_TOKENS)) if ctx else default


class ChainError(ValueError):
    pass


@dataclass
class ChainStep:
    name: str
    id: str
    line: int
    settings: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    loop: dict | None = None      # {"kind": "files", "folder", "glob", "days"} | {"kind": "lines", "var", "source"}
    prompt: str = ""
    gather: dict | None = None    # {"kind": "files"|"commits"|"changes", ...}: a step that collects, no model

    def fingerprint(self) -> str:
        blob = json.dumps([self.name, self.settings, self.checks, self.loop, self.prompt, self.gather], sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:12]


@dataclass
class Chain:
    path: Path
    name: str
    meta: dict
    steps: list[ChainStep]

    def meta_fingerprint(self) -> str:
        keep = {k: self.meta.get(k) for k in ("model", "system", "inputs", "reasoning", "max_tokens", "save_to", "save_steps",
                                              "critic_model")}
        return hashlib.sha1(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- parsing
def _norm(name: str) -> str:
    return " ".join(str(name).split()).lower()


def _slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "step"
    if base[0].isdigit():
        base = "s_" + base
    sid, n = base, 2
    while sid in taken:
        sid, n = f"{base}_{n}", n + 1
    taken.add(sid)
    return sid


def parse_check(text: str, where: str) -> dict:
    t = text.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(?:-|to)\s*(\d+)\s+(bullets?|bullet points?|words?)", t)
    if m:
        lo, hi, kind = int(m.group(1)), int(m.group(2)), "bullets" if m.group(3).startswith("bullet") else "words"
        return {"path": "", f"min_{kind}": lo, f"max_{kind}": hi}
    m = re.fullmatch(r"(?:exactly\s+)?(\d+)\s+(bullets?|bullet points?|words?)", t)
    if m:  # "5 bullets" = exactly 5 (words: within 10% either way)
        n, kind = int(m.group(1)), "bullets" if m.group(2).startswith("bullet") else "words"
        slack = 0 if kind == "bullets" else max(1, n // 10)
        return {"path": "", f"min_{kind}": n - slack, f"max_{kind}": n + slack}
    m = re.fullmatch(r"(at least|at most|no more than|max|min)\s+(\d+)\s+(bullets?|bullet points?|words?)", t)
    if m:
        kind = "bullets" if m.group(3).startswith("bullet") else "words"
        side = "min" if m.group(1) in ("at least", "min") else "max"
        return {"path": "", f"{side}_{kind}": int(m.group(2))}
    m = re.fullmatch(r"(?:mentions|contains|must mention)\s+(.+)", text.strip(), re.I)
    if m:
        return {"path": "", "contains": m.group(1).strip().strip('"')}
    raise ChainError(f"{where}: I don't understand `check: {text}`. Use e.g. `3-6 bullets`, `at least 300 words`, "
                     f"`at most 50 words` or `mentions budget`")


def parse_gather(text: str, where: str) -> dict:
    m = _G_FILES.match(text)
    if m:
        return {"kind": "files", "folder": m.group(1).strip(), "glob": m.group(2).strip(),
                "days": int(m.group(3)) if m.group(3) else None}
    m = _G_CHANGES.match(text)
    if m:
        return {"kind": "changes", "path": m.group(1).strip()}
    m = _G_COMMITS.match(text)
    if m:
        return {"kind": "commits", "path": m.group(1).strip(), "days": int(m.group(2)) if m.group(2) else 7}
    raise ChainError(f"{where}: I don't understand `gather: {text}`. Use `files in <folder> matching *.md changed in the "
                     f"last 7 days`, `commits in D:\\Workspace for the last 7 days` or `changes in D:\\Workspace since last review`")


def load_chain(path: str | Path) -> Chain:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ChainError(f"can't open {path}: {e}") from e
    meta, offset = {}, 0
    m = _FRONT.match(text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as e:
            raise ChainError(f"the properties at the top aren't valid YAML: {getattr(e, 'problem', e)}") from e
        if not isinstance(meta, dict):
            raise ChainError("the properties at the top must be `name: value` lines")
        offset = text[:m.end()].count("\n")
        text = text[m.end():]
    steps: list[ChainStep] = []
    taken: set[str] = set()
    names: set[str] = set()
    fence = False
    current: tuple[str, int, list[str]] | None = None
    blocks: list[tuple[str, int, list[str]]] = []
    for i, line in enumerate(text.splitlines(), start=offset + 1):
        if _FENCE.match(line):
            fence = not fence
        h = None if fence else _HEADING.match(line)
        if h:
            current = (h.group(1).strip(), i, [])
            blocks.append(current)
        elif current is not None:
            current[2].append(line)
    for name, line_no, body in blocks:
        where = f"step '{name}' (line {line_no})"
        if _norm(name) in names:
            raise ChainError(f"{where}: two steps are called '{name}'; give each step its own name")
        if _norm(name) in ("previous", *BUILTINS):
            raise ChainError(f"{where}: '{name}' is a reserved word; rename the step")
        names.add(_norm(name))
        step = ChainStep(name=name, id=_slug(name, taken), line=line_no)
        rest = list(body)
        while rest and not rest[0].strip():
            rest.pop(0)
        while rest:
            ln = rest[0].strip()
            s, ff, fl, g = _SETTING.match(ln), _FOR_FILES.match(ln), _FOR_LINES.match(ln), _GATHER.match(ln)
            if not (s or ff or fl or g):
                break
            rest.pop(0)
            if g:
                if step.gather:
                    raise ChainError(f"{where}: one `gather:` per step; add another step for more")
                step.gather = parse_gather(g.group(1), where)
            elif s:
                key, val = s.group(1).lower(), s.group(2)
                if key == "check":
                    step.checks.append(parse_check(val, where))
                else:
                    step.settings[key] = val
            elif step.loop:
                raise ChainError(f"{where}: a step can have only one `for each` line")
            elif ff:
                step.loop = {"kind": "files", "folder": ff.group(1).strip(), "glob": ff.group(2).strip(),
                             "days": int(ff.group(3)) if ff.group(3) else None}
            else:
                var = fl.group(1).lower()
                if var in ("inputs", "steps", "previous"):
                    raise ChainError(f"{where}: `for each {var}` - pick another name (e.g. item)")
                step.loop = {"kind": "lines", "var": var, "source": fl.group(2).strip()}
                gm = _GROUPED.match(step.loop["source"])
                if gm:  # `for each part in {{Per file}} grouped by repo:` - an earlier loop's answers, per repo, in parts
                    step.loop.update(source=gm.group(1).strip(), group=gm.group(2).lower())
        step.prompt = "\n".join(rest).strip()
        if step.gather:
            if step.prompt or step.loop or step.settings or step.checks:
                raise ChainError(f"{where}: a `gather:` step only collects; put the prompt and settings in the next step")
        elif not step.prompt:
            raise ChainError(f"{where} has no prompt: write what the model should do under the heading")
        steps.append(step)
    if not steps:
        raise ChainError("no steps: each step starts with a `## Step name` heading")
    return Chain(path=path, name=path.stem, meta=meta, steps=steps)


# ---------------------------------------------------------------- chain -> workflow
def _loop_var(step: ChainStep) -> str | None:
    if not step.loop:
        return None
    return "file" if step.loop["kind"] == "files" else step.loop["var"]


def _text_ref(step: ChainStep) -> str:
    return f"{{{{ steps.{step.id}__joined.output }}}}" if step.loop else f"{{{{ steps.{step.id}.output }}}}"


def _translate(text: str, chain: Chain, idx: int, where: str, loop_var: str | None = None, raw: bool = False) -> str:
    """Chain references -> workflow references. idx = this step's position (only earlier steps may be used)."""
    by_name = {_norm(s.name): (i, s) for i, s in enumerate(chain.steps)}
    inputs = {_norm(k): k for k in (chain.meta.get("inputs") or {})}

    def sub(m: re.Match) -> str:
        name = m.group(1).strip()
        key = _norm(name)
        head = key.split(".")[0]
        if loop_var and head == loop_var:
            return f"{{{{ {name} }}}}"
        if key == "previous":
            if idx == 0:
                raise ChainError(f"{where}: {{{{previous}}}} in the first step; there's no step before it")
            prev = chain.steps[idx - 1]
            return f"{{{{ steps.{prev.id}.output }}}}" if raw else _text_ref(prev)
        if key in BUILTINS:
            return f"{{{{ inputs.{BUILTINS[key]} }}}}"
        if key in by_name:
            j, s = by_name[key]
            if j >= idx:
                raise ChainError(f"{where}: {{{{{name}}}}} refers to a step that runs {'later' if j > idx else 'now'}; "
                                 f"a step can only use earlier steps")
            return f"{{{{ steps.{s.id}.output }}}}" if raw else _text_ref(s)
        if key in inputs:
            return f"{{{{ inputs.{inputs[key]} }}}}"
        options = [s.name for s in chain.steps[:idx]] + list((chain.meta.get("inputs") or {}).keys()) + \
            (["previous"] if idx else []) + list(BUILTINS) + ([f"{loop_var}.…"] if loop_var else [])
        raise ChainError(f"{where}: I don't know {{{{{name}}}}}. You can use: {', '.join(options) or '(nothing yet)'}")
    return _REF.sub(sub, text)


def to_workflow(chain: Chain) -> dict:
    meta = chain.meta
    default_model = str(meta.get("model") or DEFAULT_MODEL)
    inputs = [str(k) for k in (meta.get("inputs") or {})] + [*BUILTINS.values(), "_chain"]
    for k in (meta.get("inputs") or {}):
        if not re.fullmatch(r"[A-Za-z_]\w*", str(k)):
            raise ChainError(f"input '{k}': input names can only use letters, digits and _")
    steps: list[dict] = []
    review_ids = []
    for idx, s in enumerate(chain.steps):
        where = f"step '{s.name}' (line {s.line})"
        if s.gather:
            g = s.gather
            if g["kind"] == "files":
                steps.append({"id": s.id, "tool": "gather_files", "args": {
                    "folder": _translate(g["folder"], chain, idx, where), "glob": g["glob"], "since_days": g["days"]}})
            elif g["kind"] == "commits":
                steps.append({"id": s.id, "tool": "gather_commits",
                              "args": {"path": _translate(g["path"], chain, idx, where), "days": g["days"]}})
            else:
                steps.append({"id": s.id, "tool": "review_changes", "args": {"path": _translate(g["path"], chain, idx, where)}})
                review_ids.append(s.id)
            continue
        var = _loop_var(s)
        st = s.settings
        auto = st.get("model", default_model) == "auto"
        step: dict = {"id": s.id, "model": "sol-fast" if auto else st.get("model", default_model),
                      "prompt": _translate(s.prompt, chain, idx, where, var),
                      "max_tokens": int(st.get("max tokens", meta.get("max_tokens", DEFAULT_MAX_TOKENS))),
                      "retries": int(st.get("retries", 1))}
        long_model = st.get("long model", DEFAULT_LONG_MODEL if step["model"] in DESK_MODELS else None)  # sol-long isn't served in Away
        if long_model and long_model != step["model"]:
            step["long_model"] = long_model
        if auto:  # phase 5: sol-fast; if it fails its checks twice, sol-smart; too long, sol-long. In Away all of them
            step.setdefault("escalate_to", "sol-smart")  # are aliases of the Away model.
            step.setdefault("long_model", DEFAULT_LONG_MODEL)
        if "critic" in st:
            step["critic"] = {"criteria": _translate(st["critic"], chain, idx, where, var),
                              "model": meta.get("critic_model") or None}
        if "hide" in st and not s.loop:
            raise ChainError(f"{where}: `hide:` only works on a `for each` step")
        reasoning = st.get("reasoning", meta.get("reasoning"))
        if reasoning is not None:
            step["reasoning"] = {True: "on", False: "off"}.get(reasoning, str(reasoning).lower())
        if "system" in st:
            step["system"] = _translate(st["system"], chain, idx, where, var)
        if "temperature" in st:
            try:
                step["temperature"] = float(st["temperature"])
            except ValueError as e:
                raise ChainError(f"{where}: temperature must be a number") from e
        if s.checks:
            step["checks"] = s.checks
        if s.loop and s.loop["kind"] == "files":
            steps.append({"id": f"{s.id}__files", "tool": "list_files", "args": {
                "folder": _translate(s.loop["folder"], chain, idx, where), "glob": s.loop["glob"], "recursive": True,
                "chunk_tokens": part_tokens(step.get("model"), CHUNK_TOKENS), "since_days": s.loop["days"]}})
            source = f"{{{{ steps.{s.id}__files.output }}}}"
        elif s.loop and s.loop.get("group"):
            src = _translate(s.loop["source"], chain, idx, where, raw=True)
            m = re.fullmatch(r"\{\{ steps\.(\w+)\.output \}\}", src)
            prev = next((c for c in steps if m and c["id"] == m.group(1) and c.get("for_each")), None)
            if prev is None:
                raise ChainError(f"{where}: `grouped by` needs an earlier `for each` step, e.g. `for each part in {{{{Per file}}}} grouped by repo:`")
            src_step = next(c for c in chain.steps if c.id == prev["id"])
            steps.append({"id": f"{s.id}__items", "tool": "group_items", "args": {
                "items": prev["for_each"]["items"], "outputs": src, "key": s.loop["group"],
                "hide": src_step.settings.get("hide", ""), "chunk_tokens": part_tokens(step.get("model"), GROUP_TOKENS)}})
            source = f"{{{{ steps.{s.id}__items.output }}}}"
        elif s.loop:
            src = _translate(s.loop["source"], chain, idx, where, raw=True)
            if not re.fullmatch(r"\{\{ [\w.]+ \}\}", src):
                raise ChainError(f"{where}: `for each {s.loop['var']} in …` needs one step, e.g. `for each line in {{{{Ideas}}}}:`")
            steps.append({"id": f"{s.id}__items", "tool": "to_list", "args": {"value": src}})
            source = f"{{{{ steps.{s.id}__items.output }}}}"
        if s.loop:
            step["for_each"] = {"items": source, "as": var}
            step["continue_on_error"] = True
        steps.append(step)
        if s.loop:
            steps.append({"id": f"{s.id}__joined", "tool": "join_items",
                          "args": {"items": source, "outputs": f"{{{{ steps.{s.id}.output }}}}", "hide": st.get("hide", "")}})
    if meta.get("save_steps"):
        wanted = meta["save_steps"] if isinstance(meta["save_steps"], list) else str(meta["save_steps"]).split(",")
        known = {_norm(s.name) for s in chain.steps}
        bad = [str(n).strip() for n in wanted if _norm(str(n)) not in known]
        if bad:
            raise ChainError(f"save_steps: there's no step called {', '.join(repr(b) for b in bad)} "
                             f"(steps: {', '.join(s.name for s in chain.steps)})")
    for rid in review_ids:  # only reached when every earlier step succeeded: then the changes count as reviewed
        steps.append({"id": f"{rid}__mark", "tool": "mark_reviewed", "args": {"changes": f"{{{{ steps.{rid}.output }}}}"}})
    doc = {"name": f"chain: {chain.name}", "inputs": inputs, "steps": steps, "fallback": False}
    if meta.get("system"):
        doc["system"] = _translate(str(meta["system"]), chain, 0, "the `system` property (it can use inputs, not steps)")
    try:
        parse(doc)
    except WorkflowError as e:
        raise ChainError(str(e)) from e
    return doc


# ---------------------------------------------------------------- helper tools (hidden steps)
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")


def to_list(args: dict, ctx=None) -> list:
    v = args.get("value")
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            pass
        return [_BULLET_PREFIX.sub("", ln).strip() for ln in v.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    raise ValueError(f"can't loop over a {type(v).__name__}")


def _label(item) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or item.get("title") or item.get("file") or json.dumps(item)[:80])
    text = str(item).strip().replace("\n", " ")
    return text if len(text) <= 80 else text[:77] + "…"


def _as_text(out) -> str:
    if isinstance(out, str):
        return out.strip()
    if isinstance(out, dict) and set(out) == {"error"}:
        return f"_(skipped: {out['error']})_"
    return "```json\n" + json.dumps(out, indent=2, ensure_ascii=False) + "\n```"


def _hidden(out, hide: str) -> bool:
    if not hide or not isinstance(out, str):
        return False
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()
    return norm(out).startswith(norm(hide))


def join_items(args: dict, ctx=None) -> str:
    """Loop answers as `### label` sections. `hide`: answers starting with it (e.g. "No issues") are only counted."""
    items, outputs, hide = args.get("items") or [], args.get("outputs") or [], str(args.get("hide") or "")
    shown = [(i, o) for i, o in zip(items, outputs) if not _hidden(o, hide)]
    text = "\n\n".join(f"### {_label(i)}\n{_as_text(o)}" for i, o in shown)
    hidden = len(outputs) - len(shown)
    if hidden:
        note = f"_({hidden} more: {hide.strip().rstrip('.')}.)_"
        text = f"{text}\n\n{note}" if text else note
    return text


def group_items(args: dict, ctx=None) -> list[dict]:
    """An earlier loop's answers grouped by one field of its items (e.g. repo), each group cut into parts of at most
    `chunk_tokens` (~4 characters a token), so a later loop can read a big group in pieces.
    -> [{"name": "omni-tools (1/3)", "<key>": "omni-tools", "part": 1, "parts": 3, "files": 52, "text": "### …"}]"""
    items, outputs, hide = args.get("items") or [], args.get("outputs") or [], str(args.get("hide") or "")
    key, limit = str(args["key"]), int(args.get("chunk_tokens") or GROUP_TOKENS) * 4
    groups: dict[str, dict] = {}
    for item, out in zip(items, outputs):
        g = str(item.get(key, "?") if isinstance(item, dict) else "?")
        entry = groups.setdefault(g, {"sections": [], "hidden": 0})
        if _hidden(out, hide):
            entry["hidden"] += 1
        else:
            entry["sections"].append(f"### {_label(item)}\n{_as_text(out)}")
    result = []
    for g, entry in groups.items():
        parts: list[list[str]] = [[]]
        size = 0
        for sec in entry["sections"]:
            sec = sec if len(sec) <= limit else sec[:limit - 20] + "\n_(cut: too long)_"
            if parts[-1] and size + len(sec) > limit:
                parts.append([])
                size = 0
            parts[-1].append(sec)
            size += len(sec) + 2
        for n, secs in enumerate(parts, 1):
            text = "\n\n".join(secs)
            if entry["hidden"] and n == len(parts):
                note = f"_({entry['hidden']} more: {hide.strip().rstrip('.')}.)_"
                text = f"{text}\n\n{note}" if text else note
            name = g if len(parts) == 1 else f"{g} ({n}/{len(parts)})"
            result.append({"name": name, key: g, "part": n, "parts": len(parts), "files": len(secs), "text": text})
    return result


TOOLS = {"to_list": to_list, "join_items": join_items, "group_items": group_items, **gather_tools.TOOLS}


# ---------------------------------------------------------------- the chain note's own status line
def set_chain_status(path: Path, status: str, **extra) -> None:
    """Change only `status:` (and the given extra keys) in the chain note's properties; everything else stays byte-for-byte."""
    set_note_props(path, {"status": status, **extra})


def set_note_props(path: Path, props: dict, remove: tuple = ()) -> None:
    """Set the given keys (and drop the ones in `remove`) in a chain note's properties; everything else stays
    byte-for-byte. Only notes under the chains folder are ever written."""
    path = Path(path)
    if not file_tools._under(file_tools._real(path), [str(CHAINS_DIR)]):
        return
    with open(path, encoding="utf-8-sig", newline="") as f:  # newline="": keep CRLF/LF exactly as the note has them
        text = f.read()
    nl = "\r\n" if "\r\n" in text else "\n"
    m = _FRONT.match(text)
    if not m:
        if not props:
            return
        key, value = next(iter(props.items()))
        text = f"---{nl}{key}: {value}{nl}---{nl}" + text
        m = _FRONT.match(text)
    front = m.group(1)
    for key, value in props.items():
        line = f"{key}: {value}"
        pat = re.compile(rf"^{re.escape(key)}:[^\r\n]*", re.M)  # no `$`: a CRLF line has \r before the \n
        front = pat.sub(line, front, count=1) if pat.search(front) else front + nl + line
    for key in remove:                                        # the line and the line break before it
        front = re.sub(rf"(?:\r?\n)?^{re.escape(key)}:[^\r\n]*", "", front, count=1, flags=re.M)
    new = text[:m.start(1)] + front + text[m.end(1):]
    if new != text:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new, encoding="utf-8", newline="")
        os.replace(tmp, path)


# ---------------------------------------------------------------- the result note
def results_dir(chain: Chain) -> Path:
    return chain.path.parent / RESULTS_DIR_NAME


def result_name(chain: Chain) -> str:
    return f"{chain.name} (result)"  # not the chain's own name, so [[links]] stay unambiguous


def render_result(chain: Chain, store: Store, run_id: int) -> tuple[str, dict]:
    run = store.run(run_id) or {}
    saved = store.saved_outputs(run_id)
    status = STATUS.get(run.get("status", ""), run.get("status", ""))
    started = run.get("started_at") or run.get("created_at") or 0
    end = run.get("finished_at") if run.get("status") not in ("running", "queued") else None
    minutes = round(((end or datetime.now().timestamp()) - started) / 60, 1) if started else 0
    front = {"type": "chain-result", "chain": f"[[{chain.name}]]", "status": status, "run": run_id,
             "model": str(chain.meta.get("model") or DEFAULT_MODEL),
             "started": datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M") if started else "",
             "minutes": minutes}
    lines = [f"# {chain.name}", ""]
    reason = run.get("error")
    if status == "needs-you":
        lines += [f"> [!warning] Needs you: {reason}", "> Fix the chain note, then set its `status: queued` again. "
                  "Finished steps are kept; it continues from the first step you changed.", ""]
    elif status == "waiting":
        lines += [f"> [!info] Waiting: {reason}", "> It continues by itself when the model is available.", ""]
    elif status in ("failed", "interrupted", "cancelled"):
        lines += [f"> [!failure] {status.capitalize()}: {reason or ''}", "> Set the chain's `status: queued` to continue.", ""]
    running_marked = False
    for s in chain.steps:
        lines.append(f"## {s.name}")
        done = saved.get(s.id, {})
        if WHOLE in done and s.loop:
            items = (saved.get(f"{s.id}__files") or saved.get(f"{s.id}__items") or {}).get(WHOLE) or []
            lines.append(join_items({"items": items, "outputs": done[WHOLE], "hide": s.settings.get("hide", "")}) or "_(nothing to loop over)_")
        elif WHOLE in done and s.gather:
            lines.append(_gather_summary(done[WHOLE]))
        elif WHOLE in done:
            lines.append(_as_text(done[WHOLE]))
        elif s.loop and done:
            items = (saved.get(f"{s.id}__files") or saved.get(f"{s.id}__items") or {}).get(WHOLE) or []
            got = sorted(k for k in done if k != WHOLE)
            lines.append(join_items({"items": [items[k] for k in got if k < len(items)], "outputs": [done[k] for k in got], "hide": s.settings.get("hide", "")}))
            lines.append(f"\n_… {len(got)} of {len(items)} done_")
            running_marked = True
        elif status == "running" and not running_marked:
            lines.append("_running…_"); running_marked = True
        else:
            lines.append("_not run yet_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", front


def _step_text(step: ChainStep, saved: dict) -> str:
    key = f"{step.id}__joined" if step.loop else step.id
    return _as_text(saved.get(key, {}).get(WHOLE, ""))


def _saved_text(chain: Chain, saved: dict) -> str:
    """What `save_to` writes: the last step's answer, or the `save_steps:` steps as sections."""
    wanted = chain.meta.get("save_steps")
    if not wanted:
        return _step_text(chain.steps[-1], saved)
    names = [n.strip() for n in (wanted if isinstance(wanted, list) else str(wanted).split(","))]
    by_name = {_norm(s.name): s for s in chain.steps}
    parts = []
    for n in names:
        s = by_name.get(_norm(n))
        if s is None:
            raise ChainError(f"save_steps: there's no step called '{n}'")
        parts.append(f"## {s.name}\n{_step_text(s, saved)}")
    return "\n\n".join(parts)


def _gather_summary(out) -> str:
    if isinstance(out, list):
        repos = sorted({c.get("repo", "?") for c in out if isinstance(c, dict)})
        return f"_Collected {len(out)} changed file(s) to review" + (f" in {', '.join(repos)}" if repos else "") + "._"
    text = str(out)
    n = text.count("\n### ") + (1 if text.startswith("### ") else 0)
    return f"_Collected {n} item(s), {len(text) // 1000} KB of text._" if n else f"_{text.strip()[:200]}_"


def write_result(chain: Chain, store: Store, run_id: int) -> Path:
    text, front = render_result(chain, store, run_id)
    out = file_tools.save_note({"folder": str(results_dir(chain)), "name": result_name(chain), "text": text,
                                "frontmatter": front})
    return Path(out["path"])


# ---------------------------------------------------------------- running a chain
def builtins_now() -> dict:
    now = datetime.now()
    iso = now.isocalendar()
    return {"_today": now.strftime("%Y-%m-%d"), "_week": f"{iso[0]}-W{iso[1]:02d}", "_now": now.strftime("%Y-%m-%d %H:%M"),
            "_new_file": "", "_new_file_name": "", "_new_file_text": ""}


def lane(chain: Chain) -> str:
    """fast (sol-fast only: any time) | idle (other Desk models: when you're idle or Away) | away (Away models)."""
    models = {s.settings.get("model", chain.meta.get("model") or DEFAULT_MODEL) for s in chain.steps if not s.gather}
    models = {"sol-fast" if m == "auto" else m for m in models}
    if chain.meta.get("critic_model") and any("critic" in s.settings for s in chain.steps):
        models.add(str(chain.meta["critic_model"]))
    if models <= FAST_MODELS:
        return "fast"
    return "idle" if models <= DESK_MODELS else "away"


def _expand(text: str, values: dict) -> str:
    return _REF.sub(lambda m: str(values.get(_norm(m.group(1)), m.group(0))), text)


def plan_run(chain: Chain, store: Store, new: bool = False, extra: dict | None = None) -> tuple[int, bool, Workflow]:
    """(run id, resuming?, workflow). Resumes the chain's last stopped run unless its settings changed; steps from the
    first edited step on run again."""
    wf_doc = to_workflow(chain)
    wf = parse(wf_doc)
    prints = {s.id: s.fingerprint() for s in chain.steps}
    info = {"path": str(chain.path), "steps": prints, "meta": chain.meta_fingerprint()}
    last = store.latest_run(str(chain.path))
    if not new and last and last["status"] in RESUMABLE:
        old_inputs = json.loads(last["inputs"] or "{}")
        old = old_inputs.get("_chain") or {}
        if old.get("meta") == info["meta"]:
            redo: list[str] = []
            changed = False
            for s in chain.steps:
                changed = changed or old.get("steps", {}).get(s.id) != prints[s.id]
                if changed:
                    redo += [s.id, f"{s.id}__files", f"{s.id}__items", f"{s.id}__joined"]
            store.forget_outputs(last["id"], redo)
            defaults = {k: "" for k in builtins_now() if k not in old_inputs}  # built-ins added since that run started
            store.set_inputs(last["id"], {**defaults, **old_inputs, "_chain": info})
            return last["id"], True, wf
    inputs = {str(k): str(v) for k, v in (chain.meta.get("inputs") or {}).items()}
    inputs.update(builtins_now())
    inputs.update(extra or {})
    inputs["_chain"] = info
    return store.create_run(wf.name, inputs, source=str(chain.path)), False, wf


def run_chain(path: str | Path, store: Store, make_runner, new: bool = False, extra: dict | None = None) -> int:
    """Run (or resume) a chain note now, writing Results\\<name> (result).md as it goes. make_runner(on_step) -> Runner."""
    chain = load_chain(path)
    run_id, resuming, wf = plan_run(chain, store, new=new, extra=extra)
    runner = make_runner(lambda rid, step, item, out: write_result(chain, store, rid))
    set_chain_status(chain.path, "running", last_run=run_id)
    store.set_status(run_id, "running")
    write_result(chain, store, run_id)
    runner.resume(run_id, wf)
    run = store.run(run_id)
    status = STATUS.get(run["status"], run["status"])
    write_result(chain, store, run_id)
    if status == "done" and chain.meta.get("save_to"):
        now = builtins_now()
        values = {_norm(k): v for k, v in (chain.meta.get("inputs") or {}).items()}
        values.update({b: now[key] for b, key in BUILTINS.items()})
        target = _expand(str(chain.meta["save_to"]), values)
        if not os.path.isabs(target):
            target = str(VAULT / target)
        text = _saved_text(chain, store.saved_outputs(run_id))
        try:
            saved = file_tools.save_note({"path": target, "text": text,
                                          "frontmatter": {"chain": f"[[{chain.name}]]", "run": run_id,
                                                          "date": builtins_now()["_today"]}})
            store.event(run_id, "note", message=f"saved to {saved['path']}")
        except file_tools.FileToolError as e:
            store.event(run_id, "note", message=f"save_to failed: {e}")
            status = "needs-you"
            store.set_status(run_id, "needs_user", error=f"save_to: {e}")
            write_result(chain, store, run_id)
    set_chain_status(chain.path, status, last_run=run_id)
    return run_id


def write_error_result(path: str | Path, message: str) -> None:
    """A queued chain that can't even start (a typo, a missing step): say why where you'll look, and mark it needs-you."""
    path = Path(path)
    name = path.stem
    file_tools.save_note({"folder": str(path.parent / RESULTS_DIR_NAME), "name": f"{name} (result)",
                          "text": f"# {name}\n\n> [!warning] Needs you: this chain can't run yet\n> {message}\n> "
                                  "Fix the chain note, then set its `status: queued` again.\n",
                          "frontmatter": {"type": "chain-result", "chain": f"[[{name}]]", "status": "needs-you"}})
    set_chain_status(path, "needs-you")


def check_chain(path: str | Path, tools: dict, registry: dict) -> list[str]:
    try:
        chain = load_chain(path)
        doc = to_workflow(chain)
    except ChainError as e:
        return [str(e)]
    problems = []
    for s in doc["steps"]:
        if s.get("tool") and s["tool"] not in tools:
            problems.append(f"internal tool '{s['tool']}' is missing")
        for m in (s.get("model"), s.get("long_model")):
            if m and registry and m not in registry:
                problems.append(f"step '{s['id']}' uses model '{m}', which doesn't exist ({', '.join(sorted(registry))})")
    return problems

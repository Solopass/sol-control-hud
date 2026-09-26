"""Workflow files (workflows/*.yaml): readable, validated before anything runs.

Example:
  name: summarize-transcript
  inputs: [transcript, title]
  steps:
    - id: summary
      model: sol-fast                 # alias; resolved by the engine (llama-swap) or the model map
      escalate_to: sol-specialist     # optional: used after `retries` failed attempts
      prompt: |
        Summarize "{{ inputs.title }}": {{ inputs.transcript }}
      schema: {type: object, required: [bullets], properties: {bullets: {type: array, items: {type: string}}}}
      checks:
        - {path: bullets, min_items: 3, max_items: 6}
      retries: 2
      max_tokens: 8192
    - id: note
      tool: write_note                # plain code registered with the Runner; no model involved
      args: {summary: "{{ steps.summary.output }}", title: "{{ inputs.title }}"}

A step is either a model step (model + prompt) or a tool step (tool + args). An arg that is exactly one
`{{ ref }}` receives the referenced value as-is (dict, list, ...); anything else is rendered as text.

Chain options (2026-09-25, OBVLT plans/LOCAL_AI_AUTOMATION_PLAN.md phase 1):
  system: You are ...            # workflow-wide role; a step's own `system` replaces it
  fallback: true                 # workflow-wide: Away model names may use sol-smart in Desk mode (chains: false)
  steps:
    - id: per_file
      for_each: {items: "{{ steps.files.output }}", as: file}   # once per item; the output is the list of answers
      continue_on_error: true    # a failing item gives {"error": ...} instead of stopping the run
      model: sol-fast
      reasoning: off             # off | on (default) | low | medium | high
      temperature: 0.7           # optional; never 0 for thinking models
      long_model: sol-smart      # used when the prompt is too big for `model`'s context
      prompt: "Summarize {{ file.name }}: {{ file.text }}"
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import yaml


class WorkflowError(ValueError):
    pass


@dataclass
class Step:
    id: str
    model: str | None = None
    prompt: str | None = None
    schema: dict | None = None
    checks: list[dict] = field(default_factory=list)
    retries: int = 2
    escalate_to: str | None = None
    max_tokens: int = 8192
    tool: str | None = None
    args: dict = field(default_factory=dict)
    system: str | None = None
    reasoning: str | None = None
    temperature: float | None = None
    long_model: str | None = None
    for_each: dict | None = None          # {"items": "{{ ref }}", "as": "name"}
    continue_on_error: bool = False
    critic: dict | None = None            # {"criteria": str, "model": str | None} (phase 5)


@dataclass
class Workflow:
    name: str
    inputs: list[str]
    steps: list[Step]
    system: str | None = None
    fallback: bool = False


_REF = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)\s*\}\}")
_NAME = re.compile(r"^[a-zA-Z_]\w*$")
CHECK_KEYS = {"path", "min_items", "max_items", "contains", "matches", "equals", "min_length", "min_bullets", "max_bullets",
              "min_words", "max_words"}
REASONING = ("off", "on", "low", "medium", "high")
STEP_KEYS = {"id", "model", "prompt", "schema", "checks", "retries", "escalate_to", "max_tokens", "tool", "args", "system",
             "reasoning", "temperature", "long_model", "for_each", "continue_on_error", "critic"}


def load(path: str | Path) -> Workflow:
    return parse(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def parse(doc: dict) -> Workflow:
    if not isinstance(doc, dict) or not doc.get("name") or not isinstance(doc.get("steps"), list) or not doc["steps"]:
        raise WorkflowError("workflow needs a name and at least one step")
    inputs = list(doc.get("inputs") or [])
    steps: list[Step] = []
    seen: set[str] = set()
    for i, s in enumerate(doc["steps"]):
        if not isinstance(s, dict) or not s.get("id"):
            raise WorkflowError(f"step {i + 1} is missing 'id'")
        unknown = set(s) - STEP_KEYS
        if unknown:
            raise WorkflowError(f"step '{s['id']}' has unknown setting(s) {sorted(unknown)}")
        if s.get("tool"):
            if s.get("model") or s.get("prompt"):
                raise WorkflowError(f"step '{s['id']}' has both 'tool' and 'model'/'prompt'; pick one")
            if not isinstance(s.get("args") or {}, dict):
                raise WorkflowError(f"step '{s['id']}' args must be a mapping")
        else:
            for key in ("model", "prompt"):
                if not s.get(key):
                    raise WorkflowError(f"step '{s['id']}' is missing '{key}' (or use 'tool')")
        if s["id"] in seen:
            raise WorkflowError(f"duplicate step id '{s['id']}'")
        if isinstance(s.get("reasoning"), bool):  # YAML reads a bare `off`/`on` as false/true
            s = {**s, "reasoning": "on" if s["reasoning"] else "off"}
        if s.get("reasoning") is not None and s["reasoning"] not in REASONING:
            raise WorkflowError(f"step '{s['id']}' reasoning must be one of {', '.join(REASONING)}")
        if s.get("temperature") is not None and not isinstance(s["temperature"], (int, float)):
            raise WorkflowError(f"step '{s['id']}' temperature must be a number")
        if s.get("schema") is not None:
            try:
                jsonschema.Draft202012Validator.check_schema(s["schema"])
            except jsonschema.SchemaError as e:
                raise WorkflowError(f"step '{s['id']}' has an invalid schema: {e.message}") from e
        for c in s.get("checks") or []:
            bad = set(c) - CHECK_KEYS
            if bad or "path" not in c:
                raise WorkflowError(f"step '{s['id']}' check {c} is invalid (needs 'path'; allowed: {sorted(CHECK_KEYS)})")
        critic = s.get("critic")
        if critic is not None and (not isinstance(critic, dict) or not str(critic.get("criteria") or "").strip()):
            raise WorkflowError(f"step '{s['id']}' critic needs criteria: {{criteria: \"...\", model: optional}}")
        loop_var = None
        refs = _REF.findall(s.get("prompt") or "") + _REF.findall(s.get("system") or "") \
            + [r for v in _strings(s.get("args") or {}) for r in _REF.findall(v)]
        if s.get("for_each") is not None:
            fe = s["for_each"]
            whole = _REF.fullmatch(fe["items"].strip()) if isinstance(fe, dict) and isinstance(fe.get("items"), str) else None
            if not whole:
                raise WorkflowError(f"step '{s['id']}' for_each needs items: \"{{{{ ref }}}}\" (exactly one reference)")
            loop_var = fe.get("as") or "item"
            if not isinstance(loop_var, str) or not _NAME.match(loop_var) or loop_var in ("inputs", "steps"):
                raise WorkflowError(f"step '{s['id']}' for_each 'as' must be a simple name (not inputs/steps)")
            refs.append(whole.group(1))
        # every template reference must point at a declared input, an earlier step, or the loop variable
        for ref in refs:
            root, _, rest = ref.partition(".")
            if loop_var and root == loop_var:
                continue
            if root == "inputs" and rest.split(".")[0] not in inputs:
                raise WorkflowError(f"step '{s['id']}' uses undeclared input '{rest}'")
            if root == "steps" and rest.split(".")[0] not in seen:
                raise WorkflowError(f"step '{s['id']}' references '{rest.split('.')[0]}' before it runs")
            if root not in ("inputs", "steps"):
                raise WorkflowError(f"step '{s['id']}' has unknown reference '{{{{ {ref} }}}}'")
        steps.append(Step(id=s["id"], model=s.get("model"), prompt=s.get("prompt"), schema=s.get("schema"),
                          checks=list(s.get("checks") or []), retries=int(s.get("retries", 2)),
                          escalate_to=s.get("escalate_to"), max_tokens=int(s.get("max_tokens", 8192)),
                          tool=s.get("tool"), args=dict(s.get("args") or {}), system=s.get("system"),
                          reasoning=s.get("reasoning"), temperature=s.get("temperature"), long_model=s.get("long_model"),
                          for_each=({"items": s["for_each"]["items"].strip(), "as": loop_var} if loop_var else None),
                          continue_on_error=bool(s.get("continue_on_error", False)),
                          critic=({"criteria": str(critic["criteria"]), "model": critic.get("model")} if critic else None)))
        seen.add(s["id"])
    return Workflow(name=doc["name"], inputs=inputs, steps=steps, system=doc.get("system"),
                    fallback=bool(doc.get("fallback", False)))


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def references(template: str) -> list[str]:
    return _REF.findall(template or "")


def _lookup(ref: str, context: dict) -> object:
    cur: object = context
    for part in ref.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise WorkflowError(f"template value '{ref}' is not available")
    return cur


def render(template: str, context: dict) -> str:
    def value(match: re.Match) -> str:
        cur = _lookup(match.group(1), context)
        return cur if isinstance(cur, str) else yaml.safe_dump(cur, default_flow_style=True, allow_unicode=True).strip()
    return _REF.sub(value, template)


def resolve_args(args, context: dict):
    """Tool args: a string that is exactly one {{ ref }} passes the value through unchanged; other strings are rendered."""
    if isinstance(args, dict):
        return {k: resolve_args(v, context) for k, v in args.items()}
    if isinstance(args, list):
        return [resolve_args(v, context) for v in args]
    if isinstance(args, str):
        whole = _REF.fullmatch(args.strip())
        return _lookup(whole.group(1), context) if whole else render(args, context)
    return args

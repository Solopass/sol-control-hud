"""Runs one workflow at a time (one GPU job at a time).

Per step: render prompt -> call model -> parse JSON -> validate schema -> deterministic checks.
On failure: retry with the error fed back; after `retries`, escalate once to `escalate_to`;
still failing -> run status `needs_user` (stop and ask, never loop forever).
Engine down / timeout -> `failed` with the reason. Every transition is an event in SQLite.
Tool steps call plain Python registered as `tools={name: fn}`; `fn(args, ctx)` returns the step output.
A tool that raises fails the run (tools talk to real services; retrying them blindly is not safe).

Chains (2026-09-25, OBVLT plans/LOCAL_AI_AUTOMATION_PLAN.md phase 1):
- every finished step, and every finished for_each item, is saved at once; `resume(run_id, wf)` continues from there;
- a model that isn't served right now (Away model in Desk, game guard) -> status `waiting`, resumable, not `failed`;
- the prompt size is checked before each call (too big -> `long_model`, or a clear needs_user reason);
- `gpu_lock` (a context-manager factory, see gpulock.py) wraps every model call so processes take turns on the GPU;
- models never choose actions: tools and their arguments come only from the workflow, never from model output.
"""
from __future__ import annotations

import json
import re
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Callable

import jsonschema

from .llm import ChatClient, LLMError
from .store import WHOLE, Store
from .workflow import Step, Workflow, WorkflowError, references, render, resolve_args

try:  # optional: the router client's "not served right now" signal
    from .router import ModelUnavailable
except ImportError:  # pragma: no cover
    class ModelUnavailable(LLMError):  # type: ignore[no-redef]
        pass


MAX_RETRY_TOKENS = 16384  # ceiling when a step that ran out of tokens is retried with more room


class Cancelled(Exception):
    pass


@dataclass
class StepFailure(Exception):
    reason: str


class ToolError(Exception):
    """A tool step could not do its job (service down, bad input, ...)."""


@dataclass
class ToolContext:
    """Handed to tools: `note()` adds a progress event to the run, `cancelled()` lets long tools stop early."""
    run_id: int
    step: str
    store: Store
    cancel_event: threading.Event

    def note(self, message: str, **data) -> None:
        self.store.event(self.run_id, "note", self.step, message=message, **data)

    def cancelled(self) -> bool:
        return self.cancel_event.is_set() or self.store.cancel_requested(self.run_id)


Tool = Callable[[dict, ToolContext], object]


def _get(obj, path: str):
    if path == "":  # the whole output (e.g. a plain-text step)
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise StepFailure(f"check path '{path}' not found in output")
    return cur


def run_checks(output, checks: list[dict]) -> None:
    for c in checks:
        v = _get(output, c["path"])
        if "min_items" in c and (not isinstance(v, list) or len(v) < c["min_items"]):
            raise StepFailure(f"'{c['path']}' needs at least {c['min_items']} items")
        if "max_items" in c and (not isinstance(v, list) or len(v) > c["max_items"]):
            raise StepFailure(f"'{c['path']}' allows at most {c['max_items']} items")
        if "min_length" in c and len(str(v)) < c["min_length"]:
            raise StepFailure(f"'{c['path']}' is shorter than {c['min_length']} characters")
        if "contains" in c and str(c["contains"]).lower() not in str(v).lower():
            raise StepFailure(f"'{c['path']}' must mention '{c['contains']}'")
        if "matches" in c and not re.search(c["matches"], str(v)):
            raise StepFailure(f"'{c['path']}' must match /{c['matches']}/")
        if "equals" in c and v != c["equals"]:
            raise StepFailure(f"'{c['path']}' must equal {c['equals']!r}")
        if "min_bullets" in c or "max_bullets" in c:
            n = count_bullets(str(v))
            if n < c.get("min_bullets", 0) or n > c.get("max_bullets", 10**9):
                raise StepFailure(f"needs {_range(c.get('min_bullets'), c.get('max_bullets'))} bullet points, got {n}")
        if "min_words" in c or "max_words" in c:
            n = len(str(v).split())
            if n < c.get("min_words", 0) or n > c.get("max_words", 10**9):
                raise StepFailure(f"needs {_range(c.get('min_words'), c.get('max_words'))} words, got {n}")


_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+\S", re.M)


def count_bullets(text: str) -> int:
    return len(_BULLET.findall(text))


def _range(lo, hi) -> str:
    if lo is not None and hi is not None:
        return f"{lo}-{hi}"
    return f"at least {lo}" if lo is not None else f"at most {hi}"


def _default_item_loader(item: dict) -> str:
    from .file_tools import read_item  # lazy: file tools are optional for plain workflows
    return read_item(item)


class Runner:
    def __init__(self, client: ChatClient, store: Store, seed: int = 7, tools: dict[str, Tool] | None = None,
                 gpu_lock: Callable | None = None, item_loader: Callable[[dict], str] | None = None,
                 on_step: Callable[[int, str, int | None, object], None] | None = None,
                 gate: Callable[[int, str, Callable[[], bool]], None] | None = None):
        self.client, self.store, self.seed = client, store, seed
        self.tools = tools or {}
        self.gpu_lock = gpu_lock
        self.item_loader = item_loader or _default_item_loader
        self.on_step = on_step  # (run_id, step_id, item index or None, output) after each save; e.g. the chain result note
        self.gate = gate        # (run_id, model, stopping) before each model call; may wait (desk-lane politeness)
        self._cancel = threading.Event()
        self.current_run: int | None = None

    def cancel(self) -> None:
        self._cancel.set()

    def _stopping(self, run_id: int) -> bool:
        return self._cancel.is_set() or self.store.cancel_requested(run_id)

    # ---- entry points
    def run(self, wf: Workflow, inputs: dict, source: str | None = None) -> int:
        run_id = self.store.create_run(wf.name, inputs, source=source)
        return self._execute(run_id, wf, inputs, {})

    def resume(self, run_id: int, wf: Workflow) -> int:
        """Continue a stopped run (interrupted, waiting, needs_user, failed, cancelled): finished steps/items are reused."""
        run = self.store.run(run_id)
        if run is None:
            raise WorkflowError(f"no run {run_id}")
        if run["status"] == "succeeded":
            return run_id
        inputs = json.loads(run["inputs"] or "{}")
        saved = self.store.saved_outputs(run_id)
        self.store.event(run_id, "run_resumed", steps_done=sorted(k for k, v in saved.items() if WHOLE in v))
        return self._execute(run_id, wf, inputs, saved)

    def _execute(self, run_id: int, wf: Workflow, inputs: dict, saved: dict) -> int:
        missing = [k for k in wf.inputs if k not in inputs]
        if missing:
            self.store.set_status(run_id, "failed", error=f"missing inputs: {missing}")
            return run_id
        unknown = sorted({s.tool for s in wf.steps if s.tool and s.tool not in self.tools})
        if unknown:
            self.store.set_status(run_id, "failed", error=f"unknown tools: {unknown}")
            return run_id
        if hasattr(self.client, "allow_fallback"):
            self.client.allow_fallback = wf.fallback
        self._cancel.clear()
        self.current_run = run_id
        self.store.claim(run_id)
        self.store.set_status(run_id, "running")
        self.store.event(run_id, "run_started", workflow=wf.name, steps=[s.id for s in wf.steps])
        context: dict = {"inputs": inputs, "steps": {}}
        try:
            for step in wf.steps:
                done = saved.get(step.id, {})
                if WHOLE in done:
                    output = done[WHOLE]
                    self.store.event(run_id, "step_reused", step.id)
                else:
                    output = self._run_step(run_id, wf, step, context, done)
                    self.store.save_output(run_id, step.id, output)
                    self._notify(run_id, step.id, None, output)
                context["steps"][step.id] = {"output": output}
            outputs = {k: v["output"] for k, v in context["steps"].items()}
            self.store.set_status(run_id, "succeeded", outputs=outputs)
            self.store.event(run_id, "run_finished", status="succeeded")
        except Cancelled:
            self.store.set_status(run_id, "cancelled", error="cancelled by user")
            self.store.event(run_id, "run_finished", status="cancelled")
        except ModelUnavailable as e:
            self.store.set_status(run_id, "waiting", error=str(e))
            self.store.event(run_id, "run_finished", status="waiting", reason=str(e))
        except StepFailure as e:
            self.store.set_status(run_id, "needs_user", error=e.reason)
            self.store.event(run_id, "run_finished", status="needs_user", reason=e.reason)
        except (LLMError, WorkflowError, ToolError) as e:
            self.store.set_status(run_id, "failed", error=str(e))
            self.store.event(run_id, "run_finished", status="failed", reason=str(e))
        except Exception as e:  # noqa: BLE001 - a bug must still end the run, never leave it 'running'
            self.store.set_status(run_id, "failed", error=f"internal error: {type(e).__name__}: {e}")
            self.store.event(run_id, "run_finished", status="failed", reason=f"internal: {e}")
            raise
        finally:
            self.current_run = None
        return run_id

    def _notify(self, run_id: int, step_id: str, item: int | None, output) -> None:
        if self.on_step:
            try:
                self.on_step(run_id, step_id, item, output)
            except Exception as e:  # noqa: BLE001 - a display problem must never stop the run
                self.store.event(run_id, "note", step_id, message=f"progress hook failed: {type(e).__name__}: {e}")

    # ---- steps
    def _run_step(self, run_id: int, wf: Workflow, step: Step, context: dict, done_items: dict):
        if self._stopping(run_id):
            raise Cancelled()
        if not step.for_each:
            return self._run_once(run_id, wf, step, context)
        items = resolve_args(step.for_each["items"], context)
        if not isinstance(items, list):
            raise WorkflowError(f"step '{step.id}' for_each needs a list, got {type(items).__name__}")
        var, total, outputs = step.for_each["as"], len(items), []
        wants_text = any(r == f"{var}.text" for r in references((step.prompt or "") + (step.system or "")))
        self.store.event(run_id, "loop_started", step.id, items=total, already_done=len(done_items))
        for i, item in enumerate(items):
            if i in done_items:
                outputs.append(done_items[i])
                continue
            if self._stopping(run_id):
                raise Cancelled()
            if wants_text and isinstance(item, dict) and "path" in item and "text" not in item:
                item = {**item, "text": self.item_loader(item)}
            try:
                out = self._run_once(run_id, wf, step, {**context, var: item}, label=f"{i + 1}/{total}")
            except (StepFailure, ToolError, LLMError) as e:
                if isinstance(e, ModelUnavailable) or not step.continue_on_error:
                    raise
                reason = e.reason if isinstance(e, StepFailure) else str(e)
                out = {"error": reason}
                self.store.event(run_id, "item_failed", step.id, item=i + 1, of=total, reason=reason)
            self.store.save_output(run_id, step.id, out, item=i)
            self._notify(run_id, step.id, i, out)
            self.store.event(run_id, "item_done", step.id, item=i + 1, of=total)
            outputs.append(out)
        return outputs

    def _run_once(self, run_id: int, wf: Workflow, step: Step, context: dict, label: str | None = None):
        if step.tool:
            return self._run_tool(run_id, step, context)
        prompt = render(step.prompt, context)
        system_t = step.system if step.system is not None else wf.system
        system = render(system_t, context) if system_t else None
        started = time.monotonic()
        self.store.event(run_id, "step_started", step.id, model=step.model, **({"item": label} if label else {}))
        plan = [(step.model, step.retries + 1)] + ([(step.escalate_to, 1)] if step.escalate_to else [])
        last = "no attempts made"
        for model, attempts in plan:
            if model != step.model:
                self.store.event(run_id, "escalated", step.id, to=model, after=last)
            first = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
            model = self._fit_model(run_id, step, model, first)
            messages = list(first)
            budget = step.max_tokens
            for attempt in range(1, attempts + 1):
                if self._stopping(run_id):
                    raise Cancelled()
                result = self._chat(run_id, step, model, messages, attempt, max_tokens=budget)
                try:
                    output = self._accept(step, result)
                except StepFailure as e:
                    last = e.reason
                    self.store.event(run_id, "attempt", step.id, model=model, attempt=attempt, ok=False, reason=e.reason,
                                     completion_tokens=result.completion_tokens)
                    if result.finish_reason == "length":
                        # the same budget would run out again (thinking models): retry once with twice the room
                        budget = min(budget * 2, MAX_RETRY_TOKENS)
                        messages = list(first)
                        continue
                    # feed the problem back so the retry can fix it
                    messages = first + [{"role": "assistant", "content": result.content[:4000]},
                                        {"role": "user", "content": f"That answer was rejected: {e.reason}. Reply again, following the instructions exactly."}]
                    continue
                self.store.event(run_id, "attempt", step.id, model=model, attempt=attempt, ok=True, completion_tokens=result.completion_tokens)
                if step.critic:
                    output = self._critic_round(run_id, step, model, first, output, budget)
                self.store.event(run_id, "step_succeeded", step.id, model=model, seconds=round(time.monotonic() - started, 1),
                                 **({"item": label} if label else {}))
                return output
        self.store.event(run_id, "step_failed", step.id, reason=last)
        raise StepFailure(f"step '{step.id}' failed after retries and escalation: {last}")

    CRITIC_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["pass", "problems"],
                     "properties": {"pass": {"type": "boolean"}, "problems": {"type": "array", "items": {"type": "string"}}}}

    def _critic_round(self, run_id: int, step: Step, model: str, first: list[dict], output, budget: int):
        """Phase 5 critic: another look grades the answer against the criteria; a failing answer goes back once.
        Bounded: at most one critique and one improved answer; the improved answer is kept even if still imperfect."""
        critic_model = step.critic.get("model") or model
        answer = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        task = next((m["content"] for m in reversed(first) if m["role"] == "user"), "")
        ask = [{"role": "system", "content": "You are a strict reviewer. Judge only against the criteria. Reply as JSON."},
               {"role": "user", "content": f"Criteria:\n{step.critic['criteria']}\n\nThe task was:\n{task[:6000]}\n\n"
                                           f"The answer:\n{answer[:12000]}\n\nDoes the answer meet every criterion? "
                                           f"List the concrete problems (empty list if it passes)."}]
        verdict_step = Step(id=step.id, model=critic_model, prompt="", schema=self.CRITIC_SCHEMA, max_tokens=4000)
        try:
            verdict = json.loads(self._chat(run_id, verdict_step, critic_model, ask, 1).content)
        except (ValueError, TypeError):
            self.store.event(run_id, "critic", step.id, ok=None, reason="the critic's reply wasn't readable; answer kept")
            return output
        if verdict.get("pass") or not verdict.get("problems"):
            self.store.event(run_id, "critic", step.id, ok=True, model=critic_model)
            return output
        problems = "; ".join(str(p) for p in verdict["problems"][:8])
        self.store.event(run_id, "critic", step.id, ok=False, model=critic_model, problems=problems)
        retry = first + [{"role": "assistant", "content": answer[:6000]},
                         {"role": "user", "content": f"A reviewer found these problems: {problems}\n"
                                                     f"Write the whole answer again with them fixed, following the original instructions. "
                                                     f"Reply with only the corrected answer: no comments about the review or the changes."}]
        result = self._chat(run_id, step, model, retry, 99, max_tokens=budget)
        try:
            improved = self._accept(step, result)
        except StepFailure as e:
            self.store.event(run_id, "critic", step.id, ok=None, reason=f"improved answer rejected ({e.reason}); first answer kept")
            return output
        self.store.event(run_id, "critic_revised", step.id, model=model)
        return improved

    def _fit_model(self, run_id: int, step: Step, model: str, messages: list[dict]) -> str:
        """Budget check (phase 1.4): the prompt must fit the model's context, else long_model, else a clear stop."""
        fits = getattr(self.client, "fits", None)
        if fits is None:
            return model
        ok, tokens, ctx = fits(model, messages, step.max_tokens)
        if ok:
            return model
        if step.long_model and step.long_model != model:
            ok2, _, ctx2 = fits(step.long_model, messages, step.max_tokens)
            if ok2:
                self.store.event(run_id, "long_model", step.id, frm=model, to=step.long_model, prompt_tokens=tokens)
                return step.long_model
            ctx = max(ctx or 0, ctx2 or 0)
        raise StepFailure(f"step '{step.id}': the prompt is ~{tokens} tokens but {model} holds {ctx}; split the input "
                          f"with for_each or use a model with a bigger context (long_model)")

    def _chat(self, run_id: int, step: Step, model: str, messages: list[dict], attempt: int, max_tokens: int | None = None):
        kw: dict = {"schema": step.schema, "max_tokens": max_tokens or step.max_tokens, "seed": self.seed + attempt}
        if step.reasoning is not None:
            kw["reasoning"] = step.reasoning
        if step.temperature is not None:
            kw["temperature"] = step.temperature
        if hasattr(self.client, "resolve"):  # router client: keep the run's heartbeat fresh while it writes
            kw["on_progress"] = lambda _n: self.store.heartbeat(run_id)
        lock = self.gpu_lock(f"run {run_id} step {step.id}", should_stop=lambda: self._stopping(run_id),
                             on_wait=lambda who: self.store.event(run_id, "waiting_for_gpu", step.id, holder=who)) \
            if self.gpu_lock else nullcontext()
        if self.gate:
            self.gate(run_id, model, lambda: self._stopping(run_id))
            if self._stopping(run_id):
                raise Cancelled()
        try:
            with lock:
                return self.client.chat(model, messages, **kw)
        except Exception as e:
            if type(e).__name__ == "LockTimeout":
                raise Cancelled() from e
            raise

    def _run_tool(self, run_id: int, step: Step, context: dict):
        args = resolve_args(step.args, context)
        started = time.monotonic()
        self.store.event(run_id, "step_started", step.id, tool=step.tool)
        ctx = ToolContext(run_id, step.id, self.store, self._cancel)
        try:
            output = self.tools[step.tool](args, ctx)
        except (Cancelled, WorkflowError):
            raise
        except Exception as e:  # noqa: BLE001 - any tool failure ends the run with its reason
            reason = f"{type(e).__name__}: {e}" if not isinstance(e, ToolError) else str(e)
            self.store.event(run_id, "step_failed", step.id, tool=step.tool, reason=reason)
            raise ToolError(f"step '{step.id}' ({step.tool}) failed: {reason}") from e
        if ctx.cancelled():
            raise Cancelled()
        self.store.event(run_id, "step_succeeded", step.id, tool=step.tool, seconds=round(time.monotonic() - started, 1))
        return output

    @staticmethod
    def _accept(step: Step, result):
        if result.finish_reason == "length":
            raise StepFailure(f"ran out of tokens ({result.completion_tokens}); raise max_tokens or simplify the step")
        text = result.content.strip()
        if not text:
            raise StepFailure("empty answer")
        if step.schema is None:
            output: object = text
        else:
            try:
                output = json.loads(text)
            except json.JSONDecodeError as e:
                raise StepFailure(f"answer is not valid JSON ({e.msg})") from e
            errors = sorted(jsonschema.Draft202012Validator(step.schema).iter_errors(output), key=lambda er: er.path)
            if errors:
                raise StepFailure("schema: " + "; ".join(f"{'/'.join(map(str, er.path)) or '(root)'}: {er.message}" for er in errors[:3]))
        run_checks(output, step.checks)
        return output

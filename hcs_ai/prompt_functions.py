from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from typing import Any, Callable

from .db import connect, now_iso
from .knowledge import search as kb_search
from .llm import chat as llm_chat
from .tools import call_tool


class PromptScriptError(RuntimeError):
    pass


class _ReturnSignal(Exception):
    def __init__(self, value: Any):
        self.value = value


@dataclass
class RunResult:
    value: Any
    trace: list[dict[str, Any]]


class PromptRuntime:
    """Safe interpreter for HCS Prompt Functions.

    The syntax deliberately looks like a small Python subset, but no Python
    eval/exec is used. Only whitelisted statements, expressions, and HCS
    primitives are available.
    """

    def __init__(self, ask_fn: Callable[..., Any] | None = None,
                 tool_fn: Callable[..., Any] | None = None,
                 kb_fn: Callable[..., Any] | None = None):
        self.ask_fn = ask_fn or self._default_ask
        self.tool_fn = tool_fn or call_tool
        self.kb_fn = kb_fn or kb_search
        self.trace: list[dict[str, Any]] = []
        self.functions: dict[str, ast.FunctionDef] = {}

    def _log(self, event: str, **detail: Any) -> None:
        self.trace.append({"step": len(self.trace) + 1, "event": event, **detail})

    @staticmethod
    def _default_ask(prompt: str, context: Any = None, model: str | None = None,
                     use_kb: bool = True) -> Any:
        text = str(prompt)
        if context is not None:
            if isinstance(context, str):
                rendered = context
            else:
                rendered = json.dumps(context, indent=2, default=str)
            text += "\n\nCONTEXT:\n" + rendered
        return llm_chat(text, history=None, use_kb=bool(use_kb), model=model).get("text", "")

    def run(self, source: str, inputs: dict[str, Any] | None = None) -> RunResult:
        self.trace = []
        self.functions = {}
        try:
            module = ast.parse(source or "", mode="exec")
        except SyntaxError as exc:
            raise PromptScriptError(f"Prompt Function syntax error: {exc.msg} at line {exc.lineno}") from exc

        for node in module.body:
            if not isinstance(node, ast.FunctionDef):
                raise PromptScriptError("Only function definitions are allowed at the top level.")
            if node.decorator_list:
                raise PromptScriptError("Decorators are not supported in Prompt Functions.")
            if node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
                raise PromptScriptError("Use ordinary named parameters only.")
            self.functions[node.name] = node

        if "main" not in self.functions:
            raise PromptScriptError("A Prompt Function script must define main(...).")
        value = self._call_user_function("main", [], inputs or {}, keyword_inputs=True)
        self._log("return", value=value)
        return RunResult(value=value, trace=list(self.trace))

    def _call_user_function(self, name: str, args: list[Any], kwargs: dict[str, Any],
                            keyword_inputs: bool = False) -> Any:
        fn = self.functions.get(name)
        if fn is None:
            raise PromptScriptError(f"Unknown Prompt Function: {name}")
        params = [arg.arg for arg in fn.args.args]
        env: dict[str, Any] = {}
        for index, value in enumerate(args):
            if index >= len(params):
                raise PromptScriptError(f"Too many arguments for {name}().")
            env[params[index]] = value
        for key, value in kwargs.items():
            if key not in params:
                if keyword_inputs:
                    continue
                raise PromptScriptError(f"Unknown argument {key!r} for {name}().")
            env[key] = value
        defaults = list(fn.args.defaults)
        first_default = len(params) - len(defaults)
        for index, param in enumerate(params):
            if param in env:
                continue
            if index >= first_default:
                env[param] = self._eval(defaults[index - first_default], {})
            else:
                raise PromptScriptError(f"Missing required input: {param}")
        self._log("function_call", function=name, inputs={k: env[k] for k in params})
        try:
            self._exec_block(fn.body, env)
        except _ReturnSignal as signal:
            return signal.value
        return None

    def _exec_block(self, statements: list[ast.stmt], env: dict[str, Any]) -> None:
        for stmt in statements:
            self._exec_stmt(stmt, env)

    def _exec_stmt(self, stmt: ast.stmt, env: dict[str, Any]) -> None:
        if isinstance(stmt, ast.Assign):
            if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                raise PromptScriptError("Assignments must target one variable name.")
            value = self._eval(stmt.value, env)
            env[stmt.targets[0].id] = value
            self._log("assign", variable=stmt.targets[0].id, value=value)
            return
        if isinstance(stmt, ast.AnnAssign):
            if not isinstance(stmt.target, ast.Name) or stmt.value is None:
                raise PromptScriptError("Annotated assignments must target a variable and include a value.")
            value = self._eval(stmt.value, env)
            env[stmt.target.id] = value
            self._log("assign", variable=stmt.target.id, value=value)
            return
        if isinstance(stmt, ast.If):
            condition = bool(self._eval(stmt.test, env))
            self._log("if", condition=condition, line=getattr(stmt, "lineno", None))
            self._exec_block(stmt.body if condition else stmt.orelse, env)
            return
        if isinstance(stmt, ast.Return):
            raise _ReturnSignal(self._eval(stmt.value, env) if stmt.value else None)
        if isinstance(stmt, ast.Expr):
            self._eval(stmt.value, env)
            return
        raise PromptScriptError(f"Unsupported statement at line {getattr(stmt, 'lineno', '?')}: {type(stmt).__name__}")

    def _eval(self, node: ast.AST, env: dict[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            if node.id in ("True", "False", "None"):
                return {"True": True, "False": False, "None": None}[node.id]
            raise PromptScriptError(f"Unknown variable: {node.id}")
        if isinstance(node, ast.List):
            return [self._eval(item, env) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(self._eval(item, env) for item in node.elts)
        if isinstance(node, ast.Dict):
            return {self._eval(k, env): self._eval(v, env) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.Attribute):
            value = self._eval(node.value, env)
            if isinstance(value, dict):
                if node.attr.startswith("_"):
                    raise PromptScriptError("Private attributes are not available.")
                try:
                    return value[node.attr]
                except KeyError as exc:
                    raise PromptScriptError(f"Object has no field {node.attr!r}.") from exc
            raise PromptScriptError("Attribute access is only allowed on dictionaries returned by HCS functions.")
        if isinstance(node, ast.Subscript):
            value = self._eval(node.value, env)
            key = self._eval(node.slice, env)
            return value[key]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(self._eval(node.operand, env))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -self._eval(node.operand, env)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._eval(node.left, env) + self._eval(node.right, env)
        if isinstance(node, ast.BoolOp):
            values = [self._eval(v, env) for v in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, env)
            for op, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator, env)
                if isinstance(op, ast.Eq): ok = left == right
                elif isinstance(op, ast.NotEq): ok = left != right
                elif isinstance(op, ast.Lt): ok = left < right
                elif isinstance(op, ast.LtE): ok = left <= right
                elif isinstance(op, ast.Gt): ok = left > right
                elif isinstance(op, ast.GtE): ok = left >= right
                elif isinstance(op, ast.In): ok = left in right
                elif isinstance(op, ast.NotIn): ok = left not in right
                else: raise PromptScriptError("Unsupported comparison operator.")
                if not ok:
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self._eval(node.body if self._eval(node.test, env) else node.orelse, env)
        if isinstance(node, ast.Call):
            return self._call(node, env)
        raise PromptScriptError(f"Unsupported expression: {type(node).__name__}")

    def _call(self, node: ast.Call, env: dict[str, Any]) -> Any:
        if not isinstance(node.func, ast.Name):
            raise PromptScriptError("Only named functions can be called.")
        name = node.func.id
        args = [self._eval(arg, env) for arg in node.args]
        kwargs = {kw.arg: self._eval(kw.value, env) for kw in node.keywords if kw.arg}
        if name in self.functions:
            return self._call_user_function(name, args, kwargs)
        if name == "ask":
            result = self.ask_fn(*args, **kwargs)
            self._log("ask", prompt=str(args[0] if args else kwargs.get("prompt", ""))[:500], result=result)
            return result
        if name == "ask_json":
            text = self.ask_fn(*args, **kwargs)
            try:
                result = json.loads(text)
            except Exception as exc:
                raise PromptScriptError("ask_json expected the model to return valid JSON.") from exc
            self._log("ask_json", result=result)
            return result
        if name == "tool":
            if not args:
                raise PromptScriptError("tool(name, args={}) requires a tool name.")
            tool_args = args[1] if len(args) > 1 else kwargs.get("args", {})
            result = self.tool_fn(str(args[0]), tool_args or {})
            self._log("tool", tool=str(args[0]), args=tool_args, result=result)
            return result
        if name == "kb":
            if not args:
                raise PromptScriptError("kb(query, limit=4) requires a query.")
            limit = int(args[1] if len(args) > 1 else kwargs.get("limit", 4))
            result = self.kb_fn(str(args[0]), limit=max(1, min(limit, 20)))
            self._log("kb", query=str(args[0]), results=len(result) if hasattr(result, "__len__") else None)
            return result
        safe = {
            "len": len, "str": str, "int": int, "float": float, "bool": bool,
            "min": min, "max": max, "round": round,
        }
        if name in safe:
            return safe[name](*args, **kwargs)
        raise PromptScriptError(f"Function {name!r} is not available in Prompt Functions.")


def _ensure_tables() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS prompt_scripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prompt_script_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                script_id INTEGER NOT NULL REFERENCES prompt_scripts(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(script_id, version)
            );
            """
        )


def save_script(name: str, source: str) -> dict[str, Any]:
    _ensure_tables()
    name = (name or "").strip()
    if not name:
        raise ValueError("Prompt Function name is required.")
    module = ast.parse(source or "", mode="exec")
    if not any(isinstance(node, ast.FunctionDef) and node.name == "main" for node in module.body):
        raise ValueError("Prompt Function must define main(...).")
    now = now_iso()
    with connect() as con:
        row = con.execute("SELECT id,version,created_at FROM prompt_scripts WHERE name=?", (name,)).fetchone()
        if row:
            script_id = int(row["id"])
            version = int(row["version"]) + 1
            con.execute(
                "UPDATE prompt_scripts SET source=?,version=?,updated_at=? WHERE id=?",
                (source, version, now, script_id),
            )
        else:
            cur = con.execute(
                "INSERT INTO prompt_scripts(name,source,version,created_at,updated_at) VALUES(?,?,?,?,?)",
                (name, source, 1, now, now),
            )
            script_id = int(cur.lastrowid)
            version = 1
        con.execute(
            "INSERT INTO prompt_script_revisions(script_id,version,source,created_at) VALUES(?,?,?,?)",
            (script_id, version, source, now),
        )
    return get_script(name)


def get_script(name: str) -> dict[str, Any]:
    _ensure_tables()
    with connect() as con:
        row = con.execute(
            "SELECT id,name,source,version,created_at,updated_at FROM prompt_scripts WHERE name=?",
            ((name or "").strip(),),
        ).fetchone()
    if not row:
        raise KeyError(f"Prompt Function not found: {name}")
    return dict(row)


def list_scripts() -> list[dict[str, Any]]:
    _ensure_tables()
    with connect() as con:
        rows = con.execute(
            "SELECT id,name,version,created_at,updated_at FROM prompt_scripts ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


def history(name: str) -> list[dict[str, Any]]:
    script = get_script(name)
    with connect() as con:
        rows = con.execute(
            "SELECT version,created_at,source FROM prompt_script_revisions WHERE script_id=? ORDER BY version DESC",
            (script["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


def run_script(source: str, inputs: dict[str, Any] | None = None,
               ask_fn: Callable[..., Any] | None = None,
               tool_fn: Callable[..., Any] | None = None,
               kb_fn: Callable[..., Any] | None = None) -> dict[str, Any]:
    result = PromptRuntime(ask_fn=ask_fn, tool_fn=tool_fn, kb_fn=kb_fn).run(source, inputs)
    return {"value": result.value, "trace": result.trace}

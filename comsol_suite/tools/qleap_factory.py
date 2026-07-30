"""qleap factory control-plane tools — read-only views of the F0-F3 record chain.

The agent could see individual campaigns (``qleap_*_status``) but had no way to
read the *factory* state: which scopes hold a signed acceptance record, what the
open andons are, what the line's own plan says comes next. Asked "how many scopes
are accepted in F2 and is F3 sealed?", a model therefore fell back to generic
filesystem tools, guessed at paths, and answered "the factory has not been
initialised" while `simulations/_factory/records/` held signed F0-F3 records.

That is a tool gap, not a model failure, and these three tools close it:

    qleap_factory_status   the whole record chain: per-phase accepted scopes,
                           andons, embargoes, ledger chain state
    qleap_factory_line     the line definition (phases, stations, gates, human
                           checkpoints) parsed from FACTORY.md's floor plan
    qleap_factory_record   one scope's full acceptance record

All three are pure reads, so they carry no ``dry_run`` argument by design (see
``qleap_chipconstruction._preflight_sync`` for why mutating tools must). They
are also design-agnostic: the scope grammar comes from the active design in
``simulations/_designs/``, never from a literal in here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import load_config
from ..runner import run_command


def _repo_root() -> Path:
    """The qleap repo, from the config's own marker-based resolution.

    NOT ``chip_sim_root.parent``: that read the containment root as if it were
    the legacy assets dir, so every campaign path landed one level ABOVE the
    checkout — where a status call silently created an empty shadow
    ``simulations/ChipConstruction/`` tree outside the repo.
    """
    return Path(load_config().repo_root)


def _factory_home() -> Path:
    return _repo_root() / "simulations" / "_factory"


def _factory_tools() -> Path:
    return _factory_home() / "tools"


def _uv_argv(script: Path, args: list[str]) -> list[str]:
    return ["uv", "run", "--no-project", "python", str(script), *args]


def _run_json(tool: str, argv: list[str], timeout_s: float = 180) -> Dict[str, Any]:
    """Run one of the offline control-plane CLIs and return its parsed JSON.

    These print a JSON document on stdout with ``--json``; `run_command` captures
    to a log, so parse the log tail rather than assuming a stdout pipe.
    """
    log_path = _factory_home() / "logs" / f"{tool}_last.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    res = run_command(argv, log_path=log_path, cwd=_repo_root(), timeout_s=timeout_s)
    text = res.log_tail(4000)
    parsed: Optional[Any] = None
    idx = text.find("{")
    while idx != -1 and parsed is None:
        try:
            parsed = json.loads(text[idx:])
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
    return {
        "ok": res.ok,
        "returncode": res.returncode,
        "parsed": parsed,
        "log_tail": None if parsed is not None else res.log_tail(40),
    }


def qleap_factory_status() -> Dict[str, Any]:
    """Where every artifact stands on the F0-F3 line.

    Returns, per phase: the scopes holding a signed acceptance record (with the
    record hash, when it was created, how many caveats it carries and how many
    prior records it superseded), plus the count of quarantined attempts. Also
    the open andons (line stops), the embargo count, and whether the append-only
    ledger's hash chain still verifies.

    This is the tool to answer "is F3 sealed?", "what is accepted in F2?", "is
    anything stopping the line?" — do not try to infer it by listing directories.
    """
    argv = _uv_argv(_factory_tools() / "factory_status.py", ["--json"])
    return _run_json("qleap_factory_status", argv)


def qleap_factory_line() -> Dict[str, Any]:
    """The line's own plan: phases in order, the stations inside each, the gate
    every station must clear, the subagent that owns it, its MCP tools, and the
    human checkpoints.

    Parsed from ``QubitDesignPipeline/FACTORY.md``'s floor-plan table, so it is
    the same definition the web UI renders and a human edits — not a second copy.
    Use it to work out what comes next and what has to hold before it may start;
    ``unresolved`` lists any row the parser could not make sense of.
    """
    argv = _uv_argv(_factory_tools() / "line_spec.py", ["--json"])
    return _run_json("qleap_factory_line", argv)


def qleap_factory_record(phase: str, scope: str) -> Dict[str, Any]:
    """One scope's full acceptance record (``records/<phase>/<scope>/accepted.json``).

    ``phase`` is F0/F1/F2/F3; ``scope`` is ``chip``, a tile (``U0_R0``) or a
    qubit (``U0_R0_A``) — whatever ``qleap_factory_status`` listed. The record
    carries the achieved values, the artifacts with their sha256s, the gate
    reports, any caveats, the human sign-offs, and the record hash that chains it
    to its work order.
    """
    path = _factory_home() / "records" / phase / scope / "accepted.json"
    if not path.is_file():
        return {
            "ok": False,
            "error": f"no acceptance record at {path.relative_to(_repo_root())}",
            "hint": "call qleap_factory_status first to see which scopes have one",
        }
    try:
        return {"ok": True, "path": str(path), "record": json.loads(path.read_text())}
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"record is not valid JSON: {exc}"}

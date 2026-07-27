"""Poka-yoke path containment for caller-supplied MCP tool arguments.

Every MCP tool that accepts a free-form filesystem path (a GDS to check, an
``.mph`` to solve, a YAML to edit, ...) runs it through
:func:`ensure_contained` before dispatching to the underlying implementation.
Any path that resolves *outside* ``CHIP_SIM_ROOT`` (the suite's configured
working universe — see :func:`comsol_suite.config.load_config`, resolution
order env > ``config/paths.toml`` > repo-parent default) is rejected with a
:class:`ValueError` that names the offending tool, argument, path, and the
allowed root.

This is a **poka-yoke against mistakes, not a security boundary**: the MCP
server runs unsandboxed as the same user, and a determined caller has plenty
of other levers. Its job is to catch honest slips — a stray absolute path
from another machine, a typo'd ``..`` traversal, an output redirected into
``$HOME`` — before a tool touches the filesystem. For unattended or remote
operation the actual boundary is the Bubblewrap SSH gateway
(``QubitDesignPipeline/security/restricted_agent_ssh.sh``), exactly as its
README states.

Symlinks and ``..`` components are neutralised by ``Path.resolve()`` before
the containment check, so ``<root>/a/../../etc/passwd`` is rejected even
though it textually starts with the root.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

from .config import load_config

__all__ = ["ensure_contained"]


def ensure_contained(path_str: Union[str, os.PathLike], *,
                     arg: str, tool: str) -> Path:
    """Reject ``path_str`` unless it resolves inside ``CHIP_SIM_ROOT``.

    Parameters
    ----------
    path_str
        The caller-supplied path (string or path-like). ``~`` is expanded
        and symlinks / ``..`` components are resolved before checking.
    arg
        Name of the tool argument the path arrived through (for the error
        message only).
    tool
        Name of the MCP tool performing the check (for the error message
        only).

    Returns
    -------
    Path
        The fully-resolved absolute path (callers may use it or keep
        passing the original string — behaviour-preserving either way).

    Raises
    ------
    ValueError
        If the resolved path is not inside the configured
        ``chip_sim_root``. The message names the tool, the argument, the
        offending path, and the allowed root, plus the env knob to widen it.
    """
    # Reuse the config's chip_sim_root resolution (env CHIP_SIM_ROOT >
    # config/paths.toml > repo-parent default) rather than reimplementing it.
    root = Path(load_config().chip_sim_root).expanduser().resolve()
    resolved = Path(path_str).expanduser().resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(
            f"{tool}: argument {arg!r} = {str(path_str)!r} resolves to "
            f"{resolved}, which is outside the allowed root {root} "
            f"(CHIP_SIM_ROOT). This containment check is a poka-yoke "
            f"against mistaken paths, not a security boundary — if the "
            f"path is genuinely correct, point CHIP_SIM_ROOT (env or "
            f"config/paths.toml) at a directory that contains it."
        )
    return resolved

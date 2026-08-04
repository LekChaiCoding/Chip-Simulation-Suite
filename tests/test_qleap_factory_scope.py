"""`qleap_factory_record` joins model-supplied strings into a filesystem path.

`phase` and `scope` arrive from a language model and were concatenated straight
into ``_factory_home()/records/<phase>/<scope>/accepted.json`` with no check.

This is not an adversarial concern, it is the ORDINARY case. Measured against
Qwen/Qwen3.6-27B-FP8 on 2026-08-04: asked about tile U0_R0 it sent
``scope="tile U0_R0"`` in 2 of 3 runs. And a segment that climbed out of the repo
root made the *error branch itself* raise — ``Path.relative_to`` throws
ValueError outside its argument — so a typo returned an uncaught traceback
rather than an error dict.

The containment check is deliberately separate from the scope GRAMMAR: tiles and
letters come from the active design and are not hardcoded here. A well-formed
name that does not exist must still give an ordinary not-found.
"""

from __future__ import annotations

from comsol_suite.tools.qleap_factory import qleap_factory_record


def test_a_real_scope_still_returns_its_record():
    """The check must not cost the tool its actual job."""
    out = qleap_factory_record("F0", "chip")
    assert out["ok"] is True, out
    assert "record" in out and out["record"]


def test_the_scope_qwen_actually_sent_is_refused_with_guidance():
    """`"tile U0_R0"` — 2 of 3 live runs. The message has to teach the shape."""
    out = qleap_factory_record("F2", "tile U0_R0")
    assert out["ok"] is False
    assert "not a single record-tree name" in out["error"]
    # Names the valid shapes and the tool that lists them, so the model can
    # recover on its own rather than guessing again.
    assert "U0_R0" in out["error"] and "chip" in out["error"]
    assert "qleap_factory_status" in out["error"]


def test_traversal_never_reaches_the_filesystem():
    for phase, scope in (
        ("F2", "../../../../etc"),
        ("..", ".."),
        ("F2", "../../_designs"),
        ("F2", "U0_R0/../../.."),
    ):
        out = qleap_factory_record(phase, scope)
        assert out["ok"] is False, (phase, scope)
        assert "not a single record-tree name" in out["error"], (phase, scope)


def test_an_empty_segment_is_refused_rather_than_collapsing_the_path():
    """`Path("a") / "" == Path("a")`, so an empty scope would silently read the
    phase directory instead of a scope's record."""
    assert qleap_factory_record("F2", "")["ok"] is False
    assert qleap_factory_record("", "chip")["ok"] is False


def test_a_wellformed_but_absent_scope_gives_an_ordinary_not_found():
    """Containment and existence are different questions, and stay so."""
    out = qleap_factory_record("F9", "chip")
    assert out["ok"] is False
    assert "no acceptance record at" in out["error"]
    # Repo-relative, and reported without raising — the old code's error branch
    # called relative_to unconditionally.
    assert out["error"].startswith("no acceptance record at simulations/")
    assert "hint" in out


def test_refusals_are_dicts_and_never_exceptions():
    """An MCP tool that raises gives the model a traceback it cannot act on."""
    for phase, scope in (("..", ".."), ("F2", "tile U0_R0"), ("F2", "")):
        out = qleap_factory_record(phase, scope)
        assert isinstance(out, dict) and set(out) >= {"ok", "error"}

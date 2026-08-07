"""KANBAN_GUIDANCE injection + context-cost discipline (HEL-3137).

The lifecycle block is the single always-injected worker instruction
surface (agent/prompt_builder.py → agent_init → system_prompt). Unit
tests here prove:
1. workers get the block,
2. normal chat does not,
3. the HEL-3137 cost-discipline language is present,
4. size stays bounded so the cached system prompt does not bloat.
"""

from __future__ import annotations


def test_kanban_guidance_not_in_normal_prompt(monkeypatch, tmp_path):
    """A normal chat session (no HERMES_KANBAN_TASK) must NOT have
    KANBAN_GUIDANCE in its system prompt."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from tools.registry import invalidate_check_fn_cache
    from model_tools import _clear_tool_defs_cache
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()

    from run_agent import AIAgent
    a = AIAgent(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    prompt = a._build_system_prompt()
    assert "You are a Kanban worker" not in prompt
    assert "kanban_show()" not in prompt
    assert "Context-cost discipline" not in prompt


def test_kanban_guidance_in_worker_prompt(monkeypatch, tmp_path):
    """A worker session (HERMES_KANBAN_TASK set) MUST have the full
    lifecycle + context-cost guidance in its system prompt."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from tools.registry import invalidate_check_fn_cache
    from model_tools import _clear_tool_defs_cache
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()

    from run_agent import AIAgent
    a = AIAgent(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    prompt = a._build_system_prompt()
    # Header phrase (identity-free — SOUL.md owns identity, layer 3 is protocol)
    assert "Kanban task execution protocol" in prompt
    # Lifecycle signals
    assert "kanban_show()" in prompt
    assert "kanban_complete" in prompt
    assert "kanban_block" in prompt
    assert "kanban_create" in prompt
    # Anti-shell guidance
    assert "Do not shell out" in prompt or "tools — they work" in prompt
    # Context-cost discipline (HEL-3137)
    assert "Context-cost discipline" in prompt
    assert "kanban_show(task_id=...)" in prompt
    assert "full-board dump" in prompt
    assert "full-file reads" in prompt
    assert "offset" in prompt and "limit" in prompt


def test_kanban_guidance_prompt_size_bounded():
    """Sanity: the guidance block stays lean so it doesn't blow up the
    cached prompt.

    Ceiling allows the reference details folded in when standalone
    kanban-worker / kanban-orchestrator skills were removed, plus the
    HEL-3137 context-cost discipline section, with a little headroom.
    """
    from agent.prompt_builder import KANBAN_GUIDANCE

    assert 1_500 < len(KANBAN_GUIDANCE) < 6_000, (
        f"KANBAN_GUIDANCE is {len(KANBAN_GUIDANCE)} chars — too short (missing?) or too long"
    )


def test_kanban_show_schema_mentions_targeted_re_read():
    """Tool schema is a secondary worker-facing surface; keep in sync."""
    from tools.kanban_tools import KANBAN_SHOW_SCHEMA

    desc = KANBAN_SHOW_SCHEMA["description"]
    assert "task_id" in desc or "``task_id=" in desc
    assert "created or blocked" in desc
    assert "Call once at session start" in desc


def test_read_file_schema_mentions_ranged_reads():
    from tools.file_tools import READ_FILE_SCHEMA

    desc = READ_FILE_SCHEMA["description"]
    assert "offset/limit" in desc or "offset" in desc
    assert "full-file" in desc

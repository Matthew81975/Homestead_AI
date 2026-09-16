from hcs_ai.local_codex.prompt_optimizer import PromptOptimizer


def test_optimizer_preserves_system_prompt_verbatim_and_compacts_json_user_context():
    messages = [
        {"role": "system", "content": "SYSTEM\nPROMPT\n"},
        {"role": "user", "content": '{\n  "task": "fix it",\n  "files_changed": []\n}'},
    ]

    optimized = PromptOptimizer().optimize(messages)

    assert optimized[0] == messages[0]
    assert optimized[1]["content"] == '{"task":"fix it","files_changed":[]}'


def test_optimizer_collapses_redundant_blank_lines_for_non_json_user_text():
    optimized = PromptOptimizer().optimize([
        {"role": "user", "content": "alpha\n\n\n beta  \n"}
    ])
    assert optimized == [{"role": "user", "content": "alpha\n\nbeta"}]

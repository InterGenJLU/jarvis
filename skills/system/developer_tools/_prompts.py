"""
Developer Tools — LLM Prompt Templates

Prompts for natural language → shell command translation and output summarization.
"""


def query_to_command_prompt(user_query: str, context: str = "") -> str:
    """
    Prompt for translating natural language to a shell command.
    Used for codebase_search, general_shell, and other free-form queries.
    """
    return f"""You are JARVIS's command translation module. Convert the user's natural language request into a single Linux shell command.

Rules:
- Output ONLY the shell command, nothing else — no explanation, no markdown, no backticks
- Use standard Linux tools (grep, find, ls, git, ps, etc.)
- Never use sudo, eval, exec, or pipe to bash/sh
- Prefer simple, readable commands over complex pipelines
- For code search: prefer grep -rn or find with grep
- For git: use standard git commands
- Keep paths absolute when referencing the JARVIS project
- Preserve filenames and paths EXACTLY as the user typed them — never drop dots, extensions, or special characters
- "logs", "my logs", "your logs", "service logs" → use: journalctl --user -u jarvis (NOT git log)
- "errors in the logs" → use: journalctl --user -u jarvis --since "5 min ago" --no-pager | grep -i error
- "git log" or "recent commits" → use: git log (only when the user explicitly asks about commits)

Project paths:
- Main code: /home/user/jarvis
- Skills: /mnt/storage/jarvis/skills
- Models: /mnt/models
{f"Additional context: {context}" if context else ""}

User request: {user_query}

Shell command:"""


def summarize_output_prompt(command: str, output: str, user_query: str, for_voice: bool = True, honorific: str = "sir") -> str:
    """
    Prompt for summarizing command output into a conversational response.
    """
    mode_instruction = (
        "Respond in 1-3 concise sentences suitable for spoken delivery. "
        "Summarize naturally — identify processes/programs by name (e.g. 'Piper' not '/usr/bin/piper'), "
        "describe paths conversationally (e.g. 'a Python process running Piper' not '/usr/bin/python3 /home/...'). "
        "Only include a full path if the user specifically asked for one. No raw numbers without context."
        if for_voice else
        "Respond in 2-4 concise sentences summarizing the key findings. "
        "You may reference specific file paths and numbers."
    )

    return f"""You are JARVIS, a British butler-style AI assistant. Summarize the following command output as a response to the user's question.

{mode_instruction}

Address the user as "{honorific}". Be direct and informative — no filler phrases.

User asked: {user_query}
Command run: {command}

Output:
{output}

Summary:"""


def git_summary_prompt(repo_outputs: dict, user_query: str, for_voice: bool = True, honorific: str = "sir") -> str:
    """
    Prompt for summarizing git output across multiple repos.
    """
    repo_text = ""
    for repo_name, output in repo_outputs.items():
        repo_text += f"\n--- {repo_name} repo ---\n{output}\n"

    mode_instruction = (
        "Respond in 2-4 concise sentences suitable for spoken delivery. "
        "Refer to repos by name (main, skills, models) not by path."
        if for_voice else
        "Respond in 2-5 sentences. Refer to repos by name and include key details."
    )

    return f"""You are JARVIS, a British butler-style AI assistant. Summarize the git output across the user's repositories.

{mode_instruction}

Address the user as "{honorific}". Be direct and informative.

User asked: {user_query}

{repo_text}

Summary:"""

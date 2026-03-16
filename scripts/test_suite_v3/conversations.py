"""
Conversation definitions for JARVIS Test Suite V3.

~50 conversations, ~200 turns, zero unchecked turns.
Every turn has explicit assertions plus auto-assertions (honorific, filler, non-empty).

Categories match the V3 plan's coverage matrix.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .grader import (
    contains, not_contains, any_of, min_words, max_words,
    is_empty, routes_to_skill, routes_to_layer, uses_tool, uses_any_tool,
    no_tool, tool_output_contains, routes_to_llm, has_honorific,
    no_filler_ending, no_filler_opening, has_disclaimer, no_negation,
    no_error_pattern, tool_result_min_chars,
)


# ── Data classes ─────────────────────────────────────────────────────────

@dataclass
class Turn:
    """A single user turn in a conversation."""
    user_input: str
    description: str = ""
    user_id: str = "primary_user"
    assertions: list[tuple[str, str, str]] = field(default_factory=list)
    skip_honorific: bool = False
    skip_filler: bool = False
    skip_non_empty: bool = False
    is_greeting: bool = False
    is_farewell: bool = False


@dataclass
class Conversation:
    """A multi-turn conversation test case."""
    id: str
    name: str
    category: str
    turns: list[Turn]
    cleanup: bool = True
    cleanup_for: list[str] | None = None
    tags: list[str] | None = None


# ── Shorthand builders ───────────────────────────────────────────────────

def _t(user_input: str, description: str = "", user_id: str = "primary_user",
       assertions: list | None = None, skip_honorific: bool = False,
       skip_filler: bool = False, skip_non_empty: bool = False,
       is_greeting: bool = False, is_farewell: bool = False) -> Turn:
    return Turn(
        user_input=user_input, description=description, user_id=user_id,
        assertions=assertions or [], skip_honorific=skip_honorific,
        skip_filler=skip_filler, skip_non_empty=skip_non_empty,
        is_greeting=is_greeting, is_farewell=is_farewell,
    )


def _c(id: str, name: str, category: str, turns: list[Turn],
       cleanup: bool = True, cleanup_for: list[str] | None = None,
       tags: list[str] | None = None) -> Conversation:
    return Conversation(
        id=id, name=name, category=category, turns=turns,
        cleanup=cleanup, cleanup_for=cleanup_for, tags=tags,
    )


# ── Conversation Definitions ────────────────────────────────────────────


def get_conversations() -> list[Conversation]:
    """Return all V3 test conversations."""
    return [

        # ════════════════════════════════════════════════════════════════
        # ROUTING CORE (R01-R08) — 8 conversations, ~30 turns
        # ════════════════════════════════════════════════════════════════

        _c("R01", "Rapid Topic Shift", "routing", [
            _t("what's the weather",
               assertions=[routes_to_skill("weather")]),
            _t("check git status",
               assertions=[uses_tool("developer_tools")]),
            _t("was it supposed to rain today?", "callback to weather",
               assertions=[any_of("rain", "weather", "precipitation", desc="references weather"), contains("today")]),
        ], tags=["routing:topic_shift"]),

        _c("R02", "Anaphoric Chain", "routing", [
            _t("how many files are in my documents folder",
               assertions=[uses_tool("find_files")]),
            _t("list them for me", "anaphoric reference",
               assertions=[uses_tool("find_files")]),
            _t("which ones are the biggest", "continuation",
               assertions=[no_tool("web_search"), min_words(5)]),
            _t("how old is the oldest one", "implied context",
               assertions=[min_words(5)]),
        ], tags=["routing:anaphora"]),

        _c("R03", "Mid-Conversation Correction", "routing", [
            _t("set a reminder for 3pm to call the dentist",
               assertions=[uses_tool("manage_reminders")]),
            _t("actually make it 4pm", "time correction",
               assertions=[uses_tool("manage_reminders")]),
            _t("and change dentist to doctor", "subject correction",
               assertions=[uses_tool("manage_reminders")]),
        ], tags=["routing:correction"]),

        _c("R04", "Cross-Topic Callback", "routing", [
            _t("any cybersecurity news",
               assertions=[uses_tool("get_news")]),
            _t("tell me more about the first one", "news continuation",
               assertions=[min_words(20, "elaborates on story")]),
            _t("what's the weather this weekend",
               assertions=[routes_to_skill("weather")]),
            _t("going back to that news story, search the web for more details", "callback",
               assertions=[uses_tool("web_search")]),
        ], tags=["routing:callback"]),

        _c("R05", "Short Ambiguous Follow-ups", "routing", [
            _t("how much disk space do I have",
               assertions=[uses_tool("get_system_info")]),
            _t("what about memory", "implied: system memory",
               assertions=[any_of("memory", "ram", "gb", desc="addresses system memory")]),
            _t("and CPU",
               assertions=[any_of("cpu", "processor", "core", "%", desc="addresses CPU")]),
            _t("is that normal?", "contextual follow-up",
               assertions=[min_words(8, "contextual synthesis")]),
        ], tags=["routing:ambiguous"]),

        _c("R06", "Greeting to Task to Dismiss", "routing", [
            _t("hey jarvis",
               assertions=[min_words(2, "greeting response")]),
            _t("what time is it",
               assertions=[any_of(":", "am", "pm", "o'clock", desc="provides time")]),
            _t("thanks, that's all", "dismissal detection",
               assertions=[min_words(2, "dismissal response")]),
        ], tags=["routing:greeting", "routing:dismissal"]),

        _c("R07", "Knowledge then Tool Mix", "routing", [
            _t("what's a buffer overflow",
               assertions=[routes_to_llm(), min_words(15)]),
            _t("search for recent buffer overflow CVEs",
               assertions=[uses_tool("web_search")]),
            _t("which one is the most critical", "follow-up on search",
               assertions=[min_words(10)]),
        ], tags=["routing:knowledge_tool_mix"]),

        _c("R08", "Bare Ack Filtering", "routing", [
            _t("what's the capital of France",
               assertions=[contains("paris", "correct answer")]),
            _t("yeah", "bare ack — should be filtered",
               skip_honorific=True, skip_filler=True, skip_non_empty=True,
               assertions=[is_empty("bare ack filtered")]),
            _t("what about Germany",
               assertions=[contains("berlin", "correct answer")]),
        ], tags=["routing:bare_ack"]),

        # ════════════════════════════════════════════════════════════════
        # TOOL ROUTING (T01-T06) — 6 conversations, ~24 turns
        # ════════════════════════════════════════════════════════════════

        _c("T01", "Weather Multi-Turn", "tool-routing", [
            _t("what's the weather right now",
               assertions=[routes_to_skill("weather")]),
            _t("what about this weekend",
               assertions=[routes_to_skill("weather")]),
            _t("is it supposed to rain tomorrow",
               assertions=[routes_to_skill("weather")]),
            _t("should I wash my car this week or wait",
               assertions=[min_words(10, "contextual advice")]),
        ], tags=["tool:get_weather"]),

        _c("T02", "System Info Exploration", "tool-routing", [
            _t("how much disk space do I have",
               assertions=[uses_tool("get_system_info")]),
            _t("show me the 5 most recent files in my downloads folder",
               assertions=[uses_tool("find_files")]),
            _t("what's my CPU usage right now",
               assertions=[uses_any_tool("get_system_info", "developer_tools")]),
            _t("how much RAM is being used",
               assertions=[any_of("memory", "ram", "gb", desc="provides RAM info")]),
        ], tags=["tool:get_system_info", "tool:find_files"]),

        _c("T03", "Reminder Lifecycle", "tool-routing", [
            _t("set a reminder for tomorrow at 10am to check the oil change schedule",
               assertions=[uses_tool("manage_reminders")]),
            _t("what reminders do I have set",
               assertions=[uses_tool("manage_reminders")]),
            _t("cancel the one about oil changes",
               assertions=[any_of("cancel", "removed", "deleted", desc="confirms cancellation")]),
        ], tags=["tool:manage_reminders"]),

        _c("T04", "News Deep Dive", "tool-routing", [
            _t("any tech news today",
               assertions=[uses_tool("get_news")]),
            _t("tell me more about the first one", "news elaboration",
               assertions=[min_words(20, "elaborates on story")]),
            _t("pull that article up", "news pull-up",
               assertions=[min_words(3, "acknowledges pull-up")]),
            _t("any cybersecurity news",
               assertions=[uses_tool("get_news")]),
        ], tags=["tool:get_news"]),

        _c("T05", "File System Exploration", "tool-routing", [
            _t("how many lines of code are in the jarvis project",
               assertions=[uses_tool("find_files")]),
            _t("show me the directory tree for the core folder",
               assertions=[no_tool("web_search"), min_words(5)]),
            _t("what are the biggest files in the project",
               assertions=[uses_tool("find_files"), min_words(10),
                           not_contains("no files", "returns actual results"),
                           no_error_pattern(r"no files? .*found", r"under \d+[KMG]B",
                                            desc="returns actual file list"),
                           tool_result_min_chars("find_files", 100,
                                                 desc="tool returned real results")]),
        ], tags=["tool:find_files"]),

        _c("T06", "Developer Tools", "tool-routing", [
            _t("what's my git status",
               assertions=[uses_tool("developer_tools")]),
            _t("show me the last 5 commits",
               assertions=[uses_tool("developer_tools")]),
            _t("search the codebase for domain_disclaimer",
               assertions=[uses_tool("developer_tools", "local codebase search")]),
            _t("are there any uncommitted changes",
               assertions=[uses_tool("developer_tools")]),
        ], tags=["tool:developer_tools"]),

        # ════════════════════════════════════════════════════════════════
        # HALLUCINATION DOMAINS (H01-H04) — 4 conversations, ~16 turns
        # ════════════════════════════════════════════════════════════════

        _c("H01", "Medical Symptom Research", "hallucination-domain", [
            _t("what are the common symptoms of Type 2 diabetes",
               assertions=[any_of("diabetes", "symptom", "thirst", "fatigue", desc="on-topic"),
                           has_disclaimer("medical")]),
            _t("what medications are typically prescribed for it",
               assertions=[any_of("metformin", "medication", "prescri", desc="mentions medications"),
                           has_disclaimer("medical")]),
            _t("are there any interactions between metformin and ibuprofen",
               assertions=[any_of("interact", "metformin", "ibuprofen", desc="addresses interaction")]),
            _t("what lifestyle changes help manage blood sugar",
               assertions=[min_words(15, "substantive lifestyle advice")]),
        ], tags=["domain:medical"]),

        _c("H02", "Legal Rights Question", "hallucination-domain", [
            _t("if my landlord wants to enter my apartment, how much notice do they have to give in Alabama",
               assertions=[any_of("notice", "hour", "day", "48", "24", desc="mentions notice period"),
                           has_disclaimer("legal")]),
            _t("what if they come in without notice, what are my options",
               assertions=[min_words(15, "substantive legal options"), has_disclaimer("legal")]),
            _t("can I withhold rent if they refuse to fix something",
               assertions=[min_words(15, "addresses rent withholding"), has_disclaimer("legal")]),
            _t("should I talk to a lawyer about this",
               assertions=[min_words(10, "recommends legal counsel")]),
        ], tags=["domain:legal"]),

        _c("H03", "Stock Market Research", "hallucination-domain", [
            _t("how has NVIDIA stock performed this year",
               assertions=[any_of("nvidia", "nvda", desc="on-topic"),
                           has_disclaimer("financial")]),
            _t("what's driving the price right now",
               assertions=[min_words(15, "substantive analysis")]),
            _t("is it a good time to buy",
               assertions=[min_words(15, "investment perspective")]),
            _t("what about AMD compared to NVIDIA for a long-term hold",
               assertions=[any_of("amd", desc="addresses AMD comparison")]),
        ], tags=["domain:financial"]),

        _c("H04", "Veterinary Health", "hallucination-domain", [
            _t("my dog has been limping on his front left leg for two days, what could it be",
               assertions=[min_words(15, "substantive response")]),
            _t("he's a 7 year old lab mix, about 80 pounds",
               assertions=[min_words(10, "acknowledges breed/weight")]),
            _t("is there anything I can give him for pain at home",
               assertions=[any_of("vet", "veterinar", "don't", "avoid", desc="safety-conscious")]),
            _t("when should I take him to the vet versus waiting it out",
               assertions=[any_of("vet", "veterinar", desc="recommends vet guidance")]),
        ], tags=["domain:veterinary"]),

        # ════════════════════════════════════════════════════════════════
        # KNOWLEDGE (K01-K04) — 4 conversations, ~16 turns
        # ════════════════════════════════════════════════════════════════

        _c("K01", "Cybersecurity Deep Dive", "knowledge", [
            _t("tell me about lateral movement in cybersecurity",
               assertions=[any_of("lateral", "movement", "credential", desc="on-topic"),
                           min_words(20), no_tool("web_search")]),
            _t("what tools do attackers typically use for that",
               assertions=[any_of("mimikatz", "psexec", "cobalt", "tool", desc="names tools")]),
            _t("how do you detect it in a network",
               assertions=[any_of("detect", "monitor", "log", "anomal", desc="discusses detection")]),
            _t("what about in a cloud environment",
               assertions=[any_of("cloud", "iam", "aws", "azure", desc="addresses cloud")]),
        ], tags=["knowledge:cybersecurity"]),

        _c("K02", "DNS and Networking", "knowledge", [
            _t("explain how DNS works end to end",
               assertions=[any_of("dns", "domain", "resolver", "root", desc="on-topic"),
                           min_words(20), no_tool("web_search")]),
            _t("what happens when DNS resolution fails",
               assertions=[any_of("fail", "nxdomain", "timeout", "error", desc="addresses failure")]),
            _t("what's the difference between DNS over HTTPS and standard DNS",
               assertions=[any_of("https", "doh", "encrypt", "plain", desc="compares DoH")]),
            _t("how would I set up a Pi-hole at home",
               assertions=[any_of("pi-hole", "pihole", "raspberry", "install", desc="Pi-hole setup")]),
        ], tags=["knowledge:networking"]),

        _c("K03", "TLS Deep Dive", "knowledge", [
            _t("explain how TLS handshake works",
               assertions=[any_of("tls", "handshake", "certificate", desc="on-topic"),
                           min_words(20), no_tool("web_search")]),
            _t("what changed between TLS 1.2 and 1.3",
               assertions=[any_of("1.2", "1.3", desc="references versions")]),
            _t("why was RSA key exchange removed",
               assertions=[any_of("rsa", "key exchange", "forward secrecy", desc="addresses RSA")]),
            _t("summarize everything in 3 bullet points",
               assertions=[min_words(10, "provides summary")]),
        ], tags=["knowledge:security"]),

        _c("K04", "AI and ML Breakdown", "knowledge", [
            _t("what's the difference between machine learning, deep learning, and AI",
               assertions=[any_of("machine learning", "deep learning", desc="on-topic"),
                           min_words(20), no_tool("web_search")]),
            _t("where does a large language model fit in",
               assertions=[any_of("llm", "language model", "neural", desc="places LLMs")]),
            _t("what are the biggest limitations of current LLMs",
               assertions=[any_of("hallucin", "limit", "reason", "context", desc="discusses limits")]),
            _t("what's the most promising research direction right now",
               assertions=[min_words(15, "discusses research")]),
        ], tags=["knowledge:ai"]),

        # ════════════════════════════════════════════════════════════════
        # SELF-AWARENESS (S01-S02) — 2 conversations, ~8 turns
        # ════════════════════════════════════════════════════════════════

        _c("S01", "Hardware Identity", "self-awareness", [
            _t("what CPU are you running on",
               assertions=[contains("5900", "correct CPU")]),
            _t("how much RAM do you have",
               assertions=[contains("64", "correct RAM")]),
            _t("what GPU are you using",
               assertions=[contains("7900", "correct GPU")]),
            _t("what LLM model are you running right now",
               assertions=[any_of("qwen", "3.5", desc="correct LLM model")]),
        ], tags=["self-awareness:hardware"]),

        _c("S02", "Capability Self-Knowledge", "self-awareness", [
            _t("what can you do",
               assertions=[min_words(15, "lists capabilities")]),
            _t("can you control my desktop",
               assertions=[min_words(5)]),
            _t("do you have access to my calendar",
               assertions=[any_of("not", "don't", "no ", "can't", desc="calendar unavailable")]),
            _t("what skills do you have loaded right now",
               assertions=[min_words(10, "lists skills")]),
        ], tags=["self-awareness:capabilities"]),

        # ════════════════════════════════════════════════════════════════
        # MEMORY LIFECYCLE (M01-M03) — 3 conversations, ~12 turns
        # Linked pair: M01 stores, M02 recalls+cleans, M03 transparency
        # ════════════════════════════════════════════════════════════════

        _c("M01", "Store Preference", "memory", [
            _t("remember that my favorite restaurant is Dreamland BBQ",
               assertions=[any_of("noted", "remember", "stored", "got it", "understood", "committed", desc="confirms storage")]),
            _t("and remember I usually order the ribs",
               assertions=[any_of("noted", "remember", "stored", "got it", "understood", "committed", desc="confirms storage")]),
        ], cleanup=False, tags=["memory:store"]),

        _c("M02", "Recall and Forget Preference", "memory", [
            _t("what's my favorite restaurant",
               assertions=[contains("dreamland", "recalls stored fact")]),
            _t("what do I usually get there",
               assertions=[contains("ribs", "recalls second fact")]),
            _t("forget all of that",
               assertions=[any_of("remov", "delet", "forget", "confirm", "clear", desc="forget initiated"),
                           max_words(80, "scoped to discussed facts, not entire memory")]),
            _t("yes, delete it", "confirm forget",
               assertions=[any_of("removed", "deleted", "forgotten", "done", "cleared", "confirmed", "deletion", desc="confirms deletion"),
                           not_contains("cannot", "confirms action, not refusal"),
                           no_negation("delet", "remov", desc="confirms action, not refusal")]),
        ], cleanup=True, cleanup_for=["M01"], tags=["memory:recall", "memory:forget"]),

        _c("M03", "Memory Transparency", "memory", [
            _t("what do you remember about me",
               assertions=[min_words(10, "shares stored facts")]),
            _t("how many facts do you have stored",
               assertions=[min_words(5, "provides count or context")]),
        ], tags=["memory:transparency"]),

        # ════════════════════════════════════════════════════════════════
        # TASK PLANNER (P01-P02) — 2 conversations, ~6 turns
        # ════════════════════════════════════════════════════════════════

        _c("P01", "Compound Execution", "task-planner", [
            _t("search for the weather this weekend and then create a packing list document",
               "compound detection",
               assertions=[min_words(5, "plan announced or executed")]),
            _t("how many steps is that",
               assertions=[min_words(3, "addresses step count")]),
            _t("go ahead",
               assertions=[min_words(3, "executes or continues")]),
        ], tags=["planner:compound"]),

        _c("P02", "Compound with Cancel", "task-planner", [
            _t("check my git status and then search for the latest Python security patches and create a summary",
               "3-step compound",
               assertions=[min_words(5, "plan announced or executed")]),
            _t("cancel", "task planner interrupt",
               assertions=[any_of("cancel", "stopped", "understood", "halted", desc="acknowledges cancel")]),
        ], tags=["planner:cancel"]),

        # ════════════════════════════════════════════════════════════════
        # DOCUMENT GENERATION (D01-D02) — 2 conversations, ~6 turns
        # ════════════════════════════════════════════════════════════════

        _c("D01", "Presentation Creation", "document-gen", [
            _t("create a presentation about cybersecurity best practices for small businesses",
               assertions=[min_words(5, "acknowledges creation")]),
            _t("add a slide about password management",
               assertions=[min_words(5, "acknowledges addition")]),
            _t("open it",
               assertions=[min_words(2, "acknowledges open")]),
        ], tags=["doc:presentation"]),

        _c("D02", "Document with Readback Trigger", "document-gen", [
            _t("create a document comparing the top 3 BBQ rub recipes",
               assertions=[min_words(5, "acknowledges creation")]),
            _t("read it to me", "triggers readback",
               assertions=[min_words(5, "begins readback")]),
            _t("next", "readback navigation",
               assertions=[min_words(3, "continues readback"),
                           no_tool("web_search", "navigates existing content, not new search")]),
        ], tags=["doc:document", "readback"]),

        # ════════════════════════════════════════════════════════════════
        # DESKTOP & APPS (A01-A02) — 2 conversations, ~8 turns
        # ════════════════════════════════════════════════════════════════

        _c("A01", "App Launch and Control", "desktop-apps", [
            _t("open Chrome",
               assertions=[min_words(2, "acknowledges launch")]),
            _t("make it fullscreen",
               assertions=[min_words(2, "acknowledges fullscreen")]),
            _t("now open the calculator",
               assertions=[min_words(2, "acknowledges calculator")]),
            _t("close Chrome",
               assertions=[min_words(2, "acknowledges close")]),
        ], tags=["desktop:app_launcher"]),

        _c("A02", "Volume and Screenshot", "desktop-apps", [
            _t("turn the volume up",
               assertions=[min_words(2, "acknowledges volume up")]),
            _t("set it to 50 percent",
               assertions=[min_words(2, "acknowledges volume set")]),
            _t("mute",
               assertions=[min_words(2, "acknowledges mute")]),
            _t("take a screenshot",
               assertions=[min_words(2, "acknowledges screenshot")]),
        ], tags=["desktop:volume", "tool:take_screenshot"]),

        # ════════════════════════════════════════════════════════════════
        # WEB NAVIGATION (W01-W02) — 2 conversations, ~8 turns
        # ════════════════════════════════════════════════════════════════

        _c("W01", "Site-Specific Searches", "web-navigation", [
            _t("search YouTube for how to smoke a brisket",
               assertions=[routes_to_skill("web_navigation")]),
            _t("now search Amazon for a Thermapen thermometer",
               assertions=[routes_to_skill("web_navigation")]),
            _t("look up brisket on Wikipedia",
               assertions=[routes_to_skill("web_navigation")]),
        ], tags=["skill:web_navigation"]),

        _c("W02", "Browser Control", "web-navigation", [
            _t("open google.com",
               assertions=[min_words(2, "acknowledges open")]),
            _t("make the browser half screen",
               assertions=[min_words(2, "acknowledges resize")]),
            _t("open a new tab to reddit",
               assertions=[min_words(2, "acknowledges new tab")]),
            _t("minimize the browser",
               assertions=[min_words(2, "acknowledges minimize")]),
        ], tags=["skill:web_navigation"]),

        # ════════════════════════════════════════════════════════════════
        # FILE OPERATIONS (F01-F02) — 2 conversations, ~8 turns
        # ════════════════════════════════════════════════════════════════

        _c("F01", "File Search and Count", "file-ops", [
            _t("how many files are in my downloads folder",
               assertions=[uses_tool("find_files"), min_words(3)]),
            _t("show me the 5 most recent ones",
               assertions=[min_words(5)]),
            _t("how much disk space am I using",
               assertions=[any_of("gb", "tb", "disk", "space", "gigabyte", "terabyte", "drive", "storage", desc="provides disk info")]),
            _t("what are the biggest files on my system",
               assertions=[uses_tool("find_files"), min_words(10),
                           not_contains("no files", "returns actual results"),
                           no_error_pattern(r"no files? .*found", r"under \d+[KMG]B",
                                            desc="returns actual file list"),
                           tool_result_min_chars("find_files", 100,
                                                 desc="tool returned real results")]),
        ], tags=["tool:find_files"]),

        _c("F02", "Code Directory Analysis", "file-ops", [
            _t("how many lines of code are in the jarvis project",
               assertions=[uses_tool("find_files")]),
            _t("show me the directory tree for the core folder",
               assertions=[no_tool("web_search"), min_words(5)]),
            _t("how big is the models directory",
               assertions=[min_words(5)]),
            _t("which Python file has the most lines",
               assertions=[min_words(5)]),
        ], tags=["tool:find_files"]),

        # ════════════════════════════════════════════════════════════════
        # SYSTEM ADMIN (G01-G02) — 2 conversations, ~8 turns
        # ════════════════════════════════════════════════════════════════

        _c("G01", "Git and Dev Tools", "system-admin", [
            _t("what's my git status",
               assertions=[uses_tool("developer_tools")]),
            _t("show me the last 5 commits",
               assertions=[uses_tool("developer_tools")]),
            _t("are there any uncommitted changes",
               assertions=[uses_tool("developer_tools")]),
            _t("search the codebase for FILLER_ENDINGS",
               assertions=[uses_tool("developer_tools", "local codebase search")]),
        ], tags=["tool:developer_tools"]),

        _c("G02", "System Health", "system-admin", [
            _t("is the jarvis web service running",
               assertions=[min_words(5)]),
            _t("what processes are using the most CPU right now",
               assertions=[min_words(5)]),
            _t("run a full system health check",
               assertions=[min_words(5)]),
            _t("show me the last 10 minutes of jarvis logs",
               assertions=[min_words(5)]),
        ], tags=["tool:developer_tools"]),

        # ════════════════════════════════════════════════════════════════
        # MULTI-USER / ERICA (E01-E03) — 3 conversations, ~12 turns
        # ════════════════════════════════════════════════════════════════

        _c("E01", "Secondary User Basic Interaction", "multi-user", [
            _t("good morning", "formal greeting", user_id="secondary_user",
               is_greeting=True, skip_honorific=True,
               assertions=[has_honorific("ms. guest", "'Ms. Guest' greeting")]),
            _t("what's the weather today", "mid-convo mum", user_id="secondary_user",
               skip_honorific=True,
               assertions=[has_honorific("ma'am", "'mum' honorific")]),
            _t("do I have any reminders", user_id="secondary_user",
               skip_honorific=True,
               assertions=[has_honorific("ma'am", "'mum' honorific")]),
            _t("thank you, that's all", "formal farewell", user_id="secondary_user",
               is_farewell=True, skip_honorific=True,
               assertions=[has_honorific("ms. guest", "'Ms. Guest' farewell")]),
        ], tags=["multi-user:erica", "honorific:ms_erica"]),

        _c("E02", "Secondary User Task Request", "multi-user", [
            _t("set a reminder for tomorrow at 9am to call the pharmacy", user_id="secondary_user",
               skip_honorific=True,
               assertions=[has_honorific("ma'am", "'mum' honorific"),
                           uses_tool("manage_reminders")]),
            _t("what's for dinner tonight", user_id="secondary_user",
               skip_honorific=True,
               assertions=[has_honorific("ma'am", "'mum' honorific"), min_words(5)]),
            _t("how do I send a picture on my iPhone", user_id="secondary_user",
               skip_honorific=True,
               assertions=[has_honorific("ma'am", "'mum' honorific"), min_words(10)]),
        ], tags=["multi-user:erica", "honorific:mum"]),

        _c("E03", "User Switch Mid-Conversation", "multi-user", [
            _t("what's the weather this weekend", "the user turn",
               assertions=[routes_to_skill("weather")]),
            _t("what's a good recipe for chicken soup", "switch to secondary user", user_id="secondary_user",
               skip_honorific=True,
               assertions=[has_honorific("ma'am", "'mum' honorific"), min_words(10)]),
            _t("can you search for that on YouTube", "still secondary user", user_id="secondary_user",
               skip_honorific=True,
               assertions=[has_honorific("ma'am", "'mum' honorific")]),
            _t("check my git status", "back to the user",
               assertions=[uses_tool("developer_tools")]),
        ], tags=["multi-user:switch"]),

        # ════════════════════════════════════════════════════════════════
        # REMINDERS (N01-N02) — 2 conversations, ~8 turns
        # ════════════════════════════════════════════════════════════════

        _c("N01", "Reminder Correction Chain", "reminders", [
            _t("remind me at 2pm to call mom",
               assertions=[uses_tool("manage_reminders")]),
            _t("actually make that 3pm",
               assertions=[uses_tool("manage_reminders")]),
            _t("and change call to text",
               assertions=[uses_tool("manage_reminders")]),
            _t("cancel all my reminders",
               assertions=[uses_tool("manage_reminders")]),
        ], tags=["tool:manage_reminders"]),

        _c("N02", "Reminder Set and List", "reminders", [
            _t("set a reminder for Friday at 3pm to leave early for the weekend",
               assertions=[uses_tool("manage_reminders")]),
            _t("what reminders do I have set",
               assertions=[uses_tool("manage_reminders"),
                           tool_output_contains("manage_reminders", "leave early",
                                                desc="tool output has the test reminder")]),
            _t("cancel the one about leaving early",
               assertions=[any_of("cancel", "removed", "deleted", desc="confirms cancellation")]),
            _t("do I have any reminders left",
               assertions=[min_words(3)]),
        ], tags=["tool:manage_reminders"]),

        # ════════════════════════════════════════════════════════════════
        # READBACK (B01) — 1 conversation, 4 turns
        # ════════════════════════════════════════════════════════════════

        _c("B01", "Readback Navigation", "readback", [
            _t("search for a pulled pork recipe",
               skip_filler=True,
               assertions=[uses_tool("web_search"), not_contains("step 1", "no auto-readback")]),
            _t("read that to me", "delivery mode triggers readback",
               skip_filler=True,
               assertions=[min_words(5, "begins readback")]),
            _t("next", "readback navigation",
               assertions=[min_words(3, "continues readback")]),
            _t("go back to the ingredients", "section navigation",
               assertions=[min_words(3, "navigates to section")]),
        ], tags=["readback"]),

        # ════════════════════════════════════════════════════════════════
        # MIXED CAPABILITY (X01-X03) — 3 conversations, ~14 turns
        # ════════════════════════════════════════════════════════════════

        _c("X01", "Morning Routine", "mixed-capability", [
            _t("good morning",
               assertions=[min_words(2, "greeting response")]),
            _t("what's the weather today",
               assertions=[routes_to_skill("weather")]),
            _t("any important news",
               assertions=[uses_tool("get_news")]),
            _t("what reminders do I have set",
               assertions=[uses_tool("manage_reminders")]),
            _t("check my git status",
               assertions=[uses_tool("developer_tools")]),
            _t("how's the system health",
               assertions=[min_words(5, "provides health info")]),
        ], tags=["mixed:morning_routine"]),

        _c("X02", "Research to Document", "mixed-capability", [
            _t("search for the top 5 budget smokers for brisket",
               assertions=[uses_tool("web_search")]),
            _t("compare them in a document",
               assertions=[min_words(5, "begins doc creation")]),
            _t("add price data to each entry",
               assertions=[min_words(5, "acknowledges addition")]),
            _t("email that to me", "should explain email not available",
               assertions=[any_of("not", "can't", "don't", "unable", "unavailable", "isn't",
                                  desc="email not available")]),
        ], tags=["mixed:research_to_doc"]),

        _c("X03", "Cross-Domain Rapid Fire", "mixed-capability", [
            _t("what's 15 percent of $87.50",
               assertions=[contains("13", "correct calculation")]),
            _t("who won the Super Bowl last year",
               assertions=[min_words(5)]),
            _t("open YouTube",
               assertions=[routes_to_skill("web_navigation")]),
            _t("what's the weather tomorrow",
               assertions=[routes_to_skill("weather")]),
        ], tags=["mixed:rapid_fire"]),

        # ════════════════════════════════════════════════════════════════
        # COLLOQUIAL / SLOPPY INPUT (C01-C02) — 2 conversations, ~8 turns
        # New for V3: test informal speech patterns
        # ════════════════════════════════════════════════════════════════

        _c("C01", "Casual the user", "colloquial", [
            _t("yo jarvis whats the weather gonna be like tmrw",
               assertions=[routes_to_skill("weather")]),
            _t("any news worth caring about",
               assertions=[uses_tool("get_news")]),
            _t("how much space i got left on the drives",
               assertions=[uses_tool("get_system_info")]),
            _t("nah thats all thanks", "casual dismissal",
               assertions=[min_words(2, "dismissal response")]),
        ], tags=["colloquial:christopher"]),

        _c("C02", "Casual Secondary User", "colloquial", [
            _t("hey sweetie whats the temperature outside", user_id="secondary_user",
               skip_honorific=True,
               assertions=[has_honorific("ma'am", "'mum' honorific"),
                           routes_to_skill("weather")]),
            _t("do i have anything set for today", user_id="secondary_user",
               skip_honorific=True,
               assertions=[has_honorific("ma'am", "'mum' honorific")]),
            _t("oh can you look up how to make cornbread", user_id="secondary_user",
               skip_honorific=True,
               assertions=[has_honorific("ma'am", "'mum' honorific"), min_words(10)]),
            _t("thank you honey thats all i needed", user_id="secondary_user",
               is_farewell=True, skip_honorific=True,
               assertions=[has_honorific("ms. guest", "'Ms. Guest' farewell")]),
        ], tags=["colloquial:erica"]),

        # ════════════════════════════════════════════════════════════════
        # EDGE CASES (Z01-Z02) — 2 conversations, ~6 turns
        # ════════════════════════════════════════════════════════════════

        _c("Z01", "Unavailable Capability", "edge-cases", [
            _t("email this document to my work address",
               assertions=[any_of("not", "can't", "don't", "unable", "unavailable",
                                  desc="email not available")]),
            _t("add this to my calendar",
               assertions=[any_of("not", "don't", "can't", "calendar", desc="calendar unavailable")]),
            _t("play some music",
               assertions=[min_words(3, "addresses request")]),
        ], tags=["edge:unavailable"]),

        _c("Z02", "Compound Edge and Long Input", "edge-cases", [
            _t("what's the weather and whether I should bring a jacket",
               "NOT compound — single intent with homophone",
               assertions=[uses_tool("get_weather"),
                           no_tool("web_search", "single intent, not compound")]),
            _t("I need you to look at this really long and complicated problem where I have multiple nested data structures in Python including dictionaries inside lists inside dictionaries and I need to flatten them all out into a single flat dictionary with dot-notation keys for all the nested paths",
               "very long input — should handle gracefully",
               assertions=[min_words(15, "substantive response to long input")]),
            _t("thanks",
               assertions=[min_words(2, "acknowledges thanks")]),
        ], tags=["edge:compound", "edge:long_input"]),
    ]

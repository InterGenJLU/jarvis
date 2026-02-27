#!/usr/bin/env python3
"""Semantic Intent Overlap Diagnostic

Scores collision candidate phrases against ALL skill intents to reveal
where semantic boundaries are fuzzy. Sweeps multiple pruner thresholds
to show exactly how each value affects tool breadth.

Usage:
    python3 scripts/test_intent_overlap.py                  # Full sweep
    python3 scripts/test_intent_overlap.py --threshold 0.45 # Single threshold
"""

import sys
import os
import argparse

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

import warnings
warnings.filterwarnings("ignore")


# All semantic intents across every skill, extracted from skill.py files.
# Format: {skill_name: {intent_id: [example_phrases]}}
ALL_INTENTS = {
    # --- Phase 1 (migrated) ---
    "time_info": {
        "time_query": ["what time is it", "what's the time", "tell me the time", "current time"],
        "date_query": ["what's the date", "what is the date", "what day is it", "today's date"],
    },
    "system_info": {
        "disk_space": ["what's my disk space", "how much disk space do i have left", "check disk space", "disk usage", "how full is my hard drive"],
        "cpu_info": ["what cpu do i have", "show me my processor", "what type of cpu is installed", "tell me about this computer's cpu", "which processor am i running"],
        "memory_info": ["how much ram do i have", "how much memory is in this machine", "what's my ram", "memory info", "show me memory usage", "how much memory do i have"],
        "all_drives": ["list my hard drives", "what hard drives do i have", "show me my drives", "what's my current hard drive usage", "how much disk space do i have", "what's my storage usage"],
        "gpu_info": ["what's my gpu", "what graphics card do i have", "show me my graphics card", "what gpu is installed in this computer", "tell me about my graphics hardware"],
    },
    "filesystem": {
        "find_file": ["where is expenses.xlsx", "find the file named report.pdf", "locate my presentation", "where did I save that spreadsheet", "find my config file"],
        "count_code_lines": ["how many lines of code in your codebase", "count lines in jarvis", "how big is the project", "show me code statistics"],
        "count_files_in_directory": ["how many files are in my documents folder", "count files in downloads", "how many files in the home directory", "how many items in my desktop folder", "count the files in scripts"],
        "analyze_script": ["what does my backup script do", "analyze the install.sh file", "explain my deploy script", "what's in the cleanup.py file", "tell me about the backup.sh script", "please analyze test_backup.sh", "analyze test_backup", "what does test_backup.sh do", "please explain the backup script"],
    },

    # --- Phase 2 (to be migrated) ---
    "weather": {
        "current_weather": ["what's the weather like today", "what's the weather today", "how's the weather today", "weather right now", "current weather", "what are the current meteorological conditions", "how are the weather conditions today", "what's the weather in the news", "look into the current meteorological conditions", "what's the weather in paris", "how's the weather like in london", "weather for new york", "temperature in chicago", "tell me the weather in tokyo", "what's the temperature", "how hot is it", "how cold is it", "what's the temp"],
        "forecast": ["what's the forecast", "weather forecast", "forecast for this week", "what is the forecast"],
        "rain_check": ["will it rain", "is it raining", "will it rain tomorrow", "is it going to rain"],
        "tomorrow_weather": ["weather tomorrow", "tomorrow's forecast", "what will the weather be tomorrow", "how's it going to be tomorrow"],
    },
    "reminders": {
        "set_reminder": ["remind me to take out the trash tomorrow at 6", "set a reminder for my meeting at 3 PM", "remind me about the dentist appointment next Tuesday", "create a reminder to call mom at 5", "remind me in 30 minutes to check the oven", "set a reminder to check on something in one minute", "remind me to pick up groceries at 5 PM", "remind me to water the plants in 2 hours", "urgent reminder to call the doctor tomorrow at 8 AM", "reminder to call someone tomorrow morning"],
        "set_recurring": ["remind me every Tuesday to water the plants", "set a weekly reminder for laundry on Sundays", "remind me every day at 8 AM to take medication", "set a daily reminder at noon to stretch"],
        "list_reminders": ["what reminders do I have", "show my reminders", "list my reminders", "any upcoming reminders"],
        "cancel_reminder": ["cancel the reminder about the dentist", "delete my trash reminder", "delete my dentist reminder", "remove the meeting reminder", "remove the appointment reminder", "get rid of the reminder", "cancel reminder"],
        "acknowledge_current": ["yes I did it", "done", "already did that", "taken care of", "I already handled it", "yes I remembered", "understood", "got it", "okay", "acknowledged", "noted"],
        "snooze": ["snooze that", "remind me again in 10 minutes", "snooze the reminder", "tell me again later"],
        "daily_rundown": ["what do I have today", "daily rundown", "what's on for today", "morning briefing", "my schedule for today"],
    },
    "developer_tools": {
        "codebase_search": ["search codebase for system prompts", "grep for error handling", "find files containing database", "search the code for wake word", "look through the code for API keys"],
        "git_status": ["git status", "uncommitted changes", "what files have changed", "any unsaved changes", "are there any modified files", "check the repo status", "check the skills repo status", "status of the repos"],
        "git_log": ["recent commits", "git history", "last commit", "show me the commit log", "what was the last change", "check the recent commits", "what's been committed lately"],
        "git_diff": ["show the diff", "what changed since last commit", "git diff", "show me what's been modified"],
        "git_branch": ["what branch am I on", "list branches", "current branch", "show git branches"],
        "process_info": ["top processes", "what's eating CPU", "memory hogs", "what processes are running", "what's using the most resources"],
        "service_status": ["is jarvis running", "docker status", "check the jarvis service", "is the service active", "what services are running"],
        "network_info": ["my IP address", "what's my IP", "ping google", "open ports", "network interfaces"],
        "file_operations": ["backup config.yaml", "rename this file", "copy the config file", "create a backup of the config"],
        "file_delete": ["delete the temp file", "remove old logs", "clean up temporary files", "delete that backup"],
        "package_info": ["python version", "pip packages", "is ffmpeg installed", "what version of node", "check installed packages"],
        "general_shell": ["run df -h", "check system logs", "show dmesg", "run a command for me", "execute this shell command", "are there any errors in the logs", "check the last few minutes of logs", "any warnings in the service logs"],
        "show_output": ["show me the git diff", "let me see the logs", "pull up the process list", "display the search results", "show me the running processes"],
        "system_health": ["run a full system health check", "how is everything running", "system diagnostic", "give me a status report", "how are your systems"],
        "confirm_action": ["yes", "yes go ahead", "yeah do it", "proceed", "confirmed", "do it", "no", "no cancel", "never mind", "abort", "nah stop"],
    },

    # --- Not migrated (remain in legacy routing) ---
    "conversation": {
        "greeting": ["hello", "good morning", "good evening", "hi there", "hey"],
        "how_are_you": ["how are you", "how are you doing", "how are you feeling", "how are you feeling today", "how's it going", "how are things", "what's up"],
        "thank_you": ["thank you", "thanks a lot", "appreciate it", "excellent thank you", "perfect thanks"],
        "acknowledgment": ["ok", "sounds good", "alright", "excellent", "perfect"],
        "youre_welcome": ["you're welcome", "no problem", "anytime"],
        "goodbye": ["goodbye", "see you later", "talk to you later", "bye", "good night"],
        "user_is_good": ["i'm good", "doing well", "not bad", "i'm fine", "can't complain"],
        "user_asks_how_jarvis_is": ["how about you", "and yourself", "what about you", "and you"],
        "no_help_needed": ["no thanks", "i don't need anything", "not right now", "nothing at the moment", "i'm all set"],
        "whats_up": ["what's up", "what's new", "what's going on"],
    },
    "news": {
        "read_news": ["what's the news", "any headlines", "read me the news", "what's happening in the world", "give me the headlines", "what's going on today", "news update", "read the news", "any breaking news", "catch me up on the news", "read critical headlines", "any urgent news", "are there any important headlines"],
        "category_news": ["any tech news today", "read me the technology headlines", "what's happening in tech news", "cybersecurity news headlines", "any cyber security headlines", "read security news", "any political news today", "read the politics headlines", "local news headlines", "local news today", "any general news headlines", "world news update", "critical cybersecurity headlines", "any urgent tech news"],
        "continue_reading": ["continue", "keep going", "read more", "more headlines", "yes please continue", "next", "go on", "what else"],
        "news_count": ["how many headlines do I have", "any new articles", "how many news stories", "do I have any news", "any new headlines"],
    },
    "app_launcher": {
        "launch_app": ["open chrome", "launch brave", "start firefox", "run vs code", "launch the terminal", "open the calculator", "pull up the file manager", "start nautilus", "open settings"],
        "close_app": ["close chrome", "close the browser", "close the terminal", "close the calculator", "close settings", "shut down firefox", "exit vs code", "quit the program", "kill the app"],
        "fullscreen": ["fullscreen", "make it fullscreen", "go fullscreen", "fullscreen please", "fullscreen chrome", "make the window fullscreen"],
        "minimize": ["minimize that", "minimize the window", "minimize chrome", "hide the browser", "minimize it"],
        "maximize": ["maximize that", "maximize the window", "maximize chrome", "make it bigger", "maximize it"],
        "list_apps": ["what apps can you launch", "show me available apps", "what programs do you know", "list your applications", "what can you open"],
        "volume_up": ["turn the volume up", "louder", "increase the volume", "raise the volume", "volume up", "turn it up"],
        "volume_down": ["turn the volume down", "quieter", "decrease the volume", "lower the volume", "volume down", "turn it down"],
        "toggle_mute": ["mute", "unmute", "toggle mute", "mute the sound", "silence the audio"],
    },
    "social_introductions": {
        "introduce_person": ["meet my niece Arya", "this is my brother Jake", "I'd like you to meet my friend Sarah", "my coworker's name is Dave", "let me introduce my wife Lisa", "I want to introduce you to my cousin Marcus", "meet my neighbor Tom", "my son's name is Ethan"],
        "who_is": ["who is Arya", "what do you know about Jake", "tell me about Sarah", "do you know who Dave is", "what do you remember about my niece"],
        "fix_pronunciation": ["that's not how you say Arya", "you're mispronouncing her name", "say her name differently", "pronounce Arya like Areea", "you're saying the name wrong"],
        "list_people": ["who do you know", "list the people you know", "who have I introduced you to", "show me your contacts"],
    },
}

# Phase 2 migrated skills (will become tool-enabled)
PHASE2_MIGRATED = {"time_info", "system_info", "filesystem", "weather", "reminders", "developer_tools"}

# Collision candidates — ambiguous queries that might match multiple skills
COLLISION_CANDIDATES = [
    # Status/health (conversation vs developer_tools vs system_info)
    "how are your systems",
    "how are you doing",
    "how is everything running",
    "give me a status report",
    "how are things",

    # "show me" (developer_tools vs system_info vs app_launcher)
    "show me the logs",
    "show me my drives",
    "show me available apps",
    "show me recent commits",
    "show me what's running",

    # Bare yes/no (reminders vs developer_tools vs conversation)
    "yes",
    "yes go ahead",
    "done",
    "ok",
    "got it",
    "no",
    "never mind",

    # "what's going on" (conversation vs news vs weather)
    "what's going on",
    "what's happening",
    "what's new",
    "what's up",

    # "check" (system_info vs weather vs developer_tools)
    "check the weather",
    "check disk space",
    "check the service",
    "check the repo",

    # Time-adjacent (time_info vs reminders vs conversation)
    "what do I have today",
    "what's on for today",
    "what time is it",

    # Search overlap (filesystem vs developer_tools)
    "search for config files",
    "find the database file",
    "search the code for errors",
    "look for API keys",

    # General ambiguity
    "tell me about the system",
    "what can you tell me",
    "help me with something",
    "run a command",

    # --- Realistic voice commands (normal usage) ---
    "what's the weather",
    "remind me to call mom at 5",
    "what cpu do I have",
    "find my config file",
    "what's the forecast",
    "any reminders",
    "git status",
    "what's my IP address",
    "how much ram do I have",
    "open chrome",
    "what's the news",
    "who is Arya",
    "good morning",
    "thank you",
    "close the browser",
    "will it rain tomorrow",
    "recent commits",
    "top processes",
    "how much disk space do I have",
    "any headlines",
]


def score_all(query, model, intent_embeddings, st_util):
    """Score a query against all intents, return sorted list."""
    query_emb = model.encode(query, convert_to_tensor=True, show_progress_bar=False)
    scores = []
    for (skill_name, intent_id), emb in intent_embeddings.items():
        sims = st_util.cos_sim(query_emb, emb)
        max_sim = float(sims.max())
        scores.append((skill_name, intent_id, max_sim))
    scores.sort(key=lambda x: x[2], reverse=True)
    return scores


def get_skill_best(scores):
    """From sorted score list, get best score per skill."""
    skill_best = {}
    for skill, intent, score in scores:
        if skill not in skill_best:
            skill_best[skill] = (intent, score)
    return skill_best


def run_detail(model, intent_embeddings, st_util, threshold):
    """Run detailed per-query analysis at a given threshold."""
    SHOW_TOP_N = 5

    print(f"\n{'=' * 90}")
    print(f"DETAILED RESULTS — threshold={threshold:.2f}")
    print(f"{'=' * 90}\n")

    overlaps = []

    for query in COLLISION_CANDIDATES:
        scores = score_all(query, model, intent_embeddings, st_util)
        top = scores[:SHOW_TOP_N]

        # Find gap between top 2 DIFFERENT skills
        s1_skill = scores[0][0]
        for s in scores[1:]:
            if s[0] != s1_skill:
                gap = scores[0][2] - s[2]
                if gap < 0.10:
                    overlaps.append((query, scores[0][0], scores[0][1],
                                     scores[0][2], s[0], s[1], s[2], gap))
                break

        # How many MIGRATED skills pass the threshold?
        migrated_above = set()
        all_above = set()
        for skill, intent, score in scores:
            if score >= threshold:
                all_above.add(skill)
                if skill in PHASE2_MIGRATED:
                    migrated_above.add(skill)

        marker = "!! " if any(o[0] == query for o in overlaps) else "   "
        tool_count = len(migrated_above) + 1  # +1 for web_search
        warn = ""
        if tool_count > 5:
            warn = f" ** {tool_count} TOOLS (OVER CLIFF) **"
        elif tool_count > 4:
            warn = f" * {tool_count} tools (at cliff edge) *"

        print(f'{marker}"{query}"{warn}')
        for skill, intent, score in top:
            above = "#" if score >= threshold else "."
            migrated = "M" if skill in PHASE2_MIGRATED else " "
            print(f"      {score:.3f} {above}{migrated} {skill}:{intent}")
        if migrated_above:
            print(f"      -> tools: web_search + {', '.join(sorted(migrated_above))} = {tool_count}")
        print()

    # Overlap summary
    if overlaps:
        print(f"\n{'=' * 90}")
        print(f"DANGEROUS OVERLAPS at threshold={threshold:.2f}")
        print(f"{'=' * 90}")
        for query, s1, i1, sc1, s2, i2, sc2, gap in sorted(overlaps, key=lambda x: x[7]):
            print(f'  gap={gap:.3f}  "{query}"')
            print(f"    1st: {s1}:{i1} ({sc1:.3f})")
            print(f"    2nd: {s2}:{i2} ({sc2:.3f})")

    return overlaps


def run_sweep(model, intent_embeddings, st_util, thresholds):
    """Sweep multiple thresholds and show summary table."""

    print(f"\n{'=' * 90}")
    print("THRESHOLD SWEEP — Phase 2 tool count per query")
    print(f"{'=' * 90}")
    print(f"\nAssuming Phase 2 migrated skills: {', '.join(sorted(PHASE2_MIGRATED))}")
    print(f"Tool count = migrated skills above threshold + web_search (always included)\n")

    # Header
    thresh_strs = [f"  {t:.2f}" for t in thresholds]
    print(f"{'Query':<45} {'|'.join(thresh_strs)}  | correct_skill")
    print("-" * (46 + len(thresholds) * 7 + 20))

    # Stats per threshold
    stats = {t: {"max_tools": 0, "over_cliff": 0, "at_cliff": 0,
                 "total_tools": 0, "missed_correct": 0} for t in thresholds}

    for query in COLLISION_CANDIDATES:
        scores = score_all(query, model, intent_embeddings, st_util)
        skill_best = get_skill_best(scores)

        # Determine "correct" skill (highest scoring overall)
        correct_skill = scores[0][0]

        cols = []
        for t in thresholds:
            migrated_above = set()
            for skill, (intent, score) in skill_best.items():
                if score >= t and skill in PHASE2_MIGRATED:
                    migrated_above.add(skill)
            tool_count = len(migrated_above) + 1  # +1 for web_search

            stats[t]["total_tools"] += tool_count
            if tool_count > stats[t]["max_tools"]:
                stats[t]["max_tools"] = tool_count
            if tool_count > 5:
                stats[t]["over_cliff"] += 1
            elif tool_count == 5:
                stats[t]["at_cliff"] += 1

            # Did correct skill get included (if migrated)?
            if correct_skill in PHASE2_MIGRATED and correct_skill not in migrated_above:
                stats[t]["missed_correct"] += 1

            if tool_count > 5:
                cols.append(f"  {tool_count}!! ")
            elif tool_count >= 4:
                cols.append(f"  {tool_count}*  ")
            else:
                cols.append(f"  {tool_count}   ")

        trunc_query = query[:44]
        correct_display = correct_skill[:18]
        print(f"{trunc_query:<45}{'|'.join(cols)}  | {correct_display}")

    # Summary table
    print(f"\n{'=' * 90}")
    print("SWEEP SUMMARY")
    print(f"{'=' * 90}\n")
    print(f"{'Metric':<35}", end="")
    for t in thresholds:
        print(f"  {t:.2f}  ", end="")
    print()
    print("-" * (36 + len(thresholds) * 8))

    for label, key in [
        ("Max tools in any request", "max_tools"),
        ("Queries over cliff (>5 tools)", "over_cliff"),
        ("Queries at cliff edge (5 tools)", "at_cliff"),
        ("Avg tools per request", "total_tools"),
        ("Missed correct skill (false neg)", "missed_correct"),
    ]:
        print(f"{label:<35}", end="")
        for t in thresholds:
            val = stats[t][key]
            if key == "total_tools":
                avg = val / len(COLLISION_CANDIDATES)
                print(f"  {avg:.1f}  ", end="")
            else:
                marker = " !!" if (key in ("over_cliff", "missed_correct") and val > 0) else "   "
                print(f"  {val}{marker} ", end="")
        print()

    # Hard cap analysis
    print(f"\n{'=' * 90}")
    print("HARD CAP ANALYSIS — effect of cap=5 tools (4 domain + web_search)")
    print(f"{'=' * 90}\n")
    print(f"{'Threshold':<12} {'Queries needing cap':<25} {'Lost skills from cap'}")
    print("-" * 65)

    for t in thresholds:
        capped = 0
        lost_skills = []
        for query in COLLISION_CANDIDATES:
            scores = score_all(query, model, intent_embeddings, st_util)
            skill_best = get_skill_best(scores)
            migrated = [(skill, score) for skill, (intent, score)
                        in skill_best.items()
                        if score >= t and skill in PHASE2_MIGRATED]
            migrated.sort(key=lambda x: x[1], reverse=True)

            if len(migrated) + 1 > 5:  # +1 for web_search
                capped += 1
                dropped = migrated[4:]  # keep top 4, drop rest
                for skill, score in dropped:
                    lost_skills.append(f"{skill}({score:.2f})")

        lost_summary = ", ".join(lost_skills[:8]) if lost_skills else "none"
        if len(lost_skills) > 8:
            lost_summary += f" +{len(lost_skills)-8} more"
        print(f"  {t:.2f}       {capped:<25} {lost_summary}")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Semantic intent overlap diagnostic")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Single threshold for detailed output")
    args = parser.parse_args()

    from sentence_transformers import SentenceTransformer, util as st_util

    print("Loading embedding model...")
    model = SentenceTransformer("all-MiniLM-L6-v2")
    print("OK\n")

    # Pre-compute all intent embeddings
    intent_embeddings = {}
    total_intents = 0
    for skill_name, intents in ALL_INTENTS.items():
        for intent_id, examples in intents.items():
            emb = model.encode(examples, convert_to_tensor=True,
                               show_progress_bar=False)
            intent_embeddings[(skill_name, intent_id)] = emb
            total_intents += 1

    print(f"Loaded {total_intents} intents across {len(ALL_INTENTS)} skills")
    print(f"Testing {len(COLLISION_CANDIDATES)} queries")
    print(f"Phase 2 migrated skills: {', '.join(sorted(PHASE2_MIGRATED))}")

    if args.threshold is not None:
        # Single threshold detailed run
        run_detail(model, intent_embeddings, st_util, args.threshold)
    else:
        # Full sweep
        thresholds = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]
        run_sweep(model, intent_embeddings, st_util, thresholds)

        # Also run detailed at current (0.35) and proposed (0.45)
        run_detail(model, intent_embeddings, st_util, 0.35)
        run_detail(model, intent_embeddings, st_util, 0.45)

    print(f"\n{'=' * 90}")
    print("LEGEND")
    print(f"{'=' * 90}")
    print("  # = above threshold    . = below threshold")
    print("  M = migrated skill     (space) = non-migrated")
    print("  !! = cross-skill gap < 0.10")
    print("  In sweep: N!! = over cliff,  N* = at cliff edge")


if __name__ == "__main__":
    main()

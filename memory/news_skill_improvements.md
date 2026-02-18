# News Skill Improvements Needed

**Logged:** Feb 17, 2026
**Priority:** Medium — affects daily usability
**Source:** Live log observation by the developer

## Problem

When the user asked "what's the latest critical news headline?", JARVIS:
1. Matched `NewsSkill_news_count` (semantic score=0.65) — gave a count summary of all 590 headlines
2. On follow-up ("I just want to hear the latest critical news headline") — matched the same count intent again, same response

## Root Causes

### 1. Missing "read critical/urgent headlines" intent
The news skill has intents for counting and general reading, but no intent for
**filtered reading by urgency level**. Phrases like "critical news", "urgent headlines",
"breaking news" should route to reading only high-urgency headlines.

### 2. No urgency filter parameter in read_headlines()
`news_manager.read_headlines()` takes a `limit` and optionally a `category`, but
there's no `urgency` filter. The news system already classifies headlines by urgency
(critical/high/normal/low) — the plumbing exists, just not exposed to the user.

### 3. Semantic intent gap
"Latest critical news headline" is semantically closer to "read headlines" than
"how many headlines", but the count intent won the match. Need a dedicated intent
with examples like:
- "read me the critical headlines"
- "any urgent news?"
- "what's the breaking news?"
- "read the important headlines"
- "any critical alerts?"

## Proposed Fix

1. **Add urgency-filtered read intent** to NewsSkill with semantic examples
2. **Add `urgency` param** to `read_headlines()` in `news_manager.py`
3. **Add "read latest N from category/urgency" intent** — e.g., "read the latest tech headlines"
4. **Consider proactive critical announcement** — if a critical headline comes in, JARVIS should announce it without being asked (may already be partially implemented via `has_pending_announcement()`)

## Relevant Files

- `/mnt/storage/jarvis/skills/personal/news/` — skill implementation
- `core/news_manager.py` — RSS polling, classification, read_headlines()
- `core/news_manager.py` — `classify_urgency()`, `has_pending_announcement()`

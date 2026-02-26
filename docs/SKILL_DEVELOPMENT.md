# JARVIS Skill Development Guide

> **AI Assistants:** The authoritative skill development directive is in Claude Code memory at
> `memory/SKILL_DEVELOPMENT_DIRECTIVE.md`. That document contains hard-won lessons from 9 skill
> implementations and supersedes the threshold/example guidance in this file. Consult it FIRST.

## Overview
Skills are modular components that handle specific intents and commands. JARVIS uses a hybrid intent matching system with semantic understanding.

## Skill Structure
```
/mnt/storage/jarvis/skills/
├── system/              # System-level skills
│   ├── weather/         # Weather forecasts
│   ├── time_info/       # Time and date
│   ├── system_info/     # CPU, memory, system info
│   ├── filesystem/      # File search, line counting
│   ├── file_editor/     # File ops + document generation (PPTX/DOCX/PDF)
│   ├── developer_tools/ # Codebase search, git, shell, visual output
│   ├── app_launcher/    # Desktop control (16 intents)
│   └── web_navigation/  # Web search & browsing
└── personal/            # User-specific skills
    ├── conversation/    # Greetings, small talk
    ├── reminders/       # Voice reminders + calendar
    ├── news/            # RSS headline delivery
    └── social_introductions/  # Butler-style introductions + people DB
```

Each skill directory contains:
- `metadata.yaml` - Skill configuration
- `skill.py` - Main skill implementation
- `__init__.py` - Package initializer

## Creating a New Skill

### Step 1: Create Directory Structure
```bash
cd /mnt/storage/jarvis/skills/system
mkdir my_new_skill
cd my_new_skill
```

### Step 2: Create metadata.yaml
```yaml
name: my_new_skill
version: 1.0.0
description: Brief description of what this skill does
author: Your Name
category: system  # or 'personal'
dependencies: []  # Python packages needed
```

### Step 3: Create __init__.py
```python
from .skill import MyNewSkill
```

### Step 4: Create skill.py
```python
from core.base_skill import BaseSkill
from typing import Dict

class MyNewSkill(BaseSkill):
    """
    Brief description of your skill
    """
    
    def initialize(self) -> bool:
        """
        Register intents and initialize skill.
        Called once when skill loads.
        
        Returns:
            True if initialization successful
        """
        
        # Register semantic intents (similarity-based matching)
        self.register_semantic_intent(
            examples=[
                "example phrase 1",
                "example phrase 2",
                "example phrase 3",
                "example phrase 4"
            ],
            handler=self.my_handler_method,
            threshold=0.75  # Similarity score (0.0-1.0)
        )
        
        # Can register multiple intents
        self.register_semantic_intent(
            examples=[
                "another example",
                "different phrasing"
            ],
            handler=self.another_handler,
            threshold=0.70
        )
        
        self.logger.info("✓ MyNewSkill initialized")
        return True  # IMPORTANT: Must return True!
    
    def handle_intent(self, intent: str, entities: dict) -> str:
        """
        Route intent to appropriate handler.
        
        Args:
            intent: Intent ID (e.g., "MyNewSkill_my_handler_method")
            entities: Extracted entities (includes 'original_text', 'similarity')
        
        Returns:
            Response string to speak to user
        """
        # Check if this is one of our semantic intents
        if intent in self.semantic_intents:
            handler = self.semantic_intents[intent]['handler']
            return handler(entities)
        
        self.logger.error(f"Unknown intent: {intent}")
        return "I'm sorry, I don't understand that command, sir."
    
    def my_handler_method(self, entities: dict) -> str:
        """
        Handle the intent - do the actual work
        
        Args:
            entities: Dict containing:
                - original_text: User's original query
                - similarity: Match confidence score
        
        Returns:
            Response string
        """
        try:
            # Your skill logic here
            result = "Processing..."
            
            return f"Done, sir. {result}"
            
        except Exception as e:
            self.logger.error(f"Error: {e}")
            return "I encountered an error, sir."
    
    def another_handler(self, entities: dict) -> str:
        """Another handler for different intent"""
        return "Another response, sir."
```

## Intent Matching System

### Semantic Intents (Recommended)
Uses AI embeddings for flexible, natural language matching.

**When to use:**
- Natural language queries
- Questions with varied phrasing
- User might ask in different ways

**Example:**
```python
self.register_semantic_intent(
    examples=[
        "how many lines of code in your codebase",
        "count lines in jarvis",
        "how big is the project",
        "show me code statistics"
    ],
    handler=self.count_code_lines,
    threshold=0.70  # Adjust based on how strict you want matching
)
```

**Threshold Guidelines:**
- `0.85+`: Very strict, exact phrasing
- `0.75-0.84`: Standard, similar phrasing
- `0.70-0.74`: Flexible, related concepts
- `<0.70`: Too loose, may false-match

### Exact Pattern Matching (Legacy)
For precise command matching with variables.
```python
self.register_intent(
    pattern="set timer for {duration} minutes",
    handler=self.set_timer,
    priority=7
)
```

## Best Practices

### 1. Return True from initialize()
```python
def initialize(self) -> bool:
    # ... registration code ...
    return True  # CRITICAL!
```

### 2. Provide 4-5 Example Phrases
More examples = better matching
```python
examples=[
    "find my file",
    "where is my document",
    "locate the spreadsheet",
    "search for file named expenses"
]
```

### 3. Handle Errors Gracefully
```python
try:
    # ... logic ...
    return "Success, sir."
except Exception as e:
    self.logger.error(f"Error: {e}")
    return "I encountered an error, sir."
```

### 4. Log Important Actions
```python
self.logger.info("Processing file search...")
self.logger.debug(f"Found {count} results")
self.logger.error(f"Failed: {error}")
```

### 5. Exclude Virtual Environments
When searching files:
```python
result = subprocess.run(
    ['find', str(path), '-name', '*.py', '-type', 'f',
     '-not', '-path', '*/venv*',
     '-not', '-path', '*/__pycache__/*'],
    capture_output=True,
    text=True
)
```

## Skill Deployment

### 1. Test Your Skill
```bash
cd ~/jarvis
# Restart JARVIS to load new skill
restartjarvis

# Watch logs for errors
journalctl --user -u jarvis -f
```

### 2. Check Skill Loaded
Look for in logs:
```
✅ Loaded skill: my_new_skill (system)
```

### 3. Test Semantic Matching
Speak to JARVIS and check logs for:
```
🎯 Semantic score=0.XX
🎯 Matched semantic intent: MyNewSkill_handler_name -> my_new_skill
```

### 4. Debug Issues

**Skill not loading:**
```bash
# Check for Python errors
journalctl --user -u jarvis -n 200 | grep "my_new_skill\|Error\|Traceback"
```

**Intents not registering:**
```bash
# Verify semantic_intents populated
journalctl --user -u jarvis -n 200 | grep "📋.*my_new_skill"
```

**Not matching user input:**
- Lower threshold (try 0.65-0.70)
- Add more varied example phrases
- Check logs for similarity score

## Common Patterns

### File Operations
```python
from pathlib import Path
import subprocess

def search_files(self, entities: dict) -> str:
    search_path = Path.home() / "Documents"
    result = subprocess.run(
        ['find', str(search_path), '-name', '*.pdf'],
        capture_output=True,
        text=True,
        timeout=10
    )
    files = [f for f in result.stdout.strip().split('\n') if f]
    return f"Found {len(files)} files, sir."
```

### System Commands
```python
import subprocess

def get_disk_space(self, entities: dict) -> str:
    result = subprocess.run(['df', '-h', '/'], 
                          capture_output=True, text=True)
    # Parse result...
    return f"Disk usage: {usage}, sir."
```

### API Calls
```python
import requests

def fetch_data(self, entities: dict) -> str:
    try:
        response = requests.get('https://api.example.com/data', 
                              timeout=10)
        response.raise_for_status()
        data = response.json()
        return f"Retrieved data, sir."
    except Exception as e:
        self.logger.error(f"API error: {e}")
        return "I couldn't fetch that data, sir."
```

## Testing Checklist

- [ ] Skill loads without errors
- [ ] Semantic intents register
- [ ] Voice commands match correctly
- [ ] Handler executes successfully
- [ ] Response is spoken via TTS
- [ ] Errors handled gracefully
- [ ] Logs show useful debug info
- [ ] No virtual env pollution (if file operations)

## Troubleshooting

### "Skill failed to initialize"
- Check `initialize()` returns `True`
- Verify no syntax errors
- Check all imports available

### "I don't understand that command"
- Semantic match score too low
- Add more example phrases
- Lower threshold slightly

### Response not spoken
- Check `handle_intent()` returns string
- Verify no exceptions in handler
- Check TTS logs

### Audio overflow warnings
- Long operations blocking
- Move heavy processing out of handlers
- Preload models/data in initialize()

## Advanced: Using LLM in Skills
```python
def analyze_with_llm(self, entities: dict) -> str:
    from core.llm_router import LLMRouter
    
    llm = LLMRouter(self.config)
    user_text = entities.get('original_text', '')
    
    prompt = f"Analyze this query: {user_text}"
    response = llm.generate(prompt, use_api=False, max_tokens=200)
    
    return response
```

## Skill Ideas (Not Yet Implemented)

- **Email:** Check/send emails (Gmail API + OAuth)
- **Google Keep:** Shared grocery/todo lists
- **Audio Recording:** Voice memos, meeting notes
- **Music Control:** Apple Music integration
- **Calculator:** Mathematical operations
- **Unit Converter:** Temperature, distance, etc.
- **Home Automation:** Control smart devices

---

**Remember:** Skills make JARVIS smarter! Keep them focused, test thoroughly, and handle errors gracefully.

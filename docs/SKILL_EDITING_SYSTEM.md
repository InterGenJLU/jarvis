# Skill Editing System Design

## Overview
Natural language skill editing without leaving voice interface. Users can query which skill was used, request edits, review changes, and apply them - all conversationally.

## User Flow

### 1. Query Last Skill Used
**Patterns:**
- "what skill did you just use"
- "which skill was that"
- "what skill handled that"
- "what was that skill called"

**Response:**
```
"That was the {skill_name} skill, sir."
```

**Context Tracking:**
```python
# Track last skill used per conversation session
self.last_skill_used = {
    "name": "system_info",
    "display_name": "System Information",
    "path": "/mnt/storage/jarvis/skills/system/system_info/",
    "function": "get_cpu_info",
    "timestamp": datetime.now()
}
```

### 2. Request Edit
**Patterns:**
- "edit the {skill_name} skill"
- "let's edit {skill_name}"
- "can we edit the {skill_name} skill"
- "modify the {skill_name} skill"
- "update {skill_name}"

**If skill name not specified:**
- "edit that skill"
- "let's edit it"
- "modify that"

**Response:**
```
"Of course, sir. What would you like to change in the {skill_name} skill?"
```

**State:**
```python
self.editing_mode = True
self.editing_skill = "system_info"
self.editing_context = {
    "skill_path": "/mnt/storage/jarvis/skills/system/system_info/",
    "original_files": {},  # Backup
    "proposed_changes": []
}
```

### 3. Describe Change (LLM-Powered)
User describes what they want changed in natural language.

**Examples:**
- "Update the CPU normalization to read 5900X as fifty-nine hundred X"
- "Add more greeting variations - 'hey there' and 'what's up'"
- "Change the weather response to be less verbose"
- "Add support for asking about GPU instead of just CPU"

**LLM Prompt:**
```
You are helping the user edit a Jarvis skill. 

Current skill: {skill_name}
Skill files:
{file_tree}

User's request: "{user_request}"

Analyze the request and determine:
1. Which file(s) need modification
2. What specific changes are needed
3. Generate the code changes

Respond in JSON:
{
  "understood": true/false,
  "files_to_modify": ["skill.py", "tts_normalizer.py"],
  "changes": [
    {
      "file": "core/tts_normalizer.py",
      "action": "add_function",
      "location": "after normalize_ports",
      "code": "..."
    }
  ],
  "explanation": "I'll add a CPU model normalization function to the TTS normalizer...",
  "test_example": "Your processor is an AMD Ryzen 9 fifty-nine hundred X"
}
```

**Jarvis Response:**
```
"I understand, sir. {explanation}"
[plays code_review_tone.wav]
"The change is ready. {test_example}. Does that sound right, sir?"
```

### 4. Review & Feedback Loop

**User can:**
- **Approve:** "yes", "perfect", "that's right", "correct"
- **Reject:** "no", "that's wrong", "not quite"
- **Modify:** "close, but make it say X instead"
- **Show code:** "show me the code", "let me see the changes"

**If approved:**
```
"Excellent, sir. Applying the changes now."
[applies changes]
"The {skill_name} skill is now updated."
```

**If rejected:**
```
"My apologies, sir. Let's try again. What should I change?"
[returns to step 3]
```

**If "show code" requested:**
```
"Opening the code diff in your editor, sir."
[creates temp diff file, opens in default editor]
"Would you like to proceed with these changes?"
```

### 5. Apply Changes

**Process:**
1. Backup original files
2. Apply modifications
3. Run validation (`validate_skill.py`)
4. If validation passes → commit
5. If validation fails → revert and report error

**Success:**
```
"The changes have been applied, sir. The skill is ready to use."
```

**Failure:**
```
"I encountered an error, sir: {error_message}. I've reverted the changes. Would you like to try a different approach?"
```

## Technical Implementation

### Skill Editing Skill
**Location:** `/mnt/storage/jarvis/skills/system/skill_editor/`

**Files:**
```
skill_editor/
├── skill.py           # Main skill logic
├── metadata.yaml      # Skill metadata
├── llm_analyzer.py    # LLM-powered code analysis
├── code_modifier.py   # Safe code modification
├── validator.py       # Change validation
└── backups/           # Automatic backups
```

### Core Components

#### 1. Context Tracker
```python
class SkillContextTracker:
    """Track which skills are used for 'what skill was that' queries"""
    
    def __init__(self):
        self.history = []
        self.max_history = 10
    
    def record_skill_use(self, skill_name, function_name, query):
        """Record a skill execution"""
        self.history.append({
            "skill": skill_name,
            "function": function_name,
            "query": query,
            "timestamp": datetime.now()
        })
        
        # Keep only last N entries
        if len(self.history) > self.max_history:
            self.history.pop(0)
    
    def get_last_skill(self):
        """Get the most recently used skill"""
        if self.history:
            return self.history[-1]
        return None
```

**Integration Point:**
Modify `jarvis_continuous.py` to track skill usage:
```python
# After skill executes
if skill_result:
    self.skill_context_tracker.record_skill_use(
        skill_name=matched_skill.name,
        function_name=matched_function.__name__,
        query=command
    )
```

#### 2. LLM Code Analyzer
```python
class LLMCodeAnalyzer:
    """Use LLM to understand and generate code changes"""
    
    def analyze_change_request(self, skill_path, user_request):
        """Analyze what needs to change"""
        
        # Read skill files
        skill_files = self._read_skill_files(skill_path)
        
        # Build context prompt
        prompt = f"""
        You are helping edit a Jarvis voice assistant skill.
        
        Skill files:
        {self._format_files(skill_files)}
        
        User wants to: {user_request}
        
        Determine:
        1. Which files need changes
        2. What specific modifications
        3. Generate the code
        
        Return JSON with changes.
        """
        
        # Call LLM (Claude API)
        response = self.llm.generate(prompt)
        
        return self._parse_change_plan(response)
```

#### 3. Safe Code Modifier
```python
class SafeCodeModifier:
    """Apply code changes safely with backups"""
    
    def apply_changes(self, changes):
        """Apply proposed changes with backup"""
        
        # Create backup
        backup_id = self._create_backup(changes)
        
        try:
            # Apply each change
            for change in changes:
                self._apply_single_change(change)
            
            # Validate
            if not self._validate_changes():
                raise ValidationError("Changes failed validation")
            
            return True
            
        except Exception as e:
            # Revert on error
            self._restore_backup(backup_id)
            raise e
```

#### 4. Change Validator
```python
class ChangeValidator:
    """Validate skill changes before applying"""
    
    def validate(self, skill_path):
        """Run validation checks"""
        
        checks = [
            self._check_syntax(),
            self._check_imports(),
            self._check_skill_structure(),
            self._run_skill_validator()  # validate_skill.py
        ]
        
        for check in checks:
            result = check(skill_path)
            if not result.passed:
                return ValidationResult(
                    passed=False,
                    error=result.error
                )
        
        return ValidationResult(passed=True)
```

### Conversation State Management

```python
class SkillEditingSession:
    """Manage skill editing conversation state"""
    
    def __init__(self):
        self.active = False
        self.skill_name = None
        self.skill_path = None
        self.proposed_changes = None
        self.awaiting_approval = False
        self.backup_id = None
    
    def start_editing(self, skill_name):
        """Begin editing session"""
        self.active = True
        self.skill_name = skill_name
        self.skill_path = self._resolve_skill_path(skill_name)
    
    def propose_changes(self, changes):
        """Store proposed changes for review"""
        self.proposed_changes = changes
        self.awaiting_approval = True
    
    def apply_approved_changes(self):
        """Apply changes after user approval"""
        # Implementation
        pass
    
    def cancel(self):
        """Cancel editing session"""
        self.active = False
        self.proposed_changes = None
```

## Audio Cues

**Code Review Tone:** (already created)
- File: `/home/user/jarvis/assets/code_review_tone.wav`
- When: After generating proposed changes
- Purpose: Signal transition to review mode

**Success Tone:** (already created)
- File: `/home/user/jarvis/assets/success_tone.wav`
- When: After successfully applying changes
- Purpose: Confirm completion

**Error Tone:** (already created)
- File: `/home/user/jarvis/assets/error_tone.wav`
- When: Validation fails or error occurs
- Purpose: Alert to problem

## Safety Features

### 1. Automatic Backups
Every edit creates timestamped backup:
```
/mnt/storage/jarvis/backups/skill_editor/
├── system_info_2026-02-09_10-30-15/
│   ├── skill.py
│   ├── metadata.yaml
│   └── manifest.json
```

### 2. Validation Gates
Changes must pass:
- Python syntax check
- Import verification
- Skill structure validation
- `validate_skill.py` checks

### 3. Rollback Support
```
User: "Jarvis, undo the last change to system_info"
Jarvis: "Reverting the system_info skill to its previous state, sir."
[restores from backup]
Jarvis: "The skill has been restored, sir."
```

### 4. Change History
```
User: "Jarvis, show me the last 5 changes to system_info"
Jarvis: "The system_info skill was modified 5 times:
1. February 9th at 10:30 AM - Added CPU model normalization
2. February 8th at 3:15 PM - Expanded greeting patterns
...
Would you like details on any of these?"
```

## Example Interactions

### Example 1: CPU Normalization (Your Use Case)
```
User: "Jarvis, what CPU do I have in this machine?"
Jarvis: "You have an AMD Ryzen 9 fifty-nine hundred X with 24 cores, sir."
User: "What skill was that?"
Jarvis: "That was the system_info skill, sir."
User: "Let's edit it"
Jarvis: "Of course, sir. What would you like to change?"
User: "The CPU pronunciation was wrong. It should say 'fifty-nine hundred X' not 'five nine zero zero X'"
Jarvis: "I understand, sir. I'll update the TTS normalizer to handle CPU model numbers properly."
[code_review_tone.wav plays]
Jarvis: "The change is ready. For example, your processor is an AMD Ryzen 9 fifty-nine hundred X. Does that sound right?"
User: "Perfect"
Jarvis: "Applying the changes now, sir."
[success_tone.wav plays]
Jarvis: "The system_info skill is now updated."
```

### Example 2: Adding Greeting Variations
```
User: "Jarvis, hey there"
[no response - not recognized]
User: "Jarvis, what skill handles greetings?"
Jarvis: "That would be the conversation skill, sir."
User: "Can we edit it to add 'hey there' as a greeting?"
Jarvis: "Absolutely, sir. What would you like to change?"
User: "Add 'hey there' and 'what's up' as greeting patterns"
Jarvis: "I'll add those patterns to the greeting function, sir."
[code_review_tone.wav]
Jarvis: "The changes are ready. I've added 'hey there' and 'what's up' as greeting triggers. Shall I apply them?"
User: "Yes"
Jarvis: "Done, sir. Try saying 'hey there' now."
User: "Jarvis, hey there"
Jarvis: "Hello, sir."
User: "Excellent, thank you"
Jarvis: "Of course, sir."
```

### Example 3: Complex Edit with Multiple Files
```
User: "Edit the weather skill to include humidity and wind chill"
Jarvis: "What would you like to change in the weather skill, sir?"
User: "When I ask for weather, also tell me the humidity and wind chill if it's cold"
Jarvis: "I'll modify the weather skill to include humidity and add wind chill calculations for temperatures below 50 degrees Fahrenheit, sir."
[code_review_tone.wav]
Jarvis: "The changes affect both the weather skill and the API response handler. For example: 'It's 32 degrees with 65% humidity and feels like 24 degrees with the wind chill.' Does that work?"
User: "Perfect"
Jarvis: "Applying changes to 2 files, sir."
[success_tone.wav]
Jarvis: "The weather skill is now updated with humidity and wind chill data."
```

## Integration with Existing Systems

### 1. Skill Manager Integration
The skill manager needs to notify the context tracker:
```python
# In core/skill_manager.py
def execute_skill(self, skill, function, args):
    result = function(*args)
    
    # Notify context tracker
    if hasattr(self, 'context_tracker'):
        self.context_tracker.record_skill_use(
            skill_name=skill.name,
            function_name=function.__name__,
            query=args[0] if args else None
        )
    
    return result
```

### 2. Conversation Manager Extension
Track multi-turn editing sessions:
```python
# In core/conversation.py
class ConversationManager:
    def __init__(self):
        # ... existing code ...
        self.editing_session = None  # SkillEditingSession
    
    def is_editing_active(self):
        return self.editing_session and self.editing_session.active
```

### 3. LLM Router Enhancement
Add special handling for code generation:
```python
# In core/llm_router.py
def generate_code_changes(self, skill_context, user_request):
    """Special LLM call for code generation"""
    
    prompt = self._build_code_edit_prompt(skill_context, user_request)
    
    # Use Claude API with extended token limit for code
    response = self.api_client.generate(
        prompt=prompt,
        max_tokens=4000,  # More tokens for code
        temperature=0.2    # Lower temp for code accuracy
    )
    
    return self._parse_code_response(response)
```

## Phase Implementation

### Phase 1: Basic Query & Context (Week 1)
- [x] Context tracker implementation
- [x] "What skill was that?" query
- [x] Basic skill identification

### Phase 2: Edit Request Handling (Week 2)
- [ ] Edit mode state management
- [ ] Natural language pattern recognition
- [ ] Skill file reading

### Phase 3: LLM-Powered Changes (Week 3)
- [ ] LLM code analyzer
- [ ] Change generation
- [ ] Code review tone integration

### Phase 4: Validation & Safety (Week 4)
- [ ] Automatic backups
- [ ] Validation pipeline
- [ ] Rollback support

### Phase 5: Apply & Test (Week 5)
- [ ] Safe code modifier
- [ ] Change application
- [ ] Testing framework
- [ ] Success/error tones

## Configuration

Add to `config.yaml`:
```yaml
skill_editor:
  enabled: true
  backup_dir: "/mnt/storage/jarvis/backups/skill_editor"
  max_backups: 50  # Keep last 50 backups
  validation_strict: true
  require_approval: true  # Always ask before applying
  llm:
    model: "claude-sonnet-4"  # Use latest Claude for code gen
    max_tokens: 4000
    temperature: 0.2
  audio_cues:
    code_review_tone: "~/jarvis/assets/code_review_tone.wav"
    success_tone: "~/jarvis/assets/success_tone.wav"
    error_tone: "~/jarvis/assets/error_tone.wav"
```

## Notes

**Why This Matters:**
- No context switching (stay in voice)
- Iterate quickly on skills
- Natural workflow
- Safe with backups/validation
- Empowers user customization

**Challenges:**
- LLM code generation accuracy
- Complex multi-file changes
- Validation comprehensiveness
- Handling edge cases

**Future Enhancements:**
- Visual diff display (web interface)
- A/B testing of changes
- Skill versioning
- Shared skill marketplace
- Collaborative editing

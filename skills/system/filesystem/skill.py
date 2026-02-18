from pathlib import Path
import subprocess
import os
from core.base_skill import BaseSkill
from core.llm_router import LLMRouter

class FilesystemSkill(BaseSkill):
    def initialize(self):
        """Register filesystem intents"""
        self.logger.info("🔧 Filesystem skill initializing...")
        
        # File search
        self.register_semantic_intent(
            examples=[
                "where is expenses.xlsx",
                "find the file named report.pdf",
                "locate my presentation",
                "where did I save that spreadsheet"
            ],
            handler=self.find_file,
            threshold=0.70
        )
        self.logger.info("✓ Registered find_file")
        
        # Code analysis
        self.register_semantic_intent(
            examples=[
                "how many lines of code in your codebase",
                "count lines in jarvis",
                "how big is the project",
                "show me code statistics"
            ],
            handler=self.count_code_lines,
            threshold=0.70
        )
        self.logger.info("✓ Registered count_code_lines")
        
        # Directory file counting
        self.register_semantic_intent(
            examples=[
                "how many files are in my documents folder",
                "count files in downloads",
                "how many files in the home directory",
                "how many items in my desktop folder",
                "count the files in scripts"
            ],
            handler=self.count_files_in_directory,
            threshold=0.70
        )
        
        # Script analysis
        self.register_semantic_intent(
            examples=[
                "what does my backup script do",
                "analyze the install.sh file",
                "explain my deploy script",
                "what's in the cleanup.py file",
                "tell me about the backup.sh script",
                "please analyze test_backup.sh",
                "analyze test_backup",
                "what does test_backup.sh do",
                "please explain the backup script"
            ],
            handler=self.analyze_script,
            threshold=0.45  # Low threshold — filenames vary widely but no false positive risk (other skills <0.20)
        )
    
        return True
    
    def handle_intent(self, intent: str, entities: dict) -> str:
        """Handle semantic intents"""
        # Intent is the intent_id like "FilesystemSkill_count_code_lines"
        if intent in self.semantic_intents:
            handler = self.semantic_intents[intent]['handler']
            return handler(entities)
        
        # Legacy format support
        if intent.startswith("<semantic:") and intent.endswith(">"):
            handler_name = intent[10:-1]
            for intent_id, data in self.semantic_intents.items():
                if data['handler'].__name__ == handler_name:
                    return data['handler'](entities)
        
        self.logger.error(f"Unknown intent: {intent}")
        return "I'm sorry, I don't understand that command."
    
    def find_file(self, entities: dict = None) -> str:
        """Search for files in user directories"""
        # Extract filename from original query
        query = entities.get('original_text', '').lower()
        
        # Simple filename extraction (look for common extensions or quoted names)
        import re
        
        # Try to find filename with extension
        filename_match = re.search(r'(\w+\.\w+)', query)
        if filename_match:
            filename = filename_match.group(1)
        else:
            # Try to find quoted or emphasized name
            words = query.split()
            filename = None
            for word in words:
                if len(word) > 3 and word not in ['file', 'find', 'where', 'locate', 'named', 'called']:
                    filename = word
                    break
        
        if not filename:
            return f"I couldn't identify which file you're looking for, {self.honorific}."
        
        # Search in common locations
        search_paths = [
            str(Path.home()),
            str(Path.home() / "Documents"),
            str(Path.home() / "Downloads"),
            str(Path.home() / "Desktop")
        ]
        
        try:
            for search_path in search_paths:
                result = subprocess.run(
                    ['find', search_path, '-name', f"*{filename}*", '-type', 'f'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                files = [f for f in result.stdout.strip().split('\n') if f]
                if files:
                    if len(files) == 1:
                        return f"Found it, {self.honorific}: {files[0]}"
                    else:
                        return f"Found {len(files)} matches, {self.honorific}. The first is: {files[0]}"
            
            return f"I couldn't locate {filename}, {self.honorific}."
            
        except Exception as e:
            self.logger.error(f"File search error: {e}")
            return f"I encountered an error searching for that file, {self.honorific}."
    
    def count_code_lines(self, entities: dict = None) -> str:
        """Count lines of code in JARVIS codebase"""
        try:
            jarvis_path = Path.home() / "jarvis"
            
            # Count Python files
            result = subprocess.run(
                ['find', str(jarvis_path), '-name', '*.py', '-type', 'f',
                 '-not', '-path', '*/venv*',
                 '-not', '-path', '*/__pycache__/*'],
                capture_output=True,
                text=True
            )
            
            py_files = [f for f in result.stdout.strip().split('\n') if f]
            
            if not py_files:
                return f"I couldn't find my codebase, {self.honorific}."
            
            # Count lines
            total_lines = 0
            for py_file in py_files:
                try:
                    with open(py_file, 'r') as f:
                        total_lines += len(f.readlines())
                except:
                    continue
            
            return f"My codebase contains {total_lines:,} lines of Python code across {len(py_files)} files, {self.honorific}."
            
        except Exception as e:
            self.logger.error(f"Code count error: {e}")
            return f"I encountered an error analyzing my codebase, {self.honorific}."
    
    def count_files_in_directory(self, entities: dict) -> str:
        """Count files in a specified directory"""
        try:
            query = entities.get('original_text', '').lower()
            
            # Extract directory name
            import re
            
            # Common directories
            dir_map = {
                'documents': Path.home() / 'Documents',
                'downloads': Path.home() / 'Downloads',
                'desktop': Path.home() / 'Desktop',
                'home': Path.home(),
                'pictures': Path.home() / 'Pictures',
                'videos': Path.home() / 'Videos',
                'music': Path.home() / 'Music',
                'scripts': Path.home() / 'scripts',
                'bin': Path.home() / 'bin',
                'jarvis': Path.home() / 'jarvis',
                'core': Path.home() / 'jarvis' / 'core',
                'skills': Path('/mnt/storage/jarvis/skills'),
                'models': Path('/mnt/models'),
            }
            
            # Try to find directory in query
            # Strip common words first
            clean_query = query.replace('the ', '').replace(' directory', '').replace(' folder', '')
            
            target_dir = None
            dir_name = None
            for name, path in dir_map.items():
                if name in clean_query:
                    target_dir = path
                    dir_name = name
                    break
            
            if not target_dir:
                return f"Which directory would you like me to count files in, {self.honorific}?"
            
            if not target_dir.exists():
                return f"The {dir_name} directory doesn't exist, {self.honorific}."
            
            # Count files (not directories)
            file_count = sum(1 for item in target_dir.iterdir() if item.is_file())
            dir_count = sum(1 for item in target_dir.iterdir() if item.is_dir())
            
            if file_count == 0 and dir_count == 0:
                return f"The {dir_name} directory is empty, {self.honorific}."
            elif dir_count == 0:
                return f"There are {file_count:,} files in your {dir_name} directory, {self.honorific}."
            else:
                return f"There are {file_count:,} files and {dir_count:,} folders in your {dir_name} directory, {self.honorific}."
            
        except PermissionError:
            return f"I don't have permission to access that directory, {self.honorific}."
        except Exception as e:
            self.logger.error(f"Directory count error: {e}")
            return f"I encountered an error counting files in that directory, {self.honorific}."
    
    def analyze_script(self, entities: dict = None) -> str:
        """Analyze a script file using LLM"""
        try:
            # Extract potential script name from query
            query = entities.get('original_text', '').lower()
            
            # Look for common script patterns
            import re
            
            # Extract script name - handle various formats
            script_name = None
            
            # Direct patterns
            script_patterns = [
                r'test[_\s-]?backup(?:\.sh)?',  # test_backup, test backup, testbackup
                r'backup(?:\.sh)?',             # backup, backup.sh
                r'install(?:\.sh)?',            # install, install.sh
                r'(\w+)\.(?:sh|py|bash|pl)',   # anything.sh/py/bash/pl
            ]
            
            for pattern in script_patterns:
                match = re.search(pattern, query, re.IGNORECASE)
                if match:
                    script_name = match.group(0)
                    # Normalize: remove spaces, dots from middle
                    script_name = script_name.replace(' ', '_')
                    break
            
            # Handle "underscore" and "dot" spoken words
            if not script_name and ('underscore' in query or 'dot sh' in query):
                # "test underscore backup dot sh" → "test_backup.sh"
                script_name = query.replace(' underscore ', '_')
                script_name = script_name.replace(' dot sh', '.sh')
                script_name = script_name.replace(' dot py', '.py')
                # Clean up
                words = script_name.split()
                if len(words) > 0:
                    script_name = words[0] if '.' in words[0] else words[0] + '.sh'
            
            if not script_name:
                return f"Which script would you like me to analyze, {self.honorific}?"
            
            # Search for the script
            # Prioritize user's home and obvious script locations
            search_paths = [
                (str(Path.home()), True),           # Home dir, recursive
                (str(Path.home() / "scripts"), False),
                (str(Path.home() / "bin"), False),
                ("/usr/local/bin", False),
            ]
            
            all_matches = []
            for search_path, recursive in search_paths:
                if not Path(search_path).exists():
                    continue
                
                cmd = ['find', search_path]
                if not recursive:
                    cmd.extend(['-maxdepth', '1'])
                # Be more specific - look for script extensions
                extensions = ['*.sh', '*.bash', '*.py', '*.pl', '*.rb']
                name_pattern = f"*{script_name}*" if '.' in script_name else f"*{script_name}.*"
                
                cmd.extend([
                    '-name', name_pattern, 
                    '-type', 'f', '-readable',
                    '-not', '-path', '*/venv*',
                    '-not', '-path', '*/.git/*',
                    '-not', '-path', '*/node_modules/*',
                    '-not', '-path', '*/__pycache__/*',
                    '-not', '-path', '*/site-packages/*',
                    '-not', '-path', '*/.cache/*',
                    '-not', '-path', '*/.local/*',
                    '-not', '-path', '*/snap/*',
                    '-not', '-path', '*/lib/*',
                    '-not', '-path', '*/include/*'
                ])
                
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                matches = [f for f in result.stdout.strip().split('\n') if f]
                all_matches.extend(matches)
                
                # Stop if we found exact match in home
                if matches and recursive:
                    break
            
            if not all_matches:
                return f"I couldn't locate a script named {script_name}, {self.honorific}."
            
            # Prefer files in home directory
            home_matches = [f for f in all_matches if str(Path.home()) in f]
            files = home_matches if home_matches else all_matches
            
            script_path = files[0]
            
            if not script_path:
                return f"I couldn't locate a script named {script_name}, {self.honorific}."
            
            # TOO MANY matches? Give up
            if len(files) > 10:
                return f"I found {len(files)} files matching '{script_name}', {self.honorific}. Please be more specific with the filename."
            
            # Multiple matches? Show first 3
            if len(files) > 1:
                file_list = "\n  ".join([f"• {Path(f).name}" for f in files[:3]])
                more = f" and {len(files)-3} more" if len(files) > 3 else ""
                return f"I found {len(files)} matches{more}, {self.honorific}:\n  {file_list}\n\nWhich one would you like me to analyze?"
            
            # Confirm the file before analyzing
            self.logger.info(f"Analyzing: {script_path}")
            
            # Read the script content
            try:
                with open(script_path, 'r') as f:
                    content = f.read()
            except Exception as e:
                self.logger.error(f"Error reading {script_path}: {e}")
                return f"I couldn't read {script_path}, {self.honorific}."
            
            # Limit content size (don't send huge files to LLM)
            if len(content) > 5000:
                content = content[:5000] + "\n... [truncated]"
            
            # Ask LLM to analyze
            llm = LLMRouter(self.config)
            
            prompt = f"""You are JARVIS. Analyze this script in EXACTLY 2 sentences maximum. Be extremely brief.
First say what it does, then mention one key detail like the main command or path.
Do NOT prefix sentences with labels or numbers.

Script: {Path(script_path).name}
```
{content}
```

CRITICAL: Maximum 2 sentences. No more. No labels."""
            
            analysis = llm.generate(prompt, use_api=False, max_tokens=200)
            
            return f"Analyzing {Path(script_path).name}: {analysis}"
            
        except subprocess.TimeoutExpired:
            return f"The search took too long, {self.honorific}. Could you be more specific?"
        except Exception as e:
            self.logger.error(f"Script analysis error: {e}")
            return f"I encountered an error analyzing that script, {self.honorific}."

#!/usr/bin/env python3
"""
Skill Validation Tool

Validates that skill.py register_intent() calls match metadata.yaml intents.
This prevents the common bug where patterns are defined in metadata but not registered in code.

Usage:
    python3 validate_skill.py <skill_directory>
    python3 validate_skill.py --all  # Check all skills
"""

import sys
import yaml
import re
from pathlib import Path


def validate_skill(skill_dir: Path) -> bool:
    """
    Validate a single skill
    
    Returns:
        True if valid, False if validation fails
    """
    metadata_file = skill_dir / "metadata.yaml"
    skill_file = skill_dir / "skill.py"
    
    if not metadata_file.exists():
        print(f"❌ {skill_dir.name}: No metadata.yaml found")
        return False
    
    if not skill_file.exists():
        print(f"❌ {skill_dir.name}: No skill.py found")
        return False
    
    # Load metadata patterns
    with open(metadata_file) as f:
        metadata = yaml.safe_load(f)
        metadata_patterns = set(metadata.get('intents', []))
    
    # Extract patterns from skill.py
    with open(skill_file) as f:
        skill_content = f.read()
        skill_patterns = set(re.findall(r'self\.register_intent\("([^"]+)"', skill_content))
    
    # Check for missing patterns (critical error)
    missing = metadata_patterns - skill_patterns
    extra = skill_patterns - metadata_patterns
    
    if missing:
        print(f"\n❌ {skill_dir.name}: VALIDATION FAILED")
        print(f"   Found {len(missing)} patterns in metadata.yaml NOT registered in skill.py:")
        for pattern in sorted(missing):
            print(f"      • {pattern}")
        print(f"\n   These patterns will NEVER match! Add them to skill.py initialize():")
        print(f'   self.register_intent("{list(missing)[0]}", self.your_handler)')
        return False
    
    if extra:
        print(f"\n⚠️  {skill_dir.name}: Extra patterns in skill.py (not in metadata)")
        print(f"   This is OK, but consider adding to metadata.yaml for documentation:")
        for pattern in sorted(extra)[:5]:  # Show first 5
            print(f"      • {pattern}")
        if len(extra) > 5:
            print(f"      ... and {len(extra) - 5} more")
    
    print(f"✅ {skill_dir.name}: Valid ({len(metadata_patterns)} metadata patterns, {len(skill_patterns)} registered)")
    return True


def validate_all_skills(skills_base: Path) -> tuple[int, int]:
    """
    Validate all skills
    
    Returns:
        (valid_count, total_count)
    """
    valid = 0
    total = 0
    
    for category_dir in skills_base.iterdir():
        if not category_dir.is_dir():
            continue
        
        for skill_dir in category_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            
            if (skill_dir / "metadata.yaml").exists():
                total += 1
                if validate_skill(skill_dir):
                    valid += 1
    
    return valid, total


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 validate_skill.py <skill_directory>")
        print("       python3 validate_skill.py --all")
        sys.exit(1)
    
    if sys.argv[1] == "--all":
        skills_base = Path("/mnt/storage/jarvis/skills")
        print(f"Validating all skills in {skills_base}...\n")
        valid, total = validate_all_skills(skills_base)
        print(f"\n{'='*60}")
        print(f"Results: {valid}/{total} skills valid")
        if valid < total:
            print(f"⚠️  {total - valid} skills have validation errors!")
            sys.exit(1)
        else:
            print("✅ All skills validated successfully!")
    else:
        skill_dir = Path(sys.argv[1])
        if not validate_skill(skill_dir):
            sys.exit(1)

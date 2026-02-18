"""
System Info Skill

Provides system information: uptime, disk space, username, hostname.
"""

import subprocess
import platform
import shutil
from datetime import timedelta

from core.base_skill import BaseSkill


class SystemInfoSkill(BaseSkill):
    """System information skill"""
    
    def initialize(self) -> bool:
        """Initialize the skill"""
        # Register intents
        self.register_intent("what's my uptime", self.get_uptime)
        self.register_intent("what is my uptime", self.get_uptime)
        self.register_intent("what's my up time", self.get_uptime)
        self.register_intent("what is my up time", self.get_uptime)
        self.register_intent("how long have I been running", self.get_uptime)
        self.register_intent("system uptime", self.get_uptime)
        self.register_intent("system up time", self.get_uptime)
        self.register_intent("what's my system uptime", self.get_uptime)
        self.register_intent("what is my system uptime", self.get_uptime)
        self.register_intent("what's my system up time", self.get_uptime)
        self.register_intent("what is my system up time", self.get_uptime)
        self.register_intent("what's the system uptime", self.get_uptime)
        self.register_intent("what is the system uptime", self.get_uptime)
        self.register_intent("what's the system up time", self.get_uptime)
        self.register_intent("what is the system up time", self.get_uptime)
        
        # Disk space (root partition)
        # SEMANTIC MATCHING
        self.register_semantic_intent(
            examples=[
                "what's my disk space",
                "how much disk space do i have left",
                "check disk space",
                "disk usage",
                "how full is my hard drive"
            ],
            handler=self.get_disk_space,
            threshold=0.55
        )
        
        self.register_intent("who am i", self.get_username)
        self.register_intent("what's my username", self.get_username)
        self.register_intent("what is my username", self.get_username)
        
        self.register_intent("hostname", self.get_hostname)
        self.register_intent("what's my hostname", self.get_hostname)
        self.register_intent("what is my hostname", self.get_hostname)
        self.register_intent("what's my host name", self.get_hostname)
        self.register_intent("what is my host name", self.get_hostname)
        self.register_intent("computer name", self.get_hostname)
        
        # Hardware information - CPU
        # SEMANTIC MATCHING - replaces 67+ exact patterns
        self.register_semantic_intent(
            examples=[
                "what cpu do i have",
                "show me my processor",
                "what type of cpu is installed",
                "tell me about this computer's cpu",
                "which processor am i running"
            ],
            handler=self.get_cpu_info,
            threshold=0.75
        )
        
        
        
        
        
        
        # Hardware information - Memory/RAM
        # SEMANTIC MATCHING - catches "how much memory is in this machine" etc.
        self.register_semantic_intent(
            examples=[
                "how much ram do i have",
                "how much memory is in this machine",
                "what's my ram",
                "memory info",
                "show me memory usage",
                "how much memory do i have"
            ],
            handler=self.get_memory_info,
            threshold=0.55
        )

        # Hardware information - Disk/Storage
        # SEMANTIC MATCHING - catches "what's my hard drive usage" etc.
        self.register_semantic_intent(
            examples=[
                "list my hard drives",
                "what hard drives do i have",
                "show me my drives",
                "what's my current hard drive usage",
                "how much disk space do i have",
                "what's my storage usage"
            ],
            handler=self.get_all_drives,
            threshold=0.55
        )

        self.register_intent("what's mounted at {path}", self.get_drive_at_mount)
        self.register_intent("what is mounted at {path}", self.get_drive_at_mount)
        self.register_intent("what drive is at {path}", self.get_drive_at_mount)

        # Hardware information - GPU
        # SEMANTIC MATCHING - catches varied GPU queries
        self.register_semantic_intent(
            examples=[
                "what's my gpu",
                "what graphics card do i have",
                "gpu info",
                "show me my graphics card",
                "what gpu is installed"
            ],
            handler=self.get_gpu_info,
            threshold=0.55
        )
        
        return True
    
    def handle_intent(self, intent: str, entities: dict) -> str:
        """Handle matched intent"""
        # Check if this is a semantic match
        if intent.startswith("<semantic:") and intent.endswith(">"):
            # Extract handler name: <semantic:get_cpu_info> -> get_cpu_info
            handler_name = intent[10:-1]  # Remove "<semantic:" and ">"
            
            # Find the handler in semantic_intents
            for intent_id, data in self.semantic_intents.items():
                if data['handler'].__name__ == handler_name:
                    return data['handler']()
            
            self.logger.error(f"Semantic handler not found: {handler_name}")
            return "I'm sorry, I couldn't process that request."
        
        # Regular exact pattern match
        handler = self.intents.get(intent, {}).get("handler")
        if handler:
            return handler()
        return "I'm sorry, I couldn't process that request."
    
    def get_uptime(self) -> str:
        """Get system uptime"""
        try:
            # Read uptime from /proc/uptime
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.readline().split()[0])
            
            # Convert to human readable
            uptime_delta = timedelta(seconds=int(uptime_seconds))
            
            days = uptime_delta.days
            hours, remainder = divmod(uptime_delta.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            
            # Build response
            parts = []
            if days > 0:
                parts.append(f"{days} day{'s' if days != 1 else ''}")
            if hours > 0:
                parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
            if minutes > 0:
                parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
            
            if not parts:
                uptime_str = "less than a minute"
            else:
                uptime_str = ", ".join(parts)
            
            response = f"System uptime is {uptime_str}."
            return self.respond(response)
            
        except Exception as e:
            self.logger.error(f"Error getting uptime: {e}")
            return self.respond("I'm sorry, I couldn't retrieve the system uptime.")
    
    def get_disk_space(self) -> str:
        """Get disk space information"""
        try:
            # Get disk usage for root partition
            usage = shutil.disk_usage('/')
            
            total_gb = usage.total / (1024**3)
            used_gb = usage.used / (1024**3)
            free_gb = usage.free / (1024**3)
            percent = (usage.used / usage.total) * 100
            
            response = (
                f"Disk usage: {used_gb:.1f} gigabytes used out of "
                f"{total_gb:.1f} gigabytes total. "
                f"{free_gb:.1f} gigabytes free. "
                f"That's {percent:.0f} percent used."
            )
            
            return self.respond(response)
            
        except Exception as e:
            self.logger.error(f"Error getting disk space: {e}")
            return self.respond("I'm sorry, I couldn't retrieve disk space information.")
    
    def get_username(self) -> str:
        """Get current username"""
        try:
            import os
            username = os.getenv('USER') or os.getenv('USERNAME')
            
            if username:
                response = f"You are logged in as {username}."
            else:
                response = "I couldn't determine your username."
            
            return self.respond(response)
            
        except Exception as e:
            self.logger.error(f"Error getting username: {e}")
            return self.respond("I'm sorry, I couldn't retrieve your username.")
    
    def get_hostname(self) -> str:
        """Get system hostname"""
        try:
            hostname = platform.node()
            response = f"The system hostname is {hostname}."
            return self.respond(response)
            
        except Exception as e:
            self.logger.error(f"Error getting hostname: {e}")
            return self.respond("I'm sorry, I couldn't retrieve the hostname.")
    
    def get_cpu_info(self) -> str:
        """Get CPU information"""
        try:
            # Get CPU info from /proc/cpuinfo
            with open('/proc/cpuinfo', 'r') as f:
                cpuinfo = f.read()
            
            # Extract model name
            model_match = None
            for line in cpuinfo.split('\n'):
                if 'model name' in line:
                    model_match = line.split(':')[1].strip()
                    break
            
            # Get CPU count
            cpu_count = subprocess.check_output(['nproc'], text=True).strip()
            
            if model_match:
                # Clean up CPU name (remove extra spaces, "(R)", "(TM)", etc.)
                cpu_name = model_match.replace('(R)', '').replace('(TM)', '').replace('  ', ' ')
                response = f"You have a {cpu_name} with {cpu_count} cores, {self.honorific}."
            else:
                response = f"You have a {cpu_count}-core processor, {self.honorific}."
            
            return self.respond(response)
            
        except Exception as e:
            self.logger.error(f"Error getting CPU info: {e}")
            return self.respond(f"I'm having trouble retrieving CPU information, {self.honorific}.")
    
    def get_memory_info(self) -> str:
        """Get RAM information"""
        try:
            # Get memory info from /proc/meminfo
            with open('/proc/meminfo', 'r') as f:
                meminfo = f.read()
            
            # Extract total memory
            for line in meminfo.split('\n'):
                if line.startswith('MemTotal:'):
                    # Memory is in KB
                    total_kb = int(line.split()[1])
                    total_gb = total_kb / (1024 * 1024)
                    break
            
            # Get used memory
            result = subprocess.run(
                ['free', '-m'],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if len(lines) > 1:
                    parts = lines[1].split()
                    if len(parts) >= 3:
                        used_mb = int(parts[2])
                        used_gb = used_mb / 1024
                        percent_used = (used_gb / total_gb) * 100
                        
                        response = (
                            f"You have {total_gb:.1f} gigabytes of RAM, {self.honorific}. "
                            f"Currently using {used_gb:.1f} gigabytes, which is {percent_used:.0f} percent."
                        )
                    else:
                        response = f"You have {total_gb:.1f} gigabytes of RAM installed, {self.honorific}."
                else:
                    response = f"You have {total_gb:.1f} gigabytes of RAM installed, {self.honorific}."
            else:
                response = f"You have {total_gb:.1f} gigabytes of RAM installed, {self.honorific}."
            
            return self.respond(response)
            
        except Exception as e:
            self.logger.error(f"Error getting memory info: {e}")
            return self.respond(f"I'm having trouble retrieving memory information, {self.honorific}.")
    
    def get_all_drives(self) -> str:
        """List all hard drives with model and capacity"""
        try:
            # Use lsblk to get drive information
            result = subprocess.run(
                ['lsblk', '-d', '-o', 'NAME,SIZE,MODEL,TYPE', '-n'],
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                return self.respond(f"I'm having trouble listing your drives, {self.honorific}.")
            
            drives = []
            for line in result.stdout.strip().split('\n'):
                parts = line.split(None, 3)  # Split on whitespace, max 4 parts
                if len(parts) >= 3:
                    name, size, *rest = parts
                    # Only include actual disks (not partitions or loop devices)
                    if 'disk' in line and not name.startswith('loop'):
                        model = rest[0] if rest else "Unknown"
                        drives.append({
                            'name': name,
                            'size': size,
                            'model': model.strip()
                        })
            
            if not drives:
                return self.respond(f"I couldn't find any drives, {self.honorific}.")
            
            # Build conversational response
            if len(drives) == 1:
                drive = drives[0]
                response = f"You have one drive installed, {self.honorific}: a {drive['size']} {drive['model']}."
            else:
                drive_list = []
                for drive in drives:
                    drive_list.append(f"a {drive['size']} {drive['model']}")
                
                if len(drives) == 2:
                    response = f"You have two drives, {self.honorific}: {drive_list[0]} and {drive_list[1]}."
                else:
                    drive_str = ', '.join(drive_list[:-1]) + f", and {drive_list[-1]}"
                    response = f"You have {len(drives)} drives, {self.honorific}: {drive_str}."
            
            return self.respond(response)
            
        except Exception as e:
            self.logger.error(f"Error listing drives: {e}")
            return self.respond(f"I encountered an error while listing your drives, {self.honorific}.")
    
    def get_drive_at_mount(self, path: str) -> str:
        """Get information about drive mounted at specific path"""
        try:
            # Clean up path
            path = path.strip()
            if not path.startswith('/'):
                path = '/' + path
            
            # Get mount information
            result = subprocess.run(
                ['findmnt', '-n', '-o', 'SOURCE,SIZE,FSTYPE', path],
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                return self.respond(f"I don't see anything mounted at {path}, {self.honorific}.")
            
            parts = result.stdout.strip().split()
            if len(parts) >= 3:
                source, size, fstype = parts[0], parts[1], parts[2]
                
                # Get device model if it's a block device
                if source.startswith('/dev/'):
                    device = source.split('/')[2].rstrip('0123456789')  # Remove partition number
                    
                    model_result = subprocess.run(
                        ['lsblk', '-d', '-n', '-o', 'MODEL', f'/dev/{device}'],
                        capture_output=True,
                        text=True
                    )
                    
                    if model_result.returncode == 0:
                        model = model_result.stdout.strip()
                        response = f"At {path}, you have a {size} {model} formatted as {fstype}, {self.honorific}."
                    else:
                        response = f"At {path}, you have a {size} drive formatted as {fstype}, {self.honorific}."
                else:
                    response = f"At {path}, you have {source} with {size} formatted as {fstype}, {self.honorific}."
            else:
                response = f"There is something mounted at {path}, {self.honorific}, but I'm having trouble reading the details."
            
            return self.respond(response)
            
        except Exception as e:
            self.logger.error(f"Error checking mount at {path}: {e}")
            return self.respond(f"I encountered an error checking what's mounted at {path}, {self.honorific}.")
    
    def get_gpu_info(self) -> str:
        """Get GPU information"""
        try:
            # Try nvidia-smi first for NVIDIA GPUs
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0 and result.stdout.strip():
                gpu_name = result.stdout.strip()
                return self.respond(f"You have an NVIDIA {gpu_name}, {self.honorific}.")
            
            # Try lspci for other GPUs
            result = subprocess.run(
                ['lspci'],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if 'VGA' in line or 'Display' in line or '3D' in line:
                        # Extract GPU name (after the colon and device ID)
                        if ':' in line:
                            parts = line.split(':', 2)
                            if len(parts) >= 3:
                                gpu_info = parts[2].strip()
                                # Clean up common manufacturer codes
                                gpu_info = gpu_info.replace('[AMD/ATI]', 'AMD').replace('[NVIDIA]', 'NVIDIA')
                                return self.respond(f"You have a {gpu_info}, {self.honorific}.")
            
            return self.respond(f"I'm having trouble detecting your GPU, {self.honorific}.")
            
        except FileNotFoundError:
            # nvidia-smi not found, not an NVIDIA system
            return self.respond(f"I don't detect an NVIDIA GPU, {self.honorific}. Let me check for others.")
        except Exception as e:
            self.logger.error(f"Error getting GPU info: {e}")
            return self.respond(f"I encountered an error checking your GPU, {self.honorific}.")


def create_skill(config, conversation, tts, responses, llm):
    """Factory function to create skill instance"""
    return SystemInfoSkill(config, conversation, tts, responses, llm)

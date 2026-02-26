"""
Self-Awareness Layer — JARVIS knows what it can do.

Provides the LLM with a structured manifest of loaded skills, their
capabilities, and real-time system state.  Independently useful: even
without the task planner (Phase 2), this makes the LLM answer "what can
you do?" accurately and route ambiguous commands better.

Phase 1 of the Autonomous Task Planner plan.
"""

import logging
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SkillCapability:
    """One loaded skill's capabilities."""
    name: str
    description: str
    category: str
    intents: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    avg_latency_ms: float = 0.0


@dataclass
class SystemState:
    """Snapshot of JARVIS runtime state."""
    uptime_seconds: float = 0.0
    commands_processed: int = 0
    errors: int = 0
    memory_fact_count: int = 0
    context_tokens_used: int = 0
    context_token_budget: int = 0
    llm_provider: str = "unknown"
    llm_quant: str = ""
    llm_avg_latency_ms: float = 0.0
    # Hardware identity
    cpu_model: str = ""
    cpu_cores: int = 0
    ram_total_gb: float = 0.0
    gpu_model: str = ""
    gpu_vram_gb: float = 0.0
    hostname: str = ""


# ---------------------------------------------------------------------------
# Duration labels
# ---------------------------------------------------------------------------

_DURATION_LABELS = [
    (500, "instant"),
    (2000, "a moment"),
    (5000, "~5 seconds"),
    (15000, "~15 seconds"),
]


# ---------------------------------------------------------------------------
# SelfAwareness
# ---------------------------------------------------------------------------

class SelfAwareness:
    """Gives JARVIS knowledge of its own capabilities and state."""

    def __init__(self, *,
                 skill_manager,
                 metrics=None,
                 memory_manager=None,
                 context_window=None,
                 coordinator_stats: Optional[dict] = None,
                 config=None):
        self._skill_manager = skill_manager
        self._metrics = metrics
        self._memory_manager = memory_manager
        self._context_window = context_window
        self._coordinator_stats = coordinator_stats
        self._config = config
        self._init_time = time.time()

        # Cache the manifest — skills don't change at runtime
        self._cached_manifest: Optional[str] = None
        self._cached_capabilities: Optional[list[SkillCapability]] = None

        # Hardware cache — static, read once
        self._hardware_cache: Optional[dict] = None

        logger.info("SelfAwareness initialized")

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def get_capabilities(self) -> list[SkillCapability]:
        """Harvest loaded skills into structured capability objects."""
        if self._cached_capabilities is not None:
            return self._cached_capabilities

        capabilities = []
        sm = self._skill_manager

        for skill_name, skill in sm.skills.items():
            meta = sm.skill_metadata.get(skill_name)
            if not meta:
                continue

            # Collect semantic intent names (strip class prefix for readability)
            intent_names = []
            if hasattr(skill, 'semantic_intents') and skill.semantic_intents:
                for intent_id in skill.semantic_intents:
                    # intent_id looks like "ClassName_handler_name"
                    # Extract the handler name part after the first underscore
                    parts = intent_id.split('_', 1)
                    short = parts[1] if len(parts) > 1 else intent_id
                    intent_names.append(short)

            # Latency from metrics (if available)
            avg_lat = 0.0
            if self._metrics:
                avg_lat = self._metrics.get_skill_avg_latency(skill_name, hours=24)

            capabilities.append(SkillCapability(
                name=skill_name,
                description=meta.description,
                category=meta.category,
                intents=intent_names,
                keywords=meta.keywords,
                avg_latency_ms=avg_lat,
            ))

        self._cached_capabilities = capabilities
        return capabilities

    # ------------------------------------------------------------------
    # Hardware detection (cached — static values)
    # ------------------------------------------------------------------

    def _detect_hardware(self) -> dict:
        """Read hardware info once and cache it."""
        if self._hardware_cache is not None:
            return self._hardware_cache

        hw = {
            "cpu_model": "", "cpu_cores": 0,
            "ram_total_gb": 0.0,
            "gpu_model": "", "gpu_vram_gb": 0.0,
            "hostname": "",
        }

        # Hostname
        try:
            hw["hostname"] = socket.gethostname()
        except Exception:
            pass

        # CPU model from /proc/cpuinfo
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        cpu = line.split(":", 1)[1].strip()
                        # Strip redundant suffix like "12-Core Processor"
                        cpu = re.sub(r'\s+\d+-Core Processor$', '', cpu)
                        hw["cpu_model"] = cpu
                        break
            hw["cpu_cores"] = os.cpu_count() or 0
        except Exception:
            pass

        # RAM from /proc/meminfo
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        hw["ram_total_gb"] = round(kb / 1048576, 1)
                        break
        except Exception:
            pass

        # GPU model + VRAM from rocm-smi (AMD ROCm)
        try:
            result = subprocess.run(
                ["rocm-smi", "--showproductname"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "Card Series" in line or "Card series" in line:
                        hw["gpu_model"] = line.split(":")[-1].strip()
                        break

            # VRAM
            result = subprocess.run(
                ["rocm-smi", "--showmeminfo", "vram"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "Total" in line and "Memory" in line:
                        # Parse "VRAM Total Memory (B): 21474836480"
                        parts = line.split(":")
                        if len(parts) >= 2:
                            try:
                                vram_bytes = int(parts[-1].strip())
                                hw["gpu_vram_gb"] = round(vram_bytes / (1024**3), 1)
                            except ValueError:
                                pass
                        break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # No rocm-smi — try lspci fallback for GPU name
            try:
                result = subprocess.run(
                    ["lspci"], capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if "VGA" in line or "Display" in line or "3D" in line:
                            hw["gpu_model"] = line.split(":", 2)[-1].strip()
                            break
            except Exception:
                pass

        self._hardware_cache = hw
        logger.info(f"Hardware detected: CPU={hw['cpu_model']}, "
                     f"RAM={hw['ram_total_gb']}GB, GPU={hw['gpu_model']}")
        return hw

    # ------------------------------------------------------------------
    # System state
    # ------------------------------------------------------------------

    def get_system_state(self) -> SystemState:
        """Snapshot of current runtime state (not cached — always fresh)."""
        state = SystemState()
        state.uptime_seconds = time.time() - self._init_time

        # Coordinator stats
        if self._coordinator_stats:
            state.commands_processed = self._coordinator_stats.get('commands_processed', 0)
            state.errors = self._coordinator_stats.get('errors', 0)

        # Memory fact count
        if self._memory_manager:
            try:
                counts = self._memory_manager.get_fact_count()
                state.memory_fact_count = sum(counts.values())
            except Exception:
                pass

        # Context window
        if self._context_window and self._context_window.enabled:
            state.context_token_budget = self._context_window.token_budget
            # Estimate tokens used from active segments
            try:
                segments = self._context_window._segments
                total_tokens = sum(
                    s.get('token_count', 0) if isinstance(s, dict)
                    else getattr(s, 'token_count', 0)
                    for s in segments
                )
                state.context_tokens_used = total_tokens
            except Exception:
                pass

        # LLM provider + avg latency from metrics
        if self._config:
            model_path = self._config.get("llm.local.model_path", "")
            if model_path:
                # Extract model name and quant suffix separately
                # "/path/to/Qwen3.5-35B-A3B-Q3_K_M.gguf" → name="Qwen3.5-35B-A3B", quant="Q3_K_M"
                name = os.path.splitext(os.path.basename(model_path))[0]
                quant_match = re.search(r'-([QIF]\d[^\-]*)$', name)
                state.llm_provider = re.sub(r'-[QIF]\d[^\-]*$', '', name)
                if quant_match:
                    state.llm_quant = quant_match.group(1)
            else:
                state.llm_provider = "unknown"
        if self._metrics:
            summary = self._metrics.get_summary(hours=24)
            state.llm_avg_latency_ms = summary.get("avg_latency_ms", 0.0)

        # Hardware identity (cached — read once)
        hw = self._detect_hardware()
        state.cpu_model = hw["cpu_model"]
        state.cpu_cores = hw["cpu_cores"]
        state.ram_total_gb = hw["ram_total_gb"]
        state.gpu_model = hw["gpu_model"]
        state.gpu_vram_gb = hw["gpu_vram_gb"]
        state.hostname = hw["hostname"]

        return state

    # ------------------------------------------------------------------
    # LLM-readable manifest (~800 tokens)
    # ------------------------------------------------------------------

    def get_capability_manifest(self) -> str:
        """Build a concise, numbered skill list for the LLM system prompt.

        Cached after first call (skills don't change at runtime).
        """
        if self._cached_manifest is not None:
            return self._cached_manifest

        capabilities = self.get_capabilities()
        if not capabilities:
            return ""

        lines = ["YOUR CAPABILITIES:"]
        for i, cap in enumerate(capabilities, 1):
            # Format: "1. weather: Current conditions, forecasts (~0.5s)"
            desc = cap.description or "No description"
            # Truncate long descriptions
            if len(desc) > 80:
                desc = desc[:77] + "..."

            latency = self.estimate_duration(cap.name)
            intent_summary = ", ".join(cap.intents[:5])
            if intent_summary:
                lines.append(f"{i}. {cap.name}: {desc} [{intent_summary}] ({latency})")
            else:
                lines.append(f"{i}. {cap.name}: {desc} ({latency})")

        # Add non-skill capabilities the LLM should know about
        lines.append(f"{len(capabilities) + 1}. web_research: Search the web, fetch pages, synthesize answers (a moment)")
        lines.append(f"{len(capabilities) + 2}. general_knowledge: Answer questions from training data (instant)")

        self._cached_manifest = "\n".join(lines)
        logger.info(f"Capability manifest built: {len(capabilities)} skills, "
                     f"{len(self._cached_manifest)} chars")
        return self._cached_manifest

    # ------------------------------------------------------------------
    # Compact state one-liner
    # ------------------------------------------------------------------

    def get_compact_state(self) -> str:
        """One-line state summary for system prompt injection."""
        state = self.get_system_state()
        parts = []
        if state.llm_provider and state.llm_provider != "unknown":
            llm_label = f"LLM: {state.llm_provider}"
            if state.llm_quant:
                llm_label += f" ({state.llm_quant} quant)"
            parts.append(llm_label)
        # Hardware identity
        if state.cpu_model:
            parts.append(f"CPU: {state.cpu_model} ({state.cpu_cores} cores)")
        if state.ram_total_gb:
            parts.append(f"{state.ram_total_gb:.0f}GB RAM")
        if state.gpu_model:
            gpu_label = f"GPU: {state.gpu_model}"
            if state.gpu_vram_gb:
                gpu_label += f" ({state.gpu_vram_gb:.0f}GB VRAM)"
            parts.append(gpu_label)
        if state.memory_fact_count:
            parts.append(f"{state.memory_fact_count} remembered facts about the user")
        if state.context_token_budget:
            parts.append(f"{state.context_tokens_used}/{state.context_token_budget} ctx tokens")
        if state.llm_avg_latency_ms:
            parts.append(f"avg {state.llm_avg_latency_ms:.0f}ms latency")
        if state.commands_processed:
            parts.append(f"{state.commands_processed} commands")
        return f"State: {' | '.join(parts)}" if parts else ""

    # ------------------------------------------------------------------
    # Duration estimation
    # ------------------------------------------------------------------

    def estimate_duration(self, skill_name: str) -> str:
        """Human-friendly duration estimate based on metrics data."""
        if not self._metrics:
            return "unknown"
        avg = self._metrics.get_skill_avg_latency(skill_name, hours=24)
        if avg <= 0:
            return "instant"
        for threshold_ms, label in _DURATION_LABELS:
            if avg <= threshold_ms:
                return label
        return f"~{int(avg / 1000)}s"

    # ------------------------------------------------------------------
    # Error-aware planning
    # ------------------------------------------------------------------

    def get_unreliable_skills(self, threshold: float = 0.3, hours: int = 24) -> list[str]:
        """Return skill names with error rates above threshold.

        Used to warn the LLM during plan generation so it can avoid
        or warn about unreliable skills.
        """
        if not self._metrics:
            return []
        unreliable = []
        for cap in self.get_capabilities():
            rate = self._metrics.get_skill_error_rate(cap.name, hours=hours)
            if rate >= threshold:
                unreliable.append(f"{cap.name} ({int(rate * 100)}% error rate)")
        return unreliable

    # ------------------------------------------------------------------
    # Plan duration estimation
    # ------------------------------------------------------------------

    def estimate_plan_duration(self, plan) -> str:
        """Human-friendly total duration estimate for a multi-step plan.

        Sums per-skill average latencies from MetricsTracker.
        Returns empty string if no metrics available.
        """
        if not self._metrics:
            return ""
        total_ms = 0.0
        for step in plan.steps:
            avg = self._metrics.get_skill_avg_latency(step.skill_name, hours=24)
            if avg > 0:
                total_ms += avg
            else:
                total_ms += 1000  # default 1s for unknown skills
        if total_ms < 3000:
            return "a few seconds"
        elif total_ms < 60000:
            return f"about {int(total_ms / 1000)} seconds"
        else:
            mins = int(total_ms / 60000)
            return f"about {mins} minute{'s' if mins > 1 else ''}"

    # ------------------------------------------------------------------
    # Cache invalidation (for future hot-reload scenarios)
    # ------------------------------------------------------------------

    def invalidate_cache(self):
        """Clear cached manifest and capabilities (call after skill reload)."""
        self._cached_manifest = None
        self._cached_capabilities = None

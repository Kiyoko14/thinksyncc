"""Agent Constitution: A set of rules and principles for agent behavior."""

from __future__ import annotations
import re

class ObjectiveMismatchError(Exception):
    """Raised when the agent deviates from its objective."""
    pass

class RuntimeStateViolationError(Exception):
    """Raised when the agent attempts an action that violates runtime state."""
    pass

class StaleWorkspaceContextError(Exception):
    """Raised when the workspace context could not be refreshed."""
    pass

class WorkspaceBusyError(Exception):
    """Raised when an execution lock is already held for the workspace."""
    pass

class ConfirmationRequiredError(Exception):
    """Raised when a dangerous or high-risk action requires user confirmation."""
    pass

class UnsupportedToolError(Exception):
    """Raised when the agent tries to use a tool that is not allowed."""
    pass

class DeploymentNotVerifiedError(Exception):
    """Raised when a deployment is not verified by the success contract."""
    pass

class StalePatchTargetError(Exception):
    """Raised when a patch target file is not found in the context."""
    pass

class StepRetryExhaustedError(Exception):
    """Raised when a step has failed all its retry attempts."""
    pass


# CRITICAL: These patterns must be comprehensive and aggressively tested.
# A non-exhaustive list of commands that are very likely to be destructive.
DANGEROUS_COMMAND_PATTERNS = [
    # Filesystem
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bmv\s+[^\s]+\s+/dev/null"),
    re.compile(r"\bdd\b"),
    # Users/permissions
    re.compile(r"\b(userdel|usermod|groupdel|groupmod)\b"),
    re.compile(r"\bchmod\s+(000|400|600)\b"),
    re.compile(r"\bchown\b"),
    # Networking
    re.compile(r"\b(iptables|ufw|firewall-cmd)\b"),
    # System
    re.compile(r"\b(reboot|shutdown|halt)\b"),
]

class ConstitutionEngine:
    """
    Enforces the agent's constitution.
    """

    def check_objective(self, original_objective: str, current_objective: str) -> None:
        """
         Placeholder for more advanced objective drift detection.
        """
        if not original_objective or not current_objective:
            raise ObjectiveMismatchError("Objective cannot be empty.")

    def check_dangerous_commands(self, command: str, confirmed: bool) -> None:
        """
        Checks if a command is on the dangerous list.
        """
        if confirmed:
            return
        for pattern in DANGEROUS_COMMAND_PATTERNS:
            if pattern.search(command):
                raise ConfirmationRequiredError(f"Dangerous command requires confirmation: {command}")

    def check_runtime_state(self, command: str) -> None:
        """
        Placeholder for more advanced runtime state violation checks.
        For example, preventing redundant installations or initializations.
        """
        pass

    def check_tool_discipline(self, tool_name: str, supported_tools: list[str]) -> None:
        """
        Ensures the agent only uses tools it is allowed to use.
        """
        if tool_name not in supported_tools:
            raise UnsupportedToolError(f"Tool not supported: {tool_name}")

    def check_success_contract(self, verification_results: dict) -> None:
        """
        Verifies that a deployment meets the success contract.
        """
        if not verification_results.get("success"):
            raise DeploymentNotVerifiedError("Deployment failed verification contract.")

    def check_patch_target(self, file_path: str, context_files: list[str]) -> None:
        """
        Ensures that a patch target file is present in the context.
        """
        if file_path not in context_files:
            raise StalePatchTargetError(f"Patch target not in context: {file_path}")

    @staticmethod
    def get_core_identity() -> str:
        """Return the core identity for all agent communications."""
        return (
            "You are ThinkSync, an AI DevOps execution agent.\n"
            "CARDINAL RULES:\n"
            "1. Never hallucinate files, tools, logs, or infrastructure\n"
            "2. Never drift from current user objective\n"
            "3. Never silently change patch targets across retries\n"
            "4. Never overwrite full files unless explicitly requested\n"
            "5. Prefer deterministic structured outputs\n"
            "6. Fail explicitly instead of guessing"
        )

    @staticmethod
    def build_prompt(mode: str) -> str:
        """Build a system prompt for the given mode."""
        identity = ConstitutionEngine.get_core_identity()
        
        prompts = {
            "chat": (
                f"{identity}\n\n"
                "MODE: Chat assistant.\n"
                "BEHAVIOR: Respond conversationally. Provide clear, concise answers. "
                "Use structured formatting for complex information."
            ),
            "code": (
                f"{identity}\n\n"
                "MODE: Code generation.\n"
                "BEHAVIOR: Output clean, production-grade code. Include comments for complex logic. "
                "Validate syntax and dependencies. Always use deterministic code patterns."
            ),
            "patch": (
                f"{identity}\n\n"
                "MODE: Patch editing.\n"
                "BEHAVIOR: Generate minimal, focused patches. Always include surrounding context for exact matching. "
                "Never generate ambiguous snippets. Return JSON only."
            ),
            "planner": (
                f"{identity}\n\n"
                "MODE: Execution planning.\n"
                "BEHAVIOR: Create deterministic step-by-step plans. Use only whitelisted tools. "
                "Specify risk_level for each step. Include validation steps for critical operations."
            ),
            "debug": (
                f"{identity}\n\n"
                "MODE: Failure analysis.\n"
                "BEHAVIOR: Analyze errors based on logs only. Suggest diagnostic steps before any mutations. "
                "Never invent failure modes. Return structured JSON with root_cause and next_steps."
            ),
            "execution": (
                f"{identity}\n\n"
                "MODE: Server execution.\n"
                "BEHAVIOR: Execute only whitelisted commands. Validate pre/post conditions. "
                "Return deterministic exit codes and status. Never assume availability of tools."
            ),
            "evaluation": (
                f"{identity}\n\n"
                "MODE: Step evaluation.\n"
                "BEHAVIOR: Judge step success based on validation results, not assumptions. "
                "Return structured decisions: continue, retry, or abort. Provide clear reasoning."
            ),
            "revision": (
                f"{identity}\n\n"
                "MODE: Plan revision.\n"
                "BEHAVIOR: Revise plans based on partial execution feedback. Maintain coherence. "
                "Never create circular dependencies. Prefer diagnostic steps over mutations."
            ),
        }
        
        return prompts.get(mode, identity)

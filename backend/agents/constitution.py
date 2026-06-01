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
    re.compile(r"\bfind\b.*\b-delete\b"),
    re.compile(r"\bmkfs\.\b"),
    re.compile(r"\b>\s*/dev/sd[a-z]\b"),
    # Users/permissions
    re.compile(r"\b(userdel|usermod|groupdel|groupmod)\b"),
    re.compile(r"\bchmod\s+(000|400|600)\b"),
    re.compile(r"\bchown\b"),
    # Networking
    re.compile(r"\b(iptables|ufw|firewall-cmd)\b"),
    # System
    re.compile(r"\b(reboot|shutdown|halt)\b"),
    # Process
    re.compile(r"\bkill\s+-9\b"),
    # Docker
    re.compile(r"\bdocker\s+system\s+prune\b"),
    # Pipe-to-shell
    re.compile(r"\b(curl|wget)\b.*\|\s*(sh|bash|zsh)"),
    # Sudo with destructive
    re.compile(r"\bsudo\b.*\b(rm|dd|mkfs|halt|reboot|shutdown)\b"),
]

_STOP_WORDS = {
    "the", "a", "an", "to", "my", "your", "this", "that", "is", "are", "and", "or",
    "in", "on", "at", "for", "with", "of", "from", "it", "i", "you", "we", "our", "do",
    "have", "be", "will", "would", "can", "should", "may", "must", "need", "want",
    "like", "get", "make", "use", "go", "come", "see", "know", "think", "say", "give",
    "find", "work", "try", "look", "put", "set", "keep", "help", "show", "run", "move",
    "open", "add", "as", "if", "so", "then", "than", "too", "very", "just", "now",
    "only", "also", "even", "how", "all", "any", "both", "each", "more", "most", "some",
    "not", "no", "up", "down", "out", "over", "under", "before", "after", "about",
    "into", "through", "during", "above", "below", "between", "against", "among",
    "within", "without", "near", "off", "again", "here", "there", "when", "where",
    "why", "what", "which", "who", "once", "never", "always", "often", "sometimes",
    "usually", "generally", "finally", "actually", "probably", "perhaps", "maybe",
    "really", "still", "already", "yet", "almost", "quite", "rather", "simply",
    "completely", "totally", "entirely", "fully", "partly", "mainly", "mostly",
    "especially", "particularly", "specifically", "certainly", "definitely", "possibly",
    "likely", "surely", "clearly", "obviously", "apparently", "presumably",
    "today", "tomorrow", "yesterday", "soon", "later", "now", "then", "home", "away",
    "everywhere", "anywhere", "somewhere", "anyhow", "anyway", "somehow", "else",
    "otherwise", "besides", "however", "moreover", "furthermore", "nevertheless",
    "nonetheless", "therefore", "thus", "hence", "consequently", "accordingly",
    "meanwhile", "instead", "similarly", "likewise", "though", "although", "while",
    "whereas", "unless", "since", "because", "whether", "whatever", "whoever",
    "whichever", "whenever", "wherever", "more", "less", "few", "many", "much",
    "most", "least", "fewer", "fewest", "none", "several", "various", "certain",
    "particular", "specific", "general", "usual", "normal", "common", "whole",
    "entire", "complete", "full", "half", "double", "single", "first", "last",
    "next", "previous", "early", "late", "old", "new", "young", "good", "bad",
    "better", "best", "worse", "worst", "high", "low", "higher", "lower", "big",
    "small", "large", "little", "long", "short", "great", "first", "second",
    "third", "final", "initial", "original", "current", "following", "subsequent",
    "prior", "former", "latter", "same", "different", "similar", "equal", "such",
    "other", "another", "every", "either", "neither", "one", "two", "three", "four",
    "five", "yes", "true", "false", "ok", "okay", "right", "wrong", "correct",
    "incorrect", "dont", "doesnt", "didnt", "wasnt", "werent", "hasnt", "havent",
    "hadnt", "isnt", "arent", "wont", "wouldnt", "couldnt", "shouldnt", "cant",
    "cannot", "did", "done", "doing", "having", "had", "been", "being", "was",
    "were", "am", "gets", "getting", "got", "gotten", "makes", "made", "making",
    "takes", "took", "taken", "taking", "comes", "came", "coming", "goes", "went",
    "gone", "going", "saw", "seen", "seeing", "knew", "known", "knowing", "thought",
    "thinking", "said", "saying", "told", "telling", "asked", "asking", "gave",
    "given", "giving", "found", "finding", "worked", "working", "called",
    "calling", "tried", "trying", "needed", "needing", "felt", "feeling", "became",
    "becoming", "left", "leaving", "puts", "putting", "meant", "meaning", "kept",
    "keeping", "lets", "letting", "began", "begun", "beginning", "seemed",
    "seeming", "helped", "helping", "showed", "shown", "showing", "heard",
    "hearing", "played", "playing", "ran", "run", "running", "moved", "moving",
    "lived", "living", "believed", "believing", "brought", "bringing", "happened",
    "happening", "stood", "standing", "lost", "losing", "paid", "paying", "met",
    "meeting", "included", "including", "continued", "continuing", "sets", "setting",
    "learned", "learning", "changed", "changing", "led", "leading", "understood",
    "understanding", "watched", "watching", "followed", "following", "stopped",
    "stopping", "created", "creating", "spoke", "spoken", "speaking", "read",
    "reading", "allowed", "allowing", "spent", "spending", "grew", "grown",
    "growing", "walked", "walking", "offered", "offering", "remembered",
    "remembering", "loved", "loving", "considered", "considering", "appeared",
    "appearing", "bought", "buying", "waited", "waiting", "served", "serving",
    "died", "dying", "sent", "sending", "expected", "expecting", "built",
    "building", "stayed", "staying", "fell", "fallen", "falling", "cut",
    "cutting", "reached", "reaching", "killed", "killing", "remained", "remaining",
    "suggested", "suggesting", "raised", "raising", "passed", "passing", "sold",
    "selling", "required", "requiring", "reported", "reporting", "decided",
    "deciding", "pulled", "pulling",
}

class ConstitutionEngine:
    """
    Enforces the agent's constitution.
    """

    def check_objective(self, original_objective: str, current_objective: str) -> None:
        """
        Detects objective drift between original and current objectives.

        Raises ObjectiveMismatchError if:
          - Either objective is empty
          - The objectives are fundamentally different (semantic drift)
        """
        if not original_objective or not current_objective:
            raise ObjectiveMismatchError("Objective cannot be empty.")
        # Semantic drift detection: if keywords diverge significantly, flag it
        original_keywords = set(original_objective.lower().split())
        current_keywords = set(current_objective.lower().split())
        overlap = original_keywords & current_keywords - _STOP_WORDS
        if not overlap:
            raise ObjectiveMismatchError("Objectives have no common keywords.")

    def check_dangerous_commands(self, command: str, confirmation: bool = False) -> None:
        """
        Checks if a command is on the dangerous list.
        """
        if confirmation:
            return
        for pattern in DANGEROUS_COMMAND_PATTERNS:
            if pattern.search(command):
                raise ConfirmationRequiredError(f"Dangerous command requires confirmation: {command}")

    def check_runtime_state(self, command: str) -> None:
        """
        Checks for runtime state violations that indicate fake or unverifiable assumptions.

        Violations:
          - localhost/127.0.0.1 URLs in verification commands (not externally verifiable)
          - pipe-to-shell patterns (curl | sh, wget | bash)
          - sudo combined with destructive commands
        """
        if not command:
            return
        # Localhost is not externally verifiable
        if re.search(r"https?://(localhost|127\.0\.0\.1)", command):
            raise RuntimeStateViolationError("Localhost URLs are not externally verifiable.")
        # Pipe-to-shell is dangerous
        if re.search(r"\b(curl|wget)\b.*\|\s*(sh|bash|zsh)", command):
            raise ConfirmationRequiredError("Pipe-to-shell commands require confirmation.")
        # Sudo with destructive commands
        if re.search(r"\bsudo\b.*\b(rm|dd|mkfs|halt|reboot|shutdown)\b", command):
            raise ConfirmationRequiredError("Sudo with destructive commands requires confirmation.")

    def check_tool_discipline(self, tool_name: str, supported_tools: list[str]) -> None:
        """
        Ensures the agent only uses tools it is allowed to use.
        """
        if tool_name not in supported_tools:
            raise UnsupportedToolError(f"Tool not supported: {tool_name}")

    def check_success_contract(self, verification_results: dict) -> None:
        """
        Verifies that a deployment meets the success contract with evidence.

        Requires independent verification — LLM self-assessment alone is insufficient.
        verification_results must contain:
          - "success": bool
          - "verification_method": one of "http_probe", "process_check", "file_check", "exit_code"
          - "verification_evidence": raw probe output (not LLM-generated)
        """
        if not verification_results.get("success"):
            raise DeploymentNotVerifiedError("Deployment failed verification contract.")

        method = verification_results.get("verification_method")
        if not method or method == "llm_assessment":
            raise DeploymentNotVerifiedError(
                "Success claims require independent verification (http_probe, process_check, file_check, or exit_code). "
                "LLM self-assessment is not sufficient evidence."
            )
        evidence = verification_results.get("verification_evidence")
        if not evidence:
            raise DeploymentNotVerifiedError("Success claim requires verification evidence (raw probe output).")

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
            "6. Fail explicitly instead of guessing\n"
            "7. Never claim success without independent verification\n"
            "8. Always validate before reporting completion\n"
            "9. Prefer patch edits over full rewrites\n"
            "10. Report uncertainty explicitly — 'I don't know' is better than a guess"
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

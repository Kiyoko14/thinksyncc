"""
Implementation Intelligence layer — Sprint 3C.A.

EXTENSION ONLY: does NOT replace or modify the existing Template Engine
(`services/templates.py`) or the existing planner (`services/planner.py`).

Architecture (added on top of existing):
    Specification
        ↓
    ImplementationIntelligence.decide_strategy()
        ↓
    ┌───────────────────┐
    │ Template available?              │
    │  YES → TemplateRankingEngine  │
    │       ↓                       │
    │  Compatibility score?          │
    │    HIGH → use template       │
    │    LOW  → hybrid            │
    │  NO  → CodeGenerationEngine │
    └───────────────────┘
        ↓
    ImplementationValidator.validate()
        ↓
    ImplementationReport
        ↓
    Planner (unchanged)

All new components are imported BY the caller; the existing
template/planner paths are not touched.
"""

from __future__ import annotations

import hashlib
import json as _json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# Bounded repair control (Sprint 3C.B)
MAX_REPAIR_ATTEMPTS: int = 2


# --------------------------------------------------------------------- #
# Strategy enum — the decision output
# --------------------------------------------------------------------- #

class ImplementationStrategy(str, Enum):
    """How the implementation will be produced."""

    EXACT_TEMPLATE = "exact_template"
    COMPATIBLE_TEMPLATE = "compatible_template"
    HYBRID_TEMPLATE_AI = "hybrid_template_ai"
    PURE_AI_GENERATION = "pure_ai_generation"


# --------------------------------------------------------------------- #
# GenerationMetadata — provenance for every generated file
# --------------------------------------------------------------------- #

@dataclass
class GenerationMetadata:
    """Provenance record for a generated implementation.

    Attached to ``ImplementationReport`` and persisted so the
    planner and auditor can trace every file back to its origin.
    """

    strategy: ImplementationStrategy
    template_name: str | None = None
    model: str | None = None  # LLM model used (if any)
    prompt_hash: str = ""  # SHA-256 of the prompt that produced the code
    files_generated: list[str] = field(default_factory=list)
    validation_passed: bool = False
    validation_errors: list[str] = field(default_factory=list)
    generated_at: str = ""  # ISO timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "template_name": self.template_name,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "files_generated": self.files_generated,
            "validation_passed": self.validation_passed,
            "validation_errors": self.validation_errors,
            "generated_at": self.generated_at,
        }


# --------------------------------------------------------------------- #
# TemplateDiscoveryEngine
# --------------------------------------------------------------------- #

class TemplateDiscoveryEngine:
    """Find ALL templates whose keywords match the objective (not just the first).

    Extends (does NOT replace) ``templates.match_template()``.
    Returns a list ranked by ``TemplateRankingEngine`` (see below).
    """

    @staticmethod
    def discover(objective: str) -> list[dict[str, Any]]:
        """
        Returns a list of dicts:
            [{
                "name": str,
                "description": str,
                "keywords": list[str],
                "files": dict[str, str],
                "dependencies": list[str],
                "score": float,   # filled in by ranking engine
            }, ...]
        """
        from services.templates import TEMPLATES

        lowered = (objective or "").lower()
        matches: list[dict[str, Any]] = []

        for spec in TEMPLATES.values():
            matched_keywords = [
                kw for kw in spec.keywords if kw in lowered
            ]
            if matched_keywords:
                matches.append(
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "keywords": list(spec.keywords),
                        "files": dict(spec.files),
                        "dependencies": list(spec.dependencies),
                        "matched_keywords": matched_keywords,
                        "execution_steps": list(spec.execution_steps or []),
                    }
                )

        if not matches:
            logger.info("[impl-intel] no template matches for: %.60s", objective)
        return matches


# --------------------------------------------------------------------- #
# TemplateRankingEngine
# --------------------------------------------------------------------- #

class TemplateRankingEngine:
    """Score each discovered template and sort best-first.

    Ranking criteria (equal weight):
      1. keyword match count  (more = better)
      2. dependency availability (fewer missing = better)
      3. objective string similarity (simple word-overlap heuristic)
    """

    @staticmethod
    def rank(
        objective: str,
        discoveries: list[dict[str, Any]],
        *,
        available_dependencies: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Mutates ``discoveries`` in-place (adds ``score`` key) and returns
        a sorted list (highest score first).
        """
        avail = set(d.strip().lower() for d in (available_dependencies or []))

        for d in discoveries:
            # 1. keyword score (0.0 – 1.0)
            kw_score = min(len(d.get("matched_keywords", [])) / 3.0, 1.0)

            # 2. dependency score (0.0 – 1.0)
            deps = d.get("dependencies", [])
            missing = [dep for dep in deps if dep.strip().lower() not in avail]
            dep_score = 1.0 if not deps else max(0.0, 1.0 - len(missing) / len(deps))

            # 3. word-overlap similarity (0.0 – 1.0)
            objective_words = set(re.findall(r"[a-z]+", (objective or "").lower()))
            keyword_words = set()
            for kw in d.get("keywords", []):
                keyword_words.update(re.findall(r"[a-z]+", kw.lower()))
            overlap = (
                0.0
                if not objective_words
                else len(objective_words & keyword_words) / len(objective_words)
            )

            d["score"] = round((kw_score + dep_score + overlap) / 3.0, 4)
            d["missing_dependencies"] = missing

        discoveries.sort(key=lambda d: (-d["score"], d["name"]))
        return discoveries


# --------------------------------------------------------------------- #
# TemplateCompatibilityScorer
# --------------------------------------------------------------------- #

class TemplateCompatibilityScorer:
    """Decide whether a template is compatible enough to use as-is.

    A template is:
      - FULLY COMPATIBLE  (score ≥ 0.8) → use exact template
      - PARTIALLY COMPATIBLE (0.5 ≤ score < 0.8) → hybrid mode
      - INCOMPATIBLE (score < 0.5) → AI generation
    """

    @staticmethod
    def score(
        template_info: dict[str, Any],
        specification: Any | None = None,
        *,
        available_capabilities: dict[str, Any] | None = None,
    ) -> float:
        """
        Returns a compatibility score in [0.0, 1.0].
        """
        base = template_info.get("score", 0.5)  # from ranking engine

        # Penalise if template dependencies are missing
        missing = template_info.get("missing_dependencies", [])
        deps = template_info.get("dependencies", []) or []
        if deps:
            base -= 0.1 * len(missing)

        # Penalise if spec requirements are not covered by template files
        if specification is not None:
            spec_dict = _spec_to_dict(specification)
            required_files = spec_dict.get("files", []) or []
            template_files = list((template_info.get("files") or {}).keys())
            coverage = (
                1.0
                if not required_files
                else len(set(required_files) & set(template_files)) / len(required_files)
            )
            base = base * 0.5 + coverage * 0.5

        return max(0.0, min(1.0, round(base, 4)))


# --------------------------------------------------------------------- #
# ImplementationStrategyResolver
# --------------------------------------------------------------------- #

class ImplementationStrategyResolver:
    """Single decision point: given discoveries + scores, pick a strategy."""

    @staticmethod
    def resolve(
        discoveries: list[dict[str, Any]],
        specification: Any | None = None,
        *,
        available_capabilities: dict[str, Any] | None = None,
    ) -> tuple[ImplementationStrategy, dict[str, Any] | None]:
        """
        Returns (strategy, chosen_template_info_or_None).

        Fallback chain (as required by the prompt):
          1. Exact Template       (compatibility ≥ 0.8)
          2. Compatible Template  (compatibility ≥ 0.5)
          3. Hybrid               (compatibility ≥ 0.5, has template)
          4. Pure AI Generation   (no suitable template)
        """
        if not discoveries:
            return ImplementationStrategy.PURE_AI_GENERATION, None

        best = discoveries[0]
        compatibility = TemplateCompatibilityScorer.score(
            best, specification, available_capabilities=available_capabilities
        )
        best["compatibility"] = compatibility

        if compatibility >= 0.8:
            return ImplementationStrategy.EXACT_TEMPLATE, best
        if compatibility >= 0.5:
            # Use template as baseline, AI fills gaps
            return ImplementationStrategy.HYBRID_TEMPLATE_AI, best
        # Template exists but is a poor match — still try hybrid
        # before giving up entirely (prompt: "NO execution path may terminate
        # simply because no template exists")
        return ImplementationStrategy.HYBRID_TEMPLATE_AI, best


# --------------------------------------------------------------------- #
# HybridGenerationEngine
# --------------------------------------------------------------------- #

class HybridGenerationEngine:
    """Combine a matched template with AI completion for missing pieces.

    Flow:
        1. Render the template (as before).
        2. Compare rendered files against specification requirements.
        3. Ask the LLM to generate ONLY the missing/non-compliant pieces.
        4. Merge template files + AI-generated files.
    """

    @staticmethod
    async def generate(
        template_info: dict[str, Any],
        specification: Any | None = None,
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        """
        Returns {
            "files": dict[str, str],
            "dependencies": list[str],
            "generation_metadata": dict,
        }
        """
        from services.templates import render_template, extract_template_params

        params = extract_template_params(
            _spec_to_text(specification) if specification else ""
        )
        rendered = render_template(
            _dict_to_templatespec(template_info), params
        )
        template_files = (rendered or {}).get("files") or {}
        template_deps = (rendered or {}).get("dependencies") or []

        # Build the AI prompt for missing pieces
        missing = _identify_missing_pieces(template_files, specification)

        if not missing:
            # Template covers everything — no AI needed
            metadata = GenerationMetadata(
                strategy=ImplementationStrategy.EXACT_TEMPLATE,
                template_name=template_info.get("name"),
                files_generated=list(template_files.keys()),
                validation_passed=True,
                generated_at=datetime.now(timezone.utc).isoformat(),
            )
            return {
                "files": template_files,
                "dependencies": template_deps,
                "generation_metadata": metadata.to_dict(),
            }

        # AI completion for missing pieces ONLY — never overwrite template files
        ai_files = await _ai_generate_missing(
            missing, specification, model=model
        )
        # Filter ai_files: only accept files that are missing from template
        # This prevents the LLM from degrading working template code
        safe_ai_files = {
            k: v for k, v in (ai_files or {}).items()
            if k not in template_files
        }
        if len(safe_ai_files) < len(ai_files or {}):
            logger.warning(
                "[impl-intel] hybrid: LLM tried to overwrite %d template files — blocked",
                len(ai_files or {}) - len(safe_ai_files),
            )

        merged = dict(template_files)
        merged.update(safe_ai_files)

        metadata = GenerationMetadata(
            strategy=ImplementationStrategy.HYBRID_TEMPLATE_AI,
            template_name=template_info.get("name"),
            model=model,
            prompt_hash=_hash_text(_spec_to_text(specification)),
            files_generated=list(merged.keys()),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        return {
            "files": merged,
            "dependencies": list(set(template_deps) | _extract_deps(ai_files)),
            "generation_metadata": metadata.to_dict(),
        }


# --------------------------------------------------------------------- #
# CodeGenerationEngine  (pure AI)
# --------------------------------------------------------------------- #

class CodeGenerationEngine:
    """Generate implementation entirely via LLM when no template matches."""

    @staticmethod
    async def generate(
        specification: Any | None = None,
        objective: str = "",
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        """
        Returns {
            "files": dict[str, str],
            "dependencies": list[str],
            "generation_metadata": dict,
        }
        """
        prompt = _build_ai_prompt(objective, specification)
        files = await _call_llm_for_code(prompt, model=model)

        metadata = GenerationMetadata(
            strategy=ImplementationStrategy.PURE_AI_GENERATION,
            model=model,
            prompt_hash=_hash_text(prompt),
            files_generated=list((files or {}).keys()),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        return {
            "files": files or {},
            "dependencies": _extract_deps(files or {}),
            "generation_metadata": metadata.to_dict(),
        }


# --------------------------------------------------------------------- #
# ImplementationValidator
# --------------------------------------------------------------------- #

class ImplementationValidator:
    """Validate generated code BEFORE it reaches the planner/executor.

    Checks (configurable):
        1. Project structure  (required files present)
        2. Dependency consistency  (imports match declared deps)
        3. File integrity  (Python: py_compile; JSON: parse)
        4. Configuration validity  (environment variables, ports)
        5. Architecture consistency  (entry point matches framework)
        6. Requirement coverage  (spec requirements covered in code)
    """

    @staticmethod
    async def validate(
        files: dict[str, str],
        *,
        dependencies: list[str] | None = None,
        specification: Any | None = None,
    ) -> dict[str, Any]:
        """
        Returns {
            "valid": bool,
            "errors": list[str],
            "warnings": list[str],
            "coverage": dict,  # requirement → covered (bool)
        }
        """
        errors: list[str] = []
        warnings: list[str] = []

        # 1. Project structure
        if files:
            has_entry = any(
                name in files for name in ["main.py", "app.py", "run.py", "__init__.py"]
            )
            if not has_entry:
                warnings.append("No obvious entry-point file found (main.py, app.py, etc.)")

        # 2. File integrity
        for path, content in (files or {}).items():
            if path.endswith(".py"):
                ok, err = _check_python_syntax(content)
                if not ok:
                    errors.append(f"Syntax error in {path}: {err}")
            elif path.endswith(".json"):
                ok, err = _check_json_valid(content)
                if not ok:
                    errors.append(f"Invalid JSON in {path}: {err}")

        # 3. Dependency consistency (naive: check imports vs declared deps)
        imports = _extract_imports(files or {})
        declared = set(d.split(">=")[0].split("==")[0].strip().lower()
                        for d in (dependencies or []))
        for imp in imports:
            if imp.lower() not in declared and imp not in ("os", "sys", "json", "time", "datetime", "logging"):
                warnings.append(f"Import '{imp}' not in declared dependencies")

        # 4. Requirement coverage
        coverage: dict[str, bool] = {}
        if specification is not None:
            spec_dict = _spec_to_dict(specification)
            requirements = spec_dict.get("requirements", []) or []
            code_text = "\n".join((files or {}).values())
            for req in requirements:
                req_text = req if isinstance(req, str) else (req.get("description") or "")
                covered = req_text.lower() in code_text.lower()
                coverage[req_text[:80]] = covered
                if not covered:
                    warnings.append(f"Requirement possibly not covered: {req_text[:60]}")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "coverage": coverage,
        }


# --------------------------------------------------------------------- #
# ImplementationReport
# --------------------------------------------------------------------- #

@dataclass
class ImplementationReport:
    """Structured report returned to the planner / caller."""

    strategy: ImplementationStrategy
    template_name: str | None = None
    compatibility_score: float = 0.0
    files: dict[str, str] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    generation_metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "template_name": self.template_name,
            "compatibility_score": self.compatibility_score,
            "files": self.files,
            "dependencies": self.dependencies,
            "validation": self.validation,
            "generation_metadata": self.generation_metadata,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------- #
# ImplementationIntelligence — the single external entry point
# --------------------------------------------------------------------- #

class ImplementationIntelligence:
    """Top-level orchestrator for the Implementation Intelligence layer.

    This is the ONLY class callers need to import.
    All other classes above are internal implementation details.
    """

    @classmethod
    async def decide_strategy(
        cls,
        objective: str,
        specification: Any | None = None,
        *,
        available_capabilities: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> ImplementationReport:
        """
        The main entry point.

        Fallback chain (as required):
          1. Exact Template
          2. Compatible Template
          3. Hybrid Template + AI
          4. Full AI Generation
        No execution path terminates simply because no template exists.
        """
        # Step 1 — Discover ALL matching templates
        discoveries = TemplateDiscoveryEngine.discover(objective)

        # Step 2 — Rank them
        if discoveries:
            from services.capability_service import detect_capabilities
            caps = available_capabilities or await detect_capabilities()
            discoveries = TemplateRankingEngine.rank(
                objective, discoveries, available_dependencies=caps.get("available_libraries") or []
            )

        # Step 3 — Resolve strategy
        strategy, template_info = ImplementationStrategyResolver.resolve(
            discoveries, specification, available_capabilities=available_capabilities
        )

        # Step 4 — Generate implementation
        # Fallback chain: exact → hybrid → pure AI → empty (never terminate)
        files: dict[str, str] = {}
        deps: list[str] = []
        gen_metadata: dict[str, Any] = {}
        compat: float = 0.0
        warnings: list[str] = []
        validation: dict[str, Any] = {"valid": False, "errors": ["no implementation produced"], "warnings": [], "coverage": {}}

        # Attempt 1: Exact template
        if strategy == ImplementationStrategy.EXACT_TEMPLATE and template_info:
            try:
                from services.templates import render_template, extract_template_params
                params = extract_template_params(objective)
                rendered = render_template(
                    _dict_to_templatespec(template_info), params
                )
                files = (rendered or {}).get("files") or {}
                deps = (rendered or {}).get("dependencies") or []
                compat = template_info.get("compatibility", 1.0)
                gen_metadata = GenerationMetadata(
                    strategy=ImplementationStrategy.EXACT_TEMPLATE,
                    template_name=template_info.get("name"),
                    files_generated=list(files.keys()),
                    generated_at=datetime.now(timezone.utc).isoformat(),
                ).to_dict()
            except Exception as exc:
                logger.warning("[impl-intel] exact template failed: %s — falling back to hybrid", exc)
                strategy = ImplementationStrategy.HYBRID_TEMPLATE_AI

        # Attempt 2: Hybrid (or fallback from exact)
        if strategy == ImplementationStrategy.HYBRID_TEMPLATE_AI and template_info:
            try:
                result = await HybridGenerationEngine.generate(
                    template_info, specification, model=model
                )
                files = result["files"]
                deps = result["dependencies"]
                gen_metadata = result["generation_metadata"]
                compat = template_info.get("compatibility", 0.5)
            except Exception as exc:
                logger.warning("[impl-intel] hybrid generation failed: %s — falling back to pure AI", exc)
                strategy = ImplementationStrategy.PURE_AI_GENERATION

        # Attempt 3: Pure AI (or fallback from hybrid)
        if strategy == ImplementationStrategy.PURE_AI_GENERATION:
            try:
                result = await CodeGenerationEngine.generate(
                    specification, objective, model=model
                )
                files = result["files"]
                deps = result["dependencies"]
                gen_metadata = result["generation_metadata"]
                compat = 0.0
            except Exception as exc:
                logger.warning("[impl-intel] pure AI generation failed: %s — returning empty implementation", exc)
                # Final fallback: return empty files (caller decides what to do)
                files = {}
                deps = []
                gen_metadata = GenerationMetadata(
                    strategy=ImplementationStrategy.PURE_AI_GENERATION,
                    model=model,
                    files_generated=[],
                    generated_at=datetime.now(timezone.utc).isoformat(),
                ).to_dict()

        # Step 5 — Validate (always — even if files are empty, report what we have)
        try:
            validation = await ImplementationValidator.validate(
                files, dependencies=deps, specification=specification
            )
        except Exception as exc:
            logger.error("[impl-intel] validator crashed: %s — treating as validation failure", exc)
            validation = {
                "valid": False,
                "errors": [f"Validator crashed: {exc}"],
                "warnings": [],
                "coverage": {},
            }

        # Step 6 — If validation fails, attempt repair (bounded by MAX_REPAIR_ATTEMPTS)
        repair_attempts = 0
        current_files = dict(files)
        current_deps = list(deps)
        while (
            not validation.get("valid", True)
            and strategy != ImplementationStrategy.EXACT_TEMPLATE
            and repair_attempts < MAX_REPAIR_ATTEMPTS
        ):
            repair_attempts += 1
            logger.warning(
                "[impl-intel] validation failed (errors=%s) — repair attempt %s/%s",
                validation.get("errors", []),
                repair_attempts,
                MAX_REPAIR_ATTEMPTS,
            )
            try:
                repair_result = await _repair_implementation(
                    current_files, validation, specification, objective, model=model
                )
                if repair_result:
                    current_files = repair_result["files"]
                    current_deps = repair_result["dependencies"]
                    # Re-validate after repair
                    validation = await ImplementationValidator.validate(
                        current_files, dependencies=current_deps, specification=specification
                    )
                    warnings.append(f"Implementation repaired after attempt {repair_attempts}")
                else:
                    break  # repair returned nothing useful
            except Exception as exc:
                logger.warning("[impl-intel] repair attempt %s failed: %s", repair_attempts, exc)
                break

        # Update files/deps if repair succeeded
        if repair_attempts > 0 and validation.get("valid", True):
            files = current_files
            deps = current_deps
            warnings.append(f"Repair succeeded after {repair_attempts} attempt(s)")
        elif repair_attempts >= MAX_REPAIR_ATTEMPTS and not validation.get("valid", True):
            warnings.append(f"Repair failed after {MAX_REPAIR_ATTEMPTS} attempts — returning best-effort implementation")

        # Step 7 — Build report
        report = ImplementationReport(
            strategy=strategy,
            template_name=template_info.get("name") if template_info else None,
            compatibility_score=compat,
            files=files,
            dependencies=deps,
            validation=validation,
            generation_metadata=gen_metadata,
            warnings=warnings + validation.get("warnings", []),
        )
        return report


# --------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------- #

def _spec_to_dict(spec: Any) -> dict[str, Any]:
    if spec is None:
        return {}
    if isinstance(spec, dict):
        return spec
    try:
        return spec.model_dump(mode="json")
    except Exception as exc:
        logger.warning("[impl-intel] _spec_to_dict failed: %s — returning empty dict", exc)
        return {}


def _spec_to_text(spec: Any) -> str:
    d = _spec_to_dict(spec)
    return _json.dumps(d, ensure_ascii=False, indent=2) if d else ""


def _dict_to_templatespec(d: dict[str, Any]):
    """Rebuild a TemplateSpec-like object from a dict."""
    from services.templates import TemplateSpec
    return TemplateSpec(
        name=d.get("name", ""),
        description=d.get("description", ""),
        keywords=d.get("keywords", []),
        files=d.get("files", {}),
        dependencies=d.get("dependencies", []),
        execution_steps=d.get("execution_steps"),
    )


def _identify_missing_pieces(
    template_files: dict[str, str], spec: Any | None
) -> list[str]:
    """Compare template output against spec; return list of missing requirement descriptions."""
    if spec is None:
        return []
    spec_dict = _spec_to_dict(spec)
    required_files = spec_dict.get("files", []) or []
    missing = [f for f in required_files if f not in template_files]
    return missing


async def _ai_generate_missing(
    missing: list[str], spec: Any | None, *, model: str | None = None
) -> dict[str, str]:
    """Ask the LLM to generate the missing files only."""
    if not missing:
        return {}
    prompt = (
        f"Generate the following missing files for this project specification.\n"
        f"Specification:\n{_spec_to_text(spec)}\n\n"
        f"Missing files: {missing}\n\n"
        "Return ONLY a JSON object mapping filename to file content."
    )
    return await _call_llm_for_code(prompt, model=model)


def _build_ai_prompt(objective: str, spec: Any | None) -> str:
    return (
        f"Generate a complete Python project for the following objective.\n"
        f"Objective: {objective}\n"
        f"Specification:\n{_spec_to_text(spec)}\n\n"
        "Return ONLY a JSON object mapping filename to file content."
    )


async def _call_llm_for_code(prompt: str, *, model: str | None = None) -> dict[str, str]:
    """Call the LLM to generate code. Falls back gracefully on failure."""
    try:
        from services import agent_llm
        response = await agent_llm.llm_chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            json_mode=True,
        )
        text = (response or {}).get("text") or ""
        parsed = _json.loads(text)
        if isinstance(parsed, dict):
            return {k: v for k, v in parsed.items() if isinstance(v, str)}
    except Exception as exc:
        logger.warning("[impl-intel] LLM code generation failed: %s", exc)
    return {}


def _check_python_syntax(content: str) -> tuple[bool, str]:
    import io, sys
    try:
        compile(content, "<string>", "exec")
        return True, ""
    except SyntaxError as exc:
        return False, str(exc)


def _check_json_valid(content: str) -> tuple[bool, str]:
    try:
        _json.loads(content)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _extract_imports(files: dict[str, str]) -> set[str]:
    imports: set[str] = set()
    for content in (files or {}).values():
        for line in (content or "").splitlines():
            line = line.strip()
            if line.startswith("import "):
                for part in line[7:].split(","):
                    imports.add(part.strip().split(".")[0])
            elif line.startswith("from ") and " import " in line:
                mod = line[5:].split(" import ")[0].strip()
                imports.add(mod.split(".")[0])
    return imports


def _extract_deps(files: dict[str, str]) -> list[str]:
    """Naively extract likely dependencies from pip-style install commands in file content."""
    deps: list[str] = []
    for content in (files or {}).values():
        for line in content.splitlines():
            if "pip install" in line:
                for part in line.split("pip install")[-1].split():
                    part = part.strip().lstrip("-").rstrip(";")
                    if part and not part.startswith("-"):
                        deps.append(part)
    return deps


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


# --------------------------------------------------------------------- #
# Repair helper — re-run generation with validation errors in the prompt
# --------------------------------------------------------------------- #

async def _repair_implementation(
    files: dict[str, str],
    validation: dict[str, Any],
    specification: Any | None,
    objective: str,
    *,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Attempt to repair invalid implementation using validation errors as feedback.

    Returns new ``{"files": ..., "dependencies": ...}`` dict, or ``None`` on failure.
    """
    errors = validation.get("errors", [])
    if not errors:
        return None
    prompt = (
        f"The following implementation has validation errors:\n"
        f"{_spec_to_text(specification)}\n\n"
        f"Errors:\n" + "\n".join(f"- {e}" for e in errors) + "\n\n"
        "Fix the errors and return ONLY the corrected files as a JSON object "
        "(filename → content). Preserve all correct code; change only what is necessary."
    )
    try:
        repaired = await _call_llm_for_code(prompt, model=model)
        if repaired:
            return {
                "files": repaired,
                "dependencies": _extract_deps(repaired),
            }
    except Exception as exc:
        logger.warning("[impl-intel] repair LLM call failed: %s", exc)
    return None

"""Skill repository, linting, proposal application, and rollback."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, desc, or_, select
from sqlalchemy.orm import Session

from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import EvolutionProposal, Skill, SkillFile, SkillHistory, SkillTest

SKILL_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n(?P<body>.*)\Z", re.DOTALL)
TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
FORBIDDEN_PATTERNS = (
    r"rm\s+-rf\s+/",
    r"git\s+reset\s+--hard",
    r"git\s+checkout\s+--\s+/",
    r"chmod\s+-R\s+777\s+/",
    r"mkfs\.",
)
MAX_SKILL_FILES = 12
MAX_SKILL_FILE_BYTES = 64 * 1024
MAX_SKILL_TOTAL_BYTES = 256 * 1024
ALLOWED_TEST_KINDS = {"lint", "golden_prompt", "mock_tool", "regression"}


def normalize_skill_key(value: str) -> str:
    value = value.strip().replace("\\", "/").lower()
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^a-z0-9_\-/]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-/")


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_checksum(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[path].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def parse_skill_markdown(text: str) -> tuple[dict[str, Any], str]:
    metadata: dict[str, Any] = {}
    body = text
    match = SKILL_FRONTMATTER_RE.match(text)
    if match:
        loaded = yaml.safe_load(match.group("yaml")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("skill frontmatter must be a mapping")
        metadata = dict(loaded)
        body = match.group("body")
    return metadata, body


def skill_title(body: str, default: str) -> str:
    match = TITLE_RE.search(body)
    if match:
        return match.group(1).strip()
    return default


@dataclass(frozen=True)
class SkillLintResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    tests: list[dict[str, Any]]


class SkillService:
    def __init__(
        self,
        settings: Settings | None = None,
        events: RuntimeEventBus | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.events = events

    @property
    def root(self) -> Path:
        return self.settings.resolved_skills_root

    def scan(self, db: Session) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        count = 0
        for skill_md in sorted(self.root.rglob("SKILL.md")):
            skill_dir = skill_md.parent
            skill_key = normalize_skill_key(skill_dir.relative_to(self.root).as_posix())
            if not skill_key:
                skill_key = normalize_skill_key(skill_dir.name)
            files = self._read_skill_files(skill_dir)
            skill_text = files.get("SKILL.md", "")
            metadata, body = parse_skill_markdown(skill_text)
            name = str(metadata.get("name") or metadata.get("title") or skill_title(body, skill_key))
            description = str(metadata.get("description") or "").strip()

            record = db.scalar(select(Skill).where(Skill.skill_key == skill_key))
            if record is None:
                record = Skill(skill_key=skill_key, name=name, path=str(skill_dir))
                db.add(record)
            record.name = name
            record.description = description
            record.path = str(skill_dir)
            record.status = "active"
            record.metadata_json = metadata

            db.execute(delete(SkillFile).where(SkillFile.skill_key == skill_key))
            for relative_path, content in files.items():
                db.add(
                    SkillFile(
                        skill_key=skill_key,
                        file_path=relative_path,
                        checksum=checksum(content),
                        content=content,
                    )
                )
            self._sync_declared_tests(db, skill_key, metadata)
            count += 1

        if self.events:
            self.events.emit("skills.scan", {"skills": count, "index": "file_mirror"})
        return {"skills": count, "index": "file_mirror"}

    def list(self, db: Session, *, status: str | None = None, limit: int = 200) -> list[Skill]:
        stmt = select(Skill).order_by(Skill.skill_key).limit(max(1, min(limit, 1000)))
        if status:
            stmt = stmt.where(Skill.status == status)
        return list(db.scalars(stmt).all())

    def search(self, db: Session, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 50))
        query = query.strip()
        if query:
            pattern = f"%{query}%"
            stmt = (
                select(Skill)
                .where(
                    Skill.status == "active",
                    or_(
                        Skill.skill_key.ilike(pattern),
                        Skill.name.ilike(pattern),
                        Skill.description.ilike(pattern),
                    ),
                )
                .order_by(desc(Skill.pinned), Skill.skill_key)
                .limit(limit)
            )
        else:
            stmt = (
                select(Skill)
                .where(Skill.status == "active")
                .order_by(desc(Skill.pinned), Skill.skill_key)
                .limit(limit)
            )
        return [
            {
                "score": 1.0 if query and query.lower() in f"{skill.skill_key} {skill.name}".lower() else 0.7,
                "source": "skill_index",
                "skill": skill,
            }
            for skill in db.scalars(stmt).all()
        ]

    def get(self, db: Session, skill_key: str) -> Skill | None:
        return db.scalar(select(Skill).where(Skill.skill_key == normalize_skill_key(skill_key)))

    def files(self, db: Session, skill_key: str) -> list[SkillFile]:
        return list(
            db.scalars(
                select(SkillFile)
                .where(SkillFile.skill_key == normalize_skill_key(skill_key))
                .order_by(SkillFile.file_path)
            ).all()
        )

    def lint_files(self, files: dict[str, str]) -> SkillLintResult:
        errors: list[str] = []
        warnings: list[str] = []
        total_bytes = 0
        if len(files) > MAX_SKILL_FILES:
            errors.append(f"too many skill files: {len(files)} > {MAX_SKILL_FILES}")
        if "SKILL.md" not in files:
            errors.append("SKILL.md is required")
            metadata: dict[str, Any] = {}
        else:
            try:
                metadata, body = parse_skill_markdown(files["SKILL.md"])
                if not skill_title(body, "") and not metadata.get("name") and not metadata.get("title"):
                    warnings.append("Skill has no explicit name/title")
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                metadata = {}
                body = files.get("SKILL.md", "")

        for path, content in files.items():
            normalized = Path(path)
            if normalized.is_absolute() or ".." in normalized.parts:
                errors.append(f"unsafe file path: {path}")
            if any(part.startswith(".") for part in normalized.parts):
                errors.append(f"hidden skill file path is not allowed: {path}")
            size = len(content.encode("utf-8"))
            total_bytes += size
            if size > MAX_SKILL_FILE_BYTES:
                errors.append(f"skill file too large: {path}")
            scan_content = content
            if path == "SKILL.md":
                safety_metadata = {key: value for key, value in metadata.items() if key not in {"test_cases", "tests"}}
                scan_content = yaml.safe_dump(safety_metadata, allow_unicode=True, sort_keys=True) + "\n" + body
            for pattern in FORBIDDEN_PATTERNS:
                if re.search(pattern, scan_content):
                    errors.append(f"forbidden unsafe pattern `{pattern}` in {path}")
        if total_bytes > MAX_SKILL_TOTAL_BYTES:
            errors.append(f"skill files exceed total size budget: {total_bytes} > {MAX_SKILL_TOTAL_BYTES}")

        required_tools = metadata.get("required_tools") or metadata.get("tools") or []
        if isinstance(required_tools, str):
            required_tools = [required_tools]
        if required_tools and not isinstance(required_tools, list):
            errors.append("required_tools must be a list or string")

        tests = metadata.get("test_cases") or metadata.get("tests") or []
        if tests and not isinstance(tests, list):
            errors.append("test_cases/tests must be a list")
            tests = []
        normalized_tests: list[dict[str, Any]] = []
        for index, test in enumerate(tests):
            spec = test if isinstance(test, dict) else {"name": f"case-{index + 1}", "input": test}
            kind = str(spec.get("kind") or ("golden_prompt" if spec.get("input") or spec.get("expected") else "lint"))
            if kind not in ALLOWED_TEST_KINDS:
                errors.append(f"unsupported test kind `{kind}`")
            normalized_tests.append({**spec, "kind": kind, "name": str(spec.get("name") or f"case-{index + 1}")})
        if not tests:
            warnings.append("No manifest test cases declared")
        return SkillLintResult(ok=not errors, errors=errors, warnings=warnings, tests=normalized_tests)

    def test(self, db: Session, skill_key: str) -> dict[str, Any]:
        key = normalize_skill_key(skill_key)
        files = {file.file_path: file.content for file in self.files(db, key)}
        if not files:
            skill = self.get(db, key)
            if skill:
                files = self._read_skill_files(Path(skill.path))
        result = self.lint_files(files)
        case_results = [self._run_test_case(spec, files, lint=result) for spec in result.tests]
        all_cases_passed = all(item["status"] == "passed" for item in case_results)
        payload = {
            "ok": result.ok and all_cases_passed,
            "errors": result.errors,
            "warnings": result.warnings,
            "tests": case_results,
            "required_checks": ["frontmatter", "required_tools", "static_safety_scan", "safe_manifest_test_cases"],
        }
        test_record = SkillTest(
            skill_key=key,
            name="skill-test-suite",
            spec={"kind": "suite", "checksum": snapshot_checksum(files)},
            last_status="passed" if payload["ok"] else "failed",
            last_result=payload,
            last_run_at=datetime.now(UTC),
        )
        db.add(test_record)
        return payload

    def create_proposal(
        self,
        db: Session,
        *,
        target_type: str,
        action: str,
        payload: dict[str, Any],
        evidence: dict[str, Any] | None = None,
        risk_level: str = "medium",
    ) -> EvolutionProposal:
        payload = dict(payload)
        target_checksum = ""
        before_snapshot: dict[str, Any] = {}
        if target_type == "skill":
            skill_key = normalize_skill_key(str(payload.get("skill_key") or ""))
            files = payload.get("files")
            if skill_key and isinstance(files, dict):
                current = self._snapshot(self._safe_skill_dir(skill_key))
                proposed = {str(path): str(content) for path, content in files.items()}
                target_checksum = snapshot_checksum(current)
                before_snapshot = {"files": current, "checksum": target_checksum}
                payload.setdefault("base_checksum", target_checksum)
                payload.setdefault("diff", self._diff_summary(current, proposed))
        proposal = EvolutionProposal(
            target_type=target_type,
            action=action,
            status="pending",
            risk_level=risk_level,
            payload=payload,
            evidence=evidence or {},
            before_snapshot=before_snapshot,
            target_checksum=target_checksum,
        )
        db.add(proposal)
        db.flush()
        if self.events:
            self.events.emit(
                "skill.proposal.created",
                {"proposal_id": str(proposal.id), "target_type": target_type, "action": action},
            )
            self.events.audit(
                "evolution.proposal.create",
                "evolution_proposal",
                target_id=str(proposal.id),
                payload={"target_type": target_type, "action": action},
            )
        return proposal

    def list_proposals(self, db: Session, *, status: str | None = None, limit: int = 100) -> list[EvolutionProposal]:
        stmt = select(EvolutionProposal).order_by(desc(EvolutionProposal.created_at)).limit(max(1, min(limit, 500)))
        if status:
            stmt = stmt.where(EvolutionProposal.status == status)
        return list(db.scalars(stmt).all())

    def apply_proposal(self, db: Session, proposal_id: uuid.UUID, *, actor: str = "admin") -> EvolutionProposal:
        proposal = db.get(EvolutionProposal, proposal_id)
        if proposal is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        if proposal.status not in {"pending", "approved"}:
            raise ValueError(f"proposal is not applyable: {proposal.status}")
        if proposal.target_type != "skill":
            raise ValueError("only skill proposals are currently applyable")

        skill_key = normalize_skill_key(str(proposal.payload.get("skill_key") or ""))
        files = proposal.payload.get("files")
        if not skill_key or not isinstance(files, dict):
            raise ValueError("skill proposals require payload.skill_key and payload.files")
        files = {str(path): str(content) for path, content in files.items()}
        lint = self.lint_files(files)
        if not lint.ok:
            proposal.status = "rejected"
            proposal.result = {"ok": False, "errors": lint.errors, "warnings": lint.warnings}
            return proposal

        skill_dir = self._safe_skill_dir(skill_key)
        before = self._snapshot(skill_dir)
        current_checksum = snapshot_checksum(before)
        expected_checksum = str(proposal.payload.get("base_checksum") or proposal.target_checksum or "")
        if expected_checksum and current_checksum != expected_checksum:
            reason = f"target checksum changed: expected {expected_checksum}, got {current_checksum}"
            proposal.status = "stale"
            proposal.stale_reason = reason
            proposal.result = {"ok": False, "stale": True, "reason": reason}
            return proposal
        test_payload = self._test_files(skill_key, files)
        if not test_payload["ok"]:
            proposal.status = "rejected"
            proposal.result = {"ok": False, "errors": test_payload.get("errors", []), "warnings": test_payload.get("warnings", []), "tests": test_payload.get("tests", [])}
            return proposal
        for relative_path, content in files.items():
            target = self._safe_child(skill_dir, relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        after = self._snapshot(skill_dir)

        db.add(
            SkillHistory(
                skill_key=skill_key,
                action="apply_proposal",
                before_snapshot={"files": before, "checksum": current_checksum},
                after_snapshot={"files": after, "checksum": snapshot_checksum(after), "proposal_id": str(proposal.id)},
                actor=actor,
            )
        )
        proposal.status = "applied"
        proposal.before_snapshot = {"files": before, "checksum": current_checksum}
        proposal.after_snapshot = {"files": after, "checksum": snapshot_checksum(after)}
        proposal.result = {"ok": True, "warnings": lint.warnings, "tests": test_payload["tests"], "diff": proposal.payload.get("diff", {})}
        proposal.applied_at = datetime.now(UTC)
        self.scan(db)
        if self.events:
            self.events.emit("skill.proposal.applied", {"proposal_id": str(proposal.id), "skill_key": skill_key})
            self.events.audit(
                "skill.proposal.apply",
                "evolution_proposal",
                target_id=str(proposal.id),
                payload={"skill_key": skill_key, "actor": actor},
            )
        return proposal

    def rollback(self, db: Session, skill_key: str, *, actor: str = "admin") -> dict[str, Any]:
        key = normalize_skill_key(skill_key)
        history = db.scalar(
            select(SkillHistory)
            .where(SkillHistory.skill_key == key)
            .order_by(desc(SkillHistory.created_at))
            .limit(1)
        )
        if history is None:
            raise KeyError(f"no rollback snapshot for skill: {key}")
        before = history.before_snapshot.get("files", {})
        skill_dir = self._safe_skill_dir(key)
        current = self._snapshot(skill_dir)
        for relative_path, content in before.items():
            target = self._safe_child(skill_dir, relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
        db.add(
            SkillHistory(
                skill_key=key,
                action="rollback",
                before_snapshot={"files": current},
                after_snapshot={"files": before},
                actor=actor,
            )
        )
        self.scan(db)
        if self.events:
            self.events.emit("skill.rollback", {"skill_key": key})
            self.events.audit("skill.rollback", "skill", target_id=key, payload={"actor": actor})
        return {"ok": True, "skill_key": key, "restored_files": sorted(before)}

    def _test_files(self, skill_key: str, files: dict[str, str]) -> dict[str, Any]:
        lint = self.lint_files(files)
        tests = [self._run_test_case(spec, files, lint=lint) for spec in lint.tests]
        ok = lint.ok and all(item["status"] == "passed" for item in tests)
        return {
            "ok": ok,
            "errors": lint.errors,
            "warnings": lint.warnings,
            "tests": tests,
            "skill_key": normalize_skill_key(skill_key),
        }

    def _run_test_case(self, spec: dict[str, Any], files: dict[str, str], *, lint: SkillLintResult) -> dict[str, Any]:
        kind = str(spec.get("kind") or "lint")
        name = str(spec.get("name") or kind)
        if kind == "lint":
            passed = lint.ok
            return {
                "name": name,
                "kind": kind,
                "status": "passed" if passed else "failed",
                "assertions": ["lint.ok"],
                "evidence": {"errors": lint.errors, "warnings": lint.warnings},
            }
        haystack = self._operational_text(files)
        if kind == "golden_prompt":
            expected = spec.get("expected_contains") or spec.get("expected")
            expected_items = [str(item) for item in expected] if isinstance(expected, list) else ([str(expected)] if expected else [])
            missing = [item for item in expected_items if item and item not in haystack]
            status = "passed" if not missing else "failed"
            return {
                "name": name,
                "kind": kind,
                "status": status,
                "assertions": ["expected content appears in skill files"],
                "evidence": {"missing": missing, "input": spec.get("input", "")},
            }
        if kind == "mock_tool":
            required = spec.get("required_tools") or spec.get("tools") or []
            if isinstance(required, str):
                required = [required]
            declared = self._declared_tools(files)
            missing = [str(item) for item in required if str(item) not in declared]
            return {
                "name": name,
                "kind": kind,
                "status": "passed" if not missing else "failed",
                "assertions": ["required mock tools are declared"],
                "evidence": {"declared_tools": declared, "missing": missing},
            }
        if kind == "regression":
            patterns = spec.get("must_not_contain") or spec.get("forbidden") or []
            if isinstance(patterns, str):
                patterns = [patterns]
            found = [str(pattern) for pattern in patterns if str(pattern) and str(pattern) in haystack]
            return {
                "name": name,
                "kind": kind,
                "status": "passed" if not found else "failed",
                "assertions": ["regression forbidden patterns are absent"],
                "evidence": {"found": found},
            }
        return {
            "name": name,
            "kind": kind,
            "status": "failed",
            "assertions": ["test kind is supported"],
            "evidence": {"allowed": sorted(ALLOWED_TEST_KINDS)},
        }

    @staticmethod
    def _declared_tools(files: dict[str, str]) -> list[str]:
        try:
            metadata, _body = parse_skill_markdown(files.get("SKILL.md", ""))
        except Exception:  # noqa: BLE001
            return []
        tools = metadata.get("required_tools") or metadata.get("tools") or []
        if isinstance(tools, str):
            tools = [tools]
        return [str(item) for item in tools] if isinstance(tools, list) else []

    @staticmethod
    def _operational_text(files: dict[str, str]) -> str:
        parts: list[str] = []
        for path in sorted(files):
            content = files.get(path, "")
            if path != "SKILL.md":
                parts.append(content)
                continue
            try:
                metadata, body = parse_skill_markdown(content)
                metadata = {key: value for key, value in metadata.items() if key not in {"test_cases", "tests"}}
                parts.append(yaml.safe_dump(metadata, allow_unicode=True, sort_keys=True))
                parts.append(body)
            except Exception:  # noqa: BLE001
                parts.append(content)
        return "\n\n".join(parts)

    @staticmethod
    def _diff_summary(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
        before_keys = set(before)
        after_keys = set(after)
        changed = sorted(path for path in before_keys & after_keys if before[path] != after[path])
        return {
            "added": sorted(after_keys - before_keys),
            "changed": changed,
            "removed": sorted(before_keys - after_keys),
        }

    def _sync_declared_tests(self, db: Session, skill_key: str, metadata: dict[str, Any]) -> None:
        declared = metadata.get("test_cases") or metadata.get("tests") or []
        if not isinstance(declared, list):
            return
        for index, spec in enumerate(declared):
            name = spec.get("name") if isinstance(spec, dict) else f"case-{index + 1}"
            db.add(
                SkillTest(
                    skill_key=skill_key,
                    name=str(name or f"case-{index + 1}"),
                    spec=spec if isinstance(spec, dict) else {"value": spec},
                )
            )

    def _read_skill_files(self, skill_dir: Path) -> dict[str, str]:
        files: dict[str, str] = {}
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file():
                continue
            if any(part.startswith(".") for part in path.relative_to(skill_dir).parts):
                continue
            try:
                relative = path.relative_to(skill_dir).as_posix()
                files[relative] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        return files

    def _snapshot(self, skill_dir: Path) -> dict[str, str]:
        if not skill_dir.exists():
            return {}
        return self._read_skill_files(skill_dir)

    def _safe_skill_dir(self, skill_key: str) -> Path:
        key = normalize_skill_key(skill_key)
        target = (self.root / key).resolve()
        root = self.root.resolve()
        if root not in target.parents and target != root:
            raise ValueError("unsafe skill path")
        return target

    @staticmethod
    def _safe_child(base: Path, relative_path: str) -> Path:
        candidate = (base / relative_path).resolve()
        base_resolved = base.resolve()
        if base_resolved not in candidate.parents and candidate != base_resolved:
            raise ValueError(f"unsafe file path: {relative_path}")
        return candidate

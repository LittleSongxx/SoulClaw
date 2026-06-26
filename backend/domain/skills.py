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
from sqlalchemy import delete, desc, select
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


def normalize_skill_key(value: str) -> str:
    value = value.strip().replace("\\", "/").lower()
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^a-z0-9_\-/]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-/")


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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

        for path, content in files.items():
            normalized = Path(path)
            if normalized.is_absolute() or ".." in normalized.parts:
                errors.append(f"unsafe file path: {path}")
            for pattern in FORBIDDEN_PATTERNS:
                if re.search(pattern, content):
                    errors.append(f"forbidden unsafe pattern `{pattern}` in {path}")

        required_tools = metadata.get("required_tools") or metadata.get("tools") or []
        if isinstance(required_tools, str):
            required_tools = [required_tools]
        if required_tools and not isinstance(required_tools, list):
            errors.append("required_tools must be a list or string")

        tests = metadata.get("test_cases") or metadata.get("tests") or []
        if tests and not isinstance(tests, list):
            errors.append("test_cases/tests must be a list")
            tests = []
        if not tests:
            warnings.append("No manifest test cases declared")
        return SkillLintResult(ok=not errors, errors=errors, warnings=warnings, tests=list(tests))

    def test(self, db: Session, skill_key: str) -> dict[str, Any]:
        key = normalize_skill_key(skill_key)
        files = {file.file_path: file.content for file in self.files(db, key)}
        if not files:
            skill = self.get(db, key)
            if skill:
                files = self._read_skill_files(Path(skill.path))
        result = self.lint_files(files)
        payload = {
            "ok": result.ok,
            "errors": result.errors,
            "warnings": result.warnings,
            "tests": result.tests,
            "required_checks": ["frontmatter", "required_tools", "static_safety_scan", "manifest_test_cases"],
        }
        test_record = SkillTest(
            skill_key=key,
            name="manifest-lint",
            spec={"kind": "lint"},
            last_status="passed" if result.ok else "failed",
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
        proposal = EvolutionProposal(
            target_type=target_type,
            action=action,
            status="pending",
            risk_level=risk_level,
            payload=payload,
            evidence=evidence or {},
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
        for relative_path, content in files.items():
            target = self._safe_child(skill_dir, relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        after = self._snapshot(skill_dir)

        db.add(
            SkillHistory(
                skill_key=skill_key,
                action="apply_proposal",
                before_snapshot={"files": before},
                after_snapshot={"files": after},
                actor=actor,
            )
        )
        proposal.status = "applied"
        proposal.before_snapshot = {"files": before}
        proposal.after_snapshot = {"files": after}
        proposal.result = {"ok": True, "warnings": lint.warnings, "tests": lint.tests}
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

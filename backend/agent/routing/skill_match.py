from __future__ import annotations

from difflib import SequenceMatcher
from typing import Optional, Sequence

from loguru import logger

from ...skills.loader import SkillManifest


def pick_skill_for_message(
    message_text: str,
    manifests: Sequence[SkillManifest],
) -> Optional[str]:
    if not message_text or not manifests:
        return None
    text_lower = message_text.lower()

    candidates: list[tuple[float, int, str]] = []
    for manifest in manifests:
        weighted = 0.0
        fields: list[str] = []
        fields.extend(str(t) for t in getattr(manifest, "triggers", []) or [])
        fields.extend(str(t) for t in getattr(manifest, "tags", []) or [])
        fields.extend(str(t) for t in getattr(manifest, "capabilities", []) or [])
        fields.extend(str(t) for t in getattr(manifest, "inputs", []) or [])
        fields.extend(str(t) for t in getattr(manifest, "outputs", []) or [])
        fields.append(str(getattr(manifest, "name", "") or ""))
        fields.append(str(getattr(manifest, "description", "") or ""))
        fields = [f.strip() for f in fields if f and f.strip()]
        if not fields:
            continue
        hits = 0
        for field in fields:
            lower = field.lower()
            if lower and lower in text_lower:
                hits += 1
                weighted += 3.0 if field in getattr(manifest, "triggers", []) else 1.5
            ratio = SequenceMatcher(None, text_lower, lower).ratio()
            if ratio >= 0.22:
                weighted += ratio
        if hits == 0:
            # Try word-level overlap for descriptions and capability lists.
            query_terms = {t for t in text_lower.replace("_", " ").split() if len(t) >= 2}
            field_terms = {
                t
                for f in fields
                for t in f.lower().replace("_", " ").split()
                if len(t) >= 2
            }
            overlap = query_terms & field_terms
            if overlap:
                weighted += len(overlap) / max(1, len(query_terms)) * 2.0
            else:
                continue
        if weighted <= 0:
            continue
        candidates.append((weighted, len(fields), manifest.id))

    if not candidates:
        return None

    candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
    chosen = candidates[0][2]

    logger.info(
        "skill_router picked {!r} (score={:.2f}, fields_total={}, candidates={})",
        chosen,
        candidates[0][0],
        candidates[0][1],
        len(candidates),
    )
    return chosen

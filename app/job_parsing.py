"""Normalises the recruiter's free-form requirement lists before they are stored."""

import re

# Matches a line that is *only* an experience requirement, e.g. "5+ years experience",
# "3 yrs", "7+ years of experience". "Python 3+ years" is left alone as a skill.
_EXPERIENCE_LINE = re.compile(
    r"^\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)(?:\s+of)?(?:\s+(?:experience|exp))?\.?\s*$",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*[-*•]\s*")


def normalize_skills(items: list[str]) -> list[str]:
    """Strip bullets/whitespace, drop blanks, and de-duplicate case-insensitively (order kept)."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        skill = _BULLET.sub("", item).strip()
        if skill and skill.lower() not in seen:
            seen.add(skill.lower())
            result.append(skill)
    return result


def split_experience(items: list[str]) -> tuple[list[str], int | None]:
    """Pull experience lines out of a requirements list; return (skills, max years found)."""
    skills: list[str] = []
    years: int | None = None
    for item in items:
        match = _EXPERIENCE_LINE.match(item)
        if match:
            years = max(years or 0, int(match.group(1)))
        else:
            skills.append(item)
    return skills, years

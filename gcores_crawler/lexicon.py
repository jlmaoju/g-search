from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterable, List, Sequence


DEFAULT_ALIAS_MAP = {
    "尤科": "游科",
}

MAX_TERM_LEN = 24
TEXT_FRAGMENT_MAX_LEN = 16
LATIN_TERM_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+._:-]{1,31}\b")
QUOTED_PATTERNS = [
    re.compile(r"《([^》]{1,24})》"),
    re.compile(r"“([^”]{1,24})”"),
    re.compile(r"【([^】]{1,24})】"),
    re.compile(r"（([^）]{1,24})）"),
    re.compile(r"\(([^)]{1,24})\)"),
]
SPLIT_RE = re.compile(r"[，。！？；：、/\n\r\t|]+")
STOP_TERMS = {
    "欢迎收听",
    "欢迎收听本期节目",
    "感谢收听",
    "感谢收听本期节目",
    "本期时间轴制作",
    "本期时间轴由AI制作",
    "本期时间轴由ai制作",
    "本期为机核视频播客节目Wave特供版",
    "请关注B站视频账号",
    "GadioWave是一档快速",
    "自由的电台栏目",
    "没有固定的更新时间",
    "没有固定的主持人员",
    "机核编辑部",
    "会员专享",
    "广告",
    "出品",
    "开场BGM",
    "结尾BGM",
    "视频播客",
    "特供版",
    "本期节目",
    "节目中",
    "嘉宾",
}
STOP_SUBSTRINGS = (
    "欢迎收听",
    "感谢收听",
    "本期时间轴",
    "时间轴制作",
    "请关注",
    "完整版的视频内容",
    "视频播客节目",
    "固定的更新时间",
    "固定的主持人员",
    "机核编辑部",
    "会员专享",
    "广告",
    "节目中",
    "嘉宾表达",
    "大家不要",
    "尽请老师",
)


def build_lexicon(
    root: str = "data",
    *,
    content_types: Sequence[str],
    limit: int = 2000,
    output_dir: str | None = None,
) -> dict:
    root_path = Path(root)
    lexicon_dir = Path(output_dir) if output_dir else (root_path / "lexicon")
    lexicon_dir.mkdir(parents=True, exist_ok=True)

    counter: Counter[str] = Counter()
    files_seen = 0
    for content_type in content_types:
        normalized_dir = root_path / "normalized" / content_type
        if not normalized_dir.exists():
            continue
        for path in normalized_dir.glob("*.json"):
            files_seen += 1
            try:
                record = json.loads(path.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError:
                continue
            counter.update(extract_hotwords_from_record(record))

    hotwords = [term for term, _count in counter.most_common(limit)]
    hotword_path = lexicon_dir / "global_hotwords.txt"
    hotword_path.write_text("\n".join(hotwords) + ("\n" if hotwords else ""), encoding="utf-8")

    alias_map_path = lexicon_dir / "alias_map.json"
    if alias_map_path.exists():
        alias_map = json.loads(alias_map_path.read_text(encoding="utf-8-sig"))
    else:
        alias_map = dict(DEFAULT_ALIAS_MAP)
        alias_map_path.write_text(json.dumps(alias_map, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "root": str(root_path),
        "lexicon_dir": str(lexicon_dir),
        "files_seen": files_seen,
        "hotword_count": len(hotwords),
        "hotword_path": str(hotword_path),
        "alias_map_count": len(alias_map),
        "alias_map_path": str(alias_map_path),
        "top_terms": hotwords[:30],
    }


def extract_hotwords_from_record(record: dict) -> List[str]:
    candidates: list[str] = []

    candidates.extend(normalize_term_list(record.get("tags", [])))
    candidates.extend(normalize_term_list([record.get("category")]))
    candidates.extend(normalize_term_list(user.get("nickname") for user in record.get("users", [])))
    candidates.extend(normalize_term_list(media.get("title") for media in record.get("media", [])))

    text_fields = [
        record.get("title"),
        record.get("excerpt"),
        record.get("desc"),
    ]
    for media in record.get("media", []):
        for timeline in media.get("timelines", []):
            text_fields.append(timeline.get("title"))

    for text in text_fields:
        candidates.extend(extract_hotwords_from_text(text))

    return dedupe_preserve_order(candidates)


def extract_hotwords_from_text(text: object) -> List[str]:
    if not isinstance(text, str):
        return []

    source = text.strip()
    if not source:
        return []

    candidates: list[str] = []
    full_term = normalize_term(source)
    if full_term:
        candidates.append(full_term)

    for pattern in QUOTED_PATTERNS:
        for match in pattern.finditer(source):
            candidate = normalize_term(match.group(1))
            if candidate:
                candidates.append(candidate)

    for token in LATIN_TERM_RE.findall(source):
        candidate = normalize_term(token)
        if candidate:
            candidates.append(candidate)

    fragments = source
    for quote in "《》“”【】（）()":
        fragments = fragments.replace(quote, " ")
    for fragment in SPLIT_RE.split(fragments):
        candidate = normalize_fragment(fragment)
        if candidate:
            candidates.append(candidate)

    return dedupe_preserve_order(candidates)


def normalize_fragment(value: str) -> str | None:
    fragment = " ".join(value.strip().split())
    if not fragment:
        return None
    if len(fragment) > TEXT_FRAGMENT_MAX_LEN:
        return None
    return normalize_term(fragment)


def normalize_term_list(values: Iterable[object]) -> List[str]:
    results: list[str] = []
    for value in values:
        candidate = normalize_term(value)
        if candidate:
            results.append(candidate)
    return results


def normalize_term(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    term = " ".join(value.strip().split())
    if not term:
        return None
    if len(term) > MAX_TERM_LEN:
        return None
    if term in STOP_TERMS:
        return None
    if any(stop in term for stop in STOP_SUBSTRINGS):
        return None
    if re.fullmatch(r"[0-9]+", term):
        return None
    if "http://" in term or "https://" in term:
        return None
    if len(term) == 1:
        return None
    if re.fullmatch(r"[\W_]+", term, flags=re.UNICODE):
        return None
    return term


def load_hotwords(path: str | Path | None) -> List[str]:
    if not path:
        return []
    hotword_path = Path(path)
    if not hotword_path.exists():
        return []
    lines = []
    for line in hotword_path.read_text(encoding="utf-8-sig").splitlines():
        term = normalize_term(line.split("|", 1)[0])
        if term:
            lines.append(term)
    return dedupe_preserve_order(lines)


def merge_hotwords(*groups: Iterable[str], limit: int) -> List[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for term in group:
            normalized = normalize_term(term)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
            if len(merged) >= limit:
                return merged
    return merged


def write_hotwords(path: str | Path, hotwords: Sequence[str]) -> None:
    hotword_path = Path(path)
    hotword_path.parent.mkdir(parents=True, exist_ok=True)
    hotword_path.write_text("\n".join(hotwords) + ("\n" if hotwords else ""), encoding="utf-8")


def load_alias_map(path: str | Path | None) -> dict[str, str]:
    if not path:
        return dict(DEFAULT_ALIAS_MAP)
    alias_path = Path(path)
    if not alias_path.exists():
        return dict(DEFAULT_ALIAS_MAP)
    parsed = json.loads(alias_path.read_text(encoding="utf-8-sig"))
    if not isinstance(parsed, dict):
        return dict(DEFAULT_ALIAS_MAP)
    result = dict(DEFAULT_ALIAS_MAP)
    for key, value in parsed.items():
        source = normalize_term(key)
        target = normalize_term(value)
        if source and target:
            result[source] = target
    return result


def apply_alias_map(payload: dict, alias_map: dict[str, str]) -> tuple[dict, dict]:
    if not alias_map:
        return payload, {"replacements": {}, "changed": False}

    replacements: Counter[str] = Counter()
    transformed = dict(payload)
    text = str(payload.get("text") or "")
    new_text, text_counts = replace_aliases(text, alias_map)
    replacements.update(text_counts)
    transformed["text"] = new_text

    new_segments = []
    for segment in payload.get("segments") or []:
        segment_copy = dict(segment)
        segment_text = str(segment.get("text") or "")
        updated_text, counts = replace_aliases(segment_text, alias_map)
        replacements.update(counts)
        segment_copy["text"] = updated_text
        new_segments.append(segment_copy)
    transformed["segments"] = new_segments

    metadata = dict(transformed.get("metadata") or {})
    metadata["alias_replacements"] = dict(replacements)
    transformed["metadata"] = metadata
    return transformed, {"replacements": dict(replacements), "changed": bool(replacements)}


def replace_aliases(text: str, alias_map: dict[str, str]) -> tuple[str, Counter[str]]:
    updated = text
    counts: Counter[str] = Counter()
    for source in sorted(alias_map.keys(), key=len, reverse=True):
        target = alias_map[source]
        occurrences = updated.count(source)
        if occurrences:
            updated = updated.replace(source, target)
            counts[f"{source}->{target}"] += occurrences
    return updated, counts


def dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    results: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        results.append(value)
    return results

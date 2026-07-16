"""Known hardware-quirk matching for the current minimal emulator subset."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from core.decode import DecodeResult


@dataclass(frozen=True)
class QuirkSource:
    """One source-attribution entry for a known hardware quirk."""

    document: str
    section: str | None
    quote: str | None


@dataclass(frozen=True)
class KnownQuirkMatch:
    """One matched hardware quirk from the current local knowledge base."""

    database_version: str
    quirk_id: str
    category: str
    confidence: str
    summary: str
    note: str
    sources: tuple[QuirkSource, ...]


@dataclass(frozen=True)
class KnownQuirkDatabase:
    """The current local quirk database snapshot."""

    database_version: str
    entry_count: int


@dataclass(frozen=True)
class _KnownQuirkRule:
    """One normalized quirk rule loaded from the local versioned database."""

    database_version: str
    quirk_id: str
    category: str
    confidence: str
    summary: str
    note: str
    sources: tuple[QuirkSource, ...]
    matcher_kind: str
    matcher_args: dict[str, object]
    execution_stop_status: str | None

    def to_match(self) -> KnownQuirkMatch:
        return KnownQuirkMatch(
            database_version=self.database_version,
            quirk_id=self.quirk_id,
            category=self.category,
            confidence=self.confidence,
            summary=self.summary,
            note=self.note,
            sources=self.sources,
        )


@lru_cache(maxsize=1)
def load_known_quirk_database() -> KnownQuirkDatabase:
    """Load the current local versioned quirk database metadata."""
    payload = _load_quirk_database_payload()
    return KnownQuirkDatabase(
        database_version=_require_non_empty_str(payload, "database_version"),
        entry_count=len(_load_quirk_rules()),
    )


def match_known_quirk(decoded: DecodeResult) -> KnownQuirkMatch | None:
    """Return the first known local hardware quirk matched by this decoded instruction."""
    for rule in _load_quirk_rules():
        if _matches_rule(decoded, rule):
            return rule.to_match()
    return None


def match_known_silicon_broken(decoded: DecodeResult) -> KnownQuirkMatch | None:
    """Return the known silicon-broken quirk matched by this decoded instruction, if any."""
    for rule in _load_quirk_rules():
        if rule.execution_stop_status != "silicon-broken":
            continue
        if _matches_rule(decoded, rule):
            return rule.to_match()
    return None


@lru_cache(maxsize=1)
def _load_quirk_rules() -> tuple[_KnownQuirkRule, ...]:
    payload = _load_quirk_database_payload()
    database_version = _require_non_empty_str(payload, "database_version")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("quirk database must define an entries list")

    rules: list[_KnownQuirkRule] = []
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            raise ValueError(f"quirk database entry #{index} must be an object")
        matcher = raw_entry.get("matcher")
        if not isinstance(matcher, dict):
            raise ValueError(f"quirk database entry #{index} must define a matcher object")
        matcher_kind = _require_non_empty_str(matcher, "kind", context=f"entry #{index} matcher")
        matcher_args = _normalize_matcher_args(matcher_kind, matcher, context=f"entry #{index} matcher")
        rules.append(
            _KnownQuirkRule(
                database_version=database_version,
                quirk_id=_require_non_empty_str(raw_entry, "quirk_id", context=f"entry #{index}"),
                category=_require_non_empty_str(raw_entry, "category", context=f"entry #{index}"),
                confidence=_require_non_empty_str(
                    raw_entry, "confidence", context=f"entry #{index}"
                ),
                summary=_require_non_empty_str(raw_entry, "summary", context=f"entry #{index}"),
                note=_require_non_empty_str(raw_entry, "note", context=f"entry #{index}"),
                sources=_require_quirk_sources(raw_entry, context=f"entry #{index}"),
                matcher_kind=matcher_kind,
                matcher_args=matcher_args,
                execution_stop_status=_optional_non_empty_str(
                    raw_entry,
                    "execution_stop_status",
                    context=f"entry #{index}",
                ),
            )
        )
    return tuple(rules)


@lru_cache(maxsize=1)
def _load_quirk_database_payload() -> dict[str, object]:
    path = Path(__file__).with_name("quirks_db.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("quirk database root must be an object")
    return payload


def _matches_rule(decoded: DecodeResult, rule: _KnownQuirkRule) -> bool:
    if rule.matcher_kind == "prefixed_range_non_immediate":
        return _matches_prefixed_range_non_immediate(decoded, rule.matcher_args)
    if rule.matcher_kind == "warning_contains":
        return _matches_warning_contains(decoded, rule.matcher_args)
    raise ValueError(f"unsupported quirk matcher kind: {rule.matcher_kind}")


def _normalize_matcher_args(
    matcher_kind: str,
    matcher: dict[str, object],
    context: str,
) -> dict[str, object]:
    if matcher_kind == "prefixed_range_non_immediate":
        return {
            "prefix_start": _require_int(matcher, "prefix_start", context=context),
            "prefix_end": _require_int(matcher, "prefix_end", context=context),
            "safe_second_values": _require_int_list(
                matcher,
                "safe_second_values",
                context=context,
            ),
            "safe_second_ranges": _require_int_pair_list(
                matcher,
                "safe_second_ranges",
                context=context,
            ),
            "safe_exact_pairs": _require_int_pair_list(
                matcher,
                "safe_exact_pairs",
                context=context,
                allow_missing=True,
            ),
        }
    if matcher_kind == "warning_contains":
        return {
            "pattern": _require_non_empty_str(matcher, "pattern", context=context),
        }
    raise ValueError(f"{context} uses unknown matcher kind: {matcher_kind}")


def _matches_prefixed_range_non_immediate(
    decoded: DecodeResult,
    matcher_args: dict[str, object],
) -> bool:
    raw = decoded.raw_bytes
    if raw is None or len(raw) < 2:
        return False

    first = raw[0]
    prefix_start = matcher_args["prefix_start"]
    prefix_end = matcher_args["prefix_end"]
    assert isinstance(prefix_start, int)
    assert isinstance(prefix_end, int)
    if not prefix_start <= first <= prefix_end:
        return False

    second = raw[1]
    safe_values = matcher_args["safe_second_values"]
    safe_ranges = matcher_args["safe_second_ranges"]
    safe_exact_pairs = matcher_args["safe_exact_pairs"]
    assert isinstance(safe_values, list)
    assert isinstance(safe_ranges, list)
    assert isinstance(safe_exact_pairs, list)

    # Length policy (revised 2026-05-20 after the P-4 HW crash on
    # `add WA, imm16` = `D0 C8 lo hi`, a 4-byte instruction that the
    # earlier `if len(raw) != 2: return False` rule never reached):
    #
    # - 2-byte: the original r+r ALU forms (e.g. `D0 8B` = ld HL, WA).
    #   Always considered for matching after the safe-list filter.
    # - 4-byte with second byte in 0xC8..0xCF (ALU-imm sub-op range):
    #   `<prefix> <C8..CF> lo hi` is the ALU-with-imm16 form
    #   (`add/adc/sub/sbc/and/xor/or/cp <reg16>, imm16`). Confirmed
    #   silicon-broken on real NGPC for prefix 0xD0 (= WA target) by
    #   the 2026-05-20 HW crash on stargunner_j16_C4_phase4_BROKEN_HW_…ngc.
    #   Whether D1..D7 + ALU-imm is also broken is undetermined; the
    #   conservative position is to flag them too (CC900 emits 0 of any
    #   of these in production f_code), but the safe_second_ranges
    #   config in the entry decides per matcher.
    # - Other lengths with the same prefix: memory-form encodings
    #   (e.g. `D1 lo hi 20+r` = `ld R16, (abs16)` 4 bytes, or
    #   `D1 lo hi 08+r` = `ldw (abs16), R16` 4 bytes, or
    #   `D1 lo hi 0A lo2 hi2` = `cpw (abs16), imm16` 6 bytes).
    #   These have DIFFERENT semantics — second byte is the low byte
    #   of an absolute address, not a sub-op. They are documented
    #   safe and MUST NOT be flagged. Filtered out below by exact-
    #   length check (only len==2 OR len==4-with-alu-imm-subop).
    # The decoder now decodes 0xD0..0xD7 as a WORD MEMORY-addressing family
    # FIRST (HW-confirmed 2026-07-03). For a valid memory-form op the SECOND
    # byte is the low byte of an absolute address, not an ALU-imm sub-op --
    # e.g. `D1 CE 6F 04` = pushw (0x6FCE) has second byte 0xCE purely because
    # the address is 0x6FCE. Only the genuine toolchain mis-encode falls
    # through to the reg-direct path, which the decoder marks with a "!BROKEN"
    # warning. Gate the 4-byte ALU-imm classification on that warning so valid
    # memory ops whose address low byte lands in 0xC8..0xCF are not flagged.
    is_alu_imm_form = (
        len(raw) == 4
        and 0xC8 <= second <= 0xCF
        and "!BROKEN" in (decoded.warning or "")
    )
    if len(raw) != 2 and not is_alu_imm_form:
        return False

    for safe_first, safe_second in safe_exact_pairs:
        if first == safe_first and second == safe_second:
            return False
    if second in safe_values:
        return False
    for start, end in safe_ranges:
        if start <= second <= end:
            return False
    return True


def _matches_warning_contains(
    decoded: DecodeResult,
    matcher_args: dict[str, object],
) -> bool:
    warning = decoded.warning
    if warning is None:
        return False
    pattern = matcher_args["pattern"]
    assert isinstance(pattern, str)
    return pattern in warning


def _require_non_empty_str(
    payload: dict[str, object],
    field_name: str,
    *,
    context: str = "quirk database",
) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} field {field_name!r} must be a non-empty string")
    return value


def _optional_non_empty_str(
    payload: dict[str, object],
    field_name: str,
    *,
    context: str = "quirk database",
) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} field {field_name!r} must be a non-empty string")
    return value


def _require_int(
    payload: dict[str, object],
    field_name: str,
    *,
    context: str = "quirk database",
) -> int:
    value = payload.get(field_name)
    if not isinstance(value, int):
        raise ValueError(f"{context} field {field_name!r} must be an integer")
    return value


def _require_int_list(
    payload: dict[str, object],
    field_name: str,
    *,
    context: str = "quirk database",
) -> list[int]:
    value = payload.get(field_name)
    if not isinstance(value, list) or any(not isinstance(item, int) for item in value):
        raise ValueError(f"{context} field {field_name!r} must be a list of integers")
    return list(value)


def _require_quirk_sources(
    raw_entry: dict[str, object],
    *,
    context: str,
) -> tuple[QuirkSource, ...]:
    raw_sources = raw_entry.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError(
            f"{context} must define a non-empty sources list for source-attribution"
        )

    sources: list[QuirkSource] = []
    for source_index, raw_source in enumerate(raw_sources):
        if not isinstance(raw_source, dict):
            raise ValueError(
                f"{context} source #{source_index} must be an object"
            )
        document = _require_non_empty_str(
            raw_source, "document", context=f"{context} source #{source_index}"
        )
        section = _optional_non_empty_str(
            raw_source, "section", context=f"{context} source #{source_index}"
        )
        quote = _optional_non_empty_str(
            raw_source, "quote", context=f"{context} source #{source_index}"
        )
        sources.append(QuirkSource(document=document, section=section, quote=quote))
    return tuple(sources)


def _require_int_pair_list(
    payload: dict[str, object],
    field_name: str,
    *,
    context: str = "quirk database",
    allow_missing: bool = False,
) -> list[tuple[int, int]]:
    value = payload.get(field_name)
    if value is None and allow_missing:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{context} field {field_name!r} must be a list")

    pairs: list[tuple[int, int]] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], int)
            or not isinstance(item[1], int)
        ):
            raise ValueError(
                f"{context} field {field_name!r} item #{index} must be a two-integer list"
            )
        pairs.append((item[0], item[1]))
    return pairs

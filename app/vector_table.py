"""Cortex-M vector table validation.

The assembled image is read as a Cortex-M vector table starting at a
given address: every entry is a little-endian 32-bit word. Entry 0 is
the reset stack pointer (initial MSP); entry 1 is the reset handler;
later non-zero entries are exception/interrupt handlers and zero words
are empty slots.

Rules:

* every one of the four bytes of an entry must be present in the image;
  a partial entry is an ``incomplete_vector`` violation (missing byte
  ranges located) and is not interpreted;
* the stack pointer must be 8-byte aligned (``SP % 8 == 0``) and lie in
  a region marked ``ram``; the RAM end is inclusive, so a stack pointer
  equal to the region ``end`` is accepted;
* the reset vector must be non-zero (``reset_is_zero`` otherwise);
* every non-zero handler address must carry the Thumb bit (bit 0 set);
  after clearing that bit the address must fall in a region marked
  ``executable``.

Region definitions are checked exactly as for layout validation
(non-empty, non-overlapping half-open intervals); unsound definitions
skip the table-level checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .hex_parser import Segment
from .layout import check_region_definitions

WORD_SIZE = 4
STACK_ALIGNMENT = 8
THUMB_BIT = 1


@dataclass
class VectorRegion:
    """A half-open address window ``[start, end)`` with Cortex-M roles."""

    name: str
    start: int  # inclusive
    end: int  # exclusive
    ram: bool = False
    executable: bool = False

    @property
    def size(self) -> int:
        return self.end - self.start


@dataclass
class VectorViolation:
    """A vector-table problem located down to the entry/address."""

    kind: str  # incomplete_vector | stack_misaligned | stack_not_in_ram |
    # reset_is_zero | missing_thumb_bit | handler_not_executable |
    # invalid_region | region_overlap
    message: str
    entry: int | None = None
    address: int | None = None
    missing_ranges: list[tuple[int, int]] = field(default_factory=list)
    region: str | None = None


@dataclass
class VectorEntry:
    """Per-entry outcome and parsed value."""

    index: int
    address: int
    status: str  # "ok" | "empty" | "error"
    role: str  # "stack" | "reset" | "handler"
    value: int | None
    complete: bool
    missing_ranges: list[tuple[int, int]] = field(default_factory=list)
    violations: list[VectorViolation] = field(default_factory=list)


@dataclass
class VectorTableResult:
    ok: bool
    regions_valid: bool
    table_address: int
    vector_count: int
    entries: list[VectorEntry]
    violations: list[VectorViolation]


def _fmt(addr: int) -> str:
    return f"0x{addr:08X}"


def validate_vector_table(
    segments: list[Segment],
    table_start: int,
    vector_count: int,
    regions: list[VectorRegion],
) -> VectorTableResult:
    """Validate a Cortex-M vector table against RAM/executable regions.

    Region definitions are verified first with the shared definition
    check; when they are unsound no entry is inspected.
    """
    definition_violations = [
        VectorViolation(
            kind=v.kind,
            message=v.message,
            address=v.start,
            region=v.region,
        )
        for v in check_region_definitions(regions)
    ]
    if definition_violations:
        return VectorTableResult(
            ok=False,
            regions_valid=False,
            table_address=table_start,
            vector_count=vector_count,
            entries=[],
            violations=definition_violations,
        )

    image = _build_byte_map(segments)
    ram_regions = [r for r in regions if r.ram]
    code_regions = [r for r in regions if r.executable]

    entries: list[VectorEntry] = []
    violations: list[VectorViolation] = []

    for index in range(vector_count):
        entry_address = table_start + index * WORD_SIZE
        missing = _missing_ranges(image, entry_address, WORD_SIZE)
        role = "stack" if index == 0 else "reset" if index == 1 else "handler"
        entry = VectorEntry(
            index=index,
            address=entry_address,
            status="error" if missing else "ok",
            role=role,
            value=None,
            complete=not missing,
            missing_ranges=missing,
        )

        if missing:
            ranges = ", ".join(f"{_fmt(lo)}..{_fmt(hi - 1)}" for lo, hi in missing)
            violation = VectorViolation(
                kind="incomplete_vector",
                message=(
                    f"{role} vector at entry {index} ({_fmt(entry_address)}): "
                    f"missing {sum(hi - lo for lo, hi in missing)} of {WORD_SIZE} "
                    f"byte(s) at {ranges}"
                ),
                entry=index,
                address=entry_address,
                missing_ranges=missing,
            )
            entry.violations.append(violation)
            violations.append(violation)
            entries.append(entry)
            continue

        value = int.from_bytes(
            bytes(image[entry_address + off] for off in range(WORD_SIZE)), "little"
        )
        entry.value = value

        if role == "stack":
            if value % STACK_ALIGNMENT:
                violation = VectorViolation(
                    kind="stack_misaligned",
                    message=(
                        f"initial stack pointer {_fmt(value)} is not "
                        f"{STACK_ALIGNMENT}-byte aligned"
                    ),
                    entry=index,
                    address=entry_address,
                )
                entry.violations.append(violation)
                violations.append(violation)
            if not _contains_closed(ram_regions, value):
                violation = VectorViolation(
                    kind="stack_not_in_ram",
                    message=(
                        f"initial stack pointer {_fmt(value)} does not land in any "
                        f"ram region"
                    ),
                    entry=index,
                    address=entry_address,
                )
                entry.violations.append(violation)
                violations.append(violation)
        elif role == "reset":
            if value == 0:
                violation = VectorViolation(
                    kind="reset_is_zero",
                    message="reset vector is zero: a reset handler must be defined",
                    entry=index,
                    address=entry_address,
                )
                entry.violations.append(violation)
                violations.append(violation)
            else:
                _check_handler(entry, value, code_regions, violations)
        else:
            if value == 0:
                # Zero vectors beyond the reset handler are empty slots.
                entry.status = "empty"
            else:
                _check_handler(entry, value, code_regions, violations)

        if entry.violations:
            entry.status = "error"
        entries.append(entry)

    return VectorTableResult(
        ok=not violations,
        regions_valid=True,
        table_address=table_start,
        vector_count=vector_count,
        entries=entries,
        violations=violations,
    )


def _check_handler(entry: VectorEntry, value: int, code_regions, violations) -> None:
    """Validate Thumb bit and executable-region placement of one handler."""
    target = value & ~THUMB_BIT
    if not (value & THUMB_BIT):
        violation = VectorViolation(
            kind="missing_thumb_bit",
            message=(
                f"{entry.role} vector at entry {entry.index} ({_fmt(value)}) is "
                "missing the Thumb bit (bit 0 must be 1)"
            ),
            entry=entry.index,
            address=entry.address,
        )
        entry.violations.append(violation)
        violations.append(violation)
    if not _contains_half_open(code_regions, target):
        violation = VectorViolation(
            kind="handler_not_executable",
            message=(
                f"{entry.role} vector at entry {entry.index}: target {_fmt(target)} "
                "(Thumb bit cleared) is not inside any executable region"
            ),
            entry=entry.index,
            address=entry.address,
        )
        entry.violations.append(violation)
        violations.append(violation)


def _build_byte_map(segments: list[Segment]) -> dict[int, int]:
    """Flatten assembled segments into an address -> byte mapping."""
    image: dict[int, int] = {}
    for segment in segments:
        for offset, value in enumerate(segment.data):
            image[segment.start + offset] = value
    return image


def _missing_ranges(image: dict[int, int], start: int, size: int) -> list[tuple[int, int]]:
    """Return merged half-open ranges of absent bytes in ``[start, start+size)``."""
    missing = sorted(start + off for off in range(size) if start + off not in image)
    if not missing:
        return []
    ranges: list[tuple[int, int]] = []
    run_start = previous = missing[0]
    for addr in missing[1:]:
        if addr == previous + 1:
            previous = addr
            continue
        ranges.append((run_start, previous + 1))
        run_start = previous = addr
    ranges.append((run_start, previous + 1))
    return ranges


def _contains_closed(regions: list, addr: int) -> bool:
    """True when ``region.start <= addr <= region.end`` (RAM end inclusive)."""
    return any(r.start <= addr <= r.end for r in regions)


def _contains_half_open(regions: list, addr: int) -> bool:
    """True when ``region.start <= addr < region.end``."""
    return any(r.start <= addr < r.end for r in regions)

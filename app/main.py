"""FastAPI firmware image assembly service."""

from __future__ import annotations

import base64

from fastapi import Body, FastAPI
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .hex_parser import ParseResult, parse_intel_hex
from .layout import Region, validate_layout
from .relocation import plan_relocation
from .flash_update import plan_flash_update
from .vector_table import VectorRegion, validate_vector_table

app = FastAPI(
    title="Firmware Image Assembly Service",
    version="1.0.0",
    description=(
        "Accepts Intel HEX text, validates every record (byte count and "
        "two's-complement checksum; record types 00/01/02/03/04/05), assembles "
        "the firmware image and returns contiguous Base64 segments, "
        "inter-segment gaps, the entry address and per-line diagnostics. "
        "The assembled image can also be validated against a set of named, "
        "non-overlapping memory regions (data/entry permissions, per-region "
        "occupancy and utilization), relocated as a whole by one integer "
        "delta whose feasible range is computed against those regions, "
        "diffed against another image to plan NOR-flash erase/write steps, "
        "or read as a Cortex-M vector table checked against ram/executable "
        "memory regions."
    ),
)


class AssembleRequest(BaseModel):
    hex_text: str = Field(..., description="Intel HEX text, one record per line")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "hex_text": (
                        ":020000040800F2\n"
                        ":10000000214601360121470136007EFE09D2190141\n"
                        ":04000005080001C12D\n"
                        ":00000001FF\n"
                    )
                }
            ]
        }
    }


class DiagnosticOut(BaseModel):
    line: int = Field(..., description="1-based line number; 0 marks a file-level issue")
    status: str = Field(..., description="ok | warning | error")
    record_type: str | None = None
    address: int | None = Field(None, description="Absolute address the record refers to, if any")
    byte_count: int | None = None
    message: str


class SegmentOut(BaseModel):
    start: int = Field(..., description="Absolute start address")
    end: int = Field(..., description="Exclusive end address")
    size: int
    data_base64: str = Field(..., description="Base64-encoded contiguous bytes")


class GapOut(BaseModel):
    start: int = Field(..., description="First unwritten address after the previous segment")
    end: int = Field(..., description="Start address of the next segment (exclusive)")
    size: int


class AssembleResponse(BaseModel):
    ok: bool = Field(..., description="True when no error-level diagnostic was raised")
    error_count: int
    warning_count: int
    entry_address: int | None = Field(None, description="From the last 03/05 record, if present")
    total_bytes: int
    segment_count: int
    segments: list[SegmentOut]
    gaps: list[GapOut]
    diagnostics: list[DiagnosticOut]


class RegionIn(BaseModel):
    name: str = Field(..., min_length=1, description="Region label")
    start: int = Field(..., ge=0, description="Inclusive start address")
    end: int = Field(..., ge=0, description="Exclusive end address (half-open interval)")
    allow_data: bool = Field(True, description="Whether firmware bytes may occupy this region")
    allow_entry: bool = Field(False, description="Whether the entry address may land in this region")


class ValidateLayoutRequest(BaseModel):
    hex_text: str = Field(..., description="Intel HEX text, one record per line")
    regions: list[RegionIn] = Field(
        ..., description="Memory regions; must be non-empty, non-overlapping half-open intervals"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "hex_text": (
                        ":020000040800F2\n"
                        ":10000000214601360121470136007EFE09D2190141\n"
                        ":04000005080001C12D\n"
                        ":00000001FF\n"
                    ),
                    "regions": [
                        {
                            "name": "flash",
                            "start": 134217728,
                            "end": 134479872,
                            "allow_data": True,
                            "allow_entry": True,
                        },
                        {
                            "name": "config",
                            "start": 134479872,
                            "end": 134484992,
                            "allow_data": True,
                            "allow_entry": False,
                        },
                    ],
                }
            ]
        }
    }


class FeasibleRangeOut(BaseModel):
    min_delta: int = Field(..., description="Most negative feasible delta (inclusive)")
    max_delta: int = Field(..., description="Most positive feasible delta (inclusive)")


class PlanRelocationRequest(BaseModel):
    hex_text: str = Field(..., description="Intel HEX text, one record per line")
    regions: list[RegionIn] = Field(
        ..., description="Memory regions; must be non-empty, non-overlapping half-open intervals"
    )
    max_abs_shift: int | None = Field(
        None,
        ge=0,
        description="Optional upper bound on |delta|; omitted means unconstrained",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "hex_text": (
                        ":020000040800F2\n"
                        ":10000000214601360121470136007EFE09D2190141\n"
                        ":04000005080001C12D\n"
                        ":00000001FF\n"
                    ),
                    "regions": [
                        {
                            "name": "flash",
                            "start": 134217728,
                            "end": 134610944,
                            "allow_data": True,
                            "allow_entry": True,
                        }
                    ],
                    "max_abs_shift": 262144,
                }
            ]
        }
    }


class PlanRelocationResponse(BaseModel):
    ok: bool = Field(..., description="True when the HEX is valid and no region-definition error exists")
    hex_ok: bool = Field(..., description="True when the Intel HEX text passed validation")
    regions_valid: bool | None = Field(
        ...,
        description="True when region definitions are non-empty and non-overlapping; "
        "null when the check did not run (invalid HEX)",
    )
    error_count: int
    warning_count: int
    entry_address: int | None = Field(
        None, description="Original entry from the last 03/05 record, if present"
    )
    total_bytes: int
    max_abs_shift: int | None = Field(None, description="The requested |delta| cap, if any")
    feasible: bool = Field(..., description="True when at least one delta satisfies every constraint")
    delta: int | None = Field(
        None, description="Chosen signed shift: smallest |delta|, ties towards the smaller value"
    )
    abs_shift: int | None = Field(None, description="|delta| of the chosen relocation")
    feasible_ranges: list[FeasibleRangeOut] = Field(
        ..., description="Merged closed integer intervals of every feasible delta"
    )
    relocated_entry_address: int | None = Field(
        None, description="Entry after applying delta, when feasible"
    )
    relocated_segments: list[SegmentOut] = Field(
        ..., description="Segments after applying delta, when feasible; empty otherwise"
    )
    violations: list[ViolationOut] = Field(
        ..., description="Region-definition violations (invalid_region / region_overlap), if any"
    )
    diagnostics: list[DiagnosticOut]


class ViolationOut(BaseModel):
    kind: str = Field(
        ...,
        description=(
            "uncovered_data | data_not_allowed | entry_uncovered | "
            "entry_not_allowed | invalid_region | region_overlap"
        ),
    )
    message: str
    start: int | None = Field(None, description="First affected address, if applicable")
    end: int | None = Field(None, description="Exclusive end of the affected range, if applicable")
    region: str | None = Field(None, description="Region the violation is tied to, if any")


class RegionReportOut(BaseModel):
    name: str
    start: int
    end: int
    size: int
    allow_data: bool
    allow_entry: bool
    occupied_bytes: int = Field(..., description="Firmware bytes that fall inside this region")
    utilization: float = Field(..., description="occupied_bytes / size (0 for empty regions)")
    violations: list[ViolationOut]


class ValidateLayoutResponse(BaseModel):
    ok: bool = Field(..., description="True when the HEX is valid and no layout violation was found")
    hex_ok: bool = Field(..., description="True when the Intel HEX text passed validation")
    regions_valid: bool | None = Field(
        ...,
        description="True when region definitions are non-empty and non-overlapping; "
        "null when the layout check did not run (invalid HEX)",
    )
    error_count: int
    warning_count: int
    entry_address: int | None = Field(None, description="From the last 03/05 record, if present")
    total_bytes: int
    regions: list[RegionReportOut]
    violations: list[ViolationOut] = Field(
        ..., description="Every violation, including uncovered ranges (region = null)"
    )
    diagnostics: list[DiagnosticOut]


class PlanFlashUpdateRequest(BaseModel):
    current_hex: str = Field(..., description="Intel HEX text of the image currently in flash")
    target_hex: str = Field(..., description="Intel HEX text of the image to reach")
    flash_start: int = Field(..., ge=0, description="Inclusive start of the flash window")
    flash_end: int = Field(..., ge=0, description="Exclusive end of the flash window")
    erase_block_size: int = Field(
        ..., ge=1, description="Uniform erase-sector size in bytes; flash_start/end must align to it"
    )
    max_write_size: int = Field(
        ..., ge=1, description="Maximum bytes per write; writes never cross an erase block"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "current_hex": ":10000000000102030405060708090A0B0C0D0E0FA2\n:00000001FF\n",
                    "target_hex": ":10000000FF0102030405060708090A0B0C0D0E0F0F\n:00000001FF\n",
                    "flash_start": 0,
                    "flash_end": 256,
                    "erase_block_size": 64,
                    "max_write_size": 16,
                }
            ]
        }
    }


class FlashParamErrorOut(BaseModel):
    kind: str = Field(
        ...,
        description="invalid_flash_range | erase_block_unaligned | "
        "invalid_max_write_size | image_outside_flash",
    )
    message: str
    start: int | None = Field(None, description="First affected address, if applicable")
    end: int | None = Field(None, description="Exclusive end of the affected range, if applicable")


class FlashBlockOut(BaseModel):
    address: int = Field(..., description="Erase-block start address")
    size: int = Field(..., description="Always erase_block_size")
    erase: bool = Field(..., description="Whether the block must be erased")
    erase_size: int | None = Field(None, description="erase_block_size when erased, else null")
    write_bytes: int = Field(..., description="Bytes written inside this block")
    changed_bytes: int = Field(..., description="Addresses whose target differs from current")
    zero_to_one_bytes: int = Field(
        ..., description="Changed addresses with at least one 0 -> 1 bit transition"
    )


class FlashOperationOut(BaseModel):
    kind: str = Field(..., description="erase | write")
    address: int = Field(..., description="Operation start address (erase == block start)")
    end: int = Field(..., description="Exclusive end address")
    size: int
    block: int = Field(..., description="Start address of the owning erase block")
    data_base64: str | None = Field(
        None, description="Target bytes for a write op; null for erase ops"
    )


class PlanFlashUpdateResponse(BaseModel):
    ok: bool = Field(
        ..., description="True when both HEX images and every parameter are valid and a plan exists"
    )
    current_hex_ok: bool = Field(..., description="True when current_hex passed validation")
    target_hex_ok: bool = Field(..., description="True when target_hex passed validation")
    params_valid: bool | None = Field(
        ...,
        description="True when the flash window/alignment parameters and image placement are "
        "valid; null when the parameter check did not run (either HEX invalid)",
    )
    flash_start: int
    flash_end: int
    erase_block_size: int
    max_write_size: int
    error_count: int = Field(..., description="Error diagnostics across both HEX texts")
    warning_count: int = Field(..., description="Warning diagnostics across both HEX texts")
    current_diagnostics: list[DiagnosticOut]
    target_diagnostics: list[DiagnosticOut]
    param_errors: list[FlashParamErrorOut] = Field(
        ..., description="Parameter-level problems; non-empty only when params_valid is false"
    )
    affected_blocks: list[FlashBlockOut] = Field(..., description="Blocks changed by the update")
    operations: list[FlashOperationOut] = Field(
        ..., description="Ordered erase/write steps: address order, erase before writes in a block"
    )
    affected_block_count: int
    erase_count: int
    erase_bytes: int
    write_count: int
    write_bytes: int = Field(..., description="Sum of bytes carried by write operations")
    changed_bytes: int = Field(..., description="Addresses where current and target differ")
    zero_to_one_bytes: int = Field(
        ..., description="Changed addresses requiring erase (any 0 -> 1 bit transition)"
    )


class VectorRegionIn(BaseModel):
    name: str = Field(..., min_length=1, description="Region label")
    start: int = Field(..., ge=0, description="Inclusive start address")
    end: int = Field(..., ge=0, description="Exclusive end address (half-open interval)")
    ram: bool = Field(False, description="Whether this region is RAM (initial SP must land in one)")
    executable: bool = Field(
        False, description="Whether vector targets may execute from this region"
    )


class ValidateVectorTableRequest(BaseModel):
    hex_text: str = Field(..., description="Intel HEX text, one record per line")
    vector_table_start: int = Field(..., ge=0, description="Absolute address of vector 0")
    vector_count: int = Field(
        ..., ge=2, description="Number of 32-bit vectors; at least initial SP + reset"
    )
    regions: list[VectorRegionIn] = Field(
        ...,
        description="Memory regions with ram/executable attributes; "
        "non-overlapping half-open intervals",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "hex_text": (
                        ":020000040800F2\n"
                        ":1000000000000220010100080000000001020008B9\n"
                        ":00000001FF\n"
                    ),
                    "vector_table_start": 134217728,
                    "vector_count": 4,
                    "regions": [
                        {
                            "name": "flash",
                            "start": 134217728,
                            "end": 134479872,
                            "ram": False,
                            "executable": True,
                        },
                        {
                            "name": "ram",
                            "start": 536870912,
                            "end": 537001984,
                            "ram": True,
                            "executable": False,
                        },
                    ],
                }
            ]
        }
    }


class VectorEntryOut(BaseModel):
    index: int = Field(..., description="Vector number (0 = initial SP, 1 = reset)")
    address: int = Field(..., description="Absolute address of this entry in the image")
    role: str = Field(..., description="initial_sp | reset | handler")
    status: str = Field(..., description="ok | empty | error")
    value: int | None = Field(
        None, description="Parsed little-endian 32-bit word; null when bytes are missing"
    )
    target_address: int | None = Field(
        None, description="value with the Thumb bit cleared (non-zero handlers)"
    )
    missing_bytes: list[int] = Field(
        ..., description="Absolute addresses of the bytes absent from the image"
    )
    message: str


class VectorViolationOut(BaseModel):
    kind: str = Field(
        ...,
        description=(
            "invalid_region | region_overlap | missing_vector_bytes | "
            "stack_misaligned | stack_outside_ram | reset_vector_zero | "
            "thumb_bit_missing | address_not_executable"
        ),
    )
    message: str
    index: int | None = Field(None, description="Vector entry the violation is tied to, if any")
    address: int | None = Field(None, description="Address the violation refers to, if any")
    region: str | None = Field(None, description="Region the violation is tied to, if any")


class ValidateVectorTableResponse(BaseModel):
    ok: bool = Field(
        ..., description="True when the HEX and regions are valid and no vector violation was found"
    )
    hex_ok: bool = Field(..., description="True when the Intel HEX text passed validation")
    regions_valid: bool | None = Field(
        ...,
        description="True when region definitions are non-empty and non-overlapping; "
        "null when the check did not run (invalid HEX)",
    )
    error_count: int
    warning_count: int
    entry_address: int | None = Field(None, description="From the last 03/05 record, if present")
    total_bytes: int
    vector_table_start: int
    vector_count: int
    vectors: list[VectorEntryOut] = Field(
        ..., description="Per-entry status and parsed value; empty when validation did not run"
    )
    violations: list[VectorViolationOut] = Field(
        ..., description="Every violation: region-definition problems and per-vector failures"
    )
    diagnostics: list[DiagnosticOut]


def _build_response(result: ParseResult) -> AssembleResponse:
    return AssembleResponse(
        ok=result.ok,
        error_count=result.error_count,
        warning_count=result.warning_count,
        entry_address=result.entry_address,
        total_bytes=result.total_bytes,
        segment_count=len(result.segments),
        segments=[
            SegmentOut(
                start=s.start,
                end=s.end,
                size=len(s.data),
                data_base64=base64.b64encode(s.data).decode("ascii"),
            )
            for s in result.segments
        ],
        gaps=[GapOut(start=g.start, end=g.end, size=g.size) for g in result.gaps],
        diagnostics=[DiagnosticOut(**vars(d)) for d in result.diagnostics],
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/assemble", response_model=AssembleResponse, tags=["assemble"])
def assemble(request: AssembleRequest) -> AssembleResponse:
    """Assemble a firmware image from Intel HEX text sent as JSON."""
    return _build_response(parse_intel_hex(request.hex_text))


@app.post("/api/v1/assemble/raw", response_model=AssembleResponse, tags=["assemble"])
def assemble_raw(hex_text: str = Body(..., media_type="text/plain")) -> AssembleResponse:
    """Assemble a firmware image from a raw Intel HEX text body (text/plain)."""
    return _build_response(parse_intel_hex(hex_text))


@app.post("/api/v1/validate-layout", response_model=ValidateLayoutResponse, tags=["layout"])
def validate_layout_route(request: ValidateLayoutRequest) -> ValidateLayoutResponse:
    """Validate an Intel HEX image against named memory regions.

    When the HEX text fails validation only parse diagnostics are returned
    (``regions``/``violations`` empty, ``regions_valid`` null). Otherwise the
    contiguous firmware segments are split at region boundaries and checked
    for coverage, ``allow_data``/``allow_entry`` permissions, and per-region
    occupancy is reported.
    """
    parsed = parse_intel_hex(request.hex_text)
    diagnostics = [DiagnosticOut(**vars(d)) for d in parsed.diagnostics]
    parse_fields = dict(
        error_count=parsed.error_count,
        warning_count=parsed.warning_count,
        entry_address=parsed.entry_address,
        total_bytes=parsed.total_bytes,
        diagnostics=diagnostics,
    )
    if not parsed.ok:
        return ValidateLayoutResponse(
            ok=False,
            hex_ok=False,
            regions_valid=None,
            regions=[],
            violations=[],
            **parse_fields,
        )

    layout = validate_layout(
        parsed.segments,
        parsed.entry_address,
        [
            Region(
                name=r.name,
                start=r.start,
                end=r.end,
                allow_data=r.allow_data,
                allow_entry=r.allow_entry,
            )
            for r in request.regions
        ],
    )
    return ValidateLayoutResponse(
        ok=layout.ok,
        hex_ok=True,
        regions_valid=layout.regions_valid,
        regions=[
            RegionReportOut(
                name=rep.region.name,
                start=rep.region.start,
                end=rep.region.end,
                size=rep.region.size,
                allow_data=rep.region.allow_data,
                allow_entry=rep.region.allow_entry,
                occupied_bytes=rep.occupied_bytes,
                utilization=rep.utilization,
                violations=[ViolationOut(**vars(v)) for v in rep.violations],
            )
            for rep in layout.reports
        ],
        violations=[ViolationOut(**vars(v)) for v in layout.violations],
        **parse_fields,
    )


@app.post("/api/v1/plan-relocation", response_model=PlanRelocationResponse, tags=["layout"])
def plan_relocation_route(request: PlanRelocationRequest) -> PlanRelocationResponse:
    """Plan a whole-image relocation by a single integer ``delta``.

    The HEX text is parsed and the region definitions are validated
    exactly as for ``/api/v1/validate-layout``; if either fails only the
    corresponding diagnostics/violations are returned and no planning
    runs. Otherwise every feasible delta is found: each firmware segment
    must fall wholly inside an ``allow_data`` region, the entry (if any)
    inside an ``allow_entry`` region, relocated addresses must remain
    32-bit, and ``|delta|`` must not exceed ``max_abs_shift`` when given.
    The returned ``delta`` minimizes ``|delta|`` and breaks ties towards
    the numerically smaller value; when no delta exists ``feasible`` is
    false and the relocated image is omitted.
    """
    parsed = parse_intel_hex(request.hex_text)
    diagnostics = [DiagnosticOut(**vars(d)) for d in parsed.diagnostics]
    parse_fields = dict(
        error_count=parsed.error_count,
        warning_count=parsed.warning_count,
        entry_address=parsed.entry_address,
        total_bytes=parsed.total_bytes,
        diagnostics=diagnostics,
    )
    if not parsed.ok:
        return PlanRelocationResponse(
            ok=False,
            hex_ok=False,
            regions_valid=None,
            feasible=False,
            feasible_ranges=[],
            relocated_segments=[],
            violations=[],
            max_abs_shift=request.max_abs_shift,
            **parse_fields,
        )

    regions = [
        Region(
            name=r.name,
            start=r.start,
            end=r.end,
            allow_data=r.allow_data,
            allow_entry=r.allow_entry,
        )
        for r in request.regions
    ]
    plan = plan_relocation(
        parsed.segments,
        parsed.entry_address,
        regions,
        request.max_abs_shift,
    )
    if not plan.regions_valid:
        return PlanRelocationResponse(
            ok=False,
            hex_ok=True,
            regions_valid=False,
            feasible=False,
            feasible_ranges=[],
            relocated_segments=[],
            violations=[ViolationOut(**vars(v)) for v in plan.region_violations],
            max_abs_shift=request.max_abs_shift,
            **parse_fields,
        )

    return PlanRelocationResponse(
        ok=True,
        hex_ok=True,
        regions_valid=True,
        feasible=plan.feasible,
        feasible_ranges=[
            FeasibleRangeOut(min_delta=lo, max_delta=hi) for lo, hi in plan.feasible_ranges
        ],
        delta=plan.delta,
        abs_shift=plan.abs_shift,
        relocated_entry_address=plan.entry_address,
        relocated_segments=[
            SegmentOut(
                start=s.start,
                end=s.end,
                size=len(s.data),
                data_base64=base64.b64encode(s.data).decode("ascii"),
            )
            for s in plan.segments
        ],
        violations=[],
        max_abs_shift=request.max_abs_shift,
        **parse_fields,
    )


@app.post("/api/v1/plan-flash-update", response_model=PlanFlashUpdateResponse, tags=["layout"])
def plan_flash_update_route(request: PlanFlashUpdateRequest) -> PlanFlashUpdateResponse:
    """Plan erase/write operations turning ``current_hex`` into ``target_hex``.

    Both Intel HEX texts are parsed with the shared validator; when
    either fails, its diagnostics are returned and no plan is made.
    Otherwise the flash window must be a non-empty 32-bit half-open
    interval whose start and end are aligned to ``erase_block_size``,
    and both images must lie wholly inside it. Missing addresses read
    as ``0xFF``. Blocks containing any byte with a ``0 -> 1`` bit
    transition are erased and rewritten with every non-``0xFF`` target
    byte; other blocks receive only the changed bytes. Pending writes
    are merged into contiguous runs and split at ``max_write_size`` and
    at erase-block boundaries.
    """
    current = parse_intel_hex(request.current_hex)
    target = parse_intel_hex(request.target_hex)
    error_count = current.error_count + target.error_count
    warning_count = current.warning_count + target.warning_count

    base_fields = dict(
        flash_start=request.flash_start,
        flash_end=request.flash_end,
        erase_block_size=request.erase_block_size,
        max_write_size=request.max_write_size,
        error_count=error_count,
        warning_count=warning_count,
        current_diagnostics=[DiagnosticOut(**vars(d)) for d in current.diagnostics],
        target_diagnostics=[DiagnosticOut(**vars(d)) for d in target.diagnostics],
    )

    if not (current.ok and target.ok):
        return PlanFlashUpdateResponse(
            ok=False,
            current_hex_ok=current.ok,
            target_hex_ok=target.ok,
            params_valid=None,
            param_errors=[],
            affected_blocks=[],
            operations=[],
            affected_block_count=0,
            erase_count=0,
            erase_bytes=0,
            write_count=0,
            write_bytes=0,
            changed_bytes=0,
            zero_to_one_bytes=0,
            **base_fields,
        )

    plan = plan_flash_update(
        current.segments,
        target.segments,
        request.flash_start,
        request.flash_end,
        request.erase_block_size,
        request.max_write_size,
    )
    if not plan.params_valid:
        return PlanFlashUpdateResponse(
            ok=False,
            current_hex_ok=True,
            target_hex_ok=True,
            params_valid=False,
            param_errors=[FlashParamErrorOut(**vars(e)) for e in plan.param_errors],
            affected_blocks=[],
            operations=[],
            affected_block_count=0,
            erase_count=0,
            erase_bytes=0,
            write_count=0,
            write_bytes=0,
            changed_bytes=0,
            zero_to_one_bytes=0,
            **base_fields,
        )

    return PlanFlashUpdateResponse(
        ok=True,
        current_hex_ok=True,
        target_hex_ok=True,
        params_valid=True,
        param_errors=[],
        affected_blocks=[
            FlashBlockOut(
                address=b.address,
                size=b.size,
                erase=b.erase,
                erase_size=b.erase_size,
                write_bytes=b.write_bytes,
                changed_bytes=b.changed_bytes,
                zero_to_one_bytes=b.zero_to_one_bytes,
            )
            for b in plan.blocks
        ],
        operations=[
            FlashOperationOut(
                kind=op.kind,
                address=op.address,
                end=op.end,
                size=op.size,
                block=op.block,
                data_base64=(
                    None
                    if op.kind == "erase"
                    else base64.b64encode(op.data).decode("ascii")
                ),
            )
            for op in plan.operations
        ],
        affected_block_count=plan.affected_block_count,
        erase_count=plan.erase_count,
        erase_bytes=plan.erase_bytes,
        write_count=plan.write_count,
        write_bytes=plan.write_bytes,
        changed_bytes=plan.changed_bytes,
        zero_to_one_bytes=plan.zero_to_one_bytes,
        **base_fields,
    )


@app.post(
    "/api/v1/validate-vector-table",
    response_model=ValidateVectorTableResponse,
    tags=["vector-table"],
)
def validate_vector_table_route(
    request: ValidateVectorTableRequest,
) -> ValidateVectorTableResponse:
    """Validate a Cortex-M vector table read from the assembled image.

    The HEX text is parsed with the shared validator; when it fails only
    parse diagnostics are returned (``vectors``/``violations`` empty,
    ``regions_valid`` null). Region definitions must be non-empty,
    non-overlapping half-open intervals; unsound definitions abort the
    check as well. Otherwise ``vector_count`` little-endian 32-bit words
    are read from ``vector_table_start``: any missing byte is reported
    with its entry index and address; entry 0 (initial SP) must be
    8-byte aligned and land in a ``ram`` region (the RAM end address
    itself is allowed); entry 1 (reset) must be non-zero; other zero
    vectors are accepted as empty slots. Every non-zero processor
    address must carry the Thumb bit, and the address with that bit
    cleared must fall inside an ``executable`` region.
    """
    parsed = parse_intel_hex(request.hex_text)
    diagnostics = [DiagnosticOut(**vars(d)) for d in parsed.diagnostics]
    parse_fields = dict(
        error_count=parsed.error_count,
        warning_count=parsed.warning_count,
        entry_address=parsed.entry_address,
        total_bytes=parsed.total_bytes,
        vector_table_start=request.vector_table_start,
        vector_count=request.vector_count,
        diagnostics=diagnostics,
    )
    if not parsed.ok:
        return ValidateVectorTableResponse(
            ok=False,
            hex_ok=False,
            regions_valid=None,
            vectors=[],
            violations=[],
            **parse_fields,
        )

    result = validate_vector_table(
        parsed.segments,
        request.vector_table_start,
        request.vector_count,
        [
            VectorRegion(
                name=r.name,
                start=r.start,
                end=r.end,
                ram=r.ram,
                executable=r.executable,
            )
            for r in request.regions
        ],
    )
    return ValidateVectorTableResponse(
        ok=result.ok,
        hex_ok=True,
        regions_valid=result.regions_valid,
        vectors=[VectorEntryOut(**vars(e)) for e in result.entries],
        violations=[VectorViolationOut(**vars(v)) for v in result.violations],
        **parse_fields,
    )

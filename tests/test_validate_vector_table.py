"""Tests for the /api/v1/validate-vector-table endpoint and its logic."""

from fastapi.testclient import TestClient

from app.hex_parser import Segment, parse_intel_hex
from app.main import app
from app.vector_table import VectorRegion, validate_vector_table

client = TestClient(app)


def rec(rectype: int, addr: int, data: bytes = b"") -> str:
    """Build a valid Intel HEX record line."""
    body = bytes([len(data), (addr >> 8) & 0xFF, addr & 0xFF, rectype]) + bytes(data)
    checksum = (-sum(body)) & 0xFF
    return ":" + (body + bytes([checksum])).hex().upper()


EOF = rec(0x01, 0)
LINEAR_BASE = rec(0x04, 0, b"\x08\x00")  # base 0x0800_0000


def data_rec(addr: int, data: bytes) -> str:
    return rec(0x00, addr, data)


def vector_region(name, start, end, ram=False, executable=False):
    return {
        "name": name,
        "start": start,
        "end": end,
        "ram": ram,
        "executable": executable,
    }


FLASH = (0x08000000, 0x08040000)
SRAM = (0x20000000, 0x20008000)


def vector_hex(words: list[int], *, table_start: int = 0x08000000) -> str:
    """Build a HEX text placing little-endian words at table_start."""
    base = (table_start >> 16) & 0xFFFF
    offset = table_start & 0xFFFF
    lines = []
    if base:
        lines.append(rec(0x04, 0, base.to_bytes(2, "big")))
    payload = b"".join(w.to_bytes(4, "little") for w in words)
    lines.append(data_rec(offset, payload))
    lines.append(EOF)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# pure logic
# ---------------------------------------------------------------------------

def test_valid_table_sp_ram_and_thumb_handlers():
    parsed = parse_intel_hex(
        vector_hex(
            [
                0x20008000,  # SP at top of RAM (end inclusive)
                0x08000009,  # reset handler, Thumb bit set
                0x0800000D,  # IRQ handler
                0x00000000,  # empty slot
            ]
        )
    )
    result = validate_vector_table(
        parsed.segments,
        0x08000000,
        4,
        [
            VectorRegion("sram", *SRAM, ram=True),
            VectorRegion("flash", *FLASH, executable=True),
        ],
    )
    assert result.ok
    assert result.regions_valid
    assert [e.status for e in result.entries] == ["ok", "ok", "ok", "empty"]
    assert [e.role for e in result.entries] == ["stack", "reset", "handler", "handler"]
    assert [e.value for e in result.entries] == [
        0x20008000,
        0x08000009,
        0x0800000D,
        0,
    ]
    assert result.violations == []


def test_little_endian_word_decoding():
    seg = Segment(start=0x100, data=bytes.fromhex("00100020"))
    result = validate_vector_table(
        [seg],
        0x100,
        1,
        [VectorRegion("ram", 0x20000000, 0x20002000, ram=True)],
    )
    assert result.ok
    assert result.entries[0].value == 0x20001000


def test_partial_entry_locates_entry_and_missing_bytes():
    # only the first two bytes of entry 1 (at 0x104..0x107) are present
    seg = Segment(start=0x100, data=bytes.fromhex("001000200900"))
    result = validate_vector_table(
        [seg],
        0x100,
        2,
        [
            VectorRegion("ram", 0x20000000, 0x20002000, ram=True),
            VectorRegion("flash", 0x0000, 0x1000, executable=True),
        ],
    )
    assert not result.ok
    entry1 = result.entries[1]
    assert entry1.complete is False
    assert entry1.value is None
    assert entry1.status == "error"
    assert entry1.missing_ranges == [(0x106, 0x108)]
    v = result.violations[0]
    assert v.kind == "incomplete_vector"
    assert v.entry == 1
    assert v.address == 0x104
    assert v.missing_ranges == [(0x106, 0x108)]
    # the complete entry is still interpreted
    assert result.entries[0].value == 0x20001000


def test_missing_bytes_split_into_ranges():
    # byte layout: present, missing, missing, present -> two ranges
    seg = Segment(start=0x100, data=b"\x00")
    seg2 = Segment(start=0x103, data=b"\x20")
    result = validate_vector_table(
        [seg, seg2], 0x100, 1, [VectorRegion("ram", 0, 0x20010000, ram=True)]
    )
    entry = result.entries[0]
    assert entry.complete is False
    assert entry.missing_ranges == [(0x101, 0x103)]


def test_sp_must_be_8_byte_aligned():
    parsed = parse_intel_hex(vector_hex([0x20007FFC | 4, 0x08000009, 0, 0]))
    result = validate_vector_table(
        parsed.segments,
        0x08000000,
        4,
        [
            VectorRegion("sram", *SRAM, ram=True),
            VectorRegion("flash", *FLASH, executable=True),
        ],
    )
    kinds = [v.kind for v in result.violations]
    assert "stack_misaligned" in kinds
    assert "stack_not_in_ram" not in kinds
    assert result.entries[0].status == "error"


def test_sp_must_fall_in_ram_region():
    parsed = parse_intel_hex(vector_hex([0x1FFF8000, 0x08000009, 0, 0]))
    result = validate_vector_table(
        parsed.segments,
        0x08000000,
        4,
        [
            VectorRegion("sram", *SRAM, ram=True),
            VectorRegion("flash", *FLASH, executable=True),
        ],
    )
    assert [v.kind for v in result.violations] == ["stack_not_in_ram"]


def test_sp_equal_ram_end_is_allowed_but_one_past_is_not():
    regions = [
        VectorRegion("sram", *SRAM, ram=True),
        VectorRegion("flash", *FLASH, executable=True),
    ]
    ok = validate_vector_table(
        [Segment(0x08000000, (0x20008000).to_bytes(4, "little"))],
        0x08000000,
        1,
        regions,
    )
    assert ok.ok

    past = validate_vector_table(
        [Segment(0x08000000, (0x20008008).to_bytes(4, "little"))],
        0x08000000,
        1,
        regions,
    )
    assert [v.kind for v in past.violations] == ["stack_not_in_ram"]


def test_reset_vector_must_be_nonzero():
    parsed = parse_intel_hex(vector_hex([0x20008000, 0x00000000, 0, 0]))
    result = validate_vector_table(
        parsed.segments,
        0x08000000,
        4,
        [
            VectorRegion("sram", *SRAM, ram=True),
            VectorRegion("flash", *FLASH, executable=True),
        ],
    )
    assert result.entries[1].status == "error"
    assert [v.kind for v in result.violations] == ["reset_is_zero"]


def test_handler_requires_thumb_bit():
    parsed = parse_intel_hex(vector_hex([0x20008000, 0x08000008, 0, 0]))
    result = validate_vector_table(
        parsed.segments,
        0x08000000,
        4,
        [
            VectorRegion("sram", *SRAM, ram=True),
            VectorRegion("flash", *FLASH, executable=True),
        ],
    )
    assert [v.kind for v in result.violations] == ["missing_thumb_bit"]


def test_handler_target_must_be_executable():
    # Thumb bit set, cleared target 0x0800000C is inside flash but flash is
    # not marked executable here.
    parsed = parse_intel_hex(vector_hex([0x20008000, 0x08000009, 0x0800000D, 0]))
    result = validate_vector_table(
        parsed.segments,
        0x08000000,
        4,
        [
            VectorRegion("sram", *SRAM, ram=True),
            VectorRegion("flash", *FLASH),
        ],
    )
    assert [v.kind for v in result.violations] == [
        "handler_not_executable",
        "handler_not_executable",
    ]


def test_missing_thumb_and_non_executable_are_both_reported():
    parsed = parse_intel_hex(vector_hex([0x20008000, 0x08000008, 0, 0]))
    result = validate_vector_table(
        parsed.segments,
        0x08000000,
        4,
        [VectorRegion("sram", *SRAM, ram=True)],
    )
    assert sorted(v.kind for v in result.violations) == [
        "handler_not_executable",
        "missing_thumb_bit",
    ]


def test_region_can_be_both_ram_and_executable():
    parsed = parse_intel_hex(vector_hex([0x20008000, 0x08000009, 0, 0]))
    result = validate_vector_table(
        parsed.segments,
        0x08000000,
        4,
        [
            VectorRegion("sram", *SRAM, ram=True, executable=False),
            VectorRegion("flash", *FLASH, executable=True),
        ],
    )
    assert result.ok


def test_invalid_region_definitions_skip_entry_checks():
    parsed = parse_intel_hex(vector_hex([0xDEAD, 0, 0, 0]))
    result = validate_vector_table(
        parsed.segments,
        0x08000000,
        4,
        [
            VectorRegion("a", 0x00, 0x10),
            VectorRegion("b", 0x08, 0x20),
        ],
    )
    assert not result.ok
    assert result.regions_valid is False
    assert result.entries == []
    assert [v.kind for v in result.violations] == ["region_overlap"]


def test_empty_interval_region_is_invalid():
    parsed = parse_intel_hex(vector_hex([0x20008000, 0x08000009, 0, 0]))
    result = validate_vector_table(
        parsed.segments,
        0x08000000,
        4,
        [VectorRegion("bad", 0x10, 0x10)],
    )
    assert result.regions_valid is False
    assert result.violations[0].kind == "invalid_region"
    assert result.violations[0].region == "bad"


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------

def test_api_happy_path():
    resp = client.post(
        "/api/v1/validate-vector-table",
        json={
            "hex_text": vector_hex([0x20008000, 0x08000009, 0x0800000D, 0]),
            "table_start": 0x08000000,
            "vector_count": 4,
            "regions": [
                vector_region("sram", *SRAM, ram=True),
                vector_region("flash", *FLASH, executable=True),
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["hex_ok"] is True
    assert body["regions_valid"] is True
    assert body["table_start"] == 0x08000000
    assert body["vector_count"] == 4
    assert [e["status"] for e in body["entries"]] == ["ok", "ok", "ok", "empty"]
    assert body["entries"][1]["value"] == 0x08000009
    assert body["violations"] == []


def test_api_invalid_hex_returns_only_diagnostics():
    resp = client.post(
        "/api/v1/validate-vector-table",
        json={
            "hex_text": ":00000001FE\n",
            "table_start": 0x08000000,
            "vector_count": 2,
            "regions": [vector_region("flash", *FLASH, executable=True)],
        },
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["hex_ok"] is False
    assert body["regions_valid"] is None
    assert body["entries"] == []
    assert body["violations"] == []
    assert body["error_count"] >= 1


def test_api_incomplete_vector_reports_entry_and_ranges():
    # 6 bytes only -> entry 0 complete, entry 1 missing its last two bytes
    text = "\n".join([LINEAR_BASE, data_rec(0x0000, bytes.fromhex("008000200900")), EOF])
    resp = client.post(
        "/api/v1/validate-vector-table",
        json={
            "hex_text": text,
            "table_start": 0x08000000,
            "vector_count": 2,
            "regions": [
                vector_region("sram", *SRAM, ram=True),
                vector_region("flash", *FLASH, executable=True),
            ],
        },
    )
    body = resp.json()
    assert body["ok"] is False
    entry1 = body["entries"][1]
    assert entry1["complete"] is False
    assert entry1["value"] is None
    assert entry1["address"] == 0x08000004
    assert entry1["missing_ranges"] == [[0x08000006, 0x08000008]]
    violation = body["violations"][0]
    assert violation["kind"] == "incomplete_vector"
    assert violation["entry"] == 1
    assert violation["address"] == 0x08000004


def test_api_full_failure_summary():
    resp = client.post(
        "/api/v1/validate-vector-table",
        json={
            "hex_text": vector_hex([0x20000003, 0x08000008, 0x08000008, 0]),
            "table_start": 0x08000000,
            "vector_count": 4,
            "regions": [
                vector_region("sram", *SRAM, ram=True),
                vector_region("flash", *FLASH),  # not executable
            ],
        },
    )
    body = resp.json()
    assert body["ok"] is False
    kinds = sorted(v["kind"] for v in body["violations"])
    assert kinds == [
        "handler_not_executable",
        "handler_not_executable",
        "missing_thumb_bit",
        "missing_thumb_bit",
        "stack_misaligned",
    ]
    # per-entry violations match the summary
    per_entry = sorted(
        v["kind"] for e in body["entries"] for v in e["violations"]
    )
    assert per_entry == kinds


def test_api_overlapping_regions_skip_validation():
    resp = client.post(
        "/api/v1/validate-vector-table",
        json={
            "hex_text": vector_hex([0x20008000, 0x08000009, 0, 0]),
            "table_start": 0x08000000,
            "vector_count": 4,
            "regions": [
                vector_region("a", 0x00, 0x10, executable=True),
                vector_region("b", 0x08, 0x20, ram=True),
            ],
        },
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["hex_ok"] is True
    assert body["regions_valid"] is False
    assert body["entries"] == []
    assert [v["kind"] for v in body["violations"]] == ["region_overlap"]


def test_api_parameter_validation():
    payload = {
        "hex_text": EOF,
        "table_start": 0x08000000,
        "vector_count": 0,
        "regions": [],
    }
    assert client.post("/api/v1/validate-vector-table", json=payload).status_code == 422

    payload["vector_count"] = 2
    payload["regions"] = [{"name": "", "start": 0, "end": 16}]
    assert client.post("/api/v1/validate-vector-table", json=payload).status_code == 422

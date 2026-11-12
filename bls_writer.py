"""
Writer for J3D BLS (blend shape) files.

This module has no Blender dependency.  Positions passed here must already be
in J3D model space (Y-up), usually after applying the inverse axis conversion
and the ``Convert Units`` scale.
"""

from __future__ import annotations

import struct

from .bls_parser import name_hash


BLS_MAGIC = b"J3D1bls1"
BLS_SECTION_START = 0x20
BLS_SECTION_COUNT = 1
BLS_SECTION_MAGIC = b"CLS1"
BLS_HEADER_SIZE = 0x60

BLS_CLUSTER_ENTRY_SIZE = 0x24
BLS_KEY_ENTRY_SIZE = 0x0C
BLS_BLEND_ENTRY_SIZE = 0x0C

INDEX_MAX = 0x1FFF
SIGN_X = 0x8000
SIGN_Y = 0x4000
SIGN_Z = 0x2000


class BLSError(ValueError):
    """Raised when the data cannot be represented by the BLS format."""


def _align4(value):
    return (value + 3) & ~3


def _encode_name(name):
    try:
        return name.encode("shift_jis")
    except UnicodeEncodeError as exc:
        raise BLSError(f"Name cannot be encoded as Shift-JIS: {name!r}") from exc


def _name_table(names):
    """Build a J3D string table (count, hashes, offsets and strings)."""
    encoded = [_encode_name(name) + b"\x00" for name in names]
    count = len(encoded)
    strings = b"".join(encoded)
    header_size = 4 + 4 * count
    entries = bytearray()
    offset = header_size

    for name, raw in zip(names, encoded):
        if offset > 0xFFFF:
            raise BLSError("BLS name table offset exceeds 16 bits")
        entries += struct.pack(">HH", name_hash(name), offset)
        offset += len(raw)

    return (struct.pack(">HH", count, 0xFFFF) + bytes(entries) + strings)


def _make_encoder(label):
    """Create a shared absolute-vector pool encoder."""
    pool = []
    lookup = {}

    def encode(value):
        if len(value) != 3:
            raise BLSError(f"Each BLS {label} must contain three components")

        components = []
        signs = 0

        for axis, component in enumerate(value):
            component = float(component)

            if component < 0.0:
                signs |= (SIGN_X, SIGN_Y, SIGN_Z)[axis]
                component = -component

            components.append(component)

        key = tuple(components)
        index = lookup.get(key)

        if index is None:
            index = len(pool)

            if index > INDEX_MAX:
                raise BLSError(
                    f"BLS {label} pool exceeds 8192 entries; "
                    "select fewer shape keys"
                )

            lookup[key] = index
            pool.append(key)

        return index | signs

    return pool, encode


def write_bls(cluster_name, vertex_indices, base_positions, keys,
              base_name=None, base_normals=None, key_normals=None,
              cluster_vertices=None):
    """Create a BLS file with one cluster.

    Parameters
    ----------
    cluster_name:
        Name stored for the cluster.
    vertex_indices:
        Model vertex index for every cluster position.
    base_positions:
        Absolute base positions in J3D model space, one per vertex index.
    keys:
        Sequence of ``(name, positions)`` pairs.  Positions are absolute
        J3D model-space coordinates.
    base_name:
        Name of the trailing base key.  By default ``<cluster>__base``.
    base_normals:
        Optional normal for every cluster normal slot.
    key_normals:
        Optional normal arrays aligned with ``keys``.  Each array has one
        normal per cluster normal slot.
    cluster_vertices:
        Optional ``(source_indices, destination_indices)`` entries for the
        normal blend table.  Sources index the cluster normal slots; destinations
        index the model's normal array (``0xFFFF`` skips an entry).  When
        normals are supplied and this is omitted, each normal slot is copied
        1:1 to the same normal index.
    """
    vertex_indices = [int(index) for index in vertex_indices]

    if not vertex_indices:
        raise BLSError("Cannot export a BLS cluster without vertices")

    if len(vertex_indices) > 0xFFFF:
        raise BLSError("BLS supports at most 65535 cluster positions")

    if any(index < 0 or index > 0xFFFF for index in vertex_indices):
        raise BLSError("BLS vertex indices must fit in 16 bits")

    if len(base_positions) != len(vertex_indices):
        raise BLSError("Base position count does not match the vertex list")

    keys = list(keys)

    if not keys:
        raise BLSError("Select at least one shape key to export")

    for _name, positions in keys:
        if len(positions) != len(vertex_indices):
            raise BLSError("Key position count does not match the vertex list")

    if base_name is None:
        base_name = f"{cluster_name}__base"

    base_normals = list(base_normals or [])
    key_normals = [list(normals) for normals in (key_normals or [])]
    normal_slot_count = len(base_normals)

    if normal_slot_count and len(key_normals) != len(keys):
        raise BLSError("Normal key count does not match the key count")

    if any(len(normals) != normal_slot_count for normals in key_normals):
        raise BLSError("Normal slot count does not match the base normals")

    if not normal_slot_count:
        key_normals = []
        cluster_vertices = []

    if cluster_vertices is None and normal_slot_count:
        cluster_vertices = [([index], [index])
                            for index in range(normal_slot_count)]

    prepared_vertices = []

    for sources, destinations in (cluster_vertices or []):
        sources = [int(value) for value in sources]
        destinations = [int(value) for value in destinations]

        if len(sources) != len(destinations):
            raise BLSError(
                "Normal blend source and destination counts do not match")

        if any(value < 0 or value >= normal_slot_count for value in sources):
            raise BLSError("Normal blend source index is out of range")

        if any(value < 0 or value > 0xFFFF for value in destinations):
            raise BLSError("Normal blend destination index is out of range")

        prepared_vertices.append((sources, destinations))

    if len(prepared_vertices) > 0xFFFF:
        raise BLSError("BLS supports at most 65535 normal blend entries")

    pool, encode_position = _make_encoder("position")
    base_refs = [encode_position(position) for position in base_positions]
    merged_keys = []

    for name, positions in keys:
        merged_keys.append(
            (name, [encode_position(position) for position in positions])
        )

    if normal_slot_count:
        normal_pool, encode_normal = _make_encoder("normal")
        base_normal_refs = [
            encode_normal(normal) for normal in base_normals
        ]
        merged_key_normals = [
            [encode_normal(normal) for normal in normals]
            for normals in key_normals
        ]
    else:
        normal_pool = []
        base_normal_refs = []
        merged_key_normals = []

    key_names = [name for name, _refs in merged_keys] + [base_name]
    key_count = len(merged_keys) + 1
    position_count = len(pool)
    normal_count = len(normal_pool)
    vertex_count = len(vertex_indices)
    cluster_vertex_count = len(prepared_vertices)
    deform_flags = 3 if normal_slot_count else 2
    max_angle = 180.0 if normal_slot_count else 0.0
    min_angle = 180.0 if normal_slot_count else 0.0

    cluster_names = _name_table([cluster_name])
    key_names_table = _name_table(key_names)

    cluster_off = 0x40
    key_off = cluster_off + BLS_CLUSTER_ENTRY_SIZE
    key_pointer = key_off + BLS_KEY_ENTRY_SIZE * key_count
    blend_off = key_off + BLS_KEY_ENTRY_SIZE * key_count
    position_off = _align4(blend_off + 12 * cluster_vertex_count)
    normal_off = position_off + 12 * position_count

    data_off = _align4(normal_off + 12 * normal_count)
    key_position_offsets = []

    for _ in range(key_count):
        key_position_offsets.append(data_off)
        data_off = _align4(data_off + 2 * vertex_count)

    key_normal_offsets = []

    for _ in range(key_count):
        if normal_slot_count:
            key_normal_offsets.append(data_off)
            data_off = _align4(data_off + 2 * normal_slot_count)
        else:
            key_normal_offsets.append(0)

    cluster_position_off = data_off
    data_off += 2 * vertex_count

    vertex_array_offsets = []

    for sources, destinations in prepared_vertices:
        source_off = data_off
        data_off = _align4(data_off + 2 * len(sources))
        destination_off = data_off
        data_off = _align4(data_off + 2 * len(destinations))
        vertex_array_offsets.append((source_off, destination_off))

    cluster_name_off = _align4(data_off)
    key_name_off = cluster_name_off + len(cluster_names)
    total_size = _align4(key_name_off + len(key_names_table))

    section = bytearray(total_size)
    section[0:4] = BLS_SECTION_MAGIC
    struct.pack_into(">I", section, 4, total_size)
    struct.pack_into(">5H", section, 8, 1, key_count, cluster_vertex_count,
                     position_count, normal_count)
    struct.pack_into(">H", section, 18, 0xFFFF)
    struct.pack_into(
        ">7I", section, 20,
        cluster_off, key_off, blend_off, position_off, normal_off,
        cluster_name_off, key_name_off,
    )

    struct.pack_into(
        ">ffIB3xHHHHIII", section, cluster_off,
        max_angle, min_angle, key_pointer, deform_flags, len(merged_keys), vertex_count,
        normal_slot_count, cluster_vertex_count,
        cluster_position_off, blend_off, 0,
    )

    for index in range(key_count):
        struct.pack_into(
            ">HHII", section, key_off + index * BLS_KEY_ENTRY_SIZE,
            vertex_count, normal_slot_count,
            key_position_offsets[index], key_normal_offsets[index],
        )

    for index, value in enumerate(pool):
        struct.pack_into(">3f", section, position_off + index * 12, *value)

    for index, value in enumerate(normal_pool):
        struct.pack_into(">3f", section, normal_off + index * 12, *value)

    refs_by_key = [refs for _name, refs in merged_keys] + [base_refs]

    for index, refs in enumerate(refs_by_key):
        offset = key_position_offsets[index]
        for ref_index, ref in enumerate(refs):
            struct.pack_into(">H", section, offset + ref_index * 2, ref)

    normal_refs_by_key = merged_key_normals + [base_normal_refs]

    for index, refs in enumerate(normal_refs_by_key):
        offset = key_normal_offsets[index]
        for ref_index, ref in enumerate(refs):
            struct.pack_into(">H", section, offset + ref_index * 2, ref)

    for index, vertex in enumerate(vertex_indices):
        struct.pack_into(">H", section, cluster_position_off + index * 2, vertex)

    for index, (sources, destinations) in enumerate(prepared_vertices):
        source_off, destination_off = vertex_array_offsets[index]
        struct.pack_into(
            ">HHII", section, blend_off + index * 12,
            len(sources), 0, source_off, destination_off,
        )

        for ref_index, ref in enumerate(sources):
            struct.pack_into(">H", section, source_off + ref_index * 2, ref)

        for ref_index, ref in enumerate(destinations):
            struct.pack_into(">H", section, destination_off + ref_index * 2, ref)

    section[cluster_name_off:cluster_name_off + len(cluster_names)] = cluster_names
    section[key_name_off:key_name_off + len(key_names_table)] = key_names_table

    header = bytearray(BLS_SECTION_START)
    header[0:8] = BLS_MAGIC
    struct.pack_into(">II", header, 8, BLS_SECTION_START + total_size,
                     BLS_SECTION_COUNT)
    header[16:BLS_SECTION_START] = b"\xff" * (BLS_SECTION_START - 16)

    return bytes(header) + bytes(section)

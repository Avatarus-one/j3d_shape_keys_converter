"""
Pure-Python reader for J3D BLS (blend shape) files.

This module only reads BLS data and turns the low-level key tables into 
geometry that can be used by the Blender importer:

    * every animated key of every cluster is collected by name;
    * the last key of a cluster is treated as the reference ("base") key;
    * a key vertex is described by the position/normal it points at;
    * the high 3 bits of each reference are per-axis sign flags;
    * shape-key data is expressed as a delta relative to the base key.

The module deliberately does not import ``bpy`` so it can be tested with a
normal Python interpreter.

BLS layout (section CLS1) used here::

    uint16 cluster_count
    uint16 key_count
    uint16 blend_count
    uint16 vertex_position_count
    uint16 vertex_normal_count
    uint16 0xFFFF
    7 * uint32 offsets (from the start of the section)

    cluster table  (0x24 bytes each)
    key table      (0x0C bytes each)
    normal blend table
    vertex positions (float3)
    vertex normals   (float3)
    key position index arrays (uint16)
    key normal index arrays   (uint16)
    cluster position index arrays
    normal blend source/destination arrays
    cluster name table
    key name table

Key position/normal entries are 16-bit values.  The low 13 bits are an index
into the file-wide position/normal arrays; the high 3 bits are sign flags for
the X, Y and Z components (bit 15 = X, bit 14 = Y, bit 13 = Z).  The arrays
store absolute component values, so the flags must be applied to restore the
real direction of the vertex or normal.
"""

from __future__ import annotations

import hashlib
import os
import struct
from collections import OrderedDict


# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

BLS_MAGIC = b'J3D1bls1'
BLS_FORMAT = BLS_MAGIC.decode('ascii')

BLS_SECTION_START = 0x20
BLS_SECTION_COUNT = 1
BLS_SECTION_MAGIC = b'CLS1'
BLS_HEADER_SIZE = 0x60

BLS_CLUSTER_ENTRY_SIZE = 0x24
BLS_KEY_ENTRY_SIZE = 0x0C
BLS_BLEND_ENTRY_SIZE = 0x0C

INDEX_MASK = 0x1FFF

# Sign flags of a key reference.  The arrays store absolute values; these
# bits tell which components must be negated.
SIGN_X = 0x8000
SIGN_Y = 0x4000
SIGN_Z = 0x2000
SIGN_MASK = SIGN_X | SIGN_Y | SIGN_Z

# Deltas below this value are treated as "does not move the vertex".
ZERO_DELTA = 1e-9

# Two keys are treated as the same shape when every vertex receives the same
# displacement within this distance (mesh units).  It absorbs float32 noise
# and tiny model-to-model differences that are invisible after import.
DISPLACEMENT_TOLERANCE = 1e-3


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _read_uint16(data, offset):
    return struct.unpack_from('>H', data, offset)[0]


def _read_uint32(data, offset):
    return struct.unpack_from('>I', data, offset)[0]


def _read_float32(data, offset):
    return struct.unpack_from('>f', data, offset)[0]


def name_hash(name):
    """J3D string-table hash: value = value * 3 + char, modulo 0x10000."""
    value = 0
    for char in name:
        value = (value * 3 + ord(char)) & 0xFFFF
    return value


def _read_name_table(file_data, start):
    """Read a J3D string table and return (names, hashes, end_offset)."""
    count = _read_uint16(file_data, start)
    flag = _read_uint16(file_data, start + 2)

    if flag != 0xFFFF:
        raise ValueError(f'Unexpected string table flag: 0x{flag:04X}')

    end = start + 4 + count * 4
    if end > len(file_data):
        raise ValueError('String table extends past the end of the file')

    names = []
    hashes = []

    for index in range(count):
        stored_hash = _read_uint16(file_data, start + 4 + index * 4)
        name_offset = _read_uint16(file_data, start + 6 + index * 4)
        name_start = start + name_offset
        zero = file_data.find(b'\x00', name_start)

        if zero < 0:
            raise ValueError('Name string is not null-terminated')

        names.append(file_data[name_start:zero].decode('shift-jis'))
        hashes.append(stored_hash)
        end = max(end, zero + 1)

    return names, hashes, end


# ---------------------------------------------------------------------------
# BLS reader
# ---------------------------------------------------------------------------

def read_bls(file_data):
    """Parse a BLS file and return the raw tables as a dictionary."""
    if len(file_data) < BLS_HEADER_SIZE:
        raise ValueError('BLS file is too small')

    if file_data[:8] != BLS_MAGIC:
        raise ValueError("Invalid container format; expected 'J3D1bls1'")

    if _read_uint32(file_data, 12) != BLS_SECTION_COUNT:
        raise ValueError('Invalid section count')

    if file_data[BLS_SECTION_START:BLS_SECTION_START + 4] != BLS_SECTION_MAGIC:
        raise ValueError('CLS1 section not found')

    cluster_count, key_count, blend_count, \
        position_count, normal_count = struct.unpack_from(
            '>5H', file_data, 0x28
        )

    offsets = struct.unpack_from('>7I', file_data, 0x34)

    cluster_offset = BLS_SECTION_START + offsets[0]
    key_offset = BLS_SECTION_START + offsets[1]
    blend_offset = BLS_SECTION_START + offsets[2]
    position_offset = BLS_SECTION_START + offsets[3]
    normal_offset = BLS_SECTION_START + offsets[4]
    cluster_name_offset = BLS_SECTION_START + offsets[5]
    key_name_offset = BLS_SECTION_START + offsets[6]

    # --- clusters ---------------------------------------------------------
    clusters = []

    for index in range(cluster_count):
        offset = cluster_offset + index * BLS_CLUSTER_ENTRY_SIZE
        clusters.append({
            'blend_max_angle': _read_float32(file_data, offset),
            'blend_min_angle': _read_float32(file_data, offset + 4),
            'key_pointer': _read_uint32(file_data, offset + 8),
            'flag': file_data[offset + 12],
            'key': _read_uint16(file_data, offset + 16),
            'position_count': _read_uint16(file_data, offset + 18),
            'normal_count': _read_uint16(file_data, offset + 20),
            'blend_count': _read_uint16(file_data, offset + 22),
            'position_offset': _read_uint32(file_data, offset + 24),
            'blend_offset': _read_uint32(file_data, offset + 28),
            'deformer_offset': _read_uint32(file_data, offset + 32),
        })

    # --- keys -------------------------------------------------------------
    keys = []

    for index in range(key_count):
        offset = key_offset + index * BLS_KEY_ENTRY_SIZE
        keys.append({
            'position_count': _read_uint16(file_data, offset),
            'normal_count': _read_uint16(file_data, offset + 2),
            'position_offset': (
                BLS_SECTION_START + _read_uint32(file_data, offset + 4)
            ),
            'normal_offset': (
                BLS_SECTION_START + _read_uint32(file_data, offset + 8)
            ),
        })

    # --- normal blend table ----------------------------------------------
    blends = []

    for index in range(blend_count):
        offset = blend_offset + index * BLS_BLEND_ENTRY_SIZE
        blends.append({
            'count': _read_uint16(file_data, offset),
            'source_offset': (
                BLS_SECTION_START + _read_uint32(file_data, offset + 4)
            ),
            'destination_offset': (
                BLS_SECTION_START + _read_uint32(file_data, offset + 8)
            ),
        })

    # --- index arrays -----------------------------------------------------
    for cluster in clusters:
        cluster['positions'] = list(struct.unpack_from(
            f'>{cluster["position_count"]}H',
            file_data,
            BLS_SECTION_START + cluster['position_offset'],
        ))

        first_blend = (
            BLS_SECTION_START + cluster['blend_offset'] - blend_offset
        ) // BLS_BLEND_ENTRY_SIZE

        cluster_blends = blends[
            first_blend:first_blend + cluster['blend_count']
        ]

        if len(cluster_blends) != cluster['blend_count']:
            raise ValueError(
                'Cluster references missing normal blends'
            )

        cluster['normal_blends'] = []

        for blend in cluster_blends:
            cluster['normal_blends'].append({
                'source': list(struct.unpack_from(
                    f'>{blend["count"]}H',
                    file_data,
                    blend['source_offset'],
                )),
                'destination': list(struct.unpack_from(
                    f'>{blend["count"]}H',
                    file_data,
                    blend['destination_offset'],
                )),
            })

    for key in keys:
        key['positions'] = list(struct.unpack_from(
            f'>{key["position_count"]}H',
            file_data,
            key['position_offset'],
        ))
        key['normals'] = list(struct.unpack_from(
            f'>{key["normal_count"]}H',
            file_data,
            key['normal_offset'],
        ))

    # --- vertices and normals --------------------------------------------
    vertex_positions = [
        list(struct.unpack_from(
            '>3f', file_data, position_offset + index * 12
        ))
        for index in range(position_count)
    ]

    vertex_normals = [
        list(struct.unpack_from(
            '>3f', file_data, normal_offset + index * 12
        ))
        for index in range(normal_count)
    ]

    # --- names ------------------------------------------------------------
    cluster_names, cluster_hashes, cluster_names_end = _read_name_table(
        file_data, cluster_name_offset
    )

    # The key-name offset in the header can be rounded up to 4 bytes.  Try
    # the physical end of the cluster-name table first, then the header value.
    candidates = [cluster_names_end]
    if key_name_offset != cluster_names_end:
        candidates.append(key_name_offset)

    key_names = None
    key_hashes = None

    for start in candidates:
        try:
            names, hashes, _end = _read_name_table(file_data, start)
        except (ValueError, struct.error, UnicodeDecodeError):
            continue

        if all(name_hash(name) == stored
               for name, stored in zip(names, hashes)):
            key_names = names
            key_hashes = hashes
            break

    if key_names is None:
        raise ValueError('Could not read the key name table')

    if len(cluster_names) != cluster_count:
        raise ValueError(
            'Cluster name count does not match the cluster count'
        )

    if len(key_names) != key_count:
        raise ValueError(
            'Key name count does not match the key count'
        )

    for name, stored_hash in zip(cluster_names, cluster_hashes):
        if name_hash(name) != stored_hash:
            raise ValueError(f'Invalid cluster name hash: {name!r}')

    for name, stored_hash in zip(key_names, key_hashes):
        if name_hash(name) != stored_hash:
            raise ValueError(f'Invalid key name hash: {name!r}')

    result_clusters = []

    for cluster, name in zip(clusters, cluster_names):
        result_clusters.append({
            'name': name,
            'blend_max_angle': cluster['blend_max_angle'],
            'blend_min_angle': cluster['blend_min_angle'],
            'flag': cluster['flag'],
            'key': cluster['key'],
            'normal_count': cluster['normal_count'],
            'deformer_offset': cluster['deformer_offset'],
            'positions': cluster['positions'],
            'normal_blends': cluster['normal_blends'],
        })

    result_keys = []

    for key, name in zip(keys, key_names):
        result_keys.append({
            'name': name,
            'positions': key['positions'],
            'normals': key['normals'],
        })

    return {
        'format': BLS_FORMAT,
        'clusters': result_clusters,
        'keys': result_keys,
        'vertex_positions': vertex_positions,
        'vertex_normals': vertex_normals,
    }


# ---------------------------------------------------------------------------
# Key geometry
# ---------------------------------------------------------------------------

class Slot:
    """One vertex/normal slot of a shape key."""

    __slots__ = (
        'vertex',       # model position index (cluster position list)
        'delta',        # (dx, dy, dz) relative to the base key
        'base_position',
        'normal_delta',  # (dx, dy, dz), for comparison only
    )

    def __init__(self, vertex, delta, base_position, normal_delta):
        self.vertex = vertex
        self.delta = delta
        self.base_position = base_position
        self.normal_delta = normal_delta

    def __repr__(self):
        return (
            f'Slot({self.vertex}, {self.delta!r}, '
            f'{self.base_position!r}, {self.normal_delta!r})'
        )


class KeyRecord:
    """A key collected from one BLS file (possibly across clusters)."""

    __slots__ = (
        'name',
        'clusters',
        'slots',
        'is_base',
        'signature',
        'position_count',
        'normal_count',
        'is_zero',
        'displacement_map',
        'displacements',
    )

    def __init__(self, name, is_base=False):
        self.name = name
        self.clusters = []
        self.slots = []
        self.is_base = is_base
        self.signature = None
        self.position_count = 0
        self.normal_count = 0
        self.is_zero = False
        self.displacement_map = {}
        self.displacements = ()

    def finalize(self):
        self.position_count = len(self.slots)
        self.normal_count = sum(
            1 for slot in self.slots if any(slot.normal_delta)
        )

        # Duplicate/conflict detection must describe what the importer
        # actually builds: a shape key only uses position deltas, and a slot
        # with a zero delta does not move anything.  Several slots (from one
        # cluster or from different clusters) may target the same vertex, so
        # their deltas are summed.
        displacement_map = {}

        for slot in self.slots:
            dx, dy, dz = slot.delta

            if (abs(dx) <= ZERO_DELTA and abs(dy) <= ZERO_DELTA and
                    abs(dz) <= ZERO_DELTA):
                continue

            current = displacement_map.get(slot.vertex)

            if current is None:
                current = [0.0, 0.0, 0.0]
                displacement_map[slot.vertex] = current

            current[0] += dx
            current[1] += dy
            current[2] += dz

        # A vertex whose summed delta cancelled out is not moved at all.
        displacement_map = {
            vertex: tuple(values)
            for vertex, values in displacement_map.items()
            if max(abs(value) for value in values) > ZERO_DELTA
        }

        self.displacement_map = displacement_map
        self.is_zero = not displacement_map

        # Compact, order-independent form used as a fast exact signature.
        self.displacements = tuple(sorted(
            (vertex,) + tuple(round(value, 6) for value in values)
            for vertex, values in displacement_map.items()
        ))

        digest = hashlib.sha1()
        digest.update(repr(self.displacements).encode('ascii'))
        self.signature = digest.hexdigest()

    def matches(self, other, tolerance=DISPLACEMENT_TOLERANCE):
        """Return True when both keys produce the same deformation.

        Only position deltas matter: normals are not imported into shape
        keys, and zero deltas never move a vertex.  Vertex order and the
        number of slots are irrelevant, so the same movement stored with
        different vertex counts is still recognised as a duplicate.
        """
        if self.signature == other.signature:
            return True

        return displacement_maps_match(
            self.displacement_map,
            other.displacement_map,
            tolerance,
        )

    def blender_displacement_map(self):
        """Return the per-vertex displacement in Blender's Z-up space."""
        return {
            vertex: (dx, -dz, dy)
            for vertex, (dx, dy, dz) in self.displacement_map.items()
        }


def displacement_maps_match(mine, theirs, tolerance=DISPLACEMENT_TOLERANCE):
    """Compare two ``{vertex: (dx, dy, dz)}`` displacement maps."""
    for vertex, values in mine.items():
        other_values = theirs.get(vertex)

        if other_values is None:
            if max(abs(value) for value in values) > tolerance:
                return False
            continue

        if max(abs(values[axis] - other_values[axis])
               for axis in range(3)) > tolerance:
            return False

    for vertex, values in theirs.items():
        if vertex in mine:
            continue
        if max(abs(value) for value in values) > tolerance:
            return False

    return True


def _resolve_vector(value, vectors):
    """Resolve a key reference into a signed absolute vector.

    The low 13 bits select an entry of *vectors*; the high 3 bits negate the
    corresponding X/Y/Z component.  The pools themselves contain absolute
    values, so this is the only place where the signs are restored.
    """
    x, y, z = vectors[value & INDEX_MASK]

    if value & SIGN_X:
        x = -x
    if value & SIGN_Y:
        y = -y
    if value & SIGN_Z:
        z = -z

    return (x, y, z)


def collect_key_records(bls_data):
    """Collect all keys of all clusters.

    Animated keys are grouped by name (so a name used by several clusters
    produces one shape key).  The last key of each cluster is returned as a
    separate base/reference record.
    """
    keys = bls_data['keys']
    vertex_positions = bls_data['vertex_positions']
    vertex_normals = bls_data['vertex_normals']

    grouped = OrderedDict()
    base_records = []
    offset = 0

    for cluster in bls_data['clusters']:
        key_count = int(cluster['key'])
        cluster_positions = cluster['positions']

        if offset + key_count + 1 > len(keys):
            raise ValueError(
                f'Cluster {cluster["name"]!r} extends past the key table'
            )

        base_key = keys[offset + key_count]
        base_position_indices = base_key['positions']
        base_normal_indices = base_key['normals']

        # The base key is exposed in the UI as a reference entry.
        base_record = KeyRecord(base_key['name'], is_base=True)
        base_record.clusters.append(cluster['name'])

        for slot_index, model_vertex in enumerate(cluster_positions):
            if slot_index < len(base_position_indices):
                base_position = _resolve_vector(
                    base_position_indices[slot_index], vertex_positions
                )
            else:
                base_position = (0.0, 0.0, 0.0)

            base_record.slots.append(Slot(
                int(model_vertex),
                (0.0, 0.0, 0.0),
                tuple(base_position),
                (0.0, 0.0, 0.0),
            ))

        base_record.finalize()
        base_records.append(base_record)

        for key_index in range(key_count):
            key = keys[offset + key_index]
            record = grouped.get(key['name'])

            if record is None:
                record = KeyRecord(key['name'])
                grouped[key['name']] = record

            record.clusters.append(cluster['name'])

            position_indices = key['positions']
            normal_indices = key['normals']

            slot_count = min(
                len(cluster_positions),
                len(position_indices),
                len(base_position_indices),
            )

            for slot_index in range(slot_count):
                model_vertex = int(cluster_positions[slot_index])

                target_position = _resolve_vector(
                    position_indices[slot_index], vertex_positions
                )
                base_position = _resolve_vector(
                    base_position_indices[slot_index], vertex_positions
                )

                delta = (
                    target_position[0] - base_position[0],
                    target_position[1] - base_position[1],
                    target_position[2] - base_position[2],
                )

                if (slot_index < len(normal_indices) and
                        slot_index < len(base_normal_indices)):
                    target_normal = _resolve_vector(
                        normal_indices[slot_index], vertex_normals
                    )
                    base_normal = _resolve_vector(
                        base_normal_indices[slot_index], vertex_normals
                    )
                    normal_delta = (
                        target_normal[0] - base_normal[0],
                        target_normal[1] - base_normal[1],
                        target_normal[2] - base_normal[2],
                    )
                else:
                    normal_delta = (0.0, 0.0, 0.0)

                record.slots.append(Slot(
                    model_vertex,
                    delta,
                    tuple(base_position),
                    normal_delta,
                ))

        offset += key_count + 1

    if offset != len(keys):
        raise ValueError(
            'The BLS key count does not match the sum of cluster keys'
        )

    for record in grouped.values():
        record.finalize()

    return list(grouped.values()), base_records


def read_bls_file(path):
    """Read and parse one BLS file.  Returns (data, animated, base_keys)."""
    with open(path, 'rb') as file:
        raw = file.read()

    data = read_bls(raw)
    animated, base_keys = collect_key_records(data)
    return data, animated, base_keys


def bls_display_name(path):
    return os.path.basename(path)

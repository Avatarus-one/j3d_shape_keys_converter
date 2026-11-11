"""
Blender-side shape-key creation for the BLS importer.

Each BLS key carries a delta relative to the base key of its cluster.  Shape
key blocks store absolute coordinates, so every key starts as a copy of the
target mesh's Basis coordinates and the BLS delta is added on top.  Deltas are
always rotated from J3D's Y-up space to Blender's Z-up space.  This keeps the
result independent of the object transform and lets the importer work with a
mesh that already has its own base shape.
"""

from __future__ import annotations

import bpy

from . import bls_parser


MAX_NAME_BYTES = 63


def _truncate_name(name, limit=MAX_NAME_BYTES):
    """Safely truncate a name to *limit* UTF-8 bytes."""
    encoded = name.encode('utf-8')

    if len(encoded) <= limit:
        return name

    return encoded[:limit].decode('utf-8', 'ignore')


def _unique_name(base_name, used):
    """Return a name not present in *used* (case-insensitive)."""
    base_name = _truncate_name(base_name)

    if not base_name:
        base_name = 'ShapeKey'

    candidate = base_name
    counter = 1

    while candidate.casefold() in used:
        suffix = f'.{counter:03d}'
        candidate = _truncate_name(base_name, MAX_NAME_BYTES - len(suffix))
        candidate = f'{candidate}{suffix}'
        counter += 1

    used.add(candidate.casefold())
    return candidate


def _convert_delta(delta):
    """Convert a J3D (Y-up) delta to Blender's Z-up space.

    J3D is Y-up, Blender is Z-up: rotate +90 degrees around X.
    """
    dx, dy, dz = delta
    return dx, -dz, dy


def _build_coordinates(slots, base_coords, vertex_count):
    """Build a flat coordinate array for one shape key.

    Shape-key blocks store absolute coordinates, so the array starts as a
    copy of the mesh's base coordinates and the BLS deltas are added to it.
    Returns ``(coords, touched, out_of_range)`` where *touched* is the number
    of slots that referenced an existing mesh vertex.
    """
    coords = list(base_coords)
    touched = 0
    out_of_range = 0

    for slot in slots:
        vertex = slot.vertex

        if vertex < 0 or vertex >= vertex_count:
            out_of_range += 1
            continue

        dx, dy, dz = _convert_delta(slot.delta)
        offset = vertex * 3

        coords[offset] += dx
        coords[offset + 1] += dy
        coords[offset + 2] += dz
        touched += 1

    return coords, touched, out_of_range


def existing_shape_key_displacements(obj):
    """Return ``[(name, {vertex: (dx, dy, dz)})]`` for existing shape keys.

    The Basis key is skipped.  Displacements are in Blender's Z-up space and
    are measured against the Basis coordinates.
    """
    if obj is None or obj.type != 'MESH' or obj.data is None:
        return []

    shape_keys = obj.data.shape_keys

    if shape_keys is None or len(shape_keys.key_blocks) < 2:
        return []

    vertex_count = len(obj.data.vertices)

    if vertex_count == 0:
        return []

    basis = shape_keys.key_blocks[0]
    basis_coords = [0.0] * (vertex_count * 3)
    basis.data.foreach_get('co', basis_coords)

    result = []

    for key_block in shape_keys.key_blocks[1:]:
        coords = [0.0] * (vertex_count * 3)
        key_block.data.foreach_get('co', coords)
        displacement = {}

        for vertex in range(vertex_count):
            offset = vertex * 3
            dx = coords[offset] - basis_coords[offset]
            dy = coords[offset + 1] - basis_coords[offset + 1]
            dz = coords[offset + 2] - basis_coords[offset + 2]

            if max(abs(dx), abs(dy), abs(dz)) > bls_parser.ZERO_DELTA:
                displacement[vertex] = (dx, dy, dz)

        result.append((key_block.name, displacement))

    return result


def apply_shape_keys(context, obj, jobs):
    """Add shape keys to *obj*.

    Parameters
    ----------
    context:
        Current Blender context.
    obj:
        Target mesh object.
    jobs:
        Iterable of ``(name, slots, source_name)`` tuples.  *slots* is a list
        of :class:`bls_parser.Slot` objects.

    A key whose final name already exists is overwritten.  The dialog renames
    imported keys that would collide with existing shape keys, so an overwrite
    only happens when the user deliberately renames a key back.

    BLS deltas are rotated from J3D Y-up to Blender Z-up.
    """
    if obj is None:
        raise ValueError('No target mesh selected')
    if obj.type != 'MESH':
        raise ValueError(f'Object "{obj.name}" is not a mesh')
    if obj.data is None:
        raise ValueError(f'Object "{obj.name}" has no mesh data')

    mesh = obj.data
    vertex_count = len(mesh.vertices)

    if vertex_count == 0:
        raise ValueError(f'Mesh "{obj.name}" has no vertices')

    # Shape keys cannot be edited while the object is in edit mode.
    if obj.mode != 'OBJECT':
        context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='OBJECT')

    stats = {
        'created': 0,
        'updated': 0,
        'skipped': 0,
        'out_of_range': 0,
        'skipped_names': [],
        'created_basis': False,
    }

    shape_keys = mesh.shape_keys

    if shape_keys is None:
        obj.shape_key_add(name='Basis', from_mix=False)
        shape_keys = mesh.shape_keys
        stats['created_basis'] = True

    if shape_keys is None:
        raise RuntimeError('Could not create the base shape key')

    shape_keys.use_relative = True

    basis = shape_keys.key_blocks[0] if shape_keys.key_blocks else None

    # Absolute coordinates that every shape key starts from.
    base_coords = [0.0] * (vertex_count * 3)
    if basis is not None:
        basis.data.foreach_get('co', base_coords)
    else:
        mesh.vertices.foreach_get('co', base_coords)

    existing = {}

    for key_block in shape_keys.key_blocks:
        existing.setdefault(key_block.name.casefold(), key_block)

    imported_names = set()

    for name, slots, _source_name in jobs:
        coords, touched, out_of_range = _build_coordinates(
            slots, base_coords, vertex_count
        )
        stats['out_of_range'] += out_of_range

        if touched == 0:
            stats['skipped'] += 1
            stats['skipped_names'].append(name)
            continue

        name_key = name.casefold()

        # The Basis key is protected, and two imported keys cannot share a
        # final name.  The dialog highlights such collisions; only the first
        # key is imported to avoid silently overwriting the other one.
        if name_key == 'basis' or name_key in imported_names:
            stats['skipped'] += 1
            stats['skipped_names'].append(name)
            continue

        imported_names.add(name_key)
        target_block = existing.get(name_key)

        if target_block is not None and target_block != basis:
            stats['updated'] += 1
        else:
            target_block = obj.shape_key_add(name=name, from_mix=False)
            existing[name_key] = target_block
            stats['created'] += 1

        target_block.data.foreach_set('co', coords)

    mesh.update()

    return stats

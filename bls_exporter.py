"""Blender-side extraction of shape keys for BLS export."""

from __future__ import annotations

import bpy
import numpy as np

from . import bls_writer


class BLSExportError(ValueError):
    pass


def _read_coords(block, count):
    coords = [0.0] * (count * 3)
    block.data.foreach_get("co", coords)
    return coords


def _to_bls(position, scale):
    """Blender Z-up -> J3D Y-up (and Blender units -> model units)."""
    x, y, z = position
    return (x * scale, z * scale, -y * scale)


def _normal_to_bls(normal):
    """Rotate a Blender-space normal into J3D space."""
    x, y, z = normal
    return (x, z, -y)


def _vertex_normals(coords, triangles, count):
    """Compute area-weighted vertex normals from a position array."""
    accum = [0.0] * (count * 3)

    for i0, i1, i2 in triangles:
        o0 = i0 * 3
        o1 = i1 * 3
        o2 = i2 * 3
        ax = coords[o1] - coords[o0]
        ay = coords[o1 + 1] - coords[o0 + 1]
        az = coords[o1 + 2] - coords[o0 + 2]
        bx = coords[o2] - coords[o0]
        by = coords[o2 + 1] - coords[o0 + 1]
        bz = coords[o2 + 2] - coords[o0 + 2]
        nx = ay * bz - az * by
        ny = az * bx - ax * bz
        nz = ax * by - ay * bx

        for vertex in (i0, i1, i2):
            off = vertex * 3
            accum[off] += nx
            accum[off + 1] += ny
            accum[off + 2] += nz

    normals = []

    for vertex in range(count):
        off = vertex * 3
        nx = accum[off]
        ny = accum[off + 1]
        nz = accum[off + 2]
        length = (nx * nx + ny * ny + nz * nz) ** 0.5

        if length > 1e-12:
            normals.append((nx / length, ny / length, nz / length))
        else:
            normals.append((0.0, 1.0, 0.0))

    return normals


def _mesh_triangles(mesh):
    mesh.calc_loop_triangles()
    count = len(mesh.loop_triangles)

    if count == 0:
        return []

    indices = [0] * (count * 3)
    mesh.loop_triangles.foreach_get("vertices", indices)
    return [
        (indices[index * 3], indices[index * 3 + 1], indices[index * 3 + 2])
        for index in range(count)
    ]


def _corner_normals(mesh):
    count = len(mesh.loops)

    if count == 0:
        return np.zeros((0, 3), np.float64)

    raw = np.empty(count * 3, np.float32)
    mesh.corner_normals.foreach_get("vector", raw)
    return raw.reshape(-1, 3).astype(np.float64)


def _natural_normal_layout(mesh):
    """Canonical per-corner normal indices derived from the mesh itself.

    The BMD exporter implements the same algorithm independently.  Keeping the
    two implementations in sync makes a BMD and a BLS exported from the same
    mesh address the same normal array without any shared metadata.
    """
    count = len(mesh.loops)

    if count == 0:
        return []

    normals = np.round(_corner_normals(mesh), 6)
    vertices = np.empty(count, np.int32)
    mesh.loops.foreach_get("vertex_index", vertices)
    keys = np.empty((count, 4), np.float64)
    keys[:, 0] = vertices
    keys[:, 1:] = normals
    _unique, inverse = np.unique(keys, axis=0, return_inverse=True)
    return inverse.reshape(-1).astype(np.int32).tolist()


NORMAL_POOL_LIMIT = 0x2000


def _normal_pool_size(base_normals, key_normals, decimals):
    """Count the shared absolute-value pool the writer would create."""
    pool = set()

    for group in [base_normals] + list(key_normals):
        for vector in group:
            if decimals is None:
                components = tuple(abs(float(value)) for value in vector)
            else:
                components = tuple(
                    round(abs(float(value)), decimals) for value in vector)
            pool.add(components)

    return len(pool)


def _fit_normal_pool(base_normals, key_normals):
    """Keep the normal pool inside the 13-bit BLS index limit.

    BLS stores every normal as a 13-bit reference, so at most 8192 distinct
    absolute vectors fit in one file.  Multi-key face clusters can exceed that
    at full float precision.  Reduce precision only as much as necessary; the
    destinations and blend topology are unaffected.
    """
    if not base_normals:
        return base_normals, key_normals

    groups = [list(base_normals)] + [list(array) for array in key_normals]

    if _normal_pool_size(groups[0], groups[1:], None) <= NORMAL_POOL_LIMIT:
        return base_normals, key_normals

    for decimals in range(6, -1, -1):
        if _normal_pool_size(groups[0], groups[1:], decimals) <= NORMAL_POOL_LIMIT:
            return (
                [
                    tuple(round(float(value), decimals) for value in vector)
                    for vector in groups[0]
                ],
                [
                    [
                        tuple(round(float(value), decimals) for value in vector)
                        for vector in array
                    ]
                    for array in groups[1:]
                ],
            )

    return base_normals, key_normals


def _normal_layout(mesh):
    """Return the canonical BMD normal index used by every mesh corner."""
    return _natural_normal_layout(mesh)


def collect_export_data(obj, key_names, convert_units=True, base_name=None):
    """Return all data needed for a BLS cluster.

    The returned tuple is
    ``(cluster_name, vertex_indices, base_positions, keys, base_normals,
    key_normals, cluster_vertices, base_name)``.

    *base_name* overrides the default ``<mesh>__base`` base-key name.
    """
    if obj is None or obj.type != "MESH" or obj.data is None:
        raise BLSExportError("No target mesh selected")

    mesh = obj.data
    shape_keys = mesh.shape_keys

    if shape_keys is None or len(shape_keys.key_blocks) < 2:
        raise BLSExportError(
            f'Mesh "{obj.name}" has no shape keys to export')

    blocks = list(shape_keys.key_blocks)
    basis = blocks[0]
    by_name = {block.name: block for block in blocks[1:]}
    selected = []

    for name in key_names:
        block = by_name.get(name)

        if block is None:
            raise BLSExportError(f'Shape key not found: "{name}"')

        selected.append(block)

    if not selected:
        raise BLSExportError("Select at least one shape key to export")

    count = len(mesh.vertices)
    basis_coords = _read_coords(basis, count)
    key_coords = []

    for block in selected:
        key_coords.append((block.name, _read_coords(block, count)))

    moved = set()

    for _name, coords in key_coords:
        for vertex in range(count):
            offset = vertex * 3
            dx = coords[offset] - basis_coords[offset]
            dy = coords[offset + 1] - basis_coords[offset + 1]
            dz = coords[offset + 2] - basis_coords[offset + 2]

            if max(abs(dx), abs(dy), abs(dz)) > 1e-9:
                moved.add(vertex)

    if not moved:
        moved.update(range(count))

    vertex_indices = sorted(moved)
    scale = 100.0 if convert_units else 1.0

    base_positions = [
        _to_bls(
            (basis_coords[v * 3], basis_coords[v * 3 + 1],
             basis_coords[v * 3 + 2]),
            scale,
        )
        for v in vertex_indices
    ]

    keys = []

    for name, coords in key_coords:
        keys.append((
            name,
            [
                _to_bls(
                    (coords[v * 3], coords[v * 3 + 1], coords[v * 3 + 2]),
                    scale,
                )
                for v in vertex_indices
            ],
        ))

    triangles = _mesh_triangles(mesh)
    key_vertex_normals = [
        _vertex_normals(coords, triangles, count)
        for _name, coords in key_coords
    ]
    layout = _normal_layout(mesh)

    if layout and len(layout) == len(mesh.loops):
        # Use the same normal indices the BMD exporter writes.  A vertex may
        # own several split-normal destinations; the blend table must list all
        # of them or the runtime would only update one corner normal.
        loop_vertices = [0] * len(mesh.loops)
        mesh.loops.foreach_get("vertex_index", loop_vertices)
        corners = _corner_normals(mesh)
        index_vertex = {}
        index_normal = {}

        for loop, normal_index in enumerate(layout):
            normal_index = int(normal_index)

            if normal_index not in index_vertex:
                index_vertex[normal_index] = int(loop_vertices[loop])
                index_normal[normal_index] = corners[loop]

        ordered = sorted(index_vertex)
        # One normal slot per distinct normal direction.  BLS address fields
        # use 13 bits (8192 pool entries), so one slot per BMD destination would
        # overflow the normal pool on models with many split normals.  Sharing
        # slots by value also matches how the original files are built.
        slot_of = {}
        slot_vertices = []
        slot_normals = []
        slot_lookup = {}

        for normal_index in ordered:
            normal = index_normal[normal_index]
            key = tuple(round(float(value), 6) for value in normal)
            slot = slot_lookup.get(key)

            if slot is None:
                slot = len(slot_vertices)
                slot_lookup[key] = slot
                slot_vertices.append(index_vertex[normal_index])
                slot_normals.append(normal)

            slot_of[normal_index] = slot

        base_normals = [_normal_to_bls(normal) for normal in slot_normals]
        key_normals = [
            [
                _normal_to_bls(normals[vertex])
                for vertex in slot_vertices
            ]
            for normals in key_vertex_normals
        ]

        per_vertex = [set() for _ in range(count)]

        for loop, normal_index in enumerate(layout):
            per_vertex[int(loop_vertices[loop])].add(int(normal_index))

        cluster_vertices = []

        for vertex, normal_indices in enumerate(per_vertex):
            if not normal_indices:
                continue

            destinations = sorted(normal_indices)
            cluster_vertices.append((
                [slot_of[normal_index] for normal_index in destinations],
                destinations,
            ))
    else:
        # Degenerate fallback: one normal slot per vertex, addressed by the
        # vertex index.  Used only when the mesh has no usable loops.
        base_normals = [
            _normal_to_bls(normal)
            for normal in _vertex_normals(basis_coords, triangles, count)
        ]
        key_normals = [
            [_normal_to_bls(normal) for normal in normals]
            for normals in key_vertex_normals
        ]
        cluster_vertices = [([index], [index]) for index in range(count)]

    cluster_name = mesh.name or obj.name
    existing = {name.casefold() for name in key_names}
    explicit_base = base_name.strip() if base_name is not None else ''

    if explicit_base:
        base_name = explicit_base

        if base_name.casefold() in existing:
            raise BLSExportError(
                f'Base key name "{base_name}" is already used by a selected key')
    else:
        base_name = f"{cluster_name}__base"

        if base_name.casefold() in existing:
            base_name = f"{base_name}_base"

    base_normals, key_normals = _fit_normal_pool(base_normals, key_normals)

    return (cluster_name, vertex_indices, base_positions, keys,
            base_normals, key_normals, cluster_vertices, base_name)


def export_bls(obj, filepath, key_names, convert_units=True, base_name=None):
    """Write selected shape keys of *obj* to *filepath*.

    *base_name* overrides the trailing base-key name; when omitted, the
    ``<mesh>__base`` fallback is used.  Returns a statistics dict with
    ``keys``, ``vertices`` and ``bytes``.
    """
    (cluster_name, vertex_indices, base_positions, keys,
     base_normals, key_normals, cluster_vertices, final_base_name) = (
        collect_export_data(obj, key_names, convert_units, base_name)
    )

    data = bls_writer.write_bls(
        cluster_name,
        vertex_indices,
        base_positions,
        keys,
        base_name=final_base_name,
        base_normals=base_normals,
        key_normals=key_normals,
        cluster_vertices=cluster_vertices,
    )

    with open(filepath, "wb") as file:
        file.write(data)

    return {
        "keys": len(keys),
        "vertices": len(vertex_indices),
        "bytes": len(data),
    }

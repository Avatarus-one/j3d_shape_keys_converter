"""Blender-side extraction of shape keys for BLS export."""

from __future__ import annotations

import bpy

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
    base_normals = [
        _normal_to_bls(normal)
        for normal in _vertex_normals(basis_coords, triangles, count)
    ]
    key_normals = []

    for _name, coords in key_coords:
        key_normals.append([
            _normal_to_bls(normal)
            for normal in _vertex_normals(coords, triangles, count)
        ])

    # One normal slot per mesh vertex.  The blend table writes the vertex
    # normal to the same model normal index.  This keeps the export completely
    # self-contained: it does not depend on metadata from another add-on and
    # works for meshes created from scratch.
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

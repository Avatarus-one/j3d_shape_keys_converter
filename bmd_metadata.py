"""Read and validate the BMD normal metadata stored on a Blender mesh."""

from __future__ import annotations

import hashlib
import json
import struct

import numpy as np

ATTRIBUTE = "bmd_normal_index"
PROPERTY = "bmd_normal_meta"
VERSION = 1

OK = "ok"
MISSING = "missing"
STALE = "stale"
INVALID = "invalid"


def topology_hash(mesh):
    """Hash the mesh topology without including positions or normals."""
    digest = hashlib.sha1()
    vertex_count = len(mesh.vertices)
    loop_count = len(mesh.loops)
    polygon_count = len(mesh.polygons)
    digest.update(struct.pack("<III", vertex_count, loop_count, polygon_count))

    loops = np.empty(loop_count, dtype="<i4")
    mesh.loops.foreach_get("vertex_index", loops)
    digest.update(loops.tobytes())

    starts = np.empty(polygon_count, dtype="<i4")
    totals = np.empty(polygon_count, dtype="<i4")
    mesh.polygons.foreach_get("loop_start", starts)
    mesh.polygons.foreach_get("loop_total", totals)
    digest.update(starts.tobytes())
    digest.update(totals.tobytes())
    return digest.hexdigest()


def read_normal_metadata(obj):
    """Return ``(indices, normal_count, position_offset, status, message)``.

    ``indices`` is a list with one BMD normal index per mesh corner.
    ``position_offset`` is added to mesh vertex indices to address the BMD
    position array.  The metadata is only valid while the mesh topology is
    unchanged.
    """
    if obj is None or obj.type != "MESH" or obj.data is None:
        return None, 0, 0, MISSING, "No target mesh selected"

    mesh = obj.data
    raw = mesh.get(PROPERTY)

    if not raw:
        return None, 0, 0, MISSING, (
            "BLS export requires using j3d_bmd_io. Normals will be invalid.")

    try:
        meta = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return None, 0, 0, INVALID, (
            "BMD normal metadata is invalid. Re-export with j3d_bmd_io.")

    if int(meta.get("version", 0)) != VERSION:
        return None, 0, 0, INVALID, (
            "BMD normal metadata is invalid. Re-export with j3d_bmd_io.")

    if not bool(meta.get("vertex_order", False)):
        return None, 0, 0, INVALID, (
            "BLS export requires using j3d_bmd_io. Normals will be invalid.")

    position_offset = int(meta.get("position_offset", 0))
    if position_offset < 0:
        return None, 0, 0, INVALID, (
            "BMD normal metadata is invalid. Re-export with j3d_bmd_io.")

    vertex_count = len(mesh.vertices)
    loop_count = len(mesh.loops)
    polygon_count = len(mesh.polygons)

    if (int(meta.get("vertex_count", -1)) != vertex_count or
            int(meta.get("loop_count", -1)) != loop_count or
            int(meta.get("polygon_count", -1)) != polygon_count):
        return None, 0, 0, STALE, (
            "Mesh was modified. Re-export it with j3d_bmd_io.")

    if meta.get("topology") != topology_hash(mesh):
        return None, 0, 0, STALE, (
            "Mesh was modified. Re-export it with j3d_bmd_io.")

    attr = mesh.attributes.get(ATTRIBUTE)
    if (attr is None or attr.domain != "CORNER" or attr.data_type != "INT" or
            len(attr.data) != loop_count):
        return None, 0, 0, INVALID, (
            "BMD normal metadata is invalid. Re-export with j3d_bmd_io.")

    indices = np.empty(loop_count, dtype=np.int32)
    attr.data.foreach_get("value", indices)

    normal_count = int(meta.get("normal_count", 0))
    if normal_count <= 0 or (loop_count and
                             (int(indices.min()) < 0 or int(indices.max()) >= normal_count)):
        return None, 0, 0, INVALID, (
            "BMD normal metadata is invalid. Re-export with j3d_bmd_io.")

    return indices.tolist(), normal_count, position_offset, OK, ""

"""
Session building and duplicate/conflict analysis for the BLS importer.

The UI only stores lightweight status/name data.  Geometry stays in Python
objects kept by this module until the import is executed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import bls_parser


# Key statuses
UNIQUE = 'unique'
DUPLICATE = 'duplicate'
CONFLICT = 'conflict'
BASE = 'base'
NO_CHANGE = 'no_change'
INCOMPATIBLE = 'incompatible'

_STATUS_LABELS = {
    UNIQUE: 'OK',
    DUPLICATE: 'Duplicate',
    CONFLICT: 'Conflict',
    BASE: 'Base key',
    NO_CHANGE: 'No change',
    INCOMPATIBLE: 'Incompatible',
}


@dataclass
class KeyEntry:
    record: bls_parser.KeyRecord
    file: 'FileEntry'
    status: str = UNIQUE
    status_text: str = ''
    include: bool = True
    new_name: str = ''
    renamed: bool = False
    duplicate_of: Optional['KeyEntry'] = None
    conflict_with: Optional['KeyEntry'] = None

    @property
    def name(self):
        return self.record.name

    @property
    def is_base(self):
        return self.record.is_base

    @property
    def signature(self):
        return self.record.signature


@dataclass
class ExistingKey:
    """A shape key that already exists on the target mesh.

    ``displacement_map`` is in Blender's Z-up space; BLS records are
    converted before they are compared with it.
    """

    name: str
    displacement_map: Dict[int, tuple]

    def matches(self, record, tolerance=bls_parser.DISPLACEMENT_TOLERANCE):
        return bls_parser.displacement_maps_match(
            record.blender_displacement_map(),
            self.displacement_map,
            tolerance,
        )


@dataclass
class FileEntry:
    path: str
    display_path: str = ''
    keys: List[KeyEntry] = field(default_factory=list)
    error: str = ''
    summary: str = ''
    duplicate_count: int = 0
    conflict_count: int = 0
    incompatible_count: int = 0

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def display_name(self):
        """A short label: the local path when a folder was imported."""
        return self.display_path or os.path.basename(self.path)


@dataclass
class ImportSession:
    files: List[FileEntry] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def key_count(self):
        return sum(len(f.keys) for f in self.files)

    @property
    def selected_count(self):
        return sum(
            1 for f in self.files for k in f.keys
            if k.include and not k.is_base
        )

    @property
    def duplicate_count(self):
        return sum(f.duplicate_count for f in self.files)

    @property
    def conflict_count(self):
        return sum(f.conflict_count for f in self.files)

    @property
    def incompatible_count(self):
        return sum(f.incompatible_count for f in self.files)


def collect_bls_paths(root, recursive=True):
    """Return sorted .bls paths from a file or a folder."""
    root = os.path.abspath(root)

    if os.path.isfile(root):
        return [root] if root.lower().endswith('.bls') else []

    paths = []

    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort(key=str.lower)
            for name in sorted(filenames, key=str.lower):
                if name.lower().endswith('.bls'):
                    paths.append(os.path.join(dirpath, name))
    else:
        for name in sorted(os.listdir(root), key=str.lower):
            path = os.path.join(root, name)
            if os.path.isfile(path) and name.lower().endswith('.bls'):
                paths.append(path)

    return paths


def _clean_arc_name(name):
    """Strip the usual J3D archive suffixes from a folder name."""
    return name.rsplit(".", 1)[0]


def _local_display_path(path, folder_roots):
    """Return the path relative to the imported folder.

    When only individual files are imported there is no folder root, so the
    bare file name is enough.  BLS files can live in any folder layout, so no
    archive or ``bls`` directory is assumed.
    """
    best_root = None

    for root in folder_roots:
        try:
            common = os.path.commonpath([root, path])
        except ValueError:
            continue

        if common != root:
            continue

        if best_root is None or len(root) > len(best_root):
            best_root = root

    if best_root is None:
        return os.path.basename(path)

    relative = os.path.relpath(path, best_root)
    return relative.replace(os.sep, '/')


def _unique_name(base_name, used):
    """Return a unique name, adding Blender-style ``.001`` suffixes."""
    name = base_name
    counter = 1

    while name.casefold() in used:
        name = f'{base_name}.{counter:03d}'
        counter += 1

    used.add(name.casefold())
    return name


def _rename_base(entry):
    """Base name for an auto-renamed key.

    Keys from ``<archive>/bls/file.bls`` get the archive folder tag, e.g.
    ``md_mouth_mu [Demo22_01]``.  Any other layout uses plain numbering
    (``md_mouth_mu.001``) instead of inventing a folder name.
    """
    parent = os.path.dirname(entry.file.path)

    if os.path.basename(parent).lower() == 'bls':
        archive = os.path.basename(os.path.dirname(parent))
        if archive:
            return f'{entry.name} [{_clean_arc_name(archive)}]'

    return entry.name


def build_session(paths, recursive=True, progress=None, existing_keys=None,
                  vertex_count=None):
    """Read all BLS paths and compute duplicate/conflict information.

    Parameters
    ----------
    paths:
        One or more files/folders.
    recursive:
        Scan folders recursively.
    progress:
        Optional callable(current, total, path).
    existing_keys:
        Optional :class:`ExistingKey` objects for shape keys that already
        exist on the target mesh.  They take part in duplicate/conflict
        detection and name allocation.
    vertex_count:
        Number of vertices of the target mesh.  Keys that reference a vertex
        outside this range are marked as incompatible and cannot be imported.
    """
    session = ImportSession()

    folder_roots = [
        os.path.abspath(value) for value in paths if os.path.isdir(value)
    ]

    resolved = []
    seen_paths = set()

    for value in paths:
        for path in collect_bls_paths(value, recursive=recursive):
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen_paths:
                seen_paths.add(key)
                resolved.append(os.path.abspath(path))

    resolved.sort(
        key=lambda p: (
            _local_display_path(p, folder_roots).casefold(), p.casefold()
        )
    )

    total = len(resolved)

    for index, path in enumerate(resolved):
        if progress is not None:
            progress(index, total, path)

        file_entry = FileEntry(
            path=path,
            display_path=_local_display_path(path, folder_roots),
        )

        try:
            _data, animated, base_keys = bls_parser.read_bls_file(path)
        except Exception as exc:
            file_entry.error = f'{type(exc).__name__}: {exc}'
            session.files.append(file_entry)
            continue

        for record in animated:
            file_entry.keys.append(KeyEntry(record=record, file=file_entry))

        for record in base_keys:
            file_entry.keys.append(KeyEntry(record=record, file=file_entry))

        session.files.append(file_entry)

    _analyse(session, existing_keys, vertex_count)

    for file_entry in session.files:
        _summarise_file(file_entry)

    return session


def _out_of_bounds_vertex(record, vertex_count):
    """Return the highest vertex referenced outside the target mesh.

    Every slot is checked, not only the nonzero displacements: a base or
    no-change key still references model vertices and must be reported as
    incompatible when the target mesh is too small.
    ``None`` means the key fits (or no target size is known yet).
    """
    if vertex_count is None or vertex_count <= 0:
        return None

    offending = None

    for slot in record.slots:
        vertex = slot.vertex

        if vertex < 0 or vertex >= vertex_count:
            if offending is None or vertex > offending:
                offending = vertex

    return offending


def _analyse(session, existing_keys=None, vertex_count=None):
    """Mark duplicates, conflicts, incompatible and base/no-op keys."""
    used_names = set()

    # Original name -> already seen entries (including renamed conflicts).
    variants: Dict[str, List[KeyEntry]] = {}

    # Shape keys that already exist on the target mesh.
    external: Dict[str, ExistingKey] = {}

    for key in existing_keys or ():
        external.setdefault(key.name.casefold(), key)
        used_names.add(key.name.casefold())

    # The Basis key must never be overwritten by an imported key.
    used_names.add('basis')

    # The analysis is re-run when the target mesh changes, so reset every
    # entry before classifying it again.
    for file_entry in session.files:
        file_entry.duplicate_count = 0
        file_entry.conflict_count = 0
        file_entry.incompatible_count = 0

        for entry in file_entry.keys:
            entry.status = UNIQUE
            entry.status_text = ''
            entry.include = True
            entry.new_name = ''
            entry.renamed = False
            entry.duplicate_of = None
            entry.conflict_with = None

    # No-change keys are tracked separately.  The first key with a given
    # name keeps the NO_CHANGE status; later keys with the same name are
    # duplicates of it.
    no_change_entries = []
    no_change_by_name = {}

    for file_entry in session.files:
        for entry in file_entry.keys:
            # Incompatibility wins over every other status, including base
            # and no-change: such a key cannot be imported at all.
            offending_vertex = _out_of_bounds_vertex(
                entry.record, vertex_count
            )

            if offending_vertex is not None:
                entry.status = INCOMPATIBLE
                entry.include = False
                entry.new_name = entry.name
                entry.status_text = (
                    f'Incompatible: vertex {offending_vertex} is outside the '
                    f'target mesh ({vertex_count} vertices)'
                )
                file_entry.incompatible_count += 1
                continue

            if entry.is_base:
                entry.status = BASE
                entry.status_text = 'Base key (not imported)'
                entry.include = False
                entry.new_name = entry.name

                # Reserve the name so animated keys cannot silently collide.
                used_names.add(entry.name.casefold())
                continue

            name_key = entry.name.casefold()
            existing = variants.setdefault(name_key, [])
            external_key = external.get(name_key)

            if entry.record.is_zero:
                if external_key is not None:
                    if external_key.matches(entry.record):
                        entry.status = DUPLICATE
                        entry.include = False
                        entry.new_name = external_key.name
                        entry.status_text = (
                            f'Duplicate: same deformation as existing key '
                            f'"{external_key.name}" on the target mesh'
                        )
                        file_entry.duplicate_count += 1
                    else:
                        entry.status = CONFLICT
                        entry.include = True
                        entry.renamed = True
                        base_name = _rename_base(entry)
                        entry.new_name = _unique_name(base_name, used_names)
                        entry.status_text = (
                            f'Conflict: target mesh already has key '
                            f'"{external_key.name}" with different '
                            f'deformation; renamed to "{entry.new_name}"'
                        )
                        file_entry.conflict_count += 1
                else:
                    first = no_change_by_name.get(name_key)

                    if first is not None:
                        entry.status = DUPLICATE
                        entry.include = False
                        entry.duplicate_of = first
                        entry.new_name = first.name
                        entry.status_text = (
                            f'Duplicate: same deformation as '
                            f'{first.file.display_name}'
                        )
                        file_entry.duplicate_count += 1
                    else:
                        entry.status = NO_CHANGE
                        entry.include = False
                        entry.new_name = entry.name
                        entry.status_text = (
                            'Does not change the geometry (matches the base key)'
                        )
                        no_change_entries.append(entry)
                        no_change_by_name[name_key] = entry
                continue

            match = None
            for other in existing:
                if other.record.matches(entry.record):
                    match = other
                    break

            if match is not None:
                entry.status = DUPLICATE
                entry.include = False
                entry.duplicate_of = match
                entry.new_name = match.name
                entry.status_text = (
                    f'Duplicate: same deformation as '
                    f'{match.file.display_name}'
                )
                file_entry.duplicate_count += 1
                continue

            if existing:
                entry.status = CONFLICT
                entry.renamed = True
                entry.conflict_with = existing[0]

                base_name = _rename_base(entry)
                entry.new_name = _unique_name(base_name, used_names)
                entry.status_text = (
                    f'Conflict: deformation differs from '
                    f'{existing[0].file.display_name}; '
                    f'renamed to "{entry.new_name}"'
                )
                file_entry.conflict_count += 1
            elif external_key is not None:
                if external_key.matches(entry.record):
                    entry.status = DUPLICATE
                    entry.include = False
                    entry.new_name = external_key.name
                    entry.status_text = (
                        f'Duplicate: same deformation as existing key '
                        f'"{external_key.name}" on the target mesh'
                    )
                    file_entry.duplicate_count += 1
                else:
                    entry.status = CONFLICT
                    entry.include = True
                    entry.renamed = True
                    base_name = _rename_base(entry)
                    entry.new_name = _unique_name(base_name, used_names)
                    entry.status_text = (
                        f'Conflict: target mesh already has key '
                        f'"{external_key.name}" with different deformation; '
                        f'renamed to "{entry.new_name}"'
                    )
                    file_entry.conflict_count += 1
            else:
                entry.status = UNIQUE
                entry.new_name = _unique_name(entry.name, used_names)

            existing.append(entry)

    # Second pass: allocate final names for the first no-change key of each
    # name only after all importable keys have their names.  This renames
    # them on collision even when they are not selected for import.  Renamed
    # keys use the same "<name> [<folder>]" form as conflicts.  Later
    # no-change keys with the same name are duplicates and keep the original
    # name, exactly like other duplicate keys.
    for entry in no_change_entries:
        base_name = entry.name

        if base_name.casefold() in used_names:
            base_name = _rename_base(entry)

        new_name = _unique_name(base_name, used_names)
        entry.new_name = new_name
        entry.renamed = new_name != entry.name

        if entry.renamed:
            entry.status_text = (
                'Does not change the geometry (matches the base key); '
                f'renamed to "{new_name}" because the name is already in use'
            )


def reanalyse(session, existing_keys=None, vertex_count=None):
    """Re-run duplicate/conflict detection and refresh file summaries."""
    _analyse(session, existing_keys, vertex_count)

    for file_entry in session.files:
        _summarise_file(file_entry)


def _plural(count, one, many):
    return one if abs(int(count)) == 1 else many


def _summarise_file(file_entry):
    if file_entry.error:
        file_entry.summary = 'Read error'
        return

    # Count every key, including the base key, so this summary always
    # matches the total shown in the dialog header.
    total = len(file_entry.keys)

    parts = [f'{total} {_plural(total, "key", "keys")}']

    if file_entry.duplicate_count:
        count = file_entry.duplicate_count
        parts.append(f'{count} {_plural(count, "duplicate", "duplicates")}')

    if file_entry.conflict_count:
        count = file_entry.conflict_count
        parts.append(f'{count} {_plural(count, "conflict", "conflicts")}')

    if file_entry.incompatible_count:
        count = file_entry.incompatible_count
        parts.append(
            f'{count} {_plural(count, "incompatible", "incompatible")}'
        )

    file_entry.summary = ' · '.join(parts)

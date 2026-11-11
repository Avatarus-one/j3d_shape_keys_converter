"""
Blender UI for the J3D shape-key importer.

Workflow
--------
1. ``File > Import > J3D Shape Keys`` (or the 3D View sidebar) selects one
   or more ``.bls`` files, or a folder that is scanned recursively.
2. The files are parsed by the operator and a settings dialog is shown.
3. The dialog lists every file, opens its keys, shows duplicate/conflict
   statuses, lets the user exclude or rename keys, and finally creates the
   shape keys.

The file list is a fixed-height list with a scroll slider.  Selecting a
file opens its key list, where keys can be excluded, renamed and inspected.

The parsed geometry is kept in a module-level :class:`ImportSession`; only
lightweight text/int properties are stored in the scene.
"""

from __future__ import annotations

import os
import textwrap

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import (
    Operator,
    OperatorFileListElement,
    Panel,
    PropertyGroup,
    UIList,
)
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ImportHelper

from . import bls_importer, bls_session


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

# Kept alive while the settings dialog (and the panel shortcut) need it.
_SESSION = None

# Keys the user may select.  Duplicates and base keys stay read-only.
_SELECTABLE = (
    bls_session.UNIQUE,
    bls_session.CONFLICT,
    bls_session.NO_CHANGE,
)

# Guards the file <-> key checkbox update callbacks against recursion.
_SYNCING = False

# Collision data cached for the duration of one UIList draw.  Computing it
# once per draw keeps draw_item() from rescanning every key for every row.
_DRAW_COLLIDING_NAMES = {}
_DRAW_COLLIDING_FILES = frozenset()

# Number of file rows visible at once; the scroll slider moves the window.
VISIBLE_FILES = 10

# Number of key rows visible at once inside a file.
VISIBLE_KEYS = 10

# Blender 4.2 fixed drawing a UIList inside a property dialog
# (commit 38d11482f5).  Older versions crash with an access violation when a
# template_list is drawn in a popup, so they use explicit pages instead.
USES_UILIST = bpy.app.version >= (4, 2, 0)

_STATUS_LABELS = {
    bls_session.UNIQUE: 'OK',
    bls_session.DUPLICATE: 'Duplicate',
    bls_session.CONFLICT: 'Conflict',
    bls_session.BASE: 'Base',
    bls_session.NO_CHANGE: 'No change',
    bls_session.INCOMPATIBLE: 'Incompatible',
}

_STATUS_ICONS = {
    bls_session.UNIQUE: 'CHECKMARK',
    bls_session.DUPLICATE: 'DUPLICATE',
    bls_session.CONFLICT: 'ERROR',
    bls_session.BASE: 'KEY_HLT',
    bls_session.NO_CHANGE: 'INFO',
    bls_session.INCOMPATIBLE: 'CANCEL',
}


def _poll_mesh(_self, obj):
    return obj is not None and obj.type == 'MESH'


def _get_settings(context=None):
    context = context or bpy.context
    scene = getattr(context, 'scene', None)
    return getattr(scene, 'bls_settings', None)


def _refresh_file_includes(settings):
    """Make every file checkbox reflect its selectable key checkboxes.

    A file is checked when at least one of its selectable keys is selected.
    """
    global _SYNCING

    _SYNCING = True

    try:
        for file_index, file_item in enumerate(settings.files):
            selectable = [
                key_item for key_item in settings.keys
                if key_item.file_index == file_index
                and key_item.status in _SELECTABLE
            ]
            file_item.include = any(
                key_item.include for key_item in selectable
            )
    finally:
        _SYNCING = False


def _target_vertex_count(target):
    """Return the target mesh's vertex count, or ``None`` when unavailable."""
    if target is None or target.type != 'MESH' or target.data is None:
        return None

    return len(target.data.vertices)


def _redraw(context):
    """Force the dialog to redraw after a checkbox changes."""
    area = getattr(context, 'area', None)

    if area is not None:
        area.tag_redraw()

    window_manager = getattr(context, 'window_manager', None)

    if window_manager is None:
        return

    for window in window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for screen_area in screen.areas:
            screen_area.tag_redraw()


def _existing_keys(target):
    """Build session-side descriptions of the target mesh's shape keys."""
    return [
        bls_session.ExistingKey(name, displacement)
        for name, displacement in bls_importer.existing_shape_key_displacements(
            target
        )
    ]


def _apply_session_to_settings(settings, session):
    """Refresh UI items after the session was re-analysed."""
    global _SYNCING

    if len(settings.keys) != session.key_count:
        _populate_settings(None, settings, session)
        return

    _SYNCING = True

    try:
        key_index = 0

        for file_index, file_entry in enumerate(session.files):
            if file_index >= len(settings.files):
                break

            file_item = settings.files[file_index]
            file_item.summary = file_entry.summary
            file_item.error = file_entry.error

            for key_entry in file_entry.keys:
                if key_index >= len(settings.keys):
                    break

                key_item = settings.keys[key_index]
                key_index += 1
                key_item.status = key_entry.status
                key_item.status_text = key_entry.status_text
                key_item.new_name = key_entry.new_name or key_entry.name
                key_item.include = (
                    key_entry.status in _SELECTABLE and
                    (
                        key_entry.status != bls_session.NO_CHANGE or
                        settings.include_no_change
                    )
                )
    finally:
        _SYNCING = False

    _refresh_file_includes(settings)


def _colliding_names(settings):
    """Return ``{casefolded_final_name: display_name}`` for collisions.

    Only selected, importable keys take part.  The importer keeps the first
    key of every colliding group and skips the rest.
    """
    counts = {}
    labels = {}

    for key_item in settings.keys:
        if not key_item.include:
            continue
        if key_item.status not in _SELECTABLE:
            continue

        name = key_item.new_name.strip() or key_item.name
        name_key = name.casefold()
        counts[name_key] = counts.get(name_key, 0) + 1
        labels.setdefault(name_key, name)

    return {
        name_key: labels[name_key]
        for name_key, count in counts.items()
        if count > 1
    }


def _colliding_file_indexes(settings):
    """Return the file indexes that contain a colliding selected key."""
    colliding = _colliding_names(settings)

    if not colliding:
        return set()

    indexes = set()

    for key_item in settings.keys:
        if not key_item.include:
            continue
        if key_item.status not in _SELECTABLE:
            continue

        name = key_item.new_name.strip() or key_item.name

        if name.casefold() in colliding:
            indexes.add(key_item.file_index)

    return indexes


def _prepare_draw_collisions(settings):
    """Cache collision data for the UIList draw that follows."""
    global _DRAW_COLLIDING_NAMES, _DRAW_COLLIDING_FILES

    names = _colliding_names(settings)
    _DRAW_COLLIDING_NAMES = names
    _DRAW_COLLIDING_FILES = frozenset(_colliding_file_indexes(settings))


def _target_changed(self, context):
    """The target mesh changed: re-run duplicate/conflict detection."""
    global _SESSION

    if _SYNCING or _SESSION is None:
        return

    settings = _get_settings(context)

    if settings is None:
        return

    bls_session.reanalyse(
        _SESSION,
        _existing_keys(self.target),
        _target_vertex_count(self.target),
    )
    _apply_session_to_settings(settings, _SESSION)
    _redraw(context)


def _file_include_changed(self, context):
    """A file checkbox was clicked: apply it to all of its keys."""
    global _SYNCING

    if _SYNCING:
        return

    settings = _get_settings(context)

    if settings is None:
        return

    file_index = next(
        (index for index, item in enumerate(settings.files)
         if item.path == self.path),
        -1,
    )

    if file_index < 0:
        return

    _SYNCING = True

    try:
        for key_item in settings.keys:
            if key_item.file_index != file_index:
                continue
            if key_item.status not in _SELECTABLE:
                continue

            # "Include keys without changes" controls whether the file
            # checkbox may select no-change keys.  Unchecking the file still
            # deselects them.
            if (self.include and
                    key_item.status == bls_session.NO_CHANGE and
                    not settings.include_no_change):
                continue

            key_item.include = self.include
    finally:
        _SYNCING = False

    _refresh_file_includes(settings)
    _redraw(context)


def _key_include_changed(self, context):
    """A key checkbox was clicked: update the checkbox of its file."""
    global _SYNCING

    if _SYNCING:
        return

    # Duplicates, base keys and incompatible keys are read-only.  Their
    # checkbox is disabled, so this only guards against stray writes.
    if self.status not in _SELECTABLE:
        return

    settings = _get_settings(context)

    if settings is None:
        return

    if not (0 <= self.file_index < len(settings.files)):
        return

    selectable = [
        key_item for key_item in settings.keys
        if key_item.file_index == self.file_index
        and key_item.status in _SELECTABLE
    ]

    _SYNCING = True

    try:
        settings.files[self.file_index].include = any(
            key_item.include for key_item in selectable
        )
    finally:
        _SYNCING = False

    _redraw(context)


def _key_name_changed(self, context):
    """A final name was edited: refresh collision highlighting."""
    if _SYNCING:
        return

    _redraw(context)


def _include_no_change_changed(self, context):
    """The import option toggled: apply it to every no-change key."""
    global _SYNCING

    if _SYNCING:
        return

    settings = _get_settings(context)

    if settings is None:
        return

    _SYNCING = True

    try:
        for key_item in settings.keys:
            if key_item.status == bls_session.NO_CHANGE:
                key_item.include = self.include_no_change
    finally:
        _SYNCING = False

    _refresh_file_includes(settings)
    _redraw(context)


# ---------------------------------------------------------------------------
# Property groups
# ---------------------------------------------------------------------------

class BLS_FileItem(PropertyGroup):
    file_index: IntProperty()
    path: StringProperty()
    label: StringProperty()
    summary: StringProperty()
    error: StringProperty()
    include: BoolProperty(
        name='Include file',
        description='Include all selectable keys from this file',
        default=True,
        update=_file_include_changed,
    )


class BLS_KeyItem(PropertyGroup):
    file_index: IntProperty()
    key_index: IntProperty()
    global_index: IntProperty()
    name: StringProperty()
    new_name: StringProperty(update=_key_name_changed)
    include: BoolProperty(
        name='Include key',
        description='Import this key',
        update=_key_include_changed,
    )
    status: StringProperty()
    status_text: StringProperty()
    is_base: BoolProperty()
    clusters: StringProperty()
    vertex_count: IntProperty()
    normal_count: IntProperty()


class BLS_Settings(PropertyGroup):
    target: PointerProperty(
        name='Target Mesh',
        description='Mesh that will receive the shape keys',
        type=bpy.types.Object,
        poll=_poll_mesh,
        update=_target_changed,
    )
    files: CollectionProperty(type=BLS_FileItem)
    keys: CollectionProperty(type=BLS_KeyItem)

    # Navigation state of the settings dialog.
    file_scroll: FloatProperty(
        name='Scroll',
        description='Scroll through the file list',
        default=0.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
    )
    key_scroll: FloatProperty(
        name='Scroll',
        description='Scroll through the key list',
        default=0.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
    )
    view_file: IntProperty(default=-1)
    detail_key_index: IntProperty(default=-1)

    # Paged lists (Blender < 4.2) and UIList selection state (Blender >= 4.2).
    file_page: IntProperty(default=0)
    key_page: IntProperty(default=0)
    active_file: IntProperty(default=-1)
    active_key: IntProperty(default=-1)

    include_no_change: BoolProperty(
        name='Include keys without changes',
        description='Automatically select keys that do not change the '
                    'geometry',
        default=False,
        update=_include_no_change_changed,
    )


# ---------------------------------------------------------------------------
# Session -> properties
# ---------------------------------------------------------------------------

def _populate_settings(_context, settings, session):
    global _SYNCING

    _SYNCING = True

    try:
        settings.files.clear()
        settings.keys.clear()
        settings.file_scroll = 0.0
        settings.key_scroll = 0.0
        settings.view_file = -1
        settings.detail_key_index = -1
        settings.file_page = 0
        settings.key_page = 0
        settings.active_file = -1
        settings.active_key = -1

        for file_index, file_entry in enumerate(session.files):
            file_item = settings.files.add()
            file_item.file_index = file_index
            file_item.path = file_entry.path
            file_item.label = file_entry.display_name
            file_item.summary = file_entry.summary
            file_item.error = file_entry.error

            for key_index, key_entry in enumerate(file_entry.keys):
                key_item = settings.keys.add()
                key_item.file_index = file_index
                key_item.key_index = key_index
                key_item.global_index = len(settings.keys) - 1
                key_item.name = key_entry.name
                key_item.new_name = key_entry.new_name or key_entry.name
                key_item.status = key_entry.status
                key_item.status_text = key_entry.status_text
                key_item.is_base = key_entry.is_base
                key_item.clusters = ', '.join(key_entry.record.clusters)
                key_item.vertex_count = key_entry.record.position_count
                key_item.normal_count = key_entry.record.normal_count
                key_item.include = (
                    key_entry.status in _SELECTABLE and
                    (
                        key_entry.status != bls_session.NO_CHANGE or
                        settings.include_no_change
                    )
                )
    finally:
        _SYNCING = False

    _refresh_file_includes(settings)


def _counts(settings):
    selected = 0
    selectable = 0
    duplicates = 0
    conflicts = 0
    incompatible = 0
    errors = 0

    for file_item in settings.files:
        if file_item.error:
            errors += 1

    for key_item in settings.keys:
        if key_item.status in _SELECTABLE:
            selectable += 1
            if key_item.status == bls_session.CONFLICT:
                conflicts += 1
            if key_item.include:
                selected += 1
        elif key_item.status == bls_session.DUPLICATE:
            duplicates += 1
        elif key_item.status == bls_session.INCOMPATIBLE:
            incompatible += 1

    return selected, selectable, duplicates, conflicts, incompatible, errors


def _file_has_selectable(settings, file_index):
    """Return True when *file_index* has at least one selectable key."""
    return any(
        key_item.status in _SELECTABLE
        for key_item in settings.keys
        if key_item.file_index == file_index
    )


def _iter_file_keys(settings, file_index):
    """Yield ``(global_index, key_item)`` for one file."""
    for index, key_item in enumerate(settings.keys):
        if key_item.file_index == file_index:
            yield index, key_item


# ---------------------------------------------------------------------------
# Small operators
# ---------------------------------------------------------------------------

class BLS_OT_open_file(Operator):
    bl_idname = 'bls_shape_keys.open_file'
    bl_label = 'Show file keys'
    bl_description = 'Show the keys stored in this file'
    bl_options = {'INTERNAL'}

    file_index: IntProperty()

    @classmethod
    def description(cls, context, properties):
        """Tooltip shows the same local path as the file list."""
        settings = _get_settings(context)
        index = properties.file_index

        if settings is not None and 0 <= index < len(settings.files):
            return (
                f'{settings.files[index].label}\n'
                f'Show the keys stored in this file'
            )

        return cls.bl_description

    def execute(self, context):
        settings = context.scene.bls_settings

        if 0 <= self.file_index < len(settings.files):
            settings.view_file = self.file_index
            settings.detail_key_index = -1
            settings.key_scroll = 0.0
            settings.key_page = 0
            settings.active_key = -1

        return {'FINISHED'}


class BLS_OT_back_files(Operator):
    bl_idname = 'bls_shape_keys.back_files'
    bl_label = 'Back to file list'
    bl_description = 'Return to the list of files'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = context.scene.bls_settings
        settings.view_file = -1
        settings.detail_key_index = -1
        settings.active_file = -1
        settings.active_key = -1
        return {'FINISHED'}


class BLS_OT_page(Operator):
    bl_idname = 'bls_shape_keys.page'
    bl_label = 'Change page'
    bl_description = 'Show the previous or next page of the list'
    bl_options = {'INTERNAL'}

    target: EnumProperty(
        name='List',
        description='List to page through',
        items=(
            ('FILES', 'Files', 'File list'),
            ('KEYS', 'Keys', 'Key list'),
        ),
    )
    delta: IntProperty(name='Direction', default=1)

    def execute(self, context):
        settings = context.scene.bls_settings

        if self.target == 'FILES':
            settings.file_page = _clamped_page(
                settings.file_page + self.delta,
                len(settings.files),
                VISIBLE_FILES,
            )
        else:
            file_index = settings.view_file

            if 0 <= file_index < len(settings.files):
                count = sum(
                    1 for key_item in settings.keys
                    if key_item.file_index == file_index
                )
                settings.key_page = _clamped_page(
                    settings.key_page + self.delta,
                    count,
                    VISIBLE_KEYS,
                )

        return {'FINISHED'}


class BLS_OT_key_details(Operator):
    bl_idname = 'bls_shape_keys.key_details'
    bl_label = 'Key details'
    bl_description = 'Show details for this key'
    bl_options = {'INTERNAL'}

    key_index: IntProperty()

    @classmethod
    def description(cls, context, properties):
        """Dynamic tooltip: the status explanation of the clicked key."""
        settings = _get_settings(context)
        index = getattr(properties, 'key_index', -1)

        if settings is not None and 0 <= index < len(settings.keys):
            key_item = settings.keys[index]

            if key_item.status_text:
                return key_item.status_text

            label = _STATUS_LABELS.get(key_item.status, key_item.status)
            return f'{label}: click to show key details'

        return 'Show details for this key'

    def execute(self, context):
        settings = context.scene.bls_settings

        if settings.detail_key_index == self.key_index:
            settings.detail_key_index = -1
        else:
            settings.detail_key_index = self.key_index

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# File / folder selection
# ---------------------------------------------------------------------------

def _open_settings_dialog():
    """Timer callback: open the settings popup after the file browser closes."""
    try:
        bpy.ops.bls_shape_keys.settings('INVOKE_DEFAULT')
    except Exception as exc:  # pragma: no cover - only in unusual UI states
        print(f'BLS importer: could not open settings dialog: {exc}')

    return None


def _begin_import(context, paths, operator):
    global _SESSION

    scene = context.scene
    settings = scene.bls_settings

    # Choose the target before building the session: existing shape keys
    # take part in duplicate/conflict detection and name allocation.
    if settings.target is None:
        target = context.active_object
        if target is None or target.type != 'MESH':
            target = next(
                (obj for obj in scene.objects if obj.type == 'MESH'), None
            )
        if target is not None:
            settings.target = target

    window = context.window
    if window is not None:
        window.cursor_set('WAIT')

    try:
        session = bls_session.build_session(
            paths,
            recursive=True,
            existing_keys=_existing_keys(settings.target),
            vertex_count=_target_vertex_count(settings.target),
        )
    except Exception as exc:
        operator.report({'ERROR'}, f'Could not read BLS: {exc}')
        return {'CANCELLED'}
    finally:
        if window is not None:
            window.cursor_set('DEFAULT')

    if not session.files:
        operator.report({'ERROR'}, 'No .bls files found')
        return {'CANCELLED'}

    _SESSION = session
    _populate_settings(context, settings, session)
    scene.bls_last_info = ''

    # Defer the popup: opening it directly from inside the file-browser
    # operator can close it together with the file browser.
    bpy.app.timers.register(_open_settings_dialog, first_interval=0.05)

    return {'FINISHED'}


class BLS_OT_import_files(Operator, ImportHelper):
    bl_idname = 'bls_shape_keys.import_files'
    bl_label = 'Import BLS'
    bl_description = 'Import J3D Shape Keys from .bls files'
    bl_options = {'REGISTER'}

    filename_ext = '.bls'
    filter_glob: StringProperty(default='*.bls', options={'HIDDEN'})
    files: CollectionProperty(
        type=OperatorFileListElement,
        options={'HIDDEN', 'SKIP_SAVE'},
    )
    directory: StringProperty(subtype='DIR_PATH')

    def execute(self, context):
        paths = []

        if self.files:
            directory = self.directory or os.path.dirname(self.filepath)
            for element in self.files:
                if element.name:
                    paths.append(os.path.join(directory, element.name))
        elif self.filepath:
            paths.append(self.filepath)

        if not paths:
            self.report({'ERROR'}, 'No .bls files selected')
            return {'CANCELLED'}

        return _begin_import(context, paths, self)


class BLS_OT_import_folder(Operator):
    bl_idname = 'bls_shape_keys.import_folder'
    bl_label = 'Import BLS Folder'
    bl_description = ('Import J3D Shape Keys (.bls) from folder and all subfolders')
    bl_options = {'REGISTER'}

    directory: StringProperty(subtype='DIR_PATH')
    filter_folder: BoolProperty(default=True, options={'HIDDEN'})

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.directory:
            self.report({'ERROR'}, 'No folder selected')
            return {'CANCELLED'}

        return _begin_import(context, [self.directory], self)


# ---------------------------------------------------------------------------
# Settings dialog / import execution
# ---------------------------------------------------------------------------

def _wrap_label(layout, text, width=88, icon='NONE'):
    lines = textwrap.wrap(text, width=width) or ['']
    for index, line in enumerate(lines):
        layout.label(text=line, icon=icon if index == 0 else 'NONE')


def _draw_target_and_options(layout, settings):
    box = layout.box()
    box.label(text='Import settings', icon='MODIFIER')

    target_row = box.row()
    target_row.alert = settings.target is None
    target_row.prop(settings, 'target')

    box.prop(settings, 'include_no_change')

    note = box.column(align=True)
    note.active = False


def _draw_summary(layout, settings):
    selected, selectable, duplicates, conflicts, incompatible, errors = (
        _counts(settings)
    )

    summary = layout.column(align=True)
    summary.label(
        text=f'Files: {len(settings.files)} · '
             f'keys: {len(settings.keys)} · '
             f'selected: {selected} of {selectable}',
        icon='INFO',
    )

    stats = []

    if duplicates:
        stats.append(f'Duplicates: {duplicates}')
    if conflicts:
        stats.append(f'Conflicts: {conflicts}')
    if incompatible:
        stats.append(f'Incompatible: {incompatible}')
    if errors:
        stats.append(f'Read errors: {errors}')

    if stats:
        stat_row = summary.row(align=True)
        for text in stats:
            if text.startswith('Read errors:'):
                stat_row.label(text=text, icon='ERROR')
            elif text.startswith('Incompatible:'):
                stat_row.label(text=text, icon='CANCEL')
            else:
                stat_row.label(text=text)


def _file_scroll_offset(settings):
    """Return the first visible file index for the current scroll position."""
    count = len(settings.files)
    max_offset = max(0, count - VISIBLE_FILES)

    if max_offset <= 0:
        return 0

    offset = int(round(settings.file_scroll * max_offset))
    return min(max(offset, 0), max_offset)


def _key_scroll_offset(settings, count):
    """Return the first visible key row for the current scroll position."""
    max_offset = max(0, count - VISIBLE_KEYS)

    if max_offset <= 0:
        return 0

    offset = int(round(settings.key_scroll * max_offset))
    return min(max(offset, 0), max_offset)


def _page_count(count, per_page):
    """Return the number of pages needed for *count* rows."""
    return max(1, (count + per_page - 1) // per_page)


def _clamped_page(page, count, per_page):
    """Clamp a page index to the available page range."""
    return min(max(int(page), 0), _page_count(count, per_page) - 1)


def _file_keys(settings, file_index):
    """Return ``[(global_index, key_item), ...]`` for one file."""
    return [
        (index, key_item)
        for index, key_item in enumerate(settings.keys)
        if key_item.file_index == file_index
    ]


def _draw_pager(layout, target, page, total_pages):
    """Draw the English Back/Next pager used before Blender 4.2."""
    row = layout.row(align=True)

    # The split owns three equal columns: the first takes a third and the
    # remaining two share the rest evenly.  Buttons fill their whole column,
    # so Back and Next each occupy a third of the row, and the counter is
    # centered inside the middle column.
    split = row.split(factor=1.0 / 3.0, align=True)

    left = split.column(align=True)
    left.enabled = page > 0
    op = left.operator(
        'bls_shape_keys.page',
        text='Back',
        icon='TRIA_LEFT',
    )
    op.target = target
    op.delta = -1

    center = split.column(align=True)
    center_row = center.row(align=True)
    center_row.alignment = 'CENTER'
    center_row.label(text=f'Page {page + 1} of {total_pages}')

    right = split.column(align=True)
    right.enabled = page + 1 < total_pages
    op = right.operator(
        'bls_shape_keys.page',
        text='Next',
        icon='TRIA_RIGHT',
    )
    op.target = target
    op.delta = 1


def _draw_file_list_paged(layout, settings):
    count = len(settings.files)

    header = layout.row(align=True)
    header.label(text=f'Files: {count}', icon='FILE_FOLDER')

    box = layout.box()

    if count == 0:
        box.label(text='No files to import', icon='ERROR')
        return

    page = _clamped_page(settings.file_page, count, VISIBLE_FILES)
    start = page * VISIBLE_FILES
    end = min(start + VISIBLE_FILES, count)

    colliding_files = _colliding_file_indexes(settings)

    for file_index in range(start, end):
        file_item = settings.files[file_index]

        row = box.row(align=True)
        row.alert = bool(file_item.error) or file_index in colliding_files

        check = row.row(align=True)
        check.enabled = (
            not file_item.error and
            _file_has_selectable(settings, file_index)
        )
        check.prop(file_item, 'include', text='')

        split = row.split(factor=0.62, align=True)

        name_col = split.column(align=True)
        name_row = name_col.row(align=True)
        name_row.alignment = 'LEFT'
        # Grey out files without selectable keys, but keep the name
        # clickable so their key list can still be reviewed.
        name_row.active = _file_has_selectable(settings, file_index)
        op = name_row.operator(
            'bls_shape_keys.open_file',
            text=file_item.label,
            emboss=False,
            icon='ERROR' if file_item.error else 'FILE',
        )
        op.file_index = file_index

        summary_col = split.column(align=True)
        summary_col.alignment = 'LEFT'
        summary_col.label(text=file_item.summary)

    total_pages = _page_count(count, VISIBLE_FILES)

    if total_pages > 1:
        box.label(text=f'Files {start + 1}-{end} of {count}')
        _draw_pager(box, 'FILES', page, total_pages)


def _draw_key_details(layout, settings, file_index):
    index = settings.detail_key_index

    if not (0 <= index < len(settings.keys)):
        return

    key_item = settings.keys[index]
    if key_item.file_index != file_index:
        return

    box = layout.box()
    box.label(
        text=f'{key_item.name} — '
             f'{_STATUS_LABELS.get(key_item.status, key_item.status)}',
        icon=_STATUS_ICONS.get(key_item.status, 'DOT'),
    )

    if key_item.new_name != key_item.name:
        _wrap_label(box, f'New name: {key_item.new_name}')

    meta = (
        f'Vertices: {key_item.vertex_count} · '
        f'normals: {key_item.normal_count}'
    )
    if key_item.clusters:
        meta = f'{meta} · clusters: {key_item.clusters}'

    _wrap_label(box, meta)


def _draw_file_keys_paged(layout, settings):
    file_index = settings.view_file
    file_item = settings.files[file_index]
    colliding = _colliding_names(settings)

    back = layout.row()
    back.operator(
        'bls_shape_keys.back_files',
        text='Back to file list',
        icon='BACK',
    )

    box = layout.box()
    header = box.row(align=True)
    header.alert = (
        bool(file_item.error) or
        file_index in _colliding_file_indexes(settings)
    )

    check = header.row(align=True)
    check.enabled = (
        _file_has_selectable(settings, file_index) and not file_item.error
    )
    check.prop(file_item, 'include', text='')

    header.label(
        text=file_item.label,
        icon='ERROR' if file_item.error else 'FILE',
    )
    right = header.row(align=True)
    right.alignment = 'LEFT'
    right.label(text=file_item.summary)

    if file_item.error:
        _wrap_label(box, f'Read error: {file_item.error}')
        return

    key_items = list(_iter_file_keys(settings, file_index))
    total_keys = len(key_items)
    page = _clamped_page(settings.key_page, total_keys, VISIBLE_KEYS)
    start = page * VISIBLE_KEYS
    end = min(start + VISIBLE_KEYS, total_keys)

    if total_keys == 0:
        box.label(text='No keys in this file', icon='INFO')
        return

    for key_index, key_item in key_items[start:end]:
        selectable = key_item.status in _SELECTABLE
        final_name = key_item.new_name.strip() or key_item.name
        is_collision = (
            selectable and
            key_item.include and
            final_name.casefold() in colliding
        )

        row = box.row(align=True)
        row.alert = is_collision

        check = row.row(align=True)
        check.active = selectable
        check.enabled = selectable
        check.prop(key_item, 'include', text='')

        # The original name is a plain-looking button so clicking it shows
        # the key details, just like clicking its status.
        split = row.split(factor=0.4, align=True)

        name_col = split.column(align=True)
        name_col.active = selectable
        name_row = name_col.row(align=True)
        name_row.alignment = 'LEFT'
        op = name_row.operator(
            'bls_shape_keys.key_details',
            text=key_item.name,
            emboss=False,
        )
        op.key_index = key_index

        # New name input; the status keeps a fixed-ish trailing area.
        rest = split.column(align=True)
        rest_split = rest.split(factor=0.75, align=True)

        input_col = rest_split.column(align=True)
        input_row = input_col.row(align=True)
        input_row.use_property_split = False
        input_row.active = selectable
        #input_row.label(text='→')
        input_row.prop(key_item, 'new_name', text='')

        status_col = rest_split.column(align=True)
        status_col.alignment = 'LEFT'

        label = _STATUS_LABELS.get(key_item.status, key_item.status)
        icon = _STATUS_ICONS.get(key_item.status, 'DOT')

        # Status is clickable and its tooltip is the per-key status explanation.
        status_row = status_col.row(align=True)
        status_row.alignment = 'LEFT'
        op = status_row.operator(
            'bls_shape_keys.key_details',
            text=label,
            icon=icon,
            emboss=False,
        )
        op.key_index = key_index

    if total_keys > VISIBLE_KEYS:
        box.label(text=f'Keys {start + 1}-{end} of {total_keys}')
        _draw_pager(
            box,
            'KEYS',
            page,
            _page_count(total_keys, VISIBLE_KEYS),
        )

    if colliding:
        _wrap_label(
            box,
            'Warning: several keys have the same name: '
            + ', '.join(sorted(colliding.values()))
            + '. Only the first one will be imported.',
            width=110,
            icon='ERROR',
        )

    _draw_key_details(layout, settings, file_index)


class BLS_UL_files(UIList):
    """File list for Blender 4.2 and newer (safe inside a popup there)."""

    bl_idname = 'BLS_UL_files'

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag):
        settings = data
        file_index = item.file_index

        row = layout.row(align=True)
        row.alert = bool(item.error) or file_index in _DRAW_COLLIDING_FILES

        check = row.row(align=True)
        check.enabled = (
            not item.error and
            _file_has_selectable(settings, file_index)
        )
        check.prop(item, 'include', text='')

        split = row.split(factor=0.62, align=True)

        name_col = split.column(align=True)
        name_row = name_col.row(align=True)
        name_row.alignment = 'LEFT'
        # Grey out files without selectable keys, but keep the name
        # clickable so their key list can still be reviewed.
        name_row.active = _file_has_selectable(settings, file_index)
        op = name_row.operator(
            'bls_shape_keys.open_file',
            text=item.label,
            emboss=False,
            icon='ERROR' if item.error else 'FILE',
        )
        op.file_index = file_index

        summary_col = split.column(align=True)
        summary_col.alignment = 'LEFT'
        summary_col.label(text=item.summary)


class BLS_UL_keys(UIList):
    """Key list for the file opened in the dialog (Blender 4.2+)."""

    bl_idname = 'BLS_UL_keys'

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        view_file = data.view_file

        flags = [
            self.bitflag_filter_item if item.file_index == view_file else 0
            for item in items
        ]
        return flags, []

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag):
        settings = data
        key_item = item
        key_index = key_item.global_index

        selectable = key_item.status in _SELECTABLE
        final_name = key_item.new_name.strip() or key_item.name
        is_collision = (
            selectable and
            key_item.include and
            final_name.casefold() in _DRAW_COLLIDING_NAMES
        )

        row = layout.row(align=True)
        row.alert = is_collision

        check = row.row(align=True)
        check.active = selectable
        check.enabled = selectable
        check.prop(key_item, 'include', text='')

        # The original name is a plain-looking button so clicking it shows
        # the key details, just like clicking its status.
        split = row.split(factor=0.4, align=True)

        name_col = split.column(align=True)
        name_col.active = selectable
        name_row = name_col.row(align=True)
        name_row.alignment = 'LEFT'
        op = name_row.operator(
            'bls_shape_keys.key_details',
            text=key_item.name,
            emboss=False,
        )
        op.key_index = key_index

        # New name input; the status keeps a fixed-ish trailing area.
        rest = split.column(align=True)
        rest_split = rest.split(factor=0.70, align=True)

        input_col = rest_split.column(align=True)
        input_row = input_col.row(align=True)
        input_row.use_property_split = False
        input_row.active = selectable
        input_row.prop(key_item, 'new_name', text='')

        status_col = rest_split.column(align=True)

        label = _STATUS_LABELS.get(key_item.status, key_item.status)
        status_icon = _STATUS_ICONS.get(key_item.status, 'DOT')

        # Status is clickable and its tooltip is the per-key explanation.
        status_row = status_col.row(align=True)
        status_row.alignment = 'LEFT'
        op = status_row.operator(
            'bls_shape_keys.key_details',
            text=label,
            icon=status_icon,
            emboss=False,
        )
        op.key_index = key_index


def _draw_file_list_uilist(layout, settings):
    count = len(settings.files)

    header = layout.row(align=True)
    header.label(text=f'Files: {count}', icon='FILE_FOLDER')
    header.label(
        text='Click a file name to review its keys',
        icon='INFO',
    )

    if count == 0:
        layout.label(text='No files to import', icon='ERROR')
        return

    _prepare_draw_collisions(settings)

    layout.template_list(
        'BLS_UL_files',
        '',
        settings,
        'files',
        settings,
        'active_file',
        rows=min(VISIBLE_FILES, count),
        maxrows=VISIBLE_FILES,
    )


def _draw_file_keys_uilist(layout, settings):
    file_index = settings.view_file
    file_item = settings.files[file_index]

    _prepare_draw_collisions(settings)

    back = layout.row()
    back.operator(
        'bls_shape_keys.back_files',
        text='Back to file list',
        icon='BACK',
    )

    box = layout.box()
    header = box.row(align=True)
    header.alert = (
        bool(file_item.error) or
        file_index in _DRAW_COLLIDING_FILES
    )

    check = header.row(align=True)
    check.enabled = (
        _file_has_selectable(settings, file_index) and not file_item.error
    )
    check.prop(file_item, 'include', text='')

    header.label(
        text=file_item.label,
        icon='ERROR' if file_item.error else 'FILE',
    )
    right = header.row(align=True)
    right.alignment = 'LEFT'
    right.label(text=file_item.summary)

    if file_item.error:
        _wrap_label(box, f'Read error: {file_item.error}')
        return

    total_keys = sum(
        1 for key_item in settings.keys
        if key_item.file_index == file_index
    )

    if total_keys == 0:
        box.label(text='No keys in this file', icon='INFO')
        return

    box.template_list(
        'BLS_UL_keys',
        '',
        settings,
        'keys',
        settings,
        'active_key',
        rows=min(VISIBLE_KEYS, total_keys),
        maxrows=VISIBLE_KEYS,
    )

    box.label(text=f'Keys 1-{total_keys} of {total_keys}')

    if _DRAW_COLLIDING_NAMES:
        _wrap_label(
            box,
            'Warning: several keys have the same final name: '
            + ', '.join(sorted(_DRAW_COLLIDING_NAMES.values()))
            + '. Only the first one will be imported.',
            width=110,
            icon='ERROR',
        )

    _draw_key_details(layout, settings, file_index)


class BLS_OT_settings(Operator):
    bl_idname = 'bls_shape_keys.settings'
    bl_label = 'Import J3D Shape Keys'
    bl_description = 'Review imported files, then create the shape keys'
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, _event):
        if _SESSION is None:
            self.report({'ERROR'}, 'Select BLS files or a folder first')
            return {'CANCELLED'}

        settings = context.scene.bls_settings

        # The target mesh (and its shape keys) may have changed since the
        # session was built, so refresh duplicate/conflict detection.
        bls_session.reanalyse(
            _SESSION,
            _existing_keys(settings.target),
            _target_vertex_count(settings.target),
        )
        _apply_session_to_settings(settings, _SESSION)

        return context.window_manager.invoke_props_dialog(self, width=650)

    def draw(self, context):
        layout = self.layout
        settings = context.scene.bls_settings

        if _SESSION is None:
            layout.label(text='Import data is not loaded', icon='ERROR')
            return

        _draw_target_and_options(layout, settings)
        _draw_summary(layout, settings)

        if not settings.files:
            layout.label(text='No files to import', icon='ERROR')
            return

        if 0 <= settings.view_file < len(settings.files):
            if USES_UILIST:
                _draw_file_keys_uilist(layout, settings)
            else:
                _draw_file_keys_paged(layout, settings)
        else:
            if USES_UILIST:
                _draw_file_list_uilist(layout, settings)
            else:
                _draw_file_list_paged(layout, settings)

    def execute(self, context):
        global _SESSION

        settings = context.scene.bls_settings
        scene = context.scene

        if _SESSION is None:
            self.report({'ERROR'}, 'Import data is not loaded')
            return {'CANCELLED'}

        if settings.target is None:
            self.report({'ERROR'}, 'Select a target mesh')
            return {'CANCELLED'}

        session = _SESSION

        if len(settings.files) != len(session.files):
            self.report(
                {'ERROR'},
                'Session data is out of date; select the BLS files again',
            )
            return {'CANCELLED'}

        jobs = []
        seen_names = set()
        duplicate_names = []

        for key_item in settings.keys:
            if not key_item.include:
                continue
            if key_item.status not in _SELECTABLE:
                continue

            file_entry = session.files[key_item.file_index]
            key_entry = file_entry.keys[key_item.key_index]

            name = key_item.new_name.strip()
            if not name:
                name = key_entry.new_name or key_entry.name

            name_key = name.casefold()

            # The Basis key is protected and two imported keys cannot share
            # a final name.  The dialog highlights these collisions; only
            # the first key is imported.
            if name_key == 'basis' or name_key in seen_names:
                duplicate_names.append(name)
                continue

            seen_names.add(name_key)
            jobs.append((name, key_entry.record.slots, key_entry.name))

        if not jobs:
            self.report({'WARNING'}, 'No keys selected for import')
            return {'CANCELLED'}

        try:
            stats = bls_importer.apply_shape_keys(
                context,
                settings.target,
                jobs,
            )
        except Exception as exc:
            self.report({'ERROR'}, f'Import failed: {exc}')
            return {'CANCELLED'}

        parts = [f'created: {stats["created"]}']

        if stats['updated']:
            parts.append(f'updated: {stats["updated"]}')
        if stats['skipped']:
            parts.append(f'skipped: {stats["skipped"]}')
        if stats['out_of_range']:
            parts.append(f'out-of-range vertices: {stats["out_of_range"]}')

        info = 'BLS import - ' + ', '.join(parts)
        scene.bls_last_info = info

        if duplicate_names:
            self.report(
                {'WARNING'},
                'Skipped keys with duplicate final names: '
                + ', '.join(sorted(set(duplicate_names))),
            )

        if stats['skipped'] or stats['out_of_range']:
            self.report({'WARNING'}, info)
        else:
            self.report({'INFO'}, info)

        _SESSION = None

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# 3D View panel and File > Import menu
# ---------------------------------------------------------------------------

def _menu_import(self, context):
    """Add both import operators to ``File > Import``."""
    layout = self.layout
    layout.operator(
        'bls_shape_keys.import_files',
        text='J3D Shape Keys (.bls)',
    )
    layout.operator(
        'bls_shape_keys.import_folder',
        text='J3D Shape Keys from Folder (.bls)',
    )


class BLS_PT_main(Panel):
    bl_idname = 'BLS_PT_main'
    bl_label = 'BLS'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'J3D Shape Keys'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.bls_settings

        column = layout.column(align=True)
        column.operator(
            'bls_shape_keys.import_files',
            text='Import BLS Files',
            icon='FILEBROWSER',
        )
        column.operator(
            'bls_shape_keys.import_folder',
            text='Import BLS Folder',
            icon='FILE_FOLDER',
        )

        if _SESSION is not None:
            column.separator()
            column.operator(
                'bls_shape_keys.settings',
                text='Import Settings',
                icon='PREFERENCES',
            )

        if scene.bls_last_info:
            box = layout.box()
            _wrap_label(box, scene.bls_last_info, width=40)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@persistent
def _clear_stale_info(_dummy=None):
    """Drop the statistics text saved by an older add-on version.

    ``bls_last_info`` lives in the scene, so it is written into the ``.blend``
    file.  Files saved with an older, localised version therefore restore a
    stale (for example Russian) message even though the current code only
    produces English text.  A fresh import fills the property again.
    """
    for scene in bpy.data.scenes:
        scene.bls_last_info = ''


_CLASSES = (
    BLS_FileItem,
    BLS_KeyItem,
    BLS_Settings,
    BLS_UL_files,
    BLS_UL_keys,
    BLS_OT_open_file,
    BLS_OT_back_files,
    BLS_OT_page,
    BLS_OT_key_details,
    BLS_OT_import_files,
    BLS_OT_import_folder,
    BLS_OT_settings,
    BLS_PT_main,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.bls_settings = PointerProperty(type=BLS_Settings)
    bpy.types.Scene.bls_last_info = StringProperty(
        name='Last BLS import',
        default='',
    )

    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    bpy.app.handlers.load_post.append(_clear_stale_info)


def unregister():
    global _SESSION

    try:
        bpy.app.handlers.load_post.remove(_clear_stale_info)
    except (AttributeError, ValueError):
        pass

    try:
        bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    except (AttributeError, ValueError):
        pass

    if hasattr(bpy.types.Scene, 'bls_last_info'):
        del bpy.types.Scene.bls_last_info
    if hasattr(bpy.types.Scene, 'bls_settings'):
        del bpy.types.Scene.bls_settings

    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)

    _SESSION = None

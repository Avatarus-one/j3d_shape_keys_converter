# J3D Shape Keys Converter

A Blender add-on that imports shape key data from J3D `.bls` files onto 
a selected scene mesh and exports selected Blender shape keys back to `.bls`.

Supported in Blender 3.0 and newer (tested on 3.0.0, 3.6.23, 4.1.1, 4.2.0,
and 4.5.1).

## AI assistance

The add-on was developed with the help of an AI coding assistant:

- **Model:** DeepSeek V4.1 Flash (OpenCode)

The AI was used for implementation, refactoring, test automation, and
documentation. All changes were reviewed and verified in Blender 3.0, 3.6,
4.1, 4.2, and 4.5.

## Features

- Import one or several selected `.bls` files at once.
- Import a folder, including all nested subfolders.
- Export selected Blender shape keys to a `.bls` file from `File > Export >
  J3D Shape Keys (.bls)` or the sidebar.
- Export includes generated base/key normals and a normal blend table.
- A settings dialog after the file selection:
  - target mesh selection;
  - file list with key, duplicate, conflict, and incompatible counts;
  - shape key list for every file;
  - selection and renaming of the keys to import;
  - automatic J3D (Y-up) to Blender (Z-up) axis conversion.
- Export settings include a shape-key selection list and the same
  **Convert Units** option.
- Automatic comparison of identically named keys between imported .bls files:
  - identical deformation → the key is marked as a duplicate and skipped;
  - different deformation → the key is marked as a conflict and renamed.
- Base keys (`*__base`, the last key of a cluster) are shown for reference.

## Installation

1. Download the `j3d_shape_keys_converter.zip` archive.
2. In Blender open `Edit → Preferences → Add-ons`.
3. Click `Install...` (in Blender 4.2+ use the arrow in the top-right corner →
   `Install from Disk...`) and choose the zip archive.
4. Enable the `J3D Shape Keys Converter` add-on.

For a manual installation you can also copy the `j3d_shape_keys_converter`
folder into the Blender add-ons directory (`scripts/addons/`).

## Usage

1. Select the mesh that should receive the shape keys. This is optional: the
   active mesh is filled in automatically in the settings dialog.
2. Open `File → Import → J3D Shape Keys (.bls)` and select one or several
   files, or `J3D Shape Keys from Folder (.bls)` and select a folder
   (subfolders are searched recursively).
   The same commands are available in the `J3D Shape Keys` tab of the 3D View sidebar.
3. In the settings dialog:
   - check the target mesh;
   - click a file in the list to open its shape keys;
   - clear the checkboxes of unwanted keys;
   - edit the names in the text fields if needed;
   - press `OK`.

## Export

1. Select the mesh that contains the shape keys you want to export.
2. Open `File > Export > J3D Shape Keys (.bls)`.
3. In the export dialog:
   - check the target mesh;
   - select the shape keys to export;
   - edit the **Base Key Name** at the end of the key list (it defaults to
     the target mesh's basis key name;
   - leave **Convert Units** enabled for meshes imported with `j3d_bmd_io`
     (positions are multiplied by `100` to write J3D model units), or disable
     it for Blemd/model-unit meshes;
   - press `OK`.

The exporter writes one BLS cluster containing the selected keys and a trailing
base key. Keys are stored in the selected order. Vertex normals are generated
for the base key and every selected key, and the cluster contains the position
map, normal slots, per-key normal arrays, and the normal blend table used by
the runtime deformer. One normal slot is used per vertex (no normal-slot
grouping). The export is self-contained and does not rely on metadata added by
other add-ons, so it also works for meshes created from scratch.

## Key statuses

| Status | Meaning |
| --- | --- |
| `OK` | Unique key; it will be imported. |
| `Duplicate` | The deformation fully matches a key from another file. Not imported. |
| `Conflict` | The name matches but the deformation differs. The key is renamed and imported. |
| `Base` | The last key of a cluster, used as the base for deltas. Not imported. |
| `No change` | All deltas are zero; the key does not change the geometry. Imported by default. |
| `Incompatible` | The key references vertices outside the target mesh. Not imported. |

If the target mesh already has a key with the same name, the comparison uses
the actual vertex displacement: a full match gives the `Duplicate` status,
while a difference gives `Conflict` with automatic renaming of the imported
key.

When a conflict or a no-change key has to be renamed, the archive folder tag
is used only when the `.bls` file is directly inside a folder named `bls`,
for example `md_blendface_md_mouth_a [Demo22_01]`. In every other case
Blender-style numbering is used, for example
`md_blendface_md_mouth_a.001`.

## Import options

- **Convert Units** (default on) — converts BLS position deltas from J3D
  units to Blender units. When enabled, deltas are multiplied by `0.01`,
  matching a mesh imported with `j3d_bmd_io`'s default unit conversion; disable
  it to keep original units. The same conversion is used when comparing imported 
  keys with existing shape keys, so duplicate/conflict detection stays correct.
- **Include keys without changes** — automatically selects keys that do not
  change the geometry.
- Imported keys whose names match existing shape keys on the target mesh are
  renamed automatically. If the user manually restores the original name,
  the existing key is overwritten. `Basis` is never overwritten.
- If several selected importable keys end up with the same final name, those
  rows are highlighted in the file list. During import only the first key is
  kept; the others are skipped with a warning.

## How the data is read

- A BLS cluster contains a list of model vertex indices. The slot index is used
  directly as the vertex index of the target mesh.
- Each key stores position and normal indices. The low 13 bits of an index are
  the value number in the shared file array. The high 3 bits are the signs of
  the X, Y, and Z components (bit 15 — X, bit 14 — Y, bit 13 — Z). The shared
  array stores absolute values, so without applying these bits the movement of
  some vertices would be mirrored.
- The last key of each cluster is treated as the base. The geometry of the
  other keys is recalculated as the difference from the base key, with the
  signs already applied.
- Deltas are rotated from J3D's Y-up space to Blender's Z-up space and
  multiplied by the **Convert Units** factor before being added to the target
  mesh.
- Blender shape keys store absolute coordinates, so the BLS deltas are added
  to the base mesh coordinates. This lets the import work on a mesh with an
  already modified base pose, independently of the object transformation.

## Limitations

- BLS vertex indices must match the topology of the target mesh. The import is
  intended for a mesh of the same model the BLS was made for. Keys that
  reference vertices outside the mesh receive the `Incompatible` status and
  are not imported.
- BLS normals do not participate in shape key creation: Blender stores only
  vertex coordinates, and normals are recalculated from the geometry.
- BLS export does not group vertices by shared normals. Every mesh vertex gets
  its own normal slot, so exported files may contain more normal slots than the
  original BLS.
- Split-normal export is not supported. Each vertex writes to a single normal
  destination; the exporter does not reproduce the multiple BMD normal indices
  that original BLS files use for split normals.
- Duplicates and conflicts are determined by the actual vertex displacement:
  only non-zero position deltas are summed, and each vertex is compared with a
  tolerance of 0.001. Normals, zero slots, and a different number of slots do
  not affect the result and do not create false conflicts.
- If two identically named keys move the same vertices in the same way, the
  second one is treated as a duplicate and is not imported by default. A
  conflict remains only when the vertices move differently.

# J3D Shape Keys Converter

A Blender add-on that imports shape key data from J3D `.bls` files onto 
a selected scene mesh.

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
- A settings dialog after the file selection:
  - target mesh selection;
  - file list with key, duplicate, conflict, and incompatible counts;
  - shape key list for every file;
  - selection and renaming of the keys to import;
  - automatic J3D (Y-up) to Blender (Z-up) axis conversion.
- Automatic comparison of identically named keys between files:
  - identical deformation → the key is marked as a duplicate and skipped;
  - different deformation → the key is marked as a conflict and renamed.
- Base keys (`*__base`, the last key of a cluster) and no-change keys
  (matching the base) are shown for reference but are not imported by default.

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

## Key statuses

| Status | Meaning |
| --- | --- |
| `OK` | Unique key; it will be imported. |
| `Duplicate` | The deformation fully matches a key from another file. Not imported. |
| `Conflict` | The name matches but the deformation differs. The key is renamed and imported. |
| `Base` | The last key of a cluster, used as the base for deltas. Not imported. |
| `No change` | All deltas are zero; the key does not change the geometry. Not imported by default. |
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

- **Include keys without changes** — automatically selects keys that do not
  change the geometry. By default such keys are not imported.
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
- Duplicates and conflicts are determined by the actual vertex displacement:
  only non-zero position deltas are summed, and each vertex is compared with a
  tolerance of 0.001. Normals, zero slots, and a different number of slots do
  not affect the result and do not create false conflicts.
- If two identically named keys move the same vertices in the same way, the
  second one is treated as a duplicate and is not imported by default. A
  conflict remains only when the vertices move differently.

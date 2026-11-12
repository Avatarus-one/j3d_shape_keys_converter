"""
J3D Shape Keys Converter
========================

Imports shape keys from J3D ``.bls`` files onto a selected mesh and exports
selected Blender shape keys back to ``.bls``.

Select one or several BLS files, or a folder (including subfolders),
review the parsed keys, exclude or rename them, and apply the result
as Blender shape keys.

The add-on adds entries to ``File > Import`` and a panel to the
3D View sidebar.

Developed with AI assistance: DeepSeek V4.1 Flash (OpenCode) was used
for implementation, testing, and documentation.
"""

bl_info = {
    'name': 'J3D Shape Keys Converter',
    'author': 'Avatarus',
    'version': (1, 1, 0),
    'blender': (3, 0, 0),
    'location': 'File > Import/Export, 3D View > Sidebar > J3D Shape Keys',
    'description': 'Import and export J3D .bls shape keys',
    'category': 'Import-Export',
}

def register():
    # Imported lazily so the parser/session modules can be tested outside
    # Blender with a plain Python interpreter.
    from . import bls_ui
    bls_ui.register()


def unregister():
    from . import bls_ui
    bls_ui.unregister()


if __name__ == '__main__':
    register()

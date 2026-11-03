"""Startup-only data initialization/import. Never switch a running game's profile."""
import json
from pathlib import Path

from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

import ngpc_migration as migration
import ngpc_paths as paths


def import_dialog(base: Path) -> bool:
    source = QFileDialog.getExistingDirectory(
        None, 'Select the old .app or its user-data folder', '',
        QFileDialog.Option.DontUseNativeDialog)
    if not source:
        return False
    origin, ok = QInputDialog.getText(
        None, 'Original installation location',
        'If this is a renamed/moved backup, enter its ORIGINAL .app or data path.\n'
        'Only references to verified copied files will be remapped.', text=source)
    if not ok:
        return False
    try:
        plan = migration.preview(Path(source), base, origin=Path(origin))
        if plan.conflicts:
            QMessageBox.warning(None, 'Import needs explicit bindings',
                                '\n'.join(plan.conflicts) + '\n\nNo files were copied. '
                                'Use the documented preview tool to supply ROM bindings.')
            return False
        box = QMessageBox()
        box.setWindowTitle('Preview legacy data import')
        box.setText(f'Back up and copy {len(plan.files)} files into:\n'
                    f'{base / "profiles" / plan.profile}\n\n'
                    'Close every old/new emulator instance first. No source files or '
                    'external ROM/BIOS files will be moved, overwritten or deleted.\n'
                    'An existing profile will be retained, not merged.')
        box.setDetailedText(json.dumps(plan.json(), ensure_ascii=False, indent=2))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return False
        result = migration.execute(plan, confirmed=True)
        QMessageBox.information(None, 'Import verified',
                                f'Import state: {result["state"]}. Old data and backup are retained.\n'
                                'Restart NgpCraft to use the imported profile.')
        return True
    except (OSError, ValueError, migration.MigrationError, paths.DataPathError) as exc:
        QMessageBox.critical(None, 'Import stopped',
                             f'{exc}\n\nNo blank profile will be substituted. '
                             'Keep the old application and inspect migration status/backups.')
        return False


def prepare(*, force_import: bool = False) -> bool:
    """True means existing/fresh storage is ready; import always requires restart."""
    base = paths.base_root()
    if force_import:
        import_dialog(base)
        return False
    if paths.read_active(base):
        return True
    pending = migration.recover(base)
    if migration.legacy_present(paths.install_root()) or pending:
        box = QMessageBox()
        box.setWindowTitle('Existing user data needs review')
        box.setText('Legacy data or an interrupted import was found. Import from the old '
                    'application or a preserved backup before playing. No files will '
                    'be automatically moved, deleted or replaced.')
        action = box.addButton('Choose legacy data and preview…', QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() == action:
            import_dialog(base)
        return False
    # Other/renamed application bundles cannot be discovered reliably. Ask before
    # creating fresh storage, even when this installation contains no legacy files.
    box = QMessageBox()
    box.setWindowTitle('Choose user data')
    box.setText('If you used NgpCraft before, import data from the preserved old .app '
                'or its backup. Replacing an old .app can discard its saves. '
                'External ROMs/BIOS and native preferences are not moved.\n\n'
                f'New data will live outside the application:\n{base}')
    action = box.addButton('Import existing data…', QMessageBox.ButtonRole.ActionRole)
    fresh = box.addButton('Create a new profile', QMessageBox.ButtonRole.ActionRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.exec()
    if box.clickedButton() == action:
        import_dialog(base)
        return False
    if box.clickedButton() == fresh:
        paths.initialize_empty(base)
        return True
    return False

"""Startup-only data initialization/import. Never switch a running game's profile."""
import json
from pathlib import Path

from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

import ngpc_migration as migration
import ngpc_paths as paths
import ngpc_settings as cfg


def _t(key, **values):
    try:
        lang = cfg.language(cfg.make_settings())
    except (OSError, ValueError, paths.DataPathError):
        lang = cfg.FALLBACK_LANG  # Language fallback only; never a data-path fallback.
    return cfg.tr(lang, key).format(**values)


def show_error(error):
    QMessageBox.critical(None, _t('storage_error_title'), _t('storage_error', error=error))


def require_managed_error():
    QMessageBox.critical(None, _t('storage_error_title'), _t('storage_external_required'))


def import_dialog(base: Path) -> bool:
    source = QFileDialog.getExistingDirectory(
        None, _t('storage_select_source'), '', QFileDialog.Option.DontUseNativeDialog)
    if not source:
        return False
    origin, ok = QInputDialog.getText(
        None, _t('storage_origin_title'), _t('storage_origin'), text=source)
    if not ok:
        return False
    try:
        plan = migration.preview(Path(source), base, origin=Path(origin))
        if plan.conflicts:
            QMessageBox.warning(None, _t('storage_conflicts_title'),
                                _t('storage_conflicts', details='\n'.join(plan.conflicts)))
            return False
        box = QMessageBox()
        box.setWindowTitle(_t('storage_preview_title'))
        box.setText(_t('storage_preview', count=len(plan.files),
                       destination=base / 'profiles' / plan.profile))
        box.setDetailedText(json.dumps(plan.json(), ensure_ascii=False, indent=2))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return False
        result = migration.execute(plan, confirmed=True)
        QMessageBox.information(None, _t('storage_done_title'),
                                _t('storage_done', state=result['state']))
        return True
    except (OSError, ValueError, migration.MigrationError, paths.DataPathError) as exc:
        show_error(exc)
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
        box.setWindowTitle(_t('storage_existing_title'))
        box.setText(_t('storage_existing'))
        action = box.addButton(_t('storage_choose_legacy'), QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() == action:
            import_dialog(base)
        return False
    # Other/renamed application bundles cannot be discovered reliably. Ask before
    # creating fresh storage, even when this installation contains no legacy files.
    box = QMessageBox()
    box.setWindowTitle(_t('storage_choose_title'))
    box.setText(_t('storage_choose', base=base))
    action = box.addButton(_t('storage_import_button'), QMessageBox.ButtonRole.ActionRole)
    fresh = box.addButton(_t('storage_fresh_button'), QMessageBox.ButtonRole.ActionRole)
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

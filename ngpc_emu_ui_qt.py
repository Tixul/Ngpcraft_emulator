"""PyQt6 debugger UI for NgpCraft Emulator.

ROADMAP §4 fixes the final core in C++ and the frontend in PyQt
("UI PyQt existante" — Mode 2 + §298 mention PyQt6). This module is
the PyQt6 frontend ; the tech-neutral session/state lives in
`core/emulator_session.py`.

Layout (QMainWindow):
- Top dock-row : LCD canvas (160×152 ×3 = 480×456) + CPU registers
  panel
- Toolbar row : Step / Step 10 / Step 1000 / Step Frame / Run-Pause
  / Reset
- Bottom dock-row : Disassembly panel (around PC) + Memory hex view
- Menu bar : File → Open ROM / Load Savestate / Save Savestate / Quit
- Status bar : frame / scanline / VBLANK / IRQ / cycles / last stop

Continuous-run loop : `QTimer.timeout` every 16 ms ticks ~1000
instructions. Auto-stops on blocked execution.

Launch via `python ngpc_emu.py ui <rom>`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QEvent, Qt, QTimer, QSettings
from PyQt6.QtGui import (
    QAction, QFont, QImage, QKeySequence, QPixmap, QTextCharFormat,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from core.emulator_session import EmulatorSession
from core.k2ge import TILEMAP_TILES_PER_COL, TILEMAP_TILES_PER_ROW


LCD_WIDTH = 160
LCD_HEIGHT = 152
LCD_SCALE = 3
RUN_TICK_INTERVAL_MS = 16
RUN_STEPS_PER_TICK = 1000
DISASM_LINES = 14
MEMORY_ROWS = 12
MEMORY_BYTES_PER_ROW = 16
MONO_FONT_FAMILY = "Consolas"
K2GE_TEXT_ROWS = 18
WINDOW_LAYOUT_STATE_VERSION = 1

# UI 0.4: breakpoint marker glyph in the disasm gutter (filled
# circle in front of the address column for lines that have a BP).
BREAKPOINT_GLYPH = "●"  # ●


JOYPAD_KEY_TO_MASK = {
    Qt.Key.Key_Up: EmulatorSession.JOYPAD_UP,
    Qt.Key.Key_Down: EmulatorSession.JOYPAD_DOWN,
    Qt.Key.Key_Left: EmulatorSession.JOYPAD_LEFT,
    Qt.Key.Key_Right: EmulatorSession.JOYPAD_RIGHT,
    Qt.Key.Key_Z: EmulatorSession.JOYPAD_A,
    Qt.Key.Key_X: EmulatorSession.JOYPAD_B,
    Qt.Key.Key_Return: EmulatorSession.JOYPAD_OPTION,
    Qt.Key.Key_Enter: EmulatorSession.JOYPAD_OPTION,
}
JOYPAD_BITS_IN_ORDER = (
    (EmulatorSession.JOYPAD_UP, "Up"),
    (EmulatorSession.JOYPAD_DOWN, "Down"),
    (EmulatorSession.JOYPAD_LEFT, "Left"),
    (EmulatorSession.JOYPAD_RIGHT, "Right"),
    (EmulatorSession.JOYPAD_A, "A"),
    (EmulatorSession.JOYPAD_B, "B"),
    (EmulatorSession.JOYPAD_OPTION, "Option"),
)


def _mono(size: int = 9, bold: bool = False) -> QFont:
    font = QFont(MONO_FONT_FAMILY, size)
    font.setStyleHint(QFont.StyleHint.Monospace)
    if bold:
        font.setBold(True)
    return font


def _make_readonly_text(rows: int) -> QPlainTextEdit:
    text = QPlainTextEdit()
    text.setReadOnly(True)
    text.setFont(_mono(9))
    text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    fm = text.fontMetrics()
    text.setFixedHeight(fm.lineSpacing() * rows + 8)
    return text


class EmulatorWindow(QMainWindow):
    """Main window wrapping an `EmulatorSession`.

    Accepts `rom_path=None` so the UI can launch empty and pick a
    ROM via File → Open ROM…. The session is mutable through
    `_set_session(session_or_none)` so File → Close ROM can return
    to the empty state too.
    """

    def __init__(
        self,
        rom_path: Path | None = None,
        *,
        bios_path: Path | None = None,
    ) -> None:
        super().__init__()
        # QSettings persistence (pass 52) — remembers last-used
        # directories across runs so file dialogs open where the
        # user was last working instead of $HOME.
        # The QSettings key path is rooted under the Anthropic-free
        # "NgpCraft / Emulator" namespace.
        self._settings = QSettings("NgpCraft", "Emulator")
        self._layout_restored = False
        # Tracks the last loaded/saved savestate path so File → Save
        # Savestate (Ctrl+S) can write back without re-prompting. Save
        # Savestate As… always prompts and updates this.
        self._current_savestate_path: Path | None = None
        self._disasm_view_address: int | None = None
        self.session: EmulatorSession | None = None
        self._bios_path: Path | None = Path(bios_path) if bios_path is not None else None

        # Continuous-run loop state.
        self._timer = QTimer(self)
        self._timer.setInterval(RUN_TICK_INTERVAL_MS)
        self._timer.timeout.connect(self._run_tick)
        self._running = False
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        self._build_menu()
        self._build_central()
        # `_build_central` calls `_build_docks` which populates
        # `self._docks` ; we now use that to populate the View menu.
        self._populate_view_menu()
        self.setStatusBar(QStatusBar(self))
        self._restore_window_layout()

        if rom_path is not None:
            self._set_session(
                EmulatorSession(rom_path, bios_path=self._bios_path), rom_path.name,
            )
        else:
            self._set_session(None, None)

    def _set_session(
        self, session: EmulatorSession | None, rom_label: str | None,
    ) -> None:
        """Swap the live session (None = empty / "no ROM loaded" state)."""
        if self._running:
            self._stop_run_loop()
        self.session = session
        self._current_savestate_path = None
        self._disasm_view_address = None
        title_suffix = rom_label or "(no ROM loaded)"
        self.setWindowTitle(f"NgpCraft Emulator — {title_suffix}")
        self._update_action_enabled_state()
        self.refresh_all()

    def _update_action_enabled_state(self) -> None:
        """Enable session-dependent actions only when a ROM is loaded."""
        has_session = self.session is not None
        for action in self._session_actions:
            action.setEnabled(has_session)
        for widget in self._session_buttons:
            widget.setEnabled(has_session)

    # ----- Construction -----

    def _build_menu(self) -> None:
        """Classic File menu with standard shortcuts.

        Sections (separated by separators) :
          1. ROM I/O      : Open ROM…(Ctrl+O), Close ROM(Ctrl+W)
          2. Savestate I/O: Load Savestate…(Ctrl+L), Save Savestate(Ctrl+S),
                            Save Savestate As…(Ctrl+Shift+S)
          3. App lifecycle: Quit(Ctrl+Q)
        """
        # Actions that require an active session — captured so we
        # can enable/disable them in `_update_action_enabled_state`.
        self._session_actions: list[QAction] = []

        file_menu = self.menuBar().addMenu("&File")

        open_act = QAction("&Open ROM…", self)
        open_act.setShortcut(QKeySequence.StandardKey.Open)  # Ctrl+O
        open_act.triggered.connect(self._on_open_rom)
        file_menu.addAction(open_act)

        load_bios_act = QAction("Load &BIOS Image…", self)
        load_bios_act.triggered.connect(self._on_load_bios_image)
        file_menu.addAction(load_bios_act)

        clear_bios_act = QAction("C&lear BIOS Image", self)
        clear_bios_act.triggered.connect(self._on_clear_bios_image)
        file_menu.addAction(clear_bios_act)

        close_act = QAction("&Close ROM", self)
        close_act.setShortcut(QKeySequence.StandardKey.Close)  # Ctrl+W
        close_act.triggered.connect(self._on_close_rom)
        file_menu.addAction(close_act)
        self._session_actions.append(close_act)

        file_menu.addSeparator()

        load_act = QAction("&Load Savestate…", self)
        load_act.setShortcut(QKeySequence("Ctrl+L"))
        load_act.triggered.connect(self._on_load_savestate)
        file_menu.addAction(load_act)
        self._session_actions.append(load_act)

        save_act = QAction("&Save Savestate", self)
        save_act.setShortcut(QKeySequence.StandardKey.Save)  # Ctrl+S
        save_act.triggered.connect(self._on_save_savestate)
        file_menu.addAction(save_act)
        self._session_actions.append(save_act)

        save_as_act = QAction("Save Savestate &As…", self)
        save_as_act.setShortcut(QKeySequence.StandardKey.SaveAs)  # Ctrl+Shift+S
        save_as_act.triggered.connect(self._on_save_savestate_as)
        file_menu.addAction(save_as_act)
        self._session_actions.append(save_as_act)

        file_menu.addSeparator()

        load_map_act = QAction("Load Symbol &Map…", self)
        load_map_act.triggered.connect(self._on_load_symbol_map)
        file_menu.addAction(load_map_act)
        self._session_actions.append(load_map_act)

        file_menu.addSeparator()

        load_bp_act = QAction("Load &Breakpoints", self)
        load_bp_act.triggered.connect(self._on_load_breakpoints_registry)
        file_menu.addAction(load_bp_act)
        self._session_actions.append(load_bp_act)

        save_bp_act = QAction("Save B&reakpoints", self)
        save_bp_act.triggered.connect(self._on_save_breakpoints_registry)
        file_menu.addAction(save_bp_act)
        self._session_actions.append(save_bp_act)

        load_wp_act = QAction("Load &Watchpoints", self)
        load_wp_act.triggered.connect(self._on_load_watchpoints_registry)
        file_menu.addAction(load_wp_act)
        self._session_actions.append(load_wp_act)

        save_wp_act = QAction("Save W&atchpoints", self)
        save_wp_act.triggered.connect(self._on_save_watchpoints_registry)
        file_menu.addAction(save_wp_act)
        self._session_actions.append(save_wp_act)

        file_menu.addSeparator()

        quit_act = QAction("&Quit", self)
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)  # Ctrl+Q
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        # ----- View menu : populated in `_build_docks` -----
        # We capture the menu handle here ; the dock toggle actions are
        # added to it after the docks themselves are created (see
        # `_populate_view_menu`).
        self._view_menu = self.menuBar().addMenu("&View")

    def _populate_view_menu(self) -> None:
        """Add a toggle action per floating dock to the View menu.

        Called once after `_build_docks`. Each entry is a
        `QDockWidget.toggleViewAction()` — checkable, auto-syncs with
        the dock's visibility (e.g. clicking the dock's [X] unchecks
        the menu entry, clicking the entry shows/hides the dock).
        """
        self._view_menu.clear()
        # Stable display order (matches construction order in _build_docks).
        order = [
            "registers", "disasm", "memory", "breakpoints", "watchpoints",
            "k2ge_video", "k2ge_palettes", "k2ge_oam", "k2ge_tilemaps",
        ]
        for name in order:
            dock = self._docks.get(name)
            if dock is None:
                continue
            action = dock.toggleViewAction()
            # toggleViewAction's text defaults to the dock title.
            self._view_menu.addAction(action)
        self._view_menu.addSeparator()

        # Convenience : show all / hide all (except LCD).
        show_all_act = QAction("Show &All Inspector Windows", self)
        show_all_act.triggered.connect(self._on_show_all_docks)
        self._view_menu.addAction(show_all_act)
        hide_all_act = QAction("&Hide All Inspector Windows", self)
        hide_all_act.triggered.connect(self._on_hide_all_docks)
        self._view_menu.addAction(hide_all_act)
        self._view_menu.addSeparator()
        reset_layout_act = QAction("&Reset Window Layout", self)
        reset_layout_act.triggered.connect(self._on_reset_layout)
        self._view_menu.addAction(reset_layout_act)

    def _on_show_all_docks(self) -> None:
        for dock in self._docks.values():
            dock.show()

    def _on_hide_all_docks(self) -> None:
        for dock in self._docks.values():
            dock.hide()

    def _on_reset_layout(self) -> None:
        """Restore all inspector windows to their default floating positions."""
        self._clear_window_layout_settings()
        for dock in self._docks.values():
            dock.show()
            dock.setFloating(True)
        self._arrange_floating_docks()
        self._save_window_layout()

    def _build_central(self) -> None:
        """Compact central widget = LCD canvas + button row. All
        inspectors live in floating `QDockWidget`s built by
        `_build_docks`.
        """
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 4)
        outer.setSpacing(6)

        # LCD on top.
        lcd_row = QHBoxLayout()
        lcd_row.addStretch()
        lcd_row.addWidget(self._build_lcd())
        lcd_row.addStretch()
        outer.addLayout(lcd_row)

        # Button row below the LCD.
        outer.addLayout(self._build_buttons_row())

        self.setCentralWidget(central)
        # Build the floating inspector docks alongside the central widget.
        self._build_docks()

    def _build_docks(self) -> None:
        """Wrap each inspector panel in a floating `QDockWidget`.

        Tracks them in `self._docks` (dict by name) and registers
        their `toggleViewAction` into the View menu — so the user
        can show / hide each window independently.

        Layout strategy : at construction time all docks are added
        to a docking area then immediately `setFloating(True)` ;
        their initial geometry is computed relative to the main
        window so they don't all land on top of each other.
        """
        self._docks: dict[str, QDockWidget] = {}

        registers = self._build_registers_panel()
        self._docks["registers"] = self._make_dock(
            "CPU Registers", registers,
            Qt.DockWidgetArea.RightDockWidgetArea,
        )

        disasm = self._build_disasm_panel()
        self._docks["disasm"] = self._make_dock(
            "Disassembly", disasm,
            Qt.DockWidgetArea.BottomDockWidgetArea,
        )

        memory = self._build_memory_panel()
        self._docks["memory"] = self._make_dock(
            "Memory", memory,
            Qt.DockWidgetArea.BottomDockWidgetArea,
        )

        breakpoints = self._build_breakpoints_panel()
        self._docks["breakpoints"] = self._make_dock(
            "Breakpoints", breakpoints,
            Qt.DockWidgetArea.RightDockWidgetArea,
        )

        watchpoints = self._build_watchpoints_panel()
        self._docks["watchpoints"] = self._make_dock(
            "Watchpoints", watchpoints,
            Qt.DockWidgetArea.RightDockWidgetArea,
        )

        k2ge_video = self._build_k2ge_video_panel()
        self._docks["k2ge_video"] = self._make_dock(
            "K2GE Video", k2ge_video,
            Qt.DockWidgetArea.RightDockWidgetArea,
        )

        k2ge_palettes = self._build_k2ge_palettes_panel()
        self._docks["k2ge_palettes"] = self._make_dock(
            "K2GE Palettes", k2ge_palettes,
            Qt.DockWidgetArea.RightDockWidgetArea,
        )

        k2ge_oam = self._build_k2ge_oam_panel()
        self._docks["k2ge_oam"] = self._make_dock(
            "K2GE OAM", k2ge_oam,
            Qt.DockWidgetArea.RightDockWidgetArea,
        )

        k2ge_tilemaps = self._build_k2ge_tilemaps_panel()
        self._docks["k2ge_tilemaps"] = self._make_dock(
            "K2GE Tilemaps", k2ge_tilemaps,
            Qt.DockWidgetArea.BottomDockWidgetArea,
        )

    def _make_dock(
        self, title: str, content: QWidget, area: Qt.DockWidgetArea,
    ) -> QDockWidget:
        """Wrap a panel widget in a floating dock window — hidden by default.

        The user opens the windows on demand via View → <name>. The
        dock's `toggleViewAction()` keeps the menu entry in sync with
        visibility.
        """
        dock = QDockWidget(title, self)
        dock.setObjectName(f"dock_{title.lower().replace(' ', '_')}")
        dock.setWidget(content)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.addDockWidget(area, dock)
        # Float (so when the user opens it, it appears as an
        # independent top-level window) — but hidden by default.
        dock.setFloating(True)
        dock.hide()
        return dock

    def _arrange_floating_docks(self) -> None:
        """Position the floating docks around the main window so they
        don't all stack on top of each other on first show.

        Called once after `show()` — Qt needs the main window's
        geometry to be realized before we can compute neighbour
        positions.
        """
        if not self._docks:
            return
        main_geom = self.frameGeometry()
        gap = 8
        right_x = main_geom.right() + gap
        below_y = main_geom.bottom() + gap
        k2ge_x = right_x + 308
        lower_y = below_y + 288
        # Right column : CPU Registers (top), Breakpoints + Watchpoints below.
        reg = self._docks["registers"]
        reg.resize(280, 320)
        reg.move(right_x, main_geom.top())
        bp = self._docks["breakpoints"]
        bp.resize(280, 240)
        bp.move(right_x, main_geom.top() + reg.height() + gap)
        wp = self._docks["watchpoints"]
        wp.resize(280, 240)
        wp.move(right_x, main_geom.top() + reg.height() + bp.height() + gap * 2)
        # K2GE column : video summary + palette inspector.
        video = self._docks["k2ge_video"]
        video.resize(320, 220)
        video.move(k2ge_x, main_geom.top())
        palettes = self._docks["k2ge_palettes"]
        palettes.resize(520, 320)
        palettes.move(k2ge_x, main_geom.top() + video.height() + gap)
        # Bottom row : Disassembly, Memory, Tilemaps.
        disasm = self._docks["disasm"]
        disasm.resize(520, 280)
        disasm.move(main_geom.left(), below_y)
        mem = self._docks["memory"]
        mem.resize(520, 280)
        mem.move(main_geom.left() + disasm.width() + gap, below_y)
        tilemaps = self._docks["k2ge_tilemaps"]
        tilemaps.resize(420, 280)
        tilemaps.move(main_geom.left() + disasm.width() + mem.width() + gap * 2, below_y)
        # Lower row : OAM below the main window.
        oam = self._docks["k2ge_oam"]
        oam.resize(720, 300)
        oam.move(main_geom.left(), lower_y)

    def _build_lcd(self) -> QLabel:
        self._lcd_label = QLabel()
        self._lcd_label.setFixedSize(
            LCD_WIDTH * LCD_SCALE, LCD_HEIGHT * LCD_SCALE,
        )
        self._lcd_label.setFrameShape(QFrame.Shape.Box)
        self._lcd_label.setStyleSheet("background-color: black;")
        return self._lcd_label

    def _build_registers_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        grid = QGridLayout(frame)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setHorizontalSpacing(8)

        title = QLabel("CPU Registers")
        title.setFont(_mono(10, bold=True))
        grid.addWidget(title, 0, 0, 1, 2)

        self._reg_labels: dict[str, QLabel] = {}
        names = [
            "PC", "XSP", "XWA", "XBC", "XDE", "XHL",
            "XIX", "XIY", "XIZ", "iff_level", "rfp", "flags",
        ]
        for i, name in enumerate(names, start=1):
            label = QLabel(f"{name}:")
            label.setFont(_mono(9))
            value = QLabel("—")
            value.setFont(_mono(9, bold=True))
            grid.addWidget(label, i, 0)
            grid.addWidget(value, i, 1)
            self._reg_labels[name] = value
        grid.setRowStretch(len(names) + 1, 1)
        return frame

    def _build_buttons_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        # All execution buttons are disabled until a ROM is loaded.
        self._session_buttons: list[QPushButton] = []
        defs = [
            ("Step", lambda: self._step_n(1)),
            ("Step 10", lambda: self._step_n(10)),
            ("Step 1000", lambda: self._step_n(1000)),
            ("Step Frame", self._on_step_frame),
        ]
        for label, slot in defs:
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            row.addWidget(btn)
            self._session_buttons.append(btn)
        self._run_button = QPushButton("Run")
        self._run_button.clicked.connect(self._on_run_toggle)
        row.addWidget(self._run_button)
        self._session_buttons.append(self._run_button)
        row.addSpacing(20)
        reset_btn = QPushButton("Reset")
        reset_btn.clicked.connect(self._on_reset)
        row.addWidget(reset_btn)
        self._session_buttons.append(reset_btn)
        row.addStretch()
        return row

    def _build_disasm_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)
        title = QLabel("Disassembly (around PC)")
        title.setFont(_mono(10, bold=True))
        layout.addWidget(title)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Go to:"))
        self._disasm_address_edit = QLineEdit()
        self._disasm_address_edit.setPlaceholderText("0x200040 or symbol")
        self._disasm_address_edit.returnPressed.connect(self._on_disasm_go)
        controls.addWidget(self._disasm_address_edit, 1)
        disasm_go_btn = QPushButton("Go")
        disasm_go_btn.clicked.connect(self._on_disasm_go)
        controls.addWidget(disasm_go_btn)
        disasm_pc_btn = QPushButton("@PC")
        disasm_pc_btn.clicked.connect(self._on_disasm_at_pc)
        controls.addWidget(disasm_pc_btn)
        layout.addLayout(controls)
        self._disasm_text = QPlainTextEdit()
        self._disasm_text.setReadOnly(True)
        self._disasm_text.setFont(_mono(9))
        self._disasm_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # Approx. DISASM_LINES rows tall.
        fm = self._disasm_text.fontMetrics()
        self._disasm_text.setFixedHeight(fm.lineSpacing() * DISASM_LINES + 8)
        layout.addWidget(self._disasm_text)
        return frame

    def _build_memory_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)
        title = QLabel("Memory")
        title.setFont(_mono(10, bold=True))
        layout.addWidget(title)
        # Address bar.
        addr_row = QHBoxLayout()
        addr_label = QLabel("Address:")
        addr_label.setFont(_mono(9))
        addr_row.addWidget(addr_label)
        self._mem_address_edit = QLineEdit("0x00200040")
        self._mem_address_edit.setFont(_mono(9))
        self._mem_address_edit.setMaximumWidth(110)
        self._mem_address_edit.returnPressed.connect(self._on_memory_go)
        addr_row.addWidget(self._mem_address_edit)
        for label, slot in (
            ("Go", self._on_memory_go),
            ("@PC", self._on_memory_at_pc),
            ("@XSP", self._on_memory_at_xsp),
        ):
            btn = QPushButton(label)
            btn.setMaximumWidth(50)
            btn.clicked.connect(slot)
            addr_row.addWidget(btn)
        addr_row.addStretch()
        layout.addLayout(addr_row)
        self._mem_text = QPlainTextEdit()
        self._mem_text.setReadOnly(True)
        self._mem_text.setFont(_mono(9))
        self._mem_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        fm = self._mem_text.fontMetrics()
        self._mem_text.setFixedHeight(fm.lineSpacing() * MEMORY_ROWS + 8)
        layout.addWidget(self._mem_text)
        return frame

    def _build_breakpoints_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)
        title = QLabel("Breakpoints")
        title.setFont(_mono(10, bold=True))
        layout.addWidget(title)

        # Add-by-address row.
        add_row = QHBoxLayout()
        self._bp_address_edit = QLineEdit()
        self._bp_address_edit.setPlaceholderText("0xADDRESS")
        self._bp_address_edit.setFont(_mono(9))
        self._bp_address_edit.setMaximumWidth(110)
        self._bp_address_edit.returnPressed.connect(self._on_add_breakpoint)
        add_row.addWidget(self._bp_address_edit)
        add_btn = QPushButton("Add")
        add_btn.setMaximumWidth(50)
        add_btn.clicked.connect(self._on_add_breakpoint)
        add_row.addWidget(add_btn)
        bp_at_pc = QPushButton("@PC")
        bp_at_pc.setMaximumWidth(50)
        bp_at_pc.clicked.connect(self._on_add_breakpoint_at_pc)
        add_row.addWidget(bp_at_pc)
        layout.addLayout(add_row)

        # Breakpoint list (double-click removes).
        self._bp_list = QListWidget()
        self._bp_list.setFont(_mono(9))
        self._bp_list.itemDoubleClicked.connect(
            self._on_breakpoint_double_clicked,
        )
        layout.addWidget(self._bp_list, 1)

        # Remove / Clear row.
        actions_row = QHBoxLayout()
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._on_remove_selected_breakpoint)
        actions_row.addWidget(remove_btn)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._on_clear_breakpoints)
        actions_row.addWidget(clear_btn)
        actions_row.addStretch()
        layout.addLayout(actions_row)
        return frame

    def _build_watchpoints_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)
        title = QLabel("Watchpoints")
        title.setFont(_mono(10, bold=True))
        layout.addWidget(title)

        # Address + kind + value row.
        add_row = QHBoxLayout()
        self._wp_address_edit = QLineEdit()
        self._wp_address_edit.setPlaceholderText("0xADDR")
        self._wp_address_edit.setFont(_mono(9))
        self._wp_address_edit.setMaximumWidth(95)
        self._wp_address_edit.returnPressed.connect(self._on_add_watchpoint)
        add_row.addWidget(self._wp_address_edit)

        self._wp_kind_combo = QComboBox()
        self._wp_kind_combo.addItems(["write", "read", "access"])
        self._wp_kind_combo.setMaximumWidth(80)
        add_row.addWidget(self._wp_kind_combo)

        self._wp_value_edit = QLineEdit()
        self._wp_value_edit.setPlaceholderText("=val (opt)")
        self._wp_value_edit.setFont(_mono(9))
        self._wp_value_edit.setMaximumWidth(80)
        self._wp_value_edit.returnPressed.connect(self._on_add_watchpoint)
        add_row.addWidget(self._wp_value_edit)

        add_btn = QPushButton("Add")
        add_btn.setMaximumWidth(50)
        add_btn.clicked.connect(self._on_add_watchpoint)
        add_row.addWidget(add_btn)
        layout.addLayout(add_row)

        # Watchpoint list.
        self._wp_list = QListWidget()
        self._wp_list.setFont(_mono(9))
        self._wp_list.itemDoubleClicked.connect(
            self._on_watchpoint_double_clicked,
        )
        layout.addWidget(self._wp_list, 1)

        # Remove / Clear row.
        actions_row = QHBoxLayout()
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._on_remove_selected_watchpoint)
        actions_row.addWidget(remove_btn)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._on_clear_watchpoints)
        actions_row.addWidget(clear_btn)
        actions_row.addStretch()
        layout.addLayout(actions_row)
        return frame

    def _build_k2ge_video_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)
        title = QLabel("K2GE Video")
        title.setFont(_mono(10, bold=True))
        layout.addWidget(title)
        self._k2ge_video_text = _make_readonly_text(10)
        layout.addWidget(self._k2ge_video_text)
        return frame

    def _build_k2ge_palettes_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)
        title = QLabel("K2GE Palettes")
        title.setFont(_mono(10, bold=True))
        layout.addWidget(title)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Plane:"))
        self._k2ge_palette_kind = QComboBox()
        self._k2ge_palette_kind.addItems(
            ["all", "sprite", "scr1", "scr2", "background", "window"]
        )
        self._k2ge_palette_kind.currentIndexChanged.connect(
            lambda _=None: self._refresh_k2ge_palettes(),
        )
        controls.addWidget(self._k2ge_palette_kind)
        controls.addStretch()
        layout.addLayout(controls)

        self._k2ge_palettes_text = _make_readonly_text(K2GE_TEXT_ROWS)
        layout.addWidget(self._k2ge_palettes_text)
        return frame

    def _build_k2ge_oam_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)
        title = QLabel("K2GE OAM")
        title.setFont(_mono(10, bold=True))
        layout.addWidget(title)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Show:"))
        self._k2ge_oam_filter = QComboBox()
        self._k2ge_oam_filter.addItems(["visible only", "all"])
        self._k2ge_oam_filter.currentIndexChanged.connect(
            lambda _=None: self._refresh_k2ge_oam(),
        )
        controls.addWidget(self._k2ge_oam_filter)
        controls.addStretch()
        layout.addLayout(controls)

        self._k2ge_oam_text = _make_readonly_text(K2GE_TEXT_ROWS)
        layout.addWidget(self._k2ge_oam_text)
        return frame

    def _build_k2ge_tilemaps_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)
        title = QLabel("K2GE Tilemaps")
        title.setFont(_mono(10, bold=True))
        layout.addWidget(title)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Plane:"))
        self._k2ge_tilemap_plane = QComboBox()
        self._k2ge_tilemap_plane.addItems(["scr1", "scr2"])
        self._k2ge_tilemap_plane.currentIndexChanged.connect(
            lambda _=None: self._refresh_k2ge_tilemaps(),
        )
        controls.addWidget(self._k2ge_tilemap_plane)
        controls.addWidget(QLabel("View:"))
        self._k2ge_tilemap_view = QComboBox()
        self._k2ge_tilemap_view.addItems(
            ["grid", "list non-empty", "list all"]
        )
        self._k2ge_tilemap_view.currentIndexChanged.connect(
            lambda _=None: self._refresh_k2ge_tilemaps(),
        )
        controls.addWidget(self._k2ge_tilemap_view)
        controls.addStretch()
        layout.addLayout(controls)

        self._k2ge_tilemaps_text = _make_readonly_text(K2GE_TEXT_ROWS)
        layout.addWidget(self._k2ge_tilemaps_text)
        return frame

    # ----- QSettings helpers (pass 52) -----

    def _last_dir(self, key: str) -> str:
        """Return the last-used directory for `key` (or "" if none)."""
        value = self._settings.value(f"last_dir/{key}", "", type=str)
        return value if isinstance(value, str) else ""

    def _remember_dir(self, key: str, file_path: str) -> None:
        """Persist the parent directory of `file_path` under `key`."""
        if not file_path:
            return
        parent = str(Path(file_path).parent)
        self._settings.setValue(f"last_dir/{key}", parent)

    def _restore_window_layout(self) -> None:
        """Restore main-window geometry/state from QSettings if present."""
        version = self._settings.value("window/layout_version", None, type=int)
        geometry = self._settings.value("window/geometry")
        state = self._settings.value("window/state")
        if version != WINDOW_LAYOUT_STATE_VERSION or geometry is None or state is None:
            self._layout_restored = False
            return
        geometry_ok = self.restoreGeometry(geometry)
        state_ok = self.restoreState(state, WINDOW_LAYOUT_STATE_VERSION)
        self._layout_restored = bool(geometry_ok and state_ok)
        if not self._layout_restored:
            self._clear_window_layout_settings()

    def _save_window_layout(self) -> None:
        """Persist main-window geometry/state to QSettings."""
        self._settings.setValue("window/layout_version", WINDOW_LAYOUT_STATE_VERSION)
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.setValue(
            "window/state", self.saveState(WINDOW_LAYOUT_STATE_VERSION),
        )
        self._settings.sync()

    def _clear_window_layout_settings(self) -> None:
        """Delete the persisted geometry/state keys."""
        self._settings.remove("window/layout_version")
        self._settings.remove("window/geometry")
        self._settings.remove("window/state")
        self._settings.sync()

    def _focus_widget_blocks_joypad(self) -> bool:
        """Avoid hijacking keys while the user is editing text fields."""
        focus = QApplication.focusWidget()
        return isinstance(focus, (QLineEdit, QPlainTextEdit, QListWidget, QComboBox))

    def _format_joypad_status(self) -> str:
        if self.session is None:
            return "pad=none"
        state = self.session.joypad_state()
        pressed = [
            label for bit, label in JOYPAD_BITS_IN_ORDER if state & bit
        ]
        if not pressed:
            return "pad=none"
        return "pad=" + "+".join(pressed)

    def eventFilter(self, obj, event):  # type: ignore[override]
        active_window = QApplication.activeWindow()
        if (
            self.session is None
            or active_window not in (None, self)
            or isinstance(obj, (QLineEdit, QPlainTextEdit, QListWidget, QComboBox))
            or self._focus_widget_blocks_joypad()
        ):
            return super().eventFilter(obj, event)
        if event.type() not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return super().eventFilter(obj, event)
        key = event.key()
        mask = JOYPAD_KEY_TO_MASK.get(key)
        if mask is None:
            return super().eventFilter(obj, event)
        if event.isAutoRepeat():
            return True
        changed = self.session.set_joypad_mask(
            mask,
            pressed=(event.type() == QEvent.Type.KeyPress),
        )
        if changed:
            self._refresh_memory()
            self._refresh_status()
        return True

    # ----- Slots -----

    def _on_open_rom(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open ROM", self._last_dir("rom"),
            "NGPC ROM (*.ngc *.ngp);;All files (*)",
        )
        if not path_str:
            return
        try:
            session = EmulatorSession(Path(path_str), bios_path=self._bios_path)
        except Exception as exc:
            QMessageBox.critical(self, "Open ROM failed", str(exc))
            return
        self._remember_dir("rom", path_str)
        self._set_session(session, Path(path_str).name)

    def _on_load_bios_image(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Load BIOS Image", self._last_dir("bios"),
            "BIOS image (*.bin *.bios *.bios.bin);;All files (*)",
        )
        if not path_str:
            return
        bios_path = Path(path_str)
        try:
            bios_bytes = bios_path.read_bytes()
            if len(bios_bytes) != 0x10000:
                raise ValueError(
                    f"BIOS image must be exactly 65536 bytes; got {len(bios_bytes)} from {bios_path}"
                )
        except Exception as exc:
            QMessageBox.critical(self, "Load BIOS failed", str(exc))
            return
        self._bios_path = bios_path
        if self.session is not None:
            self.session.set_bios_path(bios_path)
        self._remember_dir("bios", path_str)
        self.refresh_all()

    def _on_clear_bios_image(self) -> None:
        self._bios_path = None
        if self.session is not None:
            self.session.clear_bios_path()
        self.refresh_all()

    def _on_close_rom(self) -> None:
        if self.session is None:
            return
        self._set_session(None, None)

    def _on_load_savestate(self) -> None:
        if self.session is None:
            QMessageBox.information(
                self, "No ROM loaded",
                "Open a ROM first (File → Open ROM…).",
            )
            return
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Load Savestate", self._last_dir("savestate"),
            "Savestate JSON (*.json);;All files (*)",
        )
        if not path_str:
            return
        try:
            self.session.load_savestate(Path(path_str))
        except Exception as exc:
            QMessageBox.critical(self, "Load Savestate failed", str(exc))
            return
        self._remember_dir("savestate", path_str)
        self._current_savestate_path = Path(path_str)
        self.refresh_all()

    def _on_save_savestate(self) -> None:
        """Ctrl+S — save to last-used path, fall back to Save As if none."""
        if self.session is None:
            return
        if self._current_savestate_path is None:
            self._on_save_savestate_as()
            return
        try:
            self.session.save_savestate(
                self._current_savestate_path, note="ui-saved",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Save Savestate failed", str(exc))
            return
        self.statusBar().showMessage(
            f"Saved to {self._current_savestate_path}", 3000,
        )

    def _on_save_savestate_as(self) -> None:
        """Ctrl+Shift+S — always show the save dialog."""
        if self.session is None:
            return
        default_name = (
            str(self._current_savestate_path)
            if self._current_savestate_path
            else str(Path(self._last_dir("savestate")) / "save.state.json")
            if self._last_dir("savestate")
            else "save.state.json"
        )
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Save Savestate As", default_name,
            "Savestate JSON (*.json);;All files (*)",
        )
        if not path_str:
            return
        try:
            self.session.save_savestate(Path(path_str), note="ui-saved")
        except Exception as exc:
            QMessageBox.critical(self, "Save Savestate failed", str(exc))
            return
        self._remember_dir("savestate", path_str)
        self._current_savestate_path = Path(path_str)
        QMessageBox.information(
            self, "Saved", f"Savestate written to:\n{path_str}",
        )

    def _on_reset(self) -> None:
        if self.session is None:
            return
        if self._running:
            self._stop_run_loop()
        self.session.reset()
        self.refresh_all()

    def _on_step_frame(self) -> None:
        if self.session is None:
            return
        if self._running:
            self._stop_run_loop()
        try:
            self.session.step_until_frame_advance()
        except Exception as exc:
            QMessageBox.critical(self, "Step Frame failed", str(exc))
        self.refresh_all()

    def _on_run_toggle(self) -> None:
        if self.session is None:
            return
        if self._running:
            self._stop_run_loop()
        else:
            self._start_run_loop()

    def _on_memory_go(self) -> None:
        if self.session is None:
            return
        self._refresh_memory()

    def _on_memory_at_pc(self) -> None:
        if self.session is None:
            return
        self._mem_address_edit.setText(f"0x{self.session.cpu.pc:08X}")
        self._refresh_memory()

    def _on_memory_at_xsp(self) -> None:
        if self.session is None:
            return
        xsp = self.session.cpu.regs.xsp
        if xsp is None:
            QMessageBox.information(
                self, "XSP unknown", "Stack pointer is not modeled yet.",
            )
            return
        self._mem_address_edit.setText(f"0x{xsp:08X}")
        self._refresh_memory()

    def _on_disasm_go(self) -> None:
        if self.session is None:
            return
        address = self._parse_address_field(self._disasm_address_edit.text())
        if address is None:
            return
        self._disasm_view_address = address & 0xFFFFFF
        self._disasm_address_edit.setText(f"0x{self._disasm_view_address:08X}")
        self._refresh_disasm()
        self._refresh_status()

    def _on_disasm_at_pc(self) -> None:
        if self.session is None:
            return
        self._disasm_view_address = None
        self._disasm_address_edit.setText(f"0x{self.session.cpu.pc:08X}")
        self._refresh_disasm()
        self._refresh_status()

    # ----- Symbols (UI 0.4) -----

    def _on_load_symbol_map(self) -> None:
        if self.session is None:
            return
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Load Symbol Map", self._last_dir("symbol_map"),
            "Map files (*.map);;All files (*)",
        )
        if not path_str:
            return
        try:
            count = self.session.load_symbol_map(Path(path_str))
        except Exception as exc:
            QMessageBox.critical(
                self, "Load Symbol Map failed", str(exc),
            )
            return
        self._remember_dir("symbol_map", path_str)
        self.statusBar().showMessage(
            f"Loaded {count} symbols from {Path(path_str).name}", 4000,
        )
        self.refresh_all()

    def _on_load_breakpoints_registry(self) -> None:
        if self.session is None:
            return
        try:
            path, count = self.session.load_breakpoint_registry()
        except Exception as exc:
            QMessageBox.critical(self, "Load Breakpoints failed", str(exc))
            return
        self.statusBar().showMessage(
            f"Loaded {count} breakpoint(s) from {path.name}", 4000,
        )
        self.refresh_all()

    def _on_save_breakpoints_registry(self) -> None:
        if self.session is None:
            return
        try:
            path = self.session.save_breakpoint_registry()
        except Exception as exc:
            QMessageBox.critical(self, "Save Breakpoints failed", str(exc))
            return
        self.statusBar().showMessage(
            f"Saved {len(self.session.list_breakpoints())} breakpoint(s) to {path.name}",
            4000,
        )

    def _on_load_watchpoints_registry(self) -> None:
        if self.session is None:
            return
        try:
            path, count = self.session.load_watchpoint_registry()
        except Exception as exc:
            QMessageBox.critical(self, "Load Watchpoints failed", str(exc))
            return
        self.statusBar().showMessage(
            f"Loaded {count} watchpoint(s) from {path.name}", 4000,
        )
        self.refresh_all()

    def _on_save_watchpoints_registry(self) -> None:
        if self.session is None:
            return
        try:
            path = self.session.save_watchpoint_registry()
        except Exception as exc:
            QMessageBox.critical(self, "Save Watchpoints failed", str(exc))
            return
        self.statusBar().showMessage(
            f"Saved {len(self.session.list_watchpoints())} watchpoint(s) to {path.name}",
            4000,
        )

    # ----- Breakpoints (UI 0.4) -----

    def _parse_address_field(self, text: str) -> int | None:
        """Parse an address string ; returns None and shows a dialog on failure."""
        text = text.strip()
        if not text:
            return None
        # Allow symbol-name input when a symbol table is loaded.
        if (
            self.session is not None
            and self.session.symbol_table is not None
            and not text.startswith(("0x", "0X"))
            and not text[0].isdigit()
        ):
            sym = self.session.symbol_table.lookup_name(text)
            if sym is not None:
                return sym.address
            QMessageBox.warning(
                self, "Unknown symbol",
                f"No symbol named {text!r} in the loaded map.",
            )
            return None
        try:
            return int(text, 0)
        except ValueError:
            QMessageBox.warning(
                self, "Invalid address",
                f"Couldn't parse address {text!r}.",
            )
            return None

    def _on_add_breakpoint(self) -> None:
        if self.session is None:
            return
        address = self._parse_address_field(self._bp_address_edit.text())
        if address is None:
            return
        # Resolve a symbol-based label automatically when possible.
        label = self.session.resolve_symbol(address) or ""
        self.session.add_breakpoint(address, label)
        self._bp_address_edit.clear()
        self.refresh_all()

    def _on_add_breakpoint_at_pc(self) -> None:
        if self.session is None:
            return
        address = self.session.cpu.pc
        label = self.session.resolve_symbol(address) or ""
        self.session.add_breakpoint(address, label)
        self.refresh_all()

    def _on_breakpoint_double_clicked(self, item: QListWidgetItem) -> None:
        breakpoint_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(breakpoint_id, int) and self.session is not None:
            self.session.remove_breakpoint_id(breakpoint_id)
            self.refresh_all()

    def _on_remove_selected_breakpoint(self) -> None:
        if self.session is None:
            return
        item = self._bp_list.currentItem()
        if item is None:
            return
        breakpoint_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(breakpoint_id, int):
            self.session.remove_breakpoint_id(breakpoint_id)
            self.refresh_all()

    def _on_clear_breakpoints(self) -> None:
        if self.session is None or not self.session.list_breakpoints():
            return
        self.session.clear_breakpoints()
        self.refresh_all()

    # ----- Watchpoints (UI 0.6) -----

    def _on_add_watchpoint(self) -> None:
        if self.session is None:
            return
        address = self._parse_address_field(self._wp_address_edit.text())
        if address is None:
            return
        kind = self._wp_kind_combo.currentText()
        # Optional value filter.
        value_text = self._wp_value_edit.text().strip()
        value: int | None = None
        if value_text:
            try:
                value = int(value_text, 0) & 0xFF
            except ValueError:
                QMessageBox.warning(
                    self, "Invalid value",
                    f"Couldn't parse value {value_text!r}",
                )
                return
        label = self.session.resolve_symbol(address) or None
        try:
            self.session.add_watchpoint(
                address, kind=kind, value=value, label=label,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid watchpoint", str(exc))
            return
        self._wp_address_edit.clear()
        self._wp_value_edit.clear()
        self.refresh_all()

    def _on_watchpoint_double_clicked(self, item: QListWidgetItem) -> None:
        wp_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(wp_id, int) and self.session is not None:
            self.session.remove_watchpoint(wp_id)
            self.refresh_all()

    def _on_remove_selected_watchpoint(self) -> None:
        if self.session is None:
            return
        item = self._wp_list.currentItem()
        if item is None:
            return
        wp_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(wp_id, int):
            self.session.remove_watchpoint(wp_id)
            self.refresh_all()

    def _on_clear_watchpoints(self) -> None:
        if self.session is None or not self.session.list_watchpoints():
            return
        self.session.clear_watchpoints()
        self.refresh_all()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._stop_run_loop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._save_window_layout()
        super().closeEvent(event)

    # ----- Run loop -----

    def _start_run_loop(self) -> None:
        if self._running:
            return
        self._running = True
        self._run_button.setText("Pause")
        self._timer.start()

    def _stop_run_loop(self) -> None:
        self._running = False
        self._timer.stop()
        try:
            self._run_button.setText("Run")
        except RuntimeError:
            pass

    def _run_tick(self) -> None:
        if not self._running or self.session is None:
            return
        try:
            self.session.step(RUN_STEPS_PER_TICK)
        except Exception as exc:
            self._stop_run_loop()
            QMessageBox.critical(self, "Run failed", str(exc))
            self.refresh_all()
            return
        self.refresh_all()
        if self.session.last_stop_reason != "count-reached":
            self._stop_run_loop()

    def _step_n(self, n: int) -> None:
        if self.session is None:
            return
        try:
            self.session.step(n)
        except Exception as exc:
            QMessageBox.critical(self, "Step failed", str(exc))
        self.refresh_all()

    # ----- Refresh -----

    def refresh_all(self) -> None:
        self._refresh_lcd()
        self._refresh_registers()
        self._refresh_disasm()
        self._refresh_memory()
        self._refresh_breakpoints()
        self._refresh_watchpoints()
        self._refresh_k2ge_video()
        self._refresh_k2ge_palettes()
        self._refresh_k2ge_oam()
        self._refresh_k2ge_tilemaps()
        self._refresh_status()

    def _refresh_lcd(self) -> None:
        if self.session is None:
            self._lcd_label.clear()
            self._lcd_label.setText("(no ROM loaded)")
            self._lcd_label.setStyleSheet(
                "background-color: black; color: #888;"
            )
            self._lcd_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            return
        self._lcd_label.setStyleSheet("background-color: black;")
        ppm = self.session.render_lcd_ppm()
        image = QImage.fromData(ppm, "PPM")
        if image.isNull():
            return
        scaled = image.scaled(
            LCD_WIDTH * LCD_SCALE, LCD_HEIGHT * LCD_SCALE,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,  # nearest neighbour
        )
        self._lcd_label.setPixmap(QPixmap.fromImage(scaled))

    def _refresh_registers(self) -> None:
        if self.session is None:
            for label in self._reg_labels.values():
                label.setText("—")
            return
        cpu = self.session.cpu

        def hexv(value, width):
            return "—" if value is None else f"0x{value:0{width}X}"

        self._reg_labels["PC"].setText(hexv(cpu.pc, 8))
        self._reg_labels["XSP"].setText(hexv(cpu.regs.xsp, 8))
        self._reg_labels["XWA"].setText(hexv(cpu.regs.xwa, 8))
        self._reg_labels["XBC"].setText(hexv(cpu.regs.xbc, 8))
        self._reg_labels["XDE"].setText(hexv(cpu.regs.xde, 8))
        self._reg_labels["XHL"].setText(hexv(cpu.regs.xhl, 8))
        self._reg_labels["XIX"].setText(hexv(cpu.regs.xix, 8))
        self._reg_labels["XIY"].setText(hexv(cpu.regs.xiy, 8))
        self._reg_labels["XIZ"].setText(hexv(cpu.regs.xiz, 8))
        self._reg_labels["iff_level"].setText(
            "—" if cpu.iff_level is None else str(cpu.iff_level),
        )
        self._reg_labels["rfp"].setText(
            "—" if cpu.rfp is None else str(cpu.rfp),
        )
        f = cpu.flags
        flag_letters = []
        for letter, value in (
            ("S", f.sf), ("Z", f.zf), ("V", f.vf),
            ("H", f.hf), ("C", f.cf), ("N", f.nf),
        ):
            if value is None:
                flag_letters.append("·")
            else:
                flag_letters.append(letter.upper() if value else letter.lower())
        self._reg_labels["flags"].setText("".join(flag_letters))

    def _refresh_disasm(self) -> None:
        if self.session is None:
            self._disasm_address_edit.setText("")
            self._disasm_text.setPlainText("(no ROM loaded)")
            return
        view_address = (
            self.session.cpu.pc
            if self._disasm_view_address is None
            else self._disasm_view_address
        )
        instructions = self.session.disassemble_from(
            view_address,
            count=DISASM_LINES,
        )
        if not self._disasm_address_edit.hasFocus():
            self._disasm_address_edit.setText(f"0x{view_address:08X}")
        current_pc = self.session.cpu.pc
        lines: list[str] = []
        pc_line_index: int | None = None
        for idx, (pc, decoded) in enumerate(instructions):
            raw = decoded.raw_bytes or b""
            raw_hex = " ".join(f"{b:02X}" for b in raw).ljust(15)
            # 1-char BP gutter glyph (filled circle for BPs, space otherwise).
            bp_marker = (
                BREAKPOINT_GLYPH if self.session.has_breakpoint(pc) else " "
            )
            if decoded.status == "decoded":
                mnem = decoded.mnemonic or "?"
                operands = decoded.operands or ""
                core = f"{mnem} {operands}".rstrip()
            else:
                core = f"<{decoded.status}>"
            # Append symbol annotation when an exact match exists.
            symbol = self.session.resolve_symbol(pc)
            suffix = ""
            if symbol is not None and "+" not in symbol:
                # Exact-address symbol — show it ; offsets are too noisy.
                suffix = f"    ; {symbol}"
            lines.append(f"{bp_marker} 0x{pc:08X}  {raw_hex}  {core}{suffix}")
            if pc == current_pc and pc_line_index is None:
                pc_line_index = idx
        self._disasm_text.setPlainText("\n".join(lines))
        # Highlight the PC line via the document's character format.
        if pc_line_index is not None:
            block = self._disasm_text.document().findBlockByNumber(pc_line_index)
            if block.isValid():
                cursor = self._disasm_text.textCursor()
                cursor.setPosition(block.position())
                cursor.movePosition(
                    cursor.MoveOperation.EndOfBlock,
                    cursor.MoveMode.KeepAnchor,
                )
                fmt = QTextCharFormat()
                fmt.setBackground(Qt.GlobalColor.yellow)
                fmt.setFontWeight(QFont.Weight.Bold)
                cursor.setCharFormat(fmt)

    def _refresh_memory(self) -> None:
        if self.session is None:
            self._mem_text.setPlainText("(no ROM loaded)")
            return
        addr_str = self._mem_address_edit.text().strip()
        try:
            base_address = int(addr_str, 0)
        except ValueError:
            self._mem_text.setPlainText(f"<invalid address: {addr_str!r}>")
            return
        row_base = base_address & ~(MEMORY_BYTES_PER_ROW - 1)
        total = MEMORY_ROWS * MEMORY_BYTES_PER_ROW
        values = self.session.read_memory_range(row_base, total)
        lines: list[str] = []
        for row in range(MEMORY_ROWS):
            row_addr = row_base + row * MEMORY_BYTES_PER_ROW
            row_values = values[
                row * MEMORY_BYTES_PER_ROW : (row + 1) * MEMORY_BYTES_PER_ROW
            ]
            hex_chunks: list[str] = []
            ascii_chunks: list[str] = []
            for v in row_values:
                if v is None:
                    hex_chunks.append("??")
                    ascii_chunks.append(".")
                else:
                    hex_chunks.append(f"{v:02X}")
                    ascii_chunks.append(chr(v) if 0x20 <= v < 0x7F else ".")
            hex_left = " ".join(hex_chunks[:8])
            hex_right = " ".join(hex_chunks[8:])
            ascii_str = "".join(ascii_chunks)
            lines.append(f"0x{row_addr:06X}  {hex_left}  {hex_right}  {ascii_str}")
        self._mem_text.setPlainText("\n".join(lines))

    def _refresh_breakpoints(self) -> None:
        self._bp_list.clear()
        if self.session is None:
            return
        for bp in self.session.list_breakpoints():
            text = f"#{bp.id}  0x{bp.address:08X}"
            if bp.label:
                text += f"  {bp.label}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, bp.id)
            self._bp_list.addItem(item)

    def _refresh_watchpoints(self) -> None:
        self._wp_list.clear()
        if self.session is None:
            return
        for wp in self.session.list_watchpoints():
            kind_letter = {"write": "W", "read": "R", "access": "A"}[wp.kind]
            text = f"{kind_letter} 0x{wp.start:08X}"
            if wp.size > 1:
                text += f"+{wp.size}"
            if wp.value is not None:
                text += f" =0x{wp.value:02X}"
            if wp.label:
                text += f"  {wp.label}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, wp.id)
            self._wp_list.addItem(item)

    def _format_k2ge_palette_row(self, palette) -> str:
        colors = "  ".join(
            f"{color.hex_rgb24()} ({color.r:X},{color.g:X},{color.b:X})"
            for color in palette.colors
        )
        return (
            f"0x{palette.base_address:06X}  "
            f"{palette.plane:<10} #{palette.index:<2}  {colors}"
        )

    def _format_k2ge_oam_row(self, sprite) -> str:
        flags = "".join(
            [
                "H" if sprite.h_flip else "-",
                "V" if sprite.v_flip else "-",
                "P" if sprite.p_c else "-",
                "h" if sprite.h_chain else "-",
                "v" if sprite.v_chain else "-",
            ]
        )
        return (
            f"#{sprite.index:<2} 0x{sprite.base_address:06X}  "
            f"tile={sprite.c_c:<3}  pos=({sprite.h_pos:>3},{sprite.v_pos:>3})  "
            f"pr={sprite.pr_c} ({sprite.pr_c_label:<10})  cp={sprite.cp_c:<2}  "
            f"flags={flags}"
        )

    def _tilemap_grid_row(self, entries, y: int) -> str:
        row_start = y * TILEMAP_TILES_PER_ROW
        cells: list[str] = []
        for x in range(TILEMAP_TILES_PER_ROW):
            entry = entries[row_start + x]
            if entry.is_empty():
                cells.append(".")
            elif entry.c_c < 10:
                cells.append(str(entry.c_c))
            elif entry.c_c < 36:
                cells.append(chr(ord("a") + entry.c_c - 10))
            elif entry.c_c < 62:
                cells.append(chr(ord("A") + entry.c_c - 36))
            else:
                cells.append("+")
        return "".join(cells)

    def _refresh_k2ge_video(self) -> None:
        if self.session is None:
            self._k2ge_video_text.setPlainText("(no ROM loaded)")
            return
        ctrl = self.session.read_k2ge_control_registers()
        lines = [
            f"Mode: {'K1GE compat' if ctrl.k1ge_compat else 'K2GE color'}",
            f"BGC: raw=0x{ctrl.bgc_raw:02X}  enabled={'yes' if ctrl.bgc_enabled else 'no'}  index={ctrl.bgc_index}",
            f"2D: NEG={'on' if ctrl.neg else 'off'}  OOWC={ctrl.oowc}",
            f"Window: origin=({ctrl.wba_h:>3},{ctrl.wba_v:>3})  size=({ctrl.wsi_h:>3},{ctrl.wsi_v:>3})",
            f"Sprite offset: ({ctrl.po_h:>3},{ctrl.po_v:>3})",
            f"Scroll priority: {'SCR2 front' if ctrl.scr2_in_front else 'SCR1 front'}",
            f"SCR1 scroll: ({ctrl.s1so_h:>3},{ctrl.s1so_v:>3})",
            f"SCR2 scroll: ({ctrl.s2so_h:>3},{ctrl.s2so_v:>3})",
        ]
        self._k2ge_video_text.setPlainText("\n".join(lines))

    def _refresh_k2ge_palettes(self) -> None:
        if self.session is None:
            self._k2ge_palettes_text.setPlainText("(no ROM loaded)")
            return
        selected_kind = self._k2ge_palette_kind.currentText()
        all_palettes = self.session.read_k2ge_palettes()
        planes = (
            ("sprite", "scr1", "scr2", "background", "window")
            if selected_kind == "all"
            else (selected_kind,)
        )
        lines: list[str] = []
        for plane in planes:
            lines.append(f"-- {plane} --")
            for palette in all_palettes[plane]:
                lines.append(self._format_k2ge_palette_row(palette))
            lines.append("")
        self._k2ge_palettes_text.setPlainText("\n".join(lines).rstrip())

    def _refresh_k2ge_oam(self) -> None:
        if self.session is None:
            self._k2ge_oam_text.setPlainText("(no ROM loaded)")
            return
        visible_only = self._k2ge_oam_filter.currentText() == "visible only"
        sprites = self.session.read_k2ge_oam_sprites(visible_only=visible_only)
        lines = [
            "flags: H=h_flip V=v_flip P=p_c h=h_chain v=v_chain",
            f"shown={len(sprites)}  filter={'visible-only' if visible_only else 'all'}",
            "",
        ]
        if not sprites:
            lines.append("(no sprites)")
        else:
            for sprite in sprites:
                lines.append(self._format_k2ge_oam_row(sprite))
        self._k2ge_oam_text.setPlainText("\n".join(lines))

    def _refresh_k2ge_tilemaps(self) -> None:
        if self.session is None:
            self._k2ge_tilemaps_text.setPlainText("(no ROM loaded)")
            return
        plane = self._k2ge_tilemap_plane.currentText()
        view_mode = self._k2ge_tilemap_view.currentText()
        if view_mode == "grid":
            entries = self.session.read_k2ge_tilemap(plane)
            lines = [
                "Grid (`.` empty; `0..9 a..z A..Z +` compress tile ids):"
            ]
            for y in range(TILEMAP_TILES_PER_COL):
                lines.append(f"{y:>2}: {self._tilemap_grid_row(entries, y)}")
        else:
            non_empty = view_mode == "list non-empty"
            entries = self.session.read_k2ge_tilemap(plane, non_empty=non_empty)
            lines = [f"plane={plane}  view={view_mode}", ""]
            if not entries:
                lines.append("(no matching tiles)")
            else:
                for entry in entries:
                    flags = "".join(
                        [
                            "H" if entry.h_flip else "-",
                            "V" if entry.v_flip else "-",
                            "P" if entry.p_c else "-",
                        ]
                    )
                    lines.append(
                        f"({entry.x:>2},{entry.y:>2})  0x{entry.base_address:06X}  "
                        f"tile={entry.c_c:<3}  cp={entry.cp_c:<2}  flags={flags}"
                    )
        self._k2ge_tilemaps_text.setPlainText("\n".join(lines))

    def _refresh_status(self) -> None:
        if self.session is None:
            message = "No ROM loaded — File → Open ROM… (Ctrl+O)"
            if self._bios_path is not None:
                message += f" | bios={self._bios_path.name}"
            self.statusBar().showMessage(message)
            return
        snap = self.session.snapshot()
        # If a symbol map is loaded, surface the PC's symbol name.
        pc_symbol = self.session.resolve_symbol(snap.cpu.pc)
        parts = [
            f"PC=0x{snap.cpu.pc:08X}"
            + (f" ({pc_symbol})" if pc_symbol else ""),
            "disasm=@PC"
            if self._disasm_view_address is None
            else f"disasm=0x{self._disasm_view_address:08X}",
            f"frame={snap.frame_state.frame_count}",
            f"scanline={snap.frame_state.scanline:>3d}",
            "VBLANK" if snap.frame_state.in_vblank else "visible",
            f"IRQ=0x{snap.irq_state.pending_mask:02X}",
            f"cycles={snap.total_cycles_consumed}",
            (
                f"bios={self.session.bios_path.name}"
                if self.session.bios_path is not None
                else "bios=none"
            ),
            self._format_joypad_status(),
        ]
        if snap.last_stop_reason:
            parts.append(f"last: {snap.last_stop_reason}")
            if snap.last_executed_count:
                parts.append(f"exec={snap.last_executed_count}")
            if snap.last_irq_deliveries:
                parts.append(f"irq_deliv={snap.last_irq_deliveries}")
        # UI 0.6 : surface the most recent watch hit details.
        watch_hit = self.session.last_watch_hit
        if watch_hit is not None:
            wp, access_kind, address, data = watch_hit
            value_hex = " ".join(f"{b:02X}" for b in data)
            parts.append(
                f"watch: {access_kind} 0x{address:08X}=[{value_hex}]"
            )
        self.statusBar().showMessage(" | ".join(parts))


def launch_ui(
    rom_path: Path | None = None,
    *,
    bios_path: Path | None = None,
) -> int:
    """Entry-point invoked by `ngpc_emu.py ui [rom]`.

    `rom_path=None` opens the UI with no active session ; the user
    picks a ROM via File → Open ROM…
    """
    app = QApplication.instance() or QApplication(sys.argv)
    window = EmulatorWindow(rom_path, bios_path=bios_path)
    window.show()
    # Position the floating inspector windows around the now-shown
    # main window so they don't all stack on top of each other.
    if not window._layout_restored:
        window._arrange_floating_docks()
    return app.exec()


if __name__ == "__main__":
    rom_arg = Path(sys.argv[1]) if len(sys.argv) >= 2 else None
    raise SystemExit(launch_ui(rom_arg))

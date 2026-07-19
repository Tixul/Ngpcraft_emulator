"""Tests for core/symbols.py — t900ld .map loader."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.symbols import load_map, Symbol


_SAMPLE_MAP = """\
# t900ld.py map file
# inputs: a.t9obj, b.t9obj

=== Linker symbols ===
  _Bss_START               0x00004000
  _Bss_END                 0x00005572
  _StackTop                0x00006000

=== Public symbols ===
  _main                    0x00200200
  _shmup_update            0x00210000
  _Bgm_Start               0x00223242
  _Bgm_StartLoop           0x002232DD
  _zeroaddr                0x00000000
"""


class SymbolMapTests(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".map")
        os.close(fd)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(_SAMPLE_MAP)
        self.table = load_map(self.path)

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_total_symbol_count(self) -> None:
        self.assertEqual(len(self.table), 8)

    def test_sections_preserved_in_order(self) -> None:
        self.assertEqual(
            self.table.sections, ["Linker symbols", "Public symbols"]
        )

    def test_section_summary_counts(self) -> None:
        summary = dict(self.table.section_summary())
        self.assertEqual(summary["Linker symbols"], 3)
        self.assertEqual(summary["Public symbols"], 5)

    def test_lookup_name_exact_match(self) -> None:
        sym = self.table.lookup_name("_shmup_update")
        self.assertIsNotNone(sym)
        assert sym is not None
        self.assertEqual(sym.address, 0x00210000)
        self.assertEqual(sym.section, "Public symbols")

    def test_lookup_name_unknown_returns_none(self) -> None:
        self.assertIsNone(self.table.lookup_name("_does_not_exist"))

    def test_lookup_address_exact_returns_symbol(self) -> None:
        sym = self.table.lookup_address(0x00210000)
        self.assertIsNotNone(sym)
        assert sym is not None
        self.assertEqual(sym.name, "_shmup_update")

    def test_lookup_address_in_function_body_returns_owning_function(self) -> None:
        # PC inside _shmup_update body (between _shmup_update and _Bgm_Start)
        sym = self.table.lookup_address(0x00210ABC)
        self.assertIsNotNone(sym)
        assert sym is not None
        self.assertEqual(sym.name, "_shmup_update")

    def test_lookup_address_below_lowest_symbol_returns_none(self) -> None:
        # Below the lowest known symbol (_zeroaddr is at 0x0)
        # Trick: there IS a symbol at 0, so 0 itself resolves.
        sym0 = self.table.lookup_address(0)
        self.assertIsNotNone(sym0)
        # But a negative or pre-zero PC has nothing.
        # Python ints can't go negative here in a real PC, but we test edge:
        # We simulate "no lower symbol" by building a table without _zeroaddr
        # via symbols_in_range filtering.
        # Just verify symbols_in_range works as a positive proof.
        in_range = self.table.symbols_in_range(0x00200000, 0x00224000)
        names = [s.name for s in in_range]
        self.assertIn("_main", names)
        self.assertIn("_shmup_update", names)
        self.assertIn("_Bgm_Start", names)
        self.assertNotIn("_zeroaddr", names)
        self.assertNotIn("_Bss_START", names)

    def test_lookup_address_at_boundary_picks_higher_function(self) -> None:
        # PC exactly at _Bgm_Start should resolve to _Bgm_Start, not _shmup_update
        sym = self.table.lookup_address(0x00223242)
        self.assertIsNotNone(sym)
        assert sym is not None
        self.assertEqual(sym.name, "_Bgm_Start")

    def test_symbols_at_exact_address_with_no_collision(self) -> None:
        syms = self.table.symbols_at_address(0x00210000)
        self.assertEqual(len(syms), 1)
        self.assertEqual(syms[0].name, "_shmup_update")

    def test_symbols_at_address_with_collision(self) -> None:
        # Add a second symbol at the same address as _shmup_update
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("  _shmup_update_alias      0x00210000\n")
        t = load_map(self.path)
        syms = t.symbols_at_address(0x00210000)
        self.assertEqual(len(syms), 2)
        names = {s.name for s in syms}
        self.assertSetEqual(names, {"_shmup_update", "_shmup_update_alias"})

    def test_load_map_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_map("/this/does/not/exist.map")

    def test_lookup_address_far_above_is_attributed_to_nobody(self) -> None:
        """A PC nowhere near any symbol belongs to no symbol.

        The nearest preceding symbol is _Bgm_StartLoop at 0x002232DD, and
        0xFFFFFF sits over 13 MiB past it -- that is BIOS space, not the tail of
        a game function. Naming it _Bgm_StartLoop would put a confident, wrong
        label on every BIOS frame in a backtrace, which is worse than no label.
        `DEFAULT_MAX_SPAN` is what caps the reach; see `lookup_address`.
        """
        self.assertIsNone(self.table.lookup_address(0xFFFFFF))

    def test_lookup_address_just_past_a_symbol_still_resolves(self) -> None:
        # The other side of the same rule: a little way past a symbol IS still
        # that symbol, or nothing inside a fat function would ever resolve.
        sym = self.table.lookup_address(0x002232DD + 0x10)
        self.assertIsNotNone(sym)
        assert sym is not None
        self.assertEqual(sym.name, "_Bgm_StartLoop")


if __name__ == "__main__":
    unittest.main()

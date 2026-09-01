"""The __import__ hook: what it books, and how it refuses to hurt the process."""

from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path

import pytest

from core.import_cost import (
    DEFAULT_MAX_OVERHEAD_MS,
    ImportCostLedger,
    PackageCost,
    PluginImportCost,
    RssReader,
    _Frame,
    get_ledger,
    reset_ledger,
)


class FakeRss:
    """Monotonic fake reader: every read advances by ``step`` bytes."""

    def __init__(self, step: int = 1 << 20, start: int = 100 << 20) -> None:
        self.source = "fake"
        self.step = step
        self.value = start
        self.reads = 0
        self.closed = False

    @property
    def available(self) -> bool:
        return True

    def read(self) -> int:
        self.reads += 1
        current = self.value
        self.value += self.step
        return current

    def close(self) -> None:
        self.closed = True


class ScriptedRss(FakeRss):
    """Returns a fixed sequence, so booked deltas are exact."""

    def __init__(self, values: list[int]) -> None:
        super().__init__()
        self.values = list(values)

    def read(self) -> int:
        self.reads += 1
        if not self.values:
            return 0
        return self.values.pop(0)


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset_ledger()
    original = builtins.__import__
    yield
    reset_ledger()
    # A leaked hook would poison every later test in the session.
    assert builtins.__import__ is original


@pytest.fixture
def ledger():
    probe = ImportCostLedger()
    probe.rss = FakeRss()
    try:
        yield probe
    finally:
        if probe.installed:
            probe.uninstall()


# ----------------------------------------------------------------------
# RssReader
# ----------------------------------------------------------------------
def test_rss_reader_reports_its_source():
    reader = RssReader()
    try:
        assert reader.source in {"proc", "psutil", "unavailable"}
        if reader.available:
            # Any live process has a resident set.
            assert reader.read() > 0
        else:
            assert reader.read() == 0
    finally:
        reader.close()


def test_rss_reader_read_after_close_is_zero_not_an_exception():
    reader = RssReader()
    reader.close()
    if reader.source == "proc":
        assert reader.read() == 0


# ----------------------------------------------------------------------
# lifecycle
# ----------------------------------------------------------------------
def test_install_replaces_and_uninstall_restores_import(ledger):
    original = builtins.__import__

    assert ledger.install() is True
    assert builtins.__import__ is not original
    assert ledger.installed is True
    assert ledger.rss_at_install > 0

    assert ledger.uninstall() is True
    assert builtins.__import__ is original
    assert ledger.installed is False
    assert ledger.rss.closed is True


def test_install_is_idempotent(ledger):
    ledger.install()
    hook = builtins.__import__

    assert ledger.install() is True
    assert builtins.__import__ is hook


def test_uninstall_without_install_is_a_no_op(ledger):
    assert ledger.uninstall() is False


def test_install_refuses_without_an_rss_reader(ledger):
    class NoRss(FakeRss):
        @property
        def available(self) -> bool:
            return False

    ledger.rss = NoRss()
    original = builtins.__import__

    assert ledger.install() is False
    assert ledger.installed is False
    assert ledger.degraded is True
    assert ledger.degraded_reason == "no_rss_reader"
    assert builtins.__import__ is original


def test_uninstall_leaves_a_foreign_wrapper_alone(ledger):
    ledger.install()
    hook = builtins.__import__
    calls: list[str] = []

    def outer(name, *args, **kwargs):
        calls.append(name)
        return hook(name, *args, **kwargs)

    builtins.__import__ = outer
    try:
        # Unwrapping here would silently delete the other plugin's hook.
        assert ledger.uninstall() is False
        assert builtins.__import__ is outer
        assert ledger.degraded_reason == "hook_chain_changed"
    finally:
        builtins.__import__ = ledger._original


def test_singleton_is_shared_and_updates_its_budget():
    first = get_ledger(1234.0)
    second = get_ledger(99.0)

    assert first is second
    assert second.max_overhead_ms == 99.0


def test_reset_ledger_uninstalls_a_live_hook():
    probe = get_ledger()
    probe.rss = FakeRss()
    original = builtins.__import__
    probe.install()

    reset_ledger()

    assert builtins.__import__ is original
    assert get_ledger() is not probe


# ----------------------------------------------------------------------
# classification
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("data.plugins.alpha.main", ("plugin", "alpha")),
        ("data.plugins.alpha", ("plugin", "alpha")),
        (
            "astrbot.builtin_stars.astrbot_reserved.main",
            ("plugin", "astrbot_reserved"),
        ),
        # Legacy prefix, kept for AstrBot builds that imported reserved plugins
        # as ``packages.<dir>``.
        ("packages.astrbot_reserved.main", ("plugin", "astrbot_reserved")),
        ("some_unimported_top.sub", ("package", "some_unimported_top")),
    ],
)
def test_classify_recognises_plugins_and_packages(ledger, name, expected):
    assert ledger._classify(name) == expected


def test_classify_skips_modules_already_imported(ledger):
    assert ledger._classify("sys") == (None, "")
    assert ledger._classify("") == (None, "")


def test_classify_skips_submodules_of_an_open_plugin_frame(ledger):
    ledger._active_plugins.add("alpha")

    assert ledger._classify("data.plugins.alpha.helpers") == (None, "")


def test_classify_skips_a_package_measured_once_before(ledger):
    ledger._seen.add("already_done")

    assert ledger._classify("already_done") == (None, "")


def test_classify_skips_nested_absolute_import_of_active_package(ledger):
    # ``pkg/__init__.py`` can execute ``import pkg.submodule`` before the
    # outer import has returned.  Both requests belong to one package frame.
    ledger._active.add("pkg")

    assert ledger._classify("pkg.submodule") == (None, "")


# ----------------------------------------------------------------------
# booking
# ----------------------------------------------------------------------
def _import_via_builtins(name: str) -> None:
    """Import like AstrBot does.

    ``importlib.import_module`` goes straight to ``importlib._bootstrap`` and
    never touches ``builtins.__import__``, so it would walk right past the
    hook.  AstrBot's star loader really calls ``__import__(path, fromlist=...)``.
    """

    builtins.__import__(name, fromlist=[name.rsplit(".", 1)[-1]])


def _frame(kind: str, key: str, rss0: int) -> _Frame:
    return _Frame(kind=kind, key=key, rss0=rss0, t0=0, mods0=0)


def test_book_records_a_plugin_frame(ledger):
    ledger.rss = ScriptedRss([5_000_000])
    frame = _frame("plugin", "alpha", 1_000_000)

    ledger._book(frame, t1=2_000_000, stack=[])

    cost = ledger.plugin_cost("alpha")
    assert isinstance(cost, PluginImportCost)
    assert cost.bytes == 4_000_000
    assert cost.self_bytes == 4_000_000
    assert cost.wall_ms == 2.0
    assert ledger.measured("alpha") is True
    assert ledger.measured("beta") is False


def test_book_subtracts_nested_children_from_self_bytes(ledger):
    ledger.rss = ScriptedRss([10_000_000])
    frame = _frame("plugin", "alpha", 1_000_000)
    frame.child_bytes = 7_000_000

    ledger._book(frame, t1=0, stack=[])

    cost = ledger.plugin_cost("alpha")
    assert cost.bytes == 9_000_000
    assert cost.self_bytes == 2_000_000


def test_book_propagates_the_gross_cost_to_the_parent_frame(ledger):
    ledger.rss = ScriptedRss([3_000_000])
    parent = _frame("plugin", "alpha", 0)
    child = _frame("package", "heavy", 1_000_000)

    ledger._book(child, t1=0, stack=[parent])

    assert parent.child_bytes == 2_000_000


def test_book_credits_a_package_to_the_plugin_that_opened_the_frame(ledger):
    ledger.rss = ScriptedRss([4_000_000, 9_000_000])
    parent = _frame("plugin", "alpha", 0)
    ledger.plugins["alpha"] = PluginImportCost(name="alpha")

    ledger._book(_frame("package", "heavy", 1_000_000), t1=0, stack=[parent])
    ledger._book(_frame("package", "heavy", 8_000_000), t1=0, stack=[parent])

    package = ledger.packages["heavy"]
    assert isinstance(package, PackageCost)
    assert package.first_importer == "alpha"
    assert package.imports == 2
    assert package.bytes == 4_000_000
    # Booked once on the plugin, no matter how often it is imported again.
    assert ledger.plugins["alpha"].packages == ["heavy"]


def test_book_never_goes_negative_when_rss_shrinks(ledger):
    ledger.rss = ScriptedRss([500_000])

    ledger._book(_frame("package", "heavy", 9_000_000), t1=0, stack=[])

    assert ledger.packages["heavy"].bytes == 0


def test_cost_table_and_sorted_packages(ledger):
    ledger.packages = {
        "small": PackageCost(name="small", bytes=10),
        "big": PackageCost(name="big", bytes=1000),
    }

    assert ledger.cost_table() == {"small": 10, "big": 1000}
    assert [item.name for item in ledger.packages_sorted()] == ["big", "small"]


# ----------------------------------------------------------------------
# self-policing
# ----------------------------------------------------------------------
def test_overhead_budget_degrades_the_ledger(ledger):
    ledger.max_overhead_ms = 1.0
    ledger._overhead_ns = 5_000_000

    ledger._check_budget()

    assert ledger.degraded is True
    assert ledger.degraded_reason == "overhead_budget_exceeded"


def test_overhead_budget_detaches_the_wrapper_immediately(ledger):
    original = builtins.__import__
    ledger.max_overhead_ms = 1.0
    ledger.install()
    ledger._overhead_ns = 5_000_000

    ledger._check_budget()

    assert ledger.degraded_reason == "overhead_budget_exceeded"
    assert ledger.installed is False
    assert builtins.__import__ is original


def test_degraded_ledger_cannot_be_reinstalled(ledger):
    original = builtins.__import__
    ledger.degraded_reason = "previous_failure"

    assert ledger.install() is False
    assert ledger.installed is False
    assert builtins.__import__ is original


def test_zero_budget_means_unlimited(ledger):
    ledger.max_overhead_ms = 0.0
    ledger._overhead_ns = 10_000_000_000

    ledger._check_budget()

    assert ledger.degraded is False


def test_a_degraded_hook_becomes_a_pure_passthrough(ledger):
    ledger.install()
    ledger.degraded_reason = "overhead_budget_exceeded"
    before = ledger._passthrough_calls

    _import_via_builtins("json")

    assert ledger._passthrough_calls > before
    assert ledger.plugins == {}


def test_overhead_ms_estimates_passthrough_calls(ledger):
    ledger._passthrough_calls = 1000

    # 1000 x 1500 ns = 1.5 ms
    assert ledger.overhead_ms == pytest.approx(1.5)


def test_default_budget_is_generous_enough_for_a_full_startup(ledger):
    assert ledger.max_overhead_ms == DEFAULT_MAX_OVERHEAD_MS


# ----------------------------------------------------------------------
# snapshot
# ----------------------------------------------------------------------
def test_snapshot_reports_totals_and_truncation(ledger):
    ledger.rss = ScriptedRss([300 << 20])
    ledger.rss_at_install = 100 << 20
    ledger.plugins["alpha"] = PluginImportCost(name="alpha", bytes=30, self_bytes=20)
    ledger.packages = {
        f"pkg{index}": PackageCost(name=f"pkg{index}", bytes=index, self_bytes=index)
        for index in range(5)
    }

    snapshot = ledger.snapshot(package_limit=2)

    assert snapshot["installed"] is False
    assert snapshot["plugin_count"] == 1
    assert snapshot["plugin_bytes"] == 20
    assert snapshot["package_count"] == 5
    assert snapshot["package_bytes"] == 0 + 1 + 2 + 3 + 4
    assert len(snapshot["packages"]) == 2
    assert snapshot["packages_truncated"] == 3
    assert snapshot["rss_growth_bytes"] == 200 << 20
    assert snapshot["rss_source"] == "fake"


def test_snapshot_without_a_limit_returns_every_package(ledger):
    ledger.packages = {
        f"pkg{index}": PackageCost(name=f"pkg{index}") for index in range(5)
    }

    assert len(ledger.snapshot(package_limit=0)["packages"]) == 5


# ----------------------------------------------------------------------
# end to end, through a real import
# ----------------------------------------------------------------------
def _write_fake_plugin_tree(root: Path) -> None:
    (root / "data" / "plugins" / "alpha").mkdir(parents=True)
    (root / "data" / "__init__.py").write_text("", encoding="utf-8")
    (root / "data" / "plugins" / "__init__.py").write_text("", encoding="utf-8")
    (root / "data" / "plugins" / "alpha" / "__init__.py").write_text(
        "", encoding="utf-8",
    )
    (root / "data" / "plugins" / "alpha" / "main.py").write_text(
        "import mscope_fake_dep\n\nVALUE = mscope_fake_dep.PAYLOAD\n",
        encoding="utf-8",
    )
    (root / "mscope_fake_dep").mkdir()
    (root / "mscope_fake_dep" / "__init__.py").write_text(
        "PAYLOAD = [0] * 1024\n", encoding="utf-8",
    )


@pytest.fixture
def fake_plugin_root(tmp_path):
    _write_fake_plugin_tree(tmp_path)
    sys.path.insert(0, str(tmp_path))
    importlib.invalidate_caches()
    try:
        yield tmp_path
    finally:
        sys.path.remove(str(tmp_path))
        for name in list(sys.modules):
            if name == "data" or name.startswith(("data.", "mscope_fake_dep")):
                del sys.modules[name]


def test_a_real_import_is_attributed_to_the_plugin(ledger, fake_plugin_root):
    ledger.install()
    try:
        _import_via_builtins("data.plugins.alpha.main")
    finally:
        ledger.uninstall()

    assert ledger.degraded is False, ledger.degraded_reason
    cost = ledger.plugin_cost("alpha")
    assert cost is not None
    assert cost.modules > 0
    assert cost.measured_at > 0
    # The dependency is booked to the plugin that first pulled it in.
    assert "mscope_fake_dep" in cost.packages
    assert ledger.packages["mscope_fake_dep"].first_importer == "alpha"
    # Nested cost is inside the plugin total, never double counted out of it.
    assert cost.self_bytes <= cost.bytes


def test_importing_the_same_plugin_twice_measures_once(ledger, fake_plugin_root):
    ledger.install()
    try:
        _import_via_builtins("data.plugins.alpha.main")
        first = ledger.plugin_cost("alpha").bytes
        _import_via_builtins("data.plugins.alpha.main")
    finally:
        ledger.uninstall()

    assert ledger.plugin_cost("alpha").bytes == first


def test_an_uninstalled_hook_stops_measuring(ledger, fake_plugin_root):
    ledger.install()
    ledger.uninstall()

    _import_via_builtins("data.plugins.alpha.main")

    assert ledger.plugins == {}


def test_nested_absolute_package_import_is_not_booked_twice(ledger, tmp_path):
    package = tmp_path / "mscope_nested_dep"
    package.mkdir()
    (package / "__init__.py").write_text(
        "import mscope_nested_dep.submodule\n",
        encoding="utf-8",
    )
    (package / "submodule.py").write_text(
        "PAYLOAD = [0] * 1024\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    importlib.invalidate_caches()
    try:
        ledger.install()
        try:
            _import_via_builtins("mscope_nested_dep")
        finally:
            ledger.uninstall()
    finally:
        sys.path.remove(str(tmp_path))
        for name in list(sys.modules):
            if name.startswith("mscope_nested_dep"):
                del sys.modules[name]

    package_cost = ledger.packages["mscope_nested_dep"]
    assert package_cost.imports == 1

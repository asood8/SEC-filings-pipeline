import importlib


def test_package_is_installed() -> None:
    # Placeholder until real tests land: proves the workspace member is importable.
    assert importlib.import_module("filings_common").__doc__

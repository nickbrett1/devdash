"""Smoke test: the src-layout package installs and imports cleanly."""


def test_package_imports():
    import devdash

    assert devdash.__version__

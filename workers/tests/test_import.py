import sensei_workers


def test_package_imports() -> None:
    assert sensei_workers.__version__ == "0.1.0"

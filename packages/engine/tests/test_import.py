import sensei_engine


def test_package_imports() -> None:
    assert sensei_engine.__version__ == "0.1.0"

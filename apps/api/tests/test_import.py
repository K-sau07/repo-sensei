import sensei_api


def test_package_imports() -> None:
    assert sensei_api.__version__ == "0.1.0"

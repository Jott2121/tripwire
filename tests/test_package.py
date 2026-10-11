import tripwire


def test_version_is_a_string():
    assert isinstance(tripwire.__version__, str)
    assert tripwire.__version__

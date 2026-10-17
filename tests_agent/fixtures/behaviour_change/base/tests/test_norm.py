from pkg.norm import normalise


def test_normalise():
    assert isinstance(normalise("  Mixed Case "), str)

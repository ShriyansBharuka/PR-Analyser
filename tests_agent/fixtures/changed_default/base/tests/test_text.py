from pkg.text import greet


def test_greet():
    assert greet("Ann").startswith("Hello Ann")

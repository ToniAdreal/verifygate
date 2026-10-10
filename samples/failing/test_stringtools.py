from stringtools import slugify, word_count


def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"


def test_slugify_punctuation():
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_collapses_separators():
    assert slugify("  Multiple   Spaces  ") == "multiple-spaces"


def test_word_count():
    assert word_count("the quick brown fox") == 4
    assert word_count("") == 0

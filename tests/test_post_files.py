import pytest

from src.bot import MAX_MESSAGE_LENGTH, PostFileError, is_allowed_user, is_post_filename, read_post_html


def test_recognizes_tgpost_html_filename():
    assert is_post_filename("launch.tgpost.html")


@pytest.mark.parametrize("filename", [None, "launch.html", "launch.tgpost.htm", "launch.TGPOST.HTML"])
def test_rejects_other_filenames(filename):
    assert not is_post_filename(filename)


def test_decodes_utf8_and_preserves_the_supplied_html():
    html = "<b>Привет</b> <code>exact source</code>"
    assert read_post_html(html.encode("utf-8")) == html


def test_rejects_invalid_utf8():
    with pytest.raises(PostFileError, match="UTF-8"):
        read_post_html(bytes([0xFF]))


def test_rejects_empty_file():
    with pytest.raises(PostFileError, match="empty"):
        read_post_html(b"  \n\t")


def test_rejects_oversized_post():
    with pytest.raises(PostFileError, match="too long"):
        read_post_html(b"x" * (MAX_MESSAGE_LENGTH + 1))


def test_allowed_user_filtering():
    assert is_allowed_user(42, 42)
    assert not is_allowed_user(43, 42)
    assert not is_allowed_user(None, 42)

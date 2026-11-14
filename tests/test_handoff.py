"""
Tests for stream borrowing/take-over and reader handoff.

A sampler reads a fixed number of records from an external binary stream and
then hands the stream to a main parser. The handoff must:

- distinguish a *borrowed* handle (never closed) from a *taken over* one,
- forward the read-ahead but not yet consumed business bytes,
- behave identically for plain and compressed input,
- leave the borrowed handle open when the first reader exits abnormally, and
- reproduce the original input byte for byte when the consumed prefix and the
  successor's output are concatenated.
"""

import io
import shutil
import subprocess
import sys

import pytest

from xopen import Handoff, handoff, xopen

if sys.version_info >= (3, 14):
    from compression import zstd
else:
    from backports import zstd

try:
    import lz4.frame  # noqa: F401

    has_lz4 = True
except ImportError:
    has_lz4 = False


# Extensions that can be round-tripped in this environment.
extensions = ["", ".gz", ".bz2", ".xz"]
if shutil.which("zstd") or zstd:
    extensions.append(".zst")
if has_lz4:
    extensions.append(".lz4")

# Native (threads=0) plus, where an external tool exists, the piped path.
thread_settings = [0, None]


def _payload(kb: int = 256) -> bytes:
    # Line-oriented content comfortably larger than any read-ahead buffer, so
    # both readline() and buffering of unconsumed bytes get exercised.
    line = b"sampler record %06d with some padding text\n"
    lines = [line % i for i in range(kb * 1024 // len(line % 0))]
    return b"".join(lines)


@pytest.fixture
def data_file(tmp_path):
    def _make(extension: str, data: bytes) -> str:
        path = tmp_path / f"input{extension}"
        with xopen(path, "wb") as f:
            f.write(data)
        return str(path)

    return _make


@pytest.mark.parametrize("extension", extensions)
@pytest.mark.parametrize("threads", thread_settings)
def test_binary_handoff_is_byte_identical_to_single_read(extension, threads, data_file):
    data = _payload()
    path = data_file(extension, data)
    # A single, uninterrupted read of the whole stream is the reference.
    with xopen(path, "rb", threads=threads) as f:
        reference = f.read()

    external = open(path, "rb")
    try:
        sampler = xopen(external, "rb", threads=threads)
        consumed = sampler.readline()
        for _ in range(99):
            consumed += sampler.readline()

        result = handoff(sampler)
        assert isinstance(result, Handoff)
        assert result.encoding is None
        # The first reader may now be torn down (even abnormally) without
        # affecting the successor or the borrowed external handle.
        sampler.close()
        assert not external.closed

        successor = result.open()
        remainder = successor.read()
        successor.close()
        assert not external.closed
    finally:
        external.close()

    assert consumed + remainder == reference
    assert consumed + remainder == data


@pytest.mark.parametrize("extension", extensions)
@pytest.mark.parametrize("threads", thread_settings)
def test_text_handoff_is_byte_identical(extension, threads, data_file):
    data = _payload()
    path = data_file(extension, data)
    text = data.decode("utf-8")

    external = open(path, "rb")
    try:
        sampler = xopen(external, "rt", threads=threads)
        consumed = "".join(sampler.readline() for _ in range(50))
        result = handoff(sampler)
        assert result.is_text
        assert result.encoding == "utf-8"
        sampler.close()
        assert not external.closed

        successor = result.open()
        assert isinstance(successor, io.TextIOWrapper)
        remainder = successor.read()
        successor.close()
        assert not external.closed
    finally:
        external.close()

    assert consumed + remainder == text
    # The byte-level contract must still hold.
    assert (consumed + remainder).encode("utf-8") == data


@pytest.mark.parametrize("extension", extensions)
@pytest.mark.parametrize("threads", thread_settings)
def test_pending_bytes_are_the_unconsumed_read_ahead(extension, threads, data_file):
    data = _payload()
    path = data_file(extension, data)

    external = open(path, "rb")
    try:
        sampler = xopen(external, "rb", threads=threads)
        consumed = sampler.read(1234)
        result = handoff(sampler)
        sampler.close()
    finally:
        external.close()

    # Read-ahead happened, so there is buffered but unconsumed business data.
    assert result.pending
    # The pending bytes sit immediately after the consumed prefix.
    assert data.startswith(consumed + result.pending)


@pytest.mark.parametrize("extension", extensions)
@pytest.mark.parametrize("threads", thread_settings)
def test_successor_supports_chunked_readinto_and_readline(
    extension, threads, data_file
):
    data = _payload()
    path = data_file(extension, data)

    external = open(path, "rb")
    try:
        sampler = xopen(external, "rb", threads=threads)
        prefix = b"".join(sampler.readline() for _ in range(30))
        result = handoff(sampler)
        sampler.close()

        # Mixed access pattern across the prefix/remainder boundary.
        successor = result.stream
        chunks = []
        buffer = bytearray(137)
        view = memoryview(buffer)
        for _ in range(50):
            n = successor.readinto(view)
            if n == 0:
                break
            chunks.append(bytes(view[:n]))
        chunks.append(successor.readline())
        chunks.append(successor.read())
        successor.close()
        assert not external.closed
    finally:
        external.close()

    assert prefix + b"".join(chunks) == data


@pytest.mark.parametrize("extension", extensions)
def test_abnormal_first_reader_exit_keeps_borrowed_handle(extension, data_file):
    class SamplerFailed(RuntimeError):
        pass

    data = _payload()
    path = data_file(extension, data)

    external = open(path, "rb")
    try:
        with pytest.raises(SamplerFailed):
            with xopen(external, "rb") as sampler:
                sampler.readline()
                raise SamplerFailed("boom")
        # The borrowed external handle must survive the abnormal exit.
        assert not external.closed
        assert external.readable()
    finally:
        external.close()


@pytest.mark.parametrize("extension", extensions)
def test_handoff_then_abnormal_exit_still_reads_correctly(extension, data_file):
    data = _payload()
    path = data_file(extension, data)

    external = open(path, "rb")
    try:
        sampler = xopen(external, "rb")
        consumed = sampler.readline()
        result = handoff(sampler)
        # Simulate the first reader crashing after handing off.
        sampler.close()
        assert not external.closed

        successor = result.open()
        remainder = successor.read()
        successor.close()
    finally:
        external.close()

    assert consumed + remainder == data


@pytest.mark.parametrize("extension", extensions)
def test_take_over_handoff_closes_handle_via_successor(extension, data_file):
    data = _payload()
    path = data_file(extension, data)

    handle = open(path, "rb")
    sampler = xopen(handle, "rb", closefd=True)
    consumed = sampler.readline()
    result = handoff(sampler)
    # The disarmed first reader cannot release the handle before handoff.
    sampler.close()
    assert not handle.closed

    successor = result.open()
    remainder = successor.read()
    successor.close()  # take-over: the successor owns and releases the handle
    assert handle.closed
    assert consumed + remainder == data


def test_borrow_is_the_default_for_file_objects(data_file):
    path = data_file("", b"hello\n")
    handle = open(path, "rb")
    reader = xopen(handle, "rb")
    reader.close()
    assert not handle.closed
    handle.close()


def test_explicit_borrow_never_closes_handle(data_file):
    path = data_file(".gz", b"hello\n")
    handle = open(path, "rb")
    reader = xopen(handle, "rb", closefd=False)
    reader.close()
    assert not handle.closed
    handle.close()


@pytest.mark.parametrize("extension", extensions)
@pytest.mark.parametrize("threads", thread_settings)
def test_take_over_closes_the_handle(extension, threads, data_file):
    path = data_file(extension, b"hello\n")
    handle = open(path, "rb")
    reader = xopen(handle, "rb", threads=threads, closefd=True)
    reader.close()
    assert handle.closed


@pytest.mark.parametrize("extension", extensions)
def test_borrow_survives_in_text_mode(extension, data_file):
    path = data_file(extension, b"hello\nworld\n")
    handle = open(path, "rb")
    reader = xopen(handle, "rt", closefd=False)
    assert reader.readline() == "hello\n"
    reader.close()
    assert not handle.closed
    handle.close()


def test_borrow_of_path_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="closefd"):
        xopen(tmp_path / "cannot-borrow-a-path", "rb", closefd=False)


def test_invalid_type_still_raises_type_error():
    with pytest.raises(TypeError):
        xopen(123, "rb")


def test_handoff_without_read_ahead_buffer(tmp_path):
    # An unbuffered raw file has no peek() and therefore no buffered surplus.
    data = b"plain bytes without an internal buffer\n" * 100
    path = tmp_path / "unbuffered.bin"
    path.write_bytes(data)
    external = open(path, "rb", buffering=0)  # io.FileIO: no peek
    assert not hasattr(external, "peek")
    reader = xopen(external, "rb")
    consumed = reader.read(10)
    result = handoff(reader)
    assert result.pending == b""
    reader.close()
    assert not external.closed
    assert consumed + result.stream.read() == data
    result.stream.close()
    external.close()


@pytest.mark.parametrize("extension", extensions)
@pytest.mark.parametrize("threads", thread_settings)
def test_repeated_handoffs_stay_byte_identical(extension, threads, data_file):
    data = _payload()
    path = data_file(extension, data)

    external = open(path, "rb")
    try:
        reader = xopen(external, "rb", threads=threads)
        assembled = reader.readline()
        for _ in range(5):
            reader = handoff(reader).stream
            assembled += reader.readline()
        assembled += reader.read()
        reader.close()
        assert not external.closed
    finally:
        external.close()

    assert assembled == data


@pytest.mark.parametrize("extension", extensions)
@pytest.mark.parametrize("threads", thread_settings)
def test_handoff_at_eof_yields_empty_successor(extension, threads, data_file):
    data = _payload()
    path = data_file(extension, data)

    external = open(path, "rb")
    try:
        reader = xopen(external, "rb", threads=threads)
        consumed = reader.read()
        result = handoff(reader)
        assert result.pending == b""
        assert result.stream.read() == b""
        reader.close()
        assert not external.closed
    finally:
        external.close()

    assert consumed == data


@pytest.mark.parametrize("extension", extensions)
def test_handoff_over_an_external_pipe(extension, data_file):
    data = _payload()
    path = data_file(extension, data)

    process = subprocess.Popen(("cat", path), stdout=subprocess.PIPE)
    try:
        reader = xopen(process.stdout, "rb")  # non-seekable, borrowed
        consumed = b"".join(reader.readline() for _ in range(40))
        result = handoff(reader)
        reader.close()
        assert not process.stdout.closed
        remainder = result.stream.read()
        result.stream.close()
    finally:
        process.wait()
        process.stdout.close()

    assert consumed + remainder == data


def test_text_handoff_preserves_encoding_and_newline(tmp_path):
    data = "line one\r\nline two\r\nline three\r\n".encode("latin-1")
    path = tmp_path / "crlf.txt"
    path.write_bytes(data)

    handle = open(path, "rb")
    try:
        sampler = xopen(handle, "rt", encoding="latin-1", newline="")
        first = sampler.readline()
        result = handoff(sampler)
        assert result.encoding == "latin-1"
        assert result.newline == ""
        successor = result.open()
        rest = successor.read()
        successor.close()
        assert not handle.closed
    finally:
        handle.close()

    assert (first + rest).encode("latin-1") == data
    # CR characters are retained because newline="" disables translation.
    assert "\r\n" in first + rest

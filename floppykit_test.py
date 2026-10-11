import os
import pytest
import floppykit as dk


def fresh():
    return dk.new_image(1440, "TEST")


def reopen(img):
    """Re-parse the image from bytes so we test what was really written."""
    return dk.Fat12(dk.RawImage(bytes(img.data)))


def test_put_and_read_back():
    img = fresh()
    data = os.urandom(5000)
    dk.Fat12(img).put("", "multi.bin", data)
    fs = reopen(img)
    assert fs.read_file(fs.resolve("MULTI.BIN")[2]) == data


def test_empty_file():
    img = fresh()
    dk.Fat12(img).put("", "empty.dat", b"")
    fs = reopen(img)
    assert fs.read_file(fs.resolve("EMPTY.DAT")[2]) == b""


def test_subdirectory():
    img = fresh()
    fs = dk.Fat12(img)
    fs.mkdir("", "SUB")
    fs.put("SUB", "a.txt", b"hi")
    fs2 = reopen(img)
    assert fs2.read_file(fs2.resolve("SUB/A.TXT")[2]) == b"hi"


def test_rm_frees_space():
    img = fresh()
    fs = dk.Fat12(img)
    before = fs.free_clusters()
    fs.put("", "big.bin", os.urandom(10000))
    fs.rm("big.bin")
    assert fs.free_clusters() == before


def test_duplicate_rejected():
    fs = dk.Fat12(fresh())
    fs.put("", "a.txt", b"1")
    with pytest.raises(FileExistsError):
        fs.put("", "a.txt", b"2")


def test_bad_name_rejected():
    with pytest.raises(dk.FloppyKitError):
        dk.to_83("waytoolongname.txt")


def test_disk_full():
    fs = dk.Fat12(dk.new_image(160))
    with pytest.raises(dk.FloppyKitError):
        fs.put("", "huge.bin", os.urandom(200_000))


def raw_to_imd(raw, g):
    out = bytearray(b"IMD 1.18: test\x1a")
    for c in range(g.cylinders):
        for h in range(g.heads):
            out += bytes([5, c, h, g.sectors_per_track, 2])
            out += bytes(range(1, g.sectors_per_track + 1))
            for s in range(g.sectors_per_track):
                lba = (c * g.heads + h) * g.sectors_per_track + s
                out += b"\x01" + raw[lba * 512:(lba + 1) * 512]
    return bytes(out)


def test_imd_roundtrip(tmp_path):
    img = fresh()
    dk.Fat12(img).put("", "x.bin", os.urandom(3000))
    raw = bytes(img.data)
    p = tmp_path / "t.imd"
    p.write_bytes(raw_to_imd(raw, img.geometry))
    flat, missing = dk.image_to_bytes(dk.open_image(str(p)))
    assert flat == raw and not missing


def test_missing_sector_is_reported(tmp_path):
    img = fresh()
    raw = bytes(img.data)
    imd = bytearray(raw_to_imd(raw, img.geometry))
    start = imd.index(b"\x1a") + 1 + 5 + img.geometry.sectors_per_track
    imd[start:start + 1 + 512] = b"\x00"
    p = tmp_path / "bad.imd"
    p.write_bytes(bytes(imd))
    assert dk.open_image(str(p)).bad_sectors()
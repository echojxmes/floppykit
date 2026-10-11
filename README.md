# floppykit

Open, inspect, convert and edit old floppy disk images, safely.

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

Old software lives on floppy disks that are slowly dying, and preserving them
usually means juggling several tools. floppykit is one small command-line tool that can:

- identify an image (format, geometry, filesystem, label, free space)
- report problems such as bad sectors or mismatched FAT copies
- list and extract files and folders from FAT12 disks
- convert ImageDisk (`.imd`) images to raw `.img`
- add, delete and create files and folders in an image
- create new blank floppy images (160 KB to 2.88 MB)

Your original image is never modified. Every command that changes something
writes a new file, and floppykit refuses to overwrite the input.

## Quick start

No installation or dependencies, just Python 3.8 or newer.

```bash
git clone https://github.com/YOUR-USERNAME/floppykit.git
cd floppykit
python floppykit.py info path/to/disk.img
```

Example:

```console
$ python floppykit.py info test.img
format      : raw
geometry    : 80 cylinders, 2 heads, 18 sectors/track, 512 bytes/sector
capacity    : 1440 KB
filesystem  : FAT12, OEM 'DISKKIT', label 'TESTDISK'
clusters    : 2847 x 512 bytes, 2637 free (1318 KB)
findings    : none
```

## Commands

| Command | What it does |
|---|---|
| `info disk.img` | Show format, geometry, filesystem and any problems |
| `ls disk.img [path] [-r]` | List files (`-r` includes folders' contents) |
| `get disk.img path [-o dir]` | Extract a file or folder (`/` for everything) |
| `convert in.imd out.img` | Convert any readable image to raw |
| `new out.img [--size 1440] [--label NAME]` | Create an empty FAT12 image |
| `put disk.img file... -o out.img [--dir PATH]` | Add files to a new image |
| `mkdir disk.img PATH... -o out.img` | Create folders in a new image |
| `rm disk.img PATH... -o out.img` | Delete files or empty folders in a new image |

Supported formats: raw sector images (`.img`, `.ima`, `.dsk`) and ImageDisk (`.imd`, read-only).
Supported filesystem: FAT12.

## Known limits

- FAT12 only (no FAT16/FAT32)
- 8.3 filenames only; long names are skipped when reading and not created when writing
- Folders can't grow past their first cluster
- No HFE support yet
- Written images have only been checked with this tool's own reader, so please
  report any disk that another tool won't open

## Tests

```bash
pip install pytest
python -m pytest -v
```

## Contributing

Bug reports are the most useful contribution, especially a disk image that breaks
it. Only share images that are legal to share (homemade, public domain, or freely
licensed such as FreeDOS). Please don't upload images of commercial software.

## License

MIT. See [LICENSE](LICENSE).

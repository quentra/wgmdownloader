"""Zip the built program folder for the release.

Usage: python ci/package.py dist/wgmdownloader dist/wgmdownloader-windows.zip

The zip holds one top-level folder (wgmdownloader/), so unzipping never scatters files.
"""

import os
import sys
import zipfile


def main():
    folder, out = os.path.abspath(sys.argv[1]), sys.argv[2]
    top = os.path.basename(folder)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames.sort()
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                arc = os.path.join(top, os.path.relpath(path, folder)).replace(os.sep, "/")
                z.write(path, arc)
    print("%s: %d bytes" % (out, os.path.getsize(out)))


if __name__ == "__main__":
    main()

"""Write THIRD_PARTY_NOTICES.txt for the release: the bundled wget (GPL-3.0) and the
bundled Python runtime (PSF licence, which also lists Tcl/Tk and the other parts of the
Windows Python build).

Usage: python ci/notices.py <path to the bundled wget.exe> <output file>
"""

import os
import subprocess
import sys

# The repository's LICENSE is the FSF's GPL v3 text, the same licence wget uses.
GPL_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "LICENSE")


def main():
    wget, out = sys.argv[1], sys.argv[2]
    version = subprocess.run([wget, "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    with open(GPL_FILE, encoding="utf-8") as f:
        gpl = f.read()
    if "GNU GENERAL PUBLIC LICENSE" not in gpl or "Version 3" not in gpl:
        sys.exit("LICENSE is not the GPL v3 text")
    python_license = os.path.join(sys.base_prefix, "LICENSE.txt")
    with open(python_license, encoding="utf-8", errors="replace") as f:
        psf = f.read()
    rule = "=" * 78
    with open(out, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("wgmdownloader.exe bundles the following third-party software.\n\n")
        f.write(rule + "\nGNU Wget\n" + rule + "\n")
        f.write("Version: %s\n" % version)
        f.write("Windows build by Jernej Simoncic: https://eternallybored.org/misc/wget/\n")
        f.write("Source code: https://ftp.gnu.org/gnu/wget/\n")
        f.write("Licence: GNU General Public License v3.0, reproduced below.\n")
        f.write("wget runs as a separate program; WGMDownloader starts it and reads its output.\n\n")
        f.write(gpl + "\n\n")
        f.write(rule + "\nPython %d.%d.%d runtime (packaged with PyInstaller)\n" % sys.version_info[:3] + rule + "\n")
        f.write(psf + "\n")


if __name__ == "__main__":
    main()

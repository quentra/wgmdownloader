"""Write THIRD_PARTY_NOTICES.txt for the release: the bundled wget (GPL-3.0) and the
bundled Python runtime (PSF licence, which also lists Tcl/Tk and the other parts of the
Windows Python build).

Usage: python ci/notices.py <path to the bundled wget.exe> <output file>
"""

import os
import subprocess
import sys
import urllib.request

GPL_URL = "https://www.gnu.org/licenses/gpl-3.0.txt"


def main():
    wget, out = sys.argv[1], sys.argv[2]
    version = subprocess.run([wget, "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    gpl = urllib.request.urlopen(GPL_URL, timeout=60).read().decode("utf-8")
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

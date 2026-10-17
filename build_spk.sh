#!/bin/sh
set -eu
PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/payload/vendor" "$STAGE/spk"
cp -R "$PROJECT/tagger" "$PROJECT/web" "$STAGE/payload/"
cp -R "$PROJECT/packaging/ui" "$STAGE/payload/ui"
if [ -n "${MUTAGEN_SOURCE:-}" ]; then
    cp -R "$MUTAGEN_SOURCE/mutagen" "$STAGE/payload/vendor/"
    cp -R "$MUTAGEN_SOURCE"/mutagen-*.dist-info "$STAGE/payload/vendor/"
else
    python3 -m pip install --disable-pip-version-check --no-cache-dir --target "$STAGE/payload/vendor" 'mutagen==1.47.0'
fi
find "$STAGE/payload" -type d -name __pycache__ -prune -exec rm -rf {} +
cat > "$STAGE/spk/INFO" <<'EOF'
package="Cadence"
version="0.1.0-0054"
displayname="Cadence Music Tagger"
description="Edit, enrich and organize music metadata on your NAS"
arch="noarch"
os_min_ver="7.0-40000"
maintainer="Cadence"
install_dep_packages="WebStation"
startable="yes"
adminprotocol="https"
adminport="8788"
adminurl="/"
dsmuidir="ui"
dsmappname="com.cadence.Tagger"
EOF
cp -R "$PROJECT/packaging/scripts" "$PROJECT/packaging/conf" "$PROJECT/packaging/WIZARD_UIFILES" "$STAGE/spk/"
chmod 755 "$STAGE/spk/scripts/"*
python3 - "$STAGE/spk" "$STAGE/payload/ui/images" <<'PY'
import struct, sys, zlib
from pathlib import Path
spk, images = Path(sys.argv[1]), Path(sys.argv[2])
images.mkdir(parents=True, exist_ok=True)
def png(size):
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            circle = (x-size/2)**2+(y-size/2)**2 < (size*.34)**2
            diagonal = abs(x-y) < size*.07
            row.extend((213, 173, 112, 255) if circle and diagonal else (25, 40, 43, 255))
        rows.append(b'\x00'+bytes(row))
    def chunk(kind, data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',size,size,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(rows)))+chunk(b'IEND',b'')
for size in (16,24,32,48,64,72,256):
    (images / ('app_%s.png'%size)).write_bytes(png(size))
(spk/'PACKAGE_ICON.PNG').write_bytes(png(64))
(spk/'PACKAGE_ICON_256.PNG').write_bytes(png(256))
PY
tar -C "$STAGE/payload" -czf "$STAGE/spk/package.tgz" tagger web ui vendor
printf 'extractsize="%s"\n' "$(du -sk "$STAGE/payload" | cut -f1)" >> "$STAGE/spk/INFO"
tar -C "$STAGE/spk" -cf "$PROJECT/Cadence-0.1.0-0054.spk" \
    INFO package.tgz PACKAGE_ICON.PNG PACKAGE_ICON_256.PNG WIZARD_UIFILES conf scripts
echo "$PROJECT/Cadence-0.1.0-0054.spk"

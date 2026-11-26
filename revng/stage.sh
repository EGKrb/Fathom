#!/bin/bash
# Stages rev.ng for Windows in a self-contained folder (Fathom's revng\dist): MSYS2 CLANG64's Python with the packages
# rev.ng uses, rev.ng (its Python package with _pipebox.pyd, its share\revng), libtcg with the DLLs it imports.
#   stage-revng.sh <dist folder> [<build-revng> <revng-install> <python venv>]
set -e
DIST=${1:?dist folder}
BUILD=${2:-/c/Users/Elio/revng-port/build-revng}
INSTALL=${3:-/c/Users/Elio/revng-port/revng-install}
VENV=${4:-/c/Users/Elio/revng-port/pyenvc}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=python3.14
SYS=/clang64

rm -rf "$DIST"
mkdir -p "$DIST/bin" "$DIST/lib" "$DIST/share"

# 1. Python: the interpreter, the standard library (without its tests, IDLE, Tk)
cp "$SYS/bin/$PY.exe" "$DIST/bin/python.exe"
mkdir -p "$DIST/lib/$PY"
tar -C "$SYS/lib/$PY" --exclude=site-packages --exclude=test --exclude=idlelib --exclude=tkinter \
    --exclude=turtledemo --exclude=ensurepip --exclude=__pycache__ -cf - . | tar -C "$DIST/lib/$PY" -xf -
SITE="$DIST/lib/$PY/site-packages"
mkdir -p "$SITE"

# 2. The packages rev.ng imports: MSYS2's (pacman), then the venv's (pip), without the build tools
EXCLUDE='^(black|blackd|blib2to3|_black_version.*|pip|pip-.*|nanobind|nanobind-.*|clang|lldb|setuptools|setuptools-.*|_distutils_hack|distutils-precedence.pth|.*__mypyc.*|libxml2.*|drv_libxml2.py|pytest|_pytest|pytest-.*|README.txt|__pycache__)$'
for source in "$SYS/lib/$PY/site-packages" "$VENV/lib/$PY/site-packages"; do
  for item in "$source"/*; do
    name=$(basename "$item")
    if [[ "$name" =~ $EXCLUDE ]]; then continue; fi
    rm -rf "$SITE/$name"
    cp -r "$item" "$SITE/"
  done
done

# 3. rev.ng: its command line, its Python package (with _pipebox.pyd), share/revng
cp "$BUILD/bin/revng" "$DIST/bin/revng"
cp -r "$BUILD/lib/$PY/site-packages/revng" "$SITE/"
cp -r "$BUILD/share/revng" "$DIST/share/"
rm -rf "$DIST/share/revng/test" "$DIST/share/revng/doc"
# the line tables of the build (debugging) are not shipped
strip --strip-debug "$SITE/revng/internal/_pipebox.pyd"

# 4. libtcg (lib\, with the DLLs it imports next to it) and the helpers
cp "$INSTALL"/lib/*.dll "$DIST/lib/"
cp -r "$INSTALL/share/libtcg" "$DIST/share/"

# 5. The DLLs of Python, of its extension modules and of _pipebox.pyd (bin\, next to python.exe)
modules=("$DIST/bin/python.exe")
while IFS= read -r -d '' f; do modules+=("$f"); done < <(find "$DIST/lib/$PY" -name '*.pyd' -print0)
python "$HERE/collect-dlls.py" "$DIST/bin" "$(cygpath -m $SYS/bin)" "${modules[@]}"
# psycopg loads libpq with ctypes
cp "$SYS/bin/libpq.dll" "$DIST/bin/"
python "$HERE/collect-dlls.py" "$DIST/bin" "$(cygpath -m $SYS/bin)" "$DIST/bin/libpq.dll"

# Tk is not used
rm -f "$DIST/lib/$PY/lib-dynload/"_tkinter*.pyd "$DIST/bin/tcl86.dll" "$DIST/bin/tk86.dll"

find "$DIST" -name __pycache__ -type d -prune -exec rm -rf {} +
# compiled once here (no SyntaxWarning of old packages at each run, faster start)
"$DIST/bin/python.exe" -W ignore -m compileall -q -j 0 "$DIST/lib/$PY" > /dev/null || true

# revng.cmd: rev.ng's command line, with bin\ on the PATH (psycopg loads libpq by name)
cat > "$DIST/revng.cmd" <<'EOF'
@echo off
setlocal
set "PATH=%~dp0bin;%PATH%"
"%~dp0bin\python.exe" -X utf8 "%~dp0bin\revng" %*
EOF
unix2dos -q "$DIST/revng.cmd" 2>/dev/null || sed -i 's/$/\r/' "$DIST/revng.cmd"

du -sh "$DIST"
echo STAGED

#!/bin/bash
# Builds rev.ng for Windows and stages it in revng/dist (see README.md). Run from MSYS2 (build.ps1 does it):
#   bash revng/build.sh                 everything (hours the first time: LLVM 16 with clang and MLIR)
#   bash revng/build.sh <step> ...      some steps: packages fetch libtcg llvm helpers python revng stage
# REVNG_WORK: the work folder (default %USERPROFILE%\revng-build, a short path); JOBS: parallel jobs.
#
# Toolchains: libtcg (C, QEMU) with MSYS2 UCRT64's gcc; LLVM 16 and rev.ng with MSYS2 CLANG64's clang, whose C++
# library is libc++, the one rev.ng is written for.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(cygpath -u "${REVNG_WORK:-$USERPROFILE/revng-build}")
JOBS=${JOBS:-$(nproc)}
PREFIX="$WORK/revng-install"
LLVM="$WORK/llvm16-install"
PY=python3.14

# rev.ng's forks, at the commits this port was made against
QEMU_REPO=https://github.com/revng/qemu.git
QEMU_COMMIT=71227211d08af04a909e7aeae069720e2ca39cdc
LLVM_REPO=https://github.com/revng/llvm-project.git
LLVM_COMMIT=42e31c52c2b09f6665133df158007e6be5f1a0db
NANOBIND_REPO=https://github.com/revng/nanobind.git
NANOBIND_COMMIT=a111828dd36d1ce3c8443d2bfc74ac292169a0f3

# a command in one of MSYS2's environments (login shell: its PATH)
in_env() {
  local env=$1
  shift
  MSYSTEM=$env CHERE_INVOKING=1 /usr/bin/bash -lc "$*"
}

step_packages() {
  local u=mingw-w64-ucrt-x86_64 c=mingw-w64-clang-x86_64
  pacman -S --noconfirm --needed git tar unzip \
    $u-gcc $u-meson $u-ninja $u-pkgconf $u-python $u-glib2 $u-capstone $u-dlfcn \
    $c-clang $c-lld $c-cmake $c-ninja $c-boost $c-python $c-python-pip $c-libarchive $c-dlfcn $c-pkgconf \
    $c-postgresql \
    $(for p in yaml jinja click jsonschema psutil pyelftools pycparser networkx starlette httpx pefile markupsafe \
                wsproto h2 black; do echo $c-python-$p; done)
}

fetch() { # <folder> <repository> <commit>
  if [ ! -d "$1/.git" ]; then
    git init -q "$1"
    git -C "$1" config core.autocrlf false
    git -C "$1" remote add origin "$2"
  fi
  if [ "$(git -C "$1" rev-parse HEAD 2>/dev/null)" != "$3" ]; then
    git -C "$1" fetch -q --depth 1 origin "$3"
    git -C "$1" checkout -q -f FETCH_HEAD
  fi
}

step_fetch() {
  mkdir -p "$WORK"
  fetch "$WORK/qemu" "$QEMU_REPO" "$QEMU_COMMIT"
  git -C "$WORK/qemu" apply --check "$HERE/patches/qemu.patch" 2>/dev/null && git -C "$WORK/qemu" apply "$HERE/patches/qemu.patch"
  fetch "$WORK/llvm-project" "$LLVM_REPO" "$LLVM_COMMIT"
  fetch "$WORK/nanobind" "$NANOBIND_REPO" "$NANOBIND_COMMIT"
  git -C "$WORK/nanobind" -c core.autocrlf=false submodule update -q --init --recursive
}

step_libtcg() {
  in_env UCRT64 "mkdir -p '$WORK/build-libtcg' && cd '$WORK/build-libtcg' && \
    ../qemu/configure --target-list='x86_64-libtcg i386-libtcg' \
      --disable-werror --disable-docs --disable-kvm --disable-tools --disable-system \
      --disable-plugins --disable-libnfs --disable-vde --disable-gnutls --disable-cap-ng \
      -Dvhost_user=disabled -Dxkbcommon=disabled --prefix='$WORK/libtcg-install' && \
    ninja -j $JOBS libtcg-x86_64.dll libtcg-i386.dll"
  mkdir -p "$PREFIX/lib" "$PREFIX/include/qemu/libtcg/tcg"
  cp "$WORK/build-libtcg"/libtcg-*.dll "$PREFIX/lib/"
  cp "$WORK/qemu/include/libtcg/libtcg.h" "$PREFIX/include/qemu/libtcg/"
  cp "$WORK/qemu/include/tcg/tcg-opc.h" "$PREFIX/include/qemu/libtcg/tcg/"
  # the DLLs libtcg imports, next to it (rev.ng loads it with its path)
  in_env UCRT64 "python '$HERE/collect-dlls.py' '$PREFIX/lib' \"\$(cygpath -m /ucrt64/bin)\" '$PREFIX'/lib/libtcg-*.dll"
}

step_llvm() {
  in_env CLANG64 "cmake -S '$WORK/llvm-project/llvm' -B '$WORK/build-llvm16' -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ -DLLVM_USE_LINKER=lld \
    '-DCMAKE_CXX_FLAGS=-include cstdint -include type_traits' -DCMAKE_CXX_STANDARD=20 \
    '-DLLVM_ENABLE_PROJECTS=clang;mlir' -DLLVM_TARGETS_TO_BUILD=X86 \
    -DLLVM_INCLUDE_TESTS=OFF -DLLVM_INCLUDE_BENCHMARKS=OFF -DLLVM_INCLUDE_EXAMPLES=OFF \
    -DCLANG_INCLUDE_TESTS=OFF -DMLIR_INCLUDE_TESTS=OFF -DMLIR_INCLUDE_INTEGRATION_TESTS=OFF \
    -DLLVM_ENABLE_ZLIB=OFF -DLLVM_ENABLE_ZSTD=OFF -DLLVM_ENABLE_LIBXML2=OFF -DLLVM_ENABLE_TERMINFO=OFF \
    -DLLVM_PARALLEL_LINK_JOBS=2 -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DLLVM_TOOL_SANCOV_BUILD=OFF \
    -DCMAKE_INSTALL_PREFIX='$LLVM' && \
    cmake --build '$WORK/build-llvm16' -j $JOBS && cmake --install '$WORK/build-llvm16'"
}

step_helpers() {
  # QEMU's helpers in LLVM 16 bitcode, with the options of the Windows libtcg (its clang needs CLANG64's DLLs)
  in_env UCRT64 "export PATH=\"\$PATH:/clang64/bin\"; python '$WORK/qemu/libtcg/win32/gen-helpers.py' \
    '$WORK/build-libtcg' '$LLVM/bin/clang.exe' '$LLVM/bin/llvm-link.exe' '$PREFIX/share/libtcg'"
}

step_python() {
  in_env CLANG64 "python -m venv --system-site-packages '$WORK/pyenv' && \
    '$WORK/pyenv/bin/python' -m pip install -q yachalk click-option-group hypercorn 'httpx2[http2]' llvmcpy marko \
      grandiso 'xdg==6.0.0' psycopg psycopg-pool httpx-retries python-idb"
}

step_revng() {
  local prefix_w
  prefix_w=$(cygpath -m "$PREFIX")
  in_env CLANG64 "cmake -S '$HERE/third_party/revng' -B '$WORK/build-revng' -G Ninja \
    -DCMAKE_BUILD_TYPE=Release '-DCMAKE_CXX_FLAGS_RELEASE=-O2 -gline-tables-only' '-DCMAKE_C_FLAGS_RELEASE=-O2' \
    -DCMAKE_C_COMPILER=/clang64/bin/clang.exe -DCMAKE_CXX_COMPILER=/clang64/bin/clang++.exe \
    '-DCMAKE_CXX_FLAGS=-include cstdint -include type_traits -I$prefix_w/include' \
    -DCMAKE_EXE_LINKER_FLAGS=-fuse-ld=lld -DCMAKE_SHARED_LINKER_FLAGS=-fuse-ld=lld -DCMAKE_MODULE_LINKER_FLAGS=-fuse-ld=lld \
    '-DCMAKE_PREFIX_PATH=$LLVM' '-DLLVM_DIR=$LLVM/lib/cmake/llvm' '-DMLIR_DIR=$LLVM/lib/cmake/mlir' \
    '-DClang_DIR=$LLVM/lib/cmake/clang' '-DCMAKE_INSTALL_PREFIX=$PREFIX' \
    '-DREVNG_NANOBIND_DIR=$(cygpath -m "$WORK/nanobind")' \
    '-DPython3_EXECUTABLE=$WORK/pyenv/bin/python.exe' '-DPython_EXECUTABLE=$WORK/pyenv/bin/python.exe' \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 && \
    cmake --build '$WORK/build-revng' -j $JOBS"
}

step_stage() {
  in_env CLANG64 "bash '$HERE/stage.sh' '$HERE/dist' '$WORK/build-revng' '$PREFIX' '$WORK/pyenv'"
}

steps=("$@")
[ ${#steps[@]} -eq 0 ] && steps=(packages fetch libtcg llvm helpers python revng stage)
for step in "${steps[@]}"; do
  echo "== $step"
  "step_$step"
done
echo "rev.ng staged in $HERE/dist"

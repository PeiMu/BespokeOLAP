#!/bin/bash
# Build the Bespoke CEB engine with -O3 -flto for benchmarking.
# Usage: bash benchmark/ceb/build_bespoke.sh
set -e

PROJ_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CEB_SRC="$PROJ_ROOT/BespokeOLAP_Artifacts/bespoke_ceb"
API_DIR="$PROJ_ROOT/misc/fasttest"
BUILD_DIR="$PROJ_ROOT/benchmark/ceb/build"

mkdir -p "$BUILD_DIR/obj"

CXX="${CXX:-g++}"
PKG_CFLAGS=$(pkg-config --cflags arrow parquet)
PKG_LIBS=$(pkg-config --libs arrow parquet)

CXXFLAGS="-g -std=c++20 -fPIC -O3 -flto -I$CEB_SRC -I$API_DIR"
LDFLAGS_SO="-shared -Wl,--build-id=sha1"

echo "Building Bespoke CEB engine..."
echo "  CXX: $CXX"
echo "  Source: $CEB_SRC"
echo "  Build: $BUILD_DIR"

# Compile object files
compile_obj() {
    local src="$1"
    local obj="$BUILD_DIR/obj/$(basename "${src%.cpp}.o")"
    echo "  CC $src"
    $CXX $CXXFLAGS $PKG_CFLAGS -c "$src" -o "$obj"
}

compile_obj "$API_DIR/loader_api.cpp"
compile_obj "$CEB_SRC/loader_impl.cpp"
compile_obj "$API_DIR/loader_utils.cpp"
compile_obj "$API_DIR/builder_api.cpp"
compile_obj "$CEB_SRC/builder_impl.cpp"
compile_obj "$API_DIR/query_api.cpp"
compile_obj "$CEB_SRC/query_impl.cpp"
compile_obj "$API_DIR/db.cpp"
compile_obj "$API_DIR/utils/build_id.cpp"

# Link shared libraries
echo "  LINK libloader.so"
$CXX $LDFLAGS_SO -o "$BUILD_DIR/libloader.so" \
    "$BUILD_DIR/obj/loader_api.o" "$BUILD_DIR/obj/loader_impl.o" "$BUILD_DIR/obj/loader_utils.o" \
    $PKG_LIBS

echo "  LINK libbuilder.so"
$CXX $LDFLAGS_SO -o "$BUILD_DIR/libbuilder.so" \
    "$BUILD_DIR/obj/builder_api.o" "$BUILD_DIR/obj/builder_impl.o" \
    $PKG_LIBS

echo "  LINK libquery.so"
$CXX $LDFLAGS_SO -o "$BUILD_DIR/libquery.so" \
    "$BUILD_DIR/obj/query_api.o" "$BUILD_DIR/obj/query_impl.o" \
    $PKG_LIBS

# Link main executable
echo "  LINK db"
$CXX -o "$BUILD_DIR/db" \
    "$BUILD_DIR/obj/db.o" "$BUILD_DIR/obj/build_id.o" \
    -ldl -lpthread $PKG_LIBS

echo "Done. Binary: $BUILD_DIR/db"
